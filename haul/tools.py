"""Locating aria2c / yt-dlp / ffmpeg, and installing or updating yt-dlp.

yt-dlp is fetched from its official GitHub releases and checked against the
published SHA2-256SUMS before it replaces the current binary. A stale yt-dlp is
the usual reason video downloads stop working, so it is updated at most once a
day in the background.
"""

import hashlib
import os
import shutil
import subprocess
import time
import urllib.request

from . import paths

YTDLP_RELEASE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
YTDLP_ASSET = "yt-dlp_linux"
UPDATE_EVERY = 24 * 3600


def aria2c():
    """(argv0, env) for aria2c: the system one, else the copy unpacked from Ubuntu's .debs."""
    system = shutil.which("aria2c")
    if system:
        return system, None
    local = paths.VENDOR / "usr/bin/aria2c"
    if local.exists():
        env = dict(os.environ)
        lib = str(paths.VENDOR / "usr/lib/x86_64-linux-gnu")
        env["LD_LIBRARY_PATH"] = lib + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        return str(local), env
    return None, None


def managed_ytdlp():
    return paths.BIN / "yt-dlp"


def ytdlp():
    p = managed_ytdlp()
    if p.exists() and os.access(p, os.X_OK):
        return str(p)
    return shutil.which("yt-dlp")


def ffmpeg():
    return shutil.which("ffmpeg")


def js_runtime():
    """yt-dlp needs a JavaScript runtime for YouTube; deno is its default, node also works."""
    for name in ("deno", "node", "bun"):
        p = shutil.which(name)
        if p:
            return name, p
    return None, None


_help_cache = {}


def ytdlp_supports(flag):
    exe = ytdlp()
    if not exe:
        return False
    key = (exe, os.path.getmtime(exe))
    if key not in _help_cache:
        try:
            _help_cache.clear()
            _help_cache[key] = subprocess.run([exe, "--help"], capture_output=True, text=True,
                                              timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return False
    return flag in _help_cache[key]


def ytdlp_version():
    exe = ytdlp()
    if not exe:
        return None
    try:
        return subprocess.run([exe, "--version"], capture_output=True, text=True,
                              timeout=30).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "haul-download-manager"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install_ytdlp(force=False):
    """Install or update the managed yt-dlp. Returns (changed, message). Raises on failure."""
    paths.ensure()
    sums = _fetch(YTDLP_RELEASE + "SHA2-256SUMS").decode()
    want = next((line.split()[0] for line in sums.splitlines()
                 if line.split()[-1:] == [YTDLP_ASSET]), None)
    if not want:
        raise RuntimeError("the yt-dlp release has no checksum for " + YTDLP_ASSET)
    target = managed_ytdlp()
    if not force and target.exists() and _sha256(target) == want:
        _stamp()
        return False, "yt-dlp is up to date"
    tmp = target.with_suffix(".download")
    req = urllib.request.Request(YTDLP_RELEASE + YTDLP_ASSET,
                                 headers={"User-Agent": "haul-download-manager"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if _sha256(tmp) != want:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("the downloaded yt-dlp did not match its published checksum")
    os.chmod(tmp, 0o755)
    os.replace(tmp, target)
    _help_cache.clear()
    _stamp()
    return True, "yt-dlp " + (ytdlp_version() or "") + " installed"


def _stamp():
    (paths.DATA / "ytdlp-checked").write_text(str(int(time.time())))


def ytdlp_update_due():
    if not managed_ytdlp().exists():
        return False
    try:
        last = int((paths.DATA / "ytdlp-checked").read_text())
    except (OSError, ValueError):
        return True
    return time.time() - last > UPDATE_EVERY
