"""capture_bodies, against a real Chrome and a local site.

The tool exists because the lazy lookup loses bodies, so each test sets up a way
a body gets lost — or a way the capture itself could hurt the page — and checks
the file on disk rather than the tool's own report. A capture that reports
success and writes nothing would pass any test that only read the response.

Marked slow: it launches Chrome. NODRIVER_HEADLESS=true keeps the windows away.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import pytest

from capture_site import BIG_SHA256, CaptureSite
from nodriver_mcp.server import _capture_filename
from test_browser_behaviour import _call, _run


def _manifest(directory: Path) -> list[dict]:
    path = directory / "capture.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _by_path(entries: list[dict]) -> dict[str, dict]:
    return {re.sub(r"^https?://[^/]+", "", e["url"]): e for e in entries}


# ---------------------------------------------------------------------------
# No browser
# ---------------------------------------------------------------------------

def test_filenames_say_where_a_body_came_from():
    assert _capture_filename(7, "https://api.example.com/v1/search?q=x", "application/json") == (
        "0007_api.example.com_search.json"
    )
    # The type Chrome reports wins over what the URL looks like.
    assert _capture_filename(1, "https://x.test/data.php", "application/json").endswith(".json")
    assert _capture_filename(2, "https://x.test/", "text/html") == "0002_x.test_index.html"
    assert _capture_filename(3, "https://x.test/blob", "") == "0003_x.test_blob.bin"
    assert _capture_filename(4, "https://x.test/a b/ü?.js", "text/javascript").endswith(".js")


# ---------------------------------------------------------------------------
# Real Chrome
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_bodies_chrome_drops_are_on_disk(tmp_path):
    """The two ways the lazy lookup loses a body, measured before this existed:
    a 12 MB response is "evicted from inspector cache" at its own loadingFinished,
    and a small one is gone after a single cross-site navigation."""

    async def scenario():
        with CaptureSite() as site:
            await _call("new_page", url=site.base + "/")
            started = await _call("capture_bodies", url_pattern="*/api/fo/*", out_dir=str(tmp_path))
            assert "Capturing" in started and "1 page" in started, started

            await _call("evaluate_script", function=(
                "async () => {"
                " const a = await (await fetch('/api/fo/small')).text();"
                " const b = await (await fetch('/api/fo/big')).arrayBuffer();"
                " const c = await (await fetch('/api/fo/post', {method: 'POST', body: '{\"q\": 1}'})).text();"
                " return [a.length, b.byteLength, c.length]; }"
            ))
            await _call("navigate_page", url=site.other_site + "/")

            # The lazy path has lost the big body; it must now come from the file.
            listing = await _call("list_network_requests", include_preserved_requests=True)
            big = re.search(r"\[(\d+)\] 200 GET \S+/api/fo/big.* saved", listing)
            assert big, f"the big request is not marked saved:\n{listing}"
            detail = await _call("get_network_request", reqid=int(big.group(1)))
            assert "from the capture" in detail, detail

            stopped = await _call("capture_bodies", action="stop")

        assert "Saved 3 bodies" in stopped, stopped
        entries = _by_path(_manifest(tmp_path))
        assert set(entries) == {"/api/fo/small", "/api/fo/big", "/api/fo/post"}, entries.keys()

        big_file = tmp_path / entries["/api/fo/big"]["file"]
        assert hashlib.sha256(big_file.read_bytes()).hexdigest() == BIG_SHA256
        small = json.loads((tmp_path / entries["/api/fo/small"]["file"]).read_text())
        assert small == {"path": "/api/fo/small"}

        post = entries["/api/fo/post"]
        assert post["method"] == "POST" and post["post_data"] == '{"q": 1}', post
        assert json.loads((tmp_path / post["file"]).read_text()) == {"echo": {"q": 1}}
        assert all(e["outcome"] == "complete" and e["status"] == 200 for e in entries.values())

    _run(scenario)


@pytest.mark.slow
def test_workers_and_the_service_worker_are_captured(tmp_path):
    """Measured: Network on the page saw 2 of these 5 requests, Fetch on the page
    3. The service worker's own requests need interception on its own target."""

    async def scenario():
        with CaptureSite() as site:
            await _call("capture_bodies", url_pattern="*/api/fo/*", out_dir=str(tmp_path))
            await _call("new_page", url=site.base + "/sw")
            await _call("evaluate_script", function=(
                "async () => { await navigator.serviceWorker.ready; return 1; }"
            ))
            await _call("navigate_page", type="reload")  # now the worker controls the page

            result = await _call("evaluate_script", function=(
                "async () => {"
                " const got = {};"
                " got.page = await (await fetch('/api/fo/page')).text();"
                " got.viasw = await (await fetch('/api/fo/viasw')).text();"
                " got.worker = await new Promise(r => { const w = new Worker('/w.js'); w.onmessage = e => r(e.data); });"
                " const src = `fetch('" + site.base + "/api/fo/blob').then(r => r.text()).then(t => postMessage(t));`;"
                " got.blob = await new Promise(r => { const w = new Worker(URL.createObjectURL(new Blob([src], {type: 'text/javascript'}))); w.onmessage = e => r(e.data); });"
                " got.swinternal = await new Promise(r => { navigator.serviceWorker.onmessage = e => r(e.data); navigator.serviceWorker.controller.postMessage('go'); });"
                " return got; }"
            ))
            assert "swinternal" in result, f"the page itself did not get every response:\n{result}"
            await asyncio.sleep(0.5)
            stopped = await _call("capture_bodies", action="stop")

        entries = _by_path(_manifest(tmp_path))
        wanted = {f"/api/fo/{n}" for n in ("page", "viasw", "worker", "blob", "swinternal")}
        assert wanted <= set(entries), f"missing {sorted(wanted - set(entries))}\n{stopped}"
        for name in wanted:
            body = json.loads((tmp_path / entries[name]["file"]).read_text())
            assert body == {"path": name}, (name, body)
        assert entries["/api/fo/swinternal"]["source"] == "service_worker"

    _run(scenario)


@pytest.mark.slow
def test_an_event_stream_is_captured_without_starving_the_page(tmp_path):
    """Fetch.getResponseBody waits for the end of a body. An event stream has
    none, so waiting for it would hand the page nothing, ever."""

    async def scenario():
        with CaptureSite() as site:
            await _call("new_page", url=site.base + "/")
            await _call("capture_bodies", url_pattern="*/api/fo/*", out_dir=str(tmp_path))
            received = await _call("evaluate_script", function=(
                "() => new Promise(resolve => {"
                " const es = new EventSource('/api/fo/sse'); let n = 0;"
                " setTimeout(() => { es.close(); resolve(n); }, 5000);"
                " es.onmessage = () => { if (++n === 3) { es.close(); resolve(n); } }; })"
            ))
            assert re.search(r"\b3\b", received), f"the page did not get its events: {received}"
            await asyncio.sleep(0.5)
            stopped = await _call("capture_bodies", action="stop")

        entries = _by_path(_manifest(tmp_path))
        assert "/api/fo/sse" in entries, stopped
        text = (tmp_path / entries["/api/fo/sse"]["file"]).read_text()
        assert all(f"tick {i}" in text for i in range(3)), text
        assert entries["/api/fo/sse"]["outcome"] == "stream closed by the page"

    _run(scenario)


@pytest.mark.slow
def test_capture_and_block_resources_leave_each_other_alone(tmp_path):
    """Both use Fetch. On one session, whichever enabled last would silently
    replace the other's patterns."""

    async def scenario():
        with CaptureSite() as site:
            await _call("new_page", url=site.base + "/")
            await _call("block_resources", types=["image"])
            await _call("capture_bodies", url_pattern="*/api/fo/*", out_dir=str(tmp_path))
            await _call("navigate_page", type="reload")
            await _call("evaluate_script", function="async () => (await fetch('/api/fo/x')).status")
            await asyncio.sleep(0.3)
            images = await _call("list_network_requests", resource_types=["Image"])
            assert "FAILED" in images, f"the image was not blocked while capturing:\n{images}"
            await _call("capture_bodies", action="stop")

            await _call("navigate_page", type="reload")
            await asyncio.sleep(0.3)
            images = await _call("list_network_requests", resource_types=["Image"])
            assert "FAILED" in images, f"stopping the capture unblocked images:\n{images}"

        assert "/api/fo/x" in _by_path(_manifest(tmp_path))

    _run(scenario)


@pytest.mark.slow
def test_the_capture_reports_what_actually_happened(tmp_path):
    async def scenario():
        await _call("capture_bodies", action="status")  # clear what earlier tests left
        nothing = await _call("capture_bodies", action="stop")
        assert "No capture is running" in nothing, nothing
        no_pattern = await _call("capture_bodies", out_dir=str(tmp_path))
        assert "url_pattern is required" in no_pattern, no_pattern

        with CaptureSite() as site:
            await _call("new_page", url=site.base + "/")
            first = await _call("capture_bodies", url_pattern="api/fo", out_dir=str(tmp_path))
            assert '"*api/fo*"' in first, f"a bare pattern must be widened, and say so:\n{first}"
            second = await _call("capture_bodies", url_pattern="*", out_dir=str(tmp_path))
            assert "already running" in second, second

            await _call("close_browser")
            stop = await _call("capture_bodies", action="stop")
            assert "already ended" in stop and "browser was closed" in stop, stop
            status = await _call("capture_bodies", action="status")
            assert "No capture running" in status and "browser was closed" in status, status

    _run(scenario)
