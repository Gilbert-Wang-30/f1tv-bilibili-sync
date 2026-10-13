// The F1 TV login's live-timing token, for livepos.js (car positions). Background service
// worker only: offscreen documents have no chrome.cookies.
//
// formula1.com keeps it in the `login-session` cookie on .formula1.com: URL-encoded JSON
// {"data": {"subscriptionToken": "<JWT>", ...}}, set to expire with the token. F1's own
// live-timing page reads it from there, as does FastF1's companion extension.
// It is a credential: handed to the caller and nothing else. Never logged, never stored,
// and livepos.js sends it to livetiming.formula1.com only.
(function (root) {
  'use strict';

  const COOKIE = 'login-session';
  const JWT = /^[\w-]+\.[\w-]+\.[\w-]*$/;
  const EARLY = 30; // seconds: a token about to expire would only fail mid-handshake

  // A JWT's claims, unverified: only read to know when it expires. null if not a JWT.
  function jwtClaims(token) {
    try {
      const b64 = String(token).split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      const claims = JSON.parse(atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4)));
      return claims && typeof claims === 'object' ? claims : null;
    } catch {
      return null;
    }
  }

  // Cookie value -> {token, exp} or null when it holds no usable token (missing, garbled,
  // expired). Tolerates a value that is encoded twice, or not at all.
  function parseLoginSession(value, nowSec = Date.now() / 1000) {
    let v = typeof value === 'string' ? value.trim().replace(/^"(.*)"$/, '$1') : ''; // cookies may be quoted
    for (let i = 0; v && i < 3; i++) {
      let obj = null;
      try { obj = JSON.parse(v); } catch { /* still URL-encoded */ }
      if (obj && typeof obj === 'object') {
        const token = obj.data?.subscriptionToken;
        if (typeof token !== 'string' || !JWT.test(token)) return null;
        const exp = Number(jwtClaims(token)?.exp);
        if (Number.isFinite(exp) && exp <= nowSec + EARLY) return null;
        return { token, exp: Number.isFinite(exp) ? exp : Infinity };
      }
      let next;
      try { next = decodeURIComponent(v); } catch { return null; }
      if (next === v) return null;
      v = next;
    }
    return null;
  }

  // The current token, or null (signed out, expired, no cookie access). Several copies of
  // the cookie can exist (www., account., .formula1.com): the one expiring last is freshest.
  async function f1Token() {
    let cookies;
    try {
      cookies = await chrome.cookies.getAll({ domain: 'formula1.com', name: COOKIE });
    } catch {
      return null; // no "cookies" permission or host access
    }
    let best = null;
    for (const c of cookies || []) {
      const t = parseLoginSession(c.value);
      if (t && (!best || t.exp > best.exp)) best = t;
    }
    return best ? best.token : null;
  }

  root.FoneF1Token = { f1Token, parseLoginSession, jwtClaims };
})(typeof globalThis !== 'undefined' ? globalThis : this);
