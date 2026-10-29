"""Media-job tests with a stand-in yt-dlp (no network).

Run: python3 tests/test_media.py
"""

import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="haul-media-")
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "data")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "config")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from haul import media, paths, tools  # noqa: E402
from haul.jobs import Manager, Store  # noqa: E402
from haul.settings import Settings  # noqa: E402

paths.ensure()
FAKE = r'''#!/usr/bin/python3
import json, os, sys, time
a = sys.argv[1:]
if "--version" in a: print("2099.01.01"); sys.exit(0)
if "--help" in a: print("--js-runtimes"); sys.exit(0)
with open(os.environ["FAKE_LOG"], "a") as f: f.write(json.dumps(a) + "\n")
if "-J" in a:
    print(json.dumps({"title": "Test Video", "extractor_key": "Youtube", "duration": 61,
        "formats": [{"height": 2160, "vcodec": "vp9"}, {"height": 1080, "vcodec": "avc1"},
                    {"height": 720, "vcodec": "avc1"}, {"vcodec": "none", "acodec": "opus"}],
        "requested_formats": [{"filesize": 700000}, {"filesize_approx": 300000}]}))
    sys.exit(0)
if "--cookies-from-browser" in a:
    print("ERROR: [youtube] Sign in to confirm you're not a bot", file=sys.stderr); sys.exit(1)
P, o = a[a.index("-P") + 1], a[a.index("-o") + 1]
path = os.path.join(P, o.replace("%%", "%").replace(".%(ext)s", ".mp4"))
total = 1000000
for i in range(1, 11):
    print("HAULP\t\t" + json.dumps({"status": "downloading", "downloaded_bytes": i * 100000,
          "total_bytes": total, "speed": 500000, "eta": 10 - i}), flush=True)
    time.sleep(float(os.environ.get("FAKE_DELAY", "0.05")))
print("HAULQMerger", flush=True)
open(path, "wb").write(b"x" * total)
print("HAULF" + path, flush=True)
'''
exe = paths.BIN / "yt-dlp"
exe.write_text(FAKE)
os.chmod(exe, 0o755)
os.environ["FAKE_LOG"] = os.path.join(TMP, "calls.log")
failures = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info and not cond else ""))
    if not cond:
        failures.append(name)


def calls():
    with open(os.environ["FAKE_LOG"]) as f:
        return [json.loads(l) for l in f]


def wait(m, jid, statuses=("done", "error"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        j = m.get(jid)
        if j["status"] in statuses:
            return j
        time.sleep(0.1)
    return m.get(jid)


check("uses managed yt-dlp", tools.ytdlp() == str(exe))
info = media.inspect("https://www.youtube.com/watch?v=x&list=PL1")
check("inspect: title/heights/size", info["title"] == "Test Video" and info["heights"] == [2160, 1080, 720]
      and info["size"] == 1000000, info)
check("inspect: playlist hint from list=", info["in_playlist"] is True)
q = [k for k, _ in media.qualities(info)]
check("qualities", q == ["best", "h2160", "h1080", "h720", "audio_mp3", "audio_best"], q)
check("format: 1080 cap with fallback", media.format_args("h1080")[1].startswith("bv*[height<=1080]"))

DL = os.path.join(TMP, "dl")
s = Settings(paths.SETTINGS)
s.set("download_dir", DL)
s.set("cookies_browser", "firefox")
m = Manager(s, Store(paths.DB))
m.start()

jid = m.add("https://www.youtube.com/watch?v=x", "media", "My 100% Video", options={"quality": "h720"})
j = wait(m, jid)
check("media job completes", j["status"] == "done", j)
check("final path reported", j["path"].endswith("My 100% Video.mp4") and os.path.exists(j["path"]), j["path"])
check("retried without cookies after sign-in error", j["options"].get("no_cookies") is True)
last = calls()[-1]
check("quality passed (MP4 preferred)", "res:720,vcodec:h264,acodec:aac,ext:mp4:m4a" in last)
check("no-playlist for single video", "--no-playlist" in last)
check("headers/progress template present", any(a.startswith("download:HAULP") for a in last))

s.set("cookies_browser", "")
os.environ["FAKE_DELAY"] = "0.4"
jid = m.add("https://vimeo.com/1", "media", "pausable", options={"quality": "best"})
time.sleep(1.5)
mid = m.get(jid)
check("progress parsed while running", mid["status"] == "active" and mid["done"] > 0 and mid["speed"] == 500000, mid)
m.pause(jid)
j = wait(m, jid, ("paused",), 15)
check("pause stops yt-dlp", j["status"] == "paused", j)
os.environ["FAKE_DELAY"] = "0.01"
m.resume(jid)
j = wait(m, jid)
check("resume finishes", j["status"] == "done", j)

m.shutdown()
print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
