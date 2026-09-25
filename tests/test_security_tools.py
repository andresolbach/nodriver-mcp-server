"""audit_security, inspect_storage and export_har.

An audit tool fails in two directions: it misses a weakness, or it cries wolf on
a page that is fine — and a report full of false WARNs gets ignored. The local
site serves one page with known weaknesses and one hardened page, and both
directions are asserted.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from capture_site import CaptureSite
from nodriver_mcp import server
from nodriver_mcp.server import _cookie_findings, _csp_findings, _jwt_summary
from test_browser_behaviour import _call, _run


def _jwt(header: dict, claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc(header)}.{enc(claims)}."


# ---------------------------------------------------------------------------
# No browser
# ---------------------------------------------------------------------------

def test_csp_with_a_nonce_is_not_called_unsafe_inline():
    """'unsafe-inline' is ignored by browsers once a nonce or hash is present —
    the standard backwards-compatible way to deploy a nonce CSP."""
    weak = _csp_findings("script-src 'self' 'unsafe-inline'", "", False)
    safe = _csp_findings("script-src 'self' 'unsafe-inline' 'nonce-r4nd0m'; frame-ancestors 'none'", "", False)
    assert any("unsafe-inline" in text for _, text in weak)
    assert not any("unsafe-inline" in text for _, text in safe), safe


def test_csp_weakness_counts_only_if_every_policy_has_it():
    both = _csp_findings("script-src *, script-src 'self'", "", True)
    assert not any("any host" in text for _, text in both), both
    assert any("any host" in text for _, text in _csp_findings("script-src *", "", True))


def test_report_only_csp_is_not_mistaken_for_protection():
    findings = _csp_findings("", "default-src 'self'", True)
    assert findings[0][0] == "WARN" and "Report-Only" in findings[0][1]


def test_jwt_is_decoded_and_its_problems_named():
    summary, warnings = _jwt_summary(_jwt({"alg": "none"}, {"sub": "1", "exp": time.time() + 90 * 86400}))
    assert "alg=none" in summary and "exp=" in summary
    assert any("unsigned" in w for w in warnings) and any("valid for another" in w for w in warnings)
    assert _jwt_summary("not.a.jwt") == ("", [])


def test_cookie_flags():
    cookies = [
        {"name": "session_id", "domain": "a.test", "secure": False, "httpOnly": False},
        {"name": "theme", "domain": "a.test", "secure": True, "httpOnly": False},
        {"name": "csrftoken", "domain": "a.test", "secure": True, "httpOnly": False},
    ]
    text = "\n".join(t for _, t in _cookie_findings(cookies, "a.test", https=True))
    assert "session_id (a.test) has no Secure" in text
    assert "session_id (a.test) looks like a session" in text
    # A CSRF token is meant to be readable by the page; it is not a finding.
    assert "csrftoken (a.test) looks like" not in text


# ---------------------------------------------------------------------------
# Real Chrome
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_audit_reports_the_weak_page_and_passes_the_hardened_one():
    async def scenario():
        with CaptureSite() as site:
            await _call("new_page", url="about:blank")
            await _call("navigate_page", url=site.base + "/audit")
            await _call("evaluate_script", function="async () => (await fetch('/api/cors')).status")
            report = await _call("audit_security")

            # Second, so the weak page's issues and requests are there to leak
            # into this report if a navigation failed to clear them.
            await _call("navigate_page", url=site.base + "/hardened")
            hardened = await _call("audit_security")
        assert "0 WARN" in hardened, f"false alarms on a hardened page:\n{hardened}"
        assert "DevTools Issues)\n  none" in hardened, hardened

        for expected in (
            "'unsafe-inline'", "'unsafe-eval'", "clickjacking", "x-powered-by discloses",
            "session_id (127.0.0.1) looks like a session", "allows the origin 'null'",
            "img-src blocked http://localhost", "ExcludeSameSiteNoneInsecure",
        ):
            assert expected in report, f"missing {expected!r}:\n{report}"
        assert "from the recorded page load" in report, report
        assert "abc123" not in report, "the audit printed a cookie value"

    _run(scenario)


@pytest.mark.slow
def test_audit_gets_headers_when_the_page_load_was_not_recorded():
    async def scenario():
        with CaptureSite() as site:
            await _call("new_page", url=site.base + "/audit")
            server._network_requests.clear()  # as for a tab the page opened itself
            report = await _call("audit_security", checks=["headers"])
        assert "fresh fetch()" in report, report
        assert "'unsafe-eval'" in report, f"the fallback headers were not audited:\n{report}"

    _run(scenario)


@pytest.mark.slow
def test_audit_reports_tls_details():
    async def scenario():
        await _call("new_page", url="https://httpbingo.org/")
        await _call("navigate_page", type="reload")
        report = await _call("audit_security", checks=["tls"])
        assert "https://httpbingo.org: TLS 1." in report, report
        assert "valid until" in report, report

    _run(scenario)


@pytest.mark.slow
def test_inspect_storage_lists_everything_and_masks_tokens():
    token = _jwt({"alg": "none"}, {"sub": "42", "exp": 4102444800})

    async def scenario():
        with CaptureSite() as site:
            await _call("new_page", url=site.base + "/")
            await _call("evaluate_script", function=(
                "async () => {"
                f" localStorage.setItem('access_token', '{token}');"
                " localStorage.setItem('prefs', JSON.stringify({theme: 'dark', auth: {refresh_token: 'r3fr3sh-s3cr3t-value'}}));"
                " sessionStorage.setItem('step', '2');"
                " await new Promise(r => { const q = indexedDB.open('db1', 1);"
                "  q.onupgradeneeded = e => { const s = e.target.result.createObjectStore('store1', {keyPath: 'id'});"
                "   s.put({id: 1}); s.put({id: 2}); };"
                "  q.onsuccess = () => r(); });"
                " const c = await caches.open('c1'); await c.put('/api/user', new Response('{}'));"
                " return 1; }"
            ))
            masked = await _call("inspect_storage")
            revealed = await _call("inspect_storage", reveal_values=True)

        for expected in (
            "localStorage (2 keys)", "sessionStorage (1 key)", "step = 2",
            "db1 v1: store1 (2 entries)", "c1: 1 entries", "alg=none", "Unsigned",
            "localStorage[prefs.auth.refresh_token]",
        ):
            assert expected in masked, f"missing {expected!r}:\n{masked}"
        assert token not in masked and "r3fr3sh-s3cr3t-value" not in masked, f"a credential was printed:\n{masked}"
        assert token in revealed, revealed

    _run(scenario)


@pytest.mark.slow
def test_har_export_carries_wire_headers_post_bodies_and_captured_bodies(tmp_path):
    async def scenario():
        with CaptureSite() as site:
            await _call("new_page", url=site.base + "/")
            await _call("capture_bodies", url_pattern="*/api/*", out_dir=str(tmp_path / "bodies"))
            await _call("navigate_page", url=site.base + "/audit")  # sets session_id
            await _call("evaluate_script", function=(
                "async () => { await (await fetch('/api/fo/x')).text();"
                " await (await fetch('/api/fo/post', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                " body: '{\"q\": 1}'})).text(); return 1; }"
            ))
            await _call("capture_bodies", action="stop")
            result = await _call("export_har", file_path=str(tmp_path / "out.har"))
        assert "from capture_bodies" in result, result

        har = json.loads((tmp_path / "out.har").read_text(encoding="utf-8"))
        assert har["log"]["version"] == "1.2"
        entries = {e["request"]["url"].split(":", 2)[-1].split("/", 1)[-1]: e for e in har["log"]["entries"]}

        post = entries["api/fo/post"]
        assert post["request"]["method"] == "POST"
        assert post["request"]["postData"]["text"] == '{"q": 1}'
        assert json.loads(post["response"]["content"]["text"]) == {"echo": {"q": 1}}

        sent = {h["name"].lower(): h["value"] for h in entries["api/fo/x"]["request"]["headers"]}
        assert "session_id=abc123" in sent.get("cookie", ""), f"no Cookie header on the wire copy: {sent}"
        document = {h["name"].lower(): h["value"] for h in entries["audit"]["response"]["headers"]}
        assert "session_id=abc123" in document.get("set-cookie", ""), document

    _run(scenario)
