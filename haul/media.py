"""yt-dlp integration: inspecting pages and building download commands."""

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

from . import paths, tools
from .probe import sanitize

STANDARD_HEIGHTS = (4320, 2160, 1440, 1080, 720, 480, 360, 240, 144)
PROGRESS = "HAULP"
POSTPROC = "HAULQ"
FINAL = "HAULF"


def available():
    return tools.ytdlp() is not None


def _header_args(headers):
    args = []
    for k, v in (headers or {}).items():
        if not v:
            continue
        if k.lower() == "user-agent":
            args += ["--user-agent", v]
        else:
            # yt-dlp scopes a Cookie header to the URL's domain, so it is not leaked on redirects.
            args += ["--add-header", f"{k}:{v}"]
    return args


def _common_args(headers, cookies_browser):
    args = ["--ignore-config", "--no-color", "--no-warnings", "--socket-timeout", "30"]
    name, path = tools.js_runtime()
    if name and name != "deno" and tools.ytdlp_supports("--js-runtimes"):
        args += ["--js-runtimes", f"{name}:{path}"]
    if cookies_browser:
        args += ["--cookies-from-browser", cookies_browser]
    return args + _header_args(headers)


def _error_text(stderr):
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    errors = [l for l in lines if l.startswith("ERROR")]
    text = (errors or lines or ["yt-dlp failed"])[-1]
    return text.removeprefix("ERROR:").strip()[:400]


# What yt-dlp learned about a page is kept so the download can start from it
# (--load-info-json) instead of reading the whole page again: that is most of the
# start-up delay. Stream URLs expire after some hours, hence the TTL.
INFO_TTL = 3 * 3600
_lock = threading.Lock()
_info = {}        # key -> (time, path, summary)
_inflight = {}    # key -> Event


def cache_key(url):
    """One key per video: YouTube links differ in list=, index=, t=... but are the same video."""
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    if re.search(r"(^|\.)(youtube\.com|youtube-nocookie\.com)$", host) or host == "youtu.be":
        vid = urllib.parse.parse_qs(u.query).get("v", [""])[0]
        if not vid and host == "youtu.be":
            vid = u.path.strip("/")
        m = re.match(r"^/(?:shorts|embed|live)/([\w-]+)", u.path)
        vid = vid or (m.group(1) if m else "")
        if vid:
            return "yt:" + vid
    return url


def cached_info(url, wait=0):
    """Path of a fresh info JSON for this page, waiting up to `wait` s for one being made."""
    key = cache_key(url)
    with _lock:
        ev = _inflight.get(key)
    if ev and wait:
        ev.wait(wait)
    with _lock:
        hit = _info.get(key)
    if hit and time.time() - hit[0] < INFO_TTL and os.path.exists(hit[1]):
        return hit[1]
    return None


def inspect(url, headers=None, cookies_browser=""):
    """Summary of what's on a page. Concurrent calls for the same video share one yt-dlp run."""
    key = cache_key(url)
    with _lock:
        hit = _info.get(key)
        if hit and time.time() - hit[0] < INFO_TTL and hit[2]:
            return hit[2]
        ev = _inflight.get(key)
        owner = ev is None
        if owner:
            ev = _inflight[key] = threading.Event()
    if not owner:
        ev.wait(120)
        with _lock:
            hit = _info.get(key)
        return hit[2] if hit else _inspect(url, headers, cookies_browser, key)
    try:
        return _inspect(url, headers, cookies_browser, key)
    finally:
        with _lock:
            _inflight.pop(key, None)
        ev.set()


def _inspect(url, headers, cookies_browser, key):
    exe = tools.ytdlp()
    if not exe:
        return {"error": "yt-dlp is not installed"}
    cmd = [exe, "-J", "--flat-playlist", "--no-playlist", "--skip-download",
           *_common_args(headers, cookies_browser), "--", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return {"error": "The site took too long to answer."}
    except OSError as e:
        return {"error": str(e)}
    if r.returncode != 0 or not r.stdout.strip():
        if cookies_browser:  # signed-in cookies are sometimes rejected (YouTube); try without
            return _inspect(url, headers, "", key)
        return {"error": _error_text(r.stderr)}
    try:
        info = json.loads(r.stdout)
    except ValueError:
        return {"error": "yt-dlp returned unreadable data"}
    summary = summarize(info, url)
    if not summary["is_playlist"]:
        cache = paths.DATA / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / (hashlib.sha1(key.encode()).hexdigest() + ".info.json")
        path.write_text(r.stdout)
        with _lock:
            _info[key] = (time.time(), str(path), summary)
    return summary


def summarize(info, url=""):
    is_playlist = info.get("_type") in ("playlist", "multi_video")
    formats = info.get("formats") or []
    heights = sorted({f["height"] for f in formats
                      if isinstance(f.get("height"), int) and f.get("vcodec") not in (None, "none")},
                     reverse=True)
    has_video = bool(heights) or any(f.get("vcodec") not in (None, "none") for f in formats)
    size = 0
    for f in info.get("requested_formats") or [info]:
        size += f.get("filesize") or f.get("filesize_approx") or 0
    duration = info.get("duration") or 0

    def fsize(f):
        if not f:
            return 0
        exact = f.get("filesize") or f.get("filesize_approx")
        if exact:
            return int(exact)
        return int(f["tbr"] * 125 * duration) if f.get("tbr") and duration else 0

    audio = [f for f in formats if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    best_audio = max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    sizes = {}
    for h in heights:  # estimated size of the best video at this height plus audio
        video = [f for f in formats if f.get("height") == h and f.get("vcodec") not in (None, "none")]
        v = max(video, key=lambda f: (f.get("ext") == "mp4", f.get("tbr") or 0))
        sizes[h] = fsize(v) + (fsize(best_audio) if v.get("acodec") in (None, "none") else 0)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return {
        "title": info.get("title") or info.get("id") or "video",
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        "is_playlist": is_playlist,
        "count": len(info.get("entries") or []) if is_playlist else 1,
        "in_playlist": not is_playlist and "list" in q,
        "duration": info.get("duration") or 0,
        "heights": heights,
        "has_video": has_video or is_playlist,
        "is_live": bool(info.get("is_live")),
        "size": int(size),
        "sizes": sizes,
        "audio_size": fsize(best_audio),
        "uploader": info.get("uploader") or info.get("channel") or "",
    }


def choices(info, prefer_mp4=True):
    """Menu entries for the in-page download button: [{id, label, detail}]."""
    def human(n):
        if not n:
            return ""
        n = float(n)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"~{n:.0f} {unit}" if unit != "GB" else f"~{n:.1f} GB"
            n /= 1024

    sizes = {int(k): v for k, v in (info.get("sizes") or {}).items()}
    container = "MP4" if prefer_mp4 else "Best format"
    out = []
    for key, label in qualities(info):
        if key == "best" or key == "audio_best":
            continue
        if key.startswith("h"):
            h = int(key[1:])
            size = max((s for hh, s in sizes.items() if hh <= h and hh >= h * 0.9), default=0)
            tag = " 4K" if h == 2160 else " HD" if h >= 720 else ""
            out.append({"id": key, "label": f"{h}p{tag}", "detail": " · ".join(x for x in (container, human(size)) if x)})
        elif key == "audio_mp3":
            out.append({"id": key, "label": "Audio", "detail": " · ".join(x for x in ("MP3", human(info.get("audio_size"))) if x)})
    if not out:
        out.append({"id": "best", "label": "Best quality", "detail": container})
    out.append({"id": "audio_best", "label": "Audio", "detail": "original format (M4A/Opus)"})
    return out


def qualities(info):
    """[(key, label)] for the quality picker."""
    out = [("best", "Best available")]
    if info.get("has_video", True):
        heights = info.get("heights") or STANDARD_HEIGHTS[2:7]
        seen = set()
        for h in heights:
            std = next((s for s in STANDARD_HEIGHTS if h >= s), h)
            if std not in seen:
                seen.add(std)
                out.append((f"h{std}", f"{std}p" + (" (4K)" if std == 2160 else "")))
    out += [("audio_mp3", "Audio only (MP3)"), ("audio_best", "Audio only (original quality)")]
    return out


def format_args(quality, prefer_mp4=False):
    """yt-dlp selection. Resolution always wins; with prefer_mp4, MP4/M4A is chosen when
    the site offers it at that resolution (4K on YouTube is only VP9/AV1, so it stays 4K)."""
    if quality == "audio_mp3":
        return ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    if quality == "audio_best":
        return ["-f", "ba/b", "-x"]
    res = f"res:{quality[1:]}" if quality.startswith("h") and quality[1:].isdigit() else "res"
    if prefer_mp4:
        # H.264 + AAC is what "plays everywhere" means (AV1/VP9 in MP4 doesn't on older devices).
        return ["-f", "bv*+ba/b", "-S", f"{res},vcodec:h264,acodec:aac,ext:mp4:m4a"]
    if res != "res":
        h = quality[1:]
        return ["-f", f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"]
    return ["-f", "bv*+ba/b"]


def command(job, cookies_browser="", rate_kib=0):
    o = job.options
    playlist = bool(o.get("playlist"))
    if playlist:
        template = ("%(playlist_title,playlist_id|Playlist).100B/"
                    "%(playlist_index)03d - %(title).150B [%(id)s].%(ext)s")
    else:
        template = sanitize(job.name).replace("%", "%%")[:180] + ".%(ext)s"
    cmd = [
        tools.ytdlp(), "--newline", "--progress", "--no-simulate",
        "--progress-template", f"download:{PROGRESS}%(info.playlist_index|)s\t"
                               f"%(info.n_entries|)s\t%(progress)j",
        "--progress-template", f"postprocess:{POSTPROC}%(progress.postprocessor)s",
        "--print", f"after_move:{FINAL}%(filepath)s",
        "-P", job.dir, "-o", template,
        "--continue", "--part", "--no-mtime", "--embed-metadata",
        "--merge-output-format", "mp4/mkv",
        "-N", "8", "--http-chunk-size", "10M",
        "--retries", "15", "--fragment-retries", "30", "--extractor-retries", "5",
        "--retry-sleep", "exp=1:30", "--retry-sleep", "fragment:exp=1:30",
        "--yes-playlist" if playlist else "--no-playlist",
        *format_args(o.get("quality", "best"), o.get("mp4", False)),
        *_common_args(job.headers, cookies_browser),
    ]
    ff = tools.ffmpeg()
    if ff:
        cmd += ["--ffmpeg-location", ff]
    if rate_kib:
        cmd += ["--limit-rate", f"{rate_kib}K"]
    if not playlist and not o.get("no_info"):
        info = cached_info(job.url, wait=60)
        if info:
            o["used_info"] = True
            return cmd + ["--load-info-json", info]
    o["used_info"] = False
    return cmd + ["--", job.url]


def parse_line(line):
    """-> ('progress', dict) | ('post', name) | ('file', path) | None"""
    if line.startswith(PROGRESS):
        idx, n, data = (line[len(PROGRESS):].split("\t", 2) + ["", ""])[:3]
        try:
            p = json.loads(data)
        except ValueError:
            return None
        p["_index"] = int(idx) if idx.isdigit() else 0
        p["_count"] = int(n) if n.isdigit() else 0
        return "progress", p
    if line.startswith(POSTPROC):
        return "post", line[len(POSTPROC):].strip()
    if line.startswith(FINAL):
        return "file", line[len(FINAL):].strip()
    return None


def error_text(stderr_lines):
    return _error_text("\n".join(stderr_lines))


def env():
    e = dict(os.environ)
    e["PYTHONUNBUFFERED"] = "1"
    return e
