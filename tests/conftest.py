"""Shared test setup.

Only the browser tests need anything here, and only in environments where
Chrome cannot start with its normal sandbox.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from nodriver_mcp.server import mcp


@pytest.fixture(scope="session", autouse=True)
def _extra_browser_flags():
    """Apply NODRIVER_TEST_EXTRA_ARGS before anything launches Chrome.

    GitHub's ubuntu runners are 24.04, which blocks unprivileged user
    namespaces, so Chrome's sandbox cannot start and nodriver reports only
    "Failed to connect to browser". CI passes --no-sandbox through this.

    It lives in the test layer rather than in the server because it is a
    property of the machine the tests run on, not something the product should
    offer to weaken on request.
    """
    args = os.environ.get("NODRIVER_TEST_EXTRA_ARGS", "").split()
    if not args:
        return
    # restart=False: nothing has launched yet, and restarting would start a
    # browser these flags exist to make startable in the first place.
    asyncio.run(mcp.call_tool("set_browser_flags", {"extra_args": args, "restart": False}))
