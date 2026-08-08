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


def test_load_session_restores_the_cookies_it_saved():
    """Regression: the advertised round trip restored nothing, and said so cheerfully.

    load_session passed the raw JSON float from `expires` into
    cdp.network.set_cookie, whose generator calls `expires.to_json()`. A float
    has no to_json, so every cookie raised AttributeError into a logger.warning
    on stderr that no MCP client ever sees, and the counter — incremented only
    on the success path — reported "Cookies restored: 0" as a normal result.

    save_session keeps CDP's -1 sentinel for session cookies, and -1 is truthy,
    so 100% of cookies took the failing branch. An agent following the
    documented "arrive already logged in" path silently scraped the logged-out
    view of every site.
    """

    async def scenario():
        await _call("new_page", url="https://example.com")
        await _call("set_cookie", name="restored", value="XYZ", domain="example.com")

        saved = await _call("save_session", name="pytest-load-roundtrip")
        match = re.search(r"saved to (.+\.json)", saved)
        assert match, f"could not find the session path in:\n{saved}"
        path = Path(match.group(1).strip())

        try:
            # A temp profile discards its cookie jar, which is what makes this a
            # real round trip rather than a no-op.
            await _call("close_browser")
            await _call("new_page", url="https://example.com")
            assert "restored=XYZ" not in await _call("get_cookies"), (
                "the cookie survived close_browser, so this proves nothing"
            )

            loaded = await _call("load_session", filename=path.name)
            assert "Cookies restored: 0" not in loaded, loaded

            jar = await _call("get_cookies")
            assert "restored=XYZ" in jar, (
                f"load_session reported success but the jar is empty:\n{loaded}\n{jar}"
            )
        finally:
            path.unlink(missing_ok=True)

    _run(scenario)


def test_evaluate_script_returns_plain_json_for_objects():
    """Regression: it returned CDP's deep-serialization wire format.

    nodriver's Tab.evaluate asks for "deep" serialization, so {"a": 1} came back
    as [["a", {"type": "number", "value": 1}]] — several times the tokens, not
    what the tool's own description promises, and something every caller had to
    write a decoder for. Primitives were unaffected, so the shape silently
    changed with the return value.
    """

    async def scenario():
        await _call("new_page", url="https://example.com")

        out = await _call("evaluate_script", function='() => ({a: 1, b: "two", c: [1, 2]})')
        assert '"type"' not in out, f"still CDP wire format:\n{out}"
        assert '"a": 1' in out or '"a":1' in out, out
        assert '"b": "two"' in out or '"b":"two"' in out, out

        # Primitives kept working throughout; make sure that did not regress.
        assert "42" in await _call("evaluate_script", function="() => 40 + 2")

    _run(scenario)


def test_a_selector_that_cannot_parse_is_reported_not_treated_as_a_hit():
    """Regression: an invalid selector was reported as a successful match.

    nodriver's Tab.evaluate *returns* the ExceptionDetails object on a JS error
    instead of raising, and that object is truthy — so `if await tab.evaluate(...)`
    in wait_for_selector answered "Element found." on the first poll for a
    selector Chrome had refused to parse.
    """

    async def scenario():
        await _call("new_page", url="https://example.com")

        found = await _call("wait_for_selector", selector=">>>bogus[[[", timeout=2000)
        assert "found" not in found.lower(), f"invalid selector reported as a hit:\n{found}"

        scrolled = await _call("scroll_to_selector", selector=">>>bogus[[[")
        assert "Scrolled to" not in scrolled, scrolled

        # A valid selector that matches nothing is a different answer entirely.
        real = await _call("wait_for_selector", selector="div.definitely-absent", timeout=1500)
        assert "Timeout" in real, real

    _run(scenario)


def test_a_response_body_can_actually_be_retrieved():
    """Regression: Network.getResponseBody failed for 100% of requests.

    Network.enable was sent with no buffer sizes, so Chrome retained no resource
    bodies at all and answered "No resource with given identifier found" even for
    a request issued a second earlier — the documented flagship workflow, find
    the page's own API call and read the JSON it already received, could never
    work. The guard was also keyed on id(tab), which does not change when the CDP
    session under it does, so the one call that mattered was skipped.
    """

    async def scenario():
        await _call("new_page", url="https://httpbingo.org/")
        await _call(
            "evaluate_script",
            function="async () => { const r = await fetch('/json'); return (await r.text()).length; }",
        )
        await asyncio.sleep(0.5)

        listing = await _call("list_network_requests", resource_types=["Fetch"])
        match = re.search(r"\[(\d+)\] GET \S+/json", listing)
        assert match, f"the fetch was not captured:\n{listing}"

        body = await _call("get_network_request", reqid=int(match.group(1)))
        assert "-32000" not in body, f"body still unavailable:\n{body}"
        assert "slideshow" in body, f"body did not contain the JSON:\n{body[:400]}"

    _run(scenario)


def test_block_resources_actually_blocks_across_a_navigation():
    """Regression: it reported success while every image still loaded.

    setBlockedURLs is a Network-domain command and is ignored when the domain is
    not enabled on the session it is sent to.
    """

    async def scenario():
        await _call("new_page", url="https://example.com")
        await _call("block_resources", types=["image"])
        await _call("navigate_page", url="https://books.toscrape.com/")

        counts = await _call("evaluate_script", function=(
            "() => { const a = [...document.images]; "
            "return a.length + ':' + a.filter(i => i.naturalWidth > 0).length; }"
        ))
        total, loaded = re.search(r"(\d+):(\d+)", counts).groups()
        assert int(total) > 0, f"no images on the page to block:\n{counts}"
        assert int(loaded) == 0, f"{loaded} of {total} images still loaded:\n{counts}"

    _run(scenario)


def test_a_trace_survives_a_navigation():
    """Regression: Tracing.end answered "Tracing is not started".

    nodriver re-attached after every navigation, minting a new CDP session, so
    the trace started on the old one was unreachable — and recording a page load
    is the whole point of the tool. Traces were also always empty because
    Tracing.start asked for ReturnAsStream while only dataCollected was read.
    """

    async def scenario():
        await _call("new_page", url="https://example.com")
        await _call("performance_start_trace", reload_page=False, auto_stop=False)
        await _call("navigate_page", url="https://example.org")

        trace = await _call("performance_stop_trace")
        match = re.search(r"(\d+) events collected", trace)
        assert match, f"stop_trace did not report a count:\n{trace}"
        assert int(match.group(1)) > 0, f"trace was empty:\n{trace}"

    _run(scenario)


def test_a_navigation_that_fails_is_reported_as_a_failure():
    """Regression: nodriver discards Page.navigate's errorText, so a dead domain
    came back as a successful navigation and the agent scraped the previous page.

    An HTTP error status is deliberately not a failure: a 404 that comes with a
    body is a page, and reading it is often the point.
    """

    async def scenario():
        await _call("new_page", url="https://example.com")

        dead = await _call(
            "navigate_page", url="https://this-domain-truly-does-not-exist-xyz42.invalid"
        )
        assert "ERR_NAME_NOT_RESOLVED" in dead or "failed" in dead.lower(), dead

        found = await _call(
            "navigate_page", url="https://the-internet.herokuapp.com/does-not-exist"
        )
        assert "Error" not in found.split("\n")[0], f"a 404 with a body must load:\n{found}"
        assert "Not Found" in await _call("get_page_content", max_chars=200)

    _run(scenario)
