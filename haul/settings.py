"""Typed settings stored as JSON, written atomically."""

import json
import os
import threading

from . import paths

DEFAULTS = {
    "download_dir": "",            # empty = the XDG Downloads folder
    "sort_by_type": False,         # Video/, Music/, Compressed/ ... subfolders
    "max_active": 3,               # downloads running at the same time
    "connections": 16,             # connections per file (aria2 caps at 16 per server)
    "speed_limit_kib": 0,          # 0 = unlimited
    "ask_before_browser_download": True,
    "ask_where_to_save": True,     # IDM-style save dialog for in-page button downloads
    "progress_window": True,       # IDM-style popup per browser download
    "recent_dirs": [],
    "type_dirs": {},               # learned: file type -> folder last saved to
    "type_auto": {},               # file type -> save there without asking
    "video_quality": "best",
    "video_format": "mp4",         # mp4 (plays everywhere) | best (WebM/MKV allowed)
    "video_button": True,          # in-page download button on videos
    "cookies_browser": "",         # yt-dlp --cookies-from-browser (firefox, chrome, ...)
    "ytdlp_auto_update": True,
    "seed_torrents": False,
    "notify": True,
    "run_in_background": True,
    "autostart": True,             # start hidden at login
}


class Settings:
    def __init__(self, path=paths.SETTINGS):
        self._path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        try:
            with open(path) as f:
                stored = json.load(f)
            self._data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        with self._lock:
            return self._data[key]

    def get(self, key):
        return self[key]

    def set(self, key, value):
        with self._lock:
            if self._data.get(key) == value:
                return
            self._data[key] = value
            data = dict(self._data)
        self._save(data)

    def download_dir(self):
        return self["download_dir"] or paths.download_dir()

    def _save(self, data):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
