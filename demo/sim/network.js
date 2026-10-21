// In-process network shaper for one virtual user: delays each direction separately
// (lopsided routes), adds jitter, and models packet loss the way TCP shows it to a
// WebSocket: a late message (one retransmit, ~200 ms), never a missing one. Order is kept.
//
// Example (Sydney): out 200 ms, back 100 ms, jitter 40 ms, loss 2%.
// A clock ping then sees rtt ~300 ms and an offset error of (200 - 100) / 2 = 50 ms.

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

class ShapedSocket {
  constructor(url, net, rng, onMessage) {
    this.net = { outMs: 10, backMs: 10, jitterMs: 0, loss: 0, ...net };
    this.rng = rng;
    this.lastOut = 0;
    this.lastIn = 0;
    this.queue = [];
    this.ws = new WebSocket(url);
    this.ws.onopen = () => { for (const s of this.queue.splice(0)) this.ws.send(s); };
    this.ws.onmessage = (e) => this.later('in', () => onMessage(JSON.parse(e.data)));
    this.closed = new Promise((res) => { this.ws.onclose = res; });
  }

  delayMs(dir) {
    const base = dir === 'out' ? this.net.outMs : this.net.backMs;
    const jitter = (this.rng() * 2 - 1) * this.net.jitterMs;
    const lost = this.rng() < this.net.loss ? 200 : 0;
    return Math.max(0, base + jitter + lost);
  }

  // One queue per direction, delivered strictly in order. A separate setTimeout per
  // message could fire out of order (Node timers have 1 ms resolution), so set_target
  // sometimes arrived before match_result; a real WebSocket (TCP) never reorders.
  later(dir, fn) {
    const now = performance.now();
    const key = dir === 'out' ? 'lastOut' : 'lastIn';
    const at = Math.max(now + this.delayMs(dir), this[key]);
    this[key] = at;
    const q = (this.queues ||= { out: [], in: [] })[dir];
    q.push({ at, fn });
    if (q.length === 1) this.pump(dir);
  }

  pump(dir) {
    const q = this.queues[dir];
    if (!q.length) return;
    setTimeout(() => {
      const now = performance.now();
      while (q.length && q[0].at <= now + 0.5) q.shift().fn();
      this.pump(dir);
    }, Math.max(0, q[0].at - performance.now()));
  }

  send(obj) {
    const s = JSON.stringify(obj);
    this.later('out', () => {
      if (this.ws.readyState === WebSocket.OPEN) this.ws.send(s);
      else if (this.ws.readyState === WebSocket.CONNECTING) this.queue.push(s);
    });
  }

  close() { try { this.ws.close(); } catch (e) { /* already closed */ } }
}

module.exports = { ShapedSocket, mulberry32 };
