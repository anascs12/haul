"""A misbehaving HTTP server for engine tests."""

import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class State:
    data = b""
    active = 0
    lock = threading.Lock()
    flaky_drops = 0
    max_seen = 0


def make_data(size):
    import random
    rnd = random.Random(42)
    State.data = rnd.randbytes(size)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _range(self, total):
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if not m:
            return None
        a = int(m.group(1)) if m.group(1) else 0
        b = int(m.group(2)) if m.group(2) else total - 1
        return a, min(b, total - 1)

    def _send_file(self, ranges=True, rate=0, limit=0, drop=False, disposition=None):
        data = State.data
        with State.lock:
            State.active += 1
            State.max_seen = max(State.max_seen, State.active)
            over = limit and State.active > limit
        try:
            if over:
                body = b"too many connections"
                self.send_response(403)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            r = self._range(len(data)) if ranges else None
            if r:
                a, b = r
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
            else:
                a, b = 0, len(data) - 1
                self.send_response(200)
            if ranges:
                self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.send_header("Content-Length", str(b - a + 1))
            self.end_headers()
            pos, sent = a, 0
            while pos <= b:
                chunk = data[pos:min(pos + 65536, b + 1)]
                self.wfile.write(chunk)
                pos += len(chunk)
                sent += len(chunk)
                if drop and sent > 2_000_000:
                    with State.lock:
                        if State.flaky_drops < 6:
                            State.flaky_drops += 1
                            self.connection.shutdown(2)
                            return
                if rate:
                    time.sleep(len(chunk) / rate)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with State.lock:
                State.active -= 1

    def do_GET(self):
        p = self.path.split("?")[0]
        q = self.path.split("?")[1] if "?" in self.path else ""
        rate = int(re.search(r"rate=(\d+)", q).group(1)) if "rate=" in q else 0
        if p == "/file.bin":
            return self._send_file(rate=rate)
        if p == "/limited.bin":
            return self._send_file(limit=2, rate=rate)
        if p == "/norange.bin":
            return self._send_file(ranges=False)
        if p == "/flaky.bin":
            return self._send_file(drop=True)
        if p == "/cd":
            return self._send_file(disposition="attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf")
        if p == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file.bin")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/page.html":
            return self._html(200, b"<html><body><h1>Hello</h1>No video here.</body></html>")
        if p == "/challenge":
            self.send_response(403)
            body = b"<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script></html>"
            self.send_header("cf-mitigated", "challenge")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._html(404, b"not found")

    def _html(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(size=48 * 1024 * 1024):
    make_data(size)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, base = serve()
    print(base)
    while True:
        time.sleep(3600)
