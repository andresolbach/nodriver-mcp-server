"""Regressions for the capacity ratchet found by the 2.0.1 audit.

Eighteen agents each opened a browser, worked, and called close_browser exactly
as instructed. The twelfth onwards were refused a browser for the rest of the
session: close_browser deliberately keeps the name (its profile and flags have to
survive), but nothing ever gave the name back, and the cleanup call itself ran
through the get-or-create path — so closing a name that was never opened answered
with the *creation* cap error.

Like test_multiplexer_races, these use a stub worker: every one is about the
registry's bookkeeping, not about a real Chrome.
"""

from __future__ import annotations

import asyncio

import pytest

from nodriver_mcp import multiplexer as mux


class _StubWorker:
    """A worker whose Chrome state and idleness the test controls directly."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.session = object()
        self.error = None
        self.task = None
        self.started_at = 0.0
        self.last_used = 0.0  # ancient by default: idle long enough to reap
        self.calls = 0
        self.last_tool = None
        self.retired = False
        self.shutdown_called = False
        self.started = False
        self.chrome_running = True

    @property
    def alive(self) -> bool:
        return not self.retired and self.started and self.error is None

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.retired = True
        self.shutdown_called = True


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(mux, "_workers", {})
    monkeypatch.setattr(mux, "_name_locks", {})
    monkeypatch.setattr(mux, "_Worker", _StubWorker)
    monkeypatch.setattr(mux, "_responsive", _true_if_alive)

    async def _status(name: str):
        if name == mux.DEFAULT_BROWSER:
            return {"browser": name, "runs_in": "the server process"}
        worker = mux._workers.get(name)
        if worker is None:
            return None
        return {
            "browser": name,
            "worker": "running",
            "chrome_running": worker.chrome_running,
        }

    monkeypatch.setattr(mux, "_status", _status)
    yield
    mux._workers.clear()


async def _true_if_alive(worker):
    return worker.alive


def test_closing_a_browser_that_was_never_opened_is_a_no_op():
    """Regression: it answered with the 12-browser *creation* error.

    A teardown call must work when the server is at capacity — that is precisely
    when an agent needs it — and must never spawn a subprocess to discover there
    is nothing to close. A single typo in `browser` used to burn a slot for the
    life of the server.
    """

    async def scenario():
        result = await mux.call_tool("close_browser", {"browser": "never-opened"})
        text = mux._text(result)

        assert "Refusing to open" not in text, text
        assert "nothing to close" in text
        assert not result.isError, "cleanup reported as an error"
        assert "never-opened" not in mux._workers, "teardown created a browser"

    asyncio.run(scenario())


def test_a_browser_with_no_chrome_is_reclaimed_at_the_cap():
    """Regression: the cap counted registered names and nothing ever released one.

    close_browser leaves a live registration with a dead Chrome behind. Once
    twelve of those existed the server refused every new name forever, even
    though no Chrome was running at all.
    """

    async def scenario():
        for i in range(mux._MAX_BROWSERS - 1):
            worker = await mux._ensure_worker(f"b{i}")
            worker.chrome_running = False  # what close_browser leaves behind

        assert mux._live_count() >= mux._MAX_BROWSERS

        fresh = await mux._ensure_worker("late-arrival")
        assert fresh.name == "late-arrival"
        assert mux._live_count() <= mux._MAX_BROWSERS

    asyncio.run(scenario())


def test_a_browser_still_running_chrome_is_never_reclaimed():
    """The reaper must not collect a browser another agent is using.

    Reclaiming is destructive, so "still has a Chrome" outranks "has not been
    called in a while" — an agent parked on a loaded page is idle by every
    measure this layer can see.
    """

    async def scenario():
        for i in range(mux._MAX_BROWSERS - 1):
            await mux._ensure_worker(f"busy{i}")  # chrome_running stays True

        collected = await mux._reap_idle("test")
        assert collected == [], f"reaped a browser that still had Chrome: {collected}"

        with pytest.raises(RuntimeError) as excinfo:
            await mux._ensure_worker("one-too-many")
        assert "none could be reclaimed" in str(excinfo.value)
        # The error has to name the call that actually frees a slot.
        assert "shutdown_browser" in str(excinfo.value)

    asyncio.run(scenario())


def test_a_worker_whose_age_is_unknown_is_left_alone():
    """Reaping is destructive, so an unknown idle time must mean "do not touch"."""

    async def scenario():
        worker = await mux._ensure_worker("ageless")
        worker.chrome_running = False
        del worker.last_used

        assert await mux._reap_idle("test") == []
        assert "ageless" in mux._workers

    asyncio.run(scenario())


def test_list_browsers_does_not_promise_room_it_does_not_have():
    """Regression: the hint said "pass a new name" while open == max.

    An agent reading that at capacity retries forever against a server that has
    already refused it.
    """

    async def scenario():
        for i in range(mux._MAX_BROWSERS - 1):
            worker = await mux._ensure_worker(f"b{i}")
            worker.chrome_running = False

        result = await mux.call_tool("list_browsers", {})
        import json

        report = json.loads(mux._text(result))

        assert report["open"] == report["max"]
        assert report["slots_free"] == 0
        assert "Nothing needs to be created first" not in report["hint"]
        assert "At capacity" in report["hint"]

    asyncio.run(scenario())


def test_two_spawns_racing_at_the_cap_do_not_deadlock():
    """The reclaim sweep runs from inside _ensure_worker, which already holds the
    lock for its own name. If the sweep then WAITED for another name's lock, two
    concurrent spawns each trying to reclaim the other's slot would hold one lock
    and block on the other forever — the whole server, not just the two calls.
    """

    async def scenario():
        for i in range(mux._MAX_BROWSERS - 1):
            worker = await mux._ensure_worker(f"b{i}")
            worker.chrome_running = False

        # Both arrive at the cap together and both try to make room.
        results = await asyncio.wait_for(
            asyncio.gather(
                mux._ensure_worker("racer-one"),
                mux._ensure_worker("racer-two"),
                return_exceptions=True,
            ),
            timeout=10,
        )
        # Whether both get in or one is refused depends on how much was
        # reclaimable; deadlocking is the only unacceptable outcome.
        assert all(
            not isinstance(r, BaseException) or isinstance(r, RuntimeError)
            for r in results
        ), results

    asyncio.run(scenario())
