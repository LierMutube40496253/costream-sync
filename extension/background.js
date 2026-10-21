// Service worker: one server connection per YouTube tab (each tab is one participant).
// No UI except the toolbar badge. Until the Discord login exists, `costream.join(...)`
// (called by the demo, later replaced by the login) sets user, room and max delay.
//
// The WebSocket lives here, not in the page: a youtube.com page may not open connections
// to localhost (Local Network Access); an extension with host permissions can.
importScripts('sync_math.js', 'client_core.js');

const tabs = new Map();   // tabId -> {ws, client, port, status, log}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'costream' || !port.sender.tab) return;
  const tabId = port.sender.tab.id;
  const t = tabs.get(tabId) || { log: [] };
  t.port = port;
  tabs.set(tabId, t);
  port.onMessage.addListener((msg) => onContent(tabId, msg));
  port.onDisconnect.addListener(() => { if (tabs.get(tabId)?.port === port) tabs.get(tabId).port = null; });
  if (t.client) {
    port.postMessage({ cmd: 'max', maxDelay: t.client.maxDelay });
    if (t.client.clock) port.postMessage({ cmd: 'clock', offset: t.client.clock.offset });
    const target = t.client.rejoinTarget();
    if (target) {
      // The page was reloaded during an accepted sync: rejoin, no new offer.
      log(t, `page reloaded: rejoining ${target.session_id} at ${target.target_delay.toFixed(3)} s`);
      port.postMessage({ cmd: 'set_target', target });
    }
  }
});
chrome.tabs.onRemoved.addListener((tabId) => leave(tabId));

function log(t, text) {
  t.log.push({ t: Date.now() / 1000, text });
  if (t.log.length > 100) t.log.shift();
}

function toContent(t, msg) { if (t.port) t.port.postMessage(msg); }

function onContent(tabId, msg) {
  const t = tabs.get(tabId);
  if (msg.kind === 'status') {
    t.status = msg;
    badge(tabId, msg);
    if (msg.c && msg.c.left) {
      log(t, `left ${msg.c.left}: ${msg.c.reason}`);
      send(t, { type: 'leave_sync', session_id: msg.c.left });
      if (t.client) t.client.endSession();
    }
    if (!t.client || Date.now() - (t.lastStatus || 0) < 250) return;
    const st = t.client.statusMsg(msg.m, msg.c);
    if (st) { t.lastStatus = Date.now(); send(t, st); }
  } else if (msg.kind === 'target_result') {
    log(t, msg.ok ? `holding target (${msg.session_id})` : `refused target (${msg.session_id}): ${msg.reason}`);
    if (!msg.ok) {
      send(t, { type: 'leave_sync', session_id: msg.session_id });
      if (t.client) t.client.endSession();
    }
  }
}

function send(t, obj) {
  if (t.ws && t.ws.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify(obj));
}

function connect(tabId) {
  const t = tabs.get(tabId);
  t.ws = new WebSocket(`${t.server}/ws`);
  t.ws.onopen = () => {
    t.client.connectionRestored();
    t.client.hello();
    t.client.pingBatch();
    log(t, `connected to ${t.server}, room ${t.client.room} as ${t.client.user}`);
  };
  t.ws.onmessage = (e) => t.client.onMessage(JSON.parse(e.data));
  t.ws.onclose = () => {
    if (!tabs.get(tabId)?.client) return;  // left on purpose
    log(t, 'disconnected; retrying');
    t.client.connectionLost();
    setTimeout(() => tabs.get(tabId)?.client && connect(tabId), 2000);
  };
}

async function activeYoutubeTab() {
  const list = await chrome.tabs.query({ url: 'https://www.youtube.com/*' });
  const tab = list.find((x) => x.active) || list[0];
  if (!tab) throw new Error('no YouTube tab open');
  return tab.id;
}

async function join({ tabId, server = 'ws://localhost:8765', room, user, maxDelay = 30 }) {
  tabId = tabId ?? (await activeYoutubeTab());
  leave(tabId);
  const t = tabs.get(tabId) || { log: [] };
  tabs.set(tabId, t);
  t.server = server;
  t.client = new CostreamClient({
    send: (obj) => send(t, obj), user, room, maxDelay: Number(maxDelay),
    onTarget: (msg) => {
      log(t, `set_target ${msg.session_id}: ${msg.target_delay.toFixed(3)} s -> page${t.port ? '' : ' (NO PORT: page not connected)'}`);
      toContent(t, { cmd: 'set_target', target: msg });
    },
    onLeave: (sid, why) => { log(t, `sync ${sid} ended (${why})`); toContent(t, { cmd: 'release' }); },
    onClock: (clock) => toContent(t, { cmd: 'clock', offset: clock.offset }),
    log: (text) => log(t, text),
  });
  t.clockTimer = setInterval(() => t.client.pingBatch(), 5000);
  toContent(t, { cmd: 'max', maxDelay: Number(maxDelay) });
  connect(tabId);
  return tabId;
}

function leave(tabId) {
  const t = tabs.get(tabId);
  if (!t || !t.client) return;
  clearInterval(t.clockTimer);
  t.client = null;
  try { t.ws.close(); } catch (e) { /* already closed */ }
  toContent(t, { cmd: 'release' });
}

self.costream = {
  join,
  leave,
  state() {
    const out = {};
    for (const [tabId, t] of tabs) {
      out[tabId] = { status: t.status, log: t.log.slice(-30),
                     clock: t.client && t.client.clock, session: t.client && t.client.session };
    }
    return out;
  },
};

function badge(tabId, st) {
  const c = st.c || {};
  let text = '', color = '#777';
  if (st.m && st.m.inAd) { text = 'AD'; color = '#c62828'; }
  else if (c.state === 'synced') { text = `${Math.round(Math.abs(c.err) * 1000)}ms`; color = '#2e7d32'; }
  else if (c.err != null) { text = (c.err).toFixed(1); color = '#f9a825'; }
  else if (c.state && c.state.endsWith('_paused')) { text = '||'; color = '#f9a825'; }
  const title = `costream-sync: ${c.state || 'idle'}${st.m && st.m.valid ? ` | quality ${st.m.quality}` : ''}`;
  const key = text + color + title;
  const t = tabs.get(tabId);
  if (t.badgeKey === key) return;
  t.badgeKey = key;
  chrome.action.setBadgeText({ tabId, text }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color }).catch(() => {});
  chrome.action.setTitle({ tabId, title }).catch(() => {});
}
