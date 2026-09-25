"""A local site for the capture_bodies tests.

Every case capture_bodies claims to handle needs a server that produces it on
demand: a body too large for Chrome's buffer, an event stream that stays open, a
dedicated worker, and a service worker that makes requests of its own. A public
host cannot be relied on for any of those, and the service worker needs an
origin the test controls. 127.0.0.1 counts as a secure context, so plain HTTP is
enough for the worker APIs.
"""

from __future__ import annotations

import hashlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Over Chrome's 10 MB per-resource buffer, so the lazy lookup cannot return it.
BIG_BODY = bytes(range(256)) * (12 * 1024 * 1024 // 256)
BIG_SHA256 = hashlib.sha256(BIG_BODY).hexdigest()

PAGE = b"""<!doctype html><html><head><title>capture site</title></head>
<body><h1>capture site</h1><img src="/img.png" alt="pixel"></body></html>"""

SW_PAGE = b"""<!doctype html><html><head><title>sw page</title></head><body><h1>sw</h1>
<script>navigator.serviceWorker.register('/sw.js');</script></body></html>"""

WORKER = b"fetch('/api/fo/worker').then(r => r.text()).then(t => postMessage(t));"

SERVICE_WORKER = b"""
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/fo/viasw')) e.respondWith(fetch(e.request));
});
self.addEventListener('message', e => {
  fetch('/api/fo/swinternal').then(r => r.text()).then(t => e.source.postMessage(t));
});
"""

# A 1x1 transparent PNG.
PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


# For audit_security: one page with the weaknesses it should report, and one
# configured the way it should stay silent about.
WEAK_HEADERS = [
    ("Content-Security-Policy", "script-src 'self' 'unsafe-inline' 'unsafe-eval'; img-src 'self'"),
    ("X-Powered-By", "PHP/7.4.3"),
    ("Set-Cookie", "session_id=abc123; Path=/"),
    # SameSite=None without Secure: Chrome rejects it and reports why.
    ("Set-Cookie", "tracking=1; SameSite=None"),
]
HARDENED_HEADERS = [
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self' 'nonce-abc'; object-src 'none'; "
     "base-uri 'none'; frame-ancestors 'none'"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "camera=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Set-Cookie", "session_id=x; HttpOnly; SameSite=Lax; Path=/"),
]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 - keep test output readable
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200, extra=()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        path = self.path.split("?", 1)[0]
        port = self.server.server_address[1]
        if path == "/audit":
            # The image comes from another origin, which img-src 'self' blocks.
            page = (
                b"<!doctype html><html><head><title>audit</title></head><body><h1>weak</h1>"
                b"<img src='http://localhost:%d/img.png'></body></html>" % port
            )
            self._send(page, "text/html", extra=WEAK_HEADERS)
        elif path == "/hardened":
            self._send(b"<!doctype html><html><head><title>hardened</title></head><body>ok</body></html>",
                       "text/html", extra=HARDENED_HEADERS)
        elif path == "/api/cors":
            self._send(b'{"cors": true}', "application/json", extra=[
                ("Access-Control-Allow-Origin", "null"),
                ("Access-Control-Allow-Credentials", "true"),
            ])
        elif path == "/api/fo/big":
            self._send(BIG_BODY, "application/octet-stream")
        elif path == "/api/fo/sse":
            # Three events, then the stream stays open the way a live feed does.
            # A capture that waits for the body to end would starve the page.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for i in range(3):
                    self.wfile.write(f"data: tick {i}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.1)
                time.sleep(15)
            except OSError:
                pass  # the page closed the stream
        elif path.startswith("/api/"):
            self._send(b'{"path": "%s"}' % path.encode(), "application/json")
        elif path == "/sw":
            self._send(SW_PAGE, "text/html")
        elif path == "/sw.js":
            self._send(SERVICE_WORKER, "text/javascript")
        elif path == "/w.js":
            self._send(WORKER, "text/javascript")
        elif path == "/img.png":
            self._send(PIXEL, "image/png")
        else:
            self._send(PAGE, "text/html")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        sent = self.rfile.read(length)
        self._send(b'{"echo": %s}' % sent, "application/json")


class CaptureSite:
    """Serves on 127.0.0.1 with an ephemeral port; use as a context manager."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        # Same server, different site: navigating here from `base` is a
        # cross-site navigation, which is what drops Chrome's copy of a body.
        self.other_site = f"http://localhost:{self.port}"

    def __enter__(self) -> "CaptureSite":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
