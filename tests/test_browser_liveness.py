"""A stale tab must not destroy a healthy browser or its current document."""
import asyncio
from types import SimpleNamespace

import pytest

from nodriver_mcp import server
from test_browser_behaviour import _call, _run


def test_browser_liveness_does_not_depend_on_a_detached_tab():
    async def detached_tab(_):
        raise RuntimeError("Session with given id not found")

    async def healthy_browser(_):
        return []

    browser = SimpleNamespace(
        tabs=[SimpleNamespace(send=detached_tab)], connection=None, send=healthy_browser,
    )
    assert asyncio.run(server._browser_alive(browser))


def test_browser_liveness_detects_a_closed_browser_connection():
    async def closed(_):
        raise ConnectionError("Connection closed")

    browser = SimpleNamespace(send=closed)
    assert not asyncio.run(server._browser_alive(browser))


@pytest.mark.slow
def test_stale_blank_target_does_not_overwrite_an_open_page(tmp_path):
    urls = []
    for name in ("first", "second"):
        page = tmp_path / f"{name}.html"
        page.write_text(f"<title>{name}</title><h1>{name}</h1>", encoding="utf-8")
        urls.append(page.as_uri())

    async def scenario():
        await _call("new_page", url=urls[0])
        browser = await server._get_browser()
        first = browser.tabs[0]
        assert await server._evaluate_value(first, "location.href") == urls[0]
        # Deterministically reproduce a delayed TargetInfo update after a
        # successful navigation, as seen on the slower CI browser runner.
        first.target.url = "about:blank"
        await _call("new_page", url=urls[1])
        await browser.update_targets()
        assert sorted(t.url for t in browser.tabs) == sorted(urls)

    _run(scenario)
