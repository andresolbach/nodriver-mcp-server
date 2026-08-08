"""Tests that need a real Chrome.

The rest of the suite asserts on schemas and docs, which is cheap and catches
drift but cannot catch a tool that reports success while doing nothing. This
module exists for the failures only a browser shows: the bug it was written for
sat behind a CDP method whose name promises more than it delivers, and every
schema in the project was perfectly correct while it was live.

Marked `slow` and excluded from the default run. Set NODRIVER_HEADLESS=true to
run them without windows appearing.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from nodriver_mcp.server import mcp

pytestmark = pytest.mark.slow


async def _call(tool, /, **arguments) -> str:
    """Invoke a tool the way a client does, and return its text.

    Positional-only, so a tool argument called `name` (set_cookie, save_session)
    does not collide with this function's own parameter.
    """
    result = await mcp.call_tool(tool, arguments)
    # Every tool here declares an output schema, so FastMCP hands back a
    # (content, structured) pair rather than content alone.
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        return str(result.get("result", result))
    return "\n".join(
        getattr(block, "text", "") for block in result if getattr(block, "text", None)
    )


def _run(scenario):
    """Run one scenario, then close the browser in the SAME event loop.

    The browser's websockets belong to the loop that opened them. Closing it
    from a second asyncio.run — a fixture, say — raises "Event loop is closed"
    and leaves Chrome running, so the next test inherits its cookies and this
    particular test starts lying.
    """

    async def main():
        try:
            await scenario()
        finally:
            await _call("close_browser")

    asyncio.run(main())


def test_get_cookies_reads_the_whole_jar_not_just_the_selected_tab():
    """Regression: shipped broken up to and including 1.9.3.

    get_cookies documents itself as reading the whole browser cookie jar unless
    given a url, but used Network.getCookies, which despite the name returns
    only the cookies of the tab it is sent to. Opening a second tab was enough
    to make a cookie set on the first vanish from a call claiming to list
    everything — `Cookies (0):` moments after set_cookie returned success.
    """

    async def scenario():
        await _call("new_page", url="https://example.com")
        await _call("set_cookie", name="first", value="AAA", domain="example.com")

        # The second tab becomes the selected one while the cookie belongs to
        # the first. That is the whole bug, and it needs two tabs to appear.
        await _call("new_page", url="https://example.org")
        await _call("set_cookie", name="second", value="BBB", domain="example.org")

        whole_jar = await _call("get_cookies")
        assert "first=AAA" in whole_jar, f"cookie from the unselected tab is missing:\n{whole_jar}"
        assert "second=BBB" in whole_jar

        # Passing url still narrows, via Network.getCookies, which is correct there.
        filtered = await _call("get_cookies", url="https://example.com")
        assert "first=AAA" in filtered
        assert "second=BBB" not in filtered, f"url filter stopped filtering:\n{filtered}"

    _run(scenario)


def test_close_browser_removes_the_throwaway_profile():
    """Regression: nodriver leaves this to an atexit handler that loses the race.

    Browser.stop() terminates Chrome and hands the profile to an atexit handler
    with 0.75s of retries, while Chrome releases its files asynchronously on
    Windows. Measured on one developer machine: 16 abandoned profiles holding
    1.8 GB. close_browser now waits for the process to exit before removing the
    directory itself.
    """
    from pathlib import Path

    from nodriver_mcp import server

    async def scenario():
        await _call("new_page", url="https://example.com")
        profile = Path(str(server._browser.config.user_data_dir))
        assert profile.is_dir(), f"browser reported no live profile dir: {profile}"
        assert not server._browser.config.uses_custom_data_dir, (
            "this test only means anything on a throwaway profile"
        )

        await _call("close_browser")
        assert not profile.exists(), f"temp profile survived close_browser: {profile}"

    # close_browser is the subject here, so no _run(): its teardown would run a
    # second one against a browser that is already gone.
    asyncio.run(scenario())


def test_save_session_stores_cookies_from_every_tab():
    """Regression: the same CDP call, in the costlier place.

    save_session collected cookies under a comment reading "Collect all
    cookies". A session saved with several logins open kept whichever site
    happened to be selected and dropped the rest, without saying so — a loss
    that only surfaces later, when load_session restores half a login.
    """

    async def scenario():
        await _call("new_page", url="https://example.com")
        await _call("set_cookie", name="first", value="AAA", domain="example.com")
        await _call("new_page", url="https://example.org")
        await _call("set_cookie", name="second", value="BBB", domain="example.org")

        saved = await _call("save_session", name="pytest-cookie-scope")
        try:
            assert "Cookies: 2" in saved, f"session did not keep both tabs' cookies:\n{saved}"
        finally:
            # Leave no credential file behind: a real one holds live tokens.
            match = re.search(r"saved to (.+\.json)", saved)
            if match:
                Path(match.group(1).strip()).unlink(missing_ok=True)

    _run(scenario)
