"""Tests for the abandoned-temp-profile sweep.

The sweep deletes directories on disk, so the interesting assertions are not
that it removes things — they are every case where it must keep its hands off.

An earlier design swept anything in the system temp directory named `uc_*` that
was old enough, and relied on a rename failing to protect a profile still in
use. That guard only exists on Windows; POSIX renames a directory happily while
its files are open, so on Linux and macOS a browser left running for a couple of
hours would have had its live profile deleted underneath it. The sweep now only
touches profiles this server recorded as its own, and only once the process that
recorded one is gone.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from nodriver_mcp.server import (
    _claim_temp_profile,
    _pid_alive,
    _release_temp_profile,
    sweep_stale_temp_profiles,
)

DEAD_PID = 0x7FFFFFF0  # implausible, and verified dead below
OLD = 10 * 3600


@pytest.fixture
def claims(tmp_path, monkeypatch):
    """Point both the claim store and the profiles at directories of our own."""
    store = tmp_path / "claims"
    store.mkdir()
    monkeypatch.setattr("nodriver_mcp.server._PROFILE_CLAIMS_DIR", str(store))
    return tmp_path


def _profile(root, name, *, pid, age_s):
    d = root / name
    d.mkdir()
    (d / "Cookies").write_text("session tokens", encoding="utf-8")
    _claim_temp_profile(str(d))
    # Rewrite the claim with the pid and age this test needs.
    from nodriver_mcp.server import _claim_path

    with open(_claim_path(str(d)), encoding="utf-8") as fh:
        claim = json.load(fh)
    claim["pid"] = pid
    claim["created"] = time.time() - age_s
    with open(_claim_path(str(d)), "w", encoding="utf-8") as fh:
        json.dump(claim, fh)
    return d


# ---------------------------------------------------------------------------
# Knowing whether a process is alive is the whole basis of the sweep
# ---------------------------------------------------------------------------

def test_this_process_counts_as_alive():
    assert _pid_alive(os.getpid()) is True


def test_an_unused_pid_counts_as_dead():
    assert _pid_alive(DEAD_PID) is False


@pytest.mark.parametrize("pid", [0, -1, -12345])
def test_nonsense_pids_are_dead_not_errors(pid):
    assert _pid_alive(pid) is False


def test_asking_never_kills_the_process_it_asks_about():
    """os.kill(pid, 0) is the usual trick and is wrong on Windows, where os.kill
    ignores the signal and calls TerminateProcess."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert _pid_alive(proc.pid) is True
        assert _pid_alive(proc.pid) is True
        assert proc.poll() is None, "asking whether it was alive killed it"
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# What it removes
# ---------------------------------------------------------------------------

def test_removes_a_profile_whose_owner_is_gone(claims):
    d = _profile(claims, "uc_abandoned", pid=DEAD_PID, age_s=OLD)
    assert sweep_stale_temp_profiles() == 1
    assert not d.exists()


def test_forgets_the_claim_once_the_profile_is_gone(claims):
    _profile(claims, "uc_abandoned", pid=DEAD_PID, age_s=OLD)
    sweep_stale_temp_profiles()
    assert sweep_stale_temp_profiles() == 0, "the claim outlived the profile"


# ---------------------------------------------------------------------------
# What it must never touch
# ---------------------------------------------------------------------------

def test_keeps_a_profile_whose_owner_is_still_running(claims):
    """The guard that matters. This process is the owner, so however old the
    claim is, the profile is in use."""
    d = _profile(claims, "uc_inuse", pid=os.getpid(), age_s=OLD)
    assert sweep_stale_temp_profiles() == 0
    assert (d / "Cookies").read_text(encoding="utf-8") == "session tokens"


def test_keeps_a_recent_profile_even_if_the_pid_is_gone(claims):
    """A short-lived pid can be recycled onto another process; waiting out the
    age window costs nothing and removes that whole class of mistake."""
    d = _profile(claims, "uc_fresh", pid=DEAD_PID, age_s=60)
    assert sweep_stale_temp_profiles() == 0
    assert d.exists()


def test_never_touches_a_profile_it_did_not_claim(claims):
    """The case the previous design got wrong.

    A `uc_*` directory belonging to another program, another user's nodriver, or
    a version that did not record claims is not ours to delete, no matter how
    old it looks.
    """
    stranger = claims / "uc_someone_elses"
    stranger.mkdir()
    (stranger / "Cookies").write_text("their session", encoding="utf-8")
    old = time.time() - OLD
    os.utime(stranger, (old, old))

    assert sweep_stale_temp_profiles() == 0
    assert (stranger / "Cookies").read_text(encoding="utf-8") == "their session"


def test_an_unreadable_claim_is_dropped_without_guessing(claims):
    """A corrupt note must not turn into a deletion of something inferred."""
    victim = claims / "uc_unrelated"
    victim.mkdir()
    (victim / "Cookies").write_text("keep me", encoding="utf-8")

    from nodriver_mcp.server import _PROFILE_CLAIMS_DIR

    broken = os.path.join(_PROFILE_CLAIMS_DIR, "broken.json")
    with open(broken, "w", encoding="utf-8") as fh:
        fh.write("{not json")

    assert sweep_stale_temp_profiles() == 0
    assert victim.exists() and (victim / "Cookies").exists()
    assert not os.path.exists(broken), "the unreadable claim was kept"


def test_survives_a_missing_claim_store(monkeypatch):
    """Never let bookkeeping stop the server from starting."""
    monkeypatch.setattr(
        "nodriver_mcp.server._PROFILE_CLAIMS_DIR", "/definitely/not/here"
    )
    assert sweep_stale_temp_profiles() == 0


def test_releasing_a_claim_leaves_the_profile_alone(claims):
    """Release is bookkeeping only; the directory belongs to the caller."""
    d = _profile(claims, "uc_kept", pid=DEAD_PID, age_s=OLD)
    _release_temp_profile(str(d))
    assert sweep_stale_temp_profiles() == 0
    assert d.exists()
