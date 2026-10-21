// Extension side of the protocol (docs/DESIGN.md, "Communication protocol"), shared by
// background.js and the simulator: hello, clock sync, status, and the checks that decide
// whether a set_target may reach the player.
(function (root) {
  const M = (root && root.CostreamMath) || require('./sync_math.js');
  const P = M.PARAMS;

  class SyncClient {
    // send(obj): deliver to the server. onTarget(msg): a checked set_target.
    // onLeave(sessionId, reason): stop controlling the player.
    // token: Discord access token (discord mode); user + room: names (dev mode).
    // onHello({user, name, room_name}), onAuthError(message), onNoVoice(): login / room news.
    constructor({ send, user, room, token = null, maxDelay, onTarget, onLeave, onClock = () => {}, log = () => {},
                  onHello = () => {}, onAuthError = () => {}, onNoVoice = () => {},
                  nowSec = () => Date.now() / 1000 }) {
      Object.assign(this, { send, user, room, token, maxDelay, onTarget, onLeave, onClock, log, nowSec,
                            onHello, onAuthError, onNoVoice });
      this.samples = [];
      this.clock = null;              // {offset, rtt}: server time = local time + offset
      this.accepted = new Set();      // sessions this user is in by their own accept/request
      this.session = null;
      this.dropTimer = null;
      this.last = null;               // last valid measurement, reused while in an ad
    }

    // With a token the server asks Discord who this is; the id comes from Discord, not from us.
    hello() {
      this.send(this.token ? { type: 'hello', role: 'extension', token: this.token }
                           : { type: 'hello', role: 'extension', user: this.user, room: this.room });
    }

    pingBatch(n = 8, spacingMs = 100) {
      for (let i = 0; i < n; i++) setTimeout(() => this.send({ type: 'clock_ping', t0: this.nowSec() }), i * spacingMs);
    }

    serverNow() { return this.clock ? this.nowSec() + this.clock.offset : null; }

    onMessage(msg) {
      switch (msg.type) {
        case 'clock_pong': {
          this.samples.push(M.clockSample(msg.t0, msg.t1, msg.t2, this.nowSec()));
          if (this.samples.length > 40) this.samples.shift();
          this.clock = M.bestClock(this.samples);
          this.onClock(this.clock);
          break;
        }
        case 'match_result':
          // Only a sync this user asked for or accepted counts (design doc, Safety).
          if (msg.session_id && (msg.group || []).includes(this.user)
              && ['requester', 'accepted'].includes((msg.users || {})[this.user])) {
            this.accepted.add(msg.session_id);
          }
          break;
        case 'set_target':
          if (!M.validSetTarget(msg)) { this.log('ignored malformed set_target'); break; }
          if (!this.accepted.has(msg.session_id)) { this.log(`ignored set_target for ${msg.session_id}: not accepted here`); break; }
          this.session = msg.session_id;
          this.target = msg;   // kept so a reloaded page can rejoin (rejoinTarget)
          this.onTarget(msg);
          break;
        case 'leave_sync':
          if (msg.session_id === this.session) { this.session = null; this.target = null; this.onLeave(msg.session_id, 'left'); }
          break;
        case 'hello_ok':
          if (msg.user) this.user = msg.user;   // in discord mode: the Discord id, from the server
          this.onHello(msg);
          break;
        case 'no_voice_channel':
          this.onNoVoice();
          break;
        case 'error':
          this.log('server: ' + msg.message);
          if (msg.code === 'auth') this.onAuthError(msg.message);
          break;
      }
    }

    // Network drop: keep the current delay, after 10 s hand back normal playback.
    connectionLost() {
      if (this.dropTimer || !this.session) return;
      this.dropTimer = setTimeout(() => {
        this.dropTimer = null;
        const sid = this.session;
        this.endSession();
        this.onLeave(sid, 'network');
      }, P.DROP_RELEASE_S * 1000);
    }

    connectionRestored() { clearTimeout(this.dropTimer); this.dropTimer = null; }

    // Design doc, Robustness: a page reload must not lose an accepted sync. The sync lives
    // here (background), so when the reloaded page connects again it gets the same target
    // and rejoins like after an ad: no new offer, still checked against the user's max.
    rejoinTarget() { return this.session && this.target && this.target.session_id === this.session ? this.target : null; }

    // Stop controlling the player for good (user left, target refused, out of range).
    endSession() { this.session = null; this.target = null; }

    // m: measurement (+ K, videoId, dvr, quality). c: controller tick result.
    statusMsg(m, c) {
      if (!this.clock || !m.videoId) return null;
      if (m.valid && m.K != null) {
        const current = m.at - (m.playerTime + m.K);
        // Live edge = the earliest moment we can actually reach (headGap keeps a margin to
        // YouTube's newest chunk), never later than where we are. If it were unreachable,
        // the slowest member could never reach the group target (simulator: stuck 500 ms).
        this.last = { current, edge: M.liveEdge(current, m.headGap), playerTime: m.playerTime, at: m.at };
      }
      if (!this.last) return null;
      return {
        type: 'status', video_id: m.videoId,
        current_delay: +this.last.current.toFixed(4), live_edge: +this.last.edge.toFixed(4),
        max_delay: this.maxDelay, player_time: this.last.playerTime, at: this.last.at,
        in_ad: !!m.inAd, dvr_available: m.dvr ?? null, quality: m.quality || null,
        paused_by: m.paused ? (m.pausedBy || 'user') : null,
        sync_error_ms: c && c.err != null ? Math.round(c.err * 10000) / 10 : null,
        session_id: this.session,
      };
    }
  }

  if (root) root.CostreamClient = SyncClient;
  if (typeof window === 'undefined' && typeof importScripts === 'undefined' && typeof module === 'object' && module) {
    module.exports = { SyncClient };   // Node (tests, simulator)
  }
})(typeof self !== 'undefined' ? self : typeof globalThis !== 'undefined' ? globalThis : this);
