"""
Several independent browsers behind one server.

server.py keeps its whole world in module globals — the running browser, the
selected tab, collected console and network traffic, the chosen profile. That is
exactly right for one browser and unfixable for several: two agents sharing the
process would keep stealing each other's selected tab.

So a second browser is not a variable here, it is a process. This module owns the
tool surface, adds one `browser` argument to every tool, and routes each call:

    browser="default"  -> the FastMCP server in *this* process, unchanged
    browser=<anything>  -> a worker subprocess running server.py, one per name

The default costing nothing is the point. Someone who never opens a second
browser gets the same single process, the same Chrome and the same code path as
before; only the extra names pay for isolation. And that isolation is structural
rather than careful: separate processes mean separate globals, separate Chrome,
separate throwaway profile, and a Chrome that hangs or crashes takes down nothing
but its own worker.

Tool schemas are not redeclared here. They are read from the local server and
passed through with one property added, so every description, title and
annotation stays defined exactly once, in server.py.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import sys
import time
from datetime import timedelta
from typing import Any

import mcp.types as types
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from .server import mcp

logger = logging.getLogger("nodriver-mcp.browsers")

DEFAULT_BROWSER = "default"

# Serves list_browsers; clients have no use for it and never see it.
_HIDDEN_TOOLS = frozenset({"browser_status"})

# A tool call can legitimately take a while (slow page, long wait_for), but never
# forever. Without a cap, one wedged Chrome would hang its caller indefinitely.
_CALL_TIMEOUT_S = 300.0
# Answering list_browsers must never be held hostage by one unresponsive worker.
_STATUS_TIMEOUT_S = 10.0
# Spawning is a Python import, not a Chrome launch, so this is generous.
_SPAWN_TIMEOUT_S = 60.0
# A round trip to a worker that is merely idle takes microseconds; this only has
# to be long enough to tell "busy" from "gone".
_PING_TIMEOUT_S = 10.0

# Guard rail against an agent looping on new names until the machine dies. Each
# extra browser is a Python process plus a full Chrome. Counts the default.
# A 128 GB workstation and an 8 GB CI box should not be stuck with one number.
def _env_int(var: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(var, "") or default))
    except ValueError:
        logger.warning("%s is not an integer; using %d", var, default)
        return default


_MAX_BROWSERS = _env_int("NODRIVER_MAX_BROWSERS", 12, 2)

# close_browser keeps a browser's name, profile and flags on purpose, but nothing
# ever handed the name back, so twelve well-behaved agents that each opened and
# dutifully closed a browser wedged the server permanently. A name with no Chrome
# behind it and no calls for this long is collected. Set 0 to disable.
_IDLE_TTL_S = _env_int("NODRIVER_BROWSER_IDLE_TTL_S", 900, 0)
_REAP_INTERVAL_S = 60.0

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")

# Deliberately terse: this rides along on all sixty tools, so every word is paid
# for in every session, including the majority who never open a second browser.
# The reasoning, the hazards and the costs live in the server instructions
# instead, which are sent once.
_BROWSER_PARAM_DESCRIPTION = (
    "Which browser to act on. Each name is an independent Chrome, created on "
    "first use. Omit for the shared default; parallel agents each need their "
    "own name."
)

_MULTI_BROWSER_INSTRUCTIONS = f"""
Several independent browsers
  Every tool takes an optional `browser` argument. Omitting it uses the browser
  called "{DEFAULT_BROWSER}", which runs in the server process itself and behaves
  exactly like a single-browser session. Nothing needs to be configured, and one
  agent working alone can ignore this section entirely.

  Passing a name that does not exist yet creates a browser on the spot: its own
  Chrome, in its own process, with its own profile, cookies, tabs, snapshot uids
  and console/network capture. Nothing carries over between them, in either
  direction.

  When to use a second browser
    * Several agents or subtasks working at the same time. This is the main one.
    * Two accounts on one site at once, without one login evicting the other.
    * Work that must not touch a session you already have open.

  Parallel agents must not share a name. Two callers on one browser fight over
  the same selected tab, so a select_page or a navigation by one silently
  changes what the other sees, and every uid the other holds goes stale. Give
  each its own name, e.g. "agent-a" and "agent-b", and nothing can collide.

  What it costs
    Each extra browser is a Python process plus a full Chrome, roughly 200 MB
    and about a second to start. Up to {_MAX_BROWSERS} can be open at once,
    including the default. Prefer reusing a name over opening another.

  Managing them
    list_browsers      what is open: names, whether Chrome runs, profile, tabs.
    shutdown_browser   quits one browser's Chrome and frees its name.
    close_browser      quits Chrome but keeps the browser's settings, so the
                       next call relaunches it with the same profile and flags.
""".strip()


class _Worker:
    """A non-default browser: a subprocess running server.py, plus its session.

    The subprocess is owned by a single asyncio task from spawn to shutdown.
    stdio_client and ClientSession are anyio context managers whose cancel scopes
    belong to the task that entered them, so entering them in one tool call and
    exiting them in another would fail at runtime. Keeping the whole lifecycle
    inside `_run` is what lets the worker outlive the call that created it.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.session: ClientSession | None = None
        self.ready = asyncio.Event()
        self._stop = asyncio.Event()
        self.error: BaseException | None = None
        self.task: asyncio.Task[None] | None = None
        self.started_at = time.time()
        self.last_used = time.time()
        self.calls = 0
        self.last_tool: str | None = None
        # Set when a shutdown is requested, including one that arrives while
        # this worker is still starting up.
        self.retired = False

    @property
    def alive(self) -> bool:
        """Whether this worker *should* be usable.

        Cheap and optimistic: it cannot see a subprocess that died on its own,
        because nothing here observes the child. Use _responsive() before
        handing a worker to a caller.
        """
        return (
            not self.retired
            and self.task is not None
            and not self.task.done()
            and self.error is None
            and self.session is not None
        )

    async def _run(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "nodriver_mcp", "--worker"],
            env={**os.environ, "NODRIVER_BROWSER_NAME": self.name},
        )
        try:
            async with stdio_client(params, errlog=sys.stderr) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    self.ready.set()
                    await self._stop.wait()
        except BaseException as e:  # noqa: BLE001 - recorded, then surfaced to the caller
            self.error = e
            logger.warning("browser %r worker ended: %r", self.name, e)
        finally:
            self.session = None
            self.ready.set()

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name=f"nodriver-browser-{self.name}")
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=_SPAWN_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.shutdown()
            raise RuntimeError(
                f"Browser {self.name!r} did not come up within {_SPAWN_TIMEOUT_S:.0f}s."
            ) from None
        if self.error is not None or self.session is None:
            raise RuntimeError(f"Browser {self.name!r} failed to start: {self.error!r}")

    async def shutdown(self) -> None:
        """Close the MCP session, which closes the worker's stdin, which makes it
        exit — cleaning up its Chrome and temp profile on the way out."""
        self.retired = True
        self._stop.set()
        task = self.task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception:
                pass


_workers: dict[str, _Worker] = {}
_name_locks: dict[str, asyncio.Lock] = {}
_registry_lock = asyncio.Lock()


def _validate_name(name: str) -> str:
    name = (name or DEFAULT_BROWSER).strip() or DEFAULT_BROWSER
    if not _NAME_RE.match(name):
        raise RuntimeError(
            f"Invalid browser name {name!r}. Use letters, digits, dot, dash or "
            "underscore, starting with a letter or digit, up to 40 characters."
        )
    return name


async def _name_lock(name: str) -> asyncio.Lock:
    async with _registry_lock:
        return _name_locks.setdefault(name, asyncio.Lock())


async def _responsive(worker: _Worker) -> bool:
    """Whether the worker still answers, not merely whether we think it should.

    A worker's lifecycle task parks on an event that only this process sets, so
    a subprocess that dies on its own — crash, OOM, someone tidying up processes
    — leaves every flag saying "running". Without an actual round trip the
    registry would hand that corpse to every later call, each of which then
    waits out the full call timeout.
    """
    if not worker.alive or worker.session is None:
        return False
    try:
        await asyncio.wait_for(worker.session.send_ping(), timeout=_PING_TIMEOUT_S)
        return True
    except Exception:
        logger.info("browser %r stopped responding; starting a fresh one", worker.name)
        worker.error = worker.error or RuntimeError("worker stopped responding")
        return False


def _live_count() -> int:
    """Browsers counting against the cap, including the default."""
    return 1 + sum(1 for w in _workers.values() if not w.retired)


def _entries_by_name(entries: list[dict[str, Any] | None]) -> dict[str, dict[str, Any]]:
    return {e["browser"]: e for e in entries if e and "browser" in e}


def _reclaimable(worker: _Worker, status: dict[str, Any] | None) -> bool:
    """A registered name holding no Chrome and idle long enough to collect.

    close_browser deliberately keeps the name so its profile and flags survive,
    but nothing ever gave the name back, so a pool of well-behaved agents that
    each opened and dutifully closed a browser ratcheted the server to its cap
    and stayed there.
    """
    if worker.retired:
        return False
    # Never collect something whose age is unknown: reaping is a destructive act,
    # and "I cannot tell how idle this is" must mean "leave it alone".
    last_used = getattr(worker, "last_used", None)
    if last_used is None or time.time() - last_used < _IDLE_TTL_S:
        return False
    if status is None:
        return not worker.alive
    return status.get("chrome_running") is False or status.get("worker") in {
        "stopped", "unresponsive"
    }


async def _reap_idle(reason: str) -> list[str]:
    """Retire idle, Chrome-less browsers. Returns the names collected."""
    names = [n for n, w in _workers.items() if not w.retired]
    if not names:
        return []
    statuses = _entries_by_name(list(await asyncio.gather(*(_status(n) for n in names))))
    collected: list[str] = []
    for name in names:
        worker = _workers.get(name)
        if worker is None or not _reclaimable(worker, statuses.get(name)):
            continue
        lock = await _name_lock(name)
        # Never WAIT for another name's lock. This runs from _ensure_worker while
        # that call already holds its own name's lock, so two concurrent spawns
        # each trying to reap the other would deadlock. A locked name is being
        # spawned or shut down right now, which is reason enough to leave it.
        if lock.locked():
            continue
        async with lock:
            worker = _workers.get(name)
            # Re-check under the lock: a call may have arrived since the status.
            if worker is None or not _reclaimable(worker, statuses.get(name)):
                continue
            _workers.pop(name, None)
            await worker.shutdown()
            collected.append(name)
    if collected:
        logger.info("reaped idle browsers (%s): %s", reason, ", ".join(collected))
    return collected


async def _reaper() -> None:
    """Collect idle, Chrome-less browsers so the cap stops being a one-way ratchet."""
    if _IDLE_TTL_S <= 0:
        return
    while True:
        try:
            await asyncio.sleep(_REAP_INTERVAL_S)
            await _reap_idle("idle sweep")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a sweep failure must never end the server
            logger.warning("reaper sweep failed: %r", e)


async def _ensure_worker(name: str) -> _Worker:
    """The worker for this name, spawning it on first use.

    Locked per name rather than globally, so two agents opening two different
    browsers at the same time really do start in parallel instead of queueing
    behind each other's Chrome. The slot is published *before* the spawn, not
    after: while it was published afterwards, a name being started was invisible
    to everything else, so the cap could be passed by any number of concurrent
    first calls and a shutdown arriving mid-spawn reported "no such browser"
    and then let the Chrome finish coming up.
    """
    lock = await _name_lock(name)
    async with lock:
        worker = _workers.get(name)
        if worker is not None and await _responsive(worker):
            return worker
        if worker is not None:
            await worker.shutdown()  # dead or unresponsive; replace it
            _workers.pop(name, None)

        if _live_count() >= _MAX_BROWSERS:
            # Try to make room before refusing: a name whose Chrome is already
            # gone is holding a slot for nothing.
            await _reap_idle("at capacity")

        live = _live_count()
        if live >= _MAX_BROWSERS:
            open_names = ", ".join([DEFAULT_BROWSER, *sorted(_workers)])
            raise RuntimeError(
                f"Refusing to open more than {_MAX_BROWSERS} browsers at once "
                f"({live} are open: {open_names}), and none could be reclaimed. "
                f"Release one with shutdown_browser(browser=<name>) — note that "
                "close_browser keeps its name and its slot — or reuse an existing name."
            )

        worker = _Worker(name)
        _workers[name] = worker  # claim the slot before the slow part
        try:
            await worker.start()
        except BaseException:
            _workers.pop(name, None)
            raise
        if worker.retired:
            # A shutdown arrived while this was starting. It waited on the same
            # lock, so honour it rather than handing back a browser the caller
            # was told is gone.
            _workers.pop(name, None)
            await worker.shutdown()
            raise RuntimeError(
                f"Browser {name!r} was shut down while it was starting. "
                "Call again to open a fresh one."
            )
        logger.info("browser %r started (%d open)", name, live + 1)
        return worker


# ---------------------------------------------------------------------------
# Calling a tool, locally or in a worker, with one result shape
# ---------------------------------------------------------------------------

def _as_result(raw: Any) -> types.CallToolResult:
    """Normalise what FastMCP returns into what the protocol sends.

    FastMCP hands back (content, structured) for a tool with an output schema,
    a bare dict for structured-only, or a plain sequence of blocks. Every tool
    here declares an output schema, so dropping the structured half would fail
    validation at the client.
    """
    if isinstance(raw, types.CallToolResult):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2:
        content, structured = raw
        return types.CallToolResult(content=list(content), structuredContent=structured)
    if isinstance(raw, dict):
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(raw, indent=2))],
            structuredContent=raw,
        )
    return types.CallToolResult(content=list(raw))


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], isError=True
    )


def _text(result: types.CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


async def _call_local(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Run a tool against the browser living in this process.

    The exception is reported as-is. FastMCP already raises ToolError with an
    "Error executing tool <name>:" prefix, so adding one here produced that
    sentence twice — on the default browser only, which is precisely where
    nothing was supposed to change.
    """
    try:
        return _as_result(await mcp.call_tool(name, arguments))
    except Exception as e:  # noqa: BLE001 - reported to the caller, never fatal here
        return _error(str(e))


async def _call_worker(
    worker: _Worker, name: str, arguments: dict[str, Any]
) -> types.CallToolResult:
    assert worker.session is not None
    worker.calls += 1
    worker.last_tool = name
    # Only real work counts. _status() must not keep a browser looking busy, or
    # the reaper would never collect anything an operator is watching.
    worker.last_used = time.time()
    try:
        return await worker.session.call_tool(
            name, arguments, read_timeout_seconds=timedelta(seconds=_CALL_TIMEOUT_S)
        )
    except Exception as e:  # noqa: BLE001 - reported to the caller, never fatal here
        return _error(f"[browser {worker.name!r}] {name} failed: {e}")


# ---------------------------------------------------------------------------
# The two tools this layer serves itself
# ---------------------------------------------------------------------------

_LIST_BROWSERS = types.Tool(
    name="list_browsers",
    title="List browsers",
    description=(
        "List every browser this server currently holds: its name, whether its "
        "Chrome is running, which profile it uses, how many tabs it has and what "
        "they show. Reports without starting anything.\n\n"
        'There is always a browser called "default", which runs in the server '
        "process. Others are created implicitly, by passing a new name as the "
        "`browser` argument of any tool, so this only shows names already used. "
        "A name missing here is not an error; it simply has not been opened.\n\n"
        "chrome_running=false means the browser exists but has no Chrome, either "
        "because no tool has needed one yet or because close_browser was called. "
        "The next call relaunches it."
    ),
    inputSchema={"type": "object", "properties": {}},
    annotations=types.ToolAnnotations(
        title="List browsers", readOnlyHint=True, openWorldHint=False
    ),
)

_SHUTDOWN_BROWSER = types.Tool(
    name="shutdown_browser",
    title="Shut down a browser",
    description=(
        "Quit one browser's Chrome and free its name, releasing all memory "
        "behind it. Use it when an agent is finished with its browser.\n\n"
        "The difference to close_browser: close_browser quits Chrome but keeps "
        "the browser, so its selected profile and runtime flags survive and the "
        "next call relaunches Chrome with them. shutdown_browser discards that "
        "too, and the name starts fresh if it is ever used again.\n\n"
        'The "default" browser cannot be retired, since it is the server itself; '
        "shutting it down quits its Chrome and resets it. A browser you attached "
        "to with use_running_browser is only detached from, never closed: it "
        "belongs to the user."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "browser": {
                "type": "string",
                "default": DEFAULT_BROWSER,
                "description": "Name of the browser to shut down.",
            }
        },
    },
    annotations=types.ToolAnnotations(
        title="Shut down a browser", destructiveHint=True, idempotentHint=True
    ),
)


# Teardown must never bring a browser into existence. Every other tool keeps the
# documented create-on-first-use behaviour — "a name that does not exist yet
# creates one on the spot" is the feature, and read-only tools are part of it.
# Cleanup is the exception because it has to work precisely when the server is at
# capacity, which is the one moment the creation path refuses.
_NO_ALLOC: frozenset[str] = frozenset({"close_browser"})

# name -> the arguments it accepts. Built once, from the schemas the server
# itself advertises, so it cannot drift from them.
_ACCEPTED_ARGS: dict[str, set[str]] = {}


async def _accepted_args(name: str) -> set[str] | None:
    """Which arguments this tool takes, or None if the tool is unknown here."""
    if not _ACCEPTED_ARGS:
        try:
            for tool in await mcp.list_tools():
                schema = tool.inputSchema or {}
                _ACCEPTED_ARGS[tool.name] = set((schema.get("properties") or {}).keys())
            for tool in (_LIST_BROWSERS, _SHUTDOWN_BROWSER):
                schema = tool.inputSchema or {}
                _ACCEPTED_ARGS[tool.name] = set((schema.get("properties") or {}).keys())
        except Exception as e:  # noqa: BLE001 - never block a call over this
            logger.warning("could not read tool schemas for argument checking: %r", e)
            return None
    return _ACCEPTED_ARGS.get(name)


def _decorate(tool: types.Tool) -> types.Tool:
    """Pass a tool through with the `browser` selector added."""
    schema = copy.deepcopy(tool.inputSchema) or {"type": "object", "properties": {}}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})["browser"] = {
        "type": "string",
        "default": DEFAULT_BROWSER,
        "description": _BROWSER_PARAM_DESCRIPTION,
    }
    return tool.model_copy(update={"inputSchema": schema})


async def _status(name: str) -> dict[str, Any] | None:
    """Live state of one browser, a marker if it stopped answering, or None if
    it vanished while the report was being assembled.

    The name list is snapshotted before the first await, and another call can
    retire a browser in between; indexing _workers here raised a bare KeyError
    that surfaced to the caller as a one-character error message.
    """
    if name == DEFAULT_BROWSER:
        base: dict[str, Any] = {"browser": name, "runs_in": "the server process"}
        result = await _call_local("browser_status", {})
        if result.isError:
            return {**base, "error": _text(result)}
        return {**base, **json.loads(_text(result))}

    worker = _workers.get(name)
    if worker is None:
        return None
    base = {
        "browser": name,
        "runs_in": "a worker process",
        "uptime_s": round(time.time() - worker.started_at),
        "tool_calls": worker.calls,
        "last_tool": worker.last_tool,
    }
    if not worker.alive or worker.session is None:
        return {**base, "worker": "stopped", "chrome_running": False}
    try:
        result = await asyncio.wait_for(
            worker.session.call_tool("browser_status", {}), timeout=_STATUS_TIMEOUT_S
        )
        return {**base, "worker": "running", **json.loads(_text(result))}
    except Exception as e:  # noqa: BLE001 - an unresponsive worker is a status, not a crash
        return {**base, "worker": "unresponsive", "error": str(e)}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def _instructions() -> str:
    """The single-browser instructions, plus how to use more than one."""
    return f"{(mcp.instructions or '').strip()}\n\n{_MULTI_BROWSER_INSTRUCTIONS}"


app: Server = Server("nodriver-mcp", instructions=_instructions())


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    """Read from the local server, so no subprocess starts just to be listed."""
    tools = [
        _decorate(t) for t in await mcp.list_tools() if t.name not in _HIDDEN_TOOLS
    ]
    return [*tools, _LIST_BROWSERS, _SHUTDOWN_BROWSER]


# Resources and prompts are forwarded rather than dropped. FastMCP registers
# these handlers whether or not anything is defined, so the server used to
# advertise both capabilities and answer with an empty list. Serving only tools
# here would turn that into "Method not found" for any client that probes —
# a regression nobody asked for, in exchange for nothing.

@app.list_resources()
async def list_resources() -> list[types.Resource]:
    return await mcp.list_resources()


@app.list_resource_templates()
async def list_resource_templates() -> list[types.ResourceTemplate]:
    return await mcp.list_resource_templates()


@app.read_resource()
async def read_resource(uri: types.AnyUrl):
    return await mcp.read_resource(uri)


@app.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    return await mcp.list_prompts()


@app.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    return await mcp.get_prompt(name, arguments)


@app.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Route one tool call to the browser it named.

    Returns a CallToolResult rather than bare content: the lowlevel server passes
    that through untouched, which is the only way a worker's structured output
    survives the trip without being rebuilt from its text.
    """
    args = dict(arguments or {})
    try:
        target = _validate_name(str(args.pop("browser", "") or DEFAULT_BROWSER))
    except RuntimeError as e:
        return _error(str(e))

    # Pydantic drops unknown fields silently, so a misspelled argument used to
    # vanish and the tool ran with its defaults — block_resources(resource_types=…)
    # answered "Resource blocking disabled", a success message for a call that
    # asked for the opposite. Say which argument was not understood instead.
    accepted = await _accepted_args(name)
    if accepted is not None:
        unknown = sorted(set(args) - accepted)
        if unknown:
            known = ", ".join(sorted(accepted | {"browser"})) or "(none)"
            return _error(
                f"{name} does not take {', '.join(repr(u) for u in unknown)}. "
                f"It accepts: {known}."
            )

    if name == "list_browsers":
        names = [DEFAULT_BROWSER, *sorted(_workers)]
        # Gathered, not awaited one at a time: eleven wedged workers used to cost
        # eleven status timeouts in a row — exactly the state you call this in.
        entries = list(await asyncio.gather(*(_status(n) for n in names)))
        # One definition of "live", shared with the cap at _ensure_worker, so the
        # number reported and the number enforced cannot drift apart.
        live = _live_count()
        reclaimable = sorted(
            n for n, w in _workers.items()
            if not w.retired and _reclaimable(w, _entries_by_name(entries).get(n))
        )
        report = {
            "browsers": [e for e in entries if e is not None],
            "open": live,
            "max": _MAX_BROWSERS,
            "slots_free": max(0, _MAX_BROWSERS - live),
            "hint": (
                "Pass browser=<new name> to any tool to open another independent "
                "browser. Nothing needs to be created first."
                if live < _MAX_BROWSERS
                else (
                    "At capacity. "
                    + (
                        f"Reclaimable now (no Chrome running): {', '.join(reclaimable)}. "
                        "They are collected automatically, or free one immediately with "
                        "shutdown_browser(browser=<name>)."
                        if reclaimable
                        else "Release your own with shutdown_browser(browser=<your name>)."
                    )
                )
            ),
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(report, indent=2))]
        )

    if name == "shutdown_browser":
        if target == DEFAULT_BROWSER:
            result = await _call_local("close_browser", {})
            return types.CallToolResult(
                content=[types.TextContent(
                    type="text",
                    text=(
                        f"Quit the {DEFAULT_BROWSER!r} browser's Chrome. Its name "
                        "stays, because it is the server process itself; the next "
                        "call starts a fresh Chrome.\n" + _text(result)
                    ),
                )],
                isError=result.isError,
            )
        # Under the same lock as the spawn. Popping without it raced a first
        # call: the name was invisible until start() returned, so this answered
        # "nothing to shut down" and then let the Chrome finish coming up.
        lock = await _name_lock(target)
        async with lock:
            worker = _workers.pop(target, None)
            if worker is None:
                return types.CallToolResult(content=[types.TextContent(
                    type="text",
                    text=f"No browser named {target!r} is open; nothing to shut down.",
                )])
            await worker.shutdown()
        return types.CallToolResult(content=[types.TextContent(
            type="text", text=f"Browser {target!r} shut down; its Chrome is gone."
        )])

    if target == DEFAULT_BROWSER:
        return await _call_local(name, args)

    # Tearing down is not the same as allocating. close_browser used to run
    # through the same get-or-create path as new_page, so closing a name that was
    # never opened answered with the *creation* cap error — a cleanup call that
    # fails exactly when cleanup matters — and spawned a whole subprocess, which
    # then held a slot for the life of the server, whenever the name was a typo.
    if name in _NO_ALLOC and target not in _workers:
        return types.CallToolResult(content=[types.TextContent(
            type="text",
            text=f"No browser named {target!r} is open; nothing to close.",
        )])

    try:
        worker = await _ensure_worker(target)
    except RuntimeError as e:
        return _error(str(e))
    return await _call_worker(worker, name, args)


async def _serve() -> None:
    async with stdio_server() as (read, write):
        reaper = asyncio.create_task(_reaper(), name="nodriver-browser-reaper")
        try:
            await app.run(
                read,
                write,
                InitializationOptions(
                    server_name="nodriver-mcp",
                    server_version=_version(),
                    capabilities=app.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                    instructions=_instructions(),
                ),
            )
        finally:
            reaper.cancel()
            try:
                await reaper
            except (asyncio.CancelledError, Exception):
                pass
            await _shutdown_everything()


async def _shutdown_everything() -> None:
    """Leave no Chrome and no temp profile behind on an orderly exit."""
    for worker in list(_workers.values()):
        try:
            await worker.shutdown()
        except Exception:
            pass
    _workers.clear()
    try:
        from .server import _stop_browser

        await _stop_browser()
    except Exception:
        logger.debug("closing the default browser failed", exc_info=True)


def _version() -> str:
    """This package's version, which is what a client displays.

    FastMCP left this unset, and the SDK then falls back to the version of the
    `mcp` library itself — so the server used to introduce itself as "1.26.0",
    the SDK's version, no matter which release was running.
    """
    try:
        from importlib.metadata import version

        return version("nodriver-mcp")
    except Exception:
        return "unknown"


def main() -> None:
    """Run the MCP server over stdio."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    # Once, on behalf of every worker: they skip it themselves, so a dozen
    # processes do not race over the same directories.
    from .server import sweep_stale_temp_profiles

    try:
        sweep_stale_temp_profiles()
    except Exception:
        logger.debug("temp profile sweep failed", exc_info=True)
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
