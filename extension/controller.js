// Holds a sync target on a player. Player-agnostic: content.js gives it YouTube's
// <video>, the simulator gives it a fake player. Never touches the player without a
// target that passed the local checks.
//
// Measurement `m` (made by the adapter every ~100 ms):
//   valid, inAd, paused, pausedBy ('user' | 'youtube' | null)
//   headReady   the live head estimate has seen YouTube's newest chunk move at least once
//   stalled     playing, but the position hasn't moved for over a second
//   youtubeError YouTube shows "Something went wrong"
//   playerTime  player clock of the frame on screen
//   at          server time that frame was on screen
//   headGap     seconds between that frame and the earliest point we can reach
//               (sync_math.headGap; negative if we already sit closer; null if unknown)
// Actions: pauseFor(seconds, onResumed), skip(seconds), setRate(rate), rate()
(function (root) {
  const M = (root && root.CostreamMath) || require('./sync_math.js');
  const P = M.PARAMS;

  class Controller {
    constructor(actions, nowMs = () => Date.now()) {
      this.a = actions;
      this.nowMs = nowMs;
      this.maxDelay = null;
      this.target = null;       // {session_id, offset, target_delay}
      this.pending = null;      // queued set_target, see queueTarget
      this.pausing = false;
      this.settleUntil = 0;
    }

    setMaxDelay(maxDelay) { this.maxDelay = maxDelay; }

    // err > 0: ahead of the target, must add delay.
    error(m) { return m.playerTime - (m.at + this.target.offset); }

    // Called with a validated set_target and the latest measurement.
    setTarget(msg, m) {
      if (!m || !m.valid) return { ok: false, reason: m && m.inAd ? 'ad playing' : 'not measuring yet' };
      const err = m.playerTime - (m.at + msg.target_player_offset);
      const check = M.checkTarget(M.behindEdge(m.headGap, err), this.maxDelay);
      if (!check.ok) return check;
      this.release();
      this.target = { session_id: msg.session_id, offset: msg.target_player_offset, target_delay: msg.target_delay };
      return { ok: true };
    }

    // "Leave sync" restores normal playback at once: speed 1.0, no further control.
    release() {
      this.target = null;
      this.pending = null;
      if (this.a.rate() !== 1) this.a.setRate(1);
    }

    // A target to apply as soon as we can measure (right after a page reload, or while an
    // ad plays). Waiting is not refusing: refused only by the local checks, or if we still
    // can't measure after PENDING_MS.
    queueTarget(msg) { this.pending = { msg, since: this.nowMs() }; }

    tick(m) {
      let resolved = null;
      if (this.pending) {
        const sid = this.pending.msg.session_id;
        if (m.valid && !m.inAd && !m.stalled && !m.youtubeError && m.headReady !== false) {
          const msg = this.pending.msg;
          this.pending = null;
          resolved = { session_id: sid, ...this.setTarget(msg, m) };
        } else if (this.nowMs() - this.pending.since > P.PENDING_MS) {
          this.pending = null;
          resolved = { session_id: sid, ok: false, reason: `can't measure (${m.why || 'ad, frozen or error'})` };
        }
      }
      const out = this.step(m);
      if (resolved) out.resolved = resolved;
      return out;
    }

    step(m) {
      const t = this.target;
      if (!t) return { state: 'idle' };
      if (m.inAd) { if (this.a.rate() !== 1) this.a.setRate(1); return { state: 'in_ad' }; }
      if (!m.valid) return { state: 'waiting' };
      if (this.pausing) return { state: 'pausing', err: this.error(m) };
      if (m.paused) return { state: m.pausedBy === 'youtube' ? 'youtube_paused' : 'user_paused' };
      // Playing but the picture doesn't move (rebuffer, or YouTube frozen after a seek):
      // wait like during an ad. Correcting now would skip forward mid-freeze.
      if (m.stalled) return { state: 'stalled', err: this.error(m) };
      if (m.youtubeError) return { state: 'youtube_error' };

      const err = this.error(m);
      const behindHead = M.behindEdge(m.headGap, err);
      if (behindHead > this.maxDelay + P.MAX_SLACK) {
        // e.g. after a long ad: rejoining would need more delay than the user allows.
        const reason = `would need ${behindHead.toFixed(1)} s behind live, max is ${this.maxDelay} s`;
        const sid = t.session_id;
        this.release();
        return { state: 'out_of_range', left: sid, reason };
      }
      if (this.nowMs() < this.settleUntil) return { state: 'settling', err };

      const d = M.decide(err);
      if (d.action === 'pause') {
        if (this.a.rate() !== 1) this.a.setRate(1);
        this.pausing = true;
        this.a.pauseFor(d.seconds, () => {
          this.pausing = false;
          this.settleUntil = this.nowMs() + P.SETTLE_MS;
        });
        return { state: 'pausing', err };
      }
      if (d.action === 'skip') {
        const room = m.headGap == null ? d.seconds : m.headGap;   // headGap already keeps a chunk of margin
        const by = Math.min(d.seconds, room);
        if (by > 0.05) {
          if (this.a.rate() !== 1) this.a.setRate(1);
          this.a.skip(by);
          this.settleUntil = this.nowMs() + P.SETTLE_MS;
          return { state: 'skipping', err };
        }
        // Target is at or past our live head: play normally as close as we can.
        if (this.a.rate() !== 1) this.a.setRate(1);
        return { state: 'at_live_edge', err };
      }
      if (Math.abs(this.a.rate() - d.rate) > 1e-4) this.a.setRate(d.rate);
      return { state: Math.abs(err) < P.SYNCED ? 'synced' : 'adjusting', err };
    }
  }

  if (root) root.CostreamController = Controller;
  if (typeof window === 'undefined' && typeof importScripts === 'undefined' && typeof module === 'object' && module) {
    module.exports = { Controller };   // Node (tests, simulator)
  }
})(typeof self !== 'undefined' ? self : typeof globalThis !== 'undefined' ? globalThis : this);
