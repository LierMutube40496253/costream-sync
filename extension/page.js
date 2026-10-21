// Runs in the YouTube page's own JS world (MAIN) because only there can we call the
// player's methods. READ-ONLY: it never touches playback. It posts a sample every 100 ms
// to content.js, which measures and controls.
//
// What it reads, with numbers from a real stream (2026-10-21):
//   p.getCurrentTime()         99970.38  player clock, identical for every viewer of the
//                                        same frame (checked in two tabs, to 0.1 ms)
//   video.currentTime          48570.38  the <video> element's own timeline
//   P = difference             51400.00  constant; maps <video> frames to player time
//   p.getMediaReferenceTime()  capture time of the frame on screen
//   k = that - getCurrentTime  1792504509.2762  constant within a 5 s chunk
//   seekableEnd                player time of the newest chunk; only moves on downloads
//
// No dependencies on purpose: Chrome injects a file listed in two content_scripts
// entries into one world only, so sync_math.js stays in the isolated world. The median of
// the chunk stamps is computed there (content.js); this file sends the raw stamp.
(() => {
  if (window.__costreamPage) return;
  window.__costreamPage = true;
  let videoId = null;
  let seg = 5;
  let dvr = null;
  let lastStats = 0;

  function sample() {
    const p = document.getElementById('movie_player');
    const v = p && p.querySelector('video.html5-main-video, video');
    if (!p || !v || typeof p.getCurrentTime !== 'function') return { ready: false };
    const vd = (p.getVideoData && p.getVideoData()) || {};
    videoId = vd.video_id;
    const ad = p.classList.contains('ad-showing') || p.classList.contains('ad-interrupting');
    if (ad) return { ready: true, ad: true, videoId, live: !!vd.isLive };

    const cur = p.getCurrentTime();
    if (Date.now() - lastStats > 2000) {
      lastStats = Date.now();
      try {
        const st = p.getVideoStats();
        seg = Number(st.segduration) || seg;
        dvr = st.live === 'dvr';
      } catch (e) { /* keep last */ }
    }
    // Primary: getMediaReferenceTime(). Fallback (design doc, Robustness): ingestionTime.
    const pr = p.getProgressState ? p.getProgressState() : {};
    let ref = typeof p.getMediaReferenceTime === 'function' ? p.getMediaReferenceTime() : null;
    let source = 'mediaReferenceTime';
    if (!(ref > 1.6e9) && pr.ingestionTime > 1.6e9) { ref = pr.ingestionTime; source = 'ingestionTime'; }
    return {
      ready: true, ad: false, live: !!vd.isLive, videoId, dvr, source, seg,
      P: cur - v.currentTime,                          // player time = <video> time + P
      kChunk: vd.isLive && ref > 1.6e9 ? ref - cur : null,  // this chunk's stamp
      seekableEnd: typeof pr.seekableEnd === 'number' ? pr.seekableEnd : null,   // newest chunk; see HeadTracker
      cur, wall: Date.now(),
    };
  }

  setInterval(() => {
    let msg;
    try { msg = sample(); } catch (e) { msg = { ready: false, error: String(e) }; }
    window.postMessage({ __costream: 'sample', ...msg }, location.origin);
  }, 100);
})();
