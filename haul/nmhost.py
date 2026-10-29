"""Native messaging host: browser extension <-> Haul app.

Started by the browser. Speaks length-prefixed JSON on stdin/stdout, forwards
each message to the app's loopback API, and starts the app in the background
when it isn't running. Kept free of GTK imports so it starts fast.
"""

import json
import os
import signal
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from haul import API, paths  # noqa: E402

LAUNCHER = os.path.expanduser("~/.local/bin/haul")


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (n,) = struct.unpack("=I", raw)
    if n > 64 << 20:
        return None
    return json.loads(sys.stdin.buffer.read(n))


def write_message(obj):
    data = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack("=I", len(data)) + data)
    sys.stdout.buffer.flush()


def _ipc():
    try:
        with open(paths.IPC) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _request(info, path, body=None, timeout=5):
    url = f"http://127.0.0.1:{info['port']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, {"Authorization": "Bearer " + info["token"],
                                             "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _ping(info):
    try:
        return _request(info, "/v1/ping", timeout=2) if info else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _alive(info):
    return bool((_ping(info) or {}).get("ok"))


def _pid_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _stop_old(info):
    """Ask an outdated Haul to quit (it saves its downloads), then make sure it is gone."""
    try:
        _request(info, "/v1/quit", {}, timeout=3)
    except (OSError, ValueError, urllib.error.URLError):
        pass
    pid = info.get("pid")
    deadline = time.monotonic() + 10
    while _pid_running(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _pid_running(pid):
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)


def _launch():
    launcher = LAUNCHER if os.path.exists(LAUNCHER) else None
    cmd = [launcher] if launcher else [sys.executable, os.path.join(str(paths.PROJECT), "bin", "haul")]
    subprocess.Popen(cmd + ["--background"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        time.sleep(0.25)
        info = _ipc()
        if _alive(info):
            return info
    return None


def ensure_app(force_restart=False):
    info = _ipc()
    pong = _ping(info)
    if pong and pong.get("ok"):
        if not force_restart and pong.get("api", 1) >= API:
            return info
        _stop_old(info)  # running Haul is older than this host: restart it on the new code
    return _launch()


def handle(msg):
    kind = msg.get("type")
    if kind == "ping":
        pong = _ping(_ipc()) or {}
        return {"ok": bool(pong.get("ok")), "running": bool(pong.get("ok")), "prefs": pong.get("prefs")}
    if kind in ("download", "probe"):
        path = "/v1/add" if kind == "download" else "/v1/probe"
        for attempt in (0, 1):
            info = ensure_app(force_restart=attempt == 1)
            if not info:
                return {"ok": False, "reason": "no_app", "message": "Haul could not be started."}
            try:
                return _request(info, path, msg, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code != 404 or attempt:
                    return {"ok": False, "reason": "error", "message": f"Haul answered HTTP {e.code}."}
            except (OSError, ValueError, urllib.error.URLError) as e:
                if attempt:
                    return {"ok": False, "reason": "no_app", "message": str(e)}
    return {"ok": False, "reason": "unknown_type"}


def main():
    while True:
        try:
            msg = read_message()
        except (ValueError, OSError):
            return
        if msg is None:
            return
        reply = handle(msg) if isinstance(msg, dict) else {"ok": False}
        if isinstance(msg, dict) and "id" in msg:
            reply["id"] = msg["id"]
        write_message(reply)


if __name__ == "__main__":
    main()
