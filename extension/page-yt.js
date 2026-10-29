// Runs in YouTube's own page context (world: MAIN) so it can read the stream list the
// player already loaded. Answers only Haul's isolated content script, and returns only
// a summary (no URLs): the download itself is done by yt-dlp from the page address.
(() => {
  if (window.__haulYt) return;
  window.__haulYt = true;

  function currentId() {
    const u = new URL(location.href);
    if (u.searchParams.get("v")) return u.searchParams.get("v");
    const m = u.pathname.match(/^\/(?:shorts|embed|live)\/([\w-]{6,})/);
    return m ? m[1] : "";
  }

  function summary() {
    const player = document.getElementById("movie_player");
    let r = null;
    try { r = player && player.getPlayerResponse && player.getPlayerResponse(); } catch {}
    r = r || window.ytInitialPlayerResponse;
    const d = r && r.videoDetails;
    if (!d || !r.streamingData || (currentId() && d.videoId !== currentId())) return null;  // stale after SPA nav
    const all = [...(r.streamingData.formats || []), ...(r.streamingData.adaptiveFormats || [])];
    const video = [], audio = [];
    for (const f of all) {
      const mime = f.mimeType || "";
      const size = Number(f.contentLength) || (f.bitrate && d.lengthSeconds ? f.bitrate / 8 * d.lengthSeconds : 0);
      if (mime.startsWith("video/") && f.height) {
        video.push({ height: f.height, size, mp4: mime.includes("avc1"), muxed: mime.includes("mp4a") });
      } else if (mime.startsWith("audio/")) {
        audio.push({ size, mp4: mime.includes("mp4a"), bitrate: f.bitrate || 0 });
      }
    }
    return { id: d.videoId, title: d.title || "", live: !!d.isLiveContent && !!d.isLive,
      seconds: Number(d.lengthSeconds) || 0, video, audio };
  }

  window.addEventListener("message", e => {
    if (e.source !== window || !e.data || e.data.__haul !== "yt-req") return;
    let data = null;
    try { data = summary(); } catch {}
    window.postMessage({ __haul: "yt", id: e.data.id, data }, "*");
  });
})();
