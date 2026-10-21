// Isolated-world content script: the YouTube adapter for controller.js. Measures the frame
// on screen, runs the controller, and reports to background.js. Draws nothing on the page.
// Playback is only ever changed from here (never from page.js), so scripts on the YouTube
// page cannot trigger a change by faking messages.
(() => {
  const M = self.CostreamMath;
  const stamps = new M.StampTracker();
  const head = new M.HeadTracker();
  let stampVideo = null;
  let sample = null;          // latest page.js sample
  let frame = null;           // last presented frame: {wall, mediaTime, at}
  const lags = [];            // (shown at - media time) of the last frames, for the median
  let watched = null;         // <video> the frame callback is attached to
  let clockOffset = null;     // server time - local time, from background
  let port = null;
  let ourPause = false;       // the controller paused the video
  let pausedBy = null;        // 'user' | 'youtube' | null
  let lastInput = 0;          // last click/key by the viewer
  let lastCt = null;          // freeze detection: last <video> time and when it last moved
  let lastMove = Date.now();

  window.addEventListener('message', (e) => {
    if (e.source !== window || !e.data || e.data.__costream !== 'sample') return;
    sample = e.data;
    if (sample.videoId !== stampVideo) { stamps.clear(); head.reset(); stampVideo = sample.videoId; }
    if (sample.kChunk != null) stamps.add(sample.kChunk, sample.cur);
    if (sample.seekableEnd != null) head.update(sample.seekableEnd, sample.wall / 1000);
  });
  for (const ev of ['pointerdown', 'keydown']) document.addEventListener(ev, () => { lastInput = Date.now(); }, true);

  function video() {
    return document.querySelector('#movie_player video.html5-main-video') || document.querySelector('#movie_player video');
  }

  // requestVideoFrameCallback: exactly which frame was shown and when.
  function watchFrames() {
    const v = video();
    if (!v || v === watched || !v.requestVideoFrameCallback) return;
    watched = v;
    lags.length = 0;
    v.addEventListener('pause', () => {
      // Design doc, Robustness: a pause we didn't cause and the viewer didn't click is
      // YouTube's own (consent dialog, autoplay rules); sync resumes when it plays again.
      if (!ourPause) pausedBy = Date.now() - lastInput < 1500 ? 'user' : 'youtube';
    });
    v.addEventListener('play', () => { pausedBy = null; lags.length = 0; });
    v.addEventListener('seeked', () => { lags.length = 0; });   // the lag jumps after a skip
    const onFrame = (_now, md) => {
      if (v !== watched) return;
      const perfToWall = Date.now() / 1000 - performance.now() / 1000;
      const wall = perfToWall + md.expectedDisplayTime / 1000;
      // Now and then a frame is reported one refresh late; one such report shifted the
      // measurement by a whole frame (33 ms) and the controller chased it (end-to-end
      // test). Use the median of (shown at - media time) over the last 15 frames.
      lags.push(wall - md.mediaTime);
      if (lags.length > 15) lags.shift();
      const lag = M.median(lags);
      frame = { wall: md.mediaTime + lag, mediaTime: md.mediaTime, at: Date.now() };
      v.requestVideoFrameCallback(onFrame);
    };
    v.requestVideoFrameCallback(onFrame);
  }

  function measure() {
    const v = video();
    const s = sample;
    if (!v || !s || !s.ready) return { valid: false, why: 'player not ready' };
    if (s.ad) return { valid: false, inAd: true, videoId: s.videoId };
    if (!s.live) return { valid: false, why: 'not a live stream', videoId: s.videoId };
    if (clockOffset == null) return { valid: false, why: 'no server clock yet', videoId: s.videoId };
    if (s.P == null || Date.now() - s.wall > 500) return { valid: false, why: 'no player sample', videoId: s.videoId };
    if (v.currentTime !== lastCt) { lastCt = v.currentTime; lastMove = Date.now(); }
    // Playing but not moving for over a second: rebuffer, or YouTube frozen after a seek
    // (seen in testing). currentTime is used, not frame callbacks, so a covered window
    // doesn't count as frozen.
    const stalled = !v.paused && !ourPause && Date.now() - lastMove > 1000;
    const errBox = document.querySelector('#movie_player .ytp-error');
    const youtubeError = !!errBox && errBox.offsetParent !== null;
    const fresh = frame && !v.paused && Date.now() - frame.at < 250;
    // Covered or minimised window: no frame callbacks, fall back to currentTime.
    const playerTime = (fresh ? frame.mediaTime : v.currentTime) + s.P;
    const wall = fresh ? frame.wall : Date.now() / 1000;
    const at = wall + clockOffset;
    return {
      valid: true, videoId: s.videoId, playerTime, at, headGap: M.headGap(head, wall, playerTime, s.seg),
      K: stamps.estimate(s.cur, s.seg).K, dvr: s.dvr,
      quality: fresh ? 'frame' : 'coarse', paused: v.paused, pausedBy: v.paused ? (pausedBy || 'user') : null,
      stalled, youtubeError, headReady: head.ready(),
    };
  }

  const actions = {
    pauseFor(seconds, onResumed) {
      const v = video();
      ourPause = true;
      v.pause();
      setTimeout(() => { v.play(); ourPause = false; onResumed(); }, seconds * 1000);
    },
    skip(seconds) { video().currentTime += seconds; },
    setRate(rate) { video().playbackRate = rate; },
    rate() { const v = video(); return v ? v.playbackRate : 1; },
  };
  const controller = new self.CostreamController(actions);

  function send(msg) {
    try { if (port) port.postMessage(msg); } catch (e) { /* reconnects below */ }
  }

  function onCommand(msg) {
    if (msg.cmd === 'clock') clockOffset = msg.offset;
    else if (msg.cmd === 'max') controller.setMaxDelay(msg.maxDelay);
    else if (msg.cmd === 'set_target') controller.queueTarget(msg.target);   // applied in tick() once measurable
    else if (msg.cmd === 'release') controller.release();
  }

  function connect() {
    try { port = chrome.runtime.connect({ name: 'costream' }); } catch (e) { return; }  // extension reloaded
    port.onMessage.addListener(onCommand);
    port.onDisconnect.addListener(() => { port = null; setTimeout(connect, 1000); });
  }

  connect();
  setInterval(() => {
    watchFrames();
    const m = measure();
    const c = controller.tick(m);
    if (c.resolved) send({ kind: 'target_result', ...c.resolved });
    send({ kind: 'status', m, c });
  }, 100);
})();
