"""Work out what a link is before anything is downloaded.

One `GET Range: bytes=0-` tells us the size, the name, whether ranges (and so
multi-connection and resume) work, and whether the server answered with a web
page instead of the file. Web pages go to yt-dlp, which recognises video on
~1800 sites and in generic embeds. Bot checks (Cloudflare and friends) are
reported as such instead of saving a challenge page as the user's file.
"""

import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import Message

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/146.0.0.0 Safari/537.36")

# Sites that are always pages around a video: skip the HTTP probe and ask yt-dlp directly.
VIDEO_HOSTS = re.compile(
    r"(^|\.)(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|twitch\.tv|tiktok\.com|"
    r"instagram\.com|facebook\.com|fb\.watch|x\.com|twitter\.com|reddit\.com|v\.redd\.it|"
    r"soundcloud\.com|bandcamp\.com|bilibili\.com|vk\.com|vkvideo\.ru|rumble\.com|"
    r"odysee\.com|streamable\.com|kick\.com|mixcloud\.com|nicovideo\.jp)$", re.I)

HLS_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl",
             "audio/x-mpegurl"}
DASH_TYPES = {"application/dash+xml"}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
CHALLENGE_MARKERS = ("challenge-platform", "cf-chl", "just a moment", "attention required",
                     "captcha", "ddos-guard", "are you a robot", "verify you are human",
                     "_incapsula_", "perimeterx", "px-captcha", "akamai bot")


@dataclass
class Probe:
    kind: str                       # http | media | torrent | error
    url: str
    final_url: str = ""
    name: str = ""
    size: int = 0                   # 0 = unknown
    mime: str = ""
    resumable: bool = False
    error: str = ""                 # browser_check | web_page | not_found | auth | forbidden | network | unsupported | no_ytdlp | http
    message: str = ""
    media: dict = field(default_factory=dict)


def browser_headers(headers):
    h = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    for k, v in (headers or {}).items():
        if v:
            h[k] = v
    return h


def sanitize(name):
    name = name.replace("/", "_").replace("\0", "").strip().strip(".") or "download"
    name = re.sub(r"[\x00-\x1f]", "", name)
    while len(name.encode()) > 200:
        stem, ext = os.path.splitext(name)
        name = stem[:-8] + ext if len(stem) > 8 else name[:100]
    return name


def name_from(url, disposition="", mime=""):
    name = ""
    if disposition:
        m = Message()
        m["content-disposition"] = disposition
        try:
            name = m.get_filename() or ""
        except Exception:
            name = ""
    if not name:
        path = urllib.parse.urlparse(url).path
        name = urllib.parse.unquote(os.path.basename(path.rstrip("/")))
    if name and "." not in name and mime and mime != "application/octet-stream":
        ext = mimetypes.guess_extension(mime) or ""
        name += ext
    return sanitize(name or "download")


def _looks_like_challenge(body):
    text = body[:65536].decode("utf-8", "replace").lower()
    return any(m in text for m in CHALLENGE_MARKERS)


def _is_challenge(status, headers, body):
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    return status in (403, 429, 503) and _looks_like_challenge(body)


class _NoAuthPrompt(urllib.request.HTTPRedirectHandler):
    max_redirections = 10


def http_probe(url, headers=None, timeout=20):
    opener = urllib.request.build_opener(_NoAuthPrompt)
    req = urllib.request.Request(url, headers={**browser_headers(headers), "Range": "bytes=0-"})
    try:
        resp = opener.open(req, timeout=timeout)
        status = resp.status
    except urllib.error.HTTPError as e:
        resp, status = e, e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        return Probe("error", url, error="network", message=f"Could not reach the server ({reason}).")
    with resp:
        hdr = {k.lower(): v for k, v in resp.headers.items()}
        final = resp.geturl() or url
        mime = hdr.get("content-type", "").split(";")[0].strip().lower()
        body = b""
        if mime in HTML_TYPES or status >= 400 or mime.startswith("text/"):
            try:
                body = resp.read(65536)
            except OSError:
                pass
    if _is_challenge(status, hdr, body):
        return Probe("error", url, final, error="browser_check",
                     message="This site checks for robots and only lets a web browser download it.")
    if status in (404, 410):
        return Probe("error", url, final, error="not_found",
                     message="The server says this file does not exist (HTTP %d)." % status)
    if status == 401:
        return Probe("error", url, final, error="auth",
                     message="The server wants a login (HTTP 401).")
    if status == 403:
        return Probe("error", url, final, error="forbidden",
                     message="The server refused the request (HTTP 403). The link may have "
                             "expired or only work in the browser that opened it.")
    if status >= 400:
        return Probe("error", url, final, error="http",
                     message=f"The server answered with an error (HTTP {status}).")

    size, resumable = 0, False
    if status == 206:
        m = re.search(r"/(\d+)\s*$", hdr.get("content-range", ""))
        size = int(m.group(1)) if m else 0
        resumable = True
    else:
        size = int(hdr.get("content-length", "0") or 0) if hdr.get("content-length", "").isdigit() else 0
        resumable = hdr.get("accept-ranges", "").lower() == "bytes"
    name = name_from(final, hdr.get("content-disposition", ""), mime)
    path = urllib.parse.urlparse(final).path.lower()

    if mime in HTML_TYPES and "attachment" not in hdr.get("content-disposition", "").lower():
        # A 200 page may still be a bot check; that is decided once yt-dlp finds nothing on it.
        return Probe("media", url, final, name, mime=mime,
                     error="challenge_hint" if _looks_like_challenge(body) else "web_page")
    if mime in HLS_TYPES or mime in DASH_TYPES or path.endswith((".m3u8", ".mpd")):
        return Probe("media", url, final, name, mime=mime)
    if mime == "application/x-bittorrent" or path.endswith(".torrent"):
        return Probe("torrent", url, final, name, size, mime)
    return Probe("http", url, final, name, size, mime, resumable)


def magnet_name(uri):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
    return sanitize(q.get("dn", ["Magnet link"])[0])


def classify(url, headers=None, cookies_browser="", fast=False):
    """Full classification. `fast` skips the yt-dlp step (used for browser hand-offs)."""
    from . import media

    url = url.strip()
    if url.lower().startswith("magnet:"):
        return Probe("torrent", url, url, magnet_name(url))
    if os.path.isfile(url) and url.lower().endswith(".torrent"):
        return Probe("torrent", url, url, sanitize(os.path.basename(url)[:-8]))
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme in ("ftp", "sftp"):
        return Probe("http", url, url, name_from(url), resumable=True)
    if scheme not in ("http", "https"):
        return Probe("error", url, error="unsupported",
                     message="Haul downloads http, https, ftp, sftp, magnet and .torrent links.")

    host = urllib.parse.urlparse(url).hostname or ""
    if VIDEO_HOSTS.search(host):
        p = Probe("media", url, url)
    else:
        p = http_probe(url, headers)
        if p.kind != "media":
            return p
    if fast:
        return p
    if not media.available():
        return Probe("error", url, p.final_url, error="no_ytdlp",
                     message="This is a video page. Install yt-dlp in Settings → Video to download it.")
    info = media.inspect(p.final_url or url, headers, cookies_browser)
    if info.get("error"):
        if p.error == "challenge_hint":
            return Probe("error", url, p.final_url, error="browser_check",
                         message="This site checks for robots and only lets a web browser download it.")
        if p.error == "web_page":
            return Probe("error", url, p.final_url, error="web_page",
                         message="This link opens a web page, and no video or file was found on it. "
                                 f"({info['error']})")
        return Probe("error", url, p.final_url, error="media", message=info["error"])
    p.kind, p.error, p.media = "media", "", info
    p.name = info.get("title") or p.name
    p.size = info.get("size") or 0
    return p
