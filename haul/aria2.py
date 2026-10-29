"""A private aria2c daemon driven over JSON-RPC on 127.0.0.1.

Haul's own database is the source of truth: aria2 is started without a session
file, and unfinished jobs are re-added under their fixed GID, so aria2 picks up
from its `.aria2` control files after a crash or restart.
"""

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request

from . import paths, tools


class Aria2Error(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Aria2:
    def __init__(self):
        self.proc = None
        self.url = None
        self.secret = None

    def available(self):
        return tools.aria2c()[0] is not None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, seed=False, speed_limit_kib=0):
        exe, env = tools.aria2c()
        if not exe:
            raise RuntimeError("aria2 is not installed. Run: sudo apt install aria2")
        port = _free_port()
        self.secret = secrets.token_hex(16)
        self.url = f"http://127.0.0.1:{port}/jsonrpc"
        args = [
            exe,
            "--enable-rpc", f"--rpc-listen-port={port}", "--rpc-listen-all=false",
            f"--rpc-secret={self.secret}", f"--stop-with-process={os.getpid()}",
            "--max-concurrent-downloads=1000",  # Haul does its own queueing
            "--continue=true", "--always-resume=false", "--allow-overwrite=false",
            "--auto-file-renaming=false", "--remote-time=false",
            "--file-allocation=trunc", "--disk-cache=32M", "--auto-save-interval=10",
            # The system resolver is slower but never trips over IPv6/c-ares quirks.
            "--async-dns=false",
            "--max-tries=10", "--retry-wait=3", "--timeout=30", "--connect-timeout=20",
            "--max-file-not-found=3", "--uri-selector=adaptive",
            "--http-accept-gzip=false", "--content-disposition-default-utf8=true",
            "--check-certificate=true",
            "--enable-dht=true", "--enable-dht6=true", f"--dht-file-path={paths.DHT}",
            f"--dht-file-path6={paths.DHT}6", "--bt-enable-lpd=true",
            "--enable-peer-exchange=true", "--follow-torrent=mem", "--bt-save-metadata=false",
            "--bt-remove-unselected-file=true", "--bt-detach-seed-only=true",
            "--listen-port=6881-6999", "--dht-listen-port=6881-6999",
            "--seed-ratio=1.0" if seed else "--seed-time=0",
            f"--max-overall-download-limit={speed_limit_kib}K",
            f"--log={paths.ARIA2_LOG}", "--log-level=warn", "--console-log-level=error",
            "--summary-interval=0", "--show-console-readout=false",
        ]
        if os.path.exists("/etc/ssl/certs/ca-certificates.crt"):
            args.append("--ca-certificate=/etc/ssl/certs/ca-certificates.crt")
        try:
            os.truncate(paths.ARIA2_LOG, 0)
        except OSError:
            pass
        self.proc = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"aria2 exited during start (code {self.proc.returncode}); see {paths.ARIA2_LOG}")
            try:
                self.call("aria2.getVersion")
                return
            except (OSError, Aria2Error):
                time.sleep(0.1)
        raise RuntimeError("aria2 did not answer on its RPC port")

    def stop(self):
        if not self.alive():
            return
        try:
            self.call("aria2.shutdown", timeout=5)  # flushes control files
            self.proc.wait(10)
        except Exception:
            self.proc.kill()

    def call(self, method, *params, timeout=15):
        # system.multicall takes the token inside each call, not on the outer request.
        if method != "system.multicall":
            params = (f"token:{self.secret}", *params)
        body = json.dumps({"jsonrpc": "2.0", "id": "haul", "method": method,
                           "params": list(params)}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                reply = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                reply = json.load(e)
            except ValueError:
                raise Aria2Error(e.code, str(e)) from None
        if "error" in reply:
            raise Aria2Error(reply["error"].get("code"), reply["error"].get("message", ""))
        return reply["result"]

    def multicall(self, calls):
        """calls: [(method, params...)] -> list of results or Aria2Error instances."""
        if not calls:
            return []
        payload = [{"methodName": m, "params": [f"token:{self.secret}", *p]} for m, *p in calls]
        out = []
        for r in self.call("system.multicall", payload):
            if isinstance(r, dict) and "code" in r and "message" in r:
                out.append(Aria2Error(r["code"], r["message"]))
            else:
                out.append(r[0] if isinstance(r, list) and len(r) == 1 else r)
        return out
