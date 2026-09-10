"""
server.py -- the twin, driven from the browser.

    python -m extrusion_twin serve --port 8000

Standard library only. It serves dashboard.html and puts HTTP in front of `api.TwinAPI`;
the routing and the run manager live there, so the browser build of the console behaves
identically without a second implementation to keep in step.

Everything lives under one directory (default ./twin_data): runs/, configs/, corpus/.
One run at a time, on a worker thread. Starting a run while one is going is refused.
"""
from __future__ import annotations

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

from .api import ThreadRunManager, TwinAPI, defaults, list_ports, merged_config  # noqa: F401  (re-export)

HERE = os.path.dirname(os.path.abspath(__file__))

class Handler(BaseHTTPRequestHandler):
    api: TwinAPI = None                                       # set in serve()
    token: Optional[str] = None                               # set in serve() when --token is given
    server_version = "extrusion-twin"

    def authorised(self) -> bool:
        """No token configured means no check -- that is right for localhost. Once the console is
        bound to a network address it should have one, because anything on that network can reach it."""
        if not self.token:
            return True
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [None])[0] == self.token:
            return True
        return f"twin_token={self.token}" in (self.headers.get("Cookie") or "")

    def deny(self):
        body = (b"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                b"<body style='font:16px system-ui;margin:16vh 24px;color:#12171a;max-width:34em'>"
                b"<h1 style='font-size:22px'>This console needs its key</h1>"
                b"<p style='color:#6e7a79'>Open it with the key on the end of the address, the way it was "
                b"printed when the console started:</p>"
                b"<p style='font-family:ui-monospace,monospace;font-size:14px'>http://&lt;address&gt;:&lt;port&gt;/?token=&hellip;</p>"
                b"<p style='color:#6e7a79'>The key is stored in your browser after the first visit, so you only "
                b"paste it once on each device.</p></body>")
        self._send(403, body, "text/html; charset=utf-8")

    def log_message(self, *a):                                # keep the console for the twin's own output
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # -- GET
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            return self._send(204, b"", "image/png")       # the page carries an inline SVG icon
        if not self.authorised():
            return self.deny() if path in ("/", "/dashboard.html") else self._json(dict(error="unauthorised"), 403)
        if path in ("/", "/dashboard.html"):
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                body = f.read()
            extra = ()
            if self.token:                                    # remember the key so it is pasted once per device
                extra = (("Set-Cookie", f"twin_token={self.token}; Path=/; Max-Age=31536000; SameSite=Lax"),)
            return self._send(200, body, "text/html; charset=utf-8", extra)
        if path.startswith("/api/"):
            code, obj = self.api.get(path)
            return self._json(obj, code)
        return self._json(dict(error="not found"), 404)

    # -- POST
    def do_POST(self):
        path = urlparse(self.path).path
        if not self.authorised():
            return self._json(dict(error="unauthorised"), 403)
        try:
            body = self._body()
        except Exception:
            return self._json(dict(error="bad json"), 400)
        if path.startswith("/api/"):
            code, obj = self.api.post(path, body)
            return self._json(obj, code)
        return self._json(dict(error="not found"), 404)


def open_url(url: str):
    """webbrowser does not work under Termux; termux-open-url does."""
    try:
        import shutil
        if shutil.which("termux-open-url"):
            import subprocess
            subprocess.Popen(["termux-open-url", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        webbrowser.open(url)
    except Exception:
        pass                                              # not being able to open a browser is not an error


def serve(port: int = 8000, root: str = "twin_data", open_browser: bool = True,
          host: str = "127.0.0.1", token: Optional[str] = None):
    manager = ThreadRunManager(os.path.abspath(root))
    Handler.api = TwinAPI(manager, env=dict(live=True, kind="local"))
    if token == "auto":
        import secrets
        keep = os.path.join(os.path.abspath(root), "console_token.txt")
        if os.path.exists(keep):
            token = open(keep).read().strip()
        else:
            token = secrets.token_urlsafe(9)
            os.makedirs(os.path.abspath(root), exist_ok=True)
            with open(keep, "w") as f:
                f.write(token)
            os.chmod(keep, 0o600)
    Handler.token = token
    httpd = ThreadingHTTPServer((host, port), Handler)
    suffix = f"?token={token}" if token else ""
    url = f"http://localhost:{port}/{suffix}"
    print(f"twin console on {url}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"from another device on this network: http://<this machine's address>:{port}/{suffix}")
        if not token:
            print("warning: bound to a network address with no key. Anything on that network can drive the twin.")
    print(f"runs, configs and the corpus are kept in {os.path.abspath(root)}")
    print("ctrl-c to stop")
    if open_browser:
        threading.Timer(0.7, lambda: open_url(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
