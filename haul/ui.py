"""GTK 4 / libadwaita user interface."""

import os
import re
import threading
import time
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

from . import APP_ID, __version__, integration, media, paths, probe, segments, tools  # noqa: E402
from .jobs import EXT_CATEGORY, Manager, torrent_options, _log  # noqa: E402
from .server import IPCServer  # noqa: E402
from .settings import Settings  # noqa: E402

URL_RE = re.compile(r"(?:https?|ftps?|sftp)://[^\s<>\"']+|magnet:\?[^\s<>\"']+", re.I)

CSS = b"""
.job-status { font-feature-settings: "tnum"; }
.job-row { padding: 12px; }
.job-row progressbar trough, .job-row progressbar progress { min-height: 6px; border-radius: 3px; }
.filter-bar { padding: 8px 12px 0 12px; }
"""


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def human_eta(s):
    if s is None or s < 0:
        return ""
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def extract_urls(text):
    found = URL_RE.findall(text or "")
    return [u.rstrip(").,;]") if not u.lower().startswith("magnet:") else u for u in found]


def gicon_for(job):
    if job["kind"] == "torrent":
        return Gio.ThemedIcon.new_with_default_fallbacks("application-x-bittorrent")
    if job["kind"] == "media":
        audio = job["options"].get("quality", "").startswith("audio")
        return Gio.content_type_get_icon("audio/mpeg" if audio else "video/mp4")
    ctype, _ = Gio.content_type_guess(job["name"], None)
    return Gio.content_type_get_icon(ctype)


TYPES = ["Video", "Music", "Compressed", "Programs", "Documents", "Images", "Other"]
TYPE_WORDS = {"Video": "videos", "Music": "music and audio", "Compressed": "archives",
              "Programs": "programs and disk images", "Documents": "documents",
              "Images": "images", "Other": "other files"}
TYPE_XDG = {"Video": ("VIDEOS", "Videos"), "Music": ("MUSIC", "Music"), "Images": ("PICTURES", "Pictures"),
            "Documents": ("DOCUMENTS", "Documents")}


def file_type(kind, name, options):
    """Which folder family a download belongs to."""
    if kind == "media":
        return "Music" if (options or {}).get("quality", "").startswith("audio") else "Video"
    if kind == "torrent":
        return "Other"
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    return EXT_CATEGORY.get(ext, "Other")


_icon_cache = {}


def file_icon(widget, gicon, size=32):
    """A paintable for a full-colour theme icon.

    SVGs are rendered with librsvg: GTK 4.22's own icon renderer shows some
    third-party themes (e.g. WhiteSur) as broken images.
    """
    scale = widget.get_scale_factor()
    key = (tuple(gicon.get_names()) if hasattr(gicon, "get_names") else str(gicon), size, scale)
    if key in _icon_cache:
        return _icon_cache[key]
    theme = Gtk.IconTheme.get_for_display(widget.get_display())
    # Look up at scale 1 and double size: some themes' @2x folders miss icons, and
    # GTK then returns "image-missing" instead of scaling the 1x one.
    paintable = theme.lookup_by_gicon(gicon, size * scale, 1, Gtk.TextDirection.NONE, 0)
    f = paintable.get_file()
    path = f.get_path() if f else None
    if path and path.endswith(".svg"):
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size * scale, size * scale)
            fmt = Gdk.MemoryFormat.R8G8B8A8 if pix.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
            paintable = Gdk.MemoryTexture.new(pix.get_width(), pix.get_height(), fmt,
                                              pix.read_pixel_bytes(), pix.get_rowstride())
        except GLib.Error as e:
            _log(f"icon {path}: {e.message}")
    elif os.environ.get("HAUL_DEBUG_ICONS"):
        _log(f"icon lookup {key} -> {path!r} {type(paintable).__name__}")
    _icon_cache[key] = paintable
    return paintable


# ---------------------------------------------------------------- segment map

class SegmentMap(Gtk.Widget):
    """The load filling up: one cell per group of pieces, lit as each connection delivers them.

    aria2 only reports a piece once all of it has arrived, so a thin line under the
    cells shows the exact overall progress and the map never looks behind the numbers.
    """

    GAP = 1
    LINE = 2

    def __init__(self, height, cell):
        super().__init__(hexpand=True, visible=False)
        self.height, self.cell = height, cell
        self.bitfield, self.pieces, self.fraction = "", 0, 0.0

    def set_pieces(self, bitfield, pieces, fraction):
        if (bitfield, pieces, fraction) != (self.bitfield, self.pieces, self.fraction):
            self.bitfield, self.pieces, self.fraction = bitfield, pieces, fraction
            self.queue_draw()

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.VERTICAL:
            return self.height, self.height, -1, -1
        return 40, 40, -1, -1

    def do_snapshot(self, snapshot):
        width = self.get_width()
        lit = segments.cells(self.bitfield, self.pieces, (width + self.GAP) // (self.cell + self.GAP))
        if not lit:
            return
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        empty = self.get_color()
        empty.alpha = 0.12
        step = (width + self.GAP) / len(lit)
        for i, fraction in enumerate(lit):
            color = empty
            if fraction:
                color = accent.copy()
                color.alpha = 0.4 + 0.6 * fraction
            rect = Graphene.Rect().init(round(i * step), 0, round((i + 1) * step) - round(i * step) - self.GAP,
                                        self.height - self.LINE - self.GAP)
            snapshot.append_color(color, rect)
        line_top = self.height - self.LINE
        snapshot.append_color(empty, Graphene.Rect().init(0, line_top, width, self.LINE))
        snapshot.append_color(accent, Graphene.Rect().init(0, line_top, round(width * self.fraction), self.LINE))


def show_progress(bar, cells_map, job, fraction):
    """The segment map when aria2 reports pieces, a plain bar for everything else."""
    active = job["status"] != "done"
    has_map = active and bool(job["bitfield"]) and job["pieces"] > 0
    cells_map.set_visible(has_map)
    if has_map:
        cells_map.set_pieces(job["bitfield"], job["pieces"], fraction)
    bar.set_visible(active and not has_map)
    bar.set_fraction(fraction)


# ---------------------------------------------------------------- rows

class DownloadRow(Gtk.ListBoxRow):
    def __init__(self, win, job):
        super().__init__(activatable=False, selectable=False)
        self.win, self.job_id, self.added = win, job["id"], job["added"]
        self.add_css_class("job-row")
        box = Gtk.Box(spacing=12)
        self.icon = Gtk.Image(pixel_size=32, valign=Gtk.Align.CENTER)
        box.append(self.icon)

        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True,
                      valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.title.add_css_class("heading")
        self.bar = Gtk.ProgressBar()
        self.map = SegmentMap(8, 4)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.add_css_class("caption")
        self.status.add_css_class("dim-label")
        self.status.add_css_class("job-status")
        for w in (self.title, self.bar, self.map, self.status):
            mid.append(w)
        box.append(mid)

        buttons = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.main_btn = self._button("media-playback-pause-symbolic", self._on_main)
        self.folder_btn = self._button("haul-folder-symbolic", self._on_folder, "Show in folder")
        self.remove_btn = self._button("window-close-symbolic", self._on_remove, "Remove from list")
        for b in (self.main_btn, self.folder_btn, self.remove_btn):
            buttons.append(b)
        box.append(buttons)
        self.set_child(box)

        actions = Gio.SimpleActionGroup()
        for name, cb in (("copy", self._copy_link), ("delete", self._delete_file),
                         ("open", self._open_file), ("retry", lambda *a: self.win.app.manager.retry(self.job_id))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            actions.add_action(a)
        self.insert_action_group("row", actions)
        menu = Gio.Menu()
        menu.append("Open", "row.open")
        menu.append("Copy link", "row.copy")
        menu.append("Restart download", "row.retry")
        menu.append("Remove and delete file", "row.delete")
        self.popover = Gtk.PopoverMenu.new_from_model(menu)
        self.popover.set_parent(self)
        self.popover.set_has_arrow(False)
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_right_click)
        self.add_controller(click)
        self.job = None
        self.update(job)

    def _button(self, icon, cb, tooltip=None):
        b = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
        b.add_css_class("flat")
        b.add_css_class("circular")
        b.connect("clicked", cb)
        return b

    def _on_right_click(self, gesture, n, x, y):
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.popover.set_pointing_to(rect)
        self.popover.popup()

    def update(self, job):
        self.job = job
        st = job["status"]
        self.title.set_label(job["name"])
        self.title.set_tooltip_text(job["url"])
        icon = gicon_for(job)
        if getattr(self, "_icon_key", None) != str(icon):
            self._icon_key = str(icon)
            self.icon.set_from_paintable(file_icon(self, icon))
        size, done = job["size"], job["done"]
        frac = min(1.0, done / size) if size else 0.0
        show_progress(self.bar, self.map, job, frac)
        progress = f"{human_size(done)} of {human_size(size)}" if size else human_size(done)
        self.status.remove_css_class("error")
        if st == "active":
            parts = [progress]
            if job["speed"]:
                parts.append(human_size(job["speed"]) + "/s")
            if job["eta"] >= 0:
                parts.append(human_eta(job["eta"]) + " left")
            if job["detail"]:
                parts.append(job["detail"])
            text = " · ".join(parts)
            icon, tip = "media-playback-pause-symbolic", "Pause"
        elif st == "queued":
            text = job["detail"] or "Waiting in queue"
            if job["error"] and job["retry_at"]:
                text += " — " + job["error"][:120]
            icon, tip = "media-playback-pause-symbolic", "Pause"
        elif st == "paused":
            text = "Paused · " + progress if done else "Paused"
            icon, tip = "media-playback-start-symbolic", "Resume"
        elif st == "error":
            text = job["error"] or "Failed"
            self.status.add_css_class("error")
            icon, tip = "view-refresh-symbolic", "Try again"
        else:
            finished = time.strftime("%H:%M", time.localtime(job["finished"])) if job["finished"] else ""
            text = " · ".join(x for x in (human_size(size or done), "Done " + finished,
                                          job["detail"]) if x.strip())
            icon, tip = "haul-open-symbolic", "Open"
        self.status.set_label(text)
        self.status.set_tooltip_text(text)
        self.main_btn.set_icon_name(icon)
        self.main_btn.set_tooltip_text(tip)

    def _on_main(self, *_):
        st, m = self.job["status"], self.win.app.manager
        if st in ("active", "queued"):
            m.pause(self.job_id)
        elif st in ("paused", "error"):
            m.resume(self.job_id)
        else:
            self._open_file()

    def _target(self):
        j = self.job
        if j["status"] == "done" and j["path"] and os.path.exists(j["path"]):
            return j["path"]
        return None

    def _open_file(self, *_):
        path = self._target()
        if path:
            Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self.win, None, None)
        else:
            self.win.toast("The file isn't there yet.")

    def _on_folder(self, *_):
        path = self._target()
        if path:
            Gtk.FileLauncher.new(Gio.File.new_for_path(path)).open_containing_folder(self.win, None, None)
        elif os.path.isdir(self.job["dir"]):
            Gtk.FileLauncher.new(Gio.File.new_for_path(self.job["dir"])).launch(self.win, None, None)

    def _on_remove(self, *_):
        self.win.app.manager.remove(self.job_id)

    def _copy_link(self, *_):
        self.get_clipboard().set(self.job["url"])
        self.win.toast("Link copied")

    def _delete_file(self, *_):
        dialog = Adw.AlertDialog(heading="Delete the file too?",
                                 body=f"“{self.job['name']}” will be removed from the list and deleted from disk.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda d, r: r == "delete" and
                       self.win.app.manager.remove(self.job_id, delete_files=True))
        dialog.present(self.win)


# ---------------------------------------------------------------- add dialog

class AddDialog(Adw.Dialog):
    def __init__(self, app, url="", headers=None, result=None, title=""):
        super().__init__(title="Add Download", follows_content_size=True)
        self.app, self.headers, self.page_title = app, dict(headers or {}), title
        self.result = None
        self.token = 0
        self.debounce = 0
        self.directory = app.settings.download_dir()
        self.folder_chosen = False

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12,
                       margin_bottom=12, margin_start=18, margin_end=18)
        page.add = page.append
        page.set_size_request(500, -1)
        # The dialog grows with its content instead of hiding it behind a scrollbar.
        view.set_content(Gtk.ScrolledWindow(child=page, propagate_natural_height=True,
                                            hscrollbar_policy=Gtk.PolicyType.NEVER))

        g = Adw.PreferencesGroup()
        self.link = Adw.EntryRow(title="Link (web page, file, stream, magnet or .torrent)")
        if url:
            self.link.set_text(url)  # before connecting, so a given link is checked only once
        self.link.connect("changed", self._on_link_changed)
        self.link.connect("entry-activated", lambda *_: self._probe_now())
        paste = Gtk.Button(icon_name="edit-paste-symbolic", tooltip_text="Paste",
                           valign=Gtk.Align.CENTER)
        paste.add_css_class("flat")
        paste.connect("clicked", self._paste)
        self.link.add_suffix(paste)
        g.add(self.link)
        page.add(g)

        self.state_group = Adw.PreferencesGroup()
        self.state_row = Adw.ActionRow(title="Paste or type a link")
        self.spinner = Adw.Spinner(visible=False)
        self.state_row.add_prefix(self.spinner)
        self.state_icon = Gtk.Image(visible=False)
        self.state_row.add_prefix(self.state_icon)
        self.install_btn = Gtk.Button(label="Install yt-dlp", valign=Gtk.Align.CENTER, visible=False)
        self.install_btn.connect("clicked", lambda *_: self.app.install_ytdlp(self._probe_now))
        self.state_row.add_suffix(self.install_btn)
        self.state_group.add(self.state_row)
        page.add(self.state_group)

        self.details = Adw.PreferencesGroup(visible=False)
        self.name_row = Adw.EntryRow(title="Save as")
        self.details.add(self.name_row)
        self.quality = Adw.ComboRow(title="Quality")
        self.details.add(self.quality)
        self.playlist = Adw.SwitchRow(title="Download the whole playlist")
        self.details.add(self.playlist)
        self.folder_row = Adw.ActionRow(title="Folder", subtitle=self.directory)
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        change.connect("clicked", self._choose_folder)
        self.folder_row.add_suffix(change)
        self.details.add(self.folder_row)
        self.start_now = Adw.SwitchRow(title="Start right away", active=True)
        self.details.add(self.start_now)
        page.add(self.details)

        bottom = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=18)
        self.go = Gtk.Button(label="Download", sensitive=False)
        self.go.add_css_class("suggested-action")
        self.go.add_css_class("pill")
        self.go.connect("clicked", self._on_go)
        bottom.append(self.go)
        view.add_bottom_bar(bottom)
        self.set_child(view)
        self.set_default_widget(self.go)

        if result is not None:
            self._show(result)
        elif url:
            self._probe_now()

    # --- probing ---
    def _on_link_changed(self, *_):
        if self.debounce:
            GLib.source_remove(self.debounce)
        self.debounce = GLib.timeout_add(700, self._probe_now)

    def _paste(self, *_):
        def done(clip, res):
            try:
                text = clip.read_text_finish(res)
            except GLib.Error:
                return
            if text:
                self.link.set_text(text.strip())
        self.get_clipboard().read_text_async(None, done)

    def _urls(self):
        text = self.link.get_text().strip()
        urls = extract_urls(text)
        if not urls and os.path.isfile(text) and text.lower().endswith(".torrent"):
            urls = [text]
        return urls

    def _probe_now(self, *_):
        self.debounce = 0
        self.token += 1
        token = self.token
        urls = self._urls()
        self.result = None
        self.go.set_sensitive(False)
        self.details.set_visible(False)
        self.install_btn.set_visible(False)
        if not urls:
            self._state("Paste or type a link", busy=False)
            return False
        if len(urls) > 1:
            self._state(f"{len(urls)} links", f"Each one is checked and added with the default settings.",
                        icon="view-list-symbolic")
            self.go.set_label(f"Download {len(urls)} links")
            self.go.set_sensitive(True)
            return False
        self.go.set_label("Download")
        self._state("Checking the link…", urls[0], busy=True)
        cookies = self.app.settings["cookies_browser"]

        def work():
            try:
                r = probe.classify(urls[0], self.headers, cookies)
            except Exception as e:
                r = probe.Probe("error", urls[0], error="internal", message=str(e))
            GLib.idle_add(lambda: token == self.token and self._show(r))
        threading.Thread(target=work, daemon=True).start()
        return False

    def _state(self, title, subtitle="", busy=False, icon=None, error=False):
        self.state_row.set_title(GLib.markup_escape_text(title))
        self.state_row.set_subtitle(GLib.markup_escape_text(subtitle or ""))
        self.spinner.set_visible(busy)
        self.state_icon.set_visible(bool(icon))
        if icon:
            self.state_icon.set_from_icon_name(icon)
        if error:
            self.state_icon.add_css_class("error")
        else:
            self.state_icon.remove_css_class("error")

    def _show(self, r):
        self.result = r
        if r.kind == "error":
            self._state(r.message or "This link can't be downloaded.", r.url, icon="dialog-warning-symbolic",
                        error=True)
            self.install_btn.set_visible(r.error == "no_ytdlp")
            return False
        info = r.media
        name = r.name
        if r.kind == "media":
            if self.page_title and (not info.get("title") or info.get("extractor", "").lower().startswith("generic")):
                name = self.page_title
            kind = info.get("extractor") or "Video"
            bits = [kind]
            if info.get("is_playlist"):
                bits.append(f"playlist, {info.get('count', 0)} items")
            if info.get("duration"):
                bits.append(human_eta(int(info["duration"])).replace(" left", ""))
            if r.size:
                bits.append("about " + human_size(r.size))
            if info.get("is_live"):
                bits.append("live — records until you pause it")
            self._state(info.get("title") or name, " · ".join(bits), icon="haul-video-symbolic")
        elif r.kind == "torrent":
            self._state(name, "Torrent · downloads with peers", icon="network-transmit-receive-symbolic")
        else:
            ctype, _ = Gio.content_type_guess(name, None)
            bits = [human_size(r.size) if r.size else "Unknown size", Gio.content_type_get_description(ctype),
                    "resumable, multi-connection" if r.resumable else "single connection (server can't resume)"]
            self._state(name, " · ".join(bits), icon="document-save-symbolic")
        self.name_row.set_text(name)
        if not self.folder_chosen:
            self.directory = self.app.folder_for(file_type(r.kind, name, {}))
            self.folder_row.set_subtitle(self.directory)
        self.name_row.set_visible(not info.get("is_playlist"))
        is_media = r.kind == "media"
        self.quality.set_visible(is_media)
        if is_media:
            self.qualities = media.qualities(info)
            self.quality.set_model(Gtk.StringList.new([label for _, label in self.qualities]))
            default = self.app.settings["video_quality"]
            keys = [k for k, _ in self.qualities]
            if default in keys:
                self.quality.set_selected(keys.index(default))
            elif default.startswith("h"):  # pick the best height not above the default
                want = int(default[1:])
                for i, k in enumerate(keys):
                    if k.startswith("h") and int(k[1:]) <= want:
                        self.quality.set_selected(i)
                        break
        self.playlist.set_visible(is_media and (info.get("is_playlist") or info.get("in_playlist")))
        self.playlist.set_active(bool(info.get("is_playlist")))
        self.details.set_visible(True)
        self.go.set_sensitive(True)
        return False

    def _choose_folder(self, *_):
        dialog = Gtk.FileDialog(title="Download folder", initial_folder=Gio.File.new_for_path(self.directory))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.directory = f.get_path()
                self.folder_chosen = True
                self.folder_row.set_subtitle(self.directory)
        dialog.select_folder(self.get_root(), None, done)

    # --- add ---
    def _on_go(self, *_):
        urls = self._urls()
        if len(urls) > 1:
            self.app.add_batch(urls, self.headers, self.directory)
            self.close()
            return
        r = self.result
        if not r or r.kind == "error":
            return
        name = probe.sanitize(self.name_row.get_text().strip() or r.name)
        options, url = {}, r.url
        if r.kind == "http":
            options["resumable"] = r.resumable
        elif r.kind == "media":
            options["quality"] = self.qualities[self.quality.get_selected()][0]
            options["playlist"] = self.playlist.get_visible() and self.playlist.get_active()
            url = r.final_url or r.url
        elif r.kind == "torrent" and os.path.isfile(r.url):
            options.update(torrent_options(r.url))
        jid = self.app.manager.add(url, r.kind, name, self.directory, self.headers, options,
                                   size=r.size, start=self.start_now.get_active())
        self.app.remember_folder(file_type(r.kind, name, options), self.directory)
        self.app.window.toast(f"Added “{name}”")
        self.app.window.focus_job(jid)
        self.close()


# ---------------------------------------------------------------- save dialog

class SaveDialog(Adw.Dialog):
    """IDM-style "where do you want to save it?" step for browser downloads."""

    def __init__(self, app, spec):
        super().__init__(title="Save Download", follows_content_size=True)
        self.app, self.spec = app, spec
        self.type = spec.get("type") or file_type(spec["kind"], spec["name"], spec["options"])
        words = TYPE_WORDS[self.type]
        recent = [d for d in app.settings["recent_dirs"] if os.path.isdir(d)]
        # the folder this type usually goes to comes first and is preselected
        self.dirs = list(dict.fromkeys([app.folder_for(self.type), *recent, app.settings.download_dir()]))[:7]

        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12,
                      margin_bottom=12, margin_start=18, margin_end=18)
        box.set_size_request(480, -1)
        view.set_content(box)

        g = Adw.PreferencesGroup()
        self.name = Adw.EntryRow(title="File name", text=spec["name"])
        self.name.connect("entry-activated", self._go)
        g.add(self.name)
        if spec.get("label"):
            what = Adw.ActionRow(title="Quality", subtitle=GLib.markup_escape_text(spec["label"]))
            g.add(what)
        elif spec.get("size"):
            g.add(Adw.ActionRow(title="Size", subtitle=human_size(spec["size"])))
        box.append(g)

        g = Adw.PreferencesGroup()
        learned = self.type in app.settings["type_dirs"]
        self.folder = Adw.ComboRow(title="Save to", model=Gtk.StringList.new([self._short(d) for d in self.dirs]),
                                   subtitle=f"Where you save {words}" if learned else f"Folder for {words}")
        browse = Gtk.Button(label="Browse…", valign=Gtk.Align.CENTER)
        browse.connect("clicked", self._browse)
        self.folder.add_suffix(browse)
        g.add(self.folder)
        self.remember = Adw.SwitchRow(title=f"Always save {words} here",
                                      subtitle="Skip this window for this type. Change it any time in Settings → Folders",
                                      active=bool(app.settings["type_auto"].get(self.type)))
        g.add(self.remember)
        box.append(g)

        bottom = Gtk.Box(halign=Gtk.Align.CENTER, spacing=12, margin_top=6, margin_bottom=18)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("pill")
        cancel.connect("clicked", lambda *_: self.close())
        self.go = Gtk.Button(label="Start Download")
        self.go.add_css_class("pill")
        self.go.add_css_class("suggested-action")
        self.go.connect("clicked", self._go)
        bottom.append(cancel)
        bottom.append(self.go)
        view.add_bottom_bar(bottom)
        self.set_child(view)
        self.set_default_widget(self.go)
        self.set_focus(self.go)

    @staticmethod
    def _short(path):
        home = os.path.expanduser("~")
        return "~" + path[len(home):] if path.startswith(home) else path

    def _browse(self, *_):
        current = self.dirs[self.folder.get_selected()]
        dialog = Gtk.FileDialog(title="Save to folder", initial_folder=Gio.File.new_for_path(current))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                path = f.get_path()
                if path in self.dirs:
                    self.folder.set_selected(self.dirs.index(path))
                    return
                self.dirs.insert(0, path)
                self.folder.set_model(Gtk.StringList.new([self._short(x) for x in self.dirs]))
                self.folder.set_selected(0)
        dialog.select_folder(self.get_root(), None, done)

    def _go(self, *_):
        directory = self.dirs[self.folder.get_selected()]
        self.spec["name"] = probe.sanitize(self.name.get_text().strip() or self.spec["name"])
        self.app.remember_folder(self.type, directory, auto=self.remember.get_active())
        self.app._start(self.spec, directory)
        self.app.withdraw_notification("save")
        self.app._announce(self.spec["name"])
        self.close()


# ---------------------------------------------------------------- progress popup

class ProgressWindow(Adw.Window):
    """Small IDM-style window for one download: progress, speed, Pause/Cancel, then Open."""

    def __init__(self, app, jid):
        super().__init__(application=app, title="Haul", default_width=460, resizable=False)
        self.app, self.jid, self.state = app, jid, None
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.wtitle = Adw.WindowTitle(title="Starting download")
        header.set_title_widget(self.wtitle)
        view.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=4,
                      margin_bottom=16, margin_start=16, margin_end=16)
        top = Gtk.Box(spacing=12)
        self.icon = Gtk.Image(pixel_size=40, valign=Gtk.Align.CENTER)
        top.append(self.icon)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.name.add_css_class("heading")
        self.where = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.START)
        self.where.add_css_class("caption")
        self.where.add_css_class("dim-label")
        names.append(self.name)
        names.append(self.where)
        top.append(names)
        box.append(top)
        self.bar = Gtk.ProgressBar(margin_top=4)
        box.append(self.bar)
        self.map = SegmentMap(16, 6)
        self.map.set_margin_top(4)
        box.append(self.map)
        self.line1 = Gtk.Label(xalign=0, wrap=True)
        self.line1.add_css_class("job-status")
        self.line2 = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.line2.add_css_class("caption")
        self.line2.add_css_class("dim-label")
        self.line2.add_css_class("job-status")
        box.append(self.line1)
        box.append(self.line2)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8)
        self.b_pause = self._button("Pause", self._pause)
        self.b_cancel = self._button("Cancel", self._cancel)
        self.b_retry = self._button("Try Again", lambda *_: self.app.manager.retry(self.jid))
        self.b_folder = self._button("Open Folder", self._folder)
        self.b_open = self._button("Open", self._open, suggested=True)
        self.b_close = self._button("Close", lambda *_: self.close())
        for b in (self.b_retry, self.b_pause, self.b_cancel, self.b_folder, self.b_close, self.b_open):
            buttons.append(b)
        box.append(buttons)
        view.set_content(box)
        self.set_content(view)
        self.job = None

    def _button(self, label, cb, suggested=False):
        b = Gtk.Button(label=label)
        if suggested:
            b.add_css_class("suggested-action")
        b.connect("clicked", cb)
        return b

    def update(self, job):
        self.job = job
        st = job["status"]
        self.name.set_label(job["name"])
        self.where.set_label(SaveDialog._short(job["dir"]))
        icon = gicon_for(job)
        if getattr(self, "_icon_key", None) != str(icon):
            self._icon_key = str(icon)
            self.icon.set_from_paintable(file_icon(self, icon, 40))
        size, done = job["size"], job["done"]
        frac = min(1.0, done / size) if size else 0.0
        show_progress(self.bar, self.map, job, frac)
        pct = f" ({frac * 100:.0f}%)" if size else ""
        amount = f"{human_size(done)} of {human_size(size)}{pct}" if size else human_size(done)
        self.line1.remove_css_class("error")
        if st == "active":
            title = "Downloading"
            l1 = amount
            bits = [human_size(job["speed"]) + "/s" if job["speed"] else "",
                    human_eta(job["eta"]) + " left" if job["eta"] >= 0 else "", job["detail"]]
            l2 = " · ".join(b for b in bits if b)
        elif st == "queued":
            title, l1, l2 = "Waiting", job["detail"] or "Waiting for a free slot", job["error"][:120]
        elif st == "paused":
            title, l1, l2 = "Paused", amount, ""
        elif st == "error":
            title, l1, l2 = "Download failed", job["error"] or "Failed", ""
            self.line1.add_css_class("error")
        else:
            title, l1, l2 = "Download complete", f"{human_size(size or done)} saved", job["detail"]
        self.wtitle.set_title(title)
        self.line1.set_label(l1)
        self.line2.set_label(l2)
        self.line2.set_visible(bool(l2))
        done_ = st == "done"
        self.b_pause.set_visible(st in ("active", "queued", "paused"))
        self.b_pause.set_label("Resume" if st == "paused" else "Pause")
        self.b_cancel.set_visible(not done_)
        self.b_retry.set_visible(st == "error")
        self.b_open.set_visible(done_)
        self.b_folder.set_visible(done_)
        self.b_close.set_visible(done_ or st == "error")
        if done_ and self.state != "done":
            self.set_default_widget(self.b_open)
        self.state = st

    def _pause(self, *_):
        if self.job and self.job["status"] == "paused":
            self.app.manager.resume(self.jid)
        else:
            self.app.manager.pause(self.jid)

    def _cancel(self, *_):
        self.app.manager.remove(self.jid, delete_files=True)  # only partial data at this point
        self.close()

    def _target(self):
        j = self.job
        return j["path"] if j and j["path"] and os.path.exists(j["path"]) else None

    def _open(self, *_):
        if self._target():
            Gtk.FileLauncher.new(Gio.File.new_for_path(self._target())).launch(self, None, None)
            self.close()

    def _folder(self, *_):
        if self._target():
            Gtk.FileLauncher.new(Gio.File.new_for_path(self._target())).open_containing_folder(self, None, None)
            self.close()


# ---------------------------------------------------------------- settings

class SettingsDialog(Adw.PreferencesDialog):
    FORMATS = [("mp4", "MP4 — plays everywhere"), ("best", "Best quality (WebM/MKV allowed)")]
    QUALITIES = [("best", "Best available"), ("h2160", "2160p (4K)"), ("h1440", "1440p"),
                 ("h1080", "1080p"), ("h720", "720p"), ("h480", "480p"), ("audio_mp3", "Audio only (MP3)")]
    BROWSERS = [("", "Don't use"), ("firefox", "Firefox"), ("chrome", "Chrome"), ("chromium", "Chromium"),
                ("brave", "Brave"), ("edge", "Edge"), ("vivaldi", "Vivaldi"), ("opera", "Opera")]

    def __init__(self, app):
        super().__init__(title="Settings")
        self.app, s = app, app.settings

        general = Adw.PreferencesPage(title="General", icon_name="emblem-system-symbolic")
        g = Adw.PreferencesGroup(title="Downloads")
        self.folder = Adw.ActionRow(title="Download folder", subtitle=s.download_dir())
        btn = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._choose_folder)
        self.folder.add_suffix(btn)
        g.add(self.folder)
        g.add(self._switch("Sort into folders by type", "sort_by_type",
                           "Video, Music, Compressed, Programs, Documents and Images"))
        g.add(self._spin("Downloads at the same time", "max_active", 1, 10))
        g.add(self._spin("Connections per file", "connections", 1, 16,
                         "More connections are faster on most servers"))
        g.add(self._spin("Speed limit (KB/s)", "speed_limit_kib", 0, 10_000_000, "0 means no limit", step=100))
        general.add(g)
        g = Adw.PreferencesGroup(title="Behaviour")
        g.add(self._switch("Start Haul when you log in", "autostart",
                           "Hidden in the background, so the browser button is instant and downloads resume"))
        g.add(self._switch("Keep running when the window is closed", "run_in_background",
                           "Downloads continue; open Haul again from the app grid"))
        g.add(self._switch("Notify when a download finishes", "notify"))
        g.add(self._switch("Show a progress window for browser downloads", "progress_window",
                           "Small window with speed, Pause and Cancel, then Open when done"))
        g.add(self._switch("Ask where to save browser downloads", "ask_where_to_save",
                           "Shows the file name and folder before a video download starts"))
        g.add(self._switch("Ask before downloading from the browser", "ask_before_browser_download",
                           "When off, captured downloads start right away"))
        general.add(g)
        g = Adw.PreferencesGroup(title="Torrents")
        g.add(self._switch("Seed after downloading", "seed_torrents",
                           "Share back until ratio 1.0 or 24 hours"))
        general.add(g)
        self.add(general)

        video = Adw.PreferencesPage(title="Video", icon_name="haul-video-symbolic")
        g = Adw.PreferencesGroup(title="yt-dlp",
                                 description="yt-dlp finds the video on YouTube and about 1800 other sites. "
                                             "Sites change often, so keep it up to date.")
        self.ytdlp_row = Adw.ActionRow(title="yt-dlp")
        self.ytdlp_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.ytdlp_btn.connect("clicked", self._install_ytdlp)
        self.ytdlp_row.add_suffix(self.ytdlp_btn)
        g.add(self.ytdlp_row)
        g.add(self._switch("Update automatically", "ytdlp_auto_update", "Checks once a day"))
        video.add(g)
        g = Adw.PreferencesGroup(title="Defaults")
        g.add(self._combo("Default quality", "video_quality", self.QUALITIES))
        g.add(self._combo("Preferred format", "video_format", self.FORMATS,
                          "Listed first on the download button in your browser"))
        g.add(self._combo("Use cookies from browser", "cookies_browser", self.BROWSERS,
                          "For videos that need you to be signed in"))
        video.add(g)
        self.add(video)
        self._refresh_ytdlp()

        folders = Adw.PreferencesPage(title="Folders", icon_name="haul-folder-symbolic")
        g = Adw.PreferencesGroup(title="Where each type of file goes",
                                 description="Haul remembers the folder you pick for each type and "
                                             "suggests it next time. Turn on “Save without asking” to skip "
                                             "the save window for that type.")
        for kind in TYPES:
            g.add(self._type_row(kind))
        folders.add(g)
        self.add(folders)
        self._folders_page = folders

        browser = Adw.PreferencesPage(title="Browser", icon_name="haul-browser-symbolic")
        self.browser_group = Adw.PreferencesGroup(
            title="Browser integration",
            description="Lets your browser hand downloads, videos and streams to Haul. "
                        "If Haul can't take a download (for example a robot check), the browser keeps it.")
        browser.add(self.browser_group)
        self.browser_rows = []
        setup = Adw.PreferencesGroup()
        b = Adw.ButtonRow(title="Set up browser integration", start_icon_name="emblem-ok-symbolic")
        b.add_css_class("suggested-action")
        b.connect("activated", self._setup_browsers)
        setup.add(b)
        browser.add(setup)
        g = Adw.PreferencesGroup(title="Install the extension")
        row = Adw.ActionRow(title="Chrome, Brave, Edge, Vivaldi, Opera, Chromium, Helium",
                            subtitle="Open the browser's Extensions page, turn on Developer mode, "
                                     "choose Load unpacked and pick the chrome folder.")
        row.set_subtitle_lines(0)
        open_btn = Gtk.Button(label="Open folder", valign=Gtk.Align.CENTER)
        open_btn.connect("clicked", lambda *_: self._open_ext("chrome"))
        row.add_suffix(open_btn)
        g.add(row)
        row = Adw.ActionRow(title="Firefox and Zen",
                            subtitle="Open about:debugging → This Firefox → Load Temporary Add-on and pick "
                                     "manifest.json in the firefox folder. Regular Firefox removes "
                                     "unsigned add-ons when it restarts.")
        row.set_subtitle_lines(0)
        open_btn = Gtk.Button(label="Open folder", valign=Gtk.Align.CENTER)
        open_btn.connect("clicked", lambda *_: self._open_ext("firefox"))
        row.add_suffix(open_btn)
        g.add(row)
        browser.add(g)
        self.add(browser)
        self._refresh_browsers()

    def _type_row(self, kind):
        app = self.app
        row = Adw.ExpanderRow(title=kind, subtitle=SaveDialog._short(app.folder_for(kind)))
        place = Adw.ActionRow(title="Folder", subtitle=app.folder_for(kind))
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)

        def pick(*_):
            dialog = Gtk.FileDialog(title=f"Folder for {TYPE_WORDS[kind]}",
                                    initial_folder=Gio.File.new_for_path(app.folder_for(kind)))

            def done(d, res):
                try:
                    f = d.select_folder_finish(res)
                except GLib.Error:
                    return
                if f and f.get_path():
                    app.remember_folder(kind, f.get_path())
                    place.set_subtitle(f.get_path())
                    row.set_subtitle(SaveDialog._short(f.get_path()))
            dialog.select_folder(app.window, None, done)
        change.connect("clicked", pick)
        place.add_suffix(change)
        row.add_row(place)
        auto = Adw.SwitchRow(title="Save without asking", subtitle="Start right away in this folder",
                             active=bool(app.settings["type_auto"].get(kind)))
        auto.connect("notify::active", lambda r, _: app.settings.set(
            "type_auto", {**app.settings["type_auto"], kind: r.get_active()}))
        row.add_row(auto)
        return row

    def _switch(self, title, key, subtitle=None):
        row = Adw.SwitchRow(title=title, subtitle=subtitle or "", active=bool(self.app.settings[key]))
        row.connect("notify::active", lambda r, _: self._set(key, r.get_active()))
        return row

    def _spin(self, title, key, lo, hi, subtitle=None, step=1):
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_value(self.app.settings[key])
        row.connect("notify::value", lambda r, _: self._set(key, int(r.get_value())))
        return row

    def _combo(self, title, key, options, subtitle=None):
        row = Adw.ComboRow(title=title, subtitle=subtitle or "",
                           model=Gtk.StringList.new([label for _, label in options]))
        keys = [k for k, _ in options]
        cur = self.app.settings[key]
        row.set_selected(keys.index(cur) if cur in keys else 0)
        row.connect("notify::selected", lambda r, _: self._set(key, keys[r.get_selected()]))
        return row

    def _set(self, key, value):
        self.app.settings.set(key, value)
        if key == "autostart":
            integration.set_autostart(value)
        if key in ("speed_limit_kib", "seed_torrents"):
            self.app.manager.apply_settings()

    def _choose_folder(self, *_):
        dialog = Gtk.FileDialog(title="Download folder")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.app.settings.set("download_dir", f.get_path())
                self.folder.set_subtitle(f.get_path())
        dialog.select_folder(self.app.window, None, done)

    def _refresh_ytdlp(self):
        version = tools.ytdlp_version() if media.available() else None
        managed = tools.managed_ytdlp().exists()
        if version:
            self.ytdlp_row.set_subtitle(f"Version {version}" + ("" if managed else " (system copy)"))
            self.ytdlp_btn.set_label("Update now")
        else:
            self.ytdlp_row.set_subtitle("Not installed — video sites won't work")
            self.ytdlp_btn.set_label("Install")
        self.ytdlp_btn.set_sensitive(True)

    def _install_ytdlp(self, *_):
        self.ytdlp_btn.set_sensitive(False)
        self.ytdlp_btn.set_label("Working…")
        self.app.install_ytdlp(self._refresh_ytdlp)

    def _refresh_browsers(self):
        for r in self.browser_rows:
            self.browser_group.remove(r)
        self.browser_rows = []
        found = integration.status()
        if not found:
            r = Adw.ActionRow(title="No supported browser found")
            self.browser_group.add(r)
            self.browser_rows.append(r)
        for name, ok in found:
            r = Adw.ActionRow(title=name, subtitle="Connected" if ok else "Not set up")
            icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if ok else "dialog-warning-symbolic")
            icon.add_css_class("success" if ok else "warning")
            r.add_prefix(icon)
            self.browser_group.add(r)
            self.browser_rows.append(r)

    def _setup_browsers(self, *_):
        try:
            ext_id, done = integration.register()
        except Exception as e:
            self.add_toast(Adw.Toast(title=f"Setup failed: {e}"))
            return
        self._refresh_browsers()
        self.add_toast(Adw.Toast(title=f"Set up for {len(done)} browser(s). Now load the extension, "
                                       "then restart the browser."))

    def _open_ext(self, which):
        folder = integration.OUT / which
        if not folder.exists():
            try:
                integration.build_extension()
            except Exception as e:
                self.add_toast(Adw.Toast(title=f"Could not build the extension: {e}"))
                return
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(folder))).launch(self.app.window, None, None)


# ---------------------------------------------------------------- window

class MainWindow(Adw.ApplicationWindow):
    FILTERS = {"all": lambda j: True,
               "active": lambda j: j["status"] != "done",
               "done": lambda j: j["status"] == "done"}

    def __init__(self, app):
        super().__init__(application=app, title="Haul", default_width=860, default_height=620)
        self.app = app
        self.rows = {}
        self.filter = "all"
        self.set_size_request(360, 300)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        self.wtitle = Adw.WindowTitle(title="Haul")
        header.set_title_widget(self.wtitle)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add download (Ctrl+N)")
        add.add_css_class("suggested-action")
        add.set_action_name("app.add")
        header.pack_start(add)
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Pause all", "app.pause-all")
        section.append("Resume all", "app.resume-all")
        section.append("Clear finished", "app.clear-finished")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Settings", "app.settings")
        section.append("About Haul", "app.about")
        section.append("Quit", "app.quit")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Menu", primary=True))
        view.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_banner)
        view.add_top_bar(self.banner)

        bar = Gtk.Box(halign=Gtk.Align.CENTER)
        bar.add_css_class("filter-bar")
        group = Adw.ToggleGroup()
        for name, label in (("all", "All"), ("active", "In progress"), ("done", "Finished")):
            group.add(Adw.Toggle(name=name, label=label))
        group.set_active_name("all")
        group.connect("notify::active-name", self._on_filter)
        bar.append(group)
        self.filter_bar = bar
        view.add_top_bar(bar)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        empty = Adw.StatusPage(icon_name="folder-download-symbolic", title="No downloads yet",
                               description="Paste a link with Ctrl+V, drop a link or .torrent here, "
                                           "or use the Haul button in your browser.")
        btn = Gtk.Button(label="Add Download", halign=Gtk.Align.CENTER)
        btn.add_css_class("pill")
        btn.add_css_class("suggested-action")
        btn.set_action_name("app.add")
        empty.set_child(btn)
        self.stack.add_named(empty, "empty")
        self.nothing = Adw.StatusPage(icon_name="folder-download-symbolic")
        self.stack.add_named(self.nothing, "nothing")

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self.list.add_css_class("boxed-list")
        self.list.set_sort_func(lambda a, b: (b.added > a.added) - (b.added < a.added))
        self.list.set_filter_func(lambda row: row.job is not None and self.FILTERS[self.filter](row.job))
        clamp = Adw.Clamp(maximum_size=980, child=self.list, margin_top=12, margin_bottom=24,
                          margin_start=12, margin_end=12)
        self.scroller = Gtk.ScrolledWindow(child=clamp, vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self.scroller, "list")
        view.set_content(self.stack)

        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>v"),
                                       action=Gtk.CallbackAction.new(self._paste)))
        self.add_controller(keys)
        drop = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
        drop.set_gtypes([Gdk.FileList, GObject.TYPE_STRING])
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)
        self.connect("close-request", self._on_close)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))

    def focus_job(self, jid):
        self.pending_focus = jid

    def update(self, jobs):
        seen = set()
        for job in jobs:
            seen.add(job["id"])
            row = self.rows.get(job["id"])
            if row is None:
                row = DownloadRow(self, job)
                self.rows[job["id"]] = row
                self.list.append(row)
            else:
                row.update(job)
        for jid in list(self.rows):
            if jid not in seen:
                self.list.remove(self.rows.pop(jid))
        self.list.invalidate_filter()
        visible = [j for j in jobs if self.FILTERS[self.filter](j)]
        self.filter_bar.set_visible(bool(jobs))
        self.nothing.set_title("No finished downloads" if self.filter == "done" else "Nothing in progress")
        self.stack.set_visible_child_name("list" if visible else ("nothing" if jobs else "empty"))
        active, speed = sum(1 for j in jobs if j["status"] == "active"), sum(j["speed"] for j in jobs)
        waiting = sum(1 for j in jobs if j["status"] == "queued")
        bits = []
        if active:
            bits.append(f"{active} downloading · {human_size(speed)}/s")
        if waiting:
            bits.append(f"{waiting} waiting")
        self.wtitle.set_subtitle(" · ".join(bits))

    def set_banner(self, text, button=None):
        self.banner.set_title(text or "")
        self.banner.set_button_label(button)
        self.banner.set_revealed(bool(text))

    def _on_banner(self, *_):
        self.app.install_ytdlp()

    def _on_filter(self, group, _):
        self.filter = group.get_active_name() or "all"
        self.list.invalidate_filter()
        self.update(self.app.manager.snapshot())

    def _paste(self, *_):
        if self.get_visible_dialog() is not None:
            return False

        def done(clip, res):
            try:
                text = clip.read_text_finish(res)
            except GLib.Error:
                return
            urls = extract_urls(text)
            if urls:
                self.app.open_add("\n".join(urls))
            else:
                self.toast("The clipboard has no link")
        self.get_clipboard().read_text_async(None, done)
        return True

    def _on_drop(self, target, value, x, y):
        if isinstance(value, Gdk.FileList):
            files = [f.get_path() for f in value.get_files() if f.get_path()]
            torrents = [p for p in files if p.lower().endswith(".torrent")]
            for t in torrents:
                self.app.open_add(t)
            return bool(torrents)
        urls = extract_urls(value)
        if urls:
            self.app.open_add("\n".join(urls))
        return bool(urls)

    def _on_close(self, *_):
        active, _ = self.app.manager.counts()
        if self.app.settings["run_in_background"]:
            self.app.go_background()
            return True
        if active:
            dialog = Adw.AlertDialog(heading="Downloads are running",
                                     body="Quitting pauses them. They continue the next time you open Haul.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("background", "Keep Running in Background")
            dialog.add_response("quit", "Quit")
            dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("background")

            def resp(d, r):
                if r == "background":
                    self.app.go_background()
                elif r == "quit":
                    self.app.quit()
            dialog.connect("response", resp)
            dialog.present(self)
            return True
        return False


# ---------------------------------------------------------------- application

class HaulApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.manager = None
        self.held = False
        self.told_background = False
        self.probe_cache = {}          # url -> (time, Probe) for the in-page button
        self.progress = {}             # job id -> ProgressWindow
        GLib.set_application_name("Haul")

    def do_startup(self):
        Adw.Application.do_startup(self)
        paths.ensure()
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
            str(paths.PROJECT / "data" / "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
        self.settings = Settings()
        self.manager = Manager(self.settings)
        self.manager.start()
        self.ipc = IPCServer(self.handle_external, self.handle_probe,
                             lambda msg: (GLib.idle_add(self.quit), {"ok": True})[1], self._prefs)
        self.ipc.start()
        for name, cb, accels in (
                ("add", lambda *a: self.open_add(), ["<Control>n"]),
                ("settings", lambda *a: SettingsDialog(self).present(self.window), ["<Control>comma"]),
                ("about", self._about, []),
                ("pause-all", lambda *a: self.manager.pause_all(), []),
                ("resume-all", lambda *a: self.manager.resume_all(), []),
                ("clear-finished", lambda *a: self.manager.clear_finished(), []),
                ("show", lambda *a: self.show_window(), []),
                ("quit", lambda *a: self.quit(), ["<Control>q"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            if accels:
                self.set_accels_for_action("app." + name, accels)
        present_save = Gio.SimpleAction.new("present-save", None)
        present_save.connect("activate", self._present_save)
        self.add_action(present_save)
        reveal = Gio.SimpleAction.new("reveal", GLib.VariantType.new("s"))
        reveal.connect("activate", self._reveal)
        self.add_action(reveal)
        self.window = MainWindow(self)
        GLib.timeout_add(500, self._tick)
        self._tick()
        threading.Thread(target=self._self_setup, daemon=True).start()

    def do_shutdown(self):
        try:
            self.ipc.stop()
            self.manager.shutdown()
        finally:
            Adw.Application.do_shutdown(self)

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        cwd = cmdline.get_cwd() or os.getcwd()
        background = "--background" in args
        items = []
        for a in args:
            if a.startswith("--"):
                continue
            if a.startswith("file://"):
                a = urllib.parse.unquote(urllib.parse.urlparse(a).path)
            if URL_RE.match(a):
                items.append(a)
            elif a.lower().endswith(".torrent"):
                items.append(os.path.abspath(os.path.join(cwd, a)))
        if background and not items:
            if not self.window.get_visible():
                self._hold()
        else:
            self.show_window()
        for item in items:
            self.open_add(item)
        return 0

    def show_window(self):
        self.window.present()

    def go_background(self):
        self.window.set_visible(False)
        self._hold()
        if not self.told_background:
            self.told_background = True
            n = Gio.Notification.new("Haul is still running")
            n.set_body("Downloads continue in the background. Open Haul from the app grid to see them.")
            self.send_notification("background", n)

    def _hold(self):
        if not self.held:
            self.held = True
            self.hold()

    def quit(self):
        if self.held:
            self.held = False
            self.release()
        Adw.Application.quit(self)

    # --- adding ---
    def open_add(self, url="", headers=None, result=None, title=""):
        self.show_window()
        current = self.window.get_visible_dialog()
        if isinstance(current, AddDialog) and not current._urls():
            current.close()
        AddDialog(self, url, headers, result, title).present(self.window)
        return False

    def add_batch(self, urls, headers, directory):
        self.window.toast(f"Checking {len(urls)} links…")
        cookies = self.settings["cookies_browser"]
        quality = self.settings["video_quality"]

        def work():
            failed = 0
            for u in urls:
                try:
                    r = probe.classify(u, headers, cookies)
                except Exception:
                    r = None
                if not r or r.kind == "error":
                    failed += 1
                    continue
                options = {"resumable": r.resumable} if r.kind == "http" else {"quality": quality}
                self.manager.add(r.final_url if r.kind == "media" else r.url, r.kind, r.name,
                                 directory, headers, options, size=r.size)
            msg = f"Added {len(urls) - failed} of {len(urls)} links" + (f" — {failed} could not be downloaded"
                                                                         if failed else "")
            GLib.idle_add(self.window.toast, msg)
        threading.Thread(target=work, daemon=True).start()

    def handle_external(self, msg):
        """Called on an IPC thread for requests from the browser extension."""
        url = msg["url"].strip()
        source = msg.get("source", "menu")
        headers = {}
        if msg.get("referrer"):
            headers["Referer"] = msg["referrer"]
        if msg.get("cookies"):
            headers["Cookie"] = msg["cookies"]
        if msg.get("userAgent"):
            headers["User-Agent"] = msg["userAgent"]
        title = (msg.get("title") or "").strip()
        if source == "button":
            return self._button_download(msg, url, headers, title)
        if source != "capture":
            GLib.idle_add(self.open_add, url, headers, None, title)
            return {"ok": True}
        # A captured browser download: only take it if we can really fetch it ourselves.
        r = probe.classify(url, headers, fast=True)
        if r.kind == "error" or (r.kind == "media" and r.error in ("web_page", "challenge_hint")):
            return {"ok": False, "reason": r.error or "web_page", "message": r.message}
        given = probe.sanitize(msg.get("filename") or "")
        if given != "download" and (r.name == "download" or "." not in r.name):
            r.name = given
        if r.kind == "media":
            GLib.idle_add(self.open_add, url, headers, None, title)
        elif self.settings["ask_before_browser_download"]:
            GLib.idle_add(self.open_add, url, headers, r, title)
        else:
            options = {"resumable": r.resumable} if r.kind == "http" else {}
            self._start(dict(url=r.url, kind=r.kind, name=r.name, headers=headers, options=options, size=r.size),
                        self.folder_for(file_type(r.kind, r.name, options)))
            GLib.idle_add(self.window.toast, f"Downloading “{r.name}”")
        return {"ok": True}

    @staticmethod
    def _headers(msg):
        h = {}
        for key, name in (("referrer", "Referer"), ("cookies", "Cookie"), ("userAgent", "User-Agent")):
            if msg.get(key):
                h[name] = msg[key]
        return h

    def folder_for(self, kind):
        """The folder this type of file goes to: learned from your choices, else a sensible default."""
        d = self.settings["type_dirs"].get(kind)
        if d and os.path.isdir(d):
            return d
        if kind in TYPE_XDG and not self.settings["download_dir"]:
            xdg = paths.user_dir(*TYPE_XDG[kind])
            if os.path.isdir(xdg):
                return xdg
        return self.settings.download_dir()

    def remember_folder(self, kind, directory, auto=None):
        s = self.settings
        s.set("type_dirs", {**s["type_dirs"], kind: directory})
        if auto is not None:
            s.set("type_auto", {**s["type_auto"], kind: bool(auto)})
        s.set("recent_dirs", list(dict.fromkeys([directory, *s["recent_dirs"]]))[:6])

    def _prefs(self):
        return {"quality": self.settings["video_quality"], "format": self.settings["video_format"]}

    def handle_probe(self, msg):
        """Formats for the in-page download button (IPC thread)."""
        headers = self._headers(msg)
        candidates = [msg["url"].strip()] + [u for u in msg.get("alt") or [] if isinstance(u, str)]
        last = None
        for url in dict.fromkeys(c for c in candidates if c):
            cached = self.probe_cache.get(url)
            if cached and time.time() - cached[0] < 600:
                r = cached[1]
            else:
                r = probe.classify(url, headers, self.settings["cookies_browser"])
            last = r
            if r.kind == "error":
                continue
            self.probe_cache[url] = (time.time(), r)
            mp4 = self.settings["video_format"] == "mp4"
            if r.kind == "media":
                options = media.choices(r.media, mp4)
            else:
                ctype, _ = Gio.content_type_guess(r.name, None)
                options = [{"id": "file", "label": "This file",
                            "detail": " · ".join(x for x in (Gio.content_type_get_description(ctype),
                                                             human_size(r.size) if r.size else "") if x)}]
            preferred = self.settings["video_quality"]
            ids = [o["id"] for o in options]
            if preferred not in ids:  # best height not above the preferred one
                want = int(preferred[1:]) if preferred.startswith("h") else 10 ** 6
                preferred = next((i for i in ids if i.startswith("h") and int(i[1:]) <= want), ids[0])
            return {"ok": True, "url": url, "kind": r.kind, "title": r.name or msg.get("title") or "",
                    "options": options, "preferred": preferred,
                    "playlist": bool(r.media.get("is_playlist")), "prefs": self._prefs()}
        reason = last.error if last else "error"
        message = last.message if last else "No video found."
        if reason == "no_ytdlp":
            message = "Install yt-dlp in Haul (Settings → Video) to download videos from sites."
        return {"ok": False, "reason": reason, "message": message}

    def _button_download(self, msg, url, headers, title):
        """A choice from the in-page button. Opens the Save dialog (or starts at once)."""
        choice = msg.get("quality") or "best"
        playlist = bool(msg.get("playlist"))
        cached = self.probe_cache.get(url)
        kind = msg.get("kind")
        if not cached and kind in ("media", "http"):
            # The menu already knew what this is (page data): no second lookup.
            name = probe.sanitize(title or probe.name_from(url))
            if kind == "http":
                # one quick request (well under a second): never save a web/robot page as the file
                r = probe.http_probe(url, headers, timeout=10)
                if r.kind == "error" or r.error in ("web_page", "challenge_hint"):
                    return {"ok": False, "reason": r.error or "web_page",
                            "message": r.message or "That link opens a web page, not a video file."}
                ext = os.path.splitext(r.name)[1]
                if not title or not ext:
                    name = r.name
                elif not name.lower().endswith(ext.lower()):
                    name += ext
                spec = dict(url=r.url, kind="http", name=name, options={"resumable": r.resumable}, size=r.size)
            else:
                spec = dict(url=url, kind="media", name=name, options={"quality": choice, "playlist": playlist},
                            size=0)
        else:
            r = cached[1] if cached else probe.classify(url, headers, self.settings["cookies_browser"])
            if r.kind == "error":
                return {"ok": False, "reason": r.error, "message": r.message}
            if r.kind == "media":
                spec = dict(url=r.final_url or url, kind="media", name=r.name or title or "video",
                            options={"quality": choice, "playlist": playlist}, size=r.size)
            else:
                spec = dict(url=r.url, kind=r.kind, name=r.name or title or "download",
                            options={"resumable": r.resumable} if r.kind == "http" else {}, size=r.size)
        spec["headers"] = headers
        spec["label"] = (msg.get("label") or "").strip()
        spec["type"] = file_type(spec["kind"], spec["name"], spec["options"])
        if self.settings["ask_where_to_save"] and not self.settings["type_auto"].get(spec["type"]):
            GLib.idle_add(self._ask_save, spec)
            return {"ok": True, "asked": True}
        directory = self.folder_for(spec["type"])
        jid = self._start(spec, directory)
        GLib.idle_add(self._announce, spec["name"], directory)
        return {"ok": True, "id": jid}

    def _start(self, spec, directory):
        jid = self.manager.add(spec["url"], spec["kind"], spec["name"], directory, spec["headers"],
                               spec["options"], size=spec.get("size") or 0)
        if self.settings["progress_window"]:
            GLib.idle_add(self.open_progress, jid)
        return jid

    def open_progress(self, jid):
        win = self.progress.get(jid)
        if win is None:
            win = ProgressWindow(self, jid)
            win.connect("close-request", lambda w: self.progress.pop(jid, None) and False)
            self.progress[jid] = win
            job = self.manager.get(jid)
            if job:
                win.update(job)
        win.present()
        return False

    def _ask_save(self, spec):
        dialog = SaveDialog(self, spec)
        dialog.present(None)   # a small window of its own, like IDM's; the main window stays as it is
        dialog.connect("closed", lambda *_: self.withdraw_notification("save"))
        if not self.window.is_active():
            # GNOME may open a background app's window behind the browser: offer a way to it.
            self.pending_save = dialog
            n = Gio.Notification.new("Where should Haul save it?")
            n.set_body(spec["name"])
            n.set_default_action("app.present-save")
            self.send_notification("save", n)
        return False

    def _present_save(self, *_):
        dialog = getattr(self, "pending_save", None)
        root = dialog.get_root() if dialog else None
        if root and root.get_visible():
            root.present()
        self.withdraw_notification("save")

    def _announce(self, name, directory=None):
        where = f" to {SaveDialog._short(directory)}" if directory else ""
        if self.window.get_visible():
            self.window.toast(f"Downloading “{name}”{where}")
        else:
            n = Gio.Notification.new("Downloading" + where)
            n.set_body(name)
            n.set_default_action("app.show")
            self.send_notification("started", n)
        return False

    # --- yt-dlp ---
    def install_ytdlp(self, then=None):
        self.window.toast("Getting yt-dlp…")

        def work():
            try:
                _, msg = tools.install_ytdlp()
            except Exception as e:
                msg = f"Could not get yt-dlp: {e}"
            GLib.idle_add(self.window.toast, msg)
            if then:
                GLib.idle_add(then)
        threading.Thread(target=work, daemon=True).start()

    def _self_setup(self):
        """Keep everything wired up without the user pressing anything."""
        try:
            integration.register()          # browser host + extension files (only rewrites changes)
        except Exception as e:
            _log(f"browser setup failed: {e!r}")
        try:
            integration.set_autostart(self.settings["autostart"])
        except Exception as e:
            _log(f"autostart failed: {e!r}")
        if self.settings["ytdlp_auto_update"] and (not media.available() or tools.ytdlp_update_due()):
            self._auto_update()

    def _auto_update(self):
        try:
            changed, msg = tools.install_ytdlp()
            if changed:
                _log(msg)
        except Exception as e:
            _log(f"yt-dlp update failed: {e!r}")

    # --- periodic ---
    def _tick(self):
        jobs = self.manager.snapshot()
        self.window.update(jobs)
        if self.progress:
            by_id = {j["id"]: j for j in jobs}
            for jid, win in list(self.progress.items()):
                if jid in by_id:
                    win.update(by_id[jid])
                else:
                    self.progress.pop(jid, None)
                    win.close()
        if self.manager.aria_error:
            self.window.set_banner(self.manager.aria_error)
        elif not media.available():
            self.window.set_banner("yt-dlp isn't installed, so video sites won't work.", "Install")
        else:
            self.window.set_banner(None)
        while True:
            try:
                kind, job = self.manager.events.get_nowait()
            except Exception:
                break
            if not self.settings["notify"]:
                continue
            if kind == "done":
                n = Gio.Notification.new("Download complete")
                n.set_body(job["name"])
                if job["path"]:
                    n.set_default_action_and_target("app.reveal", GLib.Variant("s", job["path"]))
            else:
                n = Gio.Notification.new("Download failed")
                n.set_body(f"{job['name']}: {job['error'][:200]}")
                n.set_default_action("app.show")
            n.set_icon(Gio.ThemedIcon.new(APP_ID))
            self.send_notification(None, n)
        return True

    def _reveal(self, action, param):
        path = param.get_string()
        if os.path.exists(path):
            Gtk.FileLauncher.new(Gio.File.new_for_path(path)).open_containing_folder(None, None, None)

    def _about(self, *_):
        about = Adw.AboutDialog(application_name="Haul", application_icon=APP_ID, version=__version__,
                                developer_name="Haul",
                                comments="A download manager for Ubuntu built on aria2 and yt-dlp.",
                                license_type=Gtk.License.MIT_X11)
        about.add_credit_section("Engines", ["aria2 https://aria2.github.io/",
                                             "yt-dlp https://github.com/yt-dlp/yt-dlp",
                                             "FFmpeg https://ffmpeg.org/"])
        about.add_credit_section("Design ideas from",
                                 ["Prism Download Manager https://github.com/FortoGraphy/prism-download-manager"])
        about.present(self.window)


def main(argv):
    return HaulApp().run(argv)
