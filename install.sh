#!/bin/sh
# Installs Haul for the current user (no root needed).
#   ./install.sh                  everything
#   ./install.sh --skip-ytdlp     don't download yt-dlp now (install it later in Settings → Video)
#   ./install.sh --autostart      also start Haul hidden at login, so unfinished downloads resume
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
CONF=${XDG_CONFIG_HOME:-$HOME/.config}
LAUNCHER=$HOME/.local/bin/haul
SKIP_YTDLP=0
AUTOSTART=0
for a in "$@"; do
  case "$a" in
    --skip-ytdlp) SKIP_YTDLP=1 ;;
    --autostart) AUTOSTART=1 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

say "Checking GTK 4 and libadwaita"
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Adw" 2>/dev/null || {
  echo "Missing: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" >&2; exit 1; }

say "Checking aria2"
if command -v aria2c >/dev/null 2>&1; then
  echo "using system aria2c"
elif [ -x "$DATA/haul/vendor/root/usr/bin/aria2c" ]; then
  echo "using Haul's local aria2c (sudo apt install aria2 gets you security updates)"
else
  echo "aria2 is not installed; unpacking Ubuntu's package locally (no root)"
  mkdir -p "$DATA/haul/vendor/debs"
  (cd "$DATA/haul/vendor/debs" && apt-get download aria2 libaria2-0 >/dev/null)
  for d in "$DATA"/haul/vendor/debs/*.deb; do dpkg -x "$d" "$DATA/haul/vendor/root"; done
  echo "done. For automatic security updates prefer: sudo apt install aria2"
fi
command -v ffmpeg >/dev/null 2>&1 || echo "note: ffmpeg is missing; merging video+audio needs it: sudo apt install ffmpeg"

say "Installing the launcher, icon and menu entry"
mkdir -p "$HOME/.local/bin" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
chmod +x "$HERE/bin/haul"
ln -sf "$HERE/bin/haul" "$LAUNCHER"
cp "$HERE/data/dev.haul.Haul.svg" "$DATA/icons/hicolor/scalable/apps/"
sed "s#@LAUNCHER@#$LAUNCHER#" "$HERE/data/dev.haul.Haul.desktop" > "$DATA/applications/dev.haul.Haul.desktop"
update-desktop-database "$DATA/applications" 2>/dev/null || true
gtk-update-icon-cache -q "$DATA/icons/hicolor" 2>/dev/null || true
xdg-mime default dev.haul.Haul.desktop x-scheme-handler/magnet application/x-bittorrent || true

if [ "$AUTOSTART" = 1 ]; then
  mkdir -p "$CONF/autostart"
  sed -e "s#@LAUNCHER@ %U#$LAUNCHER --background#" "$HERE/data/dev.haul.Haul.desktop" > "$CONF/autostart/dev.haul.Haul.desktop"
fi

say "Registering the browser extension host"
"$LAUNCHER" --setup-browsers

if [ "$SKIP_YTDLP" = 0 ]; then
  say "Getting yt-dlp (official release, checksum verified)"
  "$LAUNCHER" --install-ytdlp || echo "yt-dlp failed to install; retry later from Settings → Video"
fi

say "Done. Open Haul from the app grid, or run: haul"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "(add ~/.local/bin to PATH to use the 'haul' command)";; esac
