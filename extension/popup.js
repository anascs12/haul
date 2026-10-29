const api = globalThis.browser ?? globalThis.chrome;
const $ = id => document.getElementById(id);

function human(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
}

function nameOf(url) {
  try {
    const p = new URL(url).pathname.split("/").filter(Boolean);
    return decodeURIComponent(p.pop() || url);
  } catch { return url; }
}

async function grab(url, source, referrer, title) {
  $("msg").textContent = "Sending to Haul…";
  const r = await api.runtime.sendMessage({ type: "grab", url, source, referrer, title });
  $("msg").textContent = r.ok ? "Sent to Haul." : (r.message || "Haul could not take it.");
  if (r.ok) setTimeout(() => window.close(), 600);
}

(async () => {
  const s = await api.runtime.sendMessage({ type: "status" });
  const st = $("status");
  if (!s.host) { st.textContent = "Not set up"; st.className = "pill bad"; $("help").hidden = false; }
  else if (s.running) { st.textContent = "Connected"; st.className = "pill ok"; }
  else { st.textContent = "Starts on demand"; st.className = "pill ok"; }

  $("capture").checked = s.capture;
  $("capture").onchange = e => api.runtime.sendMessage({ type: "setCapture", value: e.target.checked });
  $("videoButton").checked = s.videoButton;
  $("videoButton").onchange = e => api.runtime.sendMessage({ type: "setVideoButton", value: e.target.checked });
  const pageOk = /^https?:/i.test(s.page);
  $("grab").disabled = !pageOk;
  $("grab").onclick = () => grab(s.page, "page", s.page, s.title);

  const ul = $("media");
  $("empty").hidden = s.media.length > 0;
  for (const m of s.media.slice().reverse()) {
    const li = document.createElement("li");
    const kind = document.createElement("span");
    kind.className = "kind"; kind.textContent = m.kind;
    const name = document.createElement("span");
    name.className = "name"; name.title = m.url;
    name.textContent = nameOf(m.url) + (m.size ? "  ·  " + human(m.size) : "");
    const btn = document.createElement("button");
    btn.textContent = "Download";
    btn.onclick = () => grab(m.url, "stream", s.page, s.title);
    li.append(kind, name, btn);
    ul.append(li);
  }
})();
