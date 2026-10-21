// Discord login helpers (implicit grant, scope "identify": we only ever learn the user's id
// and name). Shared by background.js and the tests; the panel has its own copy of the same
// two steps because it is served by the server, not by the extension.
(function (root) {
  // Example: authorizeUrl('https://discord.com/oauth2/authorize', '123', 'https://abc.chromiumapp.org/', 'x7')
  // -> https://discord.com/oauth2/authorize?client_id=123&response_type=token&scope=identify&redirect_uri=...&state=x7
  function authorizeUrl(base, clientId, redirectUri, state) {
    const q = new URLSearchParams({ client_id: clientId, response_type: 'token', scope: 'identify',
                                    redirect_uri: redirectUri, state });
    return `${base}?${q}`;
  }

  // Discord sends the token back in the fragment:
  //   https://abc.chromiumapp.org/#token_type=Bearer&access_token=XYZ&expires_in=604800&scope=identify&state=x7
  // Returns {token, expiresAt} or an {error}. The state must match, so a login we didn't
  // start can't be slipped in.
  function parseRedirect(url, expectedState, nowMs = Date.now()) {
    const hash = String(url).split('#')[1] || '';
    const p = new URLSearchParams(hash);
    if (p.get('error')) return { error: p.get('error_description') || p.get('error') };
    if (p.get('state') !== expectedState) return { error: 'login state did not match; try again' };
    const token = p.get('access_token');
    if (!token) return { error: 'no token in Discord\'s answer' };
    const seconds = Number(p.get('expires_in')) || 3600;
    return { token, expiresAt: nowMs + seconds * 1000 };
  }

  function newState() {
    const a = new Uint8Array(16);
    (root.crypto || globalThis.crypto).getRandomValues(a);
    return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
  }

  const api = { authorizeUrl, parseRedirect, newState };
  if (root) root.CostreamDiscord = api;
  if (typeof window === 'undefined' && typeof importScripts === 'undefined' && typeof module === 'object' && module) {
    module.exports = api;   // Node (tests)
  }
})(typeof self !== 'undefined' ? self : typeof globalThis !== 'undefined' ? globalThis : this);
