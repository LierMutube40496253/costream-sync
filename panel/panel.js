// Panel (docs/DESIGN.md): shows the group, requests and sync status. Talks only to the sync
// server, never to the extension. Works in a normal browser window and as an OBS custom dock.
// Until the Discord login exists, the user types a name and a room (= voice channel); both can
// also come from the address: /panel/?user=A&room=lck-casters (handy for an OBS dock).
(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    dot: $('dot'), who: $('who'), logout: $('logout'), join: $('join'), user: $('j-user'), room: $('j-room'),
    main: $('main'), me: $('me'), members: $('members'), syncall: $('syncall'), toasts: $('toasts'),
    offer: $('offer'), offerTitle: $('offer-title'), offerMove: $('offer-move'), offerOthers: $('offer-others'),
    accept: $('accept'), decline: $('decline'), countdown: $('countdown'),
  };
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* private window */ } },
  };

  let ws = null, me = null, room = null, members = [], offer = null, offerTimer = null, retry = 0;

  const fmtS = (x) => (x == null ? '–' : `${x.toFixed(1)} s`);
  const fmtMs = (x) => (x == null ? '–' : `${x > 0 ? '+' : ''}${Math.round(x)} ms`);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  function toast(text, kind = '') {
    const div = document.createElement('div');
    div.className = `toast ${kind}`;
    div.textContent = text;
    els.toasts.prepend(div);
    setTimeout(() => div.remove(), 12000);
    while (els.toasts.children.length > 5) els.toasts.lastChild.remove();
  }

  // ---------------------------------------------------------------- connection

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {
      retry = 0;
      els.dot.classList.add('on');
      els.dot.title = 'connected';
      ws.send(JSON.stringify({ type: 'hello', role: 'panel', user: me, room }));
    };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = () => {
      els.dot.classList.remove('on');
      els.dot.title = 'not connected, retrying';
      if (me) setTimeout(connect, Math.min(10000, 500 * 2 ** retry++));
    };
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
    else toast('Not connected to the sync server', 'bad');
  }

  // ---------------------------------------------------------------- messages

  function onMessage(msg) {
    if (msg.type === 'group') { members = msg.members; render(); }
    else if (msg.type === 'match_offer') showOffer(msg);
    else if (msg.type === 'match_result') onResult(msg);
    else if (msg.type === 'error') toast(msg.message, 'bad');
  }

  function onResult(r) {
    if (offer && r.offer_id === offer.offer_id) hideOffer();
    const others = Object.entries(r.users || {}).filter(([u]) => u !== me);
    if (r.status === 'refused') {
      toast(`Can't sync: ${r.reason || 'over someone\'s max'}`, 'bad');
    } else if (r.status === 'accepted' || r.status === 'synced') {
      const target = r.target_delay == null ? '' : ` at ${r.target_delay.toFixed(1)} s behind live`;
      toast(`Synced with ${r.group.filter((u) => u !== me).join(', ')}${target}`, 'ok');
    } else if (r.kind === 'match') {
      const [who, what] = others.find(([, s]) => s !== 'requester') || ['', r.status];
      toast(what === 'declined' ? `${who} declined` : what === 'timed_out' ? `No response from ${who}` : `Match failed: ${r.status}`, 'warn');
    } else {
      toast('Sync all: nobody joined', 'warn');
    }
    for (const [u, s] of others) {
      if (s === 'left_out' && r.reasons && r.reasons[u]) toast(`${u} left out: ${r.reasons[u]}`, 'warn');
    }
    if (r.users && r.users[me] === 'left_out' && r.reasons && r.reasons[me]) toast(`You were left out: ${r.reasons[me]}`, 'warn');
  }

  // ---------------------------------------------------------------- offer

  function showOffer(o) {
    offer = o;
    const what = o.kind === 'sync_all' ? 'wants to sync the whole room' : 'wants to match timing with you';
    els.offerTitle.textContent = `${o.requester} ${what}`;
    // Rule 3: worded from where you are now. For sync all the group can only shrink, so "up to".
    const move = o.your_move;
    const upTo = o.kind === 'sync_all' && move.action === 'pause' ? 'up to ' : '';
    els.offerMove.textContent = move.action === 'none' ? 'Your stream won\'t change.'
      : `Accepting will ${move.action === 'pause' ? `pause your stream ${upTo}${move.seconds.toFixed(1)} s` : move.text}.`;
    const others = Object.entries(o.moves).filter(([u]) => u !== me)
      .map(([u, mv]) => `${u}: ${mv.action === 'none' ? 'no change' : mv.text.replace('your stream', 'stream')}`);
    els.offerOthers.textContent = others.join(' · ');
    els.offer.hidden = false;
    const end = Date.now() + o.timeout * 1000;
    clearInterval(offerTimer);
    const tick = () => {
      const left = Math.max(0, Math.ceil((end - Date.now()) / 1000));
      els.countdown.textContent = `${left} s`;
      if (left === 0) hideOffer();
    };
    tick();
    offerTimer = setInterval(tick, 250);
  }

  function hideOffer() { offer = null; clearInterval(offerTimer); els.offer.hidden = true; }

  function answer(accept) {
    if (!offer) return;
    send({ type: 'match_answer', offer_id: offer.offer_id, accept });
    hideOffer();
  }

  // ---------------------------------------------------------------- render

  function stateOf(m) {
    if (!m.extension) return ['extension not connected', 'bad'];
    if (m.current_delay == null) return ['waiting for video', ''];
    if (m.in_ad) return ['in ad', 'warn'];
    if (m.paused_by === 'youtube') return ['YouTube paused', 'warn'];
    if (m.paused_by) return ['paused', 'warn'];
    if (m.session_id) return m.sync_error_ms != null && Math.abs(m.sync_error_ms) <= 33 ? ['synced', 'ok'] : ['syncing', 'warn'];
    return ['', ''];
  }

  function render() {
    const mine = members.find((m) => m.user === me) || { user: me };
    const sessionMates = mine.session_id ? members.filter((m) => m.session_id === mine.session_id) : [];
    const errs = sessionMates.map((m) => m.sync_error_ms).filter((x) => x != null);
    // Achieved offset between synced users (design doc: reported precision): the spread of
    // their errors vs the shared target.
    const spread = errs.length > 1 ? Math.max(...errs) - Math.min(...errs) : null;
    const [st, kind] = stateOf(mine);
    els.me.innerHTML = `
      <div class="me-line"><span class="big">${mine.current_delay == null ? '–' : fmtS(mine.current_delay)}</span>
        <span class="muted">behind live</span>${st ? `<span class="chip ${kind}">${esc(st)}</span>` : ''}</div>
      <dl class="kv">
        <dt>Live edge</dt><dd>${fmtS(mine.live_edge)} (earliest you can watch)</dd>
        <dt>Max extra delay</dt><dd>${mine.max_delay == null ? '–' : `${mine.max_delay} s`}</dd>
        <dt>Measurement</dt><dd>${mine.quality === 'frame' ? 'frame-exact' : mine.quality === 'coarse' ? 'coarse (window covered?)' : '–'}</dd>
      </dl>
      ${mine.session_id ? `<div class="sync-box">
        <span>In sync with <strong>${esc(sessionMates.filter((m) => m.user !== me).map((m) => m.user).join(', ') || '…')}</strong></span>
        <span class="chip ${spread != null && spread <= 33 ? 'ok' : 'warn'}">offset ${spread == null ? '–' : `${Math.round(spread)} ms`}</span>
        <button id="leave" class="small-btn" style="margin-left:auto">Leave sync</button></div>` : ''}`;
    const leave = $('leave');
    if (leave) leave.onclick = () => send({ type: 'leave_sync', session_id: mine.session_id });

    const others = members.filter((m) => m.user !== me);
    els.members.innerHTML = others.length ? '' : '<li class="muted small">Nobody else in this room yet.</li>';
    for (const m of others) {
      const [ms, mk] = stateOf(m);
      const sameVideo = mine.video_id && m.video_id && m.video_id === mine.video_id;
      const canMatch = m.extension && m.current_delay != null && !m.in_ad && mine.current_delay != null && sameVideo
        && !(mine.session_id && m.session_id === mine.session_id);
      const why = !m.extension ? 'their extension is not connected' : !sameVideo && m.video_id && mine.video_id
        ? 'watching a different video' : mine.session_id && m.session_id === mine.session_id ? 'already in sync' : '';
      const li = document.createElement('li');
      li.className = 'member';
      li.innerHTML = `
        <div><div class="name">${esc(m.user)}</div>
          <div class="sub">${m.current_delay == null ? 'no video' : `${fmtS(m.current_delay)} · edge ${fmtS(m.live_edge)}`}
          ${m.session_id && m.session_id === mine.session_id ? ` · ${fmtMs(m.sync_error_ms)}` : ''}</div></div>
        <div class="right">${ms ? `<span class="chip ${mk}">${esc(ms)}</span>` : ''}
          ${why && why !== 'already in sync' ? `<span class="chip">${esc(why)}</span>` : ''}
          <button class="small-btn" ${canMatch ? '' : 'disabled'} data-user="${esc(m.user)}">Match</button></div>`;
      li.querySelector('button').onclick = () => {
        send({ type: 'match_request', target_user: m.user });
        toast(`Asked ${m.user} to match timing…`);
      };
      els.members.appendChild(li);
    }
    els.syncall.disabled = !(others.some((m) => m.extension && m.current_delay != null) && mine.current_delay != null);
  }

  // ---------------------------------------------------------------- start

  function start(user, roomName) {
    me = user;
    room = roomName;
    store.set('costream_user', user);
    store.set('costream_room', roomName);
    els.who.textContent = `${user} · ${roomName}`;
    els.join.hidden = true;
    els.main.hidden = false;
    els.logout.hidden = false;
    render();
    connect();
  }

  els.join.onsubmit = (e) => { e.preventDefault(); start(els.user.value.trim(), els.room.value.trim()); };
  els.accept.onclick = () => answer(true);
  els.decline.onclick = () => answer(false);
  els.syncall.onclick = () => { send({ type: 'sync_all_request' }); toast('Asked the room to sync…'); };
  els.logout.onclick = () => {
    store.del('costream_user');
    me = null;
    if (ws) ws.close();
    location.search = '';
  };

  const q = new URLSearchParams(location.search);
  const user = q.get('user') || store.get('costream_user');
  const roomName = q.get('room') || store.get('costream_room');
  if (user && roomName) start(user, roomName);
  else {
    els.join.hidden = false;
    els.user.value = user || '';
    els.room.value = roomName || '';
  }
  window.__panel = { state: () => ({ me, room, members, offer }) };   // for tests
})();
