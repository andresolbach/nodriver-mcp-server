"""What a page can tell about this browser.

The product claim is "undetected", and until now nothing guarded it: the suite
checked schemas and prose, so a regression in the one property the whole server
exists for would have shipped silently. These are the cheap, deterministic
checks — the ones a detection script runs in its first few lines — not a
substitute for testing against a real anti-bot service, which cannot be a unit
test because the verdict lives on someone else's server.

Every assertion here has to hold headless as well as headed, because CI is
headless. Anything that only differs between those two modes is deliberately not
asserted: it would fail on the runner while being correct in the field.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from nodriver_mcp.server import mcp

pytestmark = pytest.mark.slow

# A local page: these probes are about the browser, not about a site, and a
# network round trip would only add flakiness.
_BLANK = "data:text/html,<title>probe</title><p>probe</p>"


async def _call(tool, /, **arguments) -> str:
    result = await mcp.call_tool(tool, arguments)
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        return str(result.get("result", result))
    return "\n".join(
        getattr(block, "text", "") for block in result if getattr(block, "text", None)
    )


async def _probe(expression: str):
    """Evaluate in the page and hand back the parsed value."""
    raw = await _call("evaluate_script", function=f"() => JSON.stringify({expression})")
    match = re.search(r"```json\n(.*)\n```", raw, re.S)
    return json.loads(json.loads(match.group(1)) if match else raw)


def _run(scenario):
    async def main():
        try:
            await scenario()
        finally:
            await _call("close_browser")

    asyncio.run(main())


def test_the_automation_giveaways_are_absent():
    """The three things a detection script looks at first.

    navigator.webdriver is set by every stock automation stack; cdc_ properties
    are ChromeDriver's calling card; an empty plugin list is the classic headless
    tell. nodriver handles these, and this is what notices if a change to the
    launch flags or the startup path quietly undoes that.
    """

    async def scenario():
        await _call("new_page", url=_BLANK)
        state = await _probe(
            "{"
            " webdriver: navigator.webdriver,"
            " cdc: Object.keys(window).concat(Object.keys(document))"
            "        .filter(k => /cdc_|\\$cdc/.test(k)).length,"
            " plugins: navigator.plugins.length,"
            " chrome: !!window.chrome,"
            "}"
        )
        assert state["webdriver"] is False, f"navigator.webdriver is {state['webdriver']!r}"
        assert state["cdc"] == 0, "ChromeDriver cdc_ properties are present"
        assert state["plugins"] > 0, "an empty plugin list is a headless giveaway"
        assert state["chrome"] is True, "window.chrome is missing"

    _run(scenario)


def test_the_window_has_a_real_size():
    """Regression: outerWidth/outerHeight read 0 without an explicit window size.

    A window with no outer dimensions is not a window anyone browses in, and the
    pair is two properties away in any fingerprinting script. Fixed by passing
    --window-size, which the anti-backgrounding work added for its own reasons.
    """

    async def scenario():
        await _call("new_page", url=_BLANK)
        size = await _probe("[outerWidth, outerHeight, innerWidth, innerHeight]")
        assert size[0] > 0 and size[1] > 0, f"outer dimensions are {size[:2]}"
        assert size[2] > 0 and size[3] > 0, f"inner dimensions are {size[2:]}"

    _run(scenario)


def test_navigator_languages_are_well_formed():
    """Regression: the device presets shipped their own q-values.

    Chrome generates those itself, so "en-US,en;q=0.9" became
    "en-US,en;q=0.9;q=0.9" on the wire and put the literal string "en;q=0.9" into
    navigator.languages — a malformed language tag no real browser produces, and
    a single-property tell.
    """

    async def scenario():
        await _call("new_page", url=_BLANK, device="pixel_7")
        languages = await _probe("navigator.languages")
        assert languages, "navigator.languages is empty"
        for tag in languages:
            assert ";" not in tag, f"malformed language tag {tag!r} in {languages}"
            assert "q=" not in tag, f"q-value leaked into {tag!r}"

    _run(scenario)


def test_an_emulated_user_agent_agrees_with_its_client_hints():
    """Regression: the presets claimed a Chrome version the browser did not have.

    Chrome fills navigator.userAgentData from the real build and cannot be talked
    out of it, so a preset pinned to Chrome 150 announced 150 in the UA string
    while its own client hints said 151. Comparing the two is one line of
    JavaScript. Anything the browser will not lie about has to be matched, not
    contradicted.
    """

    async def scenario():
        # navigator.userAgentData is exposed only in a secure context, which a
        # data: URL is not — this one probe needs a real https page.
        await _call("new_page", url="https://example.com", device="pixel_7")
        state = await _probe(
            "{"
            " ua: (navigator.userAgent.match(/Chrome\\/(\\d+)/) || [])[1],"
            " brand: (navigator.userAgentData.brands"
            "          .find(b => /Chrome/.test(b.brand)) || {}).version,"
            " mobile: navigator.userAgentData.mobile,"
            " platform: navigator.userAgentData.platform,"
            "}"
        )
        assert state["ua"] and state["brand"], f"could not read both versions: {state}"
        assert state["ua"] == state["brand"], (
            f"user agent says Chrome {state['ua']}, client hints say {state['brand']}"
        )
        # The preset is a phone, and the hints have to say so too.
        assert state["mobile"] is True, f"mobile preset reports mobile={state['mobile']}"
        assert state["platform"] == "Android", state["platform"]

    _run(scenario)


def test_the_page_sees_no_trace_of_the_input_probe():
    """The delivery check must not become the thing that gives the browser away.

    It counts events from an isolated world for exactly this reason; a counter on
    the page's own window would be a global no ordinary site defines.
    """

    async def scenario():
        await _call("new_page", url=_BLANK)
        await _call("evaluate_script", function="() => 'settle'")
        before = await _probe("Object.keys(window).filter(k => /^__nd/.test(k))")
        assert before == [], f"the page can see {before}"

        # Drive a click, which arms and reads the probe. The uid deliberately
        # lands on a text node: click promotes it to its element, and this is
        # also the path that used to raise a raw getBoundingClientRect TypeError.
        snapshot = await _call("take_snapshot")
        match = re.search(r'uid=(\S+) StaticText "probe"', snapshot)
        assert match, snapshot
        clicked = await _call("click", uid=match.group(1))
        assert "Clicked" in clicked, clicked

        after = await _probe("Object.keys(window).filter(k => /^__nd/.test(k))")
        assert after == [], f"the input probe leaked {after} onto the page"

    _run(scenario)
