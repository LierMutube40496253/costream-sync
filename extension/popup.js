// Toolbar popup: "Log in with Discord", the logged-in name, server connection status, "Log out".
const $ = (id) => document.getElementById(id);
const ask = (cmd) => chrome.runtime.sendMessage({ cmd });

async function refresh() {
  const s = await ask('popup_state');
  $('loggedout').hidden = s.loggedIn;
  $('loggedin').hidden = !s.loggedIn;
  if (!s.loggedIn) return;
  $('name').textContent = s.name;
  $('conn').textContent = s.room ? `${s.status} · ${s.room}` : s.status;
  $('dot').className = `dot ${s.status === 'connected' ? 'ok' : s.status.startsWith('connected') ? 'warn' : ''}`;
}

$('login').onclick = async () => {
  $('error').textContent = '';
  $('login').disabled = true;
  const r = await ask('login');
  $('login').disabled = false;
  if (r.error) $('error').textContent = r.error;
  refresh();
};
$('logout').onclick = async () => { await ask('logout'); refresh(); };

refresh();
setInterval(refresh, 1000);
