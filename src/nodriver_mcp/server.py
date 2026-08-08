"""
nodriver-mcp: An undetected Chrome automation MCP server.

Uses nodriver (successor of undetected-chromedriver) as the browser backend,
providing the same MCP tool interface as chrome-devtools-mcp but without
exposing CDP/WebDriver fingerprints that get detected by anti-bot systems.
"""

import asyncio
import base64
import inspect
import json
import logging
import os
import shutil
import tempfile
import time
from typing import Annotated, Any, Literal

import nodriver as uc
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nodriver-mcp")


# ---------------------------------------------------------------------------
# Global browser state
# ---------------------------------------------------------------------------
_browser: uc.Browser | None = None
_browser_lock = asyncio.Lock()

# Chrome profile (user-data-dir) selection.
# Default None -> ephemeral temp profile that nodriver creates and auto-deletes,
# so multiple nodriver instances (Claude Desktop, Claude Code, VS Code, ...) can
# run at the same time without ever colliding on a shared profile. Selectable at
# runtime via use_profile()/use_temp_profile(); NODRIVER_USER_DATA_DIR still
# works as an explicit persistent override.
_selected_profile_dir: str | None = None
_selected_profile_name: str | None = None
_PROFILES_DIR = os.path.join(os.path.expanduser("~"), ".nodriver-mcp", "profiles")
# Runtime overrides for the clean-launch flags (None -> use the env-var default).
_enable_translate: bool | None = None
_enable_extensions: bool | None = None
# Arbitrary extra Chrome launch flags set at runtime via set_browser_flags.
_extra_browser_args: list[str] = []
# Unpacked extension directories to load at launch (manage_extensions).
_loaded_extensions: list[str] = []

# When set, attach to a Chrome that is already running on this host/port instead
# of launching one. Selected at runtime via use_running_browser(), or up front
# with NODRIVER_BROWSER_URL. Chrome locks its user-data-dir, so the profile
# holding a user's real logins can only be driven by attaching to it.
_connect_host: str | None = None
_connect_port: int | None = None
# Set once the user switches back to a self-launched browser, so that choice
# also overrides NODRIVER_BROWSER_URL rather than being undone by it.
_connect_disabled: bool = False


def _connect_target() -> tuple[str, int] | None:
    """Host and port of a running browser to attach to, or None to launch one."""
    if _connect_host is not None and _connect_port is not None:
        return _connect_host, _connect_port
    if _connect_disabled:
        return None
    url = os.environ.get("NODRIVER_BROWSER_URL", "").strip()
    if not url:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(url if "//" in url else f"http://{url}")
    if parsed.hostname and parsed.port:
        return parsed.hostname, parsed.port
    logger.warning(
        "NODRIVER_BROWSER_URL=%r has no host:port; launching a browser instead", url
    )
    return None

# URLs that count as "this tab is empty, reuse it instead of opening another".
_BLANK_URLS = frozenset({
    "", "about:blank", "chrome://newtab", "chrome://new-tab-page",
    "chrome://newtab/", "chrome://new-tab-page/",
})


def _is_blank_url(url: str | None) -> bool:
    """Whether a tab holds nothing worth keeping (startup tab / empty tab)."""
    return (url or "").strip() in _BLANK_URLS


# Targets that currently run with touch emulation on. Dispatching mouse events
# through the CDP Input domain can take such a renderer down (the websocket
# dies with "Connection closed" and the tab is gone), so click() uses the
# scripted fallback there instead. Kept in sync by _apply_emulation /
# _reset_emulation.
_touch_emulated_targets: set[str] = set()

# Upper bound for a single click step, so a wedged page can never hang the call.
_CLICK_TIMEOUT_S = 10.0


def _target_key(tab: uc.Tab) -> str:
    """Stable identity of a tab's CDP target ("" if it has none yet)."""
    return str(tab.target.target_id) if tab.target else ""


def _feature_enabled(override: bool | None, env_name: str) -> bool:
    """Whether a Chrome feature (Translate / extensions) is enabled: a runtime
    override wins, else the env var, else disabled (clean-automation default)."""
    if override is not None:
        return override
    return os.environ.get(env_name, "").lower() in ("1", "true", "yes")


async def _browser_alive(b: uc.Browser) -> bool:
    """Cheap CDP probe to confirm the browser is still usable.

    Probes over a tab connection (what the tools actually use), not the
    browser-level object, so a dropped tab websocket is detected too.
    """
    try:
        import nodriver.cdp.target as cdp_target
        conn = None
        if b.tabs:
            conn = b.tabs[0]
        elif getattr(b, "main_tab", None) is not None:
            conn = b.main_tab
        elif getattr(b, "connection", None) is not None:
            conn = b.connection
        if conn is None:
            return False
        await asyncio.wait_for(conn.send(cdp_target.get_targets()), timeout=5)
        return True
    except Exception:
        return False


async def _get_browser() -> uc.Browser:
    """Start the browser on first tool call (lazy init, protected by mutex).

    Profile precedence:
      1. a persistent profile selected at runtime via use_profile()
      2. the NODRIVER_USER_DATA_DIR env var (explicit persistent dir)
      3. default: an ephemeral temp profile nodriver creates and deletes itself.
    """
    global _browser
    async with _browser_lock:
        target = _connect_target()
        attached = target is not None
        # nodriver derives .stopped from a process it spawned, so a browser we
        # attached to always reports stopped. Taking that at face value would
        # open a fresh connection on every single tool call, so liveness for an
        # attached browser comes from an actual CDP probe instead.
        have_browser = _browser is not None and (attached or not _browser.stopped)

        # Recover from a browser that was closed or crashed between calls: the
        # .stopped flag can lag, which would otherwise make every tool fail with
        # a "no close frame" websocket error until the server restarts.
        if have_browser and not await _browser_alive(_browser):
            if not attached:
                try:
                    _browser.stop()
                except Exception:
                    pass
            _browser = None
            have_browser = False

        if not have_browser:
            if attached:
                host, port = target
                try:
                    _browser = await uc.start(host=host, port=port)
                except Exception as e:
                    raise RuntimeError(
                        f"Could not attach to a browser at {host}:{port} ({e}). "
                        "Start Chrome yourself with "
                        f"--remote-debugging-port={port} and a --user-data-dir, "
                        "then try again. Chrome refuses the debugging port when an "
                        "instance is already running on that profile."
                    ) from e
                logger.info("Attached to the browser already running at %s:%s", host, port)
                await _auto_enable_network_collection(_browser.main_tab)
                return _browser

            headless = os.environ.get("NODRIVER_HEADLESS", "").lower() in ("1", "true", "yes")
            browser_path = os.environ.get("NODRIVER_BROWSER_PATH", None)
            proxy = os.environ.get("NODRIVER_PROXY", None)

            kwargs: dict[str, Any] = {"headless": headless}

            data_dir = _selected_profile_dir or os.environ.get("NODRIVER_USER_DATA_DIR")
            if data_dir:
                os.makedirs(data_dir, exist_ok=True)
                kwargs["user_data_dir"] = data_dir
                logger.info("Using persistent profile dir: %s", data_dir)
            else:
                # Omit user_data_dir -> nodriver uses a fresh temp profile it
                # auto-removes on exit. No collisions between concurrent instances.
                logger.info("Using an ephemeral temp profile (auto-cleaned)")

            if browser_path:
                kwargs["browser_executable_path"] = browser_path

            extensions_on = _feature_enabled(_enable_extensions, "NODRIVER_ENABLE_EXTENSIONS")
            # The master switch gates unpacked extensions too: manage_extensions
            # ("off") has to turn everything off, not just what the profile
            # installed. The paths stay registered so "on" brings them back.
            unpacked = (
                [p for p in _loaded_extensions if os.path.isdir(p)] if extensions_on else []
            )

            # Chrome keeps only the LAST --disable-features on the command line,
            # and nodriver always passes one of its own. Build a single merged
            # switch (ours lands last and wins) so neither side silently drops
            # the other's entries.
            #
            # Clean-automation defaults, each re-enable-able via an env var:
            #   - suppress the Google Translate popup  (NODRIVER_ENABLE_TRANSLATE=true)
            #   - block externally-installed extensions + their "action required"
            #     prompts                              (NODRIVER_ENABLE_EXTENSIONS=true)
            disabled_features = ["IsolateOrigins", "site-per-process"]
            if not _feature_enabled(_enable_translate, "NODRIVER_ENABLE_TRANSLATE"):
                disabled_features.append("Translate")
            if unpacked:
                # Chrome 137+ ignores --load-extension unless this kill switch
                # is itself switched off.
                disabled_features.append("DisableLoadExtensionCommandLineSwitch")

            browser_args: list[str] = [f"--disable-features={','.join(disabled_features)}"]
            if not extensions_on:
                browser_args.append("--disable-extensions")
            # Chrome treats a window another window covers as hidden: timers drop
            # to ~1/s, rAF stops, and input delivery to the renderer becomes
            # unreliable — while every tool still reports success. That is
            # guaranteed the moment two agents each own a browser, and it is why
            # Puppeteer and Playwright both pass these three by default.
            # --window-size also fixes outerWidth/outerHeight reading 0, which is
            # itself a fingerprint signal on a server whose point is not standing out.
            browser_args += [
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling",
                "--window-size=1280,900",
            ]
            browser_args.extend(_extra_browser_args)
            if proxy:
                browser_args.append(f"--proxy-server={proxy}")
                logger.info("Proxy configured: %s", proxy)
            # Start on about:blank rather than the New Tab page: the NTP fires
            # its own Google requests, which pollute list_network_requests and
            # cost a page load nobody asked for.
            browser_args.append("about:blank")
            kwargs["browser_args"] = browser_args

            if unpacked:
                # Extensions have to go through a Config object — uc.start()
                # has no keyword for them.
                cfg = uc.Config(
                    user_data_dir=kwargs.get("user_data_dir"),
                    headless=headless,
                    browser_executable_path=kwargs.get("browser_executable_path"),
                    browser_args=browser_args,
                )
                for path in unpacked:
                    cfg.add_extension(path)
                _browser = await uc.start(config=cfg)
                logger.info("Loaded %d unpacked extension(s)", len(unpacked))
            else:
                _browser = await uc.start(**kwargs)

            logger.info(
                "Browser started (headless=%s, profile=%s)",
                headless, _selected_profile_name or "temp",
            )

            # Record ownership of a throwaway profile, so that if this process
            # is killed before it can clean up, the next start knows the
            # directory is ours and that nobody is using it any more.
            config = getattr(_browser, "config", None)
            if config is not None and not getattr(config, "uses_custom_data_dir", True):
                _claim_temp_profile(str(config.user_data_dir))

            # Auto-enable network collection on the first tab.
            # Console collection is opt-in because Runtime.enable() can be detected.
            await _auto_enable_network_collection(_browser.main_tab)
    return _browser


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "nodriver-mcp",
    instructions=inspect.cleandoc(
        """
        Undetected Chrome browser automation via nodriver — a drop-in replacement for
        chrome-devtools-mcp that does not expose CDP/WebDriver fingerprints, so it keeps
        working on sites behind Cloudflare, DataDome and similar anti-bot systems.

        Reading a page
          * take_snapshot is the default way to see a page. It returns the accessibility
            tree as compact text and assigns every element a `uid`. Those uids are what
            click / fill / hover / drag / upload_file take.
          * Use take_screenshot ONLY when you need pixels (layout checks, visual
            regression). It cannot be searched and costs far more than a snapshot.
          * For bulk text or scraping, get_page_content (innerText/HTML) and
            query_selector (CSS) are cheaper than a snapshot and need no uids.

        The uid lifecycle
          uids come from the most recent take_snapshot and are invalidated whenever the
          page changes. "unknown uid" always means: take a fresh snapshot, then retry.

        Typical flow
          new_page(url) -> take_snapshot() -> fill(uid, ...) -> click(uid) ->
          wait_for(["expected text"]) -> take_snapshot()

        Waiting
          Prefer wait_for (text) or wait_for_selector (CSS) over blind retries; both poll
          until a timeout instead of failing immediately.

        Staying logged in
          The browser starts on a throwaway profile that is deleted on exit. To keep a
          login, create_profile(name) + use_profile(name), or save_session/load_session.

        Note: Chrome is launched lazily on the very first tool call, so that one call can
        take a few seconds longer than the rest. That is expected.
        """
    ),
)


def tool(
    *,
    title: str,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
):
    """Register an MCP tool, with the metadata clients actually consume.

    Two things a bare ``@mcp.tool()`` leaves on the table:

    * the docstring ships verbatim, so every description carries this file's
      indentation into every request. ``inspect.cleandoc`` strips it.
    * no behaviour hints. Clients use them to group permissions and to
      auto-approve read-only tools instead of prompting for each snapshot.

    ``title`` is set both on the tool and in its annotations, since clients are
    split on which one they read.
    """

    def decorator(fn):
        return mcp.tool(
            title=title,
            description=inspect.cleandoc(fn.__doc__ or ""),
            annotations=ToolAnnotations(
                title=title,
                readOnlyHint=read_only,
                destructiveHint=destructive,
                idempotentHint=idempotent,
                openWorldHint=open_world,
            ),
        )(fn)

    return decorator


# ---------------------------------------------------------------------------
# Reusable parameter types
# ---------------------------------------------------------------------------
# Declared once so a concept reads identically on every tool that takes it. In
# particular the uid contract (snapshot -> uid -> act, stale uids need a new
# snapshot) is restated on every tool that consumes one, because that is the
# single thing models most often get wrong.

Uid = Annotated[
    str,
    Field(description=(
        'Element uid from the most recent take_snapshot, e.g. "4_12". uids are '
        'invalidated whenever the page changes — if you get "unknown uid", take a '
        "fresh snapshot and retry with the new uid."
    )),
]

IncludeSnapshot = Annotated[
    bool,
    Field(description=(
        "Append a fresh page snapshot to the response. Worth it when this action "
        "changes the page and take_snapshot would be your next call anyway — it "
        "saves a round trip, at the cost of a much larger response."
    )),
]

DevicePreset = Annotated[
    str,
    Field(description=(
        "Device to emulate — sets user agent, UA client hints, viewport, device "
        'pixel ratio and touch together. Presets: "pixel_7" (aliases: pixel7, '
        'android, android_phone), "pixel_7_landscape" (pixel7_landscape, '
        'android_landscape), "ipad_air" (ipadair, ipad, tablet). Case, spaces and '
        "-/_ are normalised. Empty string leaves emulation unchanged."
    )),
]

ColorScheme = Annotated[
    Literal["", "dark", "light", "auto"],
    Field(description=(
        'Emulate the prefers-color-scheme media feature. "auto" clears a previous '
        "override; empty string leaves it unchanged."
    )),
]

NetworkConditions = Annotated[
    Literal["", "Offline", "Slow 3G", "Fast 3G", "Slow 4G", "Fast 4G"],
    Field(description=(
        'Throttle the network to a preset profile. "Offline" cuts the connection '
        "entirely. Empty string leaves throttling unchanged."
    )),
]

CpuThrottlingRate = Annotated[
    float,
    Field(ge=0, description=(
        "Slow the CPU by this factor to emulate a low-end device (4 = 4x slower; "
        "1-20 is the useful range). 0 or 1 means no throttling."
    )),
]

Geolocation = Annotated[
    str | None,
    Field(description=(
        'Override geolocation, as "latitude,longitude" (e.g. "37.7749,-122.4194"). '
        "Omit to leave unchanged; pass an empty string to clear a previous override."
    )),
]

TimeoutMs = Annotated[
    int,
    Field(ge=0, description="Maximum wait in milliseconds. 0 uses the built-in default."),
]


async def _active_tab() -> uc.Tab:
    """Return the tab selected via select_page(), else the last-opened tab."""
    browser = await _get_browser()
    if _selected_target_id is not None:
        for t in browser.tabs:
            if t.target and str(t.target.target_id) == _selected_target_id:
                return t
        # Selected tab is gone (closed/crashed); fall through to the default.
    if browser.tabs:
        return browser.tabs[-1]
    return browser.main_tab


# ---------------------------------------------------------------------------
# Shared state for console / network collection
# ---------------------------------------------------------------------------
_console_messages: list[dict] = []
_network_requests: list[dict] = []
_preserved_console_messages: list[list[dict]] = []  # last 3 navigations
_preserved_network_requests: list[list[dict]] = []  # last 3 navigations
_tracing_active = False
_network_collection_enabled_tabs: set[tuple] = set()  # (target_id, session_id) with Network enabled
_network_handler_targets: set[int] = set()  # id(tab) of tabs whose request handler is installed
_console_collection_enabled_tabs: set[int] = set()  # track which tabs have console collection enabled
_console_handlers: dict[int, tuple] = {}  # tab id -> the handlers we registered, so we can remove them
_named_browser_contexts: dict[str, Any] = {}  # isolated_context name -> BrowserContextID
_selected_target_id: str | None = None  # target_id chosen via select_page(); honored by _active_tab()
_request_counter: int = 0  # monotonic id assigned to each collected network request
_console_counter: int = 0  # monotonic id assigned to each collected console message

_DEVICE_PRESETS: dict[str, dict[str, Any]] = {
    "pixel_7": {
        "aliases": ["pixel7", "android", "android_phone"],
        "viewport": "412x915x2.625,mobile,touch",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36"
        ),
        "platform": "Android",
        "accept_language": "en-US,en",
        "metadata": {
            "platform": "Android",
            "platform_version": "14",
            "architecture": "arm",
            "model": "Pixel 7",
            "mobile": True,
            "form_factors": ["Mobile"],
        },
    },
    "pixel_7_landscape": {
        "aliases": ["pixel7_landscape", "android_landscape"],
        "viewport": "915x412x2.625,mobile,touch,landscape",
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36"
        ),
        "platform": "Android",
        "accept_language": "en-US,en",
        "metadata": {
            "platform": "Android",
            "platform_version": "14",
            "architecture": "arm",
            "model": "Pixel 7",
            "mobile": True,
            "form_factors": ["Mobile"],
        },
    },
    "ipad_air": {
        "aliases": ["ipadair", "tablet", "ipad"],
        # iPad reports as desktop-class Safari with touch (not a 'mobile' UA-CH).
        "viewport": "820x1180x2,touch",
        "user_agent": (
            "Mozilla/5.0 (iPad; CPU OS 17_6 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1"
        ),
        "platform": "iOS",
        "accept_language": "en-US,en",
        # Safari does not send Sec-CH-UA client hints; omit UA-CH metadata.
        "metadata": None,
    },
}

# Stable uid state for take_snapshot (mirrors chrome-devtools-mcp)
_snapshot_id: int = 0
_unique_id_to_mcp_id: dict[str, str] = {}  # "frameId_backendNodeId" -> stable uid
_uid_to_backend_node_id: dict[str, int] = {}  # mcp uid -> backend_dom_node_id (for element resolution)

# Boolean property mapping (same as chrome-devtools-mcp SnapshotFormatter)
_BOOL_PROPERTY_MAP: dict[str, str] = {
    "disabled": "disableable",
    "expanded": "expandable",
    "focused": "focusable",
    "selected": "selectable",
}

# Properties already rendered in the main line or internal-only
_EXCLUDED_PROPERTIES: set[str] = {"role", "name", "children", "elementHandle"}

# Properties Puppeteer doesn't expose — suppress to match chrome-devtools-mcp output
_SUPPRESS_PROPERTIES: set[str] = {
    "focusable", "editable", "settable", "busy", "live", "relevant", "atomic",
    "hidden", "controls", "describedby", "details", "errormessage", "flowto",
    "labelledby", "owns", "activedescendant",
}

# Roles to skip entirely (node AND all descendants) — Chrome internals
_SKIP_ROLES: set[str] = {"InlineTextBox", "ListMarker"}

# Roles to collapse (skip node, promote children) — container noise.
# The Layout* and table roles are pure scaffolding on sites that lay out with
# tables: they carry no name and cost a line each, which on a link-dense page is
# a sizeable share of the snapshot.
_COLLAPSE_ROLES: set[str] = {
    "generic", "list", "listitem", "paragraph", "strong", "emphasis", "code",
    "group", "Section", "blockquote", "figure", "mark", "subscript",
    "superscript", "insertion", "deletion", "DescriptionList",
    "DescriptionListTerm", "DescriptionListDetail", "time", "Abbr", "Ruby",
    "RubyAnnotation", "term", "definition", "feed", "log", "marquee",
    "timer", "directory", "tooltip",
    "LayoutTable", "LayoutTableRow", "LayoutTableCell", "LineBreak",
}
# Deliberately NOT collapsed: row, cell, columnheader, rowheader. Chrome already
# distinguishes presentational tables (the Layout* roles above) from data ones,
# and flattening a real table would leave values with no row to belong to.

# Modifier keys for press_key combo support
_MODIFIER_KEYS = {"Control", "Shift", "Alt", "Meta"}


def _preserve_on_navigation() -> None:
    """Rotate current console/network messages into preserved history (last 3 navigations)."""
    if _console_messages:
        _preserved_console_messages.append(list(_console_messages))
        if len(_preserved_console_messages) > 3:
            _preserved_console_messages.pop(0)
        _console_messages.clear()
    if _network_requests:
        _preserved_network_requests.append(list(_network_requests))
        if len(_preserved_network_requests) > 3:
            _preserved_network_requests.pop(0)
        _network_requests.clear()


# ---------------------------------------------------------------------------
# Auto-collection for network (console collection is explicit)
# ---------------------------------------------------------------------------
async def _auto_enable_network_collection(tab: uc.Tab) -> None:
    """Enable network collection on this tab's current CDP session.

    Keyed by (target, session) rather than by the Tab object. Network.enable is a
    per-session command, and id(tab) does not change when the session under it
    does — so the guard used to skip the one call that mattered, leaving the
    domain enabled only on a session nobody was talking to any more. Measured
    symptom: get_network_request could not retrieve a single response body, and
    block_resources reported success while every image still loaded.
    """
    import nodriver.cdp.network as cdp_net

    session_id = getattr(tab, "session_id", None)
    if not session_id:
        # No session yet: tab.send would go to the browser-level connection and
        # enable Network there instead. That scope stays enabled, so once the page
        # session is enabled too, Chrome reports every request twice and
        # nodriver's dispatcher — which ignores sessionId — delivers both. Skip;
        # the next call, after the attach, does it on the right session.
        return
    session_key = (getattr(tab, "target_id", None), session_id)
    if session_key in _network_collection_enabled_tabs:
        return
    _network_collection_enabled_tabs.add(session_key)
    # The handler belongs to the Tab object, not the session: adding it again
    # after a session change records every later request twice. id(tab) is the
    # right key here for the same reason it was the wrong one for the enable —
    # it tracks the object the handler is attached to, not the session state.
    needs_handler = id(tab) not in _network_handler_targets

    async def _on_request(event: cdp_net.RequestWillBeSent):
        global _request_counter
        try:
            # ResourceType is an enum whose str() is "ResourceType.XHR", not
            # "XHR" — storing that made the resource_types filter in
            # list_network_requests unmatchable. Unwrap to the CDP value.
            resource_type = getattr(event.type_, "value", event.type_) or "unknown"
            _network_requests.append({
                "seq": _request_counter,
                "id": str(event.request_id),
                "url": event.request.url,
                "method": event.request.method,
                "timestamp": str(event.timestamp),
                "type": str(resource_type),
            })
            _request_counter += 1
            if len(_network_requests) > 1000:
                _network_requests.pop(0)
        except Exception:
            pass

    try:
        # The buffer sizes are the whole reason get_network_request could not
        # return a response body: with them omitted Chrome keeps no resource
        # bodies, so Network.getResponseBody answered "No resource with given
        # identifier found" for every request, including one issued a second
        # earlier. DevTools itself passes these. Measured: without them 0 of 3
        # bodies were retrievable, with them the same JSON came back in full.
        await tab.send(
            cdp_net.enable(
                max_total_buffer_size=50_000_000,
                max_resource_buffer_size=10_000_000,
            )
        )
        if needs_handler:
            tab.add_handler(cdp_net.RequestWillBeSent, _on_request)
            _network_handler_targets.add(id(tab))
    except Exception:
        _network_collection_enabled_tabs.discard(session_key)


async def _enable_console_collection(tab: uc.Tab) -> bool:
    """Enable console event collection on a tab."""
    tab_id = id(tab)
    if tab_id in _console_collection_enabled_tabs:
        return False

    import nodriver.cdp.runtime as cdp_runtime

    async def _on_console(event):
        global _console_counter
        try:
            parts = []
            args = getattr(event, "args", None) or []
            for a in args:
                try:
                    if isinstance(a, str):
                        parts.append(a)
                    else:
                        parts.append(str(getattr(a, "value", None) or getattr(a, "description", None) or a))
                except Exception:
                    parts.append(str(a))
            msg = {
                "seq": _console_counter,
                "type": str(getattr(event, "type_", "log")),
                "text": " ".join(parts),
                "timestamp": str(getattr(event, "timestamp", "")),
            }
            _console_messages.append(msg)
            _console_counter += 1
            if len(_console_messages) > 1000:
                _console_messages.pop(0)
        except Exception:
            pass

    async def _on_exception(event):
        """Capture uncaught errors and unhandled promise rejections.

        Runtime.consoleAPICalled only fires for explicit console.* calls, so
        without this an uncaught TypeError — the thing you actually want when a
        page is broken — was reported as "0 messages".
        """
        global _console_counter
        try:
            det = getattr(event, "exception_details", None)
            exc = getattr(det, "exception", None)
            text = (
                getattr(exc, "description", None)
                or getattr(exc, "value", None)
                or getattr(det, "text", None)
                or "Uncaught exception"
            )
            url = getattr(det, "url", "") or ""
            line = getattr(det, "line_number", None)
            where = f" ({url}:{line})" if url else ""
            _console_messages.append({
                "seq": _console_counter,
                "type": "error",
                "text": f"{text}{where}",
                "timestamp": str(getattr(event, "timestamp", "")),
            })
            _console_counter += 1
            if len(_console_messages) > 1000:
                _console_messages.pop(0)
        except Exception:
            pass

    try:
        await tab.send(cdp_runtime.enable())
        tab.add_handler(cdp_runtime.ConsoleAPICalled, _on_console)
        tab.add_handler(cdp_runtime.ExceptionThrown, _on_exception)
        _console_handlers[tab_id] = (_on_console, _on_exception)
        _console_collection_enabled_tabs.add(tab_id)
        return True
    except Exception:
        return False


async def _disable_console_collection(tab: uc.Tab) -> bool:
    """Disable console event collection on a tab."""
    tab_id = id(tab)
    if tab_id not in _console_collection_enabled_tabs:
        return False

    import nodriver.cdp.runtime as cdp_runtime

    # remove_handler(event_type, callback) — callback is required. Passing only
    # the type raised TypeError into a bare except, so the handler was never
    # removed and every re-enable added another one, duplicating every message.
    handlers = _console_handlers.pop(tab_id, ())
    for event_type, cb in zip(
        (cdp_runtime.ConsoleAPICalled, cdp_runtime.ExceptionThrown), handlers
    ):
        try:
            tab.remove_handler(event_type, cb)
        except Exception:
            pass

    try:
        await tab.send(cdp_runtime.disable())
    except Exception:
        pass

    _console_collection_enabled_tabs.discard(tab_id)
    return True


# ---------------------------------------------------------------------------
# UID resolution: uid -> DOM element operations
# ---------------------------------------------------------------------------
async def _resolve_uid(tab: uc.Tab, uid: str) -> Any:
    """Resolve a snapshot uid to a CDP remote object for element manipulation.

    Returns the remote_object_id that can be used with CDP commands.
    """
    import nodriver.cdp.dom as cdp_dom

    backend_node_id = _uid_to_backend_node_id.get(uid)
    if backend_node_id is None:
        raise ValueError(f"Unknown uid '{uid}'. Take a new snapshot first.")

    result = await tab.send(cdp_dom.resolve_node(
        backend_node_id=cdp_dom.BackendNodeId(backend_node_id)
    ))
    if result is None:
        raise ValueError(f"Could not resolve uid '{uid}' to a DOM node.")
    return result


async def _get_box_model(tab: uc.Tab, uid: str) -> tuple[float, float]:
    """Get the center coordinates of an element by uid."""
    import nodriver.cdp.dom as cdp_dom

    backend_node_id = _uid_to_backend_node_id.get(uid)
    if backend_node_id is None:
        raise ValueError(f"Unknown uid '{uid}'. Take a new snapshot first.")

    model = await tab.send(cdp_dom.get_box_model(
        backend_node_id=cdp_dom.BackendNodeId(backend_node_id)
    ))
    # content quad: [x1,y1, x2,y2, x3,y3, x4,y4]
    quad = model.content
    cx = (quad[0] + quad[2] + quad[4] + quad[6]) / 4
    cy = (quad[1] + quad[3] + quad[5] + quad[7]) / 4
    return cx, cy


async def _maybe_snapshot(include_snapshot: bool) -> str:
    """Optionally append a snapshot to the response."""
    if include_snapshot:
        snapshot = await take_snapshot()
        return "\n\n" + snapshot
    return ""


def _format_exception_details(exc: Any) -> str:
    """Human-readable message from a CDP Runtime.ExceptionDetails object."""
    try:
        obj = getattr(exc, "exception", None)
        if obj is not None:
            desc = getattr(obj, "description", None) or getattr(obj, "value", None)
            if desc:
                return str(desc)
        text = getattr(exc, "text", None)
        if text:
            return str(text)
    except Exception:
        pass
    return str(exc)


async def _call_function_on(tab: uc.Tab, **kwargs: Any) -> Any:
    """Send Runtime.callFunctionOn and normalise nodriver's return.

    nodriver returns a ``(RemoteObject, ExceptionDetails | None)`` tuple from
    call_function_on. Unpack it, raise on a JS exception, and return the
    RemoteObject so callers can read ``.value`` directly.
    """
    import nodriver.cdp.runtime as cdp_runtime

    result = await tab.send(cdp_runtime.call_function_on(**kwargs))
    remote, exc = result if isinstance(result, tuple) else (result, None)
    if exc is not None:
        raise RuntimeError(_format_exception_details(exc))
    return remote


_SELECT_CONTENTS = (
    "function() {"
    " this.scrollIntoView({block: 'center', inline: 'center'});"
    " this.focus();"
    " if (this.isContentEditable) {"
    "   const r = document.createRange(); r.selectNodeContents(this);"
    "   const s = getSelection(); s.removeAllRanges(); s.addRange(r);"
    " } else if (this.setSelectionRange) {"
    "   try { this.setSelectionRange(0, (this.value || '').length); } catch (e) {}"
    " }"
    " return true; }"
)


async def _read_field(tab: uc.Tab, remote_obj: Any) -> str:
    """Current text of an input, textarea, select or contenteditable."""
    obj = await _call_function_on(
        tab,
        function_declaration=(
            "function() { return this.isContentEditable ? this.innerText : (this.value ?? ''); }"
        ),
        object_id=remote_obj.object_id,
        return_by_value=True,
    )
    if obj is None or obj.value is None:
        return ""
    return str(obj.value)


async def _fill_element(tab: uc.Tab, uid: str, value: str) -> None:
    """Fill one input, textarea, select or contenteditable by uid.

    Clearing a field before typing (``value = ''`` plus an input event) makes a
    framework-controlled input re-render, and the focus goes with the node that
    was replaced. Every keystroke after that lands nowhere, leaving the field
    empty while the call still looks like it worked. So the existing content is
    selected and typed over instead, which is also what a person does.

    The result is read back and compared. If the value did not actually land,
    this raises rather than reporting a success the caller cannot verify.
    """
    import nodriver.cdp.runtime as cdp_runtime
    import nodriver.cdp.input_ as cdp_input

    remote_obj = await _resolve_uid(tab, uid)

    # A uid can land on a text node rather than an element. Everything below
    # needs an element, and without this the first property access fails with a
    # TypeError that says nothing about the real problem.
    element = await _call_function_on(
        tab,
        function_declaration="function() { return this.nodeType === 3 ? this.parentElement : this; }",
        object_id=remote_obj.object_id,
    )
    if element is not None and getattr(element, "object_id", None):
        remote_obj = element

    tag_obj = await _call_function_on(
        tab,
        function_declaration=(
            "function() { if (!this || !this.tagName) return ''; "
            "return this.isContentEditable ? 'contenteditable' : this.tagName.toLowerCase(); }"
        ),
        object_id=remote_obj.object_id,
        return_by_value=True,
    )
    tag = tag_obj.value if tag_obj else ""
    if not tag:
        raise RuntimeError(
            f"uid={uid} is not an element that can be filled. Take a fresh "
            "take_snapshot and use the uid of the input, textarea, select or "
            "editable element itself."
        )

    if tag == "select":
        await _call_function_on(
            tab,
            function_declaration=(
                "function(val) { this.value = val; "
                "this.dispatchEvent(new Event('change', {bubbles: true})); }"
            ),
            object_id=remote_obj.object_id,
            arguments=[cdp_runtime.CallArgument(value=value)],
            return_by_value=True,
        )
        await tab
        if await _read_field(tab, remote_obj) != value:
            raise RuntimeError(
                f"no option with value {value!r} in this <select>. "
                "Use the option's value attribute, not its visible label."
            )
        return

    await _call_function_on(
        tab,
        function_declaration=_SELECT_CONTENTS,
        object_id=remote_obj.object_id,
        return_by_value=True,
    )
    for char in value:
        await tab.send(cdp_input.dispatch_key_event(type_="keyDown", text=char))
        await tab.send(cdp_input.dispatch_key_event(type_="keyUp", text=char))
    await tab

    def _matches(got: str) -> bool:
        return got.strip() == value.strip() if tag == "contenteditable" else got == value

    if _matches(await _read_field(tab, remote_obj)):
        return

    # Synthesised key events were dropped. Insert the text as a single edit,
    # which rich-text and some framework-managed fields accept instead.
    await _call_function_on(
        tab,
        function_declaration=_SELECT_CONTENTS,
        object_id=remote_obj.object_id,
        return_by_value=True,
    )
    await tab.send(cdp_input.insert_text(value))
    await tab

    got = await _read_field(tab, remote_obj)
    if not _matches(got):
        raise RuntimeError(
            f"the field did not accept the value; it now holds {got!r}. "
            "It may be read-only or disabled, covered by an overlay, or it "
            "rewrites what is typed (a masked or formatted input)."
        )


async def _double_click(tab: uc.Tab, x: float, y: float) -> None:
    """Dispatch a real double-click (click_count 1 then 2) so a dblclick fires.

    Calling mouse_click twice produces two independent click_count=1 clicks and
    never triggers a dblclick event; the click_count must escalate to 2.
    """
    import nodriver.cdp.input_ as cdp_input

    btn = cdp_input.MouseButton("left")
    for count in (1, 2):
        await tab.send(cdp_input.dispatch_mouse_event(
            "mousePressed", x=x, y=y, modifiers=0, button=btn, buttons=1, click_count=count))
        await tab.send(cdp_input.dispatch_mouse_event(
            "mouseReleased", x=x, y=y, modifiers=0, button=btn, buttons=1, click_count=count))


def _all_network_requests() -> list[dict]:
    """All collected network requests: preserved history followed by current."""
    pool: list[dict] = []
    for batch in _preserved_network_requests:
        pool.extend(batch)
    pool.extend(_network_requests)
    return pool


def _all_console_messages() -> list[dict]:
    """All collected console messages: preserved history followed by current."""
    pool: list[dict] = []
    for batch in _preserved_console_messages:
        pool.extend(batch)
    pool.extend(_console_messages)
    return pool


# Virtual-key codes for modifier keys and a table of common named keys, so
# dispatched key events carry code / windowsVirtualKeyCode / text and behave
# like real key presses (named keys and shortcuts both work).
_MODIFIER_VK: dict[str, int] = {"Control": 17, "Shift": 16, "Alt": 18, "Meta": 91}
_MODIFIER_BITS: dict[str, int] = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

_NAMED_KEYS: dict[str, dict[str, Any]] = {
    "Enter": {"key": "Enter", "code": "Enter", "vk": 13, "text": "\r"},
    "Tab": {"key": "Tab", "code": "Tab", "vk": 9},
    "Backspace": {"key": "Backspace", "code": "Backspace", "vk": 8},
    "Delete": {"key": "Delete", "code": "Delete", "vk": 46},
    "Escape": {"key": "Escape", "code": "Escape", "vk": 27},
    "ArrowUp": {"key": "ArrowUp", "code": "ArrowUp", "vk": 38},
    "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "vk": 40},
    "ArrowLeft": {"key": "ArrowLeft", "code": "ArrowLeft", "vk": 37},
    "ArrowRight": {"key": "ArrowRight", "code": "ArrowRight", "vk": 39},
    "Home": {"key": "Home", "code": "Home", "vk": 36},
    "End": {"key": "End", "code": "End", "vk": 35},
    "PageUp": {"key": "PageUp", "code": "PageUp", "vk": 33},
    "PageDown": {"key": "PageDown", "code": "PageDown", "vk": 34},
    "Space": {"key": " ", "code": "Space", "vk": 32, "text": " "},
}


def _key_descriptor(name: str) -> dict[str, Any]:
    """Resolve a key name to CDP dispatch fields (key/code/vk/text)."""
    if name in _NAMED_KEYS:
        return dict(_NAMED_KEYS[name])
    if len(name) == 1:
        ch = name
        code = None
        vk = 0
        if ch.isalpha():
            code = f"Key{ch.upper()}"
            vk = ord(ch.upper())
        elif ch.isdigit():
            code = f"Digit{ch}"
            vk = ord(ch)
        return {"key": ch, "code": code, "vk": vk, "text": ch}
    # Unknown multi-char key name — pass through as the key value.
    return {"key": name, "code": None, "vk": 0}


def _timeout_seconds(timeout_ms: int) -> float | None:
    """Convert MCP timeout milliseconds to asyncio seconds."""
    return timeout_ms / 1000 if timeout_ms and timeout_ms > 0 else None


async def _await_with_timeout(awaitable: Any, timeout_ms: int, action: str) -> Any:
    """Await an operation with an optional MCP-style timeout."""
    timeout_s = _timeout_seconds(timeout_ms)
    try:
        if timeout_s is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"{action} timed out after {timeout_ms}ms") from exc


async def _wait_for_target(browser: uc.Browser, target_id: Any, timeout_ms: int) -> uc.Tab:
    """Wait for a newly created target to appear in nodriver's tab inventory."""
    timeout_s = _timeout_seconds(timeout_ms) or 10.0
    loop = asyncio.get_running_loop()
    start = loop.time()

    while loop.time() - start < timeout_s:
        for tab in browser.tabs:
            if tab.target_id == target_id:
                tab._browser = browser
                return tab
        # A target created through CDP directly is not in nodriver's inventory
        # until something refreshes it; without this the loop can only time out.
        try:
            await browser.update_targets()
        except Exception:
            pass
        await asyncio.sleep(0.1)

    raise TimeoutError(f"New page did not appear within {int(timeout_s * 1000)}ms")


def _resolve_device_preset(device: str) -> dict[str, Any] | None:
    """Resolve a device preset by canonical name or alias."""
    normalized = device.strip().lower().replace("-", "_").replace(" ", "_")
    for name, preset in _DEVICE_PRESETS.items():
        aliases = {
            alias.strip().lower().replace("-", "_").replace(" ", "_")
            for alias in {name, *preset.get("aliases", [])}
        }
        if normalized in aliases:
            return {"name": name, **preset}
    return None


def _build_user_agent_metadata(metadata: dict[str, Any]) -> Any:
    """Build CDP user-agent metadata from a plain dict."""
    import nodriver.cdp.emulation as cdp_emu

    return cdp_emu.UserAgentMetadata(
        platform=metadata["platform"],
        platform_version=metadata["platform_version"],
        architecture=metadata["architecture"],
        model=metadata["model"],
        mobile=metadata["mobile"],
        form_factors=metadata.get("form_factors"),
    )


async def _apply_emulation(
    tab: uc.Tab,
    *,
    network_conditions: str = "",
    cpu_throttling_rate: float = 0,
    geolocation: str | None = None,
    user_agent: str | None = None,
    user_agent_platform: str = "",
    user_agent_metadata: Any = None,
    accept_language: str = "",
    color_scheme: str = "",
    viewport: str = "",
) -> list[str]:
    """Apply emulation settings and return a human-readable summary."""
    results = []

    if network_conditions:
        import nodriver.cdp.network as cdp_net

        presets = {
            "offline": {"offline": True, "latency": 0, "download": 0, "upload": 0},
            "slow 3g": {"offline": False, "latency": 2000, "download": 50000, "upload": 50000},
            "fast 3g": {"offline": False, "latency": 563, "download": 180000, "upload": 84375},
            "slow 4g": {"offline": False, "latency": 150, "download": 400000, "upload": 150000},
            "fast 4g": {"offline": False, "latency": 50, "download": 1500000, "upload": 750000},
        }
        p = presets.get(network_conditions.lower(), presets.get("fast 3g"))
        await tab.send(cdp_net.emulate_network_conditions(
            offline=p["offline"],
            latency=p["latency"],
            download_throughput=p["download"],
            upload_throughput=p["upload"],
        ))
        results.append(f"network={network_conditions}")

    if cpu_throttling_rate and cpu_throttling_rate > 1:
        import nodriver.cdp.emulation as cdp_emu

        await tab.send(cdp_emu.set_cpu_throttling_rate(rate=cpu_throttling_rate))
        results.append(f"cpu_throttle={cpu_throttling_rate}x")

    if geolocation is not None:
        import nodriver.cdp.emulation as cdp_emu

        if geolocation:
            try:
                parts = geolocation.split(",")
                lat, lng = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                raise ValueError(
                    f"Invalid geolocation '{geolocation}'. Expected 'latitude,longitude'."
                )
            await tab.send(cdp_emu.set_geolocation_override(latitude=lat, longitude=lng, accuracy=1.0))
            results.append(f"geolocation={lat},{lng}")
        else:
            await tab.send(cdp_emu.clear_geolocation_override())
            results.append("geolocation=reset")

    if user_agent is not None:
        import nodriver.cdp.network as cdp_net

        if user_agent:
            kwargs: dict[str, Any] = {"user_agent": user_agent}
            if accept_language:
                kwargs["accept_language"] = accept_language
            if user_agent_platform:
                kwargs["platform"] = user_agent_platform
            if user_agent_metadata is not None:
                kwargs["user_agent_metadata"] = user_agent_metadata
            await tab.send(cdp_net.set_user_agent_override(**kwargs))
            results.append("user_agent set")
            if user_agent_metadata is not None:
                results.append("ua_client_hints set")
        else:
            await tab.send(cdp_net.set_user_agent_override(user_agent=""))
            results.append("user_agent reset")

    if color_scheme and color_scheme != "auto":
        import nodriver.cdp.emulation as cdp_emu

        await tab.send(cdp_emu.set_emulated_media(
            features=[cdp_emu.MediaFeature(name="prefers-color-scheme", value=color_scheme)]
        ))
        results.append(f"color_scheme={color_scheme}")
    elif color_scheme == "auto":
        import nodriver.cdp.emulation as cdp_emu

        await tab.send(cdp_emu.set_emulated_media(features=[]))
        results.append("color_scheme=auto (reset)")

    if viewport:
        import nodriver.cdp.emulation as cdp_emu

        try:
            parts = viewport.split(",")
            dims = parts[0].split("x")
            w, h = int(dims[0]), int(dims[1])
            dpr = float(dims[2]) if len(dims) > 2 else 1.0
        except (ValueError, IndexError):
            raise ValueError(
                f"Invalid viewport '{viewport}'. Expected "
                "'widthxheightxdpr[,mobile][,touch][,landscape]'."
            )
        flags = {f.strip().lower() for f in parts[1:] if f.strip()}
        mobile = "mobile" in flags
        touch = "touch" in flags
        landscape = "landscape" in flags
        orientation = cdp_emu.ScreenOrientation(
            type_="landscapePrimary" if landscape else "portraitPrimary",
            angle=90 if landscape else 0,
        )
        await tab.send(cdp_emu.set_device_metrics_override(
            width=w,
            height=h,
            device_scale_factor=dpr,
            mobile=mobile,
            screen_width=w,
            screen_height=h,
            screen_orientation=orientation,
        ))
        await tab.send(cdp_emu.set_touch_emulation_enabled(
            enabled=touch,
            max_touch_points=5 if touch else None,
        ))
        await tab.send(cdp_emu.set_emit_touch_events_for_mouse(
            enabled=touch,
            configuration="mobile" if mobile else "desktop",
        ))
        key = _target_key(tab)
        if key:
            if touch:
                _touch_emulated_targets.add(key)
            else:
                _touch_emulated_targets.discard(key)
        results.append(f"viewport={viewport}")
        results.append(f"touch={'on' if touch else 'off'}")

    return results


async def _apply_device_preset(tab: uc.Tab, device: str) -> list[str]:
    """Apply a named device preset to a tab."""
    resolved = _resolve_device_preset(device)
    if resolved is None:
        supported = ", ".join(sorted(_DEVICE_PRESETS))
        raise ValueError(f"Unknown device preset '{device}'. Supported presets: {supported}")

    metadata = _build_user_agent_metadata(resolved["metadata"]) if resolved.get("metadata") else None
    results = await _apply_emulation(
        tab,
        user_agent=resolved["user_agent"],
        user_agent_platform=resolved["platform"],
        user_agent_metadata=metadata,
        accept_language=resolved.get("accept_language", ""),
        viewport=resolved["viewport"],
    )
    results.insert(0, f"device={resolved['name']}")
    return results


async def _reset_emulation(tab: uc.Tab) -> list[str]:
    """Reset emulation overrides on the current tab back to browser defaults."""
    import nodriver.cdp.emulation as cdp_emu
    import nodriver.cdp.network as cdp_net

    results = []

    await tab.send(
        cdp_net.emulate_network_conditions(
            offline=False,
            latency=0,
            download_throughput=-1,
            upload_throughput=-1,
        )
    )
    results.append("network=reset")

    await tab.send(cdp_emu.set_cpu_throttling_rate(rate=1))
    results.append("cpu_throttle=reset")

    await tab.send(cdp_emu.clear_geolocation_override())
    results.append("geolocation=reset")

    await tab.send(cdp_net.set_user_agent_override(user_agent=""))
    results.append("user_agent=reset")

    await tab.send(cdp_emu.set_emulated_media(features=[]))
    results.append("color_scheme=reset")

    await tab.send(cdp_emu.clear_device_metrics_override())
    results.append("viewport=reset")

    await tab.send(cdp_emu.reset_page_scale_factor())
    results.append("page_scale=reset")

    await tab.send(cdp_emu.set_touch_emulation_enabled(enabled=False))
    await tab.send(cdp_emu.set_emit_touch_events_for_mouse(enabled=False))
    _touch_emulated_targets.discard(_target_key(tab))
    results.append("touch=reset")

    return results


async def _open_new_tab(
    browser: uc.Browser,
    *,
    url: str,
    background: bool,
    isolated_context: str,
    timeout: int,
) -> uc.Tab:
    """Open a new tab, optionally inside a named isolated browser context."""
    if isolated_context:
        import nodriver.cdp.target as cdp_target

        ctx = _named_browser_contexts.get(isolated_context)
        if ctx is None:
            ctx = await _await_with_timeout(
                # Browser subclasses Connection, so it sends CDP itself.
                # browser.connection is initialised to None by nodriver and never
                # assigned, so reaching through it raises AttributeError every time.
                browser.send(cdp_target.create_browser_context(dispose_on_detach=False)),
                timeout,
                f"Create isolated context '{isolated_context}'",
            )
            _named_browser_contexts[isolated_context] = ctx

        target_id = await _await_with_timeout(
            browser.send(
                cdp_target.create_target(
                    url=url,
                    browser_context_id=ctx,
                    background=background,
                    # for_tab=True asks for a target of type "tab", which
                    # browser.tabs filters out — _wait_for_target would then
                    # never find it and always time out.
                )
            ),
            timeout,
            "Create new page target",
        )
        tab = await _wait_for_target(browser, target_id, timeout)
        await _await_with_timeout(tab, timeout, "Wait for new page")
        return tab

    tab = await _await_with_timeout(browser.get(url, new_tab=True), timeout, "Open new page")
    await _await_with_timeout(tab, timeout, "Wait for new page")
    return tab


async def _navigate_same_tab(tab: uc.Tab, target_url: str) -> str:
    """Navigate this tab over CDP, without nodriver's re-attach.

    Tab.get() delegates to Browser.get(), which navigates the *first* page target
    rather than self, and then calls connection.attach() again. That mints a brand
    new CDP session for the same target and never detaches the old one, so every
    domain this server enabled belongs to a stranded session while commands go to
    a session where nothing was ever enabled. Tracing.end answered "Tracing is not
    started", Network.getResponseBody could not find any request, and every
    reset/clear/disable call wrote into the orphan.

    Page.navigate resolves once the navigation has committed or failed, so the
    document is already the new one by the time this returns, and errorText and
    isDownload — both discarded by nodriver — become visible.

    Returns a note to append to the tool's answer, or "".
    """
    import nodriver.cdp.page as cdp_page

    frame_id, loader_id, error_text, is_download = await tab.send(
        cdp_page.navigate(url=target_url)
    )
    if is_download:
        return " (the URL was a download, not a page; the tab did not change)"
    if error_text:
        # Chrome reports an HTTP error status with no body as a navigation
        # failure, but the navigation committed and the page is readable — a 404
        # page is a page. Only a transport-level failure means there is nothing
        # there, and that is what deserves to be an error.
        if "ERR_HTTP_RESPONSE_CODE_FAILURE" in error_text:
            return (
                " (no page was loaded: the server answered with an HTTP error status"
                " and an empty body, so Chrome showed its own error page. An error"
                " status that does come with a body renders normally.)"
            )
        raise RuntimeError(f"navigation failed: {error_text}")

    # Wait for the new document to be usable, but never for its subresources: a
    # page with one hanging image is still readable, and blocking on the load
    # event would turn a two-second call into a thirty-second one.
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < 5.0:
        try:
            state = await _evaluate_value(tab, "document.readyState")
        except Exception:
            state = None
        if state in ("interactive", "complete"):
            break
        await asyncio.sleep(0.05)
    return ""


async def _refresh_targets(browser: uc.Browser) -> None:
    """Refresh CDP target info (url/title) so it isn't stale right after a nav."""
    try:
        await browser.update_targets()
    except Exception:
        pass


async def _format_pages() -> str:
    """Format the pages list for appending to navigation responses."""
    browser = await _get_browser()
    await _refresh_targets(browser)
    lines = ["\nOpen pages:"]
    for i, tab in enumerate(browser.tabs):
        url = tab.target.url or "about:blank"
        title = tab.target.title or ""
        lines.append(f"  [{i}] {url} — {title}")
    return "\n".join(lines)


def _safe_profile_name(name: str) -> str:
    """Sanitize a profile name to a safe directory name."""
    return "".join(c for c in (name or "").strip() if c.isalnum() or c in "-_")


async def _close_browser_and_profile(b: uc.Browser) -> None:
    """Shut a browser down completely, waiting for each step to finish.

    nodriver's own Browser.stop() schedules the connection close as a
    fire-and-forget task and terminates Chrome in the same breath, then leaves
    the temp profile to an atexit handler. Neither survives a process that is
    killed rather than asked to exit — which is exactly how an MCP server ends
    when its client restarts. Measured: Chrome lives on as orphaned processes
    and its profile stays behind, several hundred MB at a time.

    Doing it here means the browser is really gone by the time this returns.
    """
    # 1. Close the CDP websockets we hold, awaited rather than scheduled. Tabs
    #    first: each is its own connection, and a live one keeps Chrome busy.
    connections = [t for t in (getattr(b, "tabs", None) or [])]
    main = getattr(b, "connection", None)
    if main is not None:
        connections.append(main)
    for conn in connections:
        try:
            await asyncio.wait_for(conn.aclose(), timeout=5)
        except Exception:
            pass

    # 2. Terminate Chrome and wait for the process to actually be gone, so the
    #    profile below is not still held open when we try to remove it.
    proc = getattr(b, "_process", None)
    if proc is not None:
        for signal_it in (proc.terminate, proc.kill):
            try:
                signal_it()
                await asyncio.wait_for(proc.wait(), timeout=10)
                break
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

    # 3. Remove the throwaway profile. A persistent one the user chose is theirs
    #    to keep, and uses_custom_data_dir is how nodriver tells them apart.
    config = getattr(b, "config", None)
    if config is not None and not getattr(config, "uses_custom_data_dir", True):
        path = str(getattr(config, "user_data_dir", "") or "")
        if path:
            for attempt in range(8):
                try:
                    shutil.rmtree(path)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    # Chrome releases its files asynchronously on Windows; the
                    # same race delete_profile had. nodriver gives up after
                    # 0.75s, which is where the leftovers come from.
                    await asyncio.sleep(0.25 * (attempt + 1))
            else:
                logger.warning("could not remove temp profile %s", path)
            # Release the claim only once the directory is really gone. Dropping
            # it while the directory is still stuck would hide it from the sweep
            # that exists to catch exactly that case.
            if not os.path.exists(path):
                _release_temp_profile(path)

    # 4. Drop it from nodriver's registry so its atexit handler does not repeat
    #    all of the above against a browser that is already gone.
    try:
        from nodriver.core.util import __registered__instances__

        __registered__instances__.discard(b)
    except Exception:
        pass


async def _stop_browser() -> bool:
    """Stop the running browser (if any) and reset per-browser state, keeping the
    selected profile. Returns True if a browser was actually running.

    A browser we attached to is only detached from, never stopped: it belongs to
    the user, it holds their session, and closing it because a tool wanted a
    restart would be indefensible.
    """
    global _browser, _selected_target_id
    attached = _connect_target() is not None
    # Same reason as in _get_browser: .stopped is meaningless for a browser we
    # did not spawn, so an attached one counts as running whenever we hold it.
    was_running = _browser is not None and (attached or not _browser.stopped)
    if _browser is not None and not attached:
        try:
            await _close_browser_and_profile(_browser)
        except Exception:
            logger.warning("clean shutdown failed, falling back", exc_info=True)
            try:
                _browser.stop()
            except Exception:
                pass
    _browser = None
    _selected_target_id = None
    _network_collection_enabled_tabs.clear()
    _console_collection_enabled_tabs.clear()
    _named_browser_contexts.clear()
    return was_running


async def _restart_browser_with(profile_dir: str | None, profile_name: str | None) -> None:
    """Select a profile and drop the current browser so the next tool call
    relaunches Chrome with it. Any open pages are closed.

    Choosing a profile means launching our own browser, so this also leaves any
    attached browser behind. The detach has to happen first, while we still know
    the browser is not ours to close.
    """
    global _selected_profile_dir, _selected_profile_name
    global _connect_host, _connect_port, _connect_disabled
    await _stop_browser()
    _connect_host = _connect_port = None
    _connect_disabled = True
    _selected_profile_dir = profile_dir
    _selected_profile_name = profile_name


# ---------------------------------------------------------------------------
# Tools (aligned with chrome-devtools-mcp interface)
# ---------------------------------------------------------------------------

@tool(title="Bypass insecure-connection warning", open_world=True)
async def bypass_insecure_warning() -> str:
    """Click through Chrome's "Your connection is not private" interstitial.

    Use this when a navigation lands on an SSL/certificate warning page (expired
    or self-signed certificate, hostname mismatch) instead of the site itself —
    the snapshot then shows a warning page rather than the expected content.
    This performs the Advanced -> Proceed click for you.

    Has no effect on any other kind of page.
    """
    tab = await _active_tab()
    await tab.bypass_insecure_connection_warning()
    return "Bypassed insecure connection warning."


@tool(title="Solve Cloudflare challenge", open_world=True)
async def cf_verify() -> str:
    """Attempt to solve a Cloudflare "Verify you are human" challenge.

    Use when a page is stuck on a Cloudflare interstitial — a checkbox widget,
    or "Checking your browser before accessing". Drives nodriver's built-in
    verification bypass, which locates the checkbox visually and clicks it.

    Requires opencv-python to be installed; without it this returns an error.
    Many challenges also clear by themselves after a few seconds, so
    wait_for(["some text from the real page"]) is worth trying first.
    """
    tab = await _active_tab()
    try:
        await tab.verify_cf()
        return "Cloudflare verification attempted."
    except Exception as e:
        return f"Error: {e}"


async def _clickable_point(tab: uc.Tab, remote_obj: Any) -> tuple[float, float] | str:
    """A viewport point that really lands on this element, or what is covering it.

    A trusted click is delivered by coordinate, so the box centre is not good
    enough. A sticky header, a cookie banner, or the element's own layout can
    put something else on that exact pixel, and the click then goes to whatever
    is there — while still looking like it worked. So ask the page which element
    is actually hit, and try a few points before giving up.

    Returns (x, y) on success, or a string describing the blocker.
    """
    obj = await _call_function_on(
        tab,
        function_declaration=(
            "function() {"
            " const r = this.getBoundingClientRect();"
            " if (!r.width || !r.height) return JSON.stringify({"
            "   reason: 'it has zero size, so it is not rendered'});"
            " const fs = [0.5, 0.25, 0.75, 0.12, 0.88];"
            " let blocker = null;"
            " for (const fy of fs) for (const fx of fs) {"
            "   const x = r.left + r.width * fx, y = r.top + r.height * fy;"
            "   if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;"
            "   const el = document.elementFromPoint(x, y);"
            "   if (el && (el === this || this.contains(el))) return JSON.stringify({x, y});"
            "   if (el && !blocker) blocker = el;"
            " }"
            " if (!blocker) return JSON.stringify({"
            "   reason: 'it is outside the viewport even after scrolling'});"
            " const cls = (typeof blocker.className === 'string' && blocker.className)"
            "   ? '.' + blocker.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';"
            " return JSON.stringify({reason: 'it is covered by <' + blocker.tagName.toLowerCase()"
            "   + (blocker.id ? '#' + blocker.id : '') + cls + '>'});"
            " }"
        ),
        object_id=remote_obj.object_id,
        return_by_value=True,
    )
    try:
        data = json.loads(obj.value) if obj and obj.value else {}
    except (TypeError, ValueError):
        return "the page did not report a hit point"
    if "x" in data:
        return float(data["x"]), float(data["y"])
    return str(data.get("reason", "something else is in the way"))


async def _cdp_click(tab: uc.Tab, remote_obj: Any, x: float, y: float, dbl_click: bool) -> None:
    """Click through the CDP Input domain — the page sees isTrusted=true."""
    if dbl_click:
        await _double_click(tab, x, y)
    else:
        await tab.mouse_click(x, y)
    await tab


async def _scripted_click(tab: uc.Tab, uid: str, dbl_click: bool) -> None:
    """Click by dispatching an event sequence on the element itself.

    Fallback only. It triggers the same handlers (React included) and cannot
    take the renderer down, but the events carry ``isTrusted=false``, which is
    exactly what bot detection looks at — hence never the default.
    """
    remote_obj = await _resolve_uid(tab, uid)
    dbl_extra = (
        "this.dispatchEvent(new MouseEvent('dblclick', opts));" if dbl_click else ""
    )
    declaration = (
        "function() {"
        " this.scrollIntoView({block: 'center', inline: 'center'});"
        " const opts = {bubbles: true, cancelable: true, composed: true, view: window};"
        " this.dispatchEvent(new PointerEvent('pointerdown', opts));"
        " this.dispatchEvent(new MouseEvent('mousedown', opts));"
        " this.dispatchEvent(new PointerEvent('pointerup', opts));"
        " this.dispatchEvent(new MouseEvent('mouseup', opts));"
        " this.click();"
        f" {dbl_extra}"
        " return true; }"
    )
    await _call_function_on(
        tab,
        function_declaration=declaration,
        object_id=remote_obj.object_id,
        return_by_value=True,
        user_gesture=True,
    )


@tool(title="Click element", open_world=True)
async def click(
    uid: Uid,
    dbl_click: Annotated[
        bool, Field(description="Send a double-click instead of a single click.")
    ] = False,
    if_covered: Annotated[
        Literal["report", "synthetic_click"],
        Field(description=(
            "What to do when something else sits on top of the element, so a real "
            "mouse click at its position would hit that instead. "
            '"report" (the default) does not click at all: it returns an error '
            "naming what is in the way, leaving the page untouched and the session "
            "indistinguishable from a person's. Dismiss the cookie banner, close "
            "the modal or scroll, then click again. "
            '"synthetic_click" dispatches the click on the element directly, which '
            "works through anything — but the page sees `isTrusted=false`, which is "
            "precisely the signal anti-bot systems look for. Choose it when the "
            "overlay cannot be dismissed, or when stealth does not matter on this "
            "site; not as a default."
        )),
    ] = "report",
    include_snapshot: IncludeSnapshot = False,
) -> str:
    """Click an element addressed by its snapshot uid.

    This is the click you want in almost all cases — prefer it over click_at,
    because a uid survives layout shifts and raw coordinates do not. The element
    is scrolled into view first, so it need not be visible beforehand.

    Sends real CDP input events, so the page sees `isTrusted=true`, which is the
    entire point of an undetected driver. Because those are delivered by
    coordinate, the click is aimed at a point that actually hits the element:
    several points inside it are hit-tested first, since a sticky header or a
    banner can cover the centre. If none of them reach it, `if_covered` decides
    what happens, and the response always says which path was taken.

    Two other situations force the scripted fallback regardless: a touch-emulated
    target, where CDP mouse input can crash the renderer, and a CDP click that
    times out or errors. Every step is bounded at 10s, so a wedged page cannot
    hang the call.

    On "unknown uid", take a fresh take_snapshot and retry with the new uid.
    """
    import nodriver.cdp.dom as cdp_dom

    tab = await _active_tab()
    if uid not in _uid_to_backend_node_id:
        return f"Error clicking uid={uid}: unknown uid. Take a new snapshot first."
    try:
        remote_obj = await _resolve_uid(tab, uid)
    except ValueError as e:
        return f"Error clicking uid={uid}: {e}"

    # CDP input is the default: those events arrive as isTrusted, which is the
    # whole point of an undetected driver. Two cases need the scripted
    # fallback instead — a touch-emulated target, where Input.dispatchMouseEvent
    # can take the renderer down, and a page that leaves the CDP call hanging
    # or erroring. Every step is bounded so a wedged page cannot hang the call.
    fallback_reason = ""
    if _target_key(tab) in _touch_emulated_targets:
        fallback_reason = "touch-emulated target"
    else:
        # Scroll first: a coordinate click on an element that is off-screen
        # lands wherever those coordinates happen to fall.
        try:
            await tab.send(cdp_dom.scroll_into_view_if_needed(object_id=remote_obj.object_id))
            await tab
        except Exception:
            pass

        point = await _clickable_point(tab, remote_obj)
        if isinstance(point, str):
            if if_covered == "report":
                return (
                    f"Error clicking uid={uid}: {point}, so a real mouse click would land "
                    "somewhere else. Nothing was clicked. Dismiss "
                    "or scroll past whatever is in the way and try again, or pass "
                    'if_covered="synthetic_click" to click it anyway — that works '
                    "through the overlay but the page sees isTrusted=false."
                )
            fallback_reason = point
        else:
            try:
                await asyncio.wait_for(
                    _cdp_click(tab, remote_obj, point[0], point[1], dbl_click),
                    timeout=_CLICK_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                fallback_reason = f"CDP input timed out after {_CLICK_TIMEOUT_S:g}s"
            except Exception as e:
                fallback_reason = f"CDP input failed: {e}"

    if fallback_reason:
        try:
            await asyncio.wait_for(
                _scripted_click(tab, uid, dbl_click), timeout=_CLICK_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return (
                f"Error clicking uid={uid}: {fallback_reason}, and the scripted click "
                f"timed out too after {_CLICK_TIMEOUT_S:g}s (page busy or connection wedged)"
            )
        except Exception as e:
            return f"Error clicking uid={uid}: {fallback_reason}; scripted click also failed: {e}"

    result = f"Clicked uid={uid}"
    if fallback_reason:
        result += f" (scripted click, page sees isTrusted=false — {fallback_reason})"
    result += await _maybe_snapshot(include_snapshot)
    return result


async def _scripted_click_at(tab: uc.Tab, x: int, y: int, dbl_click: bool) -> None:
    """Scripted click on whatever sits at (x, y). Same caveats as _scripted_click."""
    import nodriver.cdp.runtime as cdp_runtime

    dbl_extra = "el.dispatchEvent(new MouseEvent('dblclick', opts));" if dbl_click else ""
    expression = (
        "(function() {"
        f" const el = document.elementFromPoint({int(x)}, {int(y)});"
        " if (!el) return 'no element at point';"
        f" const opts = {{bubbles: true, cancelable: true, composed: true, view: window,"
        f" clientX: {int(x)}, clientY: {int(y)}}};"
        " el.dispatchEvent(new PointerEvent('pointerdown', opts));"
        " el.dispatchEvent(new MouseEvent('mousedown', opts));"
        " el.dispatchEvent(new PointerEvent('pointerup', opts));"
        " el.dispatchEvent(new MouseEvent('mouseup', opts));"
        " el.click();"
        f" {dbl_extra}"
        " return ''; })()"
    )
    result = await tab.send(cdp_runtime.evaluate(expression=expression, return_by_value=True))
    remote, exc = result if isinstance(result, tuple) else (result, None)
    if exc is not None:
        raise RuntimeError(_format_exception_details(exc))
    if remote is not None and getattr(remote, "value", ""):
        raise RuntimeError(str(remote.value))


@tool(title="Click at coordinates", open_world=True)
async def click_at(
    x: Annotated[
        int,
        Field(description="X coordinate in CSS pixels, relative to the viewport's top-left corner."),
    ],
    y: Annotated[
        int,
        Field(description="Y coordinate in CSS pixels, relative to the viewport's top-left corner."),
    ],
    dbl_click: Annotated[
        bool, Field(description="Send a double-click instead of a single click.")
    ] = False,
    include_snapshot: IncludeSnapshot = False,
) -> str:
    """Click a raw viewport coordinate instead of an element.

    Use `click` with a uid whenever the target shows up in a snapshot — it is
    robust against layout shifts, this is not. Coordinates are for surfaces with
    no addressable element: canvases, maps, video players, image hotspots.

    The point must lie inside the current viewport; scroll_page or
    scroll_to_selector first if it does not. Coordinates are in CSS pixels and
    ignore the device pixel ratio, so they match what emulate/resize_page report.

    Same input path as `click`: real CDP events, the same reported scripted
    fallback, the same 10s bound per step.
    """
    tab = await _active_tab()

    async def _cdp() -> None:
        if dbl_click:
            await _double_click(tab, x, y)
        else:
            await tab.mouse_click(x, y)

    fallback_reason = ""
    if _target_key(tab) in _touch_emulated_targets:
        fallback_reason = "touch-emulated target"
    else:
        try:
            await asyncio.wait_for(_cdp(), timeout=_CLICK_TIMEOUT_S)
        except asyncio.TimeoutError:
            fallback_reason = f"CDP input timed out after {_CLICK_TIMEOUT_S:g}s"
        except Exception as e:
            fallback_reason = f"CDP input failed: {e}"

    if fallback_reason:
        try:
            await asyncio.wait_for(
                _scripted_click_at(tab, x, y, dbl_click), timeout=_CLICK_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return (
                f"Error clicking at ({x}, {y}): {fallback_reason}, and the scripted click "
                f"timed out too after {_CLICK_TIMEOUT_S:g}s (page busy or connection wedged)"
            )
        except Exception as e:
            return (
                f"Error clicking at ({x}, {y}): {fallback_reason}; "
                f"scripted click also failed: {e}"
            )

    result = f"Clicked at ({x}, {y})"
    if fallback_reason:
        result += f" (scripted click, page sees isTrusted=false — {fallback_reason})"
    result += await _maybe_snapshot(include_snapshot)
    return result


@tool(title="Close page", destructive=True)
async def close_page(
    page_id: Annotated[
        int,
        Field(description=(
            "Index of the page to close, as listed by list_pages. -1 (the default) "
            "closes the currently selected page."
        )),
    ] = -1,
) -> str:
    """Close a single browser tab.

    The last remaining tab cannot be closed — use close_browser to shut Chrome
    down entirely. Closing the selected page clears the selection, so subsequent
    tools fall back to the most recently opened tab.

    Returns the remaining open pages, because indices shift when a tab closes:
    re-read them from this response rather than reusing older ones.
    """
    global _selected_target_id
    browser = await _get_browser()
    if len(browser.tabs) <= 1:
        return "Error: Cannot close the last open page."
    if page_id == -1:
        tab = await _active_tab()
    else:
        if page_id < 0 or page_id >= len(browser.tabs):
            return f"Error: Invalid page_id {page_id}, have {len(browser.tabs)} tabs."
        tab = browser.tabs[page_id]
    if tab.target and str(tab.target.target_id) == _selected_target_id:
        _selected_target_id = None
    await tab.close()
    pages = await _format_pages()
    return f"Page closed.{pages}"


@tool(title="Close browser", destructive=True)
async def close_browser() -> str:
    """Quit Chrome entirely, closing every tab.

    Unlike close_page (which always keeps one tab alive), this tears down the
    whole browser. Chrome relaunches automatically on the next tool call with
    the currently selected profile, so this is also how you apply pending launch
    flags without switching profiles.

    On an ephemeral temp profile (the default) this discards cookies, logins and
    localStorage — save_session first if you need them back. Persistent profiles
    created with create_profile keep everything.

    It keeps the browser itself. The name, its selected profile and its launch
    flags survive for the next relaunch, so the name goes on counting against the
    12-browser limit. When an agent is finished for good, call shutdown_browser
    instead: it also releases the name.
    """
    attached = _connect_target() is not None
    running = await _stop_browser()
    if attached:
        return (
            "Detached from the running browser, which is still open — it is not "
            "ours to close. The next action reattaches to it; use use_temp_profile "
            "to go back to a browser this server launches itself."
            if running else "Was not attached to a browser."
        )
    return (
        "Browser closed. It will relaunch on the next action. The browser keeps "
        "its name and its slot against the 12-browser limit — use shutdown_browser "
        "to release those too."
        if running else "No browser was running."
    )


@tool(title="Drag and drop", open_world=True)
async def drag(
    from_uid: Annotated[
        str,
        Field(description="uid of the element to pick up, from the most recent take_snapshot."),
    ],
    to_uid: Annotated[
        str,
        Field(description="uid of the element to drop onto, from the most recent take_snapshot."),
    ],
    include_snapshot: IncludeSnapshot = False,
) -> str:
    """Drag one element onto another (press, move, release).

    Both uids must come from the same take_snapshot. Works for native HTML5
    drag-and-drop and for mouse-driven sortable lists.

    Some JS drag libraries require a stream of intermediate mousemove events
    that this does not emit; if a drag silently does nothing, drive it manually
    with click_at and press_key instead.
    """
    tab = await _active_tab()
    try:
        src_x, src_y = await _get_box_model(tab, from_uid)
        dst_x, dst_y = await _get_box_model(tab, to_uid)
        await tab.mouse_drag((src_x, src_y), (dst_x, dst_y))
        result = f"Dragged uid={from_uid} to uid={to_uid}"
        result += await _maybe_snapshot(include_snapshot)
        return result
    except Exception as e:
        return f"Error dragging: {e}"


@tool(title="Emulate page conditions", idempotent=True)
async def emulate(
    network_conditions: NetworkConditions = "",
    cpu_throttling_rate: CpuThrottlingRate = 0,
    geolocation: Geolocation = None,
    user_agent: Annotated[
        str | None,
        Field(description=(
            "Override the User-Agent header and navigator.userAgent. Omit to leave "
            "unchanged; pass an empty string to restore Chrome's real one. This does "
            "NOT touch UA client hints (Sec-CH-UA-*), which then contradict the "
            "spoofed UA and give the automation away — use emulate_device for mobile, "
            "it sets both consistently."
        )),
    ] = None,
    color_scheme: ColorScheme = "",
    viewport: Annotated[
        str,
        Field(description=(
            'Viewport override as "WIDTHxHEIGHTxDPR[,mobile][,touch][,landscape]", '
            'e.g. "375x812x3,mobile,touch" or "1920x1080x1". The trailing flags are '
            "optional: `mobile` turns on mobile viewport behaviour, `touch` enables "
            "touch emulation, `landscape` sets the screen orientation. Empty string "
            "leaves the viewport unchanged."
        )),
    ] = "",
) -> str:
    """Emulate network, CPU, geolocation, user agent, color scheme or viewport.

    Applies to the selected page and persists across navigations until
    reset_emulation. Every parameter is independent — pass only what you want to
    change, leave the rest at their defaults.

    To emulate a real phone or tablet, use emulate_device instead: it sets user
    agent, client hints, viewport, DPR and touch as one coherent set, which
    hand-assembled overrides here get wrong in ways anti-bot systems detect.

    Turning `touch` on in the viewport also changes how `click` behaves — CDP
    mouse input can crash a touch-emulated renderer, so clicks fall back to the
    scripted path (`isTrusted=false`) for as long as touch is enabled.
    """
    tab = await _active_tab()
    results = await _apply_emulation(
        tab,
        network_conditions=network_conditions,
        cpu_throttling_rate=cpu_throttling_rate,
        geolocation=geolocation,
        user_agent=user_agent,
        color_scheme=color_scheme,
        viewport=viewport,
    )

    return "Emulation applied: " + ", ".join(results) if results else "No emulation changes applied."


@tool(title="Reset emulation", idempotent=True)
async def reset_emulation() -> str:
    """Clear every emulation override on the selected page.

    Resets in one call: network throttling, CPU throttling, geolocation, user
    agent and client hints, color scheme, viewport, device pixel ratio, page
    scale and touch emulation — back to the real browser defaults.

    Use after emulate or emulate_device to make the page behave like ordinary
    desktop Chrome again. Turning touch emulation off here also restores trusted
    CDP clicks.
    """
    tab = await _active_tab()
    results = await _reset_emulation(tab)
    return "Emulation reset: " + ", ".join(results)


@tool(title="Emulate device preset", idempotent=True)
async def emulate_device(
    device: DevicePreset,
    color_scheme: ColorScheme = "",
    network_conditions: NetworkConditions = "",
    cpu_throttling_rate: CpuThrottlingRate = 0,
    geolocation: Geolocation = None,
) -> str:
    """Emulate a phone or tablet with one internally consistent set of signals.

    Preferred over assembling `emulate` parameters by hand for mobile work: user
    agent, UA client hints (Sec-CH-UA-*), viewport, device pixel ratio, touch
    support and Accept-Language are set together so they cannot contradict each
    other — a mismatch between them is a classic automation tell.

    Presets are pixel_7, pixel_7_landscape and ipad_air (aliases are listed in
    the `device` parameter). ipad_air deliberately reports desktop-class Safari
    with touch and sends no client hints, which is what a real iPad does.

    Applies to the selected page and survives navigation. To have mobile signals
    present on a page's very first request, pass `device` to new_page or
    navigate_page instead of calling this afterwards. Undo with reset_emulation.
    """
    if _resolve_device_preset(device) is None:
        supported = ", ".join(sorted(_DEVICE_PRESETS))
        return f"Error: Unknown device preset '{device}'. Supported presets: {supported}"

    tab = await _active_tab()
    device_results = await _apply_device_preset(tab, device)
    extra_results = await _apply_emulation(
        tab,
        network_conditions=network_conditions,
        cpu_throttling_rate=cpu_throttling_rate,
        geolocation=geolocation,
        color_scheme=color_scheme,
    )
    return "Emulation applied: " + ", ".join([*device_results, *extra_results])


async def _evaluate_value(tab: Any, expression: str, await_promise: bool = False) -> Any:
    """Evaluate an expression and return its value as plain JSON.

    nodriver's Tab.evaluate cannot be used where the result matters. It asks for
    "deep" serialization, so an object comes back as CDP's wire format —
    [["a", {"type": "number", "value": 1}], ...] instead of {"a": 1} — which is
    several times the tokens and not what any caller wants. Worse, on a JS error
    it *returns* the ExceptionDetails object instead of raising, and that object
    is truthy, so `if await tab.evaluate(...)` treats a broken selector as a hit.

    This asks for the value directly and raises on a JS error, so both problems
    disappear at every call site that uses it.
    """
    import nodriver.cdp.runtime as cdp_runtime

    remote, errors = await tab.send(
        cdp_runtime.evaluate(
            expression=expression,
            user_gesture=True,
            await_promise=await_promise,
            return_by_value=True,
            allow_unsafe_eval_blocked_by_csp=True,
        )
    )
    if errors:
        text = getattr(errors, "text", None) or "JavaScript error"
        exc = getattr(errors, "exception", None)
        detail = getattr(exc, "description", None) or getattr(exc, "value", None)
        raise RuntimeError(f"{text}: {detail}" if detail else text)
    return remote.value if remote is not None else None


@tool(title="Evaluate JavaScript", open_world=True)
async def evaluate_script(
    function: Annotated[
        str,
        Field(description=(
            'A JavaScript function expression, e.g. "() => document.title" or '
            '"(el) => el.innerText". It is invoked immediately and its return value '
            "is JSON-serialised back to you. Async functions are awaited, so "
            "\"async () => (await fetch('/api/x')).json()\" works. Return a value — "
            "console.log output is not captured. Leave empty when using script_path."
        )),
    ] = "",
    script_path: Annotated[
        str,
        Field(description=(
            "Read the function from this local .js file instead of the `function` "
            "parameter. Use it for anything long or quote-heavy, where escaping the "
            "script into a JSON string is where the mistakes happen."
        )),
    ] = "",
    file_path: Annotated[
        str,
        Field(description=(
            "Write the JSON result to this local path instead of returning it — for "
            "extractions too large to put in the conversation."
        )),
    ] = "",
    args: Annotated[
        list[str] | None,
        Field(description=(
            "Element uids from the most recent take_snapshot, resolved to live DOM "
            "nodes and passed as the function's arguments. The first uid is also "
            "bound as `this`. Omit for page-level scripts."
        )),
    ] = None,
) -> str:
    """Run JavaScript inside the page and get the result back as JSON.

    The escape hatch for anything the other tools do not cover: reading computed
    styles, calling a page's own JS API, or extracting structured data in a
    single round trip instead of a dozen snapshot-and-click cycles.

    Without `args` the function runs at page level in the main world. With
    `args`, the given uids become real element references, which is how you
    operate on one specific element from a snapshot.

    Return values must be JSON-serialisable — DOM nodes, functions and circular
    structures are not, so map them to plain values inside the function. Errors
    are returned as a string beginning with "Error:" rather than raised.

    Pass script_path to run a function kept in a .js file, and file_path to write
    the result to disk instead of into the conversation.
    """
    if script_path:
        if function.strip():
            return "Error: pass either function or script_path, not both."
        try:
            with open(script_path, encoding="utf-8") as fh:
                function = fh.read()
        except OSError as e:
            return f"Error reading {script_path}: {e}"
    if not function.strip():
        return "Error: nothing to run — provide function or script_path."

    def _deliver(value: Any) -> str:
        payload = json.dumps(value, default=str)
        if file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as fh:
                    fh.write(payload)
            except OSError as e:
                return f"Error writing {file_path}: {e}"
            return f"Result ({len(payload)} chars) written to {file_path}."
        return f"```json\n{payload}\n```"

    tab = await _active_tab()
    try:
        if args:
            # Resolve uids to remote objects and call the function with them.
            import nodriver.cdp.runtime as cdp_runtime

            remote_objs = []
            arg_objects = []
            for uid in args:
                remote_obj = await _resolve_uid(tab, uid)
                remote_objs.append(remote_obj)
                arg_objects.append(cdp_runtime.CallArgument(object_id=remote_obj.object_id))

            # call_function_on requires a binding target for its execution
            # context; use the first resolved element (also bound as `this`).
            remote = await _call_function_on(
                tab,
                function_declaration=function,
                object_id=remote_objs[0].object_id,
                arguments=arg_objects,
                return_by_value=True,
            )
            value = remote.value if remote else None
            return _deliver(value)
        else:
            # Simple evaluation without element args
            # If user passed a function declaration, wrap it in a call
            expr = function.strip()
            if expr.startswith("(") or expr.startswith("function") or expr.startswith("async"):
                expr = f"({expr})()"
            result = await _evaluate_value(tab, expr, await_promise=True)
            return _deliver(result)
    except Exception as e:
        return f"Error: {e}"


@tool(title="Fill input", open_world=True)
async def fill(
    uid: Uid,
    value: Annotated[
        str,
        Field(description=(
            "The text to enter. For a <select> element this must match the option's "
            "value attribute, not its visible label."
        )),
    ],
    include_snapshot: IncludeSnapshot = False,
) -> str:
    """Set the value of an input, textarea, select or contenteditable element.

    Selects whatever is already in the field and types over it, character by
    character, so `input` events fire and React/Vue-style controlled components
    register the change. It deliberately does not blank the field first: that
    makes such components re-render, the focus follows the replaced node, and
    the keystrokes end up nowhere. For <select>, the option is chosen by value
    and a `change` event is dispatched.

    The value is read back afterwards. If it did not land you get an error, not
    a success you cannot trust — a field can be read-only, disabled, covered by
    an overlay, or rewrite what you type, and silently reporting success would
    send you looking for the problem several steps later.

    Use type_text instead when you want to append to a focused field rather than
    replace its contents. For several fields at once, fill_form does it in one
    round trip.
    """
    tab = await _active_tab()
    try:
        await _fill_element(tab, uid, value)
        result = f"Filled uid={uid} with \"{value}\""
        result += await _maybe_snapshot(include_snapshot)
        return result
    except Exception as e:
        return f"Error filling uid={uid}: {e}"


class FormField(BaseModel):
    """A single field for fill_form to populate."""

    uid: Annotated[
        str,
        Field(description="Element uid from the most recent take_snapshot, e.g. \"4_12\"."),
    ]
    value: Annotated[
        str,
        Field(description=(
            "The text to enter. For a <select> element, the option's value "
            "attribute rather than its visible label."
        )),
    ]


@tool(title="Fill form", open_world=True)
async def fill_form(
    elements: Annotated[
        list[FormField],
        Field(description=(
            'The fields to fill, in order — each entry is {"uid": "...", "value": "..."}.'
        )),
    ],
    include_snapshot: IncludeSnapshot = False,
) -> str:
    """Fill several form fields in a single call.

    Behaves like `fill` per field, but costs one round trip instead of one per
    field. Fields are processed in the order given, which matters on pages that
    reveal or enable later fields in response to earlier ones.

    A field that fails does not abort the rest: the response reports success or
    the specific error per uid, so a partially filled form is always visible
    rather than silent.
    """
    tab = await _active_tab()
    results = []
    for elem_spec in elements:
        uid = elem_spec.uid
        value = elem_spec.value
        try:
            await _fill_element(tab, uid, value)
            results.append(f"  uid={uid}: filled")
        except Exception as e:
            results.append(f"  uid={uid}: error — {e}")
    result = "Form fill results:\n" + "\n".join(results)
    result += await _maybe_snapshot(include_snapshot)
    return result


@tool(title="Get console message", read_only=True)
async def get_console_message(
    msgid: Annotated[
        int,
        Field(ge=0, description="Message id, as shown in square brackets by list_console_messages."),
    ],
) -> str:
    """Read one console message in full, by id.

    list_console_messages truncates every entry to 200 characters; use this to
    get a complete stack trace or error payload.

    Requires enable_console_collection to have been called for this page. The
    server leaves the CDP Runtime domain disabled by default because enabling it
    is itself something sites can detect.
    """
    tab = await _active_tab()
    if id(tab) not in _console_collection_enabled_tabs:
        return "Error: Console collection is disabled for the current page. Call enable_console_collection first."
    match = [m for m in _all_console_messages() if m.get("seq") == msgid]
    if not match:
        return f"Error: No console message with id {msgid}."
    msg = match[-1]
    return f"[{msg['type']}] {msg['text']} (timestamp: {msg['timestamp']})"


@tool(title="Get cookies", read_only=True)
async def get_cookies(
    url: Annotated[
        str,
        Field(description=(
            "Only return cookies that would be sent to this URL (matching domain, path "
            "and secure flag). Empty string returns every cookie in the browser."
        )),
    ] = "",
) -> str:
    """List browser cookies with their domain, path and secure flag.

    Reads the whole browser cookie jar, not just the current page, unless you
    pass `url`. Values are returned in full, so treat the output as sensitive —
    it contains live session tokens.

    To carry these across a browser restart use save_session, or switch to a
    persistent profile with use_profile.
    """
    tab = await _active_tab()
    import nodriver.cdp.network as cdp_net
    import nodriver.cdp.storage as cdp_storage
    if url:
        cookies = await tab.send(cdp_net.get_cookies(urls=[url]))
    else:
        # Network.getCookies returns only the cookies of the tab it is sent to,
        # despite the name — with two tabs open it silently answers about
        # whichever one happens to be selected. Storage.getCookies is the
        # whole-jar call this tool documents.
        cookies = await tab.send(cdp_storage.get_cookies())
    lines = [f"Cookies ({len(cookies)}):"]
    for c in cookies:
        lines.append(f"  {c.name}={c.value} (domain={c.domain}, path={c.path}, secure={c.secure})")
    return "\n".join(lines)


@tool(title="Get localStorage", read_only=True)
async def get_local_storage() -> str:
    """Read all localStorage entries for the current page's origin.

    Values are truncated to 200 characters each; for one entry in full use
    evaluate_script with `() => localStorage.getItem("key")`.

    localStorage is scoped per origin, so this returns nothing on about:blank —
    navigate to the site first. sessionStorage is not included.
    """
    tab = await _active_tab()
    data = await tab.get_local_storage()
    lines = ["localStorage items:"]
    for k, v in (data or {}).items():
        lines.append(f"  {k}: {str(v)[:200]}")
    return "\n".join(lines)


@tool(title="Get network request", read_only=True)
async def get_network_request(
    reqid: Annotated[
        int | None,
        Field(description=(
            "Request id, as shown in square brackets by list_network_requests. "
            "Omit to get the most recent request."
        )),
    ] = None,
    request_file_path: Annotated[
        str,
        Field(description=(
            "Write the request body (POST data) to this local file path instead of "
            "including it in the response."
        )),
    ] = "",
    response_file_path: Annotated[
        str,
        Field(description=(
            "Write the response body to this local file path instead of including it. "
            "Use this for binary responses (images, PDFs, archives) — they are "
            "base64-decoded on the way out."
        )),
    ] = "",
) -> str:
    """Inspect one network request: URL, method, resource type and both bodies.

    Often the fastest way to get structured data out of a site: find the page's
    own API call with list_network_requests, then read the JSON it already
    received here, instead of scraping the rendered DOM.

    Inline bodies are truncated at 5000 characters, so pass a file path for
    anything larger. Response bodies are only available while Chrome still holds
    them in its buffer — for a long-finished request the entry may still be
    listed while its body is already gone.
    """
    tab = await _active_tab()
    # getResponseBody is a Network-domain command: without the domain enabled on
    # *this* session Chrome holds no body to give back.
    await _auto_enable_network_collection(tab)
    import nodriver.cdp.network as cdp_net

    if reqid is None:
        if not _network_requests:
            return "No network requests collected."
        req = _network_requests[-1]
    else:
        match = [r for r in _all_network_requests() if r.get("seq") == reqid]
        if not match:
            return f"Error: No network request with id {reqid}."
        req = match[-1]

    lines = [f"Request #{reqid if reqid is not None else 'latest'}:"]
    lines.append(f"  URL: {req['url']}")
    lines.append(f"  Method: {req['method']}")
    lines.append(f"  Type: {req['type']}")

    try:
        request_body = await tab.send(cdp_net.get_request_post_data(cdp_net.RequestId(req["id"])))
        if request_file_path:
            with open(request_file_path, "w", encoding="utf-8") as f:
                f.write(request_body)
            lines.append(f"  Request body saved to: {request_file_path}")
        elif request_body:
            lines.append(f"  Request body ({len(request_body)} chars): {request_body[:5000]}")
    except Exception as e:
        if request_file_path:
            lines.append(f"  Request body: Error — {e}")

    try:
        body_result = await tab.send(cdp_net.get_response_body(cdp_net.RequestId(req['id'])))
        body_content = body_result[0]
        is_base64 = body_result[1]

        if response_file_path:
            if is_base64:
                with open(response_file_path, "wb") as f:
                    f.write(base64.b64decode(body_content))
            else:
                with open(response_file_path, "w") as f:
                    f.write(body_content)
            lines.append(f"  Response body saved to: {response_file_path}")
        else:
            lines.append(f"  Response body ({len(body_content)} chars): {body_content[:5000]}")
    except Exception as e:
        lines.append(f"  Response body: Error — {e}")

    return "\n".join(lines)


@tool(title="Handle JavaScript dialog")
async def handle_dialog(
    action: Annotated[
        Literal["accept", "dismiss"],
        Field(description=(
            'Accept (OK) or dismiss (Cancel) the dialog. Either closes an alert(); '
            "for confirm() the choice is what the page's JavaScript receives."
        )),
    ] = "accept",
    prompt_text: Annotated[
        str,
        Field(description=(
            "Text to enter into a prompt() dialog before accepting it. Ignored for "
            "alert() and confirm() dialogs."
        )),
    ] = "",
) -> str:
    """Answer an open JavaScript dialog — alert, confirm or prompt.

    A dialog blocks the page and every subsequent tool call until it is handled,
    so call this as soon as one appears. Returns an error if no dialog is open.

    beforeunload dialogs triggered by navigating away are handled automatically
    via navigate_page's `handle_before_unload` parameter; this tool is for
    dialogs the page opens by itself.
    """
    tab = await _active_tab()
    import nodriver.cdp.page as cdp_page
    try:
        if action == "accept":
            await tab.send(cdp_page.handle_java_script_dialog(accept=True, prompt_text=prompt_text))
        else:
            await tab.send(cdp_page.handle_java_script_dialog(accept=False))
    except Exception as e:
        return f"Error handling dialog (is one open?): {e}"
    return f"Dialog {action}ed."


@tool(title="Hover element", open_world=True)
async def hover(uid: Uid, include_snapshot: IncludeSnapshot = False) -> str:
    """Move the mouse over an element without clicking it.

    This is how you open hover-triggered menus, tooltips and dropdowns before
    reading what they reveal — pass include_snapshot=true to get the revealed
    content back in the same call.

    Only moves the pointer; it does not scroll. If the element sits outside the
    viewport, call scroll_to_selector first.
    """
    tab = await _active_tab()
    try:
        cx, cy = await _get_box_model(tab, uid)
        await tab.mouse_move(cx, cy)
        result = f"Hovered over uid={uid}"
        result += await _maybe_snapshot(include_snapshot)
        return result
    except Exception as e:
        return f"Error hovering uid={uid}: {e}"


@tool(title="List console messages", read_only=True)
async def list_console_messages(
    page_size: Annotated[
        int | None,
        Field(ge=1, description="Maximum number of messages to return. Omit to return all."),
    ] = None,
    page_idx: Annotated[
        int, Field(ge=0, description="0-based page number, used together with page_size.")
    ] = 0,
    types: Annotated[
        list[
            Literal[
                "log", "debug", "info", "error", "warning", "dir", "dirxml", "table",
                "trace", "clear", "startGroup", "startGroupCollapsed", "endGroup",
                "assert", "profile", "profileEnd", "count", "timeEnd",
            ]
        ]
        | None,
        Field(description=(
            'Only return these console types, e.g. ["error", "warning"]. Note the '
            'value is "warning", not "warn". Omit for all types.'
        )),
    ] = None,
    include_preserved_messages: Annotated[
        bool,
        Field(description=(
            "Also include messages from before the last navigation — the server keeps "
            "the previous 3 navigations. Default false: current page only."
        )),
    ] = False,
) -> str:
    """List console output collected for the selected page.

    Requires enable_console_collection first. Console capture is opt-in because
    it needs the CDP Runtime domain, which some sites use to detect an attached
    debugger; without it this returns a reminder instead of messages.

    Each line is prefixed with its id and truncated to 200 characters — pass the
    id to get_console_message for the full text. Only the most recent 1000
    messages are retained.
    """
    tab = await _active_tab()
    if id(tab) not in _console_collection_enabled_tabs:
        return "Console collection is disabled for the current page. Call enable_console_collection first."

    if include_preserved_messages:
        all_msgs = []
        for batch in _preserved_console_messages:
            all_msgs.extend(batch)
        all_msgs.extend(_console_messages)
        filtered = all_msgs
    else:
        filtered = list(_console_messages)

    if types:
        filtered = [m for m in filtered if m["type"] in types]

    total = len(filtered)
    if page_size:
        start = page_idx * page_size
        filtered = filtered[start:start + page_size]

    lines = [f"Console messages ({len(filtered)} of {total}):"]
    for msg in filtered:
        lines.append(f"  [{msg.get('seq', '?')}] [{msg['type']}] {msg['text'][:200]}")
    return "\n".join(lines)


@tool(title="Enable console collection", idempotent=True)
async def enable_console_collection() -> str:
    """Start capturing console output on the current page.

    Call this before list_console_messages or get_console_message — neither
    returns anything until collection is on. Network capture, by contrast, is
    always on and needs no equivalent call.

    This is opt-in rather than automatic because it enables the CDP Runtime
    domain, which some anti-bot scripts probe for to detect an attached
    debugger. Leave it off while stealth matters, and use
    disable_console_collection when you are done debugging.

    Applies per page: a new tab needs its own call.
    """
    tab = await _active_tab()
    changed = await _enable_console_collection(tab)
    if changed:
        return "Console collection enabled on the current page."
    return "Console collection was already enabled on the current page."


@tool(title="Disable console collection", idempotent=True)
async def disable_console_collection() -> str:
    """Stop capturing console output on the current page.

    Turns the CDP Runtime domain back off, which restores the quieter,
    harder-to-detect default — worth doing once you are finished debugging and
    the page still has anti-bot checks ahead of it.

    Messages already collected stay readable; only new ones stop arriving.
    """
    tab = await _active_tab()
    changed = await _disable_console_collection(tab)
    if changed:
        return "Console collection disabled on the current page."
    return "Console collection was already disabled on the current page."


@tool(title="List network requests", read_only=True)
async def list_network_requests(
    page_size: Annotated[
        int | None,
        Field(ge=1, description="Maximum number of requests to return. Omit to return all."),
    ] = None,
    page_idx: Annotated[
        int, Field(ge=0, description="0-based page number, used together with page_size.")
    ] = 0,
    resource_types: Annotated[
        list[
            Literal[
                "Document", "Stylesheet", "Image", "Media", "Font", "Script",
                "TextTrack", "XHR", "Fetch", "Prefetch", "EventSource", "WebSocket",
                "Manifest", "SignedExchange", "Ping", "CSPViolationReport",
                "Preflight", "FedCM", "Other",
            ]
        ]
        | None,
        Field(description=(
            'Only return these resource types. ["XHR", "Fetch"] is the useful filter '
            "for finding a page's own API calls. Matching is case-insensitive. Omit "
            "for all types."
        )),
    ] = None,
    url_filter: Annotated[
        str,
        Field(description=(
            "Only return requests whose URL contains this substring (plain text, not "
            'a regex), e.g. "/api/" or "graphql".'
        )),
    ] = "",
    include_preserved_requests: Annotated[
        bool,
        Field(description=(
            "Also include requests from before the last navigation — the server keeps "
            "the previous 3 navigations. Default false: current page only."
        )),
    ] = False,
) -> str:
    """List the network requests the selected page has made.

    Collection is automatic — unlike console capture, nothing needs enabling.

    The main use is finding the JSON API a page calls: filter with
    resource_types=["XHR", "Fetch"], then pass the id from the square brackets
    to get_network_request to read the actual response body. That is usually far
    cheaper and more reliable than scraping the rendered DOM.

    Only the most recent 1000 requests are retained, and each URL is truncated
    to 150 characters in this listing.
    """
    if include_preserved_requests:
        all_reqs = []
        for batch in _preserved_network_requests:
            all_reqs.extend(batch)
        all_reqs.extend(_network_requests)
        filtered = all_reqs
    else:
        filtered = list(_network_requests)

    if resource_types:
        filtered = [r for r in filtered if r["type"].lower() in [t.lower() for t in resource_types]]
    if url_filter:
        filtered = [r for r in filtered if url_filter in r["url"]]

    total = len(filtered)
    if page_size:
        start = page_idx * page_size
        filtered = filtered[start:start + page_size]

    lines = [f"Network requests ({len(filtered)} of {total}):"]
    for req in filtered:
        lines.append(f"  [{req.get('seq', '?')}] {req['method']} {req['url'][:150]} ({req['type']})")
    return "\n".join(lines)


@tool(title="List open pages", read_only=True)
async def list_pages() -> str:
    """List every open browser tab with its index, URL and title.

    The index shown here is the `page_id` taken by select_page and close_page.
    Indices are positional and shift whenever a tab opens or closes, so re-read
    them here rather than reusing an index from an earlier call.

    Tools act on the page chosen with select_page, or on the most recently
    opened tab if none was selected; the selected one is marked here.

    Each entry also carries the page's CDP targetId, which is the stable
    identity of a tab: unlike the index it survives other tabs opening and
    closing, and it is what CDP-level tooling and logs refer to.
    """
    browser = await _get_browser()
    await _refresh_targets(browser)
    active = await _active_tab()
    lines = ["Open pages:"]
    for i, tab in enumerate(browser.tabs):
        url = tab.target.url or "about:blank"
        title = tab.target.title or ""
        target_id = str(tab.target.target_id) if tab.target else "?"
        mark = "  <- selected" if tab is active else ""
        lines.append(f"  [{i}] {url} — {title} (targetId={target_id}){mark}")
    return "\n".join(lines)


@tool(title="Navigate page", open_world=True)
async def navigate_page(
    type: Annotated[
        Literal["url", "back", "forward", "reload"],
        Field(description=(
            'The kind of navigation: "url" loads `url`, "back" and "forward" move '
            'through session history, "reload" reloads the current page.'
        )),
    ] = "url",
    url: Annotated[
        str,
        Field(description=(
            'Target URL — required when type="url", ignored otherwise. Include the '
            'scheme: "https://example.com", not "example.com".'
        )),
    ] = "",
    ignore_cache: Annotated[
        bool,
        Field(description='Bypass the HTTP cache (a hard reload). Only applies to type="reload".'),
    ] = False,
    handle_before_unload: Annotated[
        Literal["accept", "dismiss"],
        Field(description=(
            'How to answer a beforeunload confirmation ("Leave site?") raised by the '
            'page being left. "accept" leaves, "dismiss" stays — in which case the '
            "navigation does not happen."
        )),
    ] = "accept",
    init_script: Annotated[
        str,
        Field(description=(
            "JavaScript executed in every new document before any of the page's own "
            "scripts, for this navigation. Use it to stub out APIs or plant values a "
            "page reads at startup. Plain statements, not a function expression."
        )),
    ] = "",
    timeout: TimeoutMs = 0,
    device: DevicePreset = "",
) -> str:
    """Navigate the selected page — load a URL, or go back, forward or reload.

    Navigating rotates the collected logs: the previous page's console and
    network entries move into the preserved history (the last 3 navigations are
    kept), still reachable via `include_preserved_*` on the list tools.

    Passing `device` applies the emulation before the request is sent, so the
    server sees mobile signals on the very first byte — calling emulate_device
    afterwards is too late for that first request.

    Returns the resulting URL together with all open pages. This reuses the
    current tab; use new_page to open an additional one.
    """
    tab = await _active_tab()

    if device and _resolve_device_preset(device) is None:
        supported = ", ".join(sorted(_DEVICE_PRESETS))
        return f"Error: Unknown device preset '{device}'. Supported presets: {supported}"
    if type == "url" and not url:
        return "Error: URL is required for type=url."

    # Preserve current console/network messages before navigation
    _preserve_on_navigation()

    import nodriver.cdp.page as cdp_page

    async def _on_javascript_dialog(event: cdp_page.JavascriptDialogOpening):
        dialog_type = getattr(getattr(event, "type_", None), "value", getattr(event, "type_", None))
        if str(dialog_type).lower() != "beforeunload":
            return
        await tab.send(
            cdp_page.handle_java_script_dialog(
                accept=(handle_before_unload == "accept"),
            )
        )

    await tab.send(cdp_page.enable())
    tab.add_handler(cdp_page.JavascriptDialogOpening, _on_javascript_dialog)

    # Inject init script if provided (runs before page scripts on next navigation)
    if init_script:
        await tab.send(cdp_page.add_script_to_evaluate_on_new_document(source=init_script))

    note = ""

    async def _navigate() -> None:
        nonlocal note
        if type == "url":
            if not url:
                raise ValueError("URL is required for type=url.")
            note = await _navigate_same_tab(tab, url)
        elif type == "back":
            await tab.back()
            await tab
        elif type == "forward":
            await tab.forward()
            await tab
        elif type == "reload":
            await tab.reload(ignore_cache=ignore_cache)
            await tab

    try:
        device_results: list[str] = []
        if device:
            device_results = await _apply_device_preset(tab, device)

        await _await_with_timeout(_navigate(), timeout, f"Navigation ({type})")
        # Auto-enable network collection on navigated tab.
        await _auto_enable_network_collection(tab)
        # Page.navigate does not touch nodriver's cached TargetInfo, and reading
        # tab.target.url straight after it can still report the previous URL.
        await _refresh_targets(await _get_browser())
        pages = await _format_pages()
        suffix = f" (pre-navigation emulation: {', '.join(device_results)})" if device_results else ""
        return f"Navigated to {tab.target.url or 'about:blank'}{suffix}{note}{pages}"
    except Exception as e:
        return f"Error: {e}"
    finally:
        tab.remove_handler(cdp_page.JavascriptDialogOpening, _on_javascript_dialog)


@tool(title="Open new page", open_world=True)
async def new_page(
    url: Annotated[
        str,
        Field(description=(
            'URL to load in the new tab, including the scheme. Defaults to '
            '"about:blank" for an empty tab.'
        )),
    ] = "about:blank",
    background: Annotated[
        bool,
        Field(description=(
            "Open the tab without focusing it, leaving the current page selected. "
            "Default false, which focuses the new page and makes it the target of "
            "all following tool calls."
        )),
    ] = False,
    isolated_context: Annotated[
        str,
        Field(description=(
            "Open the page in a named isolated browser context — its own cookie jar "
            "and storage, comparable to a separate incognito window. Pages given the "
            "same name share that context; different names are fully isolated from "
            "each other. Use it to hold several logins to one site at once. Empty "
            "string uses the normal shared context."
        )),
    ] = "",
    timeout: TimeoutMs = 0,
    device: DevicePreset = "",
) -> str:
    """Open a new browser tab and load a URL.

    Chrome always starts with one tab, so the first call here reuses that empty
    startup tab rather than leaving a stray blank page behind for the rest of
    the session. A blank tab sitting among others is left alone, and an isolated
    context always gets a target of its own.

    Unless `background` is set, the new page becomes the selected page for every
    subsequent tool call.

    Passing `device` applies emulation before the first real request, so mobile
    signals are present from the very first byte — which calling emulate_device
    afterwards cannot achieve.
    """
    global _selected_target_id
    if device and _resolve_device_preset(device) is None:
        supported = ", ".join(sorted(_DEVICE_PRESETS))
        return f"Error: Unknown device preset '{device}'. Supported presets: {supported}"

    browser = await _get_browser()
    previous_tab = await _active_tab()
    initial_url = "about:blank" if device and url != "about:blank" else url

    # Chrome always comes up with one tab. Opening a second one on top of that
    # empty startup tab leaves a stray blank page behind for the rest of the
    # session, so reuse it instead. Only when it is the sole tab and genuinely
    # empty — never silently navigate away from a page that has content.
    # An isolated context needs its own target, so it is excluded.
    reuse_tab = None
    if not isolated_context and len(browser.tabs) == 1:
        only_tab = browser.tabs[0]
        if only_tab.target and _is_blank_url(only_tab.target.url):
            reuse_tab = only_tab

    try:
        if reuse_tab is not None:
            tab = reuse_tab
            if not _is_blank_url(initial_url):
                await _await_with_timeout(
                    tab.get(initial_url), timeout, f"Navigate to {initial_url}"
                )
                await _await_with_timeout(tab, timeout, "Wait for page")
        else:
            tab = await _open_new_tab(
                browser,
                url=initial_url,
                background=background,
                isolated_context=isolated_context,
                timeout=timeout,
            )

        # Auto-enable network collection on new tab.
        await _auto_enable_network_collection(tab)

        device_results: list[str] = []
        if device:
            device_results = await _apply_device_preset(tab, device)

        if url != initial_url:
            await _await_with_timeout(tab.get(url), timeout, f"Navigate new page to {url}")
            await _await_with_timeout(tab, timeout, "Wait for new page navigation")

        if background and previous_tab != tab:
            await previous_tab.activate()
        elif not background and tab.target:
            # Foreground new page becomes the selected context for later tools.
            _selected_target_id = str(tab.target.target_id)

        pages = await _format_pages()
        suffix = f" (pre-navigation emulation: {', '.join(device_results)})" if device_results else ""
        verb = "Reused the empty startup tab for" if reuse_tab is not None else "Opened new page:"
        return f"{verb} {tab.target.url or 'about:blank'}{suffix}{pages}"
    except Exception as e:
        return f"Error opening new page: {e}"


@tool(title="Start performance trace")
async def performance_start_trace(
    reload: Annotated[
        bool,
        Field(description=(
            "Reload the page after starting the trace so the recording covers page "
            "load. Default true, which is what you want for load performance; set "
            "false to profile the page as it currently stands."
        )),
    ] = True,
    auto_stop: Annotated[
        bool,
        Field(description=(
            "Record for about 5 seconds, then stop and return the result in this same "
            "call. Default true. Set false to control the window yourself and end it "
            "with performance_stop_trace."
        )),
    ] = True,
    file_path: Annotated[
        str,
        Field(description=(
            "Write the raw trace JSON to this local path. Omit to get only the "
            "collected event count back."
        )),
    ] = "",
) -> str:
    """Record a Chrome performance trace of page load or interaction.

    Captures the same DevTools timeline categories the Performance panel uses:
    rendering, scripting, loading, screenshots and the V8 CPU profile.

    With the defaults (reload + auto_stop) this is one self-contained call that
    reloads, records for ~5s and returns. Only one trace can run at a time.

    Pass file_path to keep the data — without it you get just an event count,
    which confirms the trace ran but says nothing about what it measured. The
    output is raw trace JSON: load it via DevTools -> Performance -> Load profile.
    """
    global _tracing_active
    tab = await _active_tab()
    import nodriver.cdp.tracing as cdp_tracing

    if _tracing_active:
        return "Error: A trace is already running. Stop it first."

    categories = [
        "-*", "blink.console", "blink.user_timing", "devtools.timeline",
        "disabled-by-default-devtools.screenshot",
        "disabled-by-default-devtools.timeline",
        "disabled-by-default-devtools.timeline.frame",
        "disabled-by-default-devtools.timeline.stack",
        "disabled-by-default-v8.cpu_profiler",
        "latencyInfo", "loading", "v8.execute", "v8",
    ]
    # ReportEvents, not ReturnAsStream: stop_trace collects the trace from
    # Tracing.dataCollected events, and ReturnAsStream never emits those — it
    # hands back a stream handle instead, so every trace came back empty.
    await tab.send(cdp_tracing.start(categories=",".join(categories), transfer_mode="ReportEvents"))
    _tracing_active = True

    if reload:
        await tab.reload()

    if auto_stop:
        await tab.sleep(5)
        return await performance_stop_trace(file_path=file_path)

    return "Trace started. Use performance_stop_trace to stop."


@tool(title="Stop performance trace")
async def performance_stop_trace(
    file_path: Annotated[
        str,
        Field(description=(
            'Write the raw trace JSON to this local path (e.g. "trace.json"), loadable '
            "in DevTools -> Performance. Omit to get only the event count."
        )),
    ] = "",
) -> str:
    """Stop the running performance trace and collect its data.

    Only needed when performance_start_trace was called with auto_stop=false —
    otherwise the trace has already ended and this returns an error.

    Waits up to 30 seconds for Chrome to flush its buffered events.
    """
    global _tracing_active
    tab = await _active_tab()
    import nodriver.cdp.tracing as cdp_tracing

    if not _tracing_active:
        return "Error: No trace is running."

    trace_chunks = []

    async def on_data(event: cdp_tracing.DataCollected):
        trace_chunks.extend(event.value)

    tab.add_handler(cdp_tracing.DataCollected, on_data)

    done_event = asyncio.Event()

    async def on_complete(event: cdp_tracing.TracingComplete):
        done_event.set()

    tab.add_handler(cdp_tracing.TracingComplete, on_complete)
    await tab.send(cdp_tracing.end())

    try:
        await asyncio.wait_for(done_event.wait(), timeout=30)
    except asyncio.TimeoutError:
        pass

    _tracing_active = False
    tab.remove_handler(cdp_tracing.DataCollected, on_data)
    tab.remove_handler(cdp_tracing.TracingComplete, on_complete)

    result = f"Trace stopped. {len(trace_chunks)} events collected."

    if file_path:
        if trace_chunks:
            with open(file_path, "w") as f:
                json.dump(trace_chunks, f)
            result += f" Saved to {file_path}"
        else:
            # Silently not writing the file the caller asked for reads as success.
            result += f" Nothing was written to {file_path} — the trace was empty."

    return result


@tool(title="Press key", open_world=True)
async def press_key(
    key: Annotated[
        str,
        Field(description=(
            'A key or chord, e.g. "Enter", "Tab", "Escape", "ArrowDown", "a", '
            '"Control+A" or "Control+Shift+R". Modifiers are Control, Shift, Alt and '
            "Meta, joined with +. Named keys: Enter, Tab, Backspace, Delete, Escape, "
            "ArrowUp, ArrowDown, ArrowLeft, ArrowRight, Home, End, PageUp, PageDown, "
            "Space. Any single character works as itself."
        )),
    ],
    include_snapshot: IncludeSnapshot = False,
) -> str:
    """Send a key press or keyboard shortcut to the page.

    Goes to whatever currently holds focus, so click or fill the target element
    first — with nothing focused the key lands on the document body.

    Modifiers are held around the main key with the proper modifier bitmask, so
    real chords such as Control+A or Control+Shift+R register as shortcuts
    instead of arriving as unrelated key presses.

    For entering text use fill (replaces the field) or type_text (appends to it);
    this tool is for single keys and shortcuts.
    """
    tab = await _active_tab()
    import nodriver.cdp.input_ as cdp_input

    parts = key.split("+")
    target_key = parts[-1]
    modifier_names = [p for p in parts[:-1] if p in _MODIFIER_KEYS]

    modifiers = 0
    for m in modifier_names:
        modifiers |= _MODIFIER_BITS[m]

    ki = _key_descriptor(target_key)

    # Press each modifier down, accumulating the active bitmask.
    held = 0
    for m in modifier_names:
        held |= _MODIFIER_BITS[m]
        await tab.send(cdp_input.dispatch_key_event(
            type_="keyDown", key=m, modifiers=held,
            windows_virtual_key_code=_MODIFIER_VK.get(m, 0),
        ))

    # Press the target key WITH the modifier mask applied, so shortcuts such as
    # Control+A / Control+Shift+R actually register (a bare Control keyDown alone
    # does not make the browser treat the next key as a chord).
    down: dict[str, Any] = {"type_": "keyDown", "key": ki["key"], "modifiers": modifiers}
    up: dict[str, Any] = {"type_": "keyUp", "key": ki["key"], "modifiers": modifiers}
    if ki.get("code"):
        down["code"] = up["code"] = ki["code"]
    if ki.get("vk"):
        down["windows_virtual_key_code"] = up["windows_virtual_key_code"] = ki["vk"]
    # Emit text only for a bare printable key (no modifiers other than Shift).
    if ki.get("text") and not (modifiers & ~_MODIFIER_BITS["Shift"]):
        down["text"] = ki["text"]
    await tab.send(cdp_input.dispatch_key_event(**down))
    await tab.send(cdp_input.dispatch_key_event(**up))

    # Release modifiers in reverse order.
    for m in reversed(modifier_names):
        held &= ~_MODIFIER_BITS[m]
        await tab.send(cdp_input.dispatch_key_event(
            type_="keyUp", key=m, modifiers=held,
            windows_virtual_key_code=_MODIFIER_VK.get(m, 0),
        ))

    result = f"Pressed {key}"
    result += await _maybe_snapshot(include_snapshot)
    return result


@tool(title="Resize page", idempotent=True)
async def resize_page(
    width: Annotated[int, Field(gt=0, description="Target page width in CSS pixels.")],
    height: Annotated[int, Field(gt=0, description="Target page height in CSS pixels.")],
) -> str:
    """Resize the browser window so the page gets the given dimensions.

    Moves the actual OS window, which is what you want for responsive-layout
    checks that should also come out right in a screenshot.

    To emulate a viewport size without touching the window — including device
    pixel ratio, the mobile flag and touch — use emulate's `viewport` parameter
    or emulate_device. Those are what a site's media queries and fingerprinting
    read as a genuine device change.
    """
    tab = await _active_tab()
    await tab.set_window_size(width=width, height=height)
    return f"Resized to {width}x{height}."


@tool(title="Scroll page", open_world=True)
async def scroll_page(
    direction: Annotated[Literal["up", "down"], Field(description="Direction to scroll.")] = "down",
    amount: Annotated[
        int,
        Field(gt=0, description=(
            "How far to scroll, as a percentage of the viewport height — 25 is a "
            "quarter screen, 100 a full screen."
        )),
    ] = 50,
) -> str:
    """Scroll the page up or down by a percentage of the viewport.

    The way to trigger lazy-loaded content and infinite scroll; take a fresh
    take_snapshot afterwards to see what was added.

    To bring one known element into view, scroll_to_selector is more precise.
    `click` already scrolls to its target, so no scrolling is needed before it.
    """
    tab = await _active_tab()
    if direction == "down":
        await tab.scroll_down(amount)
    else:
        await tab.scroll_up(amount)
    return f"Scrolled {direction} {amount}%."


@tool(title="Select page", idempotent=True)
async def select_page(
    page_id: Annotated[
        int, Field(ge=0, description="Index of the page to select, as listed by list_pages.")
    ],
    bring_to_front: Annotated[
        bool,
        Field(description=(
            "Also focus the tab in the real browser window. Default true. Many pages "
            "pause animations, timers and media while backgrounded, so leave this on "
            "unless you specifically want the page to stay hidden."
        )),
    ] = True,
) -> str:
    """Choose which open tab every subsequent tool call acts on.

    Without a selection, tools act on the most recently opened tab. Selecting
    makes that choice explicit and sticky — needed when a click opened a tab you
    now want to drive, or when working across several sites at once.

    Indices come from list_pages and shift as tabs open and close, so the
    response lists them again. If the selected tab is later closed, the
    selection is dropped and the default applies again.
    """
    global _selected_target_id
    browser = await _get_browser()
    if page_id < 0 or page_id >= len(browser.tabs):
        return f"Error: Invalid page_id {page_id}, have {len(browser.tabs)} tabs."
    tab = browser.tabs[page_id]
    _selected_target_id = str(tab.target.target_id) if tab.target else None
    if bring_to_front:
        await tab.activate()
    await tab
    pages = await _format_pages()
    return f"Selected page [{page_id}]: {tab.target.url}{pages}"


@tool(title="Set cookie", idempotent=True)
async def set_cookie(
    name: Annotated[str, Field(description="Cookie name.")],
    value: Annotated[str, Field(description="Cookie value.")],
    domain: Annotated[
        str,
        Field(description=(
            'Domain the cookie belongs to, e.g. "example.com". A leading dot '
            '(".example.com") makes it apply to subdomains as well.'
        )),
    ],
    path: Annotated[
        str,
        Field(description='URL path the cookie is scoped to. "/" covers the whole site.'),
    ] = "/",
    secure: Annotated[
        bool,
        Field(description=(
            "Send the cookie over HTTPS only. Browsers require this for any cookie "
            "with SameSite=None."
        )),
    ] = False,
) -> str:
    """Set a single browser cookie.

    Useful for injecting a known session token, consent flag or A/B bucket
    without walking through a login flow.

    This creates a session cookie: no expiry, gone when the browser closes. To
    restore a full cookie set including expiry and SameSite, use load_session.
    Cookies are browser-wide, not per tab.
    """
    tab = await _active_tab()
    import nodriver.cdp.network as cdp_net
    success = await tab.send(cdp_net.set_cookie(
        name=name, value=value, domain=domain, path=path, secure=secure,
    ))
    return f"Cookie '{name}' set." if success else f"Failed to set cookie '{name}'."


@tool(title="Set localStorage", idempotent=True)
async def set_local_storage(
    items: Annotated[
        dict[str, str],
        Field(description=(
            'Key-value pairs to write, e.g. {"token": "abc", "locale": "de"}. Values '
            "must be strings — JSON-encode anything structured yourself."
        )),
    ],
) -> str:
    """Write entries into the current page's localStorage.

    Merges into what is already there rather than clearing it. Handy for setting
    feature flags, consent state or auth tokens before the page's own scripts
    read them.

    localStorage is scoped per origin, so navigate to the site first — writing
    on about:blank goes nowhere. Most pages read these values only at startup,
    so reload after setting them.
    """
    tab = await _active_tab()
    await tab.set_local_storage(items)
    return f"Set {len(items)} localStorage items."


@tool(title="Take heap snapshot")
async def take_memory_snapshot(
    file_path: Annotated[
        str,
        Field(description=(
            "Local path to write the .heapsnapshot file to. Open it in DevTools -> "
            "Memory -> Load profile."
        )),
    ],
) -> str:
    """Capture a V8 heap snapshot of the page, for memory-leak debugging.

    Writes the raw .heapsnapshot file for Chrome DevTools -> Memory -> Load. The
    usual workflow is two snapshots taken around a suspect interaction, then
    comparing retained objects between them.

    On a heavy page a snapshot can be hundreds of megabytes and take several
    seconds; the response reports the resulting file size.
    """
    tab = await _active_tab()
    import nodriver.cdp.heap_profiler as cdp_heap

    chunks = []

    async def on_chunk(event: cdp_heap.AddHeapSnapshotChunk):
        chunks.append(event.chunk)

    tab.add_handler(cdp_heap.AddHeapSnapshotChunk, on_chunk)
    await tab.send(cdp_heap.take_heap_snapshot(report_progress=False))
    tab.remove_handler(cdp_heap.AddHeapSnapshotChunk, on_chunk)

    data = "".join(chunks)
    with open(file_path, "w") as f:
        f.write(data)

    size_mb = round(len(data) / 1024 / 1024, 2)
    return f"Heap snapshot saved to {file_path} ({size_mb} MB)."


@tool(title="Take screenshot", read_only=True)
async def take_screenshot(
    full_page: Annotated[
        bool,
        Field(description=(
            "Capture the whole scrollable page rather than just the visible viewport. "
            "Cannot be combined with `uid`."
        )),
    ] = False,
    format: Annotated[
        Literal["png", "jpeg", "webp"],
        Field(description=(
            'Image format. "png" is lossless and the default; "jpeg" or "webp" with a '
            "quality setting are far smaller, which matters for full-page captures."
        )),
    ] = "png",
    quality: Annotated[
        int,
        Field(ge=0, le=100, description=(
            "Compression quality from 1 to 100, for jpeg and webp only. Ignored for "
            "png. 0 leaves Chrome's default."
        )),
    ] = 0,
    uid: Annotated[
        str,
        Field(description=(
            "Capture just this element, using a uid from the most recent take_snapshot. "
            "Cannot be combined with full_page. Empty string captures the page."
        )),
    ] = "",
    file_path: Annotated[
        str,
        Field(description=(
            "Write the image to this local path. Omit to get a base64 data URL inline "
            "in the response — that is expensive, so prefer a file for full-page or "
            "high-DPR captures."
        )),
    ] = "",
) -> str:
    """Capture the page, the viewport or a single element as an image.

    Do NOT use this to read a page. take_snapshot gives you the same content as
    searchable text, is dramatically smaller, and yields the uids every
    interaction tool needs. Use a screenshot only when you genuinely need
    pixels: layout and styling checks, visual regression, or text that exists
    only inside an image.

    Element capture needs a uid from the current snapshot. Without file_path the
    image is returned inline as a base64 data URL.
    """
    tab = await _active_tab()
    import nodriver.cdp.page as cdp_page

    if uid and full_page:
        return "Error: Cannot use both uid and full_page together."

    clip = None
    if uid:
        try:
            import nodriver.cdp.dom as cdp_dom
            backend_node_id = _uid_to_backend_node_id.get(uid)
            if backend_node_id is None:
                return f"Error: Unknown uid '{uid}'. Take a new snapshot first."
            model = await tab.send(cdp_dom.get_box_model(
                backend_node_id=cdp_dom.BackendNodeId(backend_node_id)
            ))
            quad = model.content
            x = min(quad[0], quad[2], quad[4], quad[6])
            y = min(quad[1], quad[3], quad[5], quad[7])
            w = max(quad[0], quad[2], quad[4], quad[6]) - x
            h = max(quad[1], quad[3], quad[5], quad[7]) - y
            clip = cdp_page.Viewport(x=x, y=y, width=w, height=h, scale=1)
        except Exception as e:
            return f"Error getting element bounds for uid={uid}: {e}"

    kwargs = {"format_": format, "capture_beyond_viewport": full_page}
    if quality and format in ("jpeg", "webp"):
        kwargs["quality"] = quality
    if clip:
        kwargs["clip"] = clip

    result = await tab.send(cdp_page.capture_screenshot(**kwargs))

    if file_path:
        with open(file_path, "wb") as f:
            f.write(base64.b64decode(result))
        return f"Screenshot saved to {file_path}."
    else:
        return f"data:image/{format};base64,{result}"


@tool(title="Take page snapshot", read_only=True)
async def take_snapshot(
    verbose: Annotated[
        bool,
        Field(description=(
            "Return the complete accessibility tree, including nodes normally filtered "
            "out as noise and containers that are otherwise collapsed. Much larger — "
            "use it only when an element you need is missing from the default output."
        )),
    ] = False,
    file_path: Annotated[
        str,
        Field(description=(
            "Write the snapshot to this local path instead of returning it — useful "
            "for very large pages you intend to search rather than read in full."
        )),
    ] = "",
) -> str:
    """Read the page as compact text, with a uid for every element.

    This is the primary way to see a page, and the source of the uids that
    click, fill, hover, drag and upload_file all take. Prefer it over
    take_screenshot for anything but a genuine visual check: it is searchable,
    far smaller, and it is what makes interaction possible at all.

    The output is the accessibility tree — roles, names, values and states,
    indented by nesting — with Chrome-internal and purely presentational nodes
    filtered out unless `verbose` is set.

    uids stay stable across snapshots for elements that did not change, but any
    page change can invalidate them. Whenever a tool reports "unknown uid", take
    a fresh snapshot and use the new uid. Output is capped at 200 000 characters.

    For plain page text without uids, get_page_content is cheaper; to find
    elements by CSS selector, use query_selector.
    """
    global _snapshot_id
    tab = await _active_tab()
    import nodriver.cdp.accessibility as cdp_a11y

    nodes = await tab.send(cdp_a11y.get_full_ax_tree())

    # Build a lookup: node_id -> AXNode
    node_map: dict[str, Any] = {}
    for node in nodes:
        node_map[node.node_id] = node

    # Build tree structure
    children_map: dict[str, list[str]] = {}
    root_ids: list[str] = []
    nodes_with_parent: set[str] = set()
    for node in nodes:
        if node.child_ids:
            children_map[node.node_id] = list(node.child_ids)
            for cid in node.child_ids:
                nodes_with_parent.add(cid)

    for node in nodes:
        if node.node_id not in nodes_with_parent:
            root_ids.append(node.node_id)

    # --- Stable uid assignment (mirrors chrome-devtools-mcp) ---
    _snapshot_id += 1
    id_counter = 0
    uid_map: dict[str, str] = {}
    seen_unique_ids: set[str] = set()
    new_uid_to_backend: dict[str, int] = {}

    for node in nodes:
        frame_id = str(node.frame_id) if node.frame_id else ""
        backend_id = str(node.backend_dom_node_id) if node.backend_dom_node_id else ""
        unique_id = f"{frame_id}_{backend_id}"

        if unique_id != "_" and unique_id in _unique_id_to_mcp_id:
            uid_map[node.node_id] = _unique_id_to_mcp_id[unique_id]
        else:
            new_uid = f"{_snapshot_id}_{id_counter}"
            id_counter += 1
            uid_map[node.node_id] = new_uid
            if unique_id != "_":
                _unique_id_to_mcp_id[unique_id] = new_uid

        if unique_id != "_":
            seen_unique_ids.add(unique_id)

        # Record uid -> backend_node_id mapping for element resolution
        if node.backend_dom_node_id:
            assigned_uid = uid_map[node.node_id]
            new_uid_to_backend[assigned_uid] = int(node.backend_dom_node_id)

    # Clean up stale mappings
    stale_keys = [k for k in _unique_id_to_mcp_id if k not in seen_unique_ids]
    for k in stale_keys:
        del _unique_id_to_mcp_id[k]

    # Update global uid -> backend_node_id mapping
    _uid_to_backend_node_id.clear()
    _uid_to_backend_node_id.update(new_uid_to_backend)

    def _text_leaf(node_id: str) -> str | None:
        """The text of a StaticText node, or None if this is not one.

        StaticText always carries InlineTextBox children, which are rendered
        away as Chrome internals. Treating it as a leaf only when `child_ids` is
        empty therefore never matches anything.
        """
        node = node_map.get(node_id)
        if node is None:
            return None
        role = str(node.role.value) if node.role and node.role.value else ""
        if role != "StaticText":
            return None
        for cid in node.child_ids or []:
            kid = node_map.get(cid)
            kid_role = str(kid.role.value) if kid is not None and kid.role and kid.role.value else ""
            if kid_role not in _SKIP_ROLES:
                return None
        return str(node.name.value) if node.name and node.name.value else ""

    def _repeats_parent_name(node_id: str, parent_name: str) -> bool:
        """Whether a StaticText child only echoes its parent's accessible name.

        A link's name is computed from exactly these children, so printing them
        again says nothing new. On a link-dense page they are a large share of
        the snapshot, paid for on every single call.
        """
        if not parent_name:
            return False
        text = _text_leaf(node_id)
        return bool(text) and text in parent_name

    def _format_node(node_id: str, depth: int) -> str:
        node = node_map.get(node_id)
        if node is None:
            return ""

        role = ""
        if node.role and node.role.value:
            role = str(node.role.value)

        # Skip ignored nodes in non-verbose mode (promote children)
        if not verbose and node.ignored:
            child_parts = []
            for cid in children_map.get(node_id, []):
                child_parts.append(_format_node(cid, depth))
            return "".join(child_parts)

        # Skip roles entirely (node + descendants) — Chrome internals
        if not verbose and role in _SKIP_ROLES:
            return ""

        # Collapse container roles (skip node, promote children at same depth).
        # A focusable one is kept even without a name: a contenteditable div has
        # role "generic", and collapsing it left only its text node behind, which
        # is not something fill or click can act on.
        if not verbose and role in _COLLAPSE_ROLES:
            name = ""
            if node.name and node.name.value:
                name = str(node.name.value)
            focusable = False
            for prop in node.properties or []:
                pname = prop.name.value if hasattr(prop.name, "value") else str(prop.name)
                if str(pname) == "focusable" and prop.value and prop.value.value:
                    focusable = True
                    break
            if not name and not focusable:
                child_parts = []
                for cid in children_map.get(node_id, []):
                    child_parts.append(_format_node(cid, depth))
                return "".join(child_parts)

        name = ""
        if node.name and node.name.value:
            name = str(node.name.value)

        # A node with no name of its own, holding nothing but text, costs two
        # lines to convey one thing. Fold the text into this line instead. This
        # is lossless: the same characters, one line fewer, and it is where most
        # of a table-heavy page's bulk sits.
        merged_children: set[str] = set()
        if not verbose and not name:
            kids = children_map.get(node_id, [])
            texts: list[str] = []
            for cid in kids:
                kid_text = _text_leaf(cid)
                if kid_text is None:
                    texts = []
                    break
                if kid_text:
                    texts.append(kid_text)
            if texts:
                name = " ".join(texts)
                merged_children = set(kids)

        value = ""
        if node.value and node.value.value:
            value = str(node.value.value)

        # option special handling (same as chrome-devtools-mcp)
        if role == "option" and name and not value:
            value = name

        # --- Collect properties (matching Puppeteer's exposed set) ---
        props: list[str] = []
        if node.properties:
            for prop in node.properties:
                pname = prop.name.value if hasattr(prop.name, "value") else str(prop.name)
                pval = prop.value.value if prop.value and prop.value.value is not None else None

                if pname in _EXCLUDED_PROPERTIES or pname in _SUPPRESS_PROPERTIES:
                    continue

                if pval is False or pval == "false":
                    continue

                mapped = _BOOL_PROPERTY_MAP.get(pname)
                if mapped and (pval is True or pval == "true"):
                    props.append(mapped)

                if pval is True or pval == "true":
                    props.append(pname)
                elif isinstance(pval, (str, int, float)) and pval != "":
                    props.append(f'{pname}="{pval}"')

        uid = uid_map.get(node_id, "?")
        indent = "  " * depth
        parts = [f"uid={uid}"]
        if role and role != "none":
            parts.append(role)
        elif role == "none" and verbose:
            parts.append("ignored")
        if name:
            parts.append(f'"{name}"')
        if value and value != name:
            parts.append(f'value="{value}"')
        parts.extend(props)

        line = f"{indent}{' '.join(parts)}\n"

        child_lines = []
        for cid in children_map.get(node_id, []):
            if cid in merged_children:
                continue
            if not verbose and _repeats_parent_name(cid, name):
                continue
            child_lines.append(_format_node(cid, depth + 1))

        return line + "".join(child_lines)

    output_parts = []
    for rid in root_ids:
        output_parts.append(_format_node(rid, 0))
    snapshot_text = "".join(output_parts)

    # file_path is the escape hatch for a page too large to put in the
    # conversation, so capping what gets written would leave no way to read a
    # big page at all. Only the inline path is capped.
    if file_path:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(snapshot_text)
        return f"Snapshot saved to {file_path} ({len(snapshot_text)} chars)."

    if len(snapshot_text) > 200_000:
        snapshot_text = (
            snapshot_text[:200_000]
            + f"\n... (truncated at 200000 of {len(snapshot_text)} chars"
            " — pass file_path to get the whole tree)"
        )

    return snapshot_text


@tool(title="Type text", open_world=True)
async def type_text(
    text: Annotated[str, Field(description="The text to type, one character at a time.")],
    submit_key: Annotated[
        str,
        Field(description=(
            'Key to press once typing finishes, e.g. "Enter" to submit a search box or '
            '"Tab" to move to the next field. Empty string presses nothing.'
        )),
    ] = "",
) -> str:
    """Type text into whatever element currently has focus.

    Appends at the caret instead of replacing, and needs something focused
    already — click the field first, or use fill, which takes a uid, clears the
    field and needs no separate focus step.

    Prefer fill for ordinary form filling. Use type_text when you must add to
    existing content, or for widgets that only react to raw key events such as
    contenteditable, rich-text and canvas editors.
    """
    tab = await _active_tab()
    import nodriver.cdp.input_ as cdp_input
    for char in text:
        await tab.send(cdp_input.dispatch_key_event(type_="keyDown", text=char))
        await tab.send(cdp_input.dispatch_key_event(type_="keyUp", text=char))

    if submit_key:
        ki = _key_descriptor(submit_key)
        down: dict[str, Any] = {"type_": "keyDown", "key": ki["key"]}
        up: dict[str, Any] = {"type_": "keyUp", "key": ki["key"]}
        if ki.get("code"):
            down["code"] = up["code"] = ki["code"]
        if ki.get("vk"):
            down["windows_virtual_key_code"] = up["windows_virtual_key_code"] = ki["vk"]
        if ki.get("text"):
            down["text"] = ki["text"]
        await tab.send(cdp_input.dispatch_key_event(**down))
        await tab.send(cdp_input.dispatch_key_event(**up))

    result = f"Typed {len(text)} characters"
    if submit_key:
        result += f", then pressed {submit_key}"
    return result


@tool(title="Upload file", open_world=True)
async def upload_file(
    uid: Annotated[
        str,
        Field(description=(
            'uid of the file input, or of the button, label or drop zone in front '
            "of it — the real <input type=\"file\"> is resolved from there. Comes "
            "from the most recent take_snapshot."
        )),
    ],
    file_path: Annotated[
        str,
        Field(description="Absolute path to a local file on the machine running this server."),
    ],
    include_snapshot: IncludeSnapshot = False,
) -> str:
    """Attach a local file to a file input on the page.

    Sets the input's files directly over CDP, so no OS file-picker dialog ever
    opens — clicking an upload button normally would open one, and that blocks
    every further tool call until a human dismisses it.

    Chrome renders a file input with an internal shadow button, and that is what
    the accessibility tree exposes, so the uid you can see is usually not the
    input itself. This resolves to the real input: the element, one inside it,
    the input a label controls, or one just above in the tree.

    The result is read back. If the page's own uploader consumed the file
    straight away, the response says so, which is a successful upload rather
    than an empty input.
    """
    tab = await _active_tab()
    import nodriver.cdp.dom as cdp_dom

    if uid not in _uid_to_backend_node_id:
        return f"Error: Unknown uid '{uid}'. Take a new snapshot first."
    if not os.path.isfile(file_path):
        return f"Error: no such file: {file_path}"

    try:
        remote_obj = await _resolve_uid(tab, uid)
    except ValueError as e:
        return f"Error: {e}"

    # Chrome renders <input type=file> with an internal shadow button, and that
    # is what the accessibility tree exposes. Addressing the uid directly makes
    # setFileInputFiles a silent no-op, so resolve to the real input first.
    located = await _call_function_on(
        tab,
        function_declaration=(
            "function() {"
            " const isFile = e => e && e.tagName === 'INPUT' && e.type === 'file';"
            " if (isFile(this)) return this;"
            " const own = this.querySelector && this.querySelector('input[type=file]');"
            " if (own) return own;"
            " if (this.control && isFile(this.control)) return this.control;"
            " const label = this.closest && this.closest('label');"
            " if (label && isFile(label.control)) return label.control;"
            " let n = this;"
            " for (let i = 0; i < 4 && n; i++) {"
            "   const found = n.querySelector && n.querySelector('input[type=file]');"
            "   if (found) return found;"
            "   n = n.parentElement;"
            " }"
            " return null; }"
        ),
        object_id=remote_obj.object_id,
    )
    if located is None or not getattr(located, "object_id", None):
        return (
            f"Error: uid={uid} is not a file input and none was found near it. "
            'Locate it with query_selector("input[type=file]") and use that element.'
        )

    try:
        await tab.send(cdp_dom.set_file_input_files(
            files=[file_path],
            object_id=located.object_id,
        ))
    except Exception as e:
        return f"Error attaching {file_path}: {e}"

    # Read it back: the page's own JS may consume and reset the input, which is
    # a successful upload, but an empty input plus no reaction is not.
    check = await _call_function_on(
        tab,
        function_declaration=(
            "function() { return {n: this.files ? this.files.length : -1, "
            "name: this.files && this.files[0] ? this.files[0].name : null}; }"
        ),
        object_id=located.object_id,
        return_by_value=True,
    )
    attached = (check.value or {}) if check else {}
    count = attached.get("n", -1)

    result = f"Uploaded {file_path} to uid={uid}"
    if count == 0:
        result += " (the page consumed the file immediately, which is normal for uploaders that submit on change)"
    elif count > 0:
        result += f" — attached as {attached.get('name')}"
    result += await _maybe_snapshot(include_snapshot)
    return result


@tool(title="Wait for text", read_only=True, open_world=True)
async def wait_for(
    text: Annotated[
        list[str],
        Field(min_length=1, description=(
            "Texts to wait for — the wait ends as soon as any one of them appears. "
            'Passing both outcomes, e.g. ["Welcome back", "Login failed"], lets one '
            "call tell you which happened. Matching is a case-sensitive substring "
            "test against the page's visible text."
        )),
    ],
    timeout: Annotated[
        int, Field(gt=0, description="Maximum wait in milliseconds before giving up.")
    ] = 30000,
) -> str:
    """Wait until one of several texts appears on the page, then snapshot it.

    The right way to wait after an action that starts loading — far more
    reliable than guessing a delay, and it returns the moment the text shows up
    instead of always burning the whole timeout.

    On success the page snapshot is included in the response, so no separate
    take_snapshot call is needed.

    Polls the visible text twice a second. To wait for an element rather than
    for wording, use wait_for_selector.
    """
    tab = await _active_tab()
    timeout_s = timeout / 1000

    start = time.time()
    last_error = ""
    while time.time() - start < timeout_s:
        try:
            # Get page text content
            page_text = await _evaluate_value(
                tab, "(document.body || document.documentElement || {}).innerText || ''"
            )
            if page_text:
                for t in text:
                    if t in page_text:
                        # Found — return with snapshot
                        snapshot = await take_snapshot()
                        return f"Found text \"{t}\" on page.\n\n{snapshot}"
        except Exception as e:
            # A crashed renderer, a detached context or an open JS dialog are all
            # very different from "not there yet"; remember why so the timeout can
            # say which it was instead of blaming the wait.
            last_error = str(e)
        await asyncio.sleep(0.5)

    msg = f"Timeout: None of the texts {text} appeared within {timeout}ms."
    if last_error:
        msg += f" The page could not be read while waiting: {last_error}"
    return msg


# ---------------------------------------------------------------------------
# Session management helpers
# ---------------------------------------------------------------------------

_SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".nodriver-mcp", "sessions")


def _ensure_sessions_dir() -> str:
    os.makedirs(_SESSIONS_DIR, exist_ok=True)
    return _SESSIONS_DIR


@tool(title="Save session")
async def save_session(
    name: Annotated[
        str,
        Field(description=(
            'A human-readable name for this session, e.g. "github-logged-in". It '
            "forms the filename, with a timestamp appended so repeated saves never "
            "overwrite one another."
        )),
    ],
) -> str:
    """Save cookies, localStorage and open page URLs to a reusable file.

    This is how you keep a login obtained interactively, so a later run can skip
    the login flow entirely — restore it with load_session.

    Stored as JSON under ~/.nodriver-mcp/sessions/. That file holds live session
    tokens in plain text, so treat it as a credential.

    Only the current page's origin contributes localStorage. For a login meant
    to survive without an explicit restore step, a persistent profile
    (create_profile + use_profile) is the sturdier option.
    """
    tab = await _active_tab()
    browser = await _get_browser()
    import nodriver.cdp.storage as cdp_storage

    # 1. Collect all cookies. Storage.getCookies, not Network.getCookies: the
    # latter only answers for the tab it is sent to, so a session saved with
    # several logins open kept whichever site was selected and dropped the rest.
    raw_cookies = await tab.send(cdp_storage.get_cookies())
    cookies = []
    for c in raw_cookies:
        cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "secure": c.secure,
            "httpOnly": c.http_only,
            "sameSite": c.same_site.value if c.same_site else None,
            "expires": c.expires if c.expires else None,
        })

    # 2. Collect localStorage for the current page
    local_storage = {}
    try:
        ls_data = await tab.get_local_storage()
        if ls_data:
            local_storage = {k: v for k, v in ls_data.items()}
    except Exception:
        pass

    # 3. Collect open page URLs
    pages = [t.target.url for t in browser.tabs if t.target and t.target.url]

    # 4. Build session object
    session = {
        "name": name,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "current_url": tab.target.url if tab.target else "",
        "pages": pages,
        "cookies": cookies,
        "localStorage": local_storage,
    }

    # 5. Write to file
    _ensure_sessions_dir()
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_name}_{ts}.json"
    filepath = os.path.join(_SESSIONS_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)

    return (
        f"Session '{name}' saved to {filepath}\n"
        f"  Cookies: {len(cookies)}\n"
        f"  localStorage items: {len(local_storage)}\n"
        f"  Open pages: {len(pages)}"
    )


@tool(title="Load session")
async def load_session(
    filename: Annotated[
        str,
        Field(description=(
            "Session filename as shown by list_sessions, or an absolute path to a "
            "session JSON file."
        )),
    ],
    restore_pages: Annotated[
        bool,
        Field(description=(
            "Also re-open the tabs that were open when the session was saved. Default "
            "false, which restores only cookies and localStorage."
        )),
    ] = False,
) -> str:
    """Restore cookies and localStorage from a saved session file.

    Use it at the start of a run to arrive already logged in. The page is first
    navigated to the saved origin so localStorage lands where it belongs, then
    reloaded so the restored cookies take effect.

    Cookies that no longer apply are skipped rather than failing the whole
    restore, and the response reports how many were actually restored. Expired
    tokens still leave you logged out, so check the page afterwards.
    """
    # Resolve file path
    if os.path.isabs(filename):
        filepath = filename
    else:
        filepath = os.path.join(_SESSIONS_DIR, filename)

    if not os.path.exists(filepath):
        return f"Session file not found: {filepath}"

    with open(filepath, "r", encoding="utf-8") as f:
        session = json.load(f)

    tab = await _active_tab()
    browser = await _get_browser()
    import nodriver.cdp.network as cdp_net

    # 1. Restore cookies.
    #
    # Every field goes through CookieParam.from_json rather than hand-built
    # kwargs: cdp_net.set_cookie(expires=...) calls expires.to_json(), so a raw
    # JSON float raised AttributeError for every single cookie — and because the
    # counter only advanced on success, the failure surfaced as a cheerful
    # "Cookies restored: 0" instead of an error.
    import nodriver.cdp.storage as cdp_storage

    saved_cookies = session.get("cookies", [])
    params: list[Any] = []
    for c in saved_cookies:
        entry = dict(c)
        # getCookies reports a session cookie as expires == -1. Sending that back
        # would set a cookie that expired in 1969; omitting the field is what
        # actually produces a session cookie.
        exp = entry.get("expires")
        if not isinstance(exp, (int, float)) or exp <= 0:
            entry.pop("expires", None)
        try:
            params.append(cdp_net.CookieParam.from_json(entry))
        except Exception as e:
            logger.warning("Skipping unusable cookie %s: %s", entry.get("name"), e)

    cookie_error = ""
    if params:
        try:
            await tab.send(cdp_storage.set_cookies(cookies=params))
        except Exception as e:
            cookie_error = str(e)
            logger.warning("Failed to restore cookies: %s", e)

    # Report what the browser actually holds, not how many calls did not raise.
    cookies_restored = 0
    try:
        wanted = {(c.get("name"), c.get("domain")) for c in saved_cookies}
        live = await tab.send(cdp_storage.get_cookies())
        cookies_restored = sum(1 for c in (live or []) if (c.name, c.domain) in wanted)
    except Exception as e:
        logger.warning("Could not verify restored cookies: %s", e)
        cookie_error = cookie_error or str(e)

    # 2. Navigate to the saved origin, then restore localStorage there.
    # The docstring promises this navigation unconditionally, and the reload in
    # step 4 is only meaningful if we are on the origin the cookies belong to.
    ls_items = session.get("localStorage", {})
    ls_restored = 0
    current_url = session.get("current_url", "")
    if current_url and current_url != "about:blank":
        try:
            await tab.get(current_url)
            await tab
        except Exception:
            pass
    if ls_items:
        try:
            await tab.set_local_storage(ls_items)
            ls_restored = len(ls_items)
        except Exception as e:
            logger.warning("Failed to restore localStorage: %s", e)

    # 3. Optionally restore open pages
    pages_opened = 0
    if restore_pages:
        for url in session.get("pages", []):
            if url and url != "about:blank" and url != session.get("current_url", ""):
                try:
                    await browser.get(url, new_tab=True)
                    pages_opened += 1
                except Exception:
                    pass

    # 4. Reload current page to apply cookies
    try:
        await tab.reload()
        await tab
    except Exception:
        pass

    saved_count = len(session.get("cookies", []))
    cookie_line = f"  Cookies restored: {cookies_restored} of {saved_count} saved"
    if cookies_restored < saved_count:
        cookie_line += (
            f"\n  Note: {saved_count - cookies_restored} cookie(s) did not survive"
            " — expired tokens and cookies for other origins are dropped by Chrome."
        )
        if cookie_error:
            cookie_line += f"\n  Error: {cookie_error}"
    return (
        f"Session '{session.get('name', '')}' loaded from {filepath}\n"
        f"{cookie_line}\n"
        f"  localStorage items restored: {ls_restored}\n"
        f"  Pages re-opened: {pages_opened}"
    )


@tool(title="List sessions", read_only=True)
async def list_sessions() -> str:
    """List saved session files with their name, save time and contents.

    Shows the filename to hand to load_session, along with how many cookies and
    localStorage entries each one holds, newest first.

    Sessions live in ~/.nodriver-mcp/sessions/ and are never cleaned up
    automatically, so old logins accumulate there over time.
    """
    _ensure_sessions_dir()
    files = sorted(
        [f for f in os.listdir(_SESSIONS_DIR) if f.endswith(".json")],
        reverse=True,
    )

    if not files:
        return "No saved sessions found."

    lines = [f"Saved sessions ({len(files)}):"]
    for f in files:
        try:
            filepath = os.path.join(_SESSIONS_DIR, f)
            with open(filepath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            name = data.get("name", "unknown")
            saved_at = data.get("saved_at", "unknown")
            n_cookies = len(data.get("cookies", []))
            n_ls = len(data.get("localStorage", {}))
            lines.append(f"  {f}")
            lines.append(f"    Name: {name} | Saved: {saved_at} | Cookies: {n_cookies} | localStorage: {n_ls}")
        except Exception:
            lines.append(f"  {f} (unable to read)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Content extraction, waiting, export, cookies, runtime flags
# ---------------------------------------------------------------------------

@tool(title="Get page content", read_only=True)
async def get_page_content(
    format: Annotated[
        Literal["text", "html"],
        Field(description=(
            '"text" returns document.body.innerText — visible text without markup, '
            'which is what you want for reading. "html" returns the full outerHTML '
            "including scripts and attributes, needed when you care about markup, "
            "data- attributes or hidden field values."
        )),
    ] = "text",
    max_chars: Annotated[
        int,
        Field(ge=0, description=(
            "Truncate the output at this many characters. 0 means no limit, which is "
            'risky on large pages — especially with format="html".'
        )),
    ] = 100000,
    file_path: Annotated[
        str,
        Field(description=(
            "Write the content to this local path instead of returning it — the way "
            "to capture a large page without flooding the conversation."
        )),
    ] = "",
) -> str:
    """Get the page's visible text, or its full HTML.

    The cheapest way to read a page when you only need content and not the uids
    take_snapshot provides: no accessibility tree is built, and the text form
    carries no markup overhead.

    Returns the DOM as it stands right now, so on pages that render
    asynchronously call wait_for or wait_for_selector first.

    Use take_snapshot when you intend to interact with elements, and
    query_selector when you want specific elements rather than the whole page.
    """
    tab = await _active_tab()
    if format == "html":
        content = await _evaluate_value(tab, "document.documentElement.outerHTML")
    else:
        # A <frameset> page has no body, and document.body.innerText would be ''
        # — indistinguishable from a blank page. Fall back to documentElement and
        # say so when the text is empty but frames are present.
        content = await _evaluate_value(
            tab, "(document.body || document.documentElement || {}).innerText || ''"
        )
    content = content or ""
    if format == "text" and not content.strip():
        try:
            frames = await _evaluate_value(
                tab, "document.querySelectorAll('frame,iframe').length"
            )
        except Exception:
            frames = 0
        if frames:
            return (
                f"(no text in the main document; this page is built from {frames} frame(s), "
                "whose content is not included)"
            )
    if file_path:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Saved {len(content)} chars of {format} to {file_path}."
    if max_chars and len(content) > max_chars:
        return content[:max_chars] + f"\n... (truncated, {len(content)} chars total)"
    return content


@tool(title="Query CSS selector", read_only=True)
async def query_selector(
    selector: Annotated[
        str,
        Field(description=(
            'A CSS selector, e.g. "a.result", "#nav li", "input[type=file]". Standard '
            "querySelectorAll syntax — no jQuery extensions such as :contains()."
        )),
    ],
    limit: Annotated[
        int, Field(gt=0, description="Maximum number of matching elements to return.")
    ] = 20,
) -> str:
    """Find elements by CSS selector; list their tag, text, href, id and class.

    The efficient way to pull a repeated structure off a page — search results,
    product tiles, table rows — without paying for a full snapshot.

    Returns a compact listing only. It yields no uids, so it cannot drive clicks:
    take a snapshot when you need to interact, or operate on the elements
    directly via evaluate_script.

    Element text is truncated to 200 characters.
    """
    tab = await _active_tab()
    sel = json.dumps(selector)
    expr = (
        "JSON.stringify([...document.querySelectorAll(%s)].slice(0,%d).map(el=>({"
        "tag:el.tagName.toLowerCase(),"
        "text:(el.innerText||el.textContent||'').trim().slice(0,200),"
        "href:el.getAttribute('href')||null,"
        "id:el.id||null,"
        "cls:(typeof el.className==='string'&&el.className)||null"
        "})))" % (sel, int(limit))
    )
    try:
        raw = await _evaluate_value(tab, expr)
        items = json.loads(raw) if raw else []
    except Exception as e:
        return f"Error querying '{selector}': {e}"
    if not items:
        return f"No elements match '{selector}'."
    lines = [f"{len(items)} element(s) for '{selector}':"]
    for i, el in enumerate(items):
        parts = [el.get("tag", "?")]
        if el.get("id"):
            parts.append(f"#{el['id']}")
        if el.get("href"):
            parts.append(f"href={el['href']}")
        line = f"  [{i}] " + " ".join(parts)
        if el.get("text"):
            line += f' — "{el["text"]}"'
        lines.append(line)
    return "\n".join(lines)


@tool(title="Get computed styles", read_only=True)
async def get_computed_styles(
    selector: Annotated[
        str,
        Field(description=(
            'CSS selector of the element to inspect, e.g. "#header" or ".btn.primary". '
            "The first match is used. Leave empty to use `uid` instead."
        )),
    ] = "",
    uid: Annotated[
        str,
        Field(description=(
            "Element uid from the most recent take_snapshot, as an alternative to "
            "`selector`. Ignored when `selector` is given."
        )),
    ] = "",
    properties: Annotated[
        list[str] | None,
        Field(description=(
            'Only return these CSS properties, e.g. ["display", "color", "font-size"]. '
            "Omit to get the properties that differ from the browser default, which is "
            "usually what you want — the full computed set is several hundred entries."
        )),
    ] = None,
) -> str:
    """Read an element's computed styles, as the browser actually resolved them.

    Computed styles are the end result of the whole cascade, so this answers
    what a stylesheet alone cannot: why an element is invisible
    (`display: none`, `visibility: hidden`, `opacity: 0`), what a CSS variable
    resolved to, or which font actually got used.

    Also reports the element's box: position, size, and whether it is currently
    in the viewport. An element with zero width or height is present in the DOM
    but not rendered, which is the usual reason a click appears to do nothing.

    By default only properties that differ from the browser default are
    returned, because the full set runs to several hundred entries and buries
    the interesting ones.
    """
    tab = await _active_tab()

    if not selector and not uid:
        return "Error: provide either selector or uid."

    if selector:
        sel = json.dumps(selector)
        expr = f"document.querySelector({sel})"
    else:
        if uid not in _uid_to_backend_node_id:
            return f"Error: Unknown uid '{uid}'. Take a new snapshot first."
        try:
            remote_obj = await _resolve_uid(tab, uid)
        except ValueError as e:
            return f"Error: {e}"
        expr = None

    wanted = json.dumps([p.strip() for p in (properties or []) if p and p.strip()])
    body = (
        "function() {"
        " const el = this;"
        " if (!el) return null;"
        f" const want = {wanted};"
        " const cs = getComputedStyle(el);"
        " const out = {};"
        " if (want.length) {"
        "   for (const p of want) out[p] = cs.getPropertyValue(p);"
        " } else {"
        "   const probe = document.createElement(el.tagName);"
        "   document.body.appendChild(probe);"
        "   const base = getComputedStyle(probe);"
        "   for (const p of cs) { const v = cs.getPropertyValue(p);"
        "     if (v !== base.getPropertyValue(p)) out[p] = v; }"
        "   probe.remove();"
        " }"
        " const r = el.getBoundingClientRect();"
        # Hand back a JSON string, not an object: CDP returns objects in a typed
        # wire form that differs between the two call paths below, and decoding
        # that twice is how this went wrong the first time.
        " return JSON.stringify({tag: el.tagName.toLowerCase(), styles: out,"
        "   box: {x: Math.round(r.x), y: Math.round(r.y),"
        "     width: Math.round(r.width), height: Math.round(r.height)},"
        "   rendered: r.width > 0 && r.height > 0,"
        "   inViewport: r.top < innerHeight && r.bottom > 0 && r.left < innerWidth && r.right > 0}); }"
    )

    try:
        if expr is not None:
            raw = await _evaluate_value(tab, f"({body}).call({expr})")
        else:
            remote = await _call_function_on(
                tab,
                function_declaration=body,
                object_id=remote_obj.object_id,
                return_by_value=True,
            )
            raw = remote.value if remote else None
    except Exception as e:
        return f"Error reading computed styles: {e}"

    if not raw:
        return f"No element matches '{selector or uid}'."
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as e:
        return f"Error decoding computed styles: {e}"
    if not isinstance(data, dict):
        return f"No element matches '{selector or uid}'."

    styles = data.get("styles") or {}
    box = data.get("box") or {}
    lines = [
        f"<{data.get('tag')}> {selector or f'uid={uid}'}",
        f"  box: {box.get('width')}x{box.get('height')} at ({box.get('x')}, {box.get('y')})",
        f"  rendered: {data.get('rendered')} | in viewport: {data.get('inViewport')}",
        f"  computed styles ({len(styles)}):",
    ]
    for k in sorted(styles):
        lines.append(f"    {k}: {styles[k]}")
    return "\n".join(lines)


@tool(title="Scroll to element", open_world=True)
async def scroll_to_selector(
    selector: Annotated[
        str,
        Field(description="CSS selector of the element to scroll to; the first match wins."),
    ],
) -> str:
    """Scroll the first element matching a CSS selector into view, centered.

    More precise than scroll_page when you already know what you are looking
    for, and the usual preparation for click_at, which needs its target inside
    the viewport.

    `click` scrolls to its own target, so this is unnecessary before it. Reports
    whether anything matched instead of failing silently.
    """
    tab = await _active_tab()
    sel = json.dumps(selector)
    expr = (
        "(() => { const el=document.querySelector(%s); if(!el) return false; "
        "el.scrollIntoView({block:'center', inline:'center'}); return true; })()" % sel
    )
    try:
        ok = await _evaluate_value(tab, expr)
    except Exception as e:
        # Only call it a bad selector when it is one; a detached context or a
        # crashed renderer arrives here too and deserves its own words.
        if "SyntaxError" in str(e) or "not a valid selector" in str(e):
            return f"Error: invalid selector '{selector}': {e}"
        return f"Error scrolling to '{selector}': {e}"
    return f"Scrolled to '{selector}'." if ok else f"No element matches '{selector}'."


_RESOURCE_EXTS: dict[str, list[str]] = {
    "image": ["png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp", "avif"],
    "font": ["woff", "woff2", "ttf", "otf", "eot"],
    "stylesheet": ["css"],
    "media": ["mp4", "webm", "ogg", "mp3", "wav", "m4a", "mov"],
}


@tool(title="Block resource types", idempotent=True)
async def block_resources(
    types: Annotated[
        list[Literal["image", "font", "stylesheet", "media"]] | None,
        Field(description=(
            'Resource types to block. ["image", "font", "media"] is the usual choice '
            "for scraping — it keeps stylesheets, so layout-dependent behaviour still "
            "works. Pass an empty list or omit to unblock everything."
        )),
    ] = None,
) -> str:
    """Block images, fonts, stylesheets or media to speed up page loads.

    Removes most of the bytes on a media-heavy page, which makes scraping
    several times faster and far cheaper over a metered or proxied connection.

    Blocking stylesheets breaks layout, so anything that depends on element
    geometry becomes unreliable — click_at, element screenshots, and the
    `visible` check in wait_for_selector. Text extraction is unaffected.

    Applies to the current page session and stays in effect across navigations
    until called again with no types.
    """
    tab = await _active_tab()
    import nodriver.cdp.network as cdp_net
    types = types or []
    valid, unknown, patterns = [], [], []
    for t in types:
        key = (t or "").strip().lower()
        if key in _RESOURCE_EXTS:
            valid.append(key)
            patterns.extend(f"*.{ext}*" for ext in _RESOURCE_EXTS[key])
        elif key:
            unknown.append(t)
    # setBlockedURLs is a Network-domain command and is silently ignored when the
    # domain is not enabled on this session — which is how block_resources could
    # report success while every image still loaded.
    await _auto_enable_network_collection(tab)
    await tab.send(cdp_net.set_blocked_ur_ls(urls=patterns))
    if not valid:
        base = "Resource blocking disabled (all resources allowed)."
    else:
        base = f"Blocking resource types: {', '.join(sorted(set(valid)))}."
    if unknown:
        base += f" Ignored unknown: {', '.join(unknown)} (valid: image, font, stylesheet, media)."
    return base


@tool(title="Wait for element", read_only=True, open_world=True)
async def wait_for_selector(
    selector: Annotated[
        str,
        Field(description='CSS selector to wait for, e.g. "#login" or ".results .item".'),
    ],
    timeout: Annotated[
        int, Field(gt=0, description="Maximum wait in milliseconds before giving up.")
    ] = 30000,
    visible: Annotated[
        bool,
        Field(description=(
            "Also require the element to have a non-zero size. Use this with "
            "frameworks that insert an element into the DOM before rendering it — "
            "presence alone would otherwise resolve too early."
        )),
    ] = False,
) -> str:
    """Wait until an element matching a CSS selector appears on the page.

    The structural counterpart to wait_for: use it when you know the markup but
    not the wording, or when the wording is localised.

    Polls about three times a second and returns as soon as the element shows
    up. Unlike wait_for it does not return a snapshot, so follow with
    take_snapshot when you intend to interact.
    """
    tab = await _active_tab()
    sel = json.dumps(selector)
    if visible:
        expr = ("(() => { const el = document.querySelector(%s); if (!el) return false; "
                "const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })()" % sel)
    else:
        expr = "!!document.querySelector(%s)" % sel
    timeout_s = timeout / 1000
    start = time.time()
    last_error = ""
    while time.time() - start < timeout_s:
        try:
            if await _evaluate_value(tab, expr):
                return f"Element '{selector}' found."
        except Exception as e:
            # A selector that does not parse will never start parsing, so
            # waiting out the timeout only hides the real problem.
            if "SyntaxError" in str(e) or "not a valid selector" in str(e):
                return f"Error: invalid selector '{selector}': {e}"
            last_error = str(e)
        await asyncio.sleep(0.3)
    msg = f"Timeout: '{selector}' did not appear within {timeout}ms."
    if last_error:
        msg += f" Last error while polling: {last_error}"
    return msg


@tool(title="Save page as PDF")
async def save_pdf(
    file_path: Annotated[str, Field(description="Local path to write the .pdf file to.")],
    landscape: Annotated[
        bool, Field(description="Use landscape orientation instead of portrait.")
    ] = False,
    print_background: Annotated[
        bool,
        Field(description=(
            "Include background colours and images. Default true, which resembles the "
            "page as seen on screen; false gives the leaner print view."
        )),
    ] = True,
) -> str:
    """Export the current page to a PDF using Chrome's print-to-PDF.

    Renders the entire document rather than just the viewport, and applies the
    page's print stylesheet — so the result can differ from the screen layout.

    A good way to archive a rendered page as one file. For a pixel-accurate copy
    of what is on screen, use take_screenshot with full_page instead.
    """
    tab = await _active_tab()
    import nodriver.cdp.page as cdp_page
    result = await tab.send(cdp_page.print_to_pdf(
        landscape=landscape,
        print_background=print_background,
        transfer_mode="ReturnAsBase64",
    ))
    data = result[0] if isinstance(result, tuple) else result
    with open(file_path, "wb") as f:
        f.write(base64.b64decode(data))
    return f"PDF saved to {file_path}."


@tool(title="Clear cookies", destructive=True, idempotent=True)
async def clear_cookies() -> str:
    """Delete every cookie in the browser.

    Browser-wide, not per site and not per tab — this logs you out of everything
    at once, with no undo. Restore a previous state with load_session if you
    have one saved.

    Useful for testing a first-time-visitor flow or resetting a consent banner
    decision. localStorage is left untouched, so sites that keep state there may
    still recognise you.
    """
    tab = await _active_tab()
    import nodriver.cdp.network as cdp_net
    await tab.send(cdp_net.clear_browser_cookies())
    return "All cookies cleared."


@tool(title="Set browser launch flags", destructive=True)
async def set_browser_flags(
    translate: Annotated[
        bool | None,
        Field(description=(
            "True allows Chrome's Google Translate popup, false suppresses it. Omit to "
            "leave unchanged. Suppressed by default, because the popup covers page "
            "content and swallows clicks."
        )),
    ] = None,
    extensions: Annotated[
        bool | None,
        Field(description=(
            "True allows externally-installed Chrome extensions, false blocks them. "
            'Omit to leave unchanged. Blocked by default, so no "an extension requires '
            'your attention" prompt appears. manage_extensions offers the same switch '
            "plus listing and loading."
        )),
    ] = None,
    extra_args: Annotated[
        list[str] | None,
        Field(description=(
            "Replace the set of extra Chrome launch flags with this list, e.g. "
            '["--lang=de-DE", "--window-size=1280,800"]. A leading "--" is added if '
            "missing. Pass [] to clear them, omit to leave unchanged. This replaces "
            "the list rather than appending to it."
        )),
    ] = None,
    restart: Annotated[
        bool,
        Field(description=(
            "Restart Chrome now so the flags take effect, closing all open pages. "
            "Default true; false defers them to the next browser start."
        )),
    ] = True,
) -> str:
    """Change Chrome's launch flags at runtime, and show the current ones.

    Call with no arguments at all to just read the effective configuration —
    that form changes nothing.

    These are launch-time flags, so applying them restarts Chrome and closes
    every open page; on an ephemeral profile that also drops its cookies. Values
    set here override the NODRIVER_ENABLE_* environment variables.
    """
    global _enable_translate, _enable_extensions, _extra_browser_args
    changed = []
    if translate is not None:
        _enable_translate = translate
        changed.append(f"translate={'on' if translate else 'off'}")
    if extensions is not None:
        _enable_extensions = extensions
        changed.append(f"extensions={'on' if extensions else 'off'}")
    if extra_args is not None:
        cleaned = []
        for a in extra_args:
            a = (a or "").strip()
            if not a:
                continue
            if not a.startswith("-"):
                a = "--" + a
            cleaned.append(a)
        _extra_browser_args = cleaned
        changed.append(f"extra_args={cleaned or '(cleared)'}")

    eff_t = _feature_enabled(_enable_translate, "NODRIVER_ENABLE_TRANSLATE")
    eff_e = _feature_enabled(_enable_extensions, "NODRIVER_ENABLE_EXTENSIONS")
    effective = []
    if not eff_t:
        effective.append("--disable-features=Translate")
    if not eff_e:
        effective.append("--disable-extensions")
    effective.extend(_extra_browser_args)
    status = (
        f"Google Translate popup: {'enabled' if eff_t else 'disabled'} | "
        f"External extensions: {'enabled' if eff_e else 'disabled'}\n"
        f"Extra flags: {_extra_browser_args or '(none)'}\n"
        f"Effective launch flags: {effective or '(none)'}"
    )

    if not changed:
        return f"Current browser flags:\n{status}\n(Pass translate= / extensions= / extra_args= to change.)"

    msg = "Updated: " + ", ".join(changed) + f"\n{status}"
    if restart:
        was = await _stop_browser()
        msg += "\nBrowser " + (
            "stopped; relaunches with the new flags on the next action."
            if was else "will start with these flags on the next action."
        )
    else:
        msg += "\nUse close_browser (or restart) for the change to take effect."
    return msg


# ---------------------------------------------------------------------------
# Chrome extensions
# ---------------------------------------------------------------------------

def _active_profile_dir() -> str | None:
    """The user-data-dir currently in use, or None for an ephemeral profile."""
    return _selected_profile_dir or os.environ.get("NODRIVER_USER_DATA_DIR") or None


def _is_branded_chrome() -> bool:
    """Whether the browser is an official Google Chrome build.

    Verified on Chrome 151: branded builds ignore --load-extension outright —
    the switch is on the command line, --enable-unsafe-extension-debugging is
    set and DisableLoadExtensionCommandLineSwitch is off, yet the extension is
    never registered. Chromium and Chrome for Testing still honour it.
    """
    path = os.environ.get("NODRIVER_BROWSER_PATH") or ""
    if not path:
        try:
            from nodriver.core.config import find_chrome_executable

            path = str(find_chrome_executable() or "")
        except Exception:
            return False
    low = path.replace("\\", "/").lower()
    if "for testing" in low or "for-testing" in low or "chromium" in low:
        return False
    return "google/chrome" in low or low.endswith("/google chrome")


_UNPACKED_UNSUPPORTED = (
    "Note: this is an official Google Chrome build, which ignores "
    "--load-extension since v137 — the extension will NOT actually load. "
    "Working alternatives: install the extension from the Chrome Web Store "
    "into a persistent profile (survives restarts, then just use "
    'manage_extensions("on")), or point NODRIVER_BROWSER_PATH at Chromium / '
    "Chrome for Testing, which still support unpacked extensions."
)


def _extension_display_name(version_dir: str, manifest: dict) -> str:
    """Resolve a manifest name, following __MSG_key__ into _locales/."""
    name = manifest.get("name", "")
    if not name.startswith("__MSG_"):
        return name or "(unnamed)"
    key = name[len("__MSG_"):].rstrip("_")
    locales = [manifest.get("default_locale", ""), "en", "en_US"]
    for loc in [x for x in locales if x]:
        messages = os.path.join(version_dir, "_locales", loc, "messages.json")
        if not os.path.isfile(messages):
            continue
        try:
            with open(messages, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        entry = data.get(key) or next(
            (v for k, v in data.items() if k.lower() == key.lower()), None
        )
        if isinstance(entry, dict) and entry.get("message"):
            return entry["message"]
    return name


def _scan_profile_extensions(profile_dir: str) -> list[dict]:
    """Extensions installed in a profile, newest version of each."""
    found: list[dict] = []
    root = os.path.join(profile_dir, "Default", "Extensions")
    if not os.path.isdir(root):
        return found
    for ext_id in sorted(os.listdir(root)):
        id_dir = os.path.join(root, ext_id)
        if not os.path.isdir(id_dir):
            continue
        versions = sorted(d for d in os.listdir(id_dir) if os.path.isdir(os.path.join(id_dir, d)))
        if not versions:
            continue
        version_dir = os.path.join(id_dir, versions[-1])
        manifest_path = os.path.join(version_dir, "manifest.json")
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except Exception:
            continue
        found.append({
            "id": ext_id,
            "name": _extension_display_name(version_dir, manifest),
            "version": manifest.get("version", versions[-1]),
        })
    return found


@tool(title="Manage Chrome extensions", destructive=True)
async def manage_extensions(
    action: Annotated[
        Literal["list", "on", "off", "load", "unload"],
        Field(description=(
            '"list" shows the extensions installed in the active profile plus the '
            "current state, and changes nothing. \"on\" and \"off\" flip the master "
            'switch. "load" registers an unpacked extension from `path` and implies '
            '"on". "unload" stops loading `path`, or every unpacked extension when '
            '`path` is empty. Every action except "list" restarts the browser.'
        )),
    ] = "list",
    path: Annotated[
        str,
        Field(description=(
            "Folder containing the extension's manifest.json, for \"load\" and "
            '"unload". Ignored by the other actions.'
        )),
    ] = "",
) -> str:
    """List, enable, disable or load Chrome extensions.

    Two separate mechanisms are in play. The master switch: Chrome runs with
    --disable-extensions by default, so extensions installed in the profile stay
    dark until it is turned on. And unpacked extensions loaded from a folder on
    disk. The master switch governs both — with it off, unpacked extensions stay
    registered but do not load.

    "load" only works on Chromium or Chrome for Testing. Official Google Chrome
    builds have ignored --load-extension since v137 (still true on Chrome 151,
    even with --enable-unsafe-extension-debugging), and this tool says so rather
    than pretending it worked. On official Chrome the working path is: install
    the extension once from the Web Store into a persistent profile, then switch
    it on with "on".

    Extensions only persist in a persistent profile — on the default ephemeral
    one nothing can stay installed.
    """
    global _enable_extensions, _loaded_extensions
    act = (action or "list").strip().lower()
    profile_dir = _active_profile_dir()

    if act == "list":
        master_on = _feature_enabled(_enable_extensions, "NODRIVER_ENABLE_EXTENSIONS")
        lines = [
            f"Master switch: extensions are {'ENABLED' if master_on else 'DISABLED'} "
            f"(--disable-extensions {'not set' if master_on else 'active'})",
        ]
        if profile_dir:
            installed = _scan_profile_extensions(profile_dir)
            lines.append(f"\nInstalled in profile ({len(installed)}) — {profile_dir}:")
            if installed:
                lines.extend(f"  {e['name']} v{e['version']}  [{e['id']}]" for e in installed)
            else:
                lines.append(
                    "  (none — install one from the Chrome Web Store in this browser; it then persists)"
                )
        else:
            lines.append(
                "\nActive profile is ephemeral, so nothing can stay installed. "
                "Switch to a persistent profile first (use_profile)."
            )
        lines.append(f"\nUnpacked extensions registered from disk ({len(_loaded_extensions)}):")
        if _loaded_extensions:
            if not master_on:
                marker = '  <- not loaded, master switch is off (use "on")'
            elif _is_branded_chrome():
                marker = "  <- ignored by this Chrome build"
            else:
                marker = ""
            lines.extend(f"  {p}{marker}" for p in _loaded_extensions)
        else:
            lines.append("  (none)")
        return "\n".join(lines)

    if act in ("on", "off"):
        _enable_extensions = act == "on"
        was = await _stop_browser()
        extra = ""
        if _loaded_extensions:
            extra = (
                f" The {len(_loaded_extensions)} registered unpacked extension(s) "
                + ("load again." if _enable_extensions else "stay registered but are not loaded.")
            )
        return (
            f"Extensions {'enabled' if _enable_extensions else 'disabled'}. Browser "
            + ("stopped; relaunches with the change on the next action."
               if was else "will start with this setting on the next action.")
            + extra
        )

    if act == "load":
        target = (path or "").strip().strip('"')
        if not target:
            return 'Error: "load" needs path= pointing at the extension folder.'
        if not os.path.isdir(target):
            return f"Error: not a directory: {target}"
        if not os.path.isfile(os.path.join(target, "manifest.json")):
            return f"Error: no manifest.json in {target} — point at the folder that contains it."
        if target in _loaded_extensions:
            return f"Already loaded: {target}"
        _loaded_extensions.append(target)
        _enable_extensions = True
        was = await _stop_browser()
        msg = (
            f"Will load unpacked extension: {target}\n"
            f"Loaded unpacked ({len(_loaded_extensions)} total), extensions enabled. Browser "
            + ("stopped; relaunches with it on the next action."
               if was else "will start with it on the next action.")
        )
        if _is_branded_chrome():
            msg += f"\n\n{_UNPACKED_UNSUPPORTED}"
        return msg

    if act == "unload":
        target = (path or "").strip().strip('"')
        if target and target not in _loaded_extensions:
            return f"Not currently loaded: {target}"
        removed = [target] if target else list(_loaded_extensions)
        if not removed:
            return "No unpacked extensions are loaded."
        _loaded_extensions = [p for p in _loaded_extensions if p not in removed]
        was = await _stop_browser()
        return (
            f"Unloaded {len(removed)} unpacked extension(s). Browser "
            + ("stopped; relaunches without them on the next action."
               if was else "will start without them on the next action.")
        )

    return f'Error: unknown action "{action}". Use list / on / off / load / unload.'


# ---------------------------------------------------------------------------
# Worker introspection (used by the multiplexer, hidden from clients)
# ---------------------------------------------------------------------------

@tool(title="Browser status", read_only=True)
async def browser_status() -> str:
    """Report this worker's browser state as JSON, without ever launching Chrome.

    Internal plumbing for the multiplexer's list_browsers, which has to describe
    every browser slot without starting the ones that were never used. Every
    other read-only tool would launch Chrome on the spot to answer.
    """
    attached = _connect_target()
    alive = bool(_browser is not None and (attached or not _browser.stopped))
    tabs: list[dict[str, str]] = []
    if alive and _browser is not None:
        for t in _browser.tabs:
            try:
                tabs.append({"url": t.target.url or "", "title": t.target.title or ""})
            except Exception:
                pass
    # The directory Chrome was actually launched on, which for a throwaway
    # profile is a temp dir nobody named — and the thing whose removal on
    # shutdown is worth being able to check.
    live_dir = None
    if _browser is not None:
        live_dir = str(getattr(getattr(_browser, "config", None), "user_data_dir", "") or "") or None
    return json.dumps({
        "pid": os.getpid(),
        "chrome_running": alive,
        "attached_to": f"{attached[0]}:{attached[1]}" if attached else None,
        "profile": _selected_profile_name or ("(attached)" if attached else "temp"),
        "profile_dir": _selected_profile_dir,
        "user_data_dir": live_dir,
        "tab_count": len(tabs),
        "tabs": tabs[:20],
        "selected_target_id": _selected_target_id,
    })


# ---------------------------------------------------------------------------
# Chrome profile (user-data-dir) management
# ---------------------------------------------------------------------------

@tool(title="List profiles", read_only=True)
async def list_profiles() -> str:
    """List persistent Chrome profiles and show which one is active.

    By default the browser runs on a fresh ephemeral temp profile that is
    deleted when the session ends. That default is what lets several nodriver
    instances — Claude Desktop, Claude Code, the VS Code extension — run at the
    same time without fighting over one profile directory.

    Persistent profiles keep cookies, logins and installed extensions across
    sessions. Create one with create_profile, switch with use_profile, and
    return to ephemeral with use_temp_profile.
    """
    os.makedirs(_PROFILES_DIR, exist_ok=True)
    names = sorted(
        d for d in os.listdir(_PROFILES_DIR)
        if os.path.isdir(os.path.join(_PROFILES_DIR, d))
    )
    running = bool(_browser and not _browser.stopped)
    attached = _connect_target()
    active = (
        f"attached to the running browser at {attached[0]}:{attached[1]} (no profile of ours)"
        if attached
        else (_selected_profile_name or "temp (ephemeral, auto-deleted)")
    )
    lines = [
        f"Active profile: {active}",
        f"Browser running: {running}",
        "",
        f"Persistent profiles ({len(names)}) under {_PROFILES_DIR}:",
    ]
    if not names:
        lines.append("  (none yet — create one with create_profile)")
    for n in names:
        mark = "  <- ACTIVE" if n == _selected_profile_name else ""
        lines.append(f"  - {n}{mark}")
    lines += [
        "",
        "Default = ephemeral temp profile per session. Switch with use_profile(name); "
        "return to ephemeral with use_temp_profile().",
    ]
    return "\n".join(lines)


@tool(title="Create profile", idempotent=True)
async def create_profile(
    name: Annotated[
        str,
        Field(description=(
            'Profile name, e.g. "google-login". Only letters, digits, "-" and "_" are '
            "kept; any other character is stripped out."
        )),
    ],
    activate: Annotated[
        bool,
        Field(description=(
            "Switch to the new profile straight away. That restarts the browser and "
            "closes all open pages. Default false, which only creates the directory."
        )),
    ] = False,
) -> str:
    """Create a named persistent Chrome profile — a reusable user-data dir.

    A persistent profile keeps cookies, logins and Web Store extensions between
    runs, so a site you log into once stays logged in. It is the sturdier
    alternative to save_session / load_session.

    Creating one is harmless by itself: nothing changes until you activate it,
    here or via use_profile. An existing profile of the same name is left
    untouched rather than overwritten. Profiles live under
    ~/.nodriver-mcp/profiles/<name>.

    Only one browser instance can use a given profile at a time, so give
    concurrent setups different names.
    """
    safe = _safe_profile_name(name)
    if not safe:
        return "Error: invalid profile name. Use letters, digits, '-' or '_'."
    path = os.path.join(_PROFILES_DIR, safe)
    existed = os.path.isdir(path)
    os.makedirs(path, exist_ok=True)
    msg = f"Profile '{safe}' {'already exists' if existed else 'created'} at {path}."
    if activate:
        await _restart_browser_with(path, safe)
        msg += f"\nActivated — the browser will use profile '{safe}' on the next action."
    else:
        msg += f"\nActivate it with use_profile(\"{safe}\")."
    return msg


@tool(title="Attach to a running browser", destructive=True, idempotent=True)
async def use_running_browser(
    port: Annotated[
        int,
        Field(gt=0, le=65535, description=(
            "The remote debugging port Chrome was started with, e.g. 9222 for "
            "--remote-debugging-port=9222."
        )),
    ] = 9222,
    host: Annotated[
        str,
        Field(description=(
            "Host the browser is listening on. Leave at 127.0.0.1 unless you are "
            "attaching to a browser on another machine or in a container."
        )),
    ] = "127.0.0.1",
) -> str:
    """Drive a Chrome that is already running, instead of launching a new one.

    This is how you use a browser you are already signed into. Chrome locks its
    user-data-dir, so the profile holding your real logins cannot be opened a
    second time — attaching to the running instance is the only way in, and it
    saves rebuilding every login through the automation.

    The user starts Chrome themselves, once:

        chrome --remote-debugging-port=9222 --user-data-dir=/path/to/profile

    then this tool attaches to it. Every tool then acts on that browser and its
    real tabs.

    SECURITY: that profile becomes part of the agent's reach. Whatever it is
    signed into — mail, bank, company systems — is reachable from here, because
    a cookie jar is all or nothing. Point this at a profile you are willing to
    expose, not your everyday one.

    While attached, close_browser and every profile switch only detach; they
    never close a browser they did not start. Return to a self-launched browser
    with use_temp_profile or use_profile.
    """
    global _connect_host, _connect_port, _connect_disabled
    await _stop_browser()
    _connect_host, _connect_port, _connect_disabled = host, int(port), False
    try:
        browser = await _get_browser()
    except Exception as e:
        _connect_host = _connect_port = None
        _connect_disabled = True
        return f"Error: {e}"
    pages = await _format_pages()
    return (
        f"Attached to the browser running at {host}:{port}. "
        f"It has {len(browser.tabs)} tab(s); tools now act on this browser and it "
        f"stays open when the server stops.{pages}"
    )


@tool(title="Use temporary profile", destructive=True, idempotent=True)
async def use_temp_profile() -> str:
    """Switch back to a fresh ephemeral profile, created and deleted per session.

    This is the default. It leaves nothing behind on disk and lets many nodriver
    instances run concurrently without colliding on a profile directory.

    Restarts the browser and closes all open pages. The profile you are leaving
    is not deleted — a persistent one keeps its cookies for the next
    use_profile. Anything held in the outgoing temp profile is gone.
    """
    await _restart_browser_with(None, None)
    return "Switched to an ephemeral temp profile (auto-deleted after the session)."


@tool(title="Use persistent profile", destructive=True, idempotent=True)
async def use_profile(
    name: Annotated[
        str,
        Field(description=(
            "Name of an existing persistent profile, as shown by list_profiles. Pass "
            '"", "temp", "ephemeral" or "none" to switch back to an ephemeral profile.'
        )),
    ],
) -> str:
    """Switch the browser to a named persistent profile.

    Use it to pick up the cookies, logins and extensions stored in an earlier
    session, so a run starts out already authenticated.

    Restarts the browser and closes every open page. The profile has to exist
    already — create it first with create_profile. Only one browser may use a
    profile at a time; a second instance pointed at the same one fails to start.
    """
    if not name or name.strip().lower() in ("temp", "ephemeral", "none"):
        return await use_temp_profile()
    safe = _safe_profile_name(name)
    if not safe:
        return "Error: invalid profile name."
    path = os.path.join(_PROFILES_DIR, safe)
    if not os.path.isdir(path):
        return (f"Error: profile '{safe}' does not exist. "
                f"Create it with create_profile(\"{safe}\") or see list_profiles().")
    await _restart_browser_with(path, safe)
    return f"Switched to persistent profile '{safe}' ({path}). It starts on the next action."


@tool(title="Delete profile", destructive=True)
async def delete_profile(
    name: Annotated[
        str,
        Field(description="Name of the persistent profile to delete, as shown by list_profiles."),
    ],
) -> str:
    """Permanently delete a persistent Chrome profile directory.

    Irreversible: the profile's cookies, logins, history and installed
    extensions are removed from disk, with no undo.

    The active profile cannot be deleted — switch away with use_temp_profile
    first.
    """
    safe = _safe_profile_name(name)
    if not safe:
        return "Error: invalid profile name."
    if _selected_profile_name == safe:
        return f"Error: '{safe}' is the active profile. Switch away first with use_temp_profile()."
    path = os.path.join(_PROFILES_DIR, safe)
    if not os.path.isdir(path):
        return f"Error: profile '{safe}' does not exist."

    # Chrome releases a profile directory asynchronously, so deleting right
    # after switching away from it hits files that are still open. That is a
    # timing artefact, not a real refusal, so retry for a few seconds instead of
    # handing back a raw OS error the caller cannot act on.
    last: Exception | None = None
    for attempt in range(6):
        try:
            shutil.rmtree(path)
            return f"Deleted profile '{safe}'."
        except FileNotFoundError:
            return f"Deleted profile '{safe}'."
        except OSError as e:
            last = e
            await asyncio.sleep(0.5 * (attempt + 1))
    return (
        f"Could not delete profile '{safe}': files inside it are still open "
        f"({last}). A browser using it is most likely still shutting down — wait "
        "a moment and try again, or run close_browser first."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# A temp profile whose owning process is gone is only swept once it is also this
# old, which blunts PID reuse: a recycled PID within the window makes the sweep
# skip, never delete.
_STALE_PROFILE_AGE_S = 2 * 60 * 60

# One small file per live temp profile, so concurrent servers never contend over
# a shared index and a crash cannot corrupt one.
_PROFILE_CLAIMS_DIR = os.path.join(
    os.path.expanduser("~"), ".nodriver-mcp", "temp-profiles"
)


def _pid_alive(pid: int) -> bool:
    """Whether a process with this id is running.

    os.kill(pid, 0) is the usual trick and is wrong on Windows, where os.kill
    ignores the signal and calls TerminateProcess — asking "are you alive"
    would kill the process being asked.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True  # cannot tell; treat as alive and leave the profile be
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, so certainly not ours to delete
    except OSError:
        return False
    return True


def _claim_path(profile_dir: str) -> str:
    import hashlib

    digest = hashlib.sha256(os.path.abspath(profile_dir).encode("utf-8")).hexdigest()
    return os.path.join(_PROFILE_CLAIMS_DIR, f"{digest[:32]}.json")


def _claim_temp_profile(profile_dir: str) -> None:
    """Record that this process owns this throwaway profile."""
    try:
        os.makedirs(_PROFILE_CLAIMS_DIR, exist_ok=True)
        with open(_claim_path(profile_dir), "w", encoding="utf-8") as fh:
            json.dump({"path": profile_dir, "pid": os.getpid(), "created": time.time()}, fh)
    except OSError:
        logger.debug("could not record the temp profile claim", exc_info=True)


def _release_temp_profile(profile_dir: str) -> None:
    try:
        os.remove(_claim_path(profile_dir))
    except OSError:
        pass


def sweep_stale_temp_profiles(max_age_s: int = _STALE_PROFILE_AGE_S) -> int:
    """Delete temp profiles whose owning process is gone. Returns how many went.

    When the server is killed instead of asked to exit, nothing in-process runs:
    Chrome is orphaned and its profile stays in the temp directory. Nothing can
    clean that up afterwards except a sweep at the next start, which is why this
    exists despite _close_browser_and_profile handling every orderly shutdown.

    It only ever removes a profile this server family created and recorded, and
    only once the process that recorded it is gone. An earlier version instead
    swept anything in the temp directory named `uc_*` that was old enough,
    relying on a rename failing to protect a profile still in use. That guard
    only exists on Windows: POSIX renames a directory happily while its files
    are open, so on Linux and macOS a browser left running for a couple of hours
    would have had its live profile — cookies, logins, localStorage — deleted
    underneath it. Owning a claim is checkable; guessing from a filename is not.
    """
    removed = 0
    try:
        claims = os.listdir(_PROFILE_CLAIMS_DIR)
    except OSError:
        return 0
    cutoff = time.time() - max_age_s
    for entry in claims:
        claim_file = os.path.join(_PROFILE_CLAIMS_DIR, entry)
        try:
            with open(claim_file, encoding="utf-8") as fh:
                claim = json.load(fh)
            path = str(claim["path"])
            pid = int(claim["pid"])
            created = float(claim.get("created", 0))
        except (OSError, ValueError, KeyError, TypeError):
            # Unreadable claim: drop the note, never guess at a directory.
            try:
                os.remove(claim_file)
            except OSError:
                pass
            continue
        if not os.path.isdir(path):
            # Directory already gone; the note protects nothing and is noise.
            try:
                os.remove(claim_file)
            except OSError:
                pass
            continue
        if _pid_alive(pid) or created > cutoff:
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                removed += 1
        try:
            os.remove(claim_file)
        except OSError:
            pass
    if removed:
        logger.info("removed %d abandoned temp profile(s)", removed)
    return removed


async def _serve_stdio() -> None:
    """Serve until the client closes stdin, then shut the browser down properly.

    The teardown has to happen in this loop, not after it. Doing it from a
    second asyncio.run would try to close websockets belonging to a loop that
    is already gone, which fails and leaves Chrome running — and without it the
    only cleanup left is nodriver's atexit handler, the unreliable path this
    exists to replace.
    """
    try:
        await mcp.run_stdio_async()
    finally:
        try:
            await _stop_browser()
        except Exception:
            logger.debug("closing the browser on exit failed", exc_info=True)


def main():
    """Run the MCP server via stdio transport."""
    import anyio

    # Skipped in a worker: the routing layer sweeps once for all of them, and a
    # dozen workers racing over the same directories would be pointless.
    if not os.environ.get("NODRIVER_BROWSER_NAME"):
        try:
            sweep_stale_temp_profiles()
        except Exception:
            logger.debug("temp profile sweep failed", exc_info=True)
    anyio.run(_serve_stdio)


if __name__ == "__main__":
    main()
