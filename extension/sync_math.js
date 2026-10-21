// Pure maths shared by the extension (page, content, background) and the Node simulator.
// All times are seconds.
//
// Two clocks matter:
//   server time  every client converts its own clock to the sync server's (clock_ping)
//   player time  YouTube's getCurrentTime(): identical for every viewer of the same frame
// A sync target arrives as `target_player_offset`: at server time t the player should show
// player time t + offset. "err" = shown - wanted:
//   err > 0  we are ahead (see things too early) -> add delay: pause, or play slower
//   err < 0  we are behind -> skip forward, or play faster
(function (root) {
  const P = {
    COARSE: 0.25,        // above this, pause / skip; below, change speed
    DEAD: 0.008,         // below 8 ms play at exactly 1.0 (half a frame at 60 fps)
    GAIN: 0.5,           // speed change per second of error: err 0.03 s -> 0.985x
    MAX_DEV: 0.02,       // design doc: 0.98-1.02x, pitch preserved
    SYNCED: 0.020,       // |err| under 20 ms counts as synced (about one frame)
    SYNCED_HOLD_MS: 500, // ...and stays "synced" unless out of range for 0.5 s in a row
                         // (screens show whole frames, so readings flick by one frame)
    HEAD_SLACK: 0.5,     // a target up to 0.5 s past our live head is still accepted
    MAX_SLACK: 0.05,
    HEAD_MARGIN_CHUNKS: 1, // reachable head = newest chunk end (seekableEnd) minus one chunk
                           // (YouTube said "at live head" 3.8 s short of seekableEnd)
    SETTLE_MS: 400,      // after a pause or skip, let the player settle before measuring
    DROP_RELEASE_S: 10,  // network drop: keep the delay this long, then hand back playback
    PENDING_MS: 60000,   // a queued target (after reload / during an ad) waits at most this long
    STAMP_WINDOW: 90,    // seconds of player time whose chunk stamps feed the median
  };

  // Example: err +11.8 -> pause 11.8 s.  err +0.030 -> 0.985x, closes 30 ms in a few s.
  function decide(err) {
    if (err > P.COARSE) return { action: 'pause', seconds: err };
    if (err < -P.COARSE) return { action: 'skip', seconds: -err };
    if (Math.abs(err) < P.DEAD) return { action: 'rate', rate: 1 };
    const dev = Math.max(-P.MAX_DEV, Math.min(P.MAX_DEV, P.GAIN * err));
    return { action: 'rate', rate: +(1 - dev).toFixed(4) };
  }

  // Local safety check (design doc, Safety). behindHead = how far the target is behind
  // this viewer's live head. Example: max 10 -> 12 s behind is refused, 6 s is fine.
  function checkTarget(behindHead, maxDelay) {
    if (typeof behindHead !== 'number' || !isFinite(behindHead)) return { ok: false, reason: 'not measuring' };
    if (typeof maxDelay !== 'number') return { ok: false, reason: 'no max delay set' };
    if (behindHead > maxDelay + P.MAX_SLACK)
      return { ok: false, reason: `needs ${behindHead.toFixed(1)} s behind live, max is ${maxDelay} s` };
    if (behindHead < -P.HEAD_SLACK)
      return { ok: false, reason: `target is ${(-behindHead).toFixed(1)} s ahead of this feed's live edge` };
    return { ok: true };
  }

  // NTP-style: t0 client send, t1 server receive, t2 server send, t3 client receive.
  // Example: t0 100.000, t1 102.320, t2 102.321, t3 100.041 -> offset +2.300, rtt 0.040.
  // A lopsided route (200 ms out, 100 ms back) shifts the offset by (200-100)/2 = 50 ms;
  // nothing on the client can see that.
  function clockSample(t0, t1, t2, t3) {
    return { offset: ((t1 - t0) + (t2 - t3)) / 2, rtt: (t3 - t0) - (t2 - t1) };
  }

  // Keep the lowest-round-trip samples: the least room for queueing and asymmetry.
  function bestClock(samples, keep = 3) {
    if (!samples.length) return null;
    const best = [...samples].sort((a, b) => a.rtt - b.rtt).slice(0, keep);
    return { offset: median(best.map((s) => s.offset)), rtt: best[0].rtt };
  }

  function median(xs) {
    const s = [...xs].sort((a, b) => a - b);
    const n = s.length;
    return n === 0 ? null : n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
  }

  // YouTube stamps each 5 s chunk with its capture time; k = stamp - player time is the
  // same for every viewer of that chunk but jitters ~40 ms between chunks. This keeps one
  // entry per chunk and returns the median over the last STAMP_WINDOW s of player time.
  class StampTracker {
    constructor() { this.stamps = new Map(); }
    clear() { this.stamps.clear(); }
    add(k, playerTime) {
      if (!(k > 1.6e9)) return;   // not a plausible wall-clock stamp (after 2020)
      this.stamps.set(k.toFixed(4), { k, at: playerTime });
      for (const [key, s] of this.stamps) if (s.at < playerTime - 3 * P.STAMP_WINDOW) this.stamps.delete(key);
    }
    estimate(playerTime, seg = 5) {
      const recent = [...this.stamps.values()].filter((s) => s.at >= playerTime - P.STAMP_WINDOW && s.at <= playerTime + seg);
      return { K: median(recent.map((s) => s.k)), count: recent.length };
    }
  }

  // YouTube's seekableEnd (player time of the newest chunk) only moves when the player
  // downloads a chunk; while paused, stalled or seeking it stands still although the live
  // stream keeps going. Measured: frozen at the head for 7 s, seekableEnd stayed 106780.000
  // and the old estimate of the live edge drifted from 15.4 s to 22.5 s.
  // So remember when it last moved and let the head advance in real time since then.
  // Example: seekableEnd 106780 seen moving at local 1000.0 -> at 1007.0 the head is 106787.
  class HeadTracker {
    constructor() { this.reset(); }
    reset() { this.end = null; this.at = null; this.moves = 0; this.seen = null; }
    update(seekableEnd, wallNow) {
      if (typeof seekableEnd !== 'number' || !isFinite(seekableEnd)) return;
      const newStream = this.end == null || seekableEnd < this.end - 60;
      // Count every download (any increase), for ready(). Counting only values ahead of the
      // estimate stalled the count once the estimate was accurate (a fresh chunk lands just
      // behind it, by the download delay): a sync then waited 21 s (simulator).
      if (newStream) this.moves = 0;
      else if (seekableEnd > this.seen) this.moves++;
      this.seen = seekableEnd;
      // Only take a new value that is AHEAD of our real-time estimate. After a pause the
      // player catches up by downloading the newest complete chunk, which ended seconds
      // ago; jumping back to it made the live edge look up to 3.7 s later (simulator).
      if (newStream || seekableEnd > this.headAt(wallNow)) {
        this.end = seekableEnd;
        this.at = wallNow;
      }
    }
    // The first value seen (e.g. right after a reload) can be up to a chunk old, and so can
    // the first move after it (a catch-up download after the preroll ad). The second move
    // happens in real time, right after a new chunk appears. Before that, don't judge
    // targets (simulator: a reloaded page refused its own sync, "target 4.7 s ahead of live
    // edge", then "1.8 s ahead" when waiting for one move only).
    ready() { return this.moves >= 2; }
    // Player time of the newest content at local wall time `wallNow`.
    headAt(wallNow) { return this.end == null ? null : this.end + (wallNow - this.at); }
  }

  // Seconds between the frame on screen and the earliest point we can reach (YouTube's
  // newest chunk minus a margin). Can be negative: the viewer already sits closer than that.
  function headGap(tracker, wallNow, playerTime, seg = 5) {
    const head = tracker.headAt(wallNow);
    return head == null ? null : head - P.HEAD_MARGIN_CHUNKS * seg - playerTime;
  }

  // Live edge (earliest delay this viewer can reach). Never later than where they are now.
  // Example: current 41 s, headGap 15.5 s -> edge 25.5 s; current 20 s, headGap -3 -> 20 s.
  function liveEdge(currentDelay, gap) {
    return gap == null ? currentDelay : currentDelay - Math.max(0, gap);
  }

  // How far a target is behind this viewer's live edge (same definition as liveEdge: the
  // earliest point is at least where they are now). err = shown - wanted.
  // Example (run 8): A at YouTube's head, headGap -4.5 s; target 3.4 s behind A -> 3.4 s.
  // Using headGap directly gave -1.1 s, the target was refused, and the sync ended.
  function behindEdge(gap, err) {
    return (gap == null ? 0 : Math.max(0, gap)) + err;
  }

  // Server messages are validated before anything acts on them (design doc, Safety).
  function validSetTarget(m) {
    return !!m && m.type === 'set_target' && typeof m.session_id === 'string' && m.session_id.length > 0
      && Number.isFinite(m.target_delay) && m.target_delay >= 0 && m.target_delay <= 3600
      && Number.isFinite(m.target_player_offset) && Number.isFinite(m.reference_stamp);
  }

  const api = { PARAMS: P, decide, checkTarget, clockSample, bestClock, median, StampTracker, HeadTracker,
                headGap, liveEdge, behindEdge, validSetTarget };
  // Always publish on the global; additionally export for Node. Don't decide by
  // `typeof module` alone: on youtube.com something named `module` exists, and the
  // extension crashed with "reading 'PARAMS'" because the global was never set.
  if (root) root.CostreamMath = api;
  if (isNode()) module.exports = api;

  function isNode() {
    return typeof window === 'undefined' && typeof importScripts === 'undefined'
      && typeof module === 'object' && module !== null && typeof module.exports === 'object';
  }
})(typeof self !== 'undefined' ? self : typeof globalThis !== 'undefined' ? globalThis : this);
