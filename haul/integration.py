"""Browser integration: build the extension and register the native messaging host.

Chromium-family browsers only accept the host for listed extension IDs, so the
unpacked extension carries a fixed public key (generated once) to keep its ID
stable. Firefox uses the gecko id instead.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import HOST_NAME, __version__, paths

SRC = paths.PROJECT / "extension"
OUT = paths.DATA / "extension"
KEY = paths.DATA / "extension-key.pem"
GECKO_ID = "haul@haul.local"
HOST_SCRIPT = paths.DATA / "haul-native-host"

HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")

# (display name, profile dir that shows the browser is installed, NativeMessagingHosts dir)
CHROMIUM = [
    ("Google Chrome", CONFIG / "google-chrome", CONFIG / "google-chrome/NativeMessagingHosts"),
    ("Chrome Beta", CONFIG / "google-chrome-beta", CONFIG / "google-chrome-beta/NativeMessagingHosts"),
    ("Chrome Dev", CONFIG / "google-chrome-unstable", CONFIG / "google-chrome-unstable/NativeMessagingHosts"),
    ("Chromium", CONFIG / "chromium", CONFIG / "chromium/NativeMessagingHosts"),
    ("Chromium (snap)", HOME / "snap/chromium", HOME / "snap/chromium/common/chromium/NativeMessagingHosts"),
    ("Brave", CONFIG / "BraveSoftware/Brave-Browser", CONFIG / "BraveSoftware/Brave-Browser/NativeMessagingHosts"),
    ("Brave Origin", CONFIG / "BraveSoftware/Brave-Origin", CONFIG / "BraveSoftware/Brave-Origin/NativeMessagingHosts"),
    ("Microsoft Edge", CONFIG / "microsoft-edge", CONFIG / "microsoft-edge/NativeMessagingHosts"),
    ("Edge Beta", CONFIG / "microsoft-edge-beta", CONFIG / "microsoft-edge-beta/NativeMessagingHosts"),
    ("Edge Dev", CONFIG / "microsoft-edge-dev", CONFIG / "microsoft-edge-dev/NativeMessagingHosts"),
    ("Vivaldi", CONFIG / "vivaldi", CONFIG / "vivaldi/NativeMessagingHosts"),
    ("Opera", CONFIG / "opera", CONFIG / "opera/NativeMessagingHosts"),
    ("Helium", CONFIG / "net.imput.helium", CONFIG / "net.imput.helium/NativeMessagingHosts"),
]
# Firefox (deb or snap) and Zen read the same legacy directory.
FIREFOX = [
    ("Firefox / Zen", HOME / ".mozilla", HOME / ".mozilla/native-messaging-hosts"),
]


def _public_key_der():
    if not KEY.exists():
        paths.ensure()
        subprocess.run(["openssl", "genrsa", "-out", str(KEY), "2048"], check=True,
                       capture_output=True)
        os.chmod(KEY, 0o600)
    return subprocess.run(["openssl", "rsa", "-in", str(KEY), "-pubout", "-outform", "DER"],
                          check=True, capture_output=True).stdout


def extension_id(der):
    digest = hashlib.sha256(der).hexdigest()[:32]
    return digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))


def _source_stamp():
    h = hashlib.sha256(__version__.encode())
    for p in sorted(SRC.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(SRC)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def build_extension():
    """Writes OUT/chrome and OUT/firefox when the source changed. Returns the Chrome extension id."""
    der = _public_key_der()
    ext_id = extension_id(der)
    stamp = _source_stamp()
    stamp_file = OUT / ".stamp"
    if stamp_file.exists() and stamp_file.read_text() == stamp and (OUT / "chrome/manifest.json").exists():
        return ext_id
    with open(SRC / "manifest.json") as f:
        base = json.load(f)
    base["version"] = __version__
    for target in ("chrome", "firefox"):
        dest = OUT / target
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("manifest.json", "*.md"))
        m = json.loads(json.dumps(base))
        if target == "chrome":
            m["key"] = base64.b64encode(der).decode()
            m["background"] = {"service_worker": "background.js"}
        else:
            m["background"] = {"scripts": ["background.js"]}
            m["browser_specific_settings"] = {"gecko": {"id": GECKO_ID, "strict_min_version": "115.0"}}
        with open(dest / "manifest.json", "w") as f:
            json.dump(m, f, indent=2)
    stamp_file.write_text(stamp)
    return ext_id


def write_host_script():
    paths.ensure()
    text = "#!/bin/sh\n" f'exec /usr/bin/python3 -s "{paths.PROJECT / "haul" / "nmhost.py"}" "$@"\n'
    if not HOST_SCRIPT.exists() or HOST_SCRIPT.read_text() != text:
        HOST_SCRIPT.write_text(text)
    os.chmod(HOST_SCRIPT, 0o755)


AUTOSTART = CONFIG / "autostart" / "dev.haul.Haul.desktop"


def set_autostart(enabled):
    if not enabled:
        AUTOSTART.unlink(missing_ok=True)
        return
    launcher = HOME / ".local/bin/haul"
    exe = str(launcher) if launcher.exists() else str(paths.PROJECT / "bin" / "haul")
    text = ("[Desktop Entry]\nType=Application\nName=Haul\nComment=Download manager (background)\n"
            f"Exec={exe} --background\nIcon=dev.haul.Haul\nTerminal=false\nNoDisplay=true\n"
            "X-GNOME-Autostart-enabled=true\n")
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    if not AUTOSTART.exists() or AUTOSTART.read_text() != text:
        AUTOSTART.write_text(text)


def register():
    """Register the host with every installed browser. Returns [(browser, path)]."""
    ext_id = build_extension()
    write_host_script()
    done = []
    for name, profile, nmh in CHROMIUM + FIREFOX:
        if not profile.exists():
            continue
        manifest = {"name": HOST_NAME, "description": "Haul download manager",
                    "path": str(HOST_SCRIPT), "type": "stdio"}
        if (name, profile, nmh) in FIREFOX:
            manifest["allowed_extensions"] = [GECKO_ID]
        else:
            manifest["allowed_origins"] = [f"chrome-extension://{ext_id}/"]
        nmh.mkdir(parents=True, exist_ok=True)
        target = nmh / f"{HOST_NAME}.json"
        text = json.dumps(manifest, indent=2)
        if not target.exists() or target.read_text() != text:
            target.write_text(text)
        done.append((name, str(target)))
    return ext_id, done


def status():
    out = []
    for name, profile, nmh in CHROMIUM + FIREFOX:
        if profile.exists():
            out.append((name, (nmh / f"{HOST_NAME}.json").exists()))
    return out


def unregister():
    for _, _, nmh in CHROMIUM + FIREFOX:
        (nmh / f"{HOST_NAME}.json").unlink(missing_ok=True)
