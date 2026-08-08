"""The reporting half of input delivery, without a browser.

A CDP Input ack proves only that the browser queued the event; an occluded
window, an open JavaScript dialog or a wedged renderer swallow it. Provoking that
for real means wedging a page, which is exactly the state in which no assertion
can be trusted — so the probe is faked here and the browser tests cover the
happy path, where a false warning would be the damaging failure.
"""

from __future__ import annotations

import asyncio

import pytest

from nodriver_mcp import server


def _note(monkeypatch, probe_result, armed=True) -> str:
    async def fake_probe(_tab):
        return probe_result

    monkeypatch.setattr(server, "_read_input_probe", fake_probe)
    return asyncio.run(server._input_delivery_note(object(), armed))


def test_no_events_seen_produces_a_warning(monkeypatch):
    note = _note(monkeypatch, 0)
    assert "WARNING" in note
    assert "no key event reached the page" in note
    # It has to name the causes, or the agent cannot act on it.
    assert "occluded" in note and "dialog" in note


def test_events_seen_says_nothing(monkeypatch):
    assert _note(monkeypatch, 3) == ""


def test_an_unreadable_probe_says_nothing(monkeypatch):
    """Unknown is not the same as failed: warning on it would cry wolf on every
    page where the probe could not be installed."""
    assert _note(monkeypatch, None) == ""


def test_an_unarmed_probe_says_nothing(monkeypatch):
    assert _note(monkeypatch, 0, armed=False) == ""


@pytest.mark.parametrize("expected", ["pointerdown", "keydown"])
def test_the_probe_watches_both_input_kinds(expected):
    """A click and a keystroke are both "input" for this purpose, and the probe
    is armed once per call, so it must cover both."""
    assert expected in server._PROBE_ARM


def test_the_probe_leaves_no_trace_on_the_page():
    """It runs in an isolated world and stores on globalThis there. A global on the
    page itself would be a detection signal on a server whose point is not
    standing out."""
    assert "globalThis.__ndInput" in server._PROBE_ARM
    assert "window." not in server._PROBE_ARM


def test_cf_verify_says_what_is_missing_instead_of_failing_obscurely(monkeypatch):
    """Regression: cf_verify could never run in a default install.

    opencv was not a dependency at all — not even an optional one — so the
    flagship anti-bot tool always failed, and its error came from inside nodriver
    with nothing an agent could act on.
    """
    monkeypatch.setattr(server.importlib.util, "find_spec", lambda name: None)
    # It raises now rather than returning the text, so the routing layer can mark
    # the result isError — the message is the same either way.
    with pytest.raises(server.ToolFailure) as excinfo:
        asyncio.run(server.cf_verify())
    out = str(excinfo.value)

    assert "opencv" in out
    assert "nodriver-mcp[cf]" in out, "the message must name the way to install it"
    # And it must not leave the impression that the whole server is broken.
    assert "Everything else in this server works without it" in out


def test_the_cf_extra_is_declared():
    """The install hint has to point at something that exists."""
    import tomllib
    from pathlib import Path

    root = Path(server.__file__).resolve().parents[2]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"].get("optional-dependencies", {})
    assert "cf" in extras, "pyproject declares no [cf] extra"
    assert any("opencv" in dep for dep in extras["cf"]), extras["cf"]
