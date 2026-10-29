// Haul in-page video button: a "Download" pill at the top-right of a video when you
// hover it or press play. Clicking it lists the qualities Haul found; picking one
// starts the download in Haul right away.
(() => {
  if (window.__haulVideoButton) return;
  window.__haulVideoButton = true;
  const api = globalThis.browser ?? globalThis.chrome;
  const MIN_W = 240, MIN_H = 135, INSET = 12;

  let enabled = true;
  api.storage.local.get({ videoButton: true }).then(s => { enabled = s.videoButton; }).catch(() => {});
  api.storage.onChanged.addListener(c => {
    if (c.videoButton) { enabled = c.videoButton.newValue; if (!enabled) hide(true); }
  });

  let host, root, wrap, btn, label, menu;
  let current = null, pointer = { x: -1, y: -1 }, hideTimer = 0, raf = 0, menuOpen = false;
  let probeKey = "", probeResult = null, probing = null;

  const ICON = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor"
    d="M7.25 1.5h1.5v7.19l2.47-2.47 1.06 1.06L8 11.56 3.72 7.28l1.06-1.06 2.47 2.47zM2.5 12.5h11V14h-11z"/></svg>`;

  function build() {
    host = document.createElement("haul-video-button");
    host.style.cssText = "all:initial;position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;";
    root = host.attachShadow({ mode: "closed" });
    root.innerHTML = `
<style>
  :host { all: initial; }
  .wrap { position: fixed; display: none; flex-direction: column; align-items: flex-end; gap: 6px;
    font: 500 13px/1.3 system-ui, "Cantarell", "Ubuntu", "Segoe UI", sans-serif; color: #fff;
    opacity: 0; transform: translateY(-4px); transition: opacity .15s ease, transform .15s ease; }
  .wrap.show { opacity: 1; transform: none; }
  .wrap.page { flex-direction: column-reverse; }
  .btn { all: unset; display: inline-flex; align-items: center; gap: 7px; cursor: pointer; white-space: nowrap;
    padding: 6px 12px 6px 7px; border-radius: 999px; background: rgba(18,18,20,.78);
    backdrop-filter: blur(12px) saturate(1.4); -webkit-backdrop-filter: blur(12px) saturate(1.4);
    box-shadow: 0 2px 10px rgba(0,0,0,.35), inset 0 0 0 1px rgba(255,255,255,.12); }
  .btn:hover { background: rgba(32,32,36,.9); }
  .btn:focus-visible { outline: 2px solid #62a0ea; outline-offset: 2px; }
  .dot { display: grid; place-items: center; width: 22px; height: 22px; border-radius: 50%;
    background: #3584e4; color: #fff; flex: none; }
  .btn.ok .dot { background: #2ec27e; }
  .menu { min-width: 250px; max-width: 320px; max-height: min(360px, 60vh); overflow-y: auto; padding: 6px;
    border-radius: 14px; background: rgba(18,18,20,.92); backdrop-filter: blur(16px) saturate(1.4);
    -webkit-backdrop-filter: blur(16px) saturate(1.4);
    box-shadow: 0 8px 28px rgba(0,0,0,.45), inset 0 0 0 1px rgba(255,255,255,.1); }
  .menu[hidden] { display: none; }
  .title { padding: 6px 8px 8px; font-weight: 600; font-size: 12px; color: rgba(255,255,255,.7);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; border-bottom: 1px solid rgba(255,255,255,.08);
    margin-bottom: 4px; }
  .item { all: unset; box-sizing: border-box; display: flex; align-items: center; gap: 10px; width: 100%;
    padding: 8px 10px; border-radius: 9px; cursor: pointer; }
  .item:hover, .item:focus-visible { background: rgba(255,255,255,.1); }
  .item .l { font-weight: 600; min-width: 64px; }
  .item .d { flex: 1; text-align: right; color: rgba(255,255,255,.62); font-size: 12px;
    font-feature-settings: "tnum"; }
  .chip { font-size: 10px; font-weight: 700; letter-spacing: .03em; text-transform: uppercase;
    color: #99c1f1; background: rgba(53,132,228,.22); padding: 2px 6px; border-radius: 99px; }
  .note { padding: 10px; color: rgba(255,255,255,.75); font-size: 12px; line-height: 1.45; }
  .spin { display: inline-block; width: 12px; height: 12px; border-radius: 50%; vertical-align: -2px;
    margin-right: 8px; border: 2px solid rgba(255,255,255,.25); border-top-color: #fff;
    animation: r .8s linear infinite; }
  @keyframes r { to { transform: rotate(360deg); } }
  .sep { height: 1px; background: rgba(255,255,255,.08); margin: 4px 6px; }
</style>
<div class="wrap" part="wrap">
  <button class="btn" type="button" title="Download with Haul"><span class="dot">${ICON}</span><span class="lbl">Download</span></button>
  <div class="menu" role="menu" hidden></div>
</div>`;
    wrap = root.querySelector(".wrap");
    btn = root.querySelector(".btn");
    label = root.querySelector(".lbl");
    menu = root.querySelector(".menu");
    btn.addEventListener("click", e => {
      if (!e.isTrusted) return;           // pages can't press it for you
      e.preventDefault(); e.stopPropagation();
      menuOpen ? closeMenu() : openMenu();
    });
    btn.addEventListener("pointerenter", () => prefetch());  // hoisted below
    for (const ev of ["pointerdown", "mousedown", "mouseup", "dblclick", "contextmenu"]) {
      wrap.addEventListener(ev, e => e.stopPropagation());   // keep the player from pausing
    }
    wrap.addEventListener("pointerenter", () => clearTimeout(hideTimer));
    wrap.addEventListener("pointerleave", () => scheduleHide());
  }

  function mount() {
    if (!host) build();
    const fs = document.fullscreenElement;
    const parent = fs && fs.tagName !== "VIDEO" ? fs : document.documentElement;
    if (host.parentNode !== parent) parent.appendChild(host);
  }

  function visibleRect(v) {
    const r = v.getBoundingClientRect();
    if (r.width < MIN_W || r.height < MIN_H) return null;
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) return null;
    const style = getComputedStyle(v);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) return null;
    return r;
  }

  function placePage() {
    wrap.style.top = "auto"; wrap.style.left = "auto";
    wrap.style.right = "16px"; wrap.style.bottom = "16px";
  }

  function place() {
    if (current === "page") { placePage(); return; }
    if (!current || !current.isConnected) { hide(true); return; }
    const r = visibleRect(current);
    if (!r) { if (!menuOpen) hide(true); return; }
    const top = Math.max(r.top, 0) + INSET;
    const right = Math.min(r.right, innerWidth) - INSET;
    wrap.style.top = top + "px";
    wrap.style.bottom = "auto";
    wrap.style.left = "auto";
    wrap.style.right = Math.max(innerWidth - right, 4) + "px";
    raf = requestAnimationFrame(place);
  }

  function show(video, ms) {
    if (!enabled) return;
    if (current !== video) {
      if (menuOpen) closeMenu();
      current = video;
    }
    mount();
    wrap.classList.toggle("page", video === "page");
    wrap.style.display = "flex";
    requestAnimationFrame(() => wrap.classList.add("show"));
    cancelAnimationFrame(raf);
    place();
    clearTimeout(hideTimer);
    if (ms) hideTimer = setTimeout(() => scheduleHide(), ms);
  }

  function hide(now) {
    if (!wrap || menuOpen && !now) return;
    if (pageFile && enabled && !(now && !enabled)) {
      if (current !== "page") { if (menuOpen) closeMenu(); current = null; show("page"); }
      return;
    }
    if (now && menuOpen) closeMenu();
    wrap.classList.remove("show");
    cancelAnimationFrame(raf);
    setTimeout(() => { if (!wrap.classList.contains("show")) wrap.style.display = "none"; }, 160);
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (menuOpen || current === "page" || (current instanceof Element && inside(current))) return;
      hide();
    }, 1800);
  }

  function inside(v) {
    const r = v.getBoundingClientRect();
    return pointer.x >= r.left && pointer.x <= r.right && pointer.y >= r.top && pointer.y <= r.bottom;
  }

  function videoAtPointer() {
    let best = null, area = 0;
    for (const v of document.querySelectorAll("video")) {
      const r = visibleRect(v);
      if (!r || !inside(v)) continue;
      if (r.width * r.height > area) { best = v; area = r.width * r.height; }
    }
    return best;
  }

  let moveQueued = false;
  document.addEventListener("pointermove", e => {
    pointer = { x: e.clientX, y: e.clientY };
    if (moveQueued) return;
    moveQueued = true;
    setTimeout(() => {
      moveQueued = false;
      const v = videoAtPointer();
      if (v) show(v);
      else if (current && wrap && wrap.style.display !== "none") scheduleHide();
    }, 120);
  }, { passive: true, capture: true });

  document.addEventListener("play", e => {
    if (e.target instanceof HTMLVideoElement && visibleRect(e.target)) show(e.target, 4000);
  }, true);

  document.addEventListener("fullscreenchange", () => { if (host && current) { mount(); show(current, 3000); } });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && menuOpen) closeMenu(); }, true);
  document.addEventListener("pointerdown", e => {
    if (menuOpen && !e.composedPath().includes(host)) closeMenu();
  }, true);

  // ---------- files opened in the browser (Classroom / Drive / Docs / PDFs ...) ----------
  let pageFile = null;

  function fileFromUrl(href) {
    let u;
    try { u = new URL(href, location.href); } catch { return null; }
    const host = u.hostname, q = u.searchParams;
    const ID = /^[\w-]{20,}$/;
    if (host === "drive.google.com" || host === "drive.usercontent.google.com") {
      const m = u.pathname.match(/\/file\/(?:u\/\d+\/)?d\/([\w-]{20,})/);
      const id = m ? m[1] : [q.get("id"), q.get("srcid"), q.get("docid")].find(x => x && ID.test(x));
      if (id) return { type: "drive", id };
    }
    if (host === "docs.google.com") {
      const m = u.pathname.match(/^\/(?:a\/[^/]+\/)?(document|spreadsheets|presentation)\/d\/([\w-]{20,})/);
      if (m) return { type: m[1], id: m[2] };
      const id = [q.get("srcid"), q.get("id")].find(x => x && ID.test(x));
      if (u.pathname.startsWith("/viewer") && id) return { type: "drive", id };
    }
    return null;
  }

  // Classroom opens attachments in a viewer inside its own page, so the file is
  // recognised from the attachment link you click, and forgotten when the viewer closes.
  const IS_CLASSROOM = location.hostname === "classroom.google.com" && window === top;
  let classroomFile = null;
  let lastPress = 0;
  function openClassroomFile(f, name) {
    if (classroomFile && classroomFile.id === f.id) return;
    classroomFile = { ...f, name: (name || "").replace(/^((Attachment|PowerPoint|PDF|Word|Excel|Video|Image|Google \w+):\s*)+/i, "").slice(0, 120) };
    console.debug("[Haul] Classroom file opened:", classroomFile.id, classroomFile.name);
    setTimeout(refreshPageFile, 200);
  }
  if (IS_CLASSROOM) {
    // window + capture + pointerdown: runs before Classroom's own handlers can stop the event
    const onPress = e => {
      if (!e.isTrusted || (host && e.composedPath().includes(host))) return;
      lastPress = Date.now();
      const path = e.composedPath();
      const a = path.find(n => n.tagName === "A" && n.href);
      const f = a && fileFromUrl(a.href);
      if (f) {
        openClassroomFile(f, (a.getAttribute("aria-label") || a.getAttribute("title") || a.textContent || "").trim());
        return;
      }
      // the viewer's close button
      if (e.type === "click" && classroomFile &&
          path.some(n => n.getAttribute && /^(close|back)\b/i.test(n.getAttribute("aria-label") || ""))) {
        classroomFile = null; setTimeout(refreshPageFile, 50);
      }
    };
    for (const type of ["pointerdown", "click"]) window.addEventListener(type, onPress, true);
    window.addEventListener("keydown", e => { if (e.isTrusted && e.key === "Enter") lastPress = Date.now(); }, true);
    // Backup: the background script sees the Drive requests the viewer makes for the file.
    api.runtime.onMessage.addListener(msg => {
      if (msg && msg.type === "driveFile" && Date.now() - lastPress < 10000) openClassroomFile({ type: "drive", id: msg.id }, "");
    });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && classroomFile && !menuOpen) { classroomFile = null; setTimeout(refreshPageFile, 50); }
    }, true);
    // a link that opened in a new tab (Docs, Slides) isn't shown here
    document.addEventListener("visibilitychange", () => {
      if (document.hidden && classroomFile) { classroomFile = null; refreshPageFile(); }
    });
    let lastUrl = location.href;
    setInterval(() => { if (location.href !== lastUrl) { lastUrl = location.href; classroomFile = null; refreshPageFile(); } }, 700);
  }

  function detectPageFile() {
    if (IS_CLASSROOM) return classroomFile;
    const f = fileFromUrl(location.href);
    if (f) return f;
    const ct = (document.contentType || "").toLowerCase();
    if (/^(application\/pdf|image\/|video\/|audio\/|application\/(zip|octet-stream|x-))/.test(ct)
        && /^(https?|ftp):/.test(location.protocol)) return { type: "direct", ct };
    return null;
  }

  function pageView() {
    const f = pageFile, id = f.id;
    const tidy = t => t.replace(/ - Google (Drive|Docs|Sheets|Slides)$/, "");
    const title = f.name || (IS_CLASSROOM ? "File from Classroom" : tidy(document.title || ""));
    const one = (label, detail, url) => ({ id: label, label, detail, url, kind: "http" });
    let options;
    if (f.type === "drive") {
      options = [one("Original file", "from Google Drive",
        `https://drive.usercontent.google.com/download?id=${id}&export=download&confirm=t`)];
    } else if (f.type === "document") {
      const b = `https://docs.google.com/document/d/${id}/export?format=`;
      options = [one("PDF", "document", b + "pdf"), one("Word", "DOCX", b + "docx"), one("Text", "TXT", b + "txt")];
    } else if (f.type === "spreadsheets") {
      const b = `https://docs.google.com/spreadsheets/d/${id}/export?format=`;
      options = [one("Excel", "XLSX", b + "xlsx"), one("PDF", "document", b + "pdf"), one("CSV", "this sheet", b + "csv")];
    } else if (f.type === "presentation") {
      const b = `https://docs.google.com/presentation/d/${id}/export/`;
      options = [one("PowerPoint", "PPTX", b + "pptx"), one("PDF", "document", b + "pdf")];
    } else {
      options = [one("This file", (f.ct.split("/")[1] || "file").toUpperCase(), location.href)];
    }
    return { ok: true, kind: "http", url: options[0].url, title, options };
  }

  function refreshPageFile() {
    const f = detectPageFile();
    const same = f && pageFile && f.type === pageFile.type && f.id === pageFile.id;
    if (same || (!f && !pageFile)) return;
    pageFile = f;
    if (!enabled) return;
    if (pageFile && innerWidth >= 280 && innerHeight >= 160) {
      if (!(current instanceof Element)) { if (menuOpen) closeMenu(); current = null; show("page"); }
    } else if (current === "page") {
      pageFile = null; current = null; hide(true);
    }
  }
  setTimeout(refreshPageFile, 700);
  setInterval(refreshPageFile, 1500);   // Drive and Docs switch files without reloading

  // ---------- probing & menu ----------
  // The menu opens instantly from what the page already has (YouTube's own stream list,
  // the <video> file, streams the page loaded). yt-dlp runs in the background for more.
  const IS_YT = /(^|\.)youtube(-nocookie)?\.com$/.test(location.hostname);
  let prefs = { prefQuality: "best", prefFormat: "mp4" };
  api.storage.local.get(prefs).then(p => { prefs = p; }).catch(() => {});

  function key() { return location.href + "|" + (current instanceof Element ? current.currentSrc : ""); }

  function request() {
    const k = key();
    if (probeKey === k && (probeResult || probing)) return probing || Promise.resolve(probeResult);
    probeKey = k; probeResult = null;
    const src = current instanceof Element ? (current.currentSrc || current.src || "") : "";
    const ask = () => api.runtime.sendMessage({ type: "probeVideo", frameUrl: location.href, src, title: document.title })
      .catch(e => ({ ok: false, reason: "no_app", message: String(e) }));
    probing = ask()
      // Haul may still be starting or restarting after an update: try once more.
      .then(r => r && r.reason === "no_app" ? new Promise(res => setTimeout(res, 2500)).then(ask) : r)
      .then(r => {
        probing = null;
        if (probeKey === k && r && r.ok) probeResult = r;
        if (r && r.prefs) { prefs = { prefQuality: r.prefs.quality, prefFormat: r.prefs.format }; }
        return r;
      });
    return probing;
  }

  // Start yt-dlp early (on play, or hovering the button) so it is usually done before a click.
  let prefetchTimer = 0;
  function prefetch(delay = 0) {
    if (current === "page" || probeResult || probing) return;
    clearTimeout(prefetchTimer);
    prefetchTimer = setTimeout(() => { if (!probeResult && !probing) request(); }, delay);
  }
  document.addEventListener("play", e => {
    if (e.target === current && enabled && !IS_YT) prefetch(1200);
  }, true);

  function ytData() {
    return new Promise(res => {
      const id = Math.random().toString(36).slice(2);
      const on = e => {
        if (e.source === window && e.data && e.data.__haul === "yt" && e.data.id === id) {
          removeEventListener("message", on); res(e.data.data);
        }
      };
      addEventListener("message", on);
      window.postMessage({ __haul: "yt-req", id }, "*");
      setTimeout(() => { removeEventListener("message", on); res(null); }, 400);
    });
  }

  function human(n) {
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < 3) { n /= 1024; i++; }
    return "~" + n.toFixed(i === 3 ? 1 : 0) + " " + u[i];
  }

  const STD = [4320, 2160, 1440, 1080, 720, 480, 360, 240, 144];
  function fromYouTube(d) {
    const mp4 = prefs.prefFormat !== "best";
    const audio = d.audio.slice().sort((a, b) => b.bitrate - a.bitrate);
    const audioSize = (audio.find(a => a.mp4) || audio[0] || {}).size || 0;
    const byH = new Map();
    for (const v of d.video) {
      const h = STD.find(s => v.height >= s * 0.9) || v.height;
      const cur = byH.get(h);
      const better = !cur || (mp4 && v.mp4 && !cur.mp4) || (v.mp4 === cur.mp4 && v.size > cur.size);
      if (better) byH.set(h, v);
    }
    const options = [...byH.keys()].sort((a, b) => b - a).map(h => {
      const v = byH.get(h);
      const tag = h === 2160 ? " 4K" : h >= 720 ? " HD" : "";
      const size = v.size + (v.muxed ? 0 : audioSize);
      return { id: "h" + h, label: h + "p" + tag, detail: [mp4 ? "MP4" : "Best format", human(size)].filter(Boolean).join(" · ") };
    });
    options.push({ id: "audio_mp3", label: "Audio", detail: ["MP3", human(audioSize)].filter(Boolean).join(" · ") });
    options.push({ id: "audio_best", label: "Audio", detail: "original format (M4A/Opus)" });
    const url = new URL(location.href);
    const page = url.pathname.startsWith("/shorts/") ? location.href
      : "https://www.youtube.com/watch?v=" + d.id;
    return { ok: true, kind: "media", url: page, title: d.title, options, instant: true };
  }

  function directFile() {
    const src = current instanceof Element ? (current.currentSrc || current.src || "") : "";
    if (!/^https?:/i.test(src)) return null;
    const name = (() => { try { return decodeURIComponent(new URL(src).pathname.split("/").pop()); } catch { return ""; } })();
    const ext = (name.match(/\.(\w{2,4})$/) || [])[1] || "video";
    return { id: "file", label: "This video", detail: ext.toUpperCase() + " file", url: src, kind: "http" };
  }

  function preferredId(options) {
    const ids = options.map(o => o.id);
    const want = prefs.prefQuality || "best";
    if (ids.includes(want)) return want;
    const cap = want.startsWith("h") ? Number(want.slice(1)) : Infinity;
    return ids.find(i => i.startsWith("h") && Number(i.slice(1)) <= cap) || ids[0];
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  let view = null;   // what the open menu shows

  async function openMenu() {
    menuOpen = true;
    clearTimeout(hideTimer);
    menu.hidden = false;
    if (current === "page") {
      view = { main: pageView(), direct: null, streams: [], waiting: false };
      render();
      return;
    }
    api.storage.local.get(prefs).then(p => { prefs = p; }).catch(() => {});
    const yt = IS_YT ? await ytData() : null;
    const streams = await api.runtime.sendMessage({ type: "tabStreams" }).catch(() => []) || [];
    view = { main: yt && yt.video.length && !yt.live ? fromYouTube(yt) : (probeResult || null),
             direct: IS_YT ? null : directFile(), streams, waiting: false };
    if (view.main && view.main.instant) request();   // let Haul read the page now: the download then starts fast
    if (!view.main) {
      view.waiting = true;
      request().then(r => {
        if (!view) return;
        view.waiting = false;
        if (r && r.ok) view.main = r; else view.error = r;
        if (menuOpen) render();
      });
    }
    render();
  }

  function render() {
    menu.replaceChildren();
    const v = view, m = v.main;
    const title = (m && m.title) || document.title;
    if (title) menu.append(el("div", "title", title));
    let any = false;
    if (m && m.ok) {
      const pref = current === "page" ? null : preferredId(m.options);
      const opts = m.options.slice().sort((a, b) => (b.id === pref) - (a.id === pref));
      for (const o of opts) {
        menu.append(item(o.label, o.detail, o.id === pref,
          () => send({ url: o.url || m.url, kind: o.kind || m.kind, quality: o.id, playlist: !!m.playlist,
            title: o.url ? "" : title,
            label: [o.label, o.detail].filter(Boolean).join(" · ") })));
        any = true;
      }
    }
    const extra = [];
    if (v.direct && !(m && m.kind === "http")) extra.push(v.direct);
    for (const s of (v.streams || []).slice(-5).reverse()) {
      if (v.direct && s.url === v.direct.url) continue;
      const manifest = s.kind === "HLS" || s.kind === "DASH";
      extra.push({ id: "stream", label: manifest ? `${s.kind} stream` : `${s.kind} file`,
        detail: [nameOf(s.url), s.size ? human(s.size) : ""].filter(Boolean).join(" · "),
        url: s.url, kind: manifest ? "media" : "http" });
    }
    if (extra.length && any) menu.append(el("div", "sep"));
    for (const x of extra) {
      menu.append(item(x.label, x.detail, false,
        () => send({ url: x.url, kind: x.kind, quality: "best", title, label: x.label })));
      any = true;
    }
    if (v.waiting) {
      const note = el("div", "note");
      note.append(el("span", "spin"), any ? "Looking for more qualities…" : "Finding download options…");
      menu.append(note);
    } else if (!any) {
      const r = v.error || {};
      menu.append(el("div", "note", r.reason === "no_app"
        ? "Couldn't reach Haul (" + (r.message || "not running") + "). Open Haul once from the app grid; "
          + "after that it connects by itself."
        : (r.message || "Haul couldn't find a downloadable video here.")));
    }
  }

  function item(l, d, preferred, onClick) {
    const b = el("button", "item");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.append(el("span", "l", l));
    if (preferred) b.append(el("span", "chip", "Preferred"));
    b.append(el("span", "d", d || ""));
    b.addEventListener("click", e => { if (e.isTrusted) { e.stopPropagation(); onClick(); } });
    return b;
  }

  function nameOf(u) {
    try { return decodeURIComponent(new URL(u).pathname.split("/").filter(Boolean).pop() || "").slice(0, 28); }
    catch { return ""; }
  }

  async function send(payload) {
    menu.replaceChildren();
    const note = el("div", "note");
    note.append(el("span", "spin"), "Sending to Haul…");
    menu.append(note);
    const r = await api.runtime.sendMessage({ type: "buttonDownload", ...payload })
      .catch(e => ({ ok: false, message: String(e) }));
    if (r && r.ok) {
      closeMenu();
      btn.classList.add("ok");
      label.textContent = r.asked ? "Choose where to save in Haul" : "Downloading in Haul";
      setTimeout(() => { btn.classList.remove("ok"); label.textContent = "Download"; scheduleHide(); }, 2500);
    } else {
      menu.replaceChildren(el("div", "note", (r && r.message) || "Haul couldn't start this download."));
    }
  }

  function closeMenu() {
    menuOpen = false;
    view = null;
    if (menu) { menu.hidden = true; menu.replaceChildren(); }
    scheduleHide();
  }
})();
