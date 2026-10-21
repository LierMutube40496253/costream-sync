// One virtual streamer: fake YouTube player + the REAL extension logic (controller.js,
// client_core.js, sync_math.js) + a scripted panel, all talking to the real sync server
// through a shaped network.
const { Controller } = require('../../extension/controller.js');
const { SyncClient } = require('../../extension/client_core.js');
const { StampTracker, HeadTracker, headGap } = require('../../extension/sync_math.js');
const { FakeYouTube } = require('./fake_player.js');
const { ShapedSocket, mulberry32 } = require('./network.js');

class VirtualUser {
  // cfg: {name, net, clock: {errorS, driftMsPerMin}, stream: {liveEdge, sittingAt},
  //       maxDelay, answer: 'accept'|'decline'|'ignore', answerAfterS}
  constructor(cfg, { url, room, K, trueNow, streamSeed, videoId, log }) {
    this.cfg = { maxDelay: 30, answer: 'accept', answerAfterS: 2, clock: {}, net: {}, ...cfg };
    this.name = cfg.name;
    this.url = url;
    this.room = room;
    this.trueNow = trueNow;
    this.videoId = videoId;
    this.log = (text) => log(this.name, text);
    this.rng = mulberry32((cfg.seed ?? 7) + cfg.name.charCodeAt(0));
    this.t0 = trueNow();
    this.player = new FakeYouTube({ trueNow, K, streamSeed, rng: this.rng, ...cfg.stream });
    this.stamps = new StampTracker();
    this.headTracker = new HeadTracker();
    this.state = { state: 'idle' };
    this.lastMeasure = null;
    this.events = [];
  }

  // This user's own (wrong) clock: 2.3 s slow, drifting 50 ms per minute, ...
  localNow(trueT = this.trueNow()) {
    const c = this.cfg.clock;
    return trueT + (c.errorS || 0) + ((c.driftMsPerMin || 0) / 1000) * ((trueT - this.t0) / 60);
  }

  start() {
    this.newPage();
    this.startConnections();
  }

  // Everything that lives in the YouTube page (content.js + page.js) and is lost on reload.
  newPage() {
    const p = this.player;
    this.stamps = new StampTracker();
    this.headTracker = new HeadTracker();
    this.lastPos = null;
    this.lastMove = this.trueNow();
    this.controller = new Controller({
      pauseFor: (s, done) => { p.pause(); setTimeout(() => { p.play(); done(); }, s * 1000); },
      skip: (s) => p.skip(s),
      setRate: (r) => p.setRate(r),
      rate: () => p.rate,
    }, () => this.localNow() * 1000);
    this.controller.setMaxDelay(this.cfg.maxDelay);
  }

  // Everything in the background service worker: survives a page reload.
  startConnections() {
    this.ext = new ShapedSocket(this.url, this.cfg.net, this.rng, (m) => this.client.onMessage(m));
    this.client = new SyncClient({
      send: (o) => this.ext.send(o), user: this.name, room: this.room, maxDelay: this.cfg.maxDelay,
      nowSec: () => this.localNow(),
      onTarget: (msg) => {
        this.log(`set_target ${msg.target_delay.toFixed(3)} s (session ${msg.session_id}) -> page`);
        this.controller.queueTarget(msg);   // same as content.js
      },
      onLeave: (sid, why) => { this.log(`sync ${sid} ended (${why})`); this.controller.release(); },
      log: (t) => this.log(t),
    });
    this.client.hello();
    this.client.pingBatch();
    this.timers = [
      setInterval(() => this.client.pingBatch(), 5000),
      setInterval(() => this.tick(), 100),
      setInterval(() => this.report(), 250),
    ];

    this.panel = new ShapedSocket(this.url, this.cfg.net, this.rng, (m) => this.onPanel(m));
    this.panel.send({ type: 'hello', role: 'panel', user: this.name, room: this.room });
  }

  // Same shape as content.js produces for real YouTube.
  measure() {
    const p = this.player;
    if (p.inAd()) return { valid: false, inAd: true, videoId: this.videoId };
    const f = p.frame();
    // Same freeze rule as content.js: playing but the position hasn't moved for over 1 s.
    if (p.M !== this.lastPos) { this.lastPos = p.M; this.lastMove = this.trueNow(); }
    const stalled = !p.paused && this.trueNow() - this.lastMove > 1;
    this.stamps.add(p.stamp(), p.M);
    // Same as content.js: only the (stale-able) seekableEnd is visible, never the true head.
    this.headTracker.update(p.seekableEnd, this.localNow());
    const clock = this.client.clock;
    if (!clock) return { valid: false, why: 'no server clock yet', videoId: this.videoId };
    const wall = this.localNow(f.shownAtTrue);
    return {
      valid: true, videoId: this.videoId, playerTime: f.playerTime,
      at: wall + clock.offset, headGap: headGap(this.headTracker, wall, f.playerTime, p.seg),
      K: this.stamps.estimate(p.M).K, dvr: true, quality: 'frame',
      paused: p.paused && !this.controller.pausing, pausedBy: p.pausedBy,
      stalled: stalled && !this.controller.pausing, youtubeError: !!p.errorShown,
      headReady: this.headTracker.ready(),
    };
  }

  tick() {
    const m = this.measure();
    this.lastMeasure = m;
    const c = this.controller.tick(m);
    if (c.resolved) {
      const r = c.resolved;
      this.log(r.ok ? `holding target (${r.session_id})` : `refused target (${r.session_id}): ${r.reason}`);
      if (!r.ok) { this.ext.send({ type: 'leave_sync', session_id: r.session_id }); this.client.endSession(); }
    }
    if (c.left) {
      this.log(`left ${c.left}: ${c.reason}`);
      this.ext.send({ type: 'leave_sync', session_id: c.left });
      this.client.endSession();
    }
    if (c.state !== this.state.state) this.log(`state ${c.state}`);
    this.state = c;
  }

  report() {
    if (!this.lastMeasure) return;
    const st = this.client.statusMsg(this.lastMeasure, this.state);
    if (st) this.ext.send(st);
  }

  onPanel(msg) {
    if (msg.type === 'match_offer') {
      this.log(`offer from ${msg.requester}: "${msg.your_move.text}" -> ${this.cfg.answer}`);
      if (this.cfg.answer === 'ignore') return;
      setTimeout(() => this.panel.send({ type: 'match_answer', offer_id: msg.offer_id, accept: this.cfg.answer === 'accept' }),
                 this.cfg.answerAfterS * 1000);
    } else if (msg.type === 'match_result') {
      this.log(`result ${msg.kind} ${msg.status}: group ${msg.group.join(',') || '-'} at ${msg.target_delay == null ? '-' : msg.target_delay.toFixed(2) + ' s'}`
               + (Object.keys(msg.reasons || {}).length ? ` (${Object.entries(msg.reasons).map(([u, r]) => `${u}: ${r}`).join('; ')})` : ''));
    } else if (msg.type === 'error') {
      this.log('panel error: ' + msg.message);
    }
  }

  // ---- scenario actions
  match(other) { this.panel.send({ type: 'match_request', target_user: other }); }
  syncAll() { this.panel.send({ type: 'sync_all_request' }); }
  leave() { if (this.client.session) this.panel.send({ type: 'leave_sync', session_id: this.client.session }); }
  ad(s) { this.player.ad(s); }
  rebuffer(s) { this.player.rebuffer(s); }
  youtubePause(s) { this.player.pause('youtube'); setTimeout(() => this.player.play(), s * 1000); }
  // Page reload: the page-side state is gone; the background still has the sync and
  // re-sends the accepted target (background.js onConnect -> rejoinTarget).
  reload(prerollS = 5) {
    this.player.reload(prerollS);
    this.newPage();
    const target = this.client.rejoinTarget();
    if (target) {
      this.log(`page reloaded: rejoining ${target.session_id} at ${target.target_delay.toFixed(3)} s`);
      this.controller.queueTarget(target);
    }
  }
  // YouTube's "Something went wrong": playback stops; the user refreshes after `s` seconds.
  youtubeError(s = 5) { this.player.error(); setTimeout(() => this.reload(), s * 1000); }

  stop() {
    for (const t of this.timers || []) clearInterval(t);
    this.ext && this.ext.close();
    this.panel && this.panel.close();
  }
}

module.exports = { VirtualUser };
