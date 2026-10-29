"""End-to-end engine tests against the local misbehaving server.

Run: python3 tests/test_engine.py
"""

import hashlib
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="haul-test-")
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "data")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "config")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from haul import paths, probe  # noqa: E402
from haul.jobs import Manager, Store, PART  # noqa: E402
from haul.settings import Settings  # noqa: E402
import testserver  # noqa: E402

paths.ensure()
_real_vendor = os.path.expanduser("~/.local/share/haul/vendor")
if os.path.isdir(_real_vendor):
    os.symlink(_real_vendor, paths.DATA / "vendor")
srv, BASE = testserver.serve()
SHA = hashlib.sha256(testserver.State.data).hexdigest()
DL = os.path.join(TMP, "downloads")
failures = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info and not cond else ""))
    if not cond:
        failures.append(name)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def wait(mgr, jid, statuses=("done", "error"), timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = mgr.get(jid)
        if j and j["status"] in statuses:
            return j
        time.sleep(0.2)
    return mgr.get(jid)


def make_manager():
    s = Settings(paths.SETTINGS)
    s.set("download_dir", DL)
    m = Manager(s, Store(paths.DB))
    m.start()
    assert not m.aria_error, m.aria_error
    return m


# ---- probe ----
p = probe.http_probe(BASE + "/file.bin")
check("probe: file is http, resumable, sized", p.kind == "http" and p.resumable and p.size == len(testserver.State.data), p)
p = probe.http_probe(BASE + "/norange.bin")
check("probe: no-range server is not resumable", p.kind == "http" and not p.resumable, p)
p = probe.http_probe(BASE + "/cd")
check("probe: RFC 5987 filename", p.name == "résumé.pdf", p.name)
p = probe.http_probe(BASE + "/redirect")
check("probe: follows redirects", p.final_url.endswith("/file.bin") and p.name == "file.bin", p)
p = probe.http_probe(BASE + "/challenge")
check("probe: robot check detected", p.error == "browser_check", p)
p = probe.http_probe(BASE + "/missing")
check("probe: 404 detected", p.error == "not_found", p)
p = probe.http_probe(BASE + "/page.html")
check("probe: html page routed to media check", p.kind == "media" and p.error == "web_page", p)
p = probe.http_probe("http://127.0.0.1:9/nothing")
check("probe: unreachable server", p.error == "network", p)
check("probe: magnet", probe.classify("magnet:?xt=urn:btih:abc&dn=Ubuntu+ISO").name == "Ubuntu ISO")

mgr = make_manager()

# ---- plain multi-connection download ----
t0 = time.time()
jid = mgr.add(BASE + "/file.bin", "http", "file.bin", options={"resumable": True})
j = wait(mgr, jid)
check("download completes", j["status"] == "done", j)
check("download checksum", j["status"] == "done" and sha(j["path"]) == SHA)
check("no part file left", not os.path.exists(os.path.join(DL, "file.bin" + PART)))
print(f"     48 MiB in {time.time() - t0:.2f}s, peak server connections {testserver.State.max_seen}")
check("used multiple connections", testserver.State.max_seen >= 4, testserver.State.max_seen)

# ---- same name again gets a unique name ----
jid = mgr.add(BASE + "/file.bin", "http", "file.bin", options={"resumable": True})
j = wait(mgr, jid)
check("second copy gets a unique name", j["status"] == "done" and j["name"] == "file (2).bin", j["name"])

# ---- pause / resume mid-download ----
jid = mgr.add(BASE + "/file.bin?rate=150000", "http", "slow.bin", options={"resumable": True})
time.sleep(3)
before = mgr.get(jid)["done"]
mgr.pause(jid)
time.sleep(1.5)
paused = mgr.get(jid)
check("pause stops", paused["status"] == "paused" and paused["speed"] == 0, paused)
check("part + control file kept while paused",
      os.path.exists(os.path.join(DL, "slow.bin" + PART)) and os.path.exists(os.path.join(DL, "slow.bin" + PART + ".aria2")))
mgr.resume(jid)
time.sleep(2)
resumed = mgr.get(jid)
check("resume continues from where it stopped", resumed["done"] >= before, (before, resumed["done"]))

# ---- crash: kill the whole manager + aria2, start again ----
os.kill(mgr.aria.proc.pid, 9)
mgr._stop.set()
time.sleep(1)
for job in mgr.jobs.values():
    mgr.store.save(job)
mgr2 = Manager(mgr.settings, Store(paths.DB))
mgr2.start()
j = mgr2.get(jid)
check("job survives a crash", j is not None and j["status"] in ("queued", "active"), j)
time.sleep(3)
progress_after = mgr2.get(jid)["done"]
check("resumed after crash without starting over", progress_after > 0, progress_after)
mgr2.remove(jid, delete_files=True)
time.sleep(1.5)
check("remove + delete files", not os.path.exists(os.path.join(DL, "slow.bin" + PART)))

# ---- aria2 dies while running: manager restarts it ----
jid = mgr2.add(BASE + "/file.bin?rate=8000000", "http", "restart.bin", options={"resumable": True})
time.sleep(2)
os.kill(mgr2.aria.proc.pid, 9)
j = wait(mgr2, jid, timeout=90)
check("finishes after aria2 was killed", j["status"] == "done" and sha(j["path"]) == SHA, j)

# ---- server that drops connections ----
jid = mgr2.add(BASE + "/flaky.bin", "http", "flaky.bin", options={"resumable": True})
j = wait(mgr2, jid, timeout=120)
check("flaky server: completes with correct data", j["status"] == "done" and sha(j["path"]) == SHA, j)

# ---- server limits connections (403 beyond 2) ----
testserver.State.max_seen = 0
jid = mgr2.add(BASE + "/limited.bin?rate=20000000", "http", "limited.bin", options={"resumable": True})
j = wait(mgr2, jid, timeout=120)
check("connection-limited server: completes", j["status"] == "done" and sha(j["path"]) == SHA, j)

# ---- no range support ----
jid = mgr2.add(BASE + "/norange.bin", "http", "norange.bin", options={"resumable": False})
j = wait(mgr2, jid)
check("no-range server: single connection completes", j["status"] == "done" and sha(j["path"]) == SHA, j)

# ---- 404 fails fast with a clear message ----
jid = mgr2.add(BASE + "/missing.bin", "http", "missing.bin")
j = wait(mgr2, jid, timeout=60)
check("404 fails (not retried forever)", j["status"] == "error", j)
print("     message:", j["error"][:100])

mgr2.shutdown()
print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
