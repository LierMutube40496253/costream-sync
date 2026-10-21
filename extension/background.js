// Service worker: one server connection per joined YouTube tab. No UI on the YouTube page; the
// toolbar popup (popup.html) shows login, name, connection and "Log out".
//
// Discord mode (the server's welcome says auth "discord"): "Log in with Discord" in the popup
// opens Discord's login window (scope identify). Then the first YouTube tab joins on its own,
// and the server puts you in the room of the voice channel you're in.
// Dev mode: costream.join({user, room}) from this service worker's console (tests use it).
//
// The WebSocket lives here, not in the page: a youtube.com page may not open connections
// to localhost (Local Network Access); an extension with host permissions can.
importScripts('sync_math.js', 'client_core.js', 'discord_auth.js');

const tabs = new Map();   // tabId -> {ws, client, port, status, log, name, roomName, noVoice}
const settings = { server: 'ws://localhost:8765', maxDelay: 30 };
let login = null;         // {token, expiresAt, id, name} while logged in with Discord
const ready = chrome.storage.local.get(['settings', 'login']).then((s) => {
  Object.assign(settings, s.settings || {});
  if (s.login && s.login.expiresAt > Date.now()) login = s.login;
  idleBadge();
});

chrome.runtime.onConnect.addListener(async (port) => {
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
  } else {
    await ready;
    autoJoin();
  }
});
chrome.tabs.onRemoved.addListener((tabId) => { leave(tabId); tabs.delete(tabId); autoJoin(); });

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
    log(t, `connected to ${t.server}${t.client.token ? ' with Discord login' : `, room ${t.client.room} as ${t.client.user}`}`);
  };
  t.ws.onmessage = (e) => t.client && t.client.onMessage(JSON.parse(e.data));
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

// Dev mode: join({room, user}). Discord mode: join({token}); the server finds the user (from
// Discord) and the room (their voice channel).
async function join({ tabId, server = settings.server, room, user, token = null, maxDelay = settings.maxDelay }) {
  tabId = tabId ?? (await activeYoutubeTab());
  leave(tabId);
  const t = tabs.get(tabId) || { log: [] };
  tabs.set(tabId, t);
  t.server = server;
  t.client = new CostreamClient({
    send: (obj) => send(t, obj), user, room, token, maxDelay: Number(maxDelay),
    onTarget: (msg) => {
      log(t, `set_target ${msg.session_id}: ${msg.target_delay.toFixed(3)} s -> page${t.port ? '' : ' (NO PORT: page not connected)'}`);
      toContent(t, { cmd: 'set_target', target: msg });
    },
    onLeave: (sid, why) => { log(t, `sync ${sid} ended (${why})`); toContent(t, { cmd: 'release' }); },
    onClock: (clock) => toContent(t, { cmd: 'clock', offset: clock.offset }),
    onHello: (msg) => { t.name = msg.name; t.roomName = msg.room_name; t.noVoice = !msg.room; },
    onNoVoice: () => { t.noVoice = true; t.roomName = null; },
    onAuthError: (message) => { log(t, message); logout(); },
    log: (text) => log(t, text),
  });
  t.clockTimer = setInterval(() => t.client && t.client.pingBatch(), 5000);
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

// One YouTube tab per Discord account (the server refuses a second extension connection).
function autoJoin() {
  if (!login || login.expiresAt <= Date.now()) return;
  if ([...tabs.values()].some((t) => t.client)) return;
  const free = [...tabs.entries()].find(([, t]) => t.port);
  if (free) join({ tabId: free[0], token: login.token });
}

// ---------------------------------------------------------------- Discord login

function serverMessage(server, first) {
  // Opens a short connection, optionally sends `first` after the welcome, returns the reply.
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${server}/ws`);
    const timer = setTimeout(() => { ws.close(); reject(new Error(`no answer from ${server}`)); }, 8000);
    let welcome = null;
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (!welcome) {
        welcome = msg;
        if (!first) { clearTimeout(timer); ws.close(); resolve(welcome); } else ws.send(JSON.stringify(first));
      } else { clearTimeout(timer); ws.close(); resolve(msg); }
    };
    ws.onerror = () => { clearTimeout(timer); reject(new Error(`can't reach ${server}`)); };
  });
}

async function loginWithDiscord() {
  await ready;
  const welcome = await serverMessage(settings.server);
  if (welcome.auth !== 'discord') throw new Error('this server runs without Discord login (dev mode)');
  const D = self.CostreamDiscord;
  const state = D.newState();
  const redirect = await chrome.identity.launchWebAuthFlow({
    url: D.authorizeUrl(welcome.discord_authorize_url, welcome.discord_client_id, chrome.identity.getRedirectURL(), state),
    interactive: true,
  });
  const r = D.parseRedirect(redirect, state);
  if (r.error) throw new Error(r.error);
  // The server asks Discord whose token this is; we show that name.
  const who = await serverMessage(settings.server, { type: 'whoami', token: r.token });
  if (!who.ok) throw new Error('the server could not confirm this Discord login');
  login = { token: r.token, expiresAt: r.expiresAt, id: who.user, name: who.name };
  await chrome.storage.local.set({ login });
  idleBadge();
  autoJoin();
  return login.name;
}

async function logout() {
  login = null;
  await chrome.storage.local.remove('login');
  for (const tabId of tabs.keys()) leave(tabId);
  idleBadge();
}

function connection() {
  const joined = [...tabs.values()].find((t) => t.client);
  if (!joined) return { status: tabs.size ? 'not connected' : 'open a YouTube live to connect' };
  const open = joined.ws && joined.ws.readyState === WebSocket.OPEN;
  return {
    status: !open ? 'connecting…' : joined.noVoice ? 'connected · join a Discord voice channel' : 'connected',
    room: joined.roomName || null,
    name: joined.name || null,
  };
}

// The popup asks for state, login and logout.
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    await ready;
    if (msg.cmd === 'popup_state') {
      return { loggedIn: !!login, name: login && login.name, server: settings.server, ...connection() };
    }
    if (msg.cmd === 'login') return { name: await loginWithDiscord() };
    if (msg.cmd === 'logout') { await logout(); return { ok: true }; }
    return { error: 'unknown command' };
  })().then(reply, (e) => reply({ error: String(e.message || e) }));
  return true;   // answer asynchronously
});

// For the service-worker console (dev mode, tests, a server on another PC).
self.costream = {
  join,
  leave,
  login: loginWithDiscord,
  logout,
  async setServer(url) { settings.server = url; await chrome.storage.local.set({ settings }); return url; },
  async setMaxDelay(seconds) { settings.maxDelay = Number(seconds); await chrome.storage.local.set({ settings }); },
  state() {
    const out = {};
    for (const [tabId, t] of tabs) {
      out[tabId] = { status: t.status, log: t.log.slice(-30),
                     clock: t.client && t.client.clock, session: t.client && t.client.session };
    }
    return out;
  },
};

// ---------------------------------------------------------------- badge

function idleBadge() {
  chrome.action.setBadgeText({ text: login ? '' : '?' }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ color: '#5865f2' }).catch(() => {});
  chrome.action.setTitle({ title: login ? `costream-sync: ${login.name}` : 'costream-sync: click to log in with Discord' }).catch(() => {});
}

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
