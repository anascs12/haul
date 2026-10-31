# Haul

A download manager for Ubuntu, built on proven engines:

| What | Engine | You get |
|---|---|---|
| Files (http, https, ftp, sftp) | **aria2** | up to 16 connections per file, resume after pause, crash or reboot |
| Torrents and magnet links | **aria2** | DHT, peer exchange, local discovery; optional seeding |
| Video and audio on ~1800 sites, HLS/DASH streams | **yt-dlp** + ffmpeg | quality picker, playlists, MP3 extraction |

Design ideas (robot-check detection, crash-safe part files, per-browser native
messaging paths) come from [Prism](https://github.com/FortoGraphy/prism-download-manager) (MIT).

## Install

```sh
git clone https://github.com/anascs12/haul.git ~/haul
cd ~/haul
./install.sh            # add --autostart to resume unfinished downloads at login
```

It needs no root. It installs `~/.local/bin/haul`, a menu entry, makes Haul
the handler for magnet links and .torrent files, registers the browser host,
and fetches the official yt-dlp release (checksum verified).
`sudo apt install aria2` is better than the bundled local copy because it gets security updates.

## Browser extension

1. Open Haul once; it registers the browser link by itself (Settings → Browser shows the status).
2. Chrome / Brave / Edge / Vivaldi / Opera / Chromium / Helium: open the Extensions page,
   turn on **Developer mode**, **Load unpacked**, pick `~/.local/share/haul/extension/chrome`.
3. Firefox / Zen: `about:debugging` → *This Firefox* → *Load Temporary Add-on* →
   `~/.local/share/haul/extension/firefox/manifest.json`. Regular Firefox only allows this
   until it restarts, because the add-on is unsigned.
4. Restart the browser.

The extension then:
* puts a **Download** button at the top-right of any video when you hover it or press play
  (also in fullscreen and embedded players). The menu opens instantly: on YouTube it is
  built from the stream list the player already loaded (every resolution with its size);
  elsewhere it lists the video file and the HLS/DASH streams the page loaded, while
  yt-dlp looks for more in the background. Your preferred quality is listed first;
* asks **where to save** in a small window. Haul learns the folder you use for each type
  (Video, Music, Compressed, Programs, Documents, Images, Other), preselects it next time,
  and can save a type there without asking (**Settings → Folders**);
* shows a small **progress window** per download with a segment map of the pieces each
  connection has delivered (plus speed, time left, Pause, Cancel, then Open / Open Folder);
* adds a **Download** pill (bottom-right) to files opened in **Google Classroom** and Drive,
  to **Google Docs / Sheets / Slides** (export as PDF, Word, Excel, CSV, PowerPoint, text)
  and to PDFs, images, audio and video opened directly in a tab;
* hands browser downloads to Haul (archives, ISOs, programs, video, audio, documents, anything
  > 5 MB). If Haul can't fetch a file itself (robot check, single-use link, POST download),
  the browser keeps it;
* adds **Download with Haul** to the right-click menu for links, images and videos.

Haul sets the browser link up by itself on every start (and restarts itself after an
update when the browser needs a newer version), starts hidden at login so unfinished
downloads resume, and keeps yt-dlp up to date daily.

## Using it

* **Ctrl+V** in the window adds the link(s) on the clipboard; **Ctrl+N** opens Add.
* Drop links or .torrent files on the window.
* `haul URL...` from a terminal; `haul --background` starts it without a window.
* Right-click a download to copy its link, restart it, or delete the file.

## What makes it reliable

* Data goes to `name.haulpart` and is renamed only when complete. If the
  `.aria2` control file is missing, the part file is discarded, never trusted.
* Haul's own SQLite database is the source of truth. If aria2 dies it is
  restarted and every job is re-added under the same ID, then resumes.
* Network errors retry with backoff for about 35 minutes (the budget resets
  whenever bytes arrive). Servers that refuse extra connections get fewer
  connections instead of a failure. 404, login required and disk full fail at once
  with a clear message.
* A web page or robot-check page is never saved as your file.
* yt-dlp updates itself daily. YouTube sign-in cookies that get rejected are
  retried without cookies.
* Video downloads start from the page info read while you were choosing
  (`--load-info-json`), which cuts the start-up wait from ~12 s to ~3 s; stale info is
  re-read automatically.

## Limits (honest list)

* DRM-protected streams (Netflix, Disney+, Spotify, ...) can't be downloaded, by design.
* Sites behind a login need **Settings → Video → Use cookies from browser**, or the extension
  (it forwards your cookies).
* Sites that only let a real browser in (Cloudflare challenges) stay with the browser.

## Tests

```sh
python3 tests/test_engine.py   # local misbehaving server: resume, crash, drops, limits, 404...
python3 tests/test_media.py    # yt-dlp job handling with a stand-in yt-dlp
```

## Files

* `~/.local/share/haul/`: database, logs (`haul.log`, `aria2.log`), yt-dlp, extension
* `~/.config/haul/settings.json`: settings
