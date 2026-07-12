"""
nodriver-mcp: An undetected Chrome automation MCP server.

Uses nodriver (successor of undetected-chromedriver) as the browser backend,
providing the same MCP tool interface as chrome-devtools-mcp but without
exposing CDP/WebDriver fingerprints that get detected by anti-bot systems.
"""

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import nodriver as uc
from mcp.server.fastmcp import FastMCP

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


def _feature_disabled_by_default(env_name: str) -> bool:
    """A default-on cleanup is disabled unless its env toggle is truthy."""
    return os.environ.get(env_name, "").lower() not in ("1", "true", "yes")


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
        # Recover from a browser that was closed/crashed between calls — its
        # .stopped flag can lag, which would otherwise make every tool fail
        # with a "no close frame" websocket error until the server restarts.
        if _browser is not None and not _browser.stopped:
            if not await _browser_alive(_browser):
                try:
                    _browser.stop()
                except Exception:
                    pass
                _browser = None

        if _browser is None or _browser.stopped:
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

            # Clean-automation defaults, each re-enable-able via an env var:
            #   - suppress the Google Translate popup  (NODRIVER_ENABLE_TRANSLATE=true)
            #   - block externally-installed extensions + their "action required"
            #     prompts                              (NODRIVER_ENABLE_EXTENSIONS=true)
            browser_args: list[str] = []
            if _feature_disabled_by_default("NODRIVER_ENABLE_TRANSLATE"):
                browser_args.append("--disable-features=Translate")
            if _feature_disabled_by_default("NODRIVER_ENABLE_EXTENSIONS"):
                browser_args.append("--disable-extensions")
            if proxy:
                browser_args.append(f"--proxy-server={proxy}")
                logger.info("Proxy configured: %s", proxy)
            if browser_args:
                kwargs["browser_args"] = browser_args

            _browser = await uc.start(**kwargs)

            logger.info(
                "Browser started (headless=%s, profile=%s)",
                headless, _selected_profile_name or "temp",
            )

            # Auto-enable network collection on the first tab.
            # Console collection is opt-in because Runtime.enable() can be detected.
            await _auto_enable_network_collection(_browser.main_tab)
    return _browser


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "nodriver-mcp",
    instructions=(
        "Undetected Chrome browser automation via nodriver. "
        "Drop-in replacement for chrome-devtools-mcp that avoids CDP fingerprint detection. "
        "IMPORTANT: Always use take_snapshot instead of take_screenshot to read page content. "
        "take_snapshot returns searchable HTML text and is much faster and smaller. "
        "Only use take_screenshot when you specifically need a visual image for layout checks or visual regression. "
        "NOTE: The browser is launched lazily on the first tool call. "
        "The first invocation may take a few extra seconds for Chrome to start — this is normal, just wait for it."
    ),
)


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
_network_collection_enabled_tabs: set[int] = set()  # track which tabs have network collection enabled
_console_collection_enabled_tabs: set[int] = set()  # track which tabs have console collection enabled
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
        "accept_language": "en-US,en;q=0.9",
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
        "accept_language": "en-US,en;q=0.9",
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
        "accept_language": "en-US,en;q=0.9",
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

# Roles to collapse (skip node, promote children) — container noise
_COLLAPSE_ROLES: set[str] = {
    "generic", "list", "listitem", "paragraph", "strong", "emphasis", "code",
    "group", "Section", "blockquote", "figure", "mark", "subscript",
    "superscript", "insertion", "deletion", "DescriptionList",
    "DescriptionListTerm", "DescriptionListDetail", "time", "Abbr", "Ruby",
    "RubyAnnotation", "term", "definition", "feed", "log", "marquee",
    "timer", "directory", "tooltip",
}

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
    """Auto-enable network event collection on a tab."""
    tab_id = id(tab)
    if tab_id in _network_collection_enabled_tabs:
        return
    _network_collection_enabled_tabs.add(tab_id)

    import nodriver.cdp.network as cdp_net

    async def _on_request(event: cdp_net.RequestWillBeSent):
        global _request_counter
        try:
            _network_requests.append({
                "seq": _request_counter,
                "id": str(event.request_id),
                "url": event.request.url,
                "method": event.request.method,
                "timestamp": str(event.timestamp),
                "type": str(event.type_) if event.type_ else "unknown",
            })
            _request_counter += 1
            if len(_network_requests) > 1000:
                _network_requests.pop(0)
        except Exception:
            pass

    try:
        await tab.send(cdp_net.enable())
        tab.add_handler(cdp_net.RequestWillBeSent, _on_request)
    except Exception:
        pass


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

    try:
        await tab.send(cdp_runtime.enable())
        tab.add_handler(cdp_runtime.ConsoleAPICalled, _on_console)
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

    try:
        tab.remove_handler(cdp_runtime.ConsoleAPICalled)
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


async def _fill_element(tab: uc.Tab, uid: str, value: str) -> None:
    """Fill a single input/textarea/select by uid. Raises on failure."""
    import nodriver.cdp.runtime as cdp_runtime
    import nodriver.cdp.input_ as cdp_input

    remote_obj = await _resolve_uid(tab, uid)

    tag_obj = await _call_function_on(
        tab,
        function_declaration="function() { return this.tagName.toLowerCase(); }",
        object_id=remote_obj.object_id,
        return_by_value=True,
    )
    tag = tag_obj.value if tag_obj else ""

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
    else:
        await _call_function_on(
            tab,
            function_declaration=(
                "function() { this.focus(); this.value = ''; "
                "this.dispatchEvent(new Event('input', {bubbles: true})); }"
            ),
            object_id=remote_obj.object_id,
            return_by_value=True,
        )
        for char in value:
            await tab.send(cdp_input.dispatch_key_event(type_="keyDown", text=char))
            await tab.send(cdp_input.dispatch_key_event(type_="keyUp", text=char))
    await tab


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
                browser.connection.send(
                    cdp_target.create_browser_context(dispose_on_detach=False)
                ),
                timeout,
                f"Create isolated context '{isolated_context}'",
            )
            _named_browser_contexts[isolated_context] = ctx

        target_id = await _await_with_timeout(
            browser.connection.send(
                cdp_target.create_target(
                    url=url,
                    browser_context_id=ctx,
                    background=background,
                    for_tab=True,
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


async def _stop_browser() -> bool:
    """Stop the running browser (if any) and reset per-browser state, keeping the
    selected profile. Returns True if a browser was actually running."""
    global _browser, _selected_target_id
    was_running = _browser is not None and not _browser.stopped
    if _browser is not None:
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
    relaunches Chrome with it. Any open pages are closed."""
    global _selected_profile_dir, _selected_profile_name
    _selected_profile_dir = profile_dir
    _selected_profile_name = profile_name
    await _stop_browser()


# ---------------------------------------------------------------------------
# Tools (aligned with chrome-devtools-mcp interface)
# ---------------------------------------------------------------------------

@mcp.tool()
async def bypass_insecure_warning() -> str:
    """Click through the browser's insecure connection warning page."""
    tab = await _active_tab()
    await tab.bypass_insecure_connection_warning()
    return "Bypassed insecure connection warning."


@mcp.tool()
async def cf_verify() -> str:
    """Attempt to solve a Cloudflare verification challenge.

    Uses nodriver's built-in CF verification bypass.
    Requires opencv-python to be installed.
    """
    tab = await _active_tab()
    try:
        await tab.verify_cf()
        return "Cloudflare verification attempted."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def click(uid: str, dbl_click: bool = False, include_snapshot: bool = False) -> str:
    """Click on the provided element.

    Args:
        uid: The uid of an element on the page from the page content snapshot.
        dbl_click: Set to true for double clicks. Default is false.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _active_tab()
    try:
        cx, cy = await _get_box_model(tab, uid)
        if dbl_click:
            await _double_click(tab, cx, cy)
        else:
            await tab.mouse_click(cx, cy)
        await tab
        result = f"Clicked uid={uid}"
        result += await _maybe_snapshot(include_snapshot)
        return result
    except Exception as e:
        return f"Error clicking uid={uid}: {e}"


@mcp.tool()
async def click_at(x: int, y: int, dbl_click: bool = False, include_snapshot: bool = False) -> str:
    """Click at specific coordinates on the page.

    Args:
        x: The x coordinate.
        y: The y coordinate.
        dbl_click: Set to true for double clicks.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _active_tab()
    if dbl_click:
        await _double_click(tab, x, y)
    else:
        await tab.mouse_click(x, y)
    result = f"Clicked at ({x}, {y})"
    result += await _maybe_snapshot(include_snapshot)
    return result


@mcp.tool()
async def close_page(page_id: int = -1) -> str:
    """Closes the page by its index. The last open page cannot be closed.

    Args:
        page_id: The ID of the page to close. Default -1 closes current active page.
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


@mcp.tool()
async def close_browser() -> str:
    """Close the browser entirely (quit Chrome).

    Unlike close_page (which keeps the last tab open), this shuts down the whole
    browser. It relaunches automatically on the next tool call, using the
    currently selected profile.
    """
    running = await _stop_browser()
    return (
        "Browser closed. It will relaunch on the next action."
        if running else "No browser was running."
    )


@mcp.tool()
async def drag(from_uid: str, to_uid: str, include_snapshot: bool = False) -> str:
    """Drag an element onto another element.

    Args:
        from_uid: The uid of the element to drag.
        to_uid: The uid of the element to drop into.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
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


@mcp.tool()
async def emulate(
    network_conditions: str = "",
    cpu_throttling_rate: float = 0,
    geolocation: str | None = None,
    user_agent: str | None = None,
    color_scheme: str = "",
    viewport: str = "",
) -> str:
    """Emulates various features on the selected page.

    Args:
        network_conditions: Throttle network. Options: "Offline", "Slow 3G", "Fast 3G", "Slow 4G", "Fast 4G". Omit to disable.
        cpu_throttling_rate: CPU slowdown factor (1-20). Omit or set to 1 to disable.
        geolocation: Geolocation as "latitude,longitude" (e.g. "37.7749,-122.4194"). Omit to leave unchanged, or set to "" to clear.
        user_agent: User agent string. Omit to leave unchanged, or set to "" to clear.
        color_scheme: "dark", "light", or "auto". Empty to skip.
        viewport: Viewport as "widthxheightxdpr[,mobile][,touch][,landscape]" (e.g. "375x812x3,mobile,touch"). Empty to skip.
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


@mcp.tool()
async def reset_emulation() -> str:
    """Reset emulation overrides on the selected page back to browser defaults."""
    tab = await _active_tab()
    results = await _reset_emulation(tab)
    return "Emulation reset: " + ", ".join(results)


@mcp.tool()
async def emulate_device(
    device: str,
    color_scheme: str = "",
    network_conditions: str = "",
    cpu_throttling_rate: float = 0,
    geolocation: str | None = None,
) -> str:
    """Apply a named mobile/tablet device preset to the current page.

    Args:
        device: Device preset name or alias. Supported presets: pixel_7, pixel_7_landscape, ipad_air.
        color_scheme: "dark", "light", or "auto". Empty to skip.
        network_conditions: Throttle network. Options: "Offline", "Slow 3G", "Fast 3G", "Slow 4G", "Fast 4G".
        cpu_throttling_rate: CPU slowdown factor (1-20). Omit or set to 1 to disable.
        geolocation: Geolocation as "latitude,longitude" (e.g. "37.7749,-122.4194"). Omit to leave unchanged, or set to "" to clear.
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


@mcp.tool()
async def evaluate_script(function: str, args: list[str] | None = None) -> str:
    """Evaluate a JavaScript function inside the currently selected page.

    Args:
        function: A JavaScript function declaration to be executed.
            Example: "() => { return document.title }" or "(el) => { return el.innerText; }"
        args: An optional list of element uids from the snapshot to pass as arguments to the function.
    """
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
            return f"```json\n{json.dumps(value, default=str)}\n```"
        else:
            # Simple evaluation without element args
            # If user passed a function declaration, wrap it in a call
            expr = function.strip()
            if expr.startswith("(") or expr.startswith("function") or expr.startswith("async"):
                expr = f"({expr})()"
            result = await tab.evaluate(expr, await_promise=True)
            return f"```json\n{json.dumps(result, default=str)}\n```"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def fill(uid: str, value: str, include_snapshot: bool = False) -> str:
    """Type text into an input, text area or select an option from a <select> element.

    Args:
        uid: The uid of an element on the page from the page content snapshot.
        value: The value to fill in.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _active_tab()
    try:
        await _fill_element(tab, uid, value)
        result = f"Filled uid={uid} with \"{value}\""
        result += await _maybe_snapshot(include_snapshot)
        return result
    except Exception as e:
        return f"Error filling uid={uid}: {e}"


@mcp.tool()
async def fill_form(elements: list[dict], include_snapshot: bool = False) -> str:
    """Fill out multiple form elements at once.

    Args:
        elements: Elements from snapshot to fill out. Each has "uid" and "value" keys.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _active_tab()
    results = []
    for elem_spec in elements:
        uid = elem_spec.get("uid", "")
        value = elem_spec.get("value", "")
        try:
            await _fill_element(tab, uid, value)
            results.append(f"  uid={uid}: filled")
        except Exception as e:
            results.append(f"  uid={uid}: error — {e}")
    result = "Form fill results:\n" + "\n".join(results)
    result += await _maybe_snapshot(include_snapshot)
    return result


@mcp.tool()
async def get_console_message(msgid: int) -> str:
    """Gets a console message by its ID.

    Args:
        msgid: The message ID (0-based index) from list_console_messages.
    """
    tab = await _active_tab()
    if id(tab) not in _console_collection_enabled_tabs:
        return "Error: Console collection is disabled for the current page. Call enable_console_collection first."
    match = [m for m in _all_console_messages() if m.get("seq") == msgid]
    if not match:
        return f"Error: No console message with id {msgid}."
    msg = match[-1]
    return f"[{msg['type']}] {msg['text']} (timestamp: {msg['timestamp']})"


@mcp.tool()
async def get_cookies(url: str = "") -> str:
    """Get all cookies, optionally filtered by URL.

    Args:
        url: If provided, only return cookies for this URL.
    """
    tab = await _active_tab()
    import nodriver.cdp.network as cdp_net
    if url:
        cookies = await tab.send(cdp_net.get_cookies(urls=[url]))
    else:
        cookies = await tab.send(cdp_net.get_cookies())
    lines = [f"Cookies ({len(cookies)}):"]
    for c in cookies:
        lines.append(f"  {c.name}={c.value} (domain={c.domain}, path={c.path}, secure={c.secure})")
    return "\n".join(lines)


@mcp.tool()
async def get_local_storage() -> str:
    """Get all localStorage items from the current page."""
    tab = await _active_tab()
    data = await tab.get_local_storage()
    lines = ["localStorage items:"]
    for k, v in (data or {}).items():
        lines.append(f"  {k}: {str(v)[:200]}")
    return "\n".join(lines)


@mcp.tool()
async def get_network_request(reqid: int | None = None, request_file_path: str = "", response_file_path: str = "") -> str:
    """Gets a network request by an optional reqid.

    Args:
        reqid: The index of the network request from list_network_requests. If omitted, returns the latest request.
        request_file_path: Optional path to save the request body to.
        response_file_path: Optional path to save the response body to.
    """
    tab = await _active_tab()
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


@mcp.tool()
async def handle_dialog(action: str = "accept", prompt_text: str = "") -> str:
    """If a browser dialog was opened, use this command to handle it.

    Args:
        action: Whether to "accept" or "dismiss" the dialog.
        prompt_text: Optional text to enter into a prompt dialog.
    """
    if action not in {"accept", "dismiss"}:
        return f"Error: Unknown action '{action}'. Use 'accept' or 'dismiss'."
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


@mcp.tool()
async def hover(uid: str, include_snapshot: bool = False) -> str:
    """Hover over the provided element.

    Args:
        uid: The uid of an element on the page from the page content snapshot.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
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


@mcp.tool()
async def list_console_messages(
    page_size: int | None = None,
    page_idx: int = 0,
    types: list[str] | None = None,
    include_preserved_messages: bool = False,
) -> str:
    """List all console messages for the currently selected page since the last navigation.

    Args:
        page_size: Maximum number of messages to return. When omitted, returns all.
        page_idx: Page number (0-based) for pagination. Default is 0.
        types: Filter to specific message types (e.g. ["error", "warn"]). Omit for all.
        include_preserved_messages: Set to true to return the preserved messages over the last 3 navigations. Default is false.
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


@mcp.tool()
async def enable_console_collection() -> str:
    """Enable console event collection on the current page."""
    tab = await _active_tab()
    changed = await _enable_console_collection(tab)
    if changed:
        return "Console collection enabled on the current page."
    return "Console collection was already enabled on the current page."


@mcp.tool()
async def disable_console_collection() -> str:
    """Disable console event collection on the current page."""
    tab = await _active_tab()
    changed = await _disable_console_collection(tab)
    if changed:
        return "Console collection disabled on the current page."
    return "Console collection was already disabled on the current page."


@mcp.tool()
async def list_network_requests(
    page_size: int | None = None,
    page_idx: int = 0,
    resource_types: list[str] | None = None,
    url_filter: str = "",
    include_preserved_requests: bool = False,
) -> str:
    """List all requests for the currently selected page since the last navigation.

    Args:
        page_size: Maximum number of requests to return. When omitted, returns all.
        page_idx: Page number (0-based) for pagination. Default is 0.
        resource_types: Filter by resource types (e.g. ["xhr", "fetch"]). Omit for all.
        url_filter: Only return requests whose URL contains this string.
        include_preserved_requests: Set to true to return the preserved requests over the last 3 navigations. Default is false.
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


@mcp.tool()
async def list_pages() -> str:
    """Get a list of pages open in the browser."""
    browser = await _get_browser()
    await _refresh_targets(browser)
    lines = ["Open pages:"]
    for i, tab in enumerate(browser.tabs):
        url = tab.target.url or "about:blank"
        title = tab.target.title or ""
        lines.append(f"  [{i}] {url} — {title}")
    return "\n".join(lines)


@mcp.tool()
async def navigate_page(
    type: str = "url",
    url: str = "",
    ignore_cache: bool = False,
    handle_before_unload: str = "accept",
    init_script: str = "",
    timeout: int = 0,
    device: str = "",
) -> str:
    """Go to a URL, or back, forward, or reload.

    Args:
        type: One of "url", "back", "forward", "reload".
        url: Target URL (only for type=url).
        ignore_cache: Whether to ignore cache on reload.
        handle_before_unload: Whether to auto accept or decline beforeunload dialogs. Default is "accept".
        init_script: A JavaScript script to be executed on each new document before any other scripts for the next navigation.
        timeout: Maximum wait time in milliseconds. 0 for default timeout.
        device: Device preset name or alias to apply before navigation. Empty to leave unchanged.
    """
    tab = await _active_tab()

    if handle_before_unload not in {"accept", "dismiss"}:
        return f"Error: Unknown handle_before_unload '{handle_before_unload}'."
    if device and _resolve_device_preset(device) is None:
        supported = ", ".join(sorted(_DEVICE_PRESETS))
        return f"Error: Unknown device preset '{device}'. Supported presets: {supported}"
    if type not in {"url", "back", "forward", "reload"}:
        return f"Error: Unknown type '{type}'."
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

    async def _navigate() -> None:
        if type == "url":
            if not url:
                raise ValueError("URL is required for type=url.")
            await tab.get(url)
            await tab
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
        pages = await _format_pages()
        suffix = f" (pre-navigation emulation: {', '.join(device_results)})" if device_results else ""
        return f"Navigated to {tab.target.url or 'about:blank'}{suffix}{pages}"
    except Exception as e:
        return f"Error: {e}"
    finally:
        tab.remove_handler(cdp_page.JavascriptDialogOpening, _on_javascript_dialog)


@mcp.tool()
async def new_page(
    url: str = "about:blank",
    background: bool = False,
    isolated_context: str = "",
    timeout: int = 0,
    device: str = "",
) -> str:
    """Open a new tab and load a URL.

    Args:
        url: URL to load in a new page.
        background: Whether to open without bringing to front. Default is false.
        isolated_context: If specified, the page is created in an isolated browser context with the given name.
            Pages in the same browser context share cookies and storage.
            Pages in different browser contexts are fully isolated.
        timeout: Maximum wait time in milliseconds. 0 for default.
        device: Device preset name or alias to apply before the first real navigation request. Empty to leave unchanged.
    """
    global _selected_target_id
    if device and _resolve_device_preset(device) is None:
        supported = ", ".join(sorted(_DEVICE_PRESETS))
        return f"Error: Unknown device preset '{device}'. Supported presets: {supported}"

    browser = await _get_browser()
    previous_tab = await _active_tab()
    initial_url = "about:blank" if device and url != "about:blank" else url

    try:
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
        return f"Opened new page: {tab.target.url or 'about:blank'}{suffix}{pages}"
    except Exception as e:
        return f"Error opening new page: {e}"


@mcp.tool()
async def performance_start_trace(reload: bool = True, auto_stop: bool = True, file_path: str = "") -> str:
    """Start a performance trace on the selected webpage.

    Args:
        reload: Whether to reload the page after starting the trace. Default true.
        auto_stop: Whether to auto-stop after recording. Default true.
        file_path: Optional path to save the raw trace data.
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
    await tab.send(cdp_tracing.start(categories=",".join(categories), transfer_mode="ReturnAsStream"))
    _tracing_active = True

    if reload:
        await tab.reload()

    if auto_stop:
        await tab.sleep(5)
        return await performance_stop_trace(file_path=file_path)

    return "Trace started. Use performance_stop_trace to stop."


@mcp.tool()
async def performance_stop_trace(file_path: str = "") -> str:
    """Stop the active performance trace recording on the selected webpage.

    Args:
        file_path: Optional path to save the raw trace data (e.g. trace.json).
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

    if file_path and trace_chunks:
        with open(file_path, "w") as f:
            json.dump(trace_chunks, f)
        result += f" Saved to {file_path}"

    return result


@mcp.tool()
async def press_key(key: str, include_snapshot: bool = False) -> str:
    """Press a key or key combination.

    Args:
        key: A key or combination (e.g. "Enter", "Control+A", "Control+Shift+R").
            Modifiers: Control, Shift, Alt, Meta.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
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


@mcp.tool()
async def resize_page(width: int, height: int) -> str:
    """Resizes the selected page's window so that the page has specified dimension.

    Args:
        width: Page width in pixels.
        height: Page height in pixels.
    """
    tab = await _active_tab()
    await tab.set_window_size(width=width, height=height)
    return f"Resized to {width}x{height}."


@mcp.tool()
async def scroll_page(direction: str = "down", amount: int = 50) -> str:
    """Scroll the page up or down.

    Args:
        direction: "up" or "down".
        amount: Percentage of page to scroll (25 = quarter page).
    """
    tab = await _active_tab()
    if direction == "down":
        await tab.scroll_down(amount)
    else:
        await tab.scroll_up(amount)
    return f"Scrolled {direction} {amount}%."


@mcp.tool()
async def select_page(page_id: int, bring_to_front: bool = True) -> str:
    """Select a page as a context for future tool calls.

    Args:
        page_id: The ID of the page to select (from list_pages).
        bring_to_front: Whether to focus the page and bring it to the top. Default true.
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


@mcp.tool()
async def set_cookie(name: str, value: str, domain: str, path: str = "/", secure: bool = False) -> str:
    """Set a browser cookie.

    Args:
        name: Cookie name.
        value: Cookie value.
        domain: Cookie domain.
        path: Cookie path.
        secure: Whether the cookie is secure-only.
    """
    tab = await _active_tab()
    import nodriver.cdp.network as cdp_net
    success = await tab.send(cdp_net.set_cookie(
        name=name, value=value, domain=domain, path=path, secure=secure,
    ))
    return f"Cookie '{name}' set." if success else f"Failed to set cookie '{name}'."


@mcp.tool()
async def set_local_storage(items: dict[str, str]) -> str:
    """Set localStorage items on the current page.

    Args:
        items: Dict of key-value pairs to set in localStorage.
    """
    tab = await _active_tab()
    await tab.set_local_storage(items)
    return f"Set {len(items)} localStorage items."


@mcp.tool()
async def take_memory_snapshot(file_path: str) -> str:
    """Capture a heap snapshot for memory leak debugging.

    Args:
        file_path: Path to save the .heapsnapshot file.
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


@mcp.tool()
async def take_screenshot(
    full_page: bool = False,
    format: str = "png",
    quality: int = 0,
    uid: str = "",
    file_path: str = "",
) -> str:
    """Take a screenshot of the page or element.

    WARNING: Do NOT use this tool to read page content. Use take_snapshot instead
    which returns searchable HTML text. Only use take_screenshot when you
    specifically need a visual image (layout checks, visual regression, etc.).

    Args:
        full_page: If True, capture the entire page (not just viewport). Incompatible with uid.
        format: Image format — "png", "jpeg", or "webp". Default is "png".
        quality: Compression quality for JPEG/WebP (0-100). Ignored for PNG.
        uid: The uid of an element from snapshot to screenshot. If omitted, takes full page screenshot.
        file_path: Optional path to save the screenshot. If omitted, returns base64 data.
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


@mcp.tool()
async def take_snapshot(verbose: bool = False, file_path: str = "") -> str:
    """Take a text snapshot of the currently selected page based on the a11y tree.
    The snapshot lists page elements along with a unique identifier (uid).
    Always use the latest snapshot. Prefer taking a snapshot over taking a
    screenshot. The snapshot indicates searchable, structured text that is
    much smaller than an image or raw HTML.

    Args:
        verbose: Whether to include all possible information available in the full a11y tree. Default is false.
        file_path: Optional path to save the snapshot to instead of attaching it to the response.
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

        # Collapse container roles (skip node, promote children at same depth)
        if not verbose and role in _COLLAPSE_ROLES:
            name = ""
            if node.name and node.name.value:
                name = str(node.name.value)
            if not name:
                child_parts = []
                for cid in children_map.get(node_id, []):
                    child_parts.append(_format_node(cid, depth))
                return "".join(child_parts)

        name = ""
        if node.name and node.name.value:
            name = str(node.name.value)

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
            child_lines.append(_format_node(cid, depth + 1))

        return line + "".join(child_lines)

    output_parts = []
    for rid in root_ids:
        output_parts.append(_format_node(rid, 0))
    snapshot_text = "".join(output_parts)

    # Truncate if extremely large
    if len(snapshot_text) > 200_000:
        snapshot_text = snapshot_text[:200_000] + "\n... (truncated)"

    if file_path:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(snapshot_text)
        return f"Snapshot saved to {file_path} ({len(snapshot_text)} chars)."

    return snapshot_text


@mcp.tool()
async def type_text(text: str, submit_key: str = "") -> str:
    """Type text using keyboard input into a previously focused input.

    Args:
        text: The text to type.
        submit_key: Optional key to press after typing (e.g. "Enter", "Tab", "Escape").
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


@mcp.tool()
async def upload_file(uid: str, file_path: str, include_snapshot: bool = False) -> str:
    """Upload a file through a provided element.

    Args:
        uid: The uid of the file input element from the page content snapshot.
        file_path: The local path of the file to upload.
        include_snapshot: Whether to include a snapshot in the response. Default is false.
    """
    tab = await _active_tab()
    import nodriver.cdp.dom as cdp_dom

    backend_node_id = _uid_to_backend_node_id.get(uid)
    if backend_node_id is None:
        return f"Error: Unknown uid '{uid}'. Take a new snapshot first."

    await tab.send(cdp_dom.set_file_input_files(
        files=[file_path],
        backend_node_id=cdp_dom.BackendNodeId(backend_node_id),
    ))

    result = f"Uploaded {file_path} to uid={uid}"
    result += await _maybe_snapshot(include_snapshot)
    return result


@mcp.tool()
async def wait_for(text: list[str], timeout: int = 30000) -> str:
    """Wait for the specified text to appear on the selected page.

    Args:
        text: Non-empty list of texts. Resolves when any value appears on the page.
        timeout: Maximum wait time in milliseconds. Default is 30000.
    """
    tab = await _active_tab()
    timeout_s = timeout / 1000

    start = time.time()
    while time.time() - start < timeout_s:
        try:
            # Get page text content
            page_text = await tab.evaluate("document.body ? document.body.innerText : ''")
            if page_text:
                for t in text:
                    if t in page_text:
                        # Found — return with snapshot
                        snapshot = await take_snapshot()
                        return f"Found text \"{t}\" on page.\n\n{snapshot}"
        except Exception:
            pass
        await asyncio.sleep(0.5)

    return f"Timeout: None of the texts {text} appeared within {timeout}ms."


# ---------------------------------------------------------------------------
# Session management helpers
# ---------------------------------------------------------------------------

_SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".nodriver-mcp", "sessions")


def _ensure_sessions_dir() -> str:
    os.makedirs(_SESSIONS_DIR, exist_ok=True)
    return _SESSIONS_DIR


@mcp.tool()
async def save_session(name: str) -> str:
    """Save the current browser session (cookies, localStorage, open URLs) to a file.

    The session is stored as a JSON file under ~/.nodriver-mcp/sessions/.
    Use load_session to restore it later.

    Args:
        name: A human-readable name for this session (e.g. "xiaohongshu-logged-in").
    """
    tab = await _active_tab()
    browser = await _get_browser()
    import nodriver.cdp.network as cdp_net

    # 1. Collect all cookies
    raw_cookies = await tab.send(cdp_net.get_cookies())
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


@mcp.tool()
async def load_session(filename: str, restore_pages: bool = False) -> str:
    """Load a previously saved session, restoring cookies and localStorage.

    Args:
        filename: The session filename (from list_sessions) or full path.
        restore_pages: Whether to also re-open the saved page URLs. Default is false.
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

    # 1. Restore cookies
    cookies_restored = 0
    for c in session.get("cookies", []):
        try:
            kwargs: dict[str, Any] = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "http_only": c.get("httpOnly", False),
            }
            if c.get("expires"):
                kwargs["expires"] = c["expires"]
            if c.get("sameSite"):
                from nodriver.cdp.network import CookieSameSite
                kwargs["same_site"] = CookieSameSite(c["sameSite"])
            await tab.send(cdp_net.set_cookie(**kwargs))
            cookies_restored += 1
        except Exception as e:
            logger.warning("Failed to restore cookie %s: %s", c.get("name"), e)

    # 2. Restore localStorage — navigate to the saved URL first so the origin matches
    ls_items = session.get("localStorage", {})
    ls_restored = 0
    if ls_items:
        current_url = session.get("current_url", "")
        if current_url and current_url != "about:blank":
            try:
                await tab.get(current_url)
                await tab
            except Exception:
                pass
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

    return (
        f"Session '{session.get('name', '')}' loaded from {filepath}\n"
        f"  Cookies restored: {cookies_restored}\n"
        f"  localStorage items restored: {ls_restored}\n"
        f"  Pages re-opened: {pages_opened}"
    )


@mcp.tool()
async def list_sessions() -> str:
    """List all saved browser sessions.

    Returns the available session files with their names and save times.
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
# Chrome profile (user-data-dir) management
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_profiles() -> str:
    """List saved persistent Chrome profiles and show which profile is active.

    By default the browser uses a fresh ephemeral temp profile per session
    (auto-deleted), so multiple nodriver instances never collide on one profile.
    Persistent profiles let you reuse logins/cookies across sessions.
    """
    os.makedirs(_PROFILES_DIR, exist_ok=True)
    names = sorted(
        d for d in os.listdir(_PROFILES_DIR)
        if os.path.isdir(os.path.join(_PROFILES_DIR, d))
    )
    running = bool(_browser and not _browser.stopped)
    active = _selected_profile_name or "temp (ephemeral, auto-deleted)"
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


@mcp.tool()
async def create_profile(name: str, activate: bool = False) -> str:
    """Create a new named persistent Chrome profile (a reusable user-data dir).

    Args:
        name: Profile name (letters, digits, '-' or '_'), e.g. "google-login".
        activate: If true, switch the browser to this profile now (restarts the browser).
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


@mcp.tool()
async def use_temp_profile() -> str:
    """Switch to a fresh ephemeral temp profile (auto-created and deleted per
    session). This is the default, and lets many nodriver instances run at once
    without colliding. Restarts the browser (open pages are closed).
    """
    await _restart_browser_with(None, None)
    return "Switched to an ephemeral temp profile (auto-deleted after the session)."


@mcp.tool()
async def use_profile(name: str) -> str:
    """Switch the browser to a persistent profile by name. Restarts the browser
    (any open pages are closed). Pass "" or "temp" to return to an ephemeral
    temp profile.

    Args:
        name: The persistent profile name (see list_profiles), or "" / "temp".
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


@mcp.tool()
async def delete_profile(name: str) -> str:
    """Delete a persistent Chrome profile directory (cannot delete the active one).

    Args:
        name: The persistent profile name to delete.
    """
    safe = _safe_profile_name(name)
    if not safe:
        return "Error: invalid profile name."
    if _selected_profile_name == safe:
        return f"Error: '{safe}' is the active profile. Switch away first with use_temp_profile()."
    path = os.path.join(_PROFILES_DIR, safe)
    if not os.path.isdir(path):
        return f"Error: profile '{safe}' does not exist."
    import shutil
    try:
        shutil.rmtree(path)
    except Exception as e:
        return f"Error deleting profile '{safe}': {e}"
    return f"Deleted profile '{safe}'."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server via stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
