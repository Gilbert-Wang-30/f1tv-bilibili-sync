// Timing GETs for the stats dashboard when it runs as an iframe inside F1 TV (dashboard.js),
// and the F1 TV content's stream list for its onboard slots. Loaded by the service worker:
// importScripts('dashproxy.js') in background.js.
//
// There the page around the dashboard is https and the fone server plain http on the home
// network, so Chrome blocks the iframe's own requests as mixed content (all but those to a
// LAN IP literal, and the default server is a Tailscale name). The service worker has no
// page around it, and the extension's host permissions let it read the server directly.
'use strict';

// What the dashboard reads (and the danmaku over the picture, danmaku.js), and nothing else:
// not a general-purpose fetcher for whoever can message us.
const DASH_PATHS = [/^\/api\/timing\//, /^\/api\/f1tv\/\d+$/, /^\/api\/danmaku$/];

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.target !== 'dash-fetch' || sender.id !== chrome.runtime.id) return undefined;
  if (msg.ping) {
    sendResponse({ pong: true });
    return undefined;
  }
  let url;
  try {
    url = new URL(msg.url);
  } catch {
    sendResponse({ ok: false, error: 'bad URL' });
    return undefined;
  }
  if (!/^https?:$/.test(url.protocol) || !DASH_PATHS.some((p) => p.test(url.pathname))) {
    sendResponse({ ok: false, error: 'not a dashboard URL' });
    return undefined;
  }
  fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(8000) })
    .then(async (r) => sendResponse(r.ok ? { ok: true, json: await r.json() } : { ok: false, error: `${url.pathname} → HTTP ${r.status}` }))
    .catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
  return true; // the answer comes asynchronously
});

// Bilibili's sticker and emoji images for the danmaku over F1 TV (danmaku.js), as data: URLs:
// F1 TV's Content-Security-Policy only lets the page load images from formula1.com and data:,
// and Bilibili's CDN refuses other sites as referrer. Only that CDN, nothing else.
const DM_IMAGE_HOST = /(^|\.)hdslb\.com$/;
const dmImages = new Map(); // url -> data: URL (or a pending promise)

async function dmImage(url) {
  const r = await fetch(url, { referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(8000) });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const type = r.headers.get('content-type') || 'image/png';
  if (!type.startsWith('image/')) throw new Error(`not an image: ${type}`);
  const buf = new Uint8Array(await r.arrayBuffer());
  if (buf.length > 1 << 20) throw new Error('too big');
  let s = '';
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return `data:${type};base64,${btoa(s)}`;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.target !== 'dm-image' || sender.id !== chrome.runtime.id) return undefined;
  let url;
  try {
    url = new URL(msg.url);
    url.protocol = 'https:';
  } catch {
    sendResponse({ ok: false, error: 'bad URL' });
    return undefined;
  }
  if (!DM_IMAGE_HOST.test(url.hostname)) {
    sendResponse({ ok: false, error: 'not a Bilibili image' });
    return undefined;
  }
  const key = url.href;
  if (!dmImages.has(key)) {
    if (dmImages.size > 400) dmImages.delete(dmImages.keys().next().value);
    dmImages.set(key, dmImage(key).catch((e) => { dmImages.delete(key); throw e; }));
  }
  Promise.resolve(dmImages.get(key))
    .then((data) => sendResponse({ ok: true, data }), (e) => sendResponse({ ok: false, error: e.message || String(e) }));
  return true;
});
