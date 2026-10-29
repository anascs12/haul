"""Loopback HTTP API for the native messaging host.

Binds 127.0.0.1 on a random port and requires a per-run 256-bit bearer token,
which is written (mode 0600) to ipc.json for the host to read. Requests that
carry an Origin header (i.e. come from a web page) are refused.
"""

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import API, __version__, paths


class IPCServer:
    def __init__(self, handle_add, handle_probe=None, handle_quit=None, prefs=None):
        # (dict) -> dict response, called on a server thread
        self.handle_add = handle_add
        self.handle_probe = handle_probe or (lambda msg: {"ok": False})
        self.handle_quit = handle_quit or (lambda msg: {"ok": False})
        self.prefs = prefs or (lambda: None)
        self.token = secrets.token_hex(32)
        self.httpd = None

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                if self.headers.get("Origin"):
                    return False
                given = self.headers.get("Authorization", "")
                return secrets.compare_digest(given, "Bearer " + owner.token)

            def do_GET(self):
                if not self._authorized():
                    return self._reply(403, {"ok": False})
                if self.path == "/v1/ping":
                    return self._reply(200, {"ok": True, "version": __version__, "api": API,
                                                "prefs": owner.prefs()})
                self._reply(404, {"ok": False})

            def do_POST(self):
                if not self._authorized():
                    return self._reply(403, {"ok": False})
                routes = {"/v1/add": owner.handle_add, "/v1/probe": owner.handle_probe,
                          "/v1/quit": owner.handle_quit}
                if self.path not in routes:
                    return self._reply(404, {"ok": False})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 1 << 20:
                        return self._reply(413, {"ok": False})
                    msg = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(msg, dict) or (self.path != "/v1/quit" and not isinstance(msg.get("url"), str)):
                        return self._reply(400, {"ok": False, "reason": "bad_request"})
                    self._reply(200, routes[self.path](msg))
                except Exception as e:
                    self._reply(500, {"ok": False, "reason": "error", "message": str(e)})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="haul-ipc").start()
        info = {"port": self.httpd.server_address[1], "token": self.token, "pid": os.getpid()}
        tmp = paths.IPC.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(info, f)
        os.replace(tmp, paths.IPC)

    def stop(self):
        try:
            with open(paths.IPC) as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(paths.IPC)
        except (OSError, ValueError):
            pass
        if self.httpd:
            self.httpd.shutdown()
