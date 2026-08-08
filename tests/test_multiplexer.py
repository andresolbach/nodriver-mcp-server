"""Tests for the routing layer that puts several browsers behind one server.

Most of these guard the same thing from different angles: a session that never
mentions `browser` must be indistinguishable from the single-browser server this
grew out of. That is the promise made to every existing user, and it is the one
thing no amount of new-feature testing would notice breaking.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import mcp.types as types
import pytest

from nodriver_mcp import multiplexer as mux
from nodriver_mcp.server import mcp

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = timedelta(seconds=180)


# ---------------------------------------------------------------------------
# The tool surface a client sees
# ---------------------------------------------------------------------------

def _tools():
    return asyncio.run(mux.list_tools())


def test_every_browser_tool_is_still_offered():
    """Adding the routing layer must not drop or rename anything."""
    own = {t.name for t in asyncio.run(mcp.list_tools())} - mux._HIDDEN_TOOLS
    offered = {t.name for t in _tools()}
    assert own - offered == set(), f"tools lost behind the router: {own - offered}"
    assert offered - own == {"list_browsers", "shutdown_browser"}


def test_internal_tools_are_not_exposed():
    assert "browser_status" in mux._HIDDEN_TOOLS
    assert "browser_status" not in {t.name for t in _tools()}


def test_descriptions_and_annotations_survive_the_router():
    """The router must not become a second place where tool docs live."""
    original = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for tool in _tools():
        if tool.name in ("list_browsers", "shutdown_browser"):
            continue
        source = original[tool.name]
        assert tool.description == source.description
        assert tool.annotations == source.annotations
        assert tool.title == source.title
        assert tool.outputSchema == source.outputSchema


def test_every_tool_gained_an_optional_browser_argument():
    for tool in _tools():
        if tool.name == "list_browsers":
            continue
        props = tool.inputSchema["properties"]
        assert "browser" in props, f"{tool.name} cannot be pointed at a browser"
        assert "browser" not in tool.inputSchema.get("required", []), (
            f"{tool.name} made the browser argument mandatory"
        )


def test_original_parameters_are_untouched():
    original = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for tool in _tools():
        if tool.name in ("list_browsers", "shutdown_browser"):
            continue
        before = original[tool.name].inputSchema
        after = tool.inputSchema
        assert set(after["properties"]) - set(before.get("properties", {})) == {"browser"}
        assert after.get("required", []) == before.get("required", [])


def test_the_browser_argument_stays_cheap():
    """This description rides on all sixty tools, in every session.

    At ninety words it cost roughly 7000 tokens of pure repetition before a
    single tool was called, most of it paid by people who never open a second
    browser. The teaching belongs in the instructions, which are sent once.
    """
    words = len(mux._BROWSER_PARAM_DESCRIPTION.split())
    assert words <= 35, f"the per-tool browser description grew to {words} words"
    total = sum(
        len(t.inputSchema["properties"].get("browser", {}).get("description", "").split())
        for t in _tools()
    )
    assert total <= 2500, f"{total} words spent on one repeated parameter"


def test_the_instructions_carry_what_the_parameter_no_longer_says():
    text = mux._instructions()
    assert (mcp.instructions or "").strip() in text, "single-browser guidance was dropped"
    for topic in ("list_browsers", "shutdown_browser", "own name", "costs"):
        assert topic in text, f"the instructions never mention {topic!r}"


def test_resources_and_prompts_are_still_answered():
    """Regression: serving only tools broke two methods that used to work.

    FastMCP registers resource and prompt handlers whether or not anything is
    defined, so this server advertised both capabilities and answered with an
    empty list. The first cut of the routing layer registered tools alone, which
    turned that into "Method not found" for any client that probes — measured
    against the old entry point, not assumed.
    """
    assert asyncio.run(mux.list_resources()) == asyncio.run(mcp.list_resources())
    assert asyncio.run(mux.list_prompts()) == asyncio.run(mcp.list_prompts())
    assert asyncio.run(mux.list_resource_templates()) == asyncio.run(
        mcp.list_resource_templates()
    )


def test_the_advertised_capabilities_match_the_single_browser_server():
    from mcp.server.lowlevel import NotificationOptions

    opts = NotificationOptions()
    before = mcp._mcp_server.get_capabilities(opts, {})
    after = mux.app.get_capabilities(opts, {})
    assert after.prompts == before.prompts
    assert after.resources == before.resources
    assert after.tools == before.tools


def test_decorate_does_not_mutate_the_source_schema():
    """Schemas come from the live server; mutating one poisons every later list."""
    original = {"type": "object", "properties": {"url": {"type": "string"}}}
    mux._decorate(types.Tool(name="t", description="d" * 90, inputSchema=original))
    assert "browser" not in original["properties"]


# ---------------------------------------------------------------------------
# Result normalisation — the new failure surface for existing users
# ---------------------------------------------------------------------------

def test_structured_output_is_preserved():
    """Every tool declares an output schema, so dropping the structured half
    would make the client reject a perfectly good answer."""
    result = mux._as_result(([types.TextContent(type="text", text="hi")], {"result": "hi"}))
    assert result.structuredContent == {"result": "hi"}
    assert result.content[0].text == "hi"
    assert not result.isError


def test_structured_only_results_get_readable_text():
    result = mux._as_result({"result": "hi"})
    assert result.structuredContent == {"result": "hi"}
    assert "hi" in result.content[0].text


def test_plain_content_results_pass_through():
    result = mux._as_result([types.TextContent(type="text", text="hi")])
    assert result.content[0].text == "hi"
    assert result.structuredContent is None


def test_a_worker_result_is_forwarded_verbatim():
    original = types.CallToolResult(
        content=[types.TextContent(type="text", text="hi")],
        structuredContent={"result": "hi"},
    )
    assert mux._as_result(original) is original


def test_a_failing_local_tool_becomes_an_error_result_not_a_crash():
    result = asyncio.run(mux._call_local("no_such_tool", {}))
    assert result.isError
    assert "no_such_tool" in mux._text(result)


# ---------------------------------------------------------------------------
# Browser names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["default", "agent-a", "a", "A.1", "x_y-z", "a" * 40])
def test_valid_names_are_accepted(name):
    assert mux._validate_name(name) == name


@pytest.mark.parametrize("name", ["bad name", "-lead", "a/b", "a" * 41, "..", "a;b"])
def test_invalid_names_are_rejected(name):
    with pytest.raises(RuntimeError, match="Invalid browser name"):
        mux._validate_name(name)


@pytest.mark.parametrize("name", ["", "   ", None])
def test_a_missing_name_means_the_default_browser(name):
    assert mux._validate_name(name) == mux.DEFAULT_BROWSER


def test_an_invalid_name_is_reported_rather_than_raised():
    result = asyncio.run(mux.call_tool("list_pages", {"browser": "no good"}))
    assert result.isError and "Invalid browser name" in mux._text(result)


# ---------------------------------------------------------------------------
# End to end, with real browsers
# ---------------------------------------------------------------------------

def _text(result) -> str:
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


class _Client:
    """The server, driven over stdio the way a real client drives it."""

    async def __aenter__(self):
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "nodriver_mcp"],
            cwd=str(ROOT), env={**os.environ, "NODRIVER_HEADLESS": "true"},
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        self.init = await self.session.initialize()
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def call(self, tool, /, **args):
        # Positional-only, so a tool argument named `name` does not collide.
        return _text(await self.session.call_tool(tool, args, read_timeout_seconds=TIMEOUT))


@pytest.mark.slow
def test_a_default_only_session_starts_no_subprocess():
    """The promise to everyone who never asked for this feature.

    Listing tools and driving the default browser must stay one process and one
    Chrome. If the router ever spawned a worker to answer these, every existing
    user would silently pay for a feature they are not using.
    """

    async def scenario():
        async with _Client() as c:
            await c.session.list_tools()
            await c.call("new_page", url="https://example.com")
            status = json.loads(await c.call("list_browsers"))
            assert len(status["browsers"]) == 1, status
            entry = status["browsers"][0]
            assert entry["browser"] == "default"
            assert entry["runs_in"] == "the server process"
            assert entry["chrome_running"] is True
            await c.call("shutdown_browser")

    asyncio.run(scenario())


@pytest.mark.slow
def test_a_second_browser_is_independent_and_concurrent():
    async def scenario():
        async with _Client() as c:
            loop = asyncio.get_running_loop()
            start = loop.time()
            await asyncio.gather(
                c.call("new_page", url="https://example.com"),
                c.call("new_page", url="https://example.org", browser="other"),
            )
            concurrent = loop.time() - start
            try:
                await c.call("set_cookie", name="who", value="AAA", domain="example.com")
                await c.call("set_cookie", name="who", value="BBB",
                             domain="example.org", browser="other")
                assert "who=AAA" in await c.call("get_cookies")
                assert "who=BBB" in await c.call("get_cookies", browser="other")

                # Navigating one must leave the other exactly where it was.
                await c.call("new_page", url="https://example.net")
                assert len([l for l in (await c.call("list_pages")).splitlines()
                            if "http" in l]) == 2
                assert len([l for l in (await c.call("list_pages", browser="other")).splitlines()
                            if "http" in l]) == 1

                status = json.loads(await c.call("list_browsers"))
                by = {b["browser"]: b for b in status["browsers"]}
                assert by["default"]["runs_in"] == "the server process"
                assert by["other"]["runs_in"] == "a worker process"
                assert by["default"]["pid"] != by["other"]["pid"]
            finally:
                await c.call("shutdown_browser", browser="other")
                await c.call("shutdown_browser")
            assert concurrent < 40, f"the two launches did not overlap ({concurrent:.1f}s)"

    asyncio.run(scenario())


@pytest.mark.slow
def test_errors_read_the_same_whichever_browser_raised_them():
    """A worker's failure must not arrive dressed differently from a local one,
    or an agent learns two vocabularies for the same problem."""

    async def scenario():
        async with _Client() as c:
            await c.call("new_page", url="https://example.com", browser="other")
            try:
                local = await c.call("click", uid="nope")
                remote = await c.call("click", uid="nope", browser="other")
                assert "unknown uid" in local and "unknown uid" in remote
            finally:
                await c.call("shutdown_browser", browser="other")
                await c.call("shutdown_browser")

    asyncio.run(scenario())


@pytest.mark.slow
def test_shutting_down_a_browser_removes_its_temp_profile():
    """Both paths, since they clean up through different code."""

    async def scenario():
        async with _Client() as c:
            await c.call("new_page", url="https://example.com")
            await c.call("new_page", url="https://example.org", browser="other")
            status = json.loads(await c.call("list_browsers"))
            dirs = {b["browser"]: b["user_data_dir"] for b in status["browsers"]}
            assert all(d and Path(d).is_dir() for d in dirs.values()), dirs

            await c.call("shutdown_browser", browser="other")
            await c.call("shutdown_browser")
            for name, path in dirs.items():
                assert not Path(path).exists(), f"{name} left {path} behind"

            # The ownership notes must go with them. A worker that leaves one
            # behind is a worker whose browser was reaped by nodriver's atexit
            # handler rather than shut down deliberately.
            from nodriver_mcp.server import _claim_path

            for name, path in dirs.items():
                assert not Path(_claim_path(path)).exists(), (
                    f"{name} left its ownership claim behind"
                )

    asyncio.run(scenario())
