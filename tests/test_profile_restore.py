"""Persistent profiles must resume their tabs without a recovery prompt."""
import asyncio
import json
import threading

import pytest

from nodriver_mcp import server
from test_browser_behaviour import _call


async def _wait_for_restored_document(tab, url, timeout=10):
    """TargetInfo can contain the saved URL before its document has loaded.

    The initial about:blank document can even report readyState='complete',
    but has an opaque origin and cannot access sessionStorage. Check both the
    actual document URL and readiness; keep the storage assertion separate.
    """
    state = None
    try:
        async with asyncio.timeout(timeout):
            while True:
                try:
                    state = await server._evaluate_value(
                        tab, "({href: location.href, ready: document.readyState})"
                    )
                except Exception as exc:
                    # A navigation can replace the context between CDP calls.
                    # Do not hide real JavaScript errors (including SecurityError).
                    if not any(message in str(exc) for message in (
                        "Execution context was destroyed", "Cannot find context with specified id",
                    )):
                        raise
                else:
                    if state["href"] == url and state["ready"] in ("interactive", "complete"):
                        return
                await asyncio.sleep(.05)
    except TimeoutError:
        raise AssertionError(f"Restored document {url!r} did not load; last state: {state!r}") from None


@pytest.mark.slow
@pytest.mark.parametrize("startup", ["create_opt_in", "mcp_config", "chrome_setting"])
def test_chrome_continue_setting_restores_tabs_after_two_restarts(tmp_path, monkeypatch, startup):
    monkeypatch.setattr(server, "_PROFILES_DIR", str(tmp_path / "profiles"))
    profile = tmp_path / "profiles" / "restore"
    urls = []
    for name in ("first", "second"):
        page = tmp_path / f"{name}.html"
        page.write_text(f"<title>{name}</title><h1>{name}</h1>", encoding="utf-8")
        urls.append(page.as_uri())

    async def scenario():
        try:
            await _call("create_profile", name="restore", activate=True, restore_tabs=startup == "create_opt_in")
            prefs = profile / "Default" / "Preferences"
            if startup == "chrome_setting":
                data = {"session": {"restore_on_startup": 1}}
                prefs.write_text(json.dumps(data), encoding="utf-8")
            elif startup == "mcp_config":
                await _call("set_browser_flags", startup_mode="restore", restart=False)
            for url in urls:
                await _call("new_page", url=url)
            before_restart = await _call("list_pages")
            assert sorted(t.url for t in server._browser.tabs) == sorted(urls), before_restart
            tab = next(t for t in server._browser.tabs if t.url == urls[1])
            await _wait_for_restored_document(tab, urls[1])
            assert await server._evaluate_value(tab, "sessionStorage.setItem('restore-test', 'kept'); true") is True
            for _ in range(2):
                await _call("close_browser")
                data = json.loads(prefs.read_text(encoding="utf-8"))
                assert data["profile"]["exit_type"] == "Normal", data["profile"]["exit_type"]
                # Listing launches Chrome, but does not itself open any URLs.
                listing = await _call("list_pages")
                for attempt in range(50):
                    browser = await server._get_browser()
                    actual = sorted(t.url for t in browser.tabs)
                    if actual == sorted(urls):
                        break
                    await asyncio.sleep(.1)
                    listing = await _call("list_pages")
                assert actual == sorted(urls), listing
                tab = next(t for t in browser.tabs if t.url == urls[1])
                await _wait_for_restored_document(tab, urls[1])
                value = await server._evaluate_value(tab, "sessionStorage.getItem('restore-test')")
                assert value == "kept", value
        finally:
            await _call("use_temp_profile")

    asyncio.run(scenario())


@pytest.mark.slow
def test_restored_target_url_does_not_mean_its_document_is_ready(tmp_path, monkeypatch):
    """Hold a real HTTP response to reproduce the post-merge CI failure."""
    from capture_site import CaptureSite

    monkeypatch.setattr(server, "_PROFILES_DIR", str(tmp_path / "profiles"))
    hold = threading.Event()
    release = threading.Event()
    requested = threading.Event()

    with CaptureSite() as site:
        original_handler = site._server.RequestHandlerClass

        class DelayedRestoreHandler(original_handler):
            def do_GET(self):
                if self.path == "/second" and hold.is_set():
                    requested.set()
                    if not release.wait(15):
                        self.send_error(504, "Test did not release restored document")
                        return
                super().do_GET()

        site._server.RequestHandlerClass = DelayedRestoreHandler

        async def scenario():
            waiter = None
            try:
                await _call("create_profile", name="delayed", activate=True, restore_tabs=True)
                urls = [site.base + "/first", site.base + "/second"]
                for url in urls:
                    await _call("new_page", url=url)
                await _call("list_pages")
                tab = next(t for t in server._browser.tabs if t.url == urls[1])
                await _wait_for_restored_document(tab, urls[1])
                assert await server._evaluate_value(tab, "sessionStorage.setItem('restore-test', 'kept'); true") is True
                await _call("close_browser")
                hold.set()
                async with asyncio.timeout(10):
                    while True:
                        await _call("list_pages")
                        browser = await server._get_browser()
                        if requested.is_set() and sorted(t.url for t in browser.tabs) == sorted(urls):
                            break
                        await asyncio.sleep(.05)
                tab = next(t for t in browser.tabs if t.url == urls[1])
                state = await server._evaluate_value(tab, "({href: location.href, ready: document.readyState})")
                assert state == {"href": "about:blank", "ready": "complete"}, state
                with pytest.raises(RuntimeError, match="SecurityError.*sessionStorage"):
                    await server._evaluate_value(tab, "sessionStorage.getItem('restore-test')")

                # The new readiness check must not accept the saved TargetInfo
                # URL or the initial document's misleading 'complete' state.
                waiter = asyncio.create_task(_wait_for_restored_document(tab, urls[1]))
                await asyncio.sleep(.15)
                assert not waiter.done(), "Readiness check accepted the initial document"
                release.set()
                await waiter
                assert await server._evaluate_value(tab, "sessionStorage.getItem('restore-test')") == "kept"
            finally:
                release.set()
                if waiter is not None and not waiter.done():
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                await _call("use_temp_profile")

        asyncio.run(scenario())


@pytest.mark.slow
@pytest.mark.parametrize("legacy", [False, True])
def test_profile_starts_with_new_tab_by_default(tmp_path, monkeypatch, legacy):
    monkeypatch.setattr(server, "_PROFILES_DIR", str(tmp_path / "profiles"))
    page = tmp_path / "not-restored.html"
    page.write_text("<title>Do not restore</title>", encoding="utf-8")

    async def scenario():
        try:
            await _call("create_profile", name="fresh", activate=True)
            if legacy:
                (tmp_path / "profiles/fresh/Default/Preferences").write_text("{}", encoding="utf-8")
            await _call("new_page", url=page.as_uri())
            await _call("close_browser")
            listing = await _call("list_pages")
            assert page.as_uri() not in listing, listing
        finally:
            await _call("use_temp_profile")

    asyncio.run(scenario())


def test_creating_an_existing_profile_preserves_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_PROFILES_DIR", str(tmp_path))
    prefs = tmp_path / "existing" / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True)
    content = '{"session":{"restore_on_startup":4,"startup_urls":["https://example.com"]}}'
    prefs.write_text(content, encoding="utf-8")
    asyncio.run(_call("create_profile", name="existing"))
    assert prefs.read_text(encoding="utf-8") == content
    assert not server._restore_profile_tabs(str(tmp_path / "existing"))


def test_ephemeral_profiles_do_not_restore():
    assert not server._restore_profile_tabs(None)


@pytest.mark.slow
def test_startup_config_switches_urls_restore_and_new_tab(tmp_path, monkeypatch):
    from capture_site import CaptureSite
    monkeypatch.setattr(server, "_PROFILES_DIR", str(tmp_path / "profiles"))

    async def tabs():
        await _call("list_pages")
        return sorted(t.url for t in server._browser.tabs)

    async def scenario():
        try:
            with CaptureSite() as site:
                first, second = site.base + "/first", site.base + "/second"
                await _call("create_profile", name="settings", activate=True)
                await _call("new_page", url=site.base + "/original")
                pid = server._browser._process.pid
                before = await tabs()
                reply = await _call("set_browser_flags", startup_mode="urls", startup_urls=[first, second], restart=False)
                assert "saved" in reply, reply
                assert server._browser._process.pid == pid
                assert await tabs() == before
                # Switching away and back proves the choice belongs to the
                # profile and is reloaded from disk, not a runtime global.
                await _call("use_temp_profile")
                await _call("use_profile", name="settings")
                assert await tabs() == sorted([first, second])
                third = site.base + "/third"
                await _call("new_page", url=third)
                await _call("set_browser_flags", startup_mode="restore")
                assert await tabs() == sorted([first, second, third])
                await _call("set_browser_flags", startup_mode="new_tab")
                actual = await tabs()
                assert len(actual) == 1 and actual[0] in server._BLANK_URLS, actual
                status = await _call("set_browser_flags")
                assert '"mode": "new_tab"' in status
        finally:
            await _call("use_temp_profile")

    asyncio.run(scenario())


@pytest.mark.parametrize("urls", [[], ["javascript:alert(1)"], ["https://"], ["https://x.test:bad"], ["https://x.test/\n"]])
def test_invalid_urls_do_not_change_config_or_flags(tmp_path, monkeypatch, urls):
    monkeypatch.setattr(server, "_selected_profile_dir", str(tmp_path))
    monkeypatch.setattr(server, "_enable_translate", False)
    saved = tmp_path / "nodriver-startup.json"
    saved.write_text('{"mode":"restore","urls":[]}', encoding="utf-8")
    original = saved.read_bytes()
    reply = asyncio.run(_call("set_browser_flags", startup_mode="urls", startup_urls=urls, translate=True))
    assert "requires" in reply or "URL" in reply, reply
    assert saved.read_bytes() == original
    assert server._enable_translate is False


def test_startup_requires_persistent_profile(monkeypatch):
    monkeypatch.setattr(server, "_selected_profile_dir", None)
    monkeypatch.delenv("NODRIVER_USER_DATA_DIR", raising=False)
    reply = asyncio.run(_call("set_browser_flags", startup_mode="restore"))
    assert "persistent profile" in reply


@pytest.mark.slow
def test_startup_choice_survives_a_full_mcp_process_restart(tmp_path, monkeypatch):
    from capture_site import CaptureSite
    from test_multiplexer import _Client
    monkeypatch.setenv("NODRIVER_USER_DATA_DIR", str(tmp_path / "persistent"))

    async def scenario():
        with CaptureSite() as site:
            url = site.base + "/saved-start-page"
            async with _Client() as client:
                tools = await client.session.list_tools()
                schema = next(t.inputSchema for t in tools.tools if t.name == "set_browser_flags")
                assert "startup_mode" in schema["properties"]
                assert "startup_urls" in schema["properties"]
                reply = await client.call("set_browser_flags", startup_mode="urls", startup_urls=[url], restart=False)
                assert "saved" in reply, reply
            async with _Client() as client:
                status = await client.call("set_browser_flags")
                assert '"mode": "urls"' in status and url in status
                pages = await client.call("list_pages")
                assert url in pages, pages
                assert "about:blank" not in pages and "chrome://newtab" not in pages
                await client.call("close_browser")

    asyncio.run(scenario())
