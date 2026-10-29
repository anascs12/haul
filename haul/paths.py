"""XDG locations used by Haul."""

import os
import subprocess
from pathlib import Path


def _xdg(var, default):
    value = os.environ.get(var, "")
    return Path(value) if value and os.path.isabs(value) else Path.home() / default


DATA = _xdg("XDG_DATA_HOME", ".local/share") / "haul"
CONFIG = _xdg("XDG_CONFIG_HOME", ".config") / "haul"
BIN = DATA / "bin"
VENDOR = DATA / "vendor" / "root"
DB = DATA / "haul.db"
IPC = DATA / "ipc.json"
LOG = DATA / "haul.log"
ARIA2_LOG = DATA / "aria2.log"
DHT = DATA / "dht.dat"
SETTINGS = CONFIG / "settings.json"
PROJECT = Path(__file__).resolve().parent.parent


def ensure():
    for d in (DATA, CONFIG, BIN):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)


def user_dir(name, fallback):
    """XDG user folder (DOWNLOAD, VIDEOS, MUSIC, PICTURES, DOCUMENTS)."""
    try:
        out = subprocess.run(["xdg-user-dir", name], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        if out and out != str(Path.home()):
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path.home() / fallback)


def download_dir():
    return user_dir("DOWNLOAD", "Downloads")
