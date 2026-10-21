// A fake YouTube live player with its own broadcast clock. Times are "true" seconds
// (the simulator's reference clock); each virtual user only ever sees them through its
// own wrong local clock.
//
//   real game moment E      happens at true time E
//   player clock M          = E - K  (same K for every viewer, like getCurrentTime())
//   live head at time t     = t - liveEdge - K   (this viewer can't play past it)
//   chunk stamp (5 s chunk) = K + jitter(chunk), the same for every viewer (~ +-20 ms)
//
// Example: K 1792504509.25, liveEdge 25, true time 1792604600 -> head at player time
// 100065.75. A viewer sitting at 41 s delay is at 100049.75 and can skip forward 16 s.

const FPS_DEFAULT = 30;

function hashJitter(chunk, seed, ms) {
  // Deterministic per chunk, shared by all viewers of the stream.
  let x = (chunk * 2654435761 + seed * 97) >>> 0;
  x ^= x >>> 15; x = Math.imul(x, 2246822507) >>> 0; x ^= x >>> 13;
  return ((x % 10000) / 10000 - 0.5) * 2 * ms / 1000;
}

class FakeYouTube {
  // youtubeHeadLagS: how far behind the newest chunk YouTube lets you play. Default one
  // chunk (= the extension's margin). Real YouTube measured 3.8 s once and ~0 s once.
  constructor({ trueNow, K, liveEdge, sittingAt = null, fps = FPS_DEFAULT, seg = 5, stampJitterMs = 20,
                streamSeed = 1, rng = Math.random, seekErrorS = 0.15, resumeLatencyS = 0.04, youtubeHeadLagS = null }) {
    Object.assign(this, { trueNow, K, liveEdge, fps, seg, stampJitterMs, streamSeed, rng, seekErrorS, resumeLatencyS });
    this.headLag = youtubeHeadLagS ?? seg;
    const t = trueNow();
    this.M = t - (sittingAt ?? liveEdge) - K;   // player clock of the current position
    this.lastT = t;
    this.rate = 1;
    this.paused = false;
    this.pausedBy = null;
    this.frozenUntil = 0;   // rebuffer / resume latency: playing but no frames advance
    this.adUntil = 0;
    // Like YouTube's seekableEnd: end of the newest chunk the player has downloaded. It only
    // moves when a download happens (0-300 ms after the chunk is complete, and only while
    // playing), so it stands still during pauses, freezes and ads.
    this.seekableEnd = Math.floor(this.newest(t) / seg) * seg;
    this.downloadDue = null;
  }

  // Earliest point this viewer can play (true reachable live head), in player time.
  head(t) { return t - this.liveEdge - this.K; }
  // Newest content on the server: headLag ahead of the reachable head.
  newest(t) { return this.head(t) + this.headLag; }

  update() {
    const t = this.trueNow();
    // Content only moves after the ad / freeze ended, even if nobody looked in between
    // (a first version counted a whole 30 s ad as playback on the first read after it).
    const from = Math.max(this.lastT, this.adUntil, this.frozenUntil);
    this.lastT = t;
    if (this.paused) return;
    const active = Math.max(0, t - from);
    // Reaching the live head stalls playback (buffer empty), like YouTube.
    this.M = Math.min(this.M + active * this.rate, this.head(t));
    if (t < this.adUntil || t < this.frozenUntil) return;   // no downloads while frozen / in an ad
    const boundary = Math.floor(this.newest(t) / this.seg) * this.seg;
    if (boundary > this.seekableEnd) {
      if (this.downloadDue == null) this.downloadDue = t + 0.3 * this.rng();
      if (t >= this.downloadDue) { this.seekableEnd = boundary; this.downloadDue = null; }
    }
  }

  inAd() { return this.trueNow() < this.adUntil; }

  // The frame on screen and the true time it started showing (like requestVideoFrameCallback).
  frame() {
    this.update();
    const t = this.lastT;
    const shown = Math.floor(this.M * this.fps) / this.fps;
    const moving = !this.paused && t >= this.frozenUntil && !this.inAd();
    return { playerTime: shown, shownAtTrue: moving ? t - (this.M - shown) / this.rate : t };
  }

  stamp() {
    const chunk = Math.floor(this.M / this.seg);
    return this.K + hashJitter(chunk, this.streamSeed, this.stampJitterMs);
  }

  // Ground truth: how far behind the real game this viewer's playback position is.
  // Uses the continuous position, not the frame number: two players 0.1 ms apart can
  // show neighbouring frames for an instant, which would read as a whole frame (33 ms).
  trueDelay() {
    this.update();
    return this.lastT - (this.M + this.K);
  }

  // ---- actions the controller uses (same as content.js gives it for YouTube)
  pause(by = null) { this.update(); this.paused = true; this.pausedBy = by; }
  play() {
    this.update();
    this.paused = false;
    this.pausedBy = null;
    this.frozenUntil = this.lastT + this.resumeLatencyS * this.rng();   // decoder restart
  }
  skip(seconds) {
    this.update();
    // video.currentTime seeks land close to, not exactly on, the requested time.
    const land = this.M + seconds + (this.rng() * 2 - 1) * this.seekErrorS;
    this.M = Math.min(land, this.head(this.lastT) - 0.1);
    this.frozenUntil = this.lastT + 0.05;
  }
  setRate(r) { this.update(); this.rate = r; }

  // ---- scenario events
  ad(seconds) { this.update(); this.adUntil = this.lastT + seconds; }
  rebuffer(seconds) { this.update(); this.frozenUntil = this.lastT + seconds; }
  // "Something went wrong": playback stops until the page is reloaded.
  error() { this.update(); this.errorShown = true; this.frozenUntil = Infinity; }
  // Page reload: YouTube starts again at its live head, after a preroll ad.
  reload(prerollS = 5) {
    const t = this.trueNow();
    this.lastT = t;
    this.errorShown = false;
    this.frozenUntil = 0;
    this.paused = false;
    this.pausedBy = null;
    this.rate = 1;
    this.M = this.head(t);
    this.adUntil = t + prerollS;
    this.seekableEnd = Math.floor(this.newest(t) / this.seg) * this.seg;
    this.downloadDue = null;
  }
}

module.exports = { FakeYouTube, hashJitter };
