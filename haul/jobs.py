"""Jobs, their SQLite store, and the manager that runs them.

The manager runs on its own thread. The UI never blocks on it: actions are
queued as commands, and the UI reads `snapshot()` (plain dicts) on a timer.

Reliability rules:
* HTTP data is written to `<name>.haulpart`; it is renamed only once complete,
  so a half file never looks finished.
* A part file whose aria2 control file is missing cannot be trusted (sparse
  allocation makes it look full-size), so it is discarded and restarted.
* Transient failures retry with exponential backoff; the retry budget resets
  whenever a job makes progress. Servers that refuse extra connections get
  fewer connections instead of a failure.
* If aria2 dies it is restarted and its jobs are re-added under the same GID.
"""

import base64
import os
import queue
import secrets
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import json
from collections import deque
from dataclasses import asdict, dataclass, field

from . import media, paths, tools
from .aria2 import Aria2, Aria2Error
from .probe import DEFAULT_UA, sanitize

PART = ".haulpart"
NETWORK_CODES = {"2", "6", "19"}            # timeout, network problem, DNS
TRANSIENT_CODES = {"1", "5", "8", "22", "23", "29"}
FATAL_MESSAGES = {
    "3": "The server says this file does not exist.",
    "4": "The server says this file does not exist.",
    "9": "Not enough free disk space.",
    "12": "This file is already being downloaded.",
    "13": "A file with this name already exists.",
    "15": "Could not open the file for writing.",
    "16": "Could not write the file.",
    "17": "Could not write the file.",
    "18": "Could not create the download folder.",
    "24": "The server wants a login.",
    "26": "The torrent is not valid.",
}
NETWORK_RETRIES = 40                        # ~35 minutes of backoff: rides out a dropped Wi-Fi
OTHER_RETRIES = 6
POST_NAMES = {"Merger": "Merging video and audio", "FFmpegMerger": "Merging video and audio",
              "ExtractAudio": "Converting to audio", "FFmpegExtractAudio": "Converting to audio",
              "FFmpegMetadata": "Writing details", "Metadata": "Writing details",
              "MoveFiles": "Finishing", "FFmpegFixupM3u8": "Fixing the stream",
              "FFmpegVideoRemuxer": "Remuxing", "FFmpegVideoConvertor": "Converting"}
CATEGORIES = {
    "Video": "mp4 mkv avi mov wmv flv webm m4v mpg mpeg 3gp ts",
    "Music": "mp3 m4a aac flac wav ogg opus wma",
    "Compressed": "zip rar 7z tar gz tgz bz2 xz zst",
    "Programs": "deb rpm appimage exe msi apk run bin sh flatpak snap iso img",
    "Documents": "pdf epub mobi djvu doc docx xls xlsx ppt pptx odt ods odp txt csv",
    "Images": "jpg jpeg png gif webp svg bmp tiff heic",
}
EXT_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts.split()}


@dataclass
class Job:
    id: str
    kind: str                     # http | torrent | media
    url: str
    name: str
    dir: str
    status: str = "queued"        # queued | active | paused | done | error
    size: int = 0
    done: int = 0
    error: str = ""
    headers: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    gid: str = ""
    path: str = ""
    retries: int = 0
    added: float = 0.0
    finished: float = 0.0
    # Live values, not persisted:
    speed: int = 0
    eta: int = -1
    detail: str = ""
    retry_at: float = 0.0
    seeding: bool = False

    LIVE = ("speed", "eta", "detail", "retry_at", "seeding")

    def stored(self):
        d = asdict(self)
        for k in self.LIVE:
            d.pop(k, None)
        return d


class Store:
    def __init__(self, path=paths.DB):
        paths.ensure()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        os.chmod(path, 0o600)  # holds cookies forwarded by the browser
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, added REAL, data TEXT)")
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            rows = self.db.execute("SELECT data FROM jobs ORDER BY added").fetchall()
        jobs = []
        for (data,) in rows:
            try:
                d = json.loads(data)
                jobs.append(Job(**{k: v for k, v in d.items() if k in Job.__dataclass_fields__}))
            except (ValueError, TypeError):
                continue
        return jobs

    def save(self, job):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?)",
                            (job.id, job.added, json.dumps(job.stored())))

    def delete(self, job_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def _exists(path):
    return os.path.lexists(path)


class MediaRun(threading.Thread):
    def __init__(self, manager, job, build):
        super().__init__(daemon=True, name="yt-dlp " + job.id)
        self.manager, self.job, self.build = manager, job, build
        self.proc = None
        self.stopping = False
        self.tail = deque(maxlen=40)

    def run(self):
        try:
            # Built here, not on the manager thread: it may wait for page info being read.
            self.cmd = self.build()
            if self.stopping:
                return
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, bufsize=1,
                                         errors="replace", start_new_session=True, env=media.env())
        except OSError as e:
            self.manager._media_exit(self.job, -1, [str(e)], False, self)
            return
        err = threading.Thread(target=self._read_err, daemon=True)
        err.start()
        for line in self.proc.stdout:
            if not self.stopping:
                self.manager._media_line(self.job, line.rstrip("\n"))
        rc = self.proc.wait()
        err.join(5)
        self.manager._media_exit(self.job, rc, list(self.tail), self.stopping, self)

    def _read_err(self):
        # yt-dlp prints post-processing progress (merging, converting) on stderr.
        for line in self.proc.stderr:
            line = line.rstrip()
            if media.parse_line(line):
                if not self.stopping:
                    self.manager._media_line(self.job, line)
            else:
                self.tail.append(line)

    def stop(self):
        """SIGINT lets yt-dlp and ffmpeg finish cleanly and keep their .part files."""
        self.stopping = True
        if not self.proc or self.proc.poll() is not None:
            return
        for sig, wait in ((signal.SIGINT, 8), (signal.SIGTERM, 4), (signal.SIGKILL, 0)):
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                self.proc.wait(wait)
                return
            except subprocess.TimeoutExpired:
                continue


class Manager:
    def __init__(self, settings, store=None):
        self.settings = settings
        self.store = store or Store()
        self.aria = Aria2()
        self.aria_error = ""
        self.jobs = {}
        self.lock = threading.RLock()
        self.cmds = queue.Queue()
        self.events = queue.Queue()     # ("done" | "error", job dict) for notifications
        self.runs = {}
        self._stop = threading.Event()
        self._thread = None
        self._last_persist = 0.0

    # ---------- lifecycle ----------

    def start(self):
        for job in self.store.load():
            if job.status == "active":
                job.status = "queued"
            job.gid = ""
            self.jobs[job.id] = job
        self._start_aria()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="haul-manager")
        self._thread.start()

    def _start_aria(self):
        if not self.aria.available():
            self.aria_error = "aria2 is not installed. Run: sudo apt install aria2"
            return
        try:
            self.aria.start(self.settings["seed_torrents"], self.settings["speed_limit_kib"])
            self.aria_error = ""
        except Exception as e:
            self.aria_error = str(e)

    def shutdown(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5)
        for run in list(self.runs.values()):
            run.stop()
        with self.lock:
            for job in self.jobs.values():
                self.store.save(job)
        self.aria.stop()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # never let one bad poll kill the manager
                _log(f"tick failed: {e!r}")
            self._stop.wait(0.5)

    def _tick(self):
        while True:
            try:
                fn, args = self.cmds.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as e:
                _log(f"command {fn.__name__} failed: {e!r}")
        if self.aria.proc is not None and not self.aria.alive():
            _log("aria2 stopped unexpectedly; restarting")
            with self.lock:
                for job in self.jobs.values():
                    if job.gid and job.status == "active":
                        job.status, job.gid = "queued", ""
                    elif job.gid:
                        job.gid, job.seeding = "", False
            self._start_aria()
        self._poll_aria()
        self._schedule()
        now = time.monotonic()
        if now - self._last_persist > 3:
            self._last_persist = now
            with self.lock:
                for job in self.jobs.values():
                    if job.status == "active":
                        self.store.save(job)

    # ---------- public API (thread-safe, non-blocking) ----------

    def snapshot(self):
        with self.lock:
            return [asdict(j) for j in sorted(self.jobs.values(), key=lambda j: -j.added)]

    def get(self, job_id):
        with self.lock:
            j = self.jobs.get(job_id)
            return asdict(j) if j else None

    def add(self, url, kind, name, directory=None, headers=None, options=None,
            size=0, start=True):
        """Add a job; returns its id. A link already waiting or downloading is not added twice."""
        with self.lock:
            for j in self.jobs.values():
                if j.url == url and j.status in ("queued", "active", "paused"):
                    return j.id
            directory = directory or self.settings.download_dir()
            name = sanitize(name or "download")
            if self.settings["sort_by_type"] and not directory.endswith(tuple(CATEGORIES)):
                ext = os.path.splitext(name)[1].lstrip(".").lower()
                cat = "Video" if kind == "media" and not ext else EXT_CATEGORY.get(ext)
                if kind == "media" and (options or {}).get("quality", "").startswith("audio"):
                    cat = "Music"
                if cat:
                    directory = os.path.join(directory, cat)
            if kind == "http":
                name = self._unique(directory, name)
            options = dict(options or {})
            if kind == "media":
                options.setdefault("mp4", self.settings["video_format"] == "mp4")
            job = Job(id=secrets.token_hex(8), kind=kind, url=url, name=name, dir=directory,
                      status="queued" if start else "paused", size=size or 0,
                      headers=dict(headers or {}), options=dict(options or {}), added=time.time())
            self.jobs[job.id] = job
            self.store.save(job)
            return job.id

    def pause(self, job_id):
        self.cmds.put((self._pause, (job_id,)))

    def resume(self, job_id):
        self.cmds.put((self._resume, (job_id,)))

    def retry(self, job_id):
        self.cmds.put((self._resume, (job_id, True)))

    def remove(self, job_id, delete_files=False):
        self.cmds.put((self._remove, (job_id, delete_files)))

    def pause_all(self):
        with self.lock:
            ids = [j.id for j in self.jobs.values() if j.status in ("queued", "active")]
        for i in ids:
            self.pause(i)

    def resume_all(self):
        with self.lock:
            ids = [j.id for j in self.jobs.values() if j.status in ("paused", "error")]
        for i in ids:
            self.resume(i)

    def clear_finished(self):
        with self.lock:
            ids = [j.id for j in self.jobs.values() if j.status == "done" and not j.seeding]
        for i in ids:
            self.remove(i)

    def apply_settings(self):
        self.cmds.put((self._apply_settings, ()))

    def counts(self):
        with self.lock:
            active = [j for j in self.jobs.values() if j.status == "active"]
            return len(active), sum(j.speed for j in active)

    # ---------- commands (manager thread) ----------

    def _apply_settings(self):
        if self.aria.alive():
            self.aria.call("aria2.changeGlobalOption", {
                "max-overall-download-limit": f"{self.settings['speed_limit_kib']}K",
                **({"seed-ratio": "1.0", "seed-time": "1440"} if self.settings["seed_torrents"]
                   else {"seed-time": "0"}),
            })

    def _pause(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status not in ("queued", "active"):
                return
            was_active = job.status == "active"
            job.status, job.speed, job.eta, job.detail, job.retry_at = "paused", 0, -1, "", 0
            self.store.save(job)
            gid, run = job.gid, self.runs.get(job_id)
        if was_active and gid:
            self._aria_drop(gid)
            with self.lock:
                job.gid = ""
        if run:
            threading.Thread(target=run.stop, daemon=True).start()

    def _resume(self, job_id, reset=False):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.status not in ("paused", "error"):
                return
            if job.status == "error" or reset:
                job.retries = 0
            job.status, job.error, job.retry_at = "queued", "", 0
            self.store.save(job)

    def _remove(self, job_id, delete_files):
        with self.lock:
            job = self.jobs.pop(job_id, None)
        if not job:
            return
        if job.gid:
            self._aria_drop(job.gid)
        run = self.runs.pop(job_id, None)
        if run:
            run.stop()
        self.store.delete(job_id)
        if delete_files:
            self._delete_files(job)

    def _delete_files(self, job):
        base = os.path.join(job.dir, job.name)
        targets = [job.path] if job.path else []
        if job.kind == "http":
            targets += [base + PART, base + PART + ".aria2"]
        elif job.kind == "torrent" and job.name:
            targets += [base, base + ".aria2"]
        for t in targets:
            # never delete the download folder itself or anything outside it
            if not t or os.path.realpath(t) == os.path.realpath(job.dir):
                continue
            if not os.path.realpath(t).startswith(os.path.realpath(job.dir) + os.sep):
                continue
            try:
                if os.path.isdir(t) and not os.path.islink(t):
                    shutil.rmtree(t)
                elif _exists(t):
                    os.remove(t)
            except OSError:
                pass

    # ---------- scheduling ----------

    def _schedule(self):
        now = time.time()
        with self.lock:
            active = sum(1 for j in self.jobs.values() if j.status == "active")
            slots = max(1, int(self.settings["max_active"])) - active
            waiting = sorted((j for j in self.jobs.values()
                              if j.status == "queued" and j.retry_at <= now), key=lambda j: j.added)
            for j in self.jobs.values():
                if j.status == "queued" and j.retry_at > now:
                    j.detail = f"Retrying in {int(j.retry_at - now) + 1} s"
        for job in waiting:
            if slots <= 0:
                break
            if job.kind in ("http", "torrent") and not self.aria.alive():
                with self.lock:
                    self._fail(job, self.aria_error or "aria2 is not running.")
                continue
            if job.kind == "media" and not media.available():
                with self.lock:
                    self._fail(job, "yt-dlp is not installed. Install it in Settings → Video.")
                continue
            try:
                os.makedirs(job.dir, exist_ok=True)
            except OSError as e:
                with self.lock:
                    self._fail(job, f"Could not create the folder {job.dir}: {e.strerror}")
                continue
            if job.size and job.size > job.done:
                free = shutil.disk_usage(job.dir).free
                if free < job.size - job.done:
                    with self.lock:
                        self._fail(job, f"Not enough free disk space ({_mib(job.size - job.done)} needed, "
                                        f"{_mib(free)} free).")
                    continue
            try:
                if job.kind == "media":
                    self._start_media(job)
                else:
                    self._start_aria_job(job)
                slots -= 1
            except Aria2Error as e:
                with self.lock:
                    self._retry_or_fail(job, "1", f"aria2 refused the download: {e}")
            except Exception as e:
                with self.lock:
                    self._fail(job, f"Could not start: {e}")

    # ---------- aria2 jobs ----------

    def _aria_options(self, job):
        h = {k.lower(): v for k, v in job.headers.items() if v}
        opts = {"gid": job.id, "dir": job.dir, "user-agent": h.pop("user-agent", DEFAULT_UA)}
        if "referer" in h:
            opts["referer"] = h.pop("referer")
        if h:
            opts["header"] = [f"{k}: {v}" for k, v in h.items()]
        if job.kind == "http":
            conns = int(job.options.get("connections") or self.settings["connections"])
            if job.options.get("resumable") is False:
                conns = 1
            conns = max(1, min(conns, 16))
            opts.update({"out": job.name + PART, "split": str(conns),
                         "max-connection-per-server": str(conns),
                         "min-split-size": "1M", "piece-length": "1M"})
        return opts

    def _start_aria_job(self, job):
        if job.kind == "http":
            part = os.path.join(job.dir, job.name + PART)
            if _exists(part) and not _exists(part + ".aria2"):
                os.remove(part)  # cannot tell which bytes are real: start over
                job.done = 0
        opts = self._aria_options(job)
        try:
            self.aria.call("aria2.removeDownloadResult", job.id)
        except Aria2Error:
            pass
        if job.kind == "torrent" and job.options.get("torrent_b64"):
            self.aria.call("aria2.addTorrent", job.options["torrent_b64"], [], opts)
        else:
            self.aria.call("aria2.addUri", [job.url], opts)
        with self.lock:
            job.gid, job.status, job.error, job.detail = job.id, "active", "", ""
            job.options["fail_done"] = job.options.get("fail_done", job.done)
            self.store.save(job)

    def _aria_drop(self, gid):
        for method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            try:
                self.aria.call(method, gid)
            except (Aria2Error, OSError):
                pass
        # forceRemove is asynchronous; wait for it so the control file is flushed.
        for _ in range(20):
            try:
                st = self.aria.call("aria2.tellStatus", gid, ["status"])
                if st.get("status") in ("removed", "complete", "error"):
                    self.aria.call("aria2.removeDownloadResult", gid)
                    return
            except (Aria2Error, OSError):
                return
            time.sleep(0.1)

    def _poll_aria(self):
        if not self.aria.alive():
            return
        with self.lock:
            polled = [j for j in self.jobs.values() if j.gid and (j.status == "active" or j.seeding)]
        if not polled:
            return
        keys = ["status", "totalLength", "completedLength", "downloadSpeed", "connections",
                "errorCode", "errorMessage", "followedBy", "bittorrent", "numSeeders", "files"]
        try:
            results = self.aria.multicall([("aria2.tellStatus", j.gid, keys) for j in polled])
        except (Aria2Error, OSError) as e:
            _log(f"aria2 poll failed: {e!r}")
            return
        for job, st in zip(polled, results):
            self._apply_status(job, st)

    def _apply_status(self, job, st):
        if isinstance(st, Aria2Error):  # aria2 lost it (restarted): add it again
            with self.lock:
                if job.seeding:
                    job.seeding, job.gid, job.detail = False, "", ""
                elif job.status == "active":
                    job.status, job.gid = "queued", ""
            return
        status = st.get("status")
        total, done = int(st.get("totalLength", 0)), int(st.get("completedLength", 0))
        speed = int(st.get("downloadSpeed", 0))
        bt = st.get("bittorrent") or {}
        if status == "complete" and st.get("followedBy"):
            # .torrent/magnet metadata finished; the real download continues under a new GID
            try:
                self.aria.call("aria2.removeDownloadResult", job.gid)
            except Aria2Error:
                pass
            with self.lock:
                job.gid = st["followedBy"][0]
            return
        with self.lock:
            if job.kind == "torrent":
                name = (bt.get("info") or {}).get("name")
                if name and job.status != "done":
                    job.name = sanitize(name)
                if not bt.get("info") and status == "active":
                    job.detail = "Getting torrent info from peers…"
                elif status == "active":
                    job.detail = f"{st.get('numSeeders', '0')} seeds, {st.get('connections', '0')} peers"
            elif status == "active":
                c = int(st.get("connections", 0))
                job.detail = f"{c} connection{'s' if c != 1 else ''}" if c else "Connecting…"
            if total:
                job.size = total
            if job.status == "active" or not job.seeding:
                job.done = done
            job.speed = speed
            job.eta = int((total - done) / speed) if speed and total > done else -1
            if done > job.options.get("fail_done", 0):
                job.retries = 0
            if job.kind == "torrent" and status == "active" and total and done >= total:
                if not job.seeding:  # finished downloading, still seeding
                    self._finish(job, os.path.join(job.dir, job.name))
                    job.seeding, job.detail = True, "Seeding"
                return
        if status == "complete":
            try:
                self.aria.call("aria2.removeDownloadResult", job.gid)
            except Aria2Error:
                pass
            with self.lock:
                if job.seeding:
                    job.seeding, job.gid, job.detail, job.speed = False, "", "", 0
                    return
                if job.kind == "http":
                    self._finish_http(job)
                else:
                    self._finish(job, os.path.join(job.dir, job.name))
        elif status == "error":
            try:
                self.aria.call("aria2.removeDownloadResult", job.gid)
            except Aria2Error:
                pass
            with self.lock:
                job.gid = ""
                self._retry_or_fail(job, st.get("errorCode", "1"), st.get("errorMessage", ""))

    def _finish_http(self, job):
        part = os.path.join(job.dir, job.name + PART)
        final = os.path.join(job.dir, job.name)
        if _exists(final):  # something appeared at that name while downloading
            final = os.path.join(job.dir, self._unique(job.dir, job.name, ignore=job.id))
        try:
            os.replace(part, final)
        except OSError as e:
            self._fail(job, f"Could not rename the finished file: {e.strerror}")
            return
        job.name = os.path.basename(final)
        self._finish(job, final)

    # ---------- media jobs ----------

    def _start_media(self, job):
        cookies = "" if job.options.get("no_cookies") else self.settings["cookies_browser"]
        limit = int(self.settings["speed_limit_kib"])
        if limit:
            limit = max(16, limit // max(1, self.counts()[0] + 1))
        run = MediaRun(self, job, lambda: media.command(job, cookies, limit))
        with self.lock:
            job.status, job.error, job.detail, job.gid = "active", "", "Starting…" if media.cached_info(job.url) else "Reading video info…", ""
            job.options["fail_done"] = job.done
            job.options["used_cookies"] = bool(cookies)
            self.runs[job.id] = run
            self.store.save(job)
        run.start()

    def _media_line(self, job, line):
        parsed = media.parse_line(line)
        if not parsed:
            return
        what, value = parsed
        with self.lock:
            if job.status != "active":
                return
            if what == "progress":
                p = value
                job.done = int(p.get("downloaded_bytes") or 0)
                job.size = int(p.get("total_bytes") or p.get("total_bytes_estimate") or 0)
                job.speed = int(p.get("speed") or 0)
                job.eta = int(p["eta"]) if isinstance(p.get("eta"), (int, float)) else -1
                parts = []
                if p["_count"] > 1:
                    parts.append(f"Item {p['_index']} of {p['_count']}")
                if p.get("fragment_count"):
                    parts.append(f"part {p.get('fragment_index', 0)}/{p['fragment_count']}")
                job.detail = " · ".join(parts) or "Downloading"
                if job.done > job.options.get("fail_done", 0):
                    job.retries = 0
            elif what == "post":
                job.detail = POST_NAMES.get(value, "Processing")
                job.speed, job.eta = 0, -1
            elif what == "file":
                job.path = value
                job.options["files"] = job.options.get("files", 0) + 1

    def _media_exit(self, job, rc, tail, stopped, run=None):
        with self.lock:
            if run is not None and self.runs.get(job.id) is not run:
                return  # an old run (paused, then resumed quickly) finishing late
            self.runs.pop(job.id, None)
            if job.id not in self.jobs or job.status != "active":
                return  # paused or removed while running
            if rc == 0:
                if job.path:
                    job.name = os.path.basename(job.path) if not job.options.get("playlist") else job.name
                    try:
                        job.size = job.done = os.path.getsize(job.path) if os.path.isfile(job.path) else job.done
                    except OSError:
                        pass
                self._finish(job, job.path or job.dir)
                return
            message = media.error_text(tail)
            if job.options.get("used_info") and not job.options.get("no_info"):
                job.options["no_info"] = True  # saved page info went stale: read the page again
                job.status, job.detail = "queued", "Reading the page again"
                return
            if job.options.get("used_cookies") and not job.options.get("no_cookies"):
                job.options["no_cookies"] = True  # retry once without browser cookies
                job.status, job.detail = "queued", "Retrying without browser cookies"
                return
            low = message.lower()
            transient = any(w in low for w in ("timed out", "timeout", "connection", "temporary failure",
                                                "http error 5", "incompleteread", "reset by peer",
                                                "unable to download", "network", "fragment"))
            if transient:
                self._retry_or_fail(job, "6", message)
            else:
                self._fail(job, message)

    # ---------- outcomes (call with lock held) ----------

    def _finish(self, job, path):
        job.status, job.path, job.finished = "done", path, time.time()
        job.speed, job.eta, job.detail, job.error, job.retries = 0, -1, "", "", 0
        if job.size == 0:
            job.size = job.done
        self.store.save(job)
        self.events.put(("done", asdict(job)))

    def _fail(self, job, message):
        job.status, job.error, job.speed, job.eta, job.detail, job.gid = "error", message, 0, -1, "", ""
        self.store.save(job)
        self.events.put(("error", asdict(job)))

    def _retry_or_fail(self, job, code, message):
        code = str(code)
        low = message.lower()
        conns = int(job.options.get("connections") or self.settings["connections"])
        refused = any(s in low for s in ("403", "429", "503", "too many"))
        if job.kind == "http" and refused and conns > 1 and job.done > 0:
            # The server limits connections: use fewer instead of failing.
            job.options["connections"] = max(1, conns // 2)
            job.status, job.retry_at = "queued", time.time() + 2
            job.detail = f"Server limits connections; using {job.options['connections']}"
            self.store.save(job)
            return
        if code in FATAL_MESSAGES:
            self._fail(job, FATAL_MESSAGES[code] + (f" ({message})" if message else ""))
            return
        if "403" in low or "401" in low:
            self._fail(job, "The server refused the download (the link may have expired). "
                            "Start it again from the web page.")
            return
        budget = NETWORK_RETRIES if code in NETWORK_CODES else OTHER_RETRIES
        if code in NETWORK_CODES or code in TRANSIENT_CODES:
            if job.retries < budget:
                job.retries += 1
                job.options["fail_done"] = job.done
                delay = min(60, 2 ** min(job.retries, 6))
                job.status, job.retry_at, job.speed, job.eta = "queued", time.time() + delay, 0, -1
                job.error = message
                self.store.save(job)
                return
        self._fail(job, message or f"Download failed (aria2 error {code}).")

    # ---------- helpers ----------

    def _unique(self, directory, name, ignore=None):
        taken = {os.path.join(j.dir, j.name) for j in self.jobs.values()
                 if j.id != ignore and j.status != "done"}
        stem, ext = os.path.splitext(name)
        if stem.endswith(".tar"):
            stem, ext = stem[:-4], ".tar" + ext
        n = 1
        candidate = name
        while True:
            full = os.path.join(directory, candidate)
            if full not in taken and not _exists(full) and not _exists(full + PART):
                return candidate
            n += 1
            candidate = f"{stem} ({n}){ext}"


def torrent_options(path):
    with open(path, "rb") as f:
        return {"torrent_b64": base64.b64encode(f.read()).decode()}


def _mib(n):
    return f"{n / (1 << 20):.0f} MiB"


def _log(msg):
    try:
        with open(paths.LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass
