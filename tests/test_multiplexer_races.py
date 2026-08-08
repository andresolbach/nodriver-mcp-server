"""Regressions for the defects an adversarial review of 2.0.0 turned up.

Every test here failed against the first cut of the routing layer. They use a
stub worker rather than a real subprocess, because each one is about the
registry's bookkeeping around a spawn, and a real spawn only makes the window
harder to hit reliably.
"""

from __future__ import annotations

import asyncio

import pytest

from nodriver_mcp import multiplexer as mux


class _StubWorker:
    """A worker that takes measurable time to start and records what it was told."""

    spawn_delay = 0.2

    def __init__(self, name: str) -> None:
        self.name = name
        self.session = object()
        self.error = None
        self.task = None
        self.started_at = 0.0
        self.calls = 0
        self.last_tool = None
        self.retired = False
        self.shutdown_called = False
        self.started = False

    @property
    def alive(self) -> bool:
        return not self.retired and self.started and self.error is None

    async def start(self) -> None:
        await asyncio.sleep(self.spawn_delay)
        self.started = True

    async def shutdown(self) -> None:
        self.retired = True
        self.shutdown_called = True


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(mux, "_workers", {})
    monkeypatch.setattr(mux, "_name_locks", {})
    monkeypatch.setattr(mux, "_Worker", _StubWorker)
    # Stub workers have no session to ping.
    monkeypatch.setattr(mux, "_responsive", lambda w: _true_if_alive(w))
    yield
    mux._workers.clear()


async def _true_if_alive(worker):
    return worker.alive


def test_the_cap_holds_when_many_browsers_are_opened_at_once():
    """Regression: the slot was only taken after the spawn finished.

    Ten concurrent first calls each counted the registry while every one of the
    others was still starting, so all ten saw room and all ten spawned. The cap
    only ever held for calls arriving one at a time.
    """

    async def scenario():
        wanted = mux._MAX_BROWSERS + 5
        results = await asyncio.gather(
            *(mux._ensure_worker(f"b{i}") for i in range(wanted)),
            return_exceptions=True,
        )
        spawned = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, RuntimeError)]
        # The default browser occupies one of the slots.
        assert len(spawned) <= mux._MAX_BROWSERS - 1, (
            f"{len(spawned)} browsers opened, cap is {mux._MAX_BROWSERS}"
        )
        assert refused, "nothing was refused even though more were asked for"
        assert all("Refusing to open more than" in str(r) for r in refused)

    asyncio.run(scenario())


def test_a_shutdown_during_a_spawn_is_not_reported_as_nothing_to_do():
    """Regression: a name being started was invisible to shutdown_browser.

    It answered "No browser named 'x' is open", then the Chrome finished coming
    up and stayed for the life of the server — the caller was told the opposite
    of what happened.
    """

    async def scenario():
        spawn = asyncio.create_task(mux.call_tool("list_pages", {"browser": "racer"}))
        await asyncio.sleep(_StubWorker.spawn_delay / 2)  # mid-spawn
        result = await mux.call_tool("shutdown_browser", {"browser": "racer"})
        text = mux._text(result)
        await asyncio.gather(spawn, return_exceptions=True)

        assert "nothing to shut down" not in text, text
        assert "shut down" in text
        assert "racer" not in mux._workers, "the browser outlived its shutdown"

    asyncio.run(scenario())


def test_a_browser_retired_during_its_spawn_is_not_handed_back():
    """The server shutting down does not queue behind a spawn, so it is the one
    path that can retire a worker while _ensure_worker still holds the lock.

    Handing that worker back would give the caller a browser whose subprocess is
    already being torn down, and put it in a registry nothing will clean up
    again.
    """

    async def scenario():
        task = asyncio.create_task(mux._ensure_worker("racer"))
        await asyncio.sleep(_StubWorker.spawn_delay / 2)
        await mux._shutdown_everything()  # does not take the name locks
        with pytest.raises(RuntimeError, match="shut down while it was starting"):
            await task
        assert "racer" not in mux._workers

    asyncio.run(scenario())


def test_a_shutdown_that_waits_out_a_spawn_still_wins():
    """shutdown_browser queues behind the spawn rather than missing it, and the
    browser really is gone once it returns."""

    async def scenario():
        spawn = asyncio.create_task(mux._ensure_worker("racer"))
        await asyncio.sleep(_StubWorker.spawn_delay / 2)
        result = await mux.call_tool("shutdown_browser", {"browser": "racer"})
        worker = await asyncio.gather(spawn, return_exceptions=True)

        assert "shut down" in mux._text(result)
        assert "racer" not in mux._workers
        spawned = worker[0]
        if not isinstance(spawned, BaseException):
            assert spawned.shutdown_called, "the spawned worker was left running"

    asyncio.run(scenario())


def test_list_browsers_survives_a_browser_disappearing_mid_report():
    """Regression: KeyError, surfaced to the caller as the message "'a'".

    list_browsers snapshots the names, then awaits per browser; anything that
    retires one in between used to make the whole report fail.
    """

    async def scenario():
        await mux._ensure_worker("a")
        await mux._ensure_worker("b")

        real_status = mux._status
        seen: list[str] = []

        async def status_that_races(name):
            seen.append(name)
            if name == mux.DEFAULT_BROWSER:
                mux._workers.pop("a", None)  # another caller retires it
            return await real_status(name)

        mux._status = status_that_races
        try:
            result = await mux.call_tool("list_browsers", {})
        finally:
            mux._status = real_status

        assert not result.isError, mux._text(result)
        import json

        report = json.loads(mux._text(result))
        names = [b["browser"] for b in report["browsers"]]
        assert "a" not in names, "a retired browser was still reported"
        assert "b" in names and mux.DEFAULT_BROWSER in names

    asyncio.run(scenario())


def test_a_worker_that_stopped_answering_is_replaced():
    """Regression: nothing observed the child process.

    A worker whose subprocess died kept reporting alive, so the registry handed
    the corpse to every later call and each one waited out the full call
    timeout — five minutes per call, for the rest of the session.
    """

    async def scenario():
        first = await mux._ensure_worker("zombie")
        first.started = True

        async def unresponsive(worker):
            return worker is not first and worker.alive

        mux._responsive = unresponsive
        second = await mux._ensure_worker("zombie")

        assert second is not first, "the dead worker was handed out again"
        assert first.shutdown_called, "the dead worker was never cleaned up"
        assert mux._workers["zombie"] is second

    asyncio.run(scenario())


def test_a_failed_spawn_does_not_leave_a_slot_behind():
    """The slot is claimed before the spawn, so a failure has to release it."""

    async def scenario():
        async def failing_start(self):
            raise RuntimeError("chrome went missing")

        mux._Worker.start = failing_start
        try:
            with pytest.raises(RuntimeError, match="chrome went missing"):
                await mux._ensure_worker("doomed")
            assert "doomed" not in mux._workers, "a failed spawn kept its slot"
        finally:
            mux._Worker.start = _StubWorker.start

    asyncio.run(scenario())
