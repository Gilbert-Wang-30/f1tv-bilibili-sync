// Relays between F1 TV tabs (content script) and the offscreen audio engine.
'use strict';

importScripts('dashproxy.js'); // the stats dashboard's timing reads (mixed content in F1 TV)
importScripts('f1token.js'); // the F1 TV token for live car positions (livepos.js)

let creating = null;

async function ensureOffscreen() {
  const existing = await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] });
  if (existing.length) return;
  // AUDIO_PLAYBACK alone gets the document closed after 30 s of silence (e.g. a
  // paused video); hls.js also needs blob URLs for MediaSource.
  creating ??= chrome.offscreen
    .createDocument({
      url: 'offscreen.html',
      reasons: ['AUDIO_PLAYBACK', 'BLOBS'],
      justification: 'Plays the recorded commentary in sync with F1 TV.',
    })
    .finally(() => { creating = null; });
  await creating;
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg?.target === 'engine' && sender.tab) {
    ensureOffscreen()
      .then(() => chrome.runtime.sendMessage({ ...msg, target: 'offscreen', tab: sender.tab.id }))
      .catch(() => {});
  } else if (msg?.target === 'background' && msg.type === 'status' && msg.tab != null) {
    chrome.tabs.sendMessage(msg.tab, { type: 'fone-status', status: msg.status }).catch(() => {});
  } else if (msg?.target === 'background' && msg.type === 'popout-dashboard') {
    // The stats in a window of their own (layout.js's fallback, e.g. for a second screen).
    chrome.windows.create({ url: chrome.runtime.getURL('dashboard.html'), type: 'popup', width: 560, height: 1000 });
  }
});

chrome.tabs.onRemoved.addListener((tab) => {
  chrome.runtime.sendMessage({ target: 'offscreen', type: 'detach', tab }).catch(() => {});
});

// The F1 TV token for live car positions (livepos.js in the offscreen document, which has no
// chrome.cookies). Only our own offscreen document gets it: never a content script or any
// other page, so nothing running on f1tv.formula1.com can ask for it.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.target !== 'background' || msg.type !== 'f1-token') return;
  if (sender.id !== chrome.runtime.id || sender.tab || sender.url !== chrome.runtime.getURL('offscreen.html')) {
    sendResponse(null);
    return;
  }
  FoneF1Token.f1Token().then(sendResponse, () => sendResponse(null));
  return true; // answered asynchronously
});

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());

// ---- self-update -------------------------------------------------------------------
// Dev installs are loaded unpacked from a network share (FONE_EXT_DEST in tools/publish.env);
// tools/publish_ext.sh writes new builds there, stamping each one into build.js (read once,
// when the extension loads) and build.json, which a tiny static server next to the fone
// server (fone-ext, port 8789) serves. When the served build differs from the running one,
// reload from disk and refresh F1 TV tabs so their content scripts match — but never while
// someone is watching (that waits until they pause or stop).
// The running build is never re-read from the share: Windows' SMB client keeps serving a
// file it re-opens every few seconds from its cache, and writes made on the NAS (NFS) don't
// invalidate it, so polling build.json from disk saw the old build forever.
try { importScripts('build.js'); } catch { /* loaded straight from the repo: no self-update */ }
importScripts('local.js'); // this install's default servers (tools/publish_ext.sh); none in the repo
const RUNNING = self.FONE_BUILD || null;
const DEFAULT_SERVERS = self.FONE_LOCAL?.servers || [];
const UPDATE_PORT = '8789';
// A new build must be served this long before we reload into it: on a Mac the files arrive
// by a launchd job that runs every minute (tools/mac/fone-sync.sh), so wait out one run.
const SETTLE_MS = 75000;
const RETRY_MS = 10 * 60 * 1000; // a reload that came back on the old build: try again later
const playingSeen = new Map(); // tabId -> last time the engine reported playback there

async function readBuild(url) {
  try {
    const r = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(4000) });
    return r.ok ? (await r.json()).build || null : null;
  } catch {
    return null;
  }
}

const watching = () => [...playingSeen.values()].some((t) => Date.now() - t < 15000);

async function publishedBuild() {
  const { servers } = await chrome.storage.sync.get({ servers: DEFAULT_SERVERS });
  for (const base of servers) {
    try {
      const u = new URL(base);
      u.port = UPDATE_PORT;
      u.pathname = '/build.json';
      // which build this browser runs, in the update server's request log (nothing else uses it)
      if (RUNNING) u.search = `?have=${encodeURIComponent(RUNNING)}`;
      const b = await readBuild(u.href);
      if (b) return b;
    } catch { /* bad URL in settings */ }
  }
  return null;
}

async function checkUpdate() {
  if (!RUNNING) return;
  const latest = await publishedBuild();
  if (!latest || latest === RUNNING) {
    await chrome.storage.local.remove('updateSeen');
    return;
  }
  // The service worker comes and goes between checks, so remember when we first saw it.
  const { updateSeen, lastReload } = await chrome.storage.local.get(['updateSeen', 'lastReload']);
  if (updateSeen?.build !== latest) {
    await chrome.storage.local.set({ updateSeen: { build: latest, at: Date.now() } });
    return;
  }
  if (Date.now() - updateSeen.at < SETTLE_MS || watching()) return;
  // Reloaded for this build already and still running an older one: the share served stale
  // files. Don't loop; try again in a while.
  if (lastReload?.build === latest && Date.now() - lastReload.at < RETRY_MS) return;
  await chrome.storage.local.set({ lastReload: { build: latest, at: Date.now() }, refreshF1tv: true });
  chrome.runtime.reload();
}

// After a self-reload (onInstalled isn't guaranteed for that), refresh F1 TV tabs once so
// their content scripts are the new build's.
chrome.storage.local.get('refreshF1tv').then(async ({ refreshF1tv }) => {
  if (!refreshF1tv) return;
  await chrome.storage.local.set({ refreshF1tv: false });
  for (const tab of await chrome.tabs.query({ url: 'https://f1tv.formula1.com/*' })) chrome.tabs.reload(tab.id);
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.target === 'background' && msg.type === 'status' && msg.tab != null) {
    if (msg.status?.playing) playingSeen.set(msg.tab, Date.now());
  }
});
chrome.tabs.onRemoved.addListener((tab) => playingSeen.delete(tab));

chrome.alarms.create('fone-update', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'fone-update') checkUpdate(); });
setInterval(checkUpdate, 10000);
checkUpdate();
