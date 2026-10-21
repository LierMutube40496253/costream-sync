import { describe, expect, it, vi } from 'vitest';
import M from '../../extension/sync_math.js';
import C from '../../extension/controller.js';
import S from '../../extension/client_core.js';

const { decide, checkTarget, clockSample, bestClock, StampTracker, validSetTarget, PARAMS } = M;

describe('decide', () => {
  it('pauses for exactly a big positive error', () => {
    expect(decide(11.8)).toEqual({ action: 'pause', seconds: 11.8 });
  });
  it('skips forward on a big negative error', () => {
    expect(decide(-2)).toEqual({ action: 'skip', seconds: 2 });
  });
  it('fine-tunes within 0.98-1.02x, in the right direction', () => {
    expect(decide(0.03).rate).toBe(0.985);   // ahead -> slower
    expect(decide(-0.03).rate).toBe(1.015);  // behind -> faster
    expect(decide(0.2).rate).toBe(0.98);     // capped
    expect(decide(-0.2).rate).toBe(1.02);
    expect(decide(0.005).rate).toBe(1);      // dead band
  });
});

describe('checkTarget (local max check)', () => {
  it('accepts inside max, refuses above it', () => {
    expect(checkTarget(6, 10).ok).toBe(true);
    const r = checkTarget(12, 10);
    expect(r.ok).toBe(false);
    expect(r.reason).toBe('needs 12.0 s behind live, max is 10 s');
  });
  it('refuses a target clearly ahead of the live edge', () => {
    expect(checkTarget(-0.3, 10).ok).toBe(true);
    expect(checkTarget(-2, 10).ok).toBe(false);
  });
});

describe('clock', () => {
  it('computes offset and round trip like NTP', () => {
    const s = clockSample(100.0, 102.32, 102.321, 100.041);
    expect(s.offset).toBeCloseTo(2.3, 6);
    expect(s.rtt).toBeCloseTo(0.04, 6);
  });
  it('a lopsided route shifts the offset by half the difference', () => {
    // True offset 0; 200 ms out, 100 ms back.
    expect(clockSample(0, 0.2, 0.2, 0.3).offset).toBeCloseTo(0.05, 6);
  });
  it('keeps the lowest round trips', () => {
    const c = bestClock([{ offset: 1, rtt: 0.5 }, { offset: 2.30, rtt: 0.04 }, { offset: 2.31, rtt: 0.05 }, { offset: 2.29, rtt: 0.06 }]);
    expect(c.offset).toBeCloseTo(2.30, 6);
    expect(c.rtt).toBe(0.04);
  });
});

describe('StampTracker', () => {
  it('one entry per chunk, median over the window, ignores bogus stamps', () => {
    const t = new StampTracker();
    t.add(1792504509.2762, 99970);
    t.add(1792504509.2762, 99971);   // same chunk again
    t.add(1792504509.2343, 99975);
    t.add(1792504509.2937, 99965);
    t.add(42, 99976);                // not a wall-clock stamp
    const e = t.estimate(99976);
    expect(e.count).toBe(3);
    expect(e.K).toBeCloseTo(1792504509.2762, 4);
  });
});

describe('HeadTracker and live edge', () => {
  it('keeps the head moving in real time while seekableEnd stands still', () => {
    // Real YouTube, frozen at the head: seekableEnd stayed 106780 for 7 s.
    const h = new M.HeadTracker();
    h.update(106780, 1000.0);
    h.update(106780, 1007.0);           // no new download
    expect(h.headAt(1007.0)).toBeCloseTo(106787, 6);
  });
  it('follows downloads and resets on a new stream', () => {
    const h = new M.HeadTracker();
    h.update(100, 0);
    h.update(105, 4.9);                 // a chunk arrived
    expect(h.headAt(6)).toBeCloseTo(106.1, 6);
    h.update(10, 7);                    // jumped far back: different stream
    expect(h.headAt(7)).toBe(10);
  });
  it('a catch-up download after a pause does not pull the head back', () => {
    const h = new M.HeadTracker();
    h.update(100, 0);
    // Paused 12 s; on resume the player downloads the newest complete chunk (ends at 110),
    // while the real-time estimate is already 112.
    h.update(110, 12);
    expect(h.headAt(12)).toBeCloseTo(112, 6);
    h.update(115, 14.9);                // genuinely new chunk, ahead of 114.9
    expect(h.headAt(15)).toBeCloseTo(115.1, 6);
  });

  it('is not ready until the newest chunk has been seen to move', () => {
    const h = new M.HeadTracker();
    h.update(1000, 0);
    expect(h.ready()).toBe(false);   // first value after a (re)load may be a chunk old
    h.update(1000, 2);
    expect(h.ready()).toBe(false);
    h.update(1005, 4.5);
    expect(h.ready()).toBe(false);   // first move may be a catch-up download
    h.update(1010, 9.6);
    expect(h.ready()).toBe(true);
  });

  it('gap to the reachable head keeps one chunk of margin', () => {
    const h = new M.HeadTracker();
    h.update(1000, 50);
    expect(M.headGap(h, 52, 980, 5)).toBeCloseTo(1002 - 5 - 980, 6);   // 17 s of room
    expect(M.headGap(new M.HeadTracker(), 52, 980, 5)).toBe(null);
  });
  it('a rewind or freeze does not move the live edge', () => {
    // Viewer 5 s behind the reachable head, then frozen 7 s: current delay grows
    // 18.7 -> 25.7 and the gap grows 5 -> 12, so the edge stays at 13.7.
    const h = new M.HeadTracker();
    h.update(106780, 0);
    const edge0 = M.liveEdge(18.7, M.headGap(h, 0, 106770, 5));
    const edge7 = M.liveEdge(25.7, M.headGap(h, 7, 106770, 5));
    expect(edge0).toBeCloseTo(13.7, 6);
    expect(edge7).toBeCloseTo(edge0, 6);
  });
  it('live edge is never later than where the viewer is now', () => {
    expect(M.liveEdge(20, -3)).toBe(20);
    expect(M.liveEdge(41, 15.5)).toBe(25.5);
  });
});

describe('validSetTarget', () => {
  it('checks types and ranges', () => {
    const ok = { type: 'set_target', session_id: 's1', target_delay: 25, target_player_offset: -1e9, reference_stamp: 1e9 };
    expect(validSetTarget(ok)).toBe(true);
    expect(validSetTarget({ ...ok, target_delay: -1 })).toBe(false);
    expect(validSetTarget({ ...ok, session_id: '' })).toBe(false);
    expect(validSetTarget({ ...ok, target_player_offset: 'x' })).toBe(false);
  });
});

function fakePlayer() {
  const calls = [];
  let rate = 1;
  return {
    calls,
    actions: {
      pauseFor: (s, done) => { calls.push(['pause', s]); done(); },
      skip: (s) => calls.push(['skip', s]),
      setRate: (r) => { rate = r; calls.push(['rate', r]); },
      rate: () => rate,
    },
  };
}

// Frame at player time 1000 on screen at server time 5000. target offset -4025 means the
// player should show 5000 - 4025 = 975, so we are 25 s ahead.
const m = (over = {}) => ({ valid: true, playerTime: 1000, at: 5000, headGap: 0, paused: false, ...over });
const target = (offset, sid = 's1') => ({ type: 'set_target', session_id: sid, target_delay: 25, target_player_offset: offset, reference_stamp: 0 });

describe('Controller', () => {
  it('never touches the player without a target', () => {
    const p = fakePlayer();
    const c = new C.Controller(p.actions, () => 0);
    expect(c.tick(m()).state).toBe('idle');
    expect(p.calls).toEqual([]);
  });

  it('accepts a target behind us while we sit closer to live than the margin (run 8)', () => {
    // A sits at YouTube's head: 4.5 s closer than the one-chunk margin (headGap -4.5).
    // The target is 3.4 s behind A. Before the fix this was refused ("ahead of live edge").
    const p = fakePlayer();
    const c = new C.Controller(p.actions, () => 0);
    c.setMaxDelay(30);
    const r = c.setTarget(target(-4003.4), m({ headGap: -4.5 }));
    expect(r).toEqual({ ok: true });
    c.tick(m({ headGap: -4.5 }));
    expect(p.calls[0][0]).toBe('pause');
    expect(p.calls[0][1]).toBeCloseTo(3.4, 6);
  });

  it('refuses a target beyond the max (local check)', () => {
    const p = fakePlayer();
    const c = new C.Controller(p.actions, () => 0);
    c.setMaxDelay(10);
    expect(c.setTarget(target(-4025), m()).ok).toBe(false);   // 25 s behind live > 10
    expect(c.tick(m()).state).toBe('idle');
  });

  it('pauses for the error, then fine-tunes with speed', () => {
    const p = fakePlayer();
    let now = 0;
    const c = new C.Controller(p.actions, () => now);
    c.setMaxDelay(30);
    expect(c.setTarget(target(-4025), m()).ok).toBe(true);
    c.tick(m());
    expect(p.calls[0]).toEqual(['pause', 25]);
    now = 1000;   // after settling
    const r = c.tick(m({ playerTime: 975.03 }));   // 30 ms ahead
    expect(r.state).toBe('adjusting');
    expect(p.calls.at(-1)).toEqual(['rate', 0.985]);
    expect(c.tick(m({ playerTime: 975.005 })).state).toBe('synced');
  });

  it('skips forward but never past the reachable head', () => {
    const p = fakePlayer();
    const c = new C.Controller(p.actions, () => 0);
    c.setMaxDelay(30);
    c.setTarget(target(-3990), m({ headGap: 20 }));   // wants 1010: we are 10 s behind
    c.tick(m({ headGap: 3 }));                          // but only 3 s of room (margin already included)
    expect(p.calls[0]).toEqual(['skip', 3]);
  });

  it("doesn't fight a pause by the user or by YouTube", () => {
    const p = fakePlayer();
    const c = new C.Controller(p.actions, () => 0);
    c.setMaxDelay(30);
    c.setTarget(target(-4025), m());
    expect(c.tick(m({ paused: true, pausedBy: 'youtube' })).state).toBe('youtube_paused');
    expect(c.tick(m({ paused: true, pausedBy: 'user' })).state).toBe('user_paused');
    expect(p.calls).toEqual([]);
  });

  it('a queued target (after reload) waits until the head is known, then applies', () => {
    const p = fakePlayer();
    const c = new C.Controller(p.actions, () => 0);
    c.setMaxDelay(30);
    c.queueTarget(target(-4025));
    expect(c.tick(m({ headReady: false })).resolved).toBeUndefined();
    expect(c.tick(m({ inAd: true, valid: false })).resolved).toBeUndefined();
    const r = c.tick(m({ headReady: true }));
    expect(r.resolved).toEqual({ session_id: 's1', ok: true });
  });

  it('waits while the picture is frozen or YouTube shows an error', () => {
    const p = fakePlayer();
    const c = new C.Controller(p.actions, () => 0);
    c.setMaxDelay(30);
    c.setTarget(target(-4025), m());
    expect(c.tick(m({ stalled: true })).state).toBe('stalled');
    expect(c.tick(m({ youtubeError: true })).state).toBe('youtube_error');
    expect(p.calls).toEqual([]);
  });

  it('leaves the sync when rejoining after an ad would exceed the max', () => {
    const p = fakePlayer();
    const c = new C.Controller(p.actions, () => 0);
    c.setMaxDelay(10);
    c.setTarget(target(-4005), m({ headGap: 0 }));      // 5 s behind live: fine
    const r = c.tick(m({ playerTime: 970, headGap: 40 })); // after the ad: target 35 s behind live
    expect(r.state).toBe('out_of_range');
    expect(r.left).toBe('s1');
    expect(c.tick(m()).state).toBe('idle');
  });

  it('release restores speed 1.0', () => {
    const p = fakePlayer();
    const c = new C.Controller(p.actions, () => 1000);
    c.setMaxDelay(30);
    c.setTarget(target(-4000.03), m());
    c.tick(m());
    expect(p.actions.rate()).toBe(0.985);
    c.release();
    expect(p.actions.rate()).toBe(1);
  });
});

describe('SyncClient', () => {
  const make = () => {
    const sent = [], targets = [], leaves = [];
    const c = new S.SyncClient({ send: (o) => sent.push(o), user: 'A', room: 'r', maxDelay: 30,
      onTarget: (t) => targets.push(t), onLeave: (s, why) => leaves.push([s, why]), nowSec: () => 100 });
    return { c, sent, targets, leaves };
  };
  const st = { type: 'set_target', session_id: 's1', target_delay: 8, target_player_offset: -1e9, reference_stamp: 1e9 };

  it('ignores set_target for a sync this user did not accept', () => {
    const { c, targets } = make();
    c.onMessage(st);
    expect(targets).toEqual([]);
  });

  it('acts on set_target after a match_result that lists this user as accepted', () => {
    const { c, targets } = make();
    c.onMessage({ type: 'match_result', session_id: 's1', group: ['A', 'B'], users: { A: 'accepted', B: 'requester' } });
    c.onMessage(st);
    expect(targets).toHaveLength(1);
  });

  it('a declined user is not in the group, even if named in the result', () => {
    const { c, targets } = make();
    c.onMessage({ type: 'match_result', session_id: 's1', group: ['B', 'C'], users: { A: 'declined' } });
    c.onMessage(st);
    expect(targets).toEqual([]);
  });

  it('keeps the accepted target for a reloaded page, and forgets it on leave', () => {
    const { c } = make();
    c.onMessage({ type: 'match_result', session_id: 's1', group: ['A', 'B'], users: { A: 'accepted' } });
    c.onMessage(st);
    expect(c.rejoinTarget()).toEqual(st);
    c.onMessage({ type: 'leave_sync', session_id: 's1' });
    expect(c.rejoinTarget()).toBe(null);
  });

  it('hands back playback 10 s after the network drops', () => {
    vi.useFakeTimers();
    const { c, leaves } = make();
    c.onMessage({ type: 'match_result', session_id: 's1', group: ['A'], users: { A: 'requester' } });
    c.onMessage(st);
    c.connectionLost();
    vi.advanceTimersByTime(9000);
    expect(leaves).toEqual([]);
    vi.advanceTimersByTime(1500);
    expect(leaves).toEqual([['s1', 'network']]);
    vi.useRealTimers();
  });

  it('builds status with delay and live edge from the shared measurement', () => {
    const { c } = make();
    c.onMessage({ type: 'clock_pong', t0: 99.98, t1: 102.3, t2: 102.3, t3: 100 });
    const s = c.statusMsg({ valid: true, videoId: 'v', playerTime: 1000, at: 5000, K: 3975, headGap: 2, quality: 'frame' },
                          { state: 'synced', err: 0.0123 });
    // headGap (to the reachable head, margin included) 2 s: live edge 25 - 2.
    expect(s).toMatchObject({ type: 'status', video_id: 'v', current_delay: 25, live_edge: 23, max_delay: 30,
                              player_time: 1000, at: 5000, sync_error_ms: 12.3, quality: 'frame' });
  });
});
