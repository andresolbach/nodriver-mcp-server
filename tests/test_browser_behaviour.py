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

from mcp.server.fastmcp.exceptions import ToolError

from nodriver_mcp.server import mcp

pytestmark = pytest.mark.slow


async def _call(tool, /, **arguments) -> str:
    """Invoke a tool the way a client does, and return its text.

    Positional-only, so a tool argument called `name` (set_cookie, save_session)
    does not collide with this function's own parameter.
    """
    try:
        result = await mcp.call_tool(tool, arguments)
    except ToolError as e:
        # A failing tool raises now, so that the routing layer can mark the
        # result isError. The text is the same; these tests still assert on it.
        return str(e)
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
        match = re.search(r"\[(\d+)\] \S+ GET \S+/json", listing)
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


def test_one_request_is_recorded_once():
    """The Network.enable guard is keyed on the session, the handler on the tab.

    Getting that the wrong way round records every later request twice: enabling
    per session is what makes response bodies available, but re-adding the event
    handler each time duplicates the log.
    """

    async def scenario():
        await _call("new_page", url="https://httpbingo.org/")
        # get_network_request also ensures the domain is live, which is the second
        # place a duplicate handler could be installed from.
        await _call("evaluate_script", function="async () => (await fetch('/uuid')).status")
        await asyncio.sleep(0.4)
        await _call("get_network_request")
        await _call("evaluate_script", function="async () => (await fetch('/ip')).status")
        await asyncio.sleep(0.4)

        listing = await _call("list_network_requests", resource_types=["Fetch"])
        for path in ("/uuid", "/ip"):
            hits = len(re.findall(rf"GET \S+{re.escape(path)}\b", listing))
            assert hits == 1, f"{path} recorded {hits} times:\n{listing}"

    _run(scenario)


# aria-labels so the snapshot names each control, which is how a uid is found.
_FORM_PAGE = (
    "data:text/html,<form>"
    "<input id=t type=text aria-label=TextField>"
    "<input id=cb type=checkbox aria-label=BoxField>"
    "<input id=r1 type=radio name=g value=one aria-label=RadioOne>"
    "<input id=r2 type=radio name=g value=two aria-label=RadioTwo>"
    "<input id=d type=date aria-label=DateField>"
    "<select id=s aria-label=PickField>"
    "<option value=v1>First label</option><option value=v2>Second label</option>"
    "</select></form>"
)


async def _uid(snapshot: str, pattern: str) -> str:
    match = re.search(pattern, snapshot)
    assert match, f"{pattern!r} not found in snapshot:\n{snapshot}"
    return match.group(1)


async def _prop(expr: str) -> str:
    return await _call("evaluate_script", function=f"() => String({expr})")


def test_fill_refuses_a_checkbox_and_names_the_right_tool():
    """Regression: fill ran its select-and-type path on checkboxes and radios.

    A checkbox holds no text, so the keystrokes went to whatever had focus — the
    previously filled field, in a fill_form — while the read-back compared
    `el.value`, which a checkbox never changes. So it reported success having done
    something else entirely, somewhere else.
    """

    async def scenario():
        await _call("new_page", url=_FORM_PAGE)
        snap = await _call("take_snapshot")

        out = await _call("fill", uid=await _uid(snap, r'uid=(\S+) checkbox'), value="yes")
        assert "Error" in out and "set_checked" in out, f"fill accepted a checkbox:\n{out}"
        assert "false" in (await _prop("document.getElementById('cb').checked")).lower()

        radio = await _uid(snap, r'uid=(\S+) radio "RadioOne"')
        out = await _call("fill", uid=radio, value="one")
        assert "Error" in out and "set_checked" in out, f"fill accepted a radio:\n{out}"
        assert "false" in (await _prop("document.getElementById('r1').checked")).lower()

    _run(scenario)


def test_set_checked_ticks_unticks_and_is_idempotent():
    """The tool fill could never be, because a checked state is not a value."""

    async def scenario():
        await _call("new_page", url=_FORM_PAGE)
        snap = await _call("take_snapshot")
        box = await _uid(snap, r'uid=(\S+) checkbox')

        assert "now checked" in await _call("set_checked", uid=box, checked=True)
        assert "true" in (await _prop("document.getElementById('cb').checked")).lower()

        again = await _call("set_checked", uid=box, checked=True)
        assert "already checked" in again, f"not idempotent:\n{again}"

        assert "now unchecked" in await _call("set_checked", uid=box, checked=False)
        assert "false" in (await _prop("document.getElementById('cb').checked")).lower()

        radio = await _uid(snap, r'uid=(\S+) radio "RadioTwo"')
        assert "now checked" in await _call("set_checked", uid=radio, checked=True)
        assert "true" in (await _prop("document.getElementById('r2').checked")).lower()
        # A radio cannot be cleared by the browser; say so instead of failing oddly.
        cleared = await _call("set_checked", uid=radio, checked=False)
        assert "cannot be unchecked" in cleared, cleared

    _run(scenario)


def test_select_option_matches_a_label_and_lists_the_real_options():
    """fill matched only the value attribute, which a snapshot never shows — so it
    had to be guessed, and the failure did not say what was on offer."""

    async def scenario():
        await _call("new_page", url=_FORM_PAGE)
        snap = await _call("take_snapshot")
        combo = await _uid(snap, r'uid=(\S+) combobox')

        by_label = await _call("select_option", uid=combo, option="Second label")
        assert "v2" in by_label, by_label
        assert "v2" in await _prop("document.getElementById('s').value")

        assert "v1" in await _call("select_option", uid=combo, option="v1")

        missing = await _call("select_option", uid=combo, option="nope")
        assert "First label" in missing and "Second label" in missing, (
            f"the failure did not name the real options:\n{missing}"
        )

    _run(scenario)


def test_a_native_date_input_is_set_and_reported_honestly():
    """Regression: it returned an error for a value that had landed.

    The select-and-type path typed a locale string while `el.value` holds the wire
    format, so the read-back never matched — the one shape of failure worse than
    silence is a false alarm on a success.
    """

    async def scenario():
        await _call("new_page", url=_FORM_PAGE)
        snap = await _call("take_snapshot")
        date_uid = await _uid(snap, r'uid=(\S+) Date')

        out = await _call("fill", uid=date_uid, value="2026-08-08")
        assert "Error" not in out, f"a date fill that works must not report an error:\n{out}"
        assert "2026-08-08" in await _prop("document.getElementById('d').value")
        # The value was assigned rather than typed, and that is disclosed.
        assert "rather than typed" in out, f"the scripted path was not disclosed:\n{out}"

        bad = await _call("fill", uid=date_uid, value="08/08/2026")
        assert "Error" in bad and "YYYY-MM-DD" in bad, f"wrong format accepted:\n{bad}"

    _run(scenario)


def test_the_network_log_distinguishes_outcomes():
    """Regression: a 500, a 404, a redirect, a transport failure and a request
    still in flight all rendered as the same line.

    Only requestWillBeSent was subscribed, so nothing on the response side was
    ever collected — no status, no headers, no timing, no failure flag. The one
    job of a network log is telling you which request went wrong.
    """

    async def scenario():
        await _call("new_page", url="https://httpbingo.org/")
        await _call("evaluate_script", function=(
            "async () => {"
            " await (await fetch('/status/500')).text();"
            " await (await fetch('/status/404')).text();"
            " await (await fetch('/redirect/1')).text();"
            " return 1; }"
        ))
        await asyncio.sleep(1.5)

        listing = await _call("list_network_requests")
        assert re.search(r"\b500\b.*status/500", listing), f"no 500 status:\n{listing}"
        assert re.search(r"\b404\b.*status/404", listing), f"no 404 status:\n{listing}"
        assert "->" in listing, f"no redirect marked:\n{listing}"
        assert re.search(r"\d+(\.\d+)?ms", listing), f"no timing:\n{listing}"

        # A blocked request is the one deterministic transport failure available
        # here: fetching an unroutable host from an https page dies in the
        # mixed-content check before the network stack and produces no events.
        await _call("block_resources", types=["image"])
        await _call("navigate_page", url="https://books.toscrape.com/")
        await asyncio.sleep(0.8)
        blocked = await _call("list_network_requests", resource_types=["Image"])
        assert "FAILED" in blocked, f"a blocked request was not marked failed:\n{blocked}"

        match = re.search(r"\[(\d+)\] 500", listing)
        assert match, listing
        detail = await _call("get_network_request", reqid=int(match.group(1)))
        assert "Status: 500" in detail, detail
        assert "Response headers" in detail, f"no headers:\n{detail}"
        assert "content-type" in detail.lower(), f"no content-type header:\n{detail}"
        assert "Transfer:" in detail, f"no timing/size:\n{detail}"

    _run(scenario)


def test_websocket_traffic_is_captured_with_its_frames():
    """Regression: WebSocket was an offered resource_types value that never matched.

    Nothing subscribed to any Network.webSocket* event, so a live socket produced
    no entries at all — for a real-time site the actual data channel was invisible,
    and the only workaround was an init_script shim replacing window.WebSocket,
    which de-natives the API and undermines the stealth the server exists for.
    """

    async def scenario():
        await _call("new_page", url="https://httpbingo.org/")
        sent = await _call("evaluate_script", function=(
            "async () => {"
            " const ws = new WebSocket('wss://httpbingo.org/websocket/echo');"
            " await new Promise(r => ws.onopen = r);"
            " ws.send('audit-one'); ws.send('audit-two');"
            " await new Promise(r => setTimeout(r, 500));"
            " ws.close();"
            " return 'done'; }"
        ))
        assert "done" in sent, f"the socket never opened:\n{sent}"
        await asyncio.sleep(1.5)

        listing = await _call("list_network_requests", resource_types=["WebSocket"])
        assert "websocket/echo" in listing, f"no socket in the log:\n{listing}"
        assert "101" in listing, f"no handshake status:\n{listing}"
        assert "2 sent" in listing and "2 recv" in listing, f"frames not counted:\n{listing}"

        match = re.search(r"\[(\d+)\] \S+ GET wss://", listing)
        assert match, listing
        detail = await _call("get_network_request", reqid=int(match.group(1)))
        assert "audit-one" in detail and "audit-two" in detail, f"no frame payloads:\n{detail}"
        assert "->" in detail and "<-" in detail, f"frame direction missing:\n{detail}"
        # An echo server sends back what it received, so both directions appear.
        assert detail.count("audit-one") == 2, f"expected it sent and echoed:\n{detail}"

    _run(scenario)


_FRAME_PAGE = (
    "data:text/html,<h1 style='margin:0;padding:40px'>Host</h1>"
    "<iframe style='margin-left:80px;width:400px;height:200px' "
    "srcdoc=\"<input type=checkbox aria-label=InnerBox>"
    "<input aria-label=InnerField><p>InnerText</p>\"></iframe>"
)


def test_frames_are_readable_and_listed():
    """Regression: iframe content was unreachable by every reader.

    take_snapshot called getFullAXTree with no frame_id, which stops at the frame
    boundary, and document.querySelector never crosses one — so a payment field, a
    consent wall or an embedded editor simply did not exist as far as the server
    was concerned. get_page_content returned '' for a frameset, which is
    indistinguishable from a blank page.
    """

    async def scenario():
        await _call("new_page", url=_FRAME_PAGE)

        frames = await _call("list_frames")
        assert "2 frames" in frames, frames

        snap = await _call("take_snapshot")
        # Assert on tree lines, not on the whole text: the root line echoes the
        # data: URL, whose srcdoc attribute mentions every inner element by name.
        assert re.search(r'uid=\S+ textbox "InnerField"', snap), (
            f"iframe content missing from the snapshot:\n{snap}"
        )
        # And it is nested under the frame, not appended as a second document.
        assert re.search(r"Iframe\n\s+uid=\S+ RootWebArea", snap), (
            f"the frame's tree was not spliced under its owner:\n{snap}"
        )

        assert "InnerText" in await _call("get_page_content", frame="1")
        assert "InnerText" not in await _call("get_page_content")
        assert "input" in await _call("query_selector", selector="input", frame="1")

        off = await _call("take_snapshot", include_frames=False)
        assert not re.search(r'uid=\S+ textbox "InnerField"', off), (
            f"include_frames=False still read the frame:\n{off}"
        )
        assert "Iframe" in off, f"the frame's own node should still be listed:\n{off}"

    _run(scenario)


def test_trusted_input_reaches_an_element_inside_a_frame():
    """A click is delivered by viewport coordinate, but the hit test necessarily
    runs in the element's own document — so for anything inside an iframe the point
    was frame-relative while the click was top-level, and it landed elsewhere while
    reporting success. The offset is measured and added back.
    """

    async def scenario():
        await _call("new_page", url=_FRAME_PAGE)
        snap = await _call("take_snapshot")
        box = re.search(r'uid=(\S+) checkbox "InnerBox"', snap).group(1)

        result = await _call("set_checked", uid=box, checked=True)
        assert "now checked" in result, f"input did not reach the frame:\n{result}"
        state = await _call(
            "evaluate_script", frame="1",
            function="() => String(document.querySelector('input[type=checkbox]').checked)",
        )
        assert "true" in state.lower(), f"the checkbox did not change:\n{state}"

        # Keyboard-driven fill works inside the frame too.
        field = re.search(r'uid=(\S+) textbox "InnerField"', snap).group(1)
        assert "Error" not in await _call("fill", uid=field, value="typed-in-frame")
        value = await _call(
            "evaluate_script", frame="1",
            function="() => document.querySelector('input:not([type=checkbox])').value",
        )
        assert "typed-in-frame" in value, value

        # The main document must still be clickable at unshifted coordinates.
        heading = re.search(r"uid=(\S+) heading", snap).group(1)
        assert "Clicked" in await _call("click", uid=heading)

    _run(scenario)


def test_a_delivered_click_and_keystroke_are_not_warned_about():
    """The delivery check must not cry wolf.

    A warning on every working click would be worse than the silence it replaces,
    so the happy path is what needs guarding: the note appears only when no input
    event reached the page, and type_text names the element it typed into so
    typing at nothing is distinguishable from typing at the right field.
    """

    async def scenario():
        await _call(
            "new_page",
            url=("data:text/html,<input id=email aria-label=EmailField>"
                 "<button aria-label=GoButton style='padding:30px'>Go</button>"),
        )
        snap = await _call("take_snapshot")

        clicked = await _call("click", uid=re.search(r'uid=(\S+) button "GoButton"', snap).group(1))
        assert "Clicked" in clicked
        assert "WARNING" not in clicked, f"a delivered click was warned about:\n{clicked}"

        await _call("click", uid=re.search(r'uid=(\S+) textbox "EmailField"', snap).group(1))
        typed = await _call("type_text", text="abc")
        assert "WARNING" not in typed, f"delivered keystrokes were warned about:\n{typed}"
        assert "input#email" in typed, f"the focus target was not reported:\n{typed}"
        assert "abc" in await _call(
            "evaluate_script", function="() => document.getElementById('email').value"
        )

        pressed = await _call("press_key", key="Tab")
        assert "WARNING" not in pressed, f"a delivered key was warned about:\n{pressed}"

    _run(scenario)


def test_same_origin_urls_are_printed_relative_to_the_page():
    """take_snapshot is the most-called tool, so its size is paid on every step.

    Measured on real pages, repeating the origin on every same-origin link was
    7-16% of the whole snapshot — the largest remaining cost after text folding.
    Printing them relative is lossless because the root node keeps the absolute
    URL, and an external link must still be shown in full or it cannot be
    followed.
    """

    async def scenario():
        await _call("new_page", url="https://books.toscrape.com/")
        snap = await _call("take_snapshot")
        lines = snap.splitlines()

        # The root states the origin once, in full.
        assert re.search(r'RootWebArea.*url="https://books\.toscrape\.com/', lines[0]), lines[0]
        # Same-origin links are relative...
        assert re.search(r'link "Home" url="/index\.html"', snap), (
            f"same-origin link was not shortened:\n{snap[:600]}"
        )
        # ...and no non-root line repeats the origin.
        for line in lines[1:]:
            assert "url=\"https://books.toscrape.com" not in line, (
                f"origin still repeated:\n{line}"
            )

    _run(scenario)


def test_an_external_url_is_still_shown_in_full():
    """Shortening must never touch a URL on another origin: it is not
    reconstructible from the root, and following it is the whole point."""

    async def scenario():
        await _call(
            "new_page",
            url=("data:text/html,<a href='https://example.org/deep/path'>Out</a>"
                 "<a href='/local'>In</a>"),
        )
        snap = await _call("take_snapshot")
        assert "https://example.org/deep/path" in snap, f"external URL was mangled:\n{snap}"

    _run(scenario)


def test_a_dialog_can_be_answered_on_a_tab_that_never_navigated():
    """Regression: a modal dialog could wedge a tab with no way out.

    A dialog blocks the renderer, so every later call into the page hangs until it
    is dismissed — and handle_dialog was the one call that could not dismiss it.
    Chrome reports a dialog only to a client that enabled the Page domain, and
    Page.enable was sent only from navigate_page, so a tab reached through
    new_page alone answered "No dialog is showing" while being blocked by one.
    """

    async def scenario():
        await _call("new_page", url=(
            "data:text/html,<button onclick=\"alert('blocking')\" "
            "aria-label=Boom style='padding:30px'>Go</button>"
        ))
        snap = await _call("take_snapshot")
        button = re.search(r'uid=(\S+) button "Boom"', snap).group(1)

        # The click blocks in the renderer for as long as the alert is up, so it
        # cannot be awaited — that is the whole shape of the bug.
        clicking = asyncio.create_task(_call("click", uid=button))
        await asyncio.sleep(1.5)

        answered = await asyncio.wait_for(
            _call("handle_dialog", action="accept"), timeout=20
        )
        assert "accepted" in answered, answered
        assert "blocking" in answered, f"the dialog's own text was not reported:\n{answered}"

        recovered = await asyncio.wait_for(
            _call("evaluate_script", function="() => 'recovered'"), timeout=20
        )
        assert "recovered" in recovered, f"the page is still wedged:\n{recovered}"
        await asyncio.gather(clicking, return_exceptions=True)

    _run(scenario)


_POINTER_PAGE = (
    "data:text/html,<style>div{display:inline-block;width:120px;height:60px;"
    "margin:4px;border:1px solid}</style>"
    "<div id=a aria-label=A></div><div id=b aria-label=B></div><div id=c aria-label=C></div>"
    "<script>window.seen=[];window.ups=0;window.downs=0;window.moves=0;"
    "document.addEventListener('mousemove',()=>window.moves++);"
    "for (const d of document.querySelectorAll('div')) {"
    " d.addEventListener('mouseover',()=>window.seen.push(d.id));"
    " d.addEventListener('mousedown',()=>window.downs++);"
    " d.addEventListener('mouseup',()=>window.ups++); }</script>"
)


def test_hover_touches_only_its_target():
    """Regression: hover walked the pointer from the viewport origin.

    nodriver's Tab.mouse_move interpolates from (0, 0) every time, firing
    mouseMoved along the whole diagonal — so every menu and tooltip on that line
    opened on the way — and then sent a mouseReleased, a stray mouseup that drag
    handles, sliders and canvas editors act on.
    """

    async def scenario():
        await _call("new_page", url=_POINTER_PAGE)
        snap = await _call("take_snapshot")
        target = re.search(r'uid=(\S+) \S+ "C"', snap).group(1)

        assert "Hovered" in await _call("hover", uid=target)

        seen = await _call("evaluate_script", function="() => window.seen.join(',')")
        assert "c" in seen, f"the target never saw the pointer:\n{seen}"
        for other in ("a", "b"):
            assert other not in seen, f"the pointer swept across {other}:\n{seen}"

        ups = await _call("evaluate_script", function="() => window.ups")
        assert "0" in ups, f"hover fired a mouseup:\n{ups}"

    _run(scenario)


def test_drag_holds_the_button_down_across_the_move():
    """Regression: drag went through nodriver's mouse_drag, which never holds the
    button while moving — the one thing every mouse-driven sortable listens for.
    Both ends are also hit-tested now, so a covered target fails instead of
    dragging onto whatever was on top of it."""

    async def scenario():
        await _call("new_page", url=_POINTER_PAGE)
        snap = await _call("take_snapshot")
        src = re.search(r'uid=(\S+) \S+ "A"', snap).group(1)
        dst = re.search(r'uid=(\S+) \S+ "C"', snap).group(1)

        assert "Dragged" in await _call("drag", from_uid=src, to_uid=dst)

        downs = await _call("evaluate_script", function="() => window.downs")
        ups = await _call("evaluate_script", function="() => window.ups")
        moves = await _call("evaluate_script", function="() => window.moves")
        assert "1" in downs, f"no mousedown on the source:\n{downs}"
        assert "1" in ups, f"no mouseup on the target:\n{ups}"
        assert int(re.search(r"(\d+)", moves).group(1)) > 1, (
            f"the pointer jumped instead of moving:\n{moves}"
        )

    _run(scenario)


def test_an_authenticating_proxy_is_answered():
    """Regression: a proxy that asks for credentials could not be used at all.

    Chrome ignores credentials in a --proxy-server URL, and an authenticating
    proxy then stops it at a native dialog that no page and no other CDP command
    can dismiss — the page simply never loads and nothing says why. Fetch's
    authRequired is the only way to answer it.

    The proxy here is real and really challenges, because a mock that never asks
    would not exercise the part that was broken.
    """
    # A sibling module, not a package: tests/ has no __init__.py, and pytest puts
    # the test directory on sys.path itself.
    from authproxy import PASSWORD, USERNAME, AuthProxy

    with AuthProxy() as proxy:

        async def scenario():
            await _call(
                "set_proxy", server=proxy.address, username=USERNAME, password=PASSWORD
            )
            # http, not https: a CONNECT tunnel would need a real upstream.
            await _call("new_page", url="http://proxied.test/")
            content = await _call("get_page_content")

            assert "fetched through the proxy" in content, (
                f"the page did not come through the proxy:\n{content[:300]}"
            )
            assert proxy.challenges > 0, "the proxy never challenged — auth was untested"
            assert proxy.authenticated > 0, "the challenge was never answered"

        try:
            _run(scenario)
        finally:
            asyncio.run(_call("set_proxy", server="", restart=False))


def test_a_proxy_without_credentials_needs_no_fetch_domain():
    """Fetch pauses every request, so it must stay off unless a proxy asks.

    Paying a round trip per request for a proxy that never challenges would be a
    silent tax on every page load.
    """
    from nodriver_mcp import server

    async def scenario():
        await _call("set_proxy", server="http://127.0.0.1:9", restart=False)
        assert server._proxy_config["server"] == "http://127.0.0.1:9"
        assert not server._proxy_config["username"]

        await _call("set_proxy", server="127.0.0.1:9", restart=False)
        # A bare host:port is a proxy too; it just has no scheme yet.
        assert server._proxy_config["server"] == "http://127.0.0.1:9"

        status = await _call("set_proxy")
        assert "127.0.0.1:9" in status and "no credentials" in status

        cleared = await _call("set_proxy", server="", restart=False)
        assert "removed" in cleared
        assert server._proxy_config == {}

    asyncio.run(scenario())


def test_page_text_that_says_error_is_not_a_failed_call():
    """The isError flag comes from the tool, never from reading the answer.

    The tempting shortcut — flag anything whose text starts with "Error" — breaks
    exactly here: a page is free to say that, and marking a successful read as a
    failed call would be a new lie in place of the old one.
    """
    from nodriver_mcp import multiplexer as mux

    async def scenario():
        await _call("new_page", url="data:text/html,<p>Error: the site is down</p>")
        result = await mux.call_tool("get_page_content", {})

        assert not result.isError, "reading a page that says Error was flagged as a failure"
        assert "Error: the site is down" in mux._text(result)

        # And a genuine failure on the same browser still is flagged.
        failed = await mux.call_tool("click", {"uid": "999_999"})
        assert failed.isError
        assert "unknown uid" in mux._text(failed)

    _run(scenario)
