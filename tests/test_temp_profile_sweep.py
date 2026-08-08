"""Tests for the abandoned-temp-profile sweep.

The sweep deletes directories it did not create, in a directory shared with
every other program on the machine. So the interesting assertions are not that
it removes things — they are all the cases where it must keep its hands off.
"""

from __future__ import annotations

import os
import time

import pytest

from nodriver_mcp.server import sweep_stale_temp_profiles


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Point the sweep at a directory of our own, never the real one."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    return tmp_path


def _aged_dir(root, name, age_s):
    d = root / name
    d.mkdir()
    (d / "Preferences").write_text("{}", encoding="utf-8")
    old = time.time() - age_s
    os.utime(d, (old, old))
    return d


def test_removes_an_abandoned_profile(temp_root):
    d = _aged_dir(temp_root, "uc_abandoned", 10 * 3600)
    assert sweep_stale_temp_profiles() == 1
    assert not d.exists()


def test_keeps_a_recent_profile(temp_root):
    """A young directory may belong to a browser that is running right now."""
    d = _aged_dir(temp_root, "uc_fresh", 60)
    assert sweep_stale_temp_profiles() == 0
    assert d.exists()


def test_ignores_directories_that_are_not_nodriver_profiles(temp_root):
    """The temp directory belongs to the whole machine, not to us."""
    keep = _aged_dir(temp_root, "important_backup", 10 * 3600)
    also = _aged_dir(temp_root, "pip-build-xyz", 10 * 3600)
    assert sweep_stale_temp_profiles() == 0
    assert keep.exists() and also.exists()


def test_ignores_files_that_merely_start_with_the_prefix(temp_root):
    f = temp_root / "uc_notadirectory.log"
    f.write_text("x", encoding="utf-8")
    old = time.time() - 10 * 3600
    os.utime(f, (old, old))
    assert sweep_stale_temp_profiles() == 0
    assert f.exists()


def test_a_locked_profile_is_left_whole(temp_root):
    """The guard that matters: a profile still in use must not be touched.

    Windows refuses to rename a directory that holds an open file, which is what
    the sweep relies on. Deleting in place would strip files out from under a
    running Chrome instead — a far worse outcome than a leftover directory.
    """
    d = _aged_dir(temp_root, "uc_inuse", 10 * 3600)
    locked = d / "Cookies"
    locked.write_text("session tokens", encoding="utf-8")
    handle = open(locked, "r+b")  # noqa: SIM115 - held open on purpose
    try:
        sweep_stale_temp_profiles()
        assert locked.exists(), "the sweep deleted a file from a profile in use"
        assert locked.read_bytes() == b"session tokens"
    finally:
        handle.close()


def test_survives_a_temp_directory_it_cannot_read(monkeypatch):
    """Never let a broken temp directory stop the server from starting."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: "/definitely/not/here")
    assert sweep_stale_temp_profiles() == 0
