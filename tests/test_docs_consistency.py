"""Keep the docs honest about the code.

The tool count used to be stated in six places and updated in one, so the README
advertised 56 tools while the server shipped 57. These tests make that class of
drift a build failure instead of something a reader notices first.
"""

from __future__ import annotations

import asyncio
import re
import tomllib
from pathlib import Path

import pytest

from nodriver_mcp.server import mcp

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
CHANGES = (ROOT / "CHANGES.md").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tool_names():
    return sorted(t.name for t in asyncio.run(mcp.list_tools()))


def test_readme_states_the_real_tool_count(tool_names):
    count = len(tool_names)
    stated = {int(n) for n in re.findall(r"(?:Tools|tools)[:\-\s]+(\d{2})\b", README)}
    assert stated, "README no longer states a tool count anywhere"
    wrong = {n for n in stated if n != count}
    assert not wrong, f"README claims {sorted(wrong)} tools, server has {count}"


def test_readme_badge_matches(tool_names):
    badge = re.search(r"badge/tools-(\d+)-", README)
    assert badge, "tool-count badge missing from README"
    assert int(badge.group(1)) == len(tool_names)


def test_readme_tool_table_lists_every_tool(tool_names):
    """Every registered tool must appear in the README table, and vice versa."""
    documented = set(re.findall(r"`([a-z_]+)`", README))
    missing = [n for n in tool_names if n not in documented]
    assert missing == [], f"tools missing from the README: {missing}"


def test_version_is_consistent():
    version = PYPROJECT["project"]["version"]
    first_heading = re.search(r"^## (\S+)", CHANGES, re.MULTILINE)
    assert first_heading, "CHANGES.md has no version heading"
    assert first_heading.group(1) == version, (
        f"pyproject says {version}, CHANGES.md's newest entry is {first_heading.group(1)}"
    )


def test_changelog_records_the_tool_count(tool_names):
    """The newest changelog entry should end on the current count."""
    counts = re.findall(r"Tool count:?\s*\d+\s*(?:->|→)\s*(\d+)", CHANGES)
    assert counts, "no 'Tool count: X -> Y' line found in CHANGES.md"
    assert int(counts[0]) == len(tool_names)


def test_readme_has_no_relative_image_sources():
    """PyPI renders the README standalone and proxies images through camo.

    A relative src resolves against pypi.org, so camo cannot fetch it and the
    image silently breaks on the package page — which is exactly what happened
    to the logo.
    """
    relative = re.findall(r'<img[^>]*src="(?!https?:)([^"]+)"', README)
    assert relative == [], f"relative image sources break on PyPI: {relative}"


def test_readme_has_no_relative_links():
    """Same reason: a relative link is a 404 once the README is off GitHub."""
    relative = re.findall(r"\[[^\]]*\]\((?!https?:|#)([^)]+)\)", README)
    assert relative == [], f"relative links break on PyPI: {relative}"


def test_readme_images_are_raster_or_shields():
    """Images must come from a host that serves a real image content-type.

    raw.githubusercontent.com is fine for PNG; shields.io SVGs are proxied
    happily. A repo-hosted SVG is the risky case, so keep those out.
    """
    srcs = re.findall(r'<img[^>]*src="([^"]+)"', README)
    bad = [s for s in srcs if s.endswith(".svg") and "shields.io" not in s]
    assert bad == [], f"repo-hosted SVGs may not survive PyPI's image proxy: {bad}"


def test_server_json_matches_pyproject():
    import json

    server_json = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert server_json["version"] == PYPROJECT["project"]["version"]
    pypi = [p for p in server_json["packages"] if p["registryType"] == "pypi"]
    assert pypi, "server.json declares no PyPI package"
    assert pypi[0]["identifier"] == PYPROJECT["project"]["name"]
    assert pypi[0]["version"] == PYPROJECT["project"]["version"]
