// Haul browser extension: captures downloads, adds context menus and finds
// media streams on pages, and hands them to the Haul app via native messaging.
// If Haul declines (robot check, expired link, app missing) the browser keeps
// the download, so nothing is ever lost.

const api = globalThis.browser ?? globalThis.chrome;
const HOST = "dev.haul.host";

const CAPTURE_EXT = new Set(("zip rar 7z tar gz tgz bz2 xz zst iso img dmg exe msi apk deb rpm " +
  "appimage flatpak snap run bin jar mp4 mkv avi mov wmv flv webm m4v mpg mpeg 3gp mp3 m4a aac " +
  "flac wav ogg opus wma pdf epub mobi djvu doc docx xls xlsx ppt pptx odt ods odp torrent").split(" "));
const SKIP_EXT = new Set("html htm php asp aspx jsp json xml txt css js svg png jpg jpeg gif webp ico avif".split(" "));
const CAPTURE_MIME = /^(video|audio)\/|^application\/(zip|x-zip|x-rar|vnd\.rar|x-7z|x-tar|gzip|x-gzip|x-bzip2|x-xz|zstd|x-iso9660|x-msdownload|x-msi|vnd\.android|vnd\.debian|x-rpm|x-redhat|x-executable|pdf|epub|x-bittorrent|java-archive|vnd\.(ms|openxmlformats|oasis))/i;
const BIG = 5 * 1024 * 1024;

const bypass = new Map();   // url -> expiry: downloads we hand back to the browser

async function getSettings() {
  try {
    const s = await api.storage.local.get({ capture: true });
    return s;
  } catch { return { capture: true }; }
}

async function send(msg) {
  try {
    const r = await api.runtime.sendNativeMessage(HOST, msg);
    return r || { ok: false, reason: "no_reply" };
  } catch (e) {
    return { ok: false, reason: "no_app", message: String(e && e.message || e) };
  }
}

async function cookieHeader(url) {
  try {
    const list = await api.cookies.getAll({ url });
    return list.map(c => `${c.name}=${c.value}`).join("; ");
  } catch { return ""; }
}

function extOf(s) {
  try { s = new URL(s).pathname; } catch {}
  const base = decodeURIComponent(s.split("/").pop() || "");
  const i = base.lastIndexOf(".");
  return i > 0 ? base.slice(i + 1).toLowerCase() : "";
}

function shouldCapture(item) {
  const ext = extOf(item.filename || "") || extOf(item.finalUrl || item.url);
  if (SKIP_EXT.has(ext)) return false;
  if (CAPTURE_EXT.has(ext)) return true;
  if (item.mime && CAPTURE_MIME.test(item.mime)) return true;
  const size = item.totalBytes > 0 ? item.totalBytes : item.fileSize;
  return size > BIG || item.mime === "application/octet-stream";
}

async function handOff(url, extra) {
  return send({
    type: "download", url,
    cookies: await cookieHeader(url),
    userAgent: navigator.userAgent,
    ...extra,
  });
}

// ---------- capture downloads ----------
api.downloads.onCreated.addListener(async item => {
  const url = item.finalUrl || item.url;
  if (!/^(https?|ftp):/i.test(url) || item.state !== "in_progress") return;  // blob:, data: stay
  const until = bypass.get(url);
  if (until && until > Date.now()) { bypass.delete(url); return; }
  if (!(await getSettings()).capture || !shouldCapture(item)) return;

  try { await api.downloads.pause(item.id); } catch {}
  const name = (item.filename || "").split(/[\\/]/).pop();
  const r = await handOff(url, {
    source: "capture", referrer: item.referrer || "", filename: name,
    fileSize: item.totalBytes > 0 ? item.totalBytes : 0, mime: item.mime || "",
  });
  if (r.ok) {
    try { await api.downloads.cancel(item.id); } catch {}
    try { await api.downloads.erase({ id: item.id }); } catch {}
  } else {
    try { await api.downloads.resume(item.id); } catch {}
  }
});

// ---------- context menus ----------
function createMenus() {
  api.contextMenus.removeAll(() => {
    api.contextMenus.create({ id: "haul-link", title: "Download with Haul", contexts: ["link"] });
    api.contextMenus.create({ id: "haul-media", title: "Download with Haul", contexts: ["video", "audio", "image"] });
    api.contextMenus.create({ id: "haul-page", title: "Download video from this page with Haul", contexts: ["page", "frame"] });
  });
}
api.runtime.onInstalled.addListener(createMenus);

async function syncPrefs() {
  const r = await send({ type: "ping" });
  if (r.prefs) await api.storage.local.set({ prefQuality: r.prefs.quality, prefFormat: r.prefs.format });
}
api.runtime.onInstalled.addListener(syncPrefs);
api.runtime.onStartup?.addListener(syncPrefs);
api.runtime.onStartup?.addListener(createMenus);

api.contextMenus.onClicked.addListener(async (info, tab) => {
  const page = info.frameUrl || info.pageUrl || tab?.url || "";
  let url = info.menuItemId === "haul-page" ? page : (info.linkUrl || info.srcUrl || page);
  let source = info.menuItemId === "haul-page" ? "page" : "menu";
  if (/^(blob|data|mediasource):/i.test(url)) { url = page; source = "page"; }  // player blob: grab the page
  const r = await handOff(url, { source, referrer: page, title: tab?.title || "" });
  if (!r.ok) notifyFailure(r);
});

function notifyFailure(r) {
  const text = r.reason === "no_app"
    ? "Haul is not reachable. Open Haul → Settings → Browser and press Set up."
    : (r.message || "Haul could not take this download.");
  api.action.setBadgeBackgroundColor?.({ color: "#c01c28" });
  api.action.setBadgeText({ text: "!" });
  api.action.setTitle({ title: "Haul: " + text });
}

// ---------- media stream sniffing ----------
const MEDIA_TYPES = /^(application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml)|audio\/(x-)?mpegurl|video\/(mp4|webm|x-matroska|quicktime|x-flv)|audio\/(mpeg|mp4|aac|ogg|webm|flac))/i;
const SKIP_HOSTS = /(^|\.)(googlevideo\.com|youtube\.com|ytimg\.com|doubleclick\.net|googlesyndication\.com)$/i;

async function tabMedia(tabId) {
  try {
    const k = "t" + tabId;
    return (await api.storage.session.get({ [k]: [] }))[k];
  } catch { return []; }
}

async function setTabMedia(tabId, list) {
  try { await api.storage.session.set({ ["t" + tabId]: list }); } catch {}
  api.action.setBadgeBackgroundColor?.({ color: "#3584e4", tabId });
  api.action.setBadgeText({ text: list.length ? String(list.length) : "", tabId });
}

api.webRequest.onHeadersReceived.addListener(details => {
  if (details.tabId < 0) return;
  let u;
  try { u = new URL(details.url); } catch { return; }
  if (SKIP_HOSTS.test(u.hostname)) return;
  const header = n => (details.responseHeaders || []).find(h => h.name.toLowerCase() === n)?.value || "";
  const type = header("content-type").split(";")[0].trim().toLowerCase();
  const path = u.pathname.toLowerCase();
  const isManifest = /\.(m3u8|mpd)$/.test(path) || /mpegurl|dash\+xml/.test(type);
  if (!isManifest && !MEDIA_TYPES.test(type)) return;
  if (/\.(ts|m4s|aac|m4a|vtt)$/.test(path) && !isManifest) return;  // stream segments
  const range = header("content-range").match(/\/(\d+)/);
  const size = Number(range ? range[1] : header("content-length")) || 0;
  if (!isManifest && size && size < 512 * 1024) return;               // ads, previews, beeps
  const kind = /mpd|dash/.test(path + type) ? "DASH" : isManifest ? "HLS" : type.split("/")[1].toUpperCase();
  (async () => {
    const list = await tabMedia(details.tabId);
    const key = u.origin + u.pathname;
    if (list.some(m => m.key === key)) return;
    // Variant playlists of an HLS master are noise: keep the first manifest per directory.
    if (isManifest && list.some(m => m.manifest && m.dir === key.slice(0, key.lastIndexOf("/")))) return;
    list.push({ key, url: details.url, kind, size, manifest: isManifest,
      dir: key.slice(0, key.lastIndexOf("/")), at: Date.now() });
    await setTabMedia(details.tabId, list.slice(-30));
  })();
}, { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] }, ["responseHeaders"]);

api.tabs.onUpdated.addListener((tabId, change) => {
  if (change.status === "loading" && change.url) setTabMedia(tabId, []);
});
api.tabs.onRemoved.addListener(tabId => {
  try { api.storage.session.remove("t" + tabId); } catch {}
});

// ---------- popup messages ----------
api.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    if (msg.type === "status") {
      const [tab] = await api.tabs.query({ active: true, currentWindow: true });
      const ping = await send({ type: "ping" });
      if (ping.prefs) api.storage.local.set({ prefQuality: ping.prefs.quality, prefFormat: ping.prefs.format });
      reply({ host: ping.reason !== "no_app", running: !!ping.running,
        capture: (await getSettings()).capture,
        videoButton: (await api.storage.local.get({ videoButton: true })).videoButton, media: tab ? await tabMedia(tab.id) : [],
        page: tab?.url || "", title: tab?.title || "" });
      api.action.setBadgeText({ text: "" });
    } else if (msg.type === "setCapture") {
      await api.storage.local.set({ capture: !!msg.value });
      reply({ ok: true });
    } else if (msg.type === "probeVideo") {
      // From the in-page button. In an iframe (embedded player) the frame's own URL is
      // the best bet; otherwise the page. A direct <video src> is the last resort.
      const tab = sender.tab || {};
      const page = tab.url || msg.frameUrl;
      const primary = sender.frameId ? msg.frameUrl : page;
      const alt = [page, msg.frameUrl, /^https?:/i.test(msg.src || "") ? msg.src : ""]
        .filter(u => u && u !== primary);
      const [r, streams] = await Promise.all([
        send({ type: "probe", url: primary, alt, cookies: await cookieHeader(primary),
          userAgent: navigator.userAgent, referrer: page, title: msg.title || tab.title || "" }),
        tab.id != null ? tabMedia(tab.id) : [],
      ]);
      if (r.prefs) api.storage.local.set({ prefQuality: r.prefs.quality, prefFormat: r.prefs.format });
      reply({ ...r, streams: streams.filter(s => s.url !== msg.src || !r.ok) });
    } else if (msg.type === "tabStreams") {
      reply(sender.tab?.id != null ? await tabMedia(sender.tab.id) : []);
    } else if (msg.type === "buttonDownload") {
      const page = sender.tab?.url || "";
      reply(await handOff(msg.url, { source: "button", quality: msg.quality, playlist: !!msg.playlist,
        kind: msg.kind || "", label: msg.label || "", referrer: page, title: msg.title || "" }));
    } else if (msg.type === "setVideoButton") {
      await api.storage.local.set({ videoButton: !!msg.value });
      reply({ ok: true });
    } else if (msg.type === "grab") {
      reply(await handOff(msg.url, { source: msg.source, referrer: msg.referrer || "", title: msg.title || "" }));
    }
  })();
  return true;
});
