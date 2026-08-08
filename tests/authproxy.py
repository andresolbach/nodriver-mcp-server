"""A minimal HTTP proxy that demands Basic authentication.

Real proxies are the thing the proxy support exists for, and a mock that never
challenges would not exercise the part that was broken: Chrome stops at a native
dialog no page can dismiss, so the only proof is a proxy that actually asks.

Serves a fixed page for any absolute-URI GET, so the test needs no upstream host
and no network. It speaks only what the test needs — this is a fixture, not a
proxy anyone should route traffic through.
"""

from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USERNAME = "proxyuser"
PASSWORD = "proxypass"
PAGE = b"<html><head><title>via proxy</title></head><body>fetched through the proxy</body></html>"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - keep the test output readable
        pass

    def _challenge(self) -> None:
        self.send_response(407)
        self.send_header("Proxy-Authenticate", 'Basic realm="test"')
        self.send_header("Content-Length", "0")
        self.send_header("Proxy-Connection", "keep-alive")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if not self.server.require_auth:
            self.server.authenticated += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)
            return
        header = self.headers.get("Proxy-Authorization", "")
        expected = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        if header != f"Basic {expected}":
            self.server.challenges += 1
            self._challenge()
            return
        self.server.authenticated += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


class AuthProxy:
    """Runs the proxy on a background thread and counts what it saw."""

    def __init__(self, require_auth: bool = True) -> None:
        # Threading, not the single-threaded HTTPServer: Chrome opens several
        # connections to a proxy at once, and serving them one at a time makes
        # it give up with ERR_PROXY_CONNECTION_FAILED.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.challenges = 0
        self.server.authenticated = 0
        self.server.require_auth = require_auth
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def address(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    @property
    def challenges(self) -> int:
        return self.server.challenges

    @property
    def authenticated(self) -> int:
        return self.server.authenticated

    def __enter__(self) -> AuthProxy:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
