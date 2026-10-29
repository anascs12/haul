"""Runs the real app against the test server and saves screenshots of its windows.

Run: python3 tests/shot.py OUTDIR [dark]
"""

import os
import sys
import tempfile

OUT = sys.argv[1]
TMP = tempfile.mkdtemp(prefix="haul-shot-")
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "data")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "config")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from haul import paths  # noqa: E402

paths.ensure()
os.symlink(os.path.expanduser("~/.local/share/haul/vendor"), paths.DATA / "vendor")
os.symlink(os.path.expanduser("~/.local/share/icons"), paths.DATA.parent / "icons")  # user icon themes

import testserver  # noqa: E402
from haul import ui  # noqa: E402
from gi.repository import Adw, GLib, Gtk  # noqa: E402

srv, BASE = testserver.serve(64 * 1024 * 1024)


def _no_network_installs(*a, **k):
    raise RuntimeError("disabled in screenshot tests")


ui.tools.install_ytdlp = _no_network_installs
ui.HaulApp.install_ytdlp = lambda self, then=None: None
app = ui.HaulApp()
# The test window must never react to keys typed elsewhere on the desktop.
app.connect("startup", lambda *_: app.window.set_can_focus(False))
if os.environ.get("SHOT_ICON_THEME"):
    app.connect("startup", lambda *_: Gtk.Settings.get_default().set_property("gtk-icon-theme-name", os.environ["SHOT_ICON_THEME"]))
app.set_application_id("dev.haul.HaulShot")
if len(sys.argv) > 2 and sys.argv[2] == "dark":
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)


def capture(name):
    w = app.window
    paintable = Gtk.WidgetPaintable.new(w)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w.get_width(), w.get_height())
    node = snap.to_node()
    if node is None:
        print("window not drawn yet; skipped", name)
    else:
        w.get_native().get_renderer().render_texture(node, None).save_to_png(os.path.join(OUT, name))
        print("saved", name)
    for row in list(app.window.rows.values())[:1]:
        p = row.icon.get_paintable()
        print("row icon:", type(p).__name__, row.icon.get_storage_type())
        if hasattr(p, "save_to_png"):
            p.save_to_png(os.path.join(OUT, "rowicon.png"))


def seed():
    m = app.manager
    app.settings.set("download_dir", os.path.join(TMP, "Downloads"))
    m.add(BASE + "/file.bin", "http", "ubuntu-26.04-desktop-amd64.iso", options={"resumable": True})
    m.add(BASE + "/file.bin?rate=60000&a=1", "http", "Big Buck Bunny 4K.mkv", options={"resumable": True})
    p = m.add(BASE + "/file.bin?rate=60000&b=2", "http", "project-archive-2026.tar.gz", options={"resumable": True})
    m.add(BASE + "/missing.zip", "http", "old-link.zip")
    GLib.timeout_add(2500, lambda: m.pause(p) and False)
    GLib.timeout_add(6000, step1)
    return False


def step1():
    capture("main.png")
    app.open_add(BASE + "/cd")
    GLib.timeout_add(2500, step2)
    return False


def step2():
    capture("add.png")
    d = app.window.get_visible_dialog()
    d and d.close()
    ui.SettingsDialog(app).present(app.window)
    GLib.timeout_add(1500, step3)
    return False


def step3():
    capture("settings.png")
    d = app.window.get_visible_dialog()
    d and d.close()
    app.open_add(BASE + "/challenge")
    GLib.timeout_add(2000, step4)
    return False


def step4():
    capture("add-error.png")
    app.quit()
    return False


def on_activate(*_):
    pass


app.connect("startup", lambda *_: GLib.timeout_add(300, seed))
sys.exit(app.run([sys.argv[0]]))
