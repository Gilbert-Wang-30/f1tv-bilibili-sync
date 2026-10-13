// Audio engine. Lives in an offscreen document so it isn't subject to the F1 TV
// page's mixed-content/CSP rules, and keeps playing while the tab is fullscreen.
'use strict';

const C = globalThis.FoneCore;
const audio = document.getElementById('a');

const S = {
  gen: 0,
  key: null, tab: null, cid: null, channel: null, channels: [],
  servers: [], base: null,
  content: null, contentAt: 0, clock: null, clockFinal: false,
  delay: null, delaySource: null, saveTimer: null,
  manualBase: null,
  timeline: null, timelineAt: 0, timelineTimer: null,
  hls: null, playlist: null,
  video: null, volume: 1, muted: false,
  steer: C.newSteer(), resync: true, preSeekAt: 0, lastTarget: null, nominal: null,
  fixes: { seeks: [], rates: [], speeds: [], nSeeks: 0, nRates: 0, nSpeeds: 0 }, // audible corrections (Details, e2e)
  error: null, lastReport: 0, lastState: null,
  dashDelay: null, dashDelayAt: 0, dashAt: 0, // stats dashboard clock (dashClock)
};

const now = () => Date.now() / 1000;

// ---- server ------------------------------------------------------------------

async function pickServer(servers) {
  for (const base of servers) {
    try {
      const r = await fetch(base.replace(/\/$/, '') + '/api/health', { signal: AbortSignal.timeout(2000) });
      if (r.ok) return base.replace(/\/$/, '');
    } catch { /* try next */ }
  }
  throw new Error('fone server unreachable — check the extension options');
}

async function api(path, opts = {}) {
  const r = await fetch(S.base + path, { signal: AbortSignal.timeout(10000), ...opts });
  if (!r.ok) throw new Error(`${path.split('?')[0]} → HTTP ${r.status}`);
  return r.json();
}

// ---- attach / detach -----------------------------------------------------------

function detach() {
  S.gen++;
  FoneLivePos.stop();
  clearTimeout(S.timelineTimer);
  if (S.hls) { S.hls.destroy(); S.hls = null; }
  audio.pause();
  audio.removeAttribute('src');
  Object.assign(S, { liveLock: null, minBehind: undefined, synced: false, dashDelay: null, dashDelayAt: 0, dashAt: 0,
    key: null, content: null, clock: null, clockFinal: false, delay: null, timeline: null, playlist: null,
    error: null, lastTarget: null, manualBase: null, steer: C.newSteer(), resync: true, nominal: null });
}

async function attach(msg, key) {
  detach();
  const gen = S.gen;
  Object.assign(S, { key, tab: msg.tab, cid: msg.cid, channel: msg.channel || null, servers: msg.settings.servers });
  try {
    S.base = await pickServer(S.servers);
    S.channels = await api('/api/channels');
    if (!S.channel) S.channel = S.channels[0]?.name ?? null;
    if (!S.channel) throw new Error('No recordings found on the server yet');
    await loadContent(gen);
    if (gen !== S.gen) return;
    await refreshTimeline(gen);
  } catch (e) {
    if (gen === S.gen) S.error = e.message || String(e);
  }
}

async function loadContent(gen) {
  const content = await api(`/api/f1tv/${S.cid}`);
  if (gen !== S.gen) return;
  S.content = content;
  S.contentAt = now();
  syncLivePos();
  S.clockFinal = false;
  await decideClock(gen);
}

// Live clocks depend on the player's seekable range, which only exists once the
// video has loaded — keep re-deciding until it has, then stick with the answer.
async function decideClock(gen) {
  if (S.deciding) return; // messages arrive at 4 Hz; one decision at a time
  S.deciding = true;
  try { await decideClockOnce(gen); } finally { S.deciding = false; }
}

async function decideClockOnce(gen) {
  const v = S.video;
  const clock = C.chooseClock(S.content, v, now());
  if (clock !== S.clock) {
    const sync = await api(`/api/sync/${S.cid}?channel=${encodeURIComponent(S.channel)}&clock=${clock}`);
    if (gen !== S.gen) return;
    S.clock = clock;
    S.sync = sync;
    S.delay = sync.source === 'measured' ? measuredDelay(sync) : sync.delay;
    S.delaySource = sync.source;
    if (clock === 'edge' && sync.source !== 'content') {
      // Replay calibrations measure lag behind real events; live works edge to edge.
      S.delay = C.LIVE_EDGE_DELAY;
      S.delaySource = 'live';
    }
    if (clock === 'edge') refreshDashDelay(gen);
    S.manualBase = null;
    S.resync = true;
  }
  // Final once the player has told us enough: a UTC position, a replay, or a live
  // edge. Never from the label fallback (it can't tell live edges). An item F1 TV lists as
  // LIVE isn't settled until the player says it plays it live (or as the replay it became):
  // its first reports come before the player knows, and 2026-10-10 a Mac stuck on the
  // replay's anchored clock for a whole live qualifying (seeking every 1.5 s at the edge).
  const listedLive = S.content?.subtype === 'LIVE';
  S.clockFinal = !!(v && v.ready && !String(v.src || '').startsWith('label')
    && (v.p > 1e9 || v.seekEnd != null || (!v.live && !listedLive)));
}

// A measured delay is the streamer's screen delay (the recording's F1 TV sound then coincides
// with the picture: no echo against F1 TV's own sound, lip-sync for pass-through interviews)
// plus, when asked for, their reaction time, so the commentary runs a little ahead of the
// action instead of after it.
function measuredDelay(sync) {
  const screen = Number.isFinite(sync.screen) ? sync.screen : sync.delay - (sync.reaction || 0);
  return Math.round((screen + (S.anticipate ? sync.reaction || 0 : 0)) * 1000) / 1000;
}

// The player can switch between content seconds and UTC (live wall clock appearing or
// a fallback); mapping one through the other's clock lands decades off.
const domainMismatch = (v) => S.clock != null && S.clock !== 'edge' && (v.p > 1e9) !== (S.clock === 'epoch');

function timelineWindow() {
  const t = now();
  const c = S.content;
  if (S.clock === 'multiviewer' || S.clock === 'preroll') {
    const from = c.p0 + S.delay - 2 * 3600;
    const to = c.p0 + (c.duration || 4 * 3600) + S.delay + 2 * 3600;
    return [from, Math.max(to, Math.min(t + 3600, to + 6 * 3600))];
  }
  return [t - 30 * 3600, t + 3600];
}

async function refreshTimeline(gen) {
  clearTimeout(S.timelineTimer);
  try {
    // Live sessions get upgraded to the exact anchor once MultiViewer publishes it.
    if (S.content && S.content.anchor !== 'multiviewer' && now() - S.contentAt > 600) await loadContent(gen);
    else if (S.content && !S.clockFinal) await decideClock(gen);
    const [from, to] = timelineWindow();
    const tl = await api(`/api/timeline?channel=${encodeURIComponent(S.channel)}&from=${from}&to=${to}`);
    if (gen !== S.gen) return;
    tl.receivedAt = now();
    S.timeline = tl;
    S.error = null;
    if (S.clock === 'manual' && S.manualBase == null && tl.pieces.length) {
      // Nothing to anchor on: start the video at the beginning of the newest recording.
      const last = tl.files.filter((f) => f.inPlaylist).pop();
      S.manualBase = (last ? last.wallStart : tl.pieces[0].w) - S.delay;
    }
    if (tl.playlist !== S.playlist) loadPlaylist(tl.playlist);
  } catch (e) {
    if (gen === S.gen) S.error = e.message || String(e);
  }
  if (gen === S.gen) {
    const live = S.timeline && (S.timeline.live || !S.timeline.complete);
    S.timelineTimer = setTimeout(() => refreshTimeline(gen), live || !S.clockFinal ? 3000 : 60000);
  }
}

function loadPlaylist(path) {
  S.playlist = path;
  if (S.hls) { S.hls.destroy(); S.hls = null; }
  if (!path) return;
  const hls = new Hls({
    enableWorker: false, // MV3 extension pages can't spawn blob: workers
    startPosition: S.lastTarget ?? 0,
    maxBufferLength: 20,
    maxMaxBufferLength: 40,
    backBufferLength: 30,
    liveDurationInfinity: true,
    manifestLoadingMaxRetry: 6,
    levelLoadingMaxRetry: 6,
    fragLoadingMaxRetry: 6,
  });
  hls.on(Hls.Events.ERROR, (_, data) => {
    const t = now();
    if (t - (DG.hlsAt.get(data.details) || 0) > 1) {
      DG.hlsAt.set(data.details, t);
      diag('hls', { kind: data.type, details: data.details, fatal: !!data.fatal, at: Math.round(audio.currentTime * 10) / 10 });
    }
    if (!data.fatal) return;
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
    else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
    else setTimeout(() => { if (S.hls === hls) loadPlaylist(path); }, 2000);
  });
  hls.loadSource(S.base + path);
  hls.attachMedia(audio);
  S.hls = hls;
  S.resync = true;
}

// ---- live car positions (E3) ---------------------------------------------------
// LIVE content only: livepos.js relays F1's car positions from the viewer's own F1 TV login
// to the server (the dashboard's track map). The token never passes through here: livepos
// asks the background for it right before each connection, and sends it to F1 only.
const f1Token = () => chrome.runtime.sendMessage({ target: 'background', type: 'f1-token' });

function syncLivePos() {
  const c = S.content;
  if (S.base && c?.subtype === 'LIVE' && c.sessionKey) FoneLivePos.start({ sessionKey: c.sessionKey, server: S.base, getToken: f1Token });
  else FoneLivePos.stop();
}

// For Details: is the track map being fed? (status() never holds the token.)
function livePosBrief() {
  const p = FoneLivePos.status();
  return p.state === 'idle' ? null : { state: p.state, text: p.text, relayed: p.relayed, error: p.lastError };
}

// ---- sync loop -----------------------------------------------------------------

function pauseAudio() {
  if (!audio.paused) audio.pause();
  S.steer = C.holdSteer(S.steer); // re-acquire when the video plays again
}

// F1 TV is paused or seeking: line the paused commentary up now (silently), so it is
// buffered and ready when the picture resumes instead of seeking after it.
function preSeek(m) {
  const t = performance.now();
  if (audio.seeking || Math.abs(audio.currentTime - m) < 0.5 || t - S.preSeekAt < 400) return;
  audio.currentTime = m;
  S.preSeekAt = t;
}

const ranges = (tr) => Array.from({ length: tr.length }, (_, i) => [tr.start(i), tr.end(i)]);

// See FoneCore.steer: the audio runs free and is only corrected when it is really off.
function drive(target, rate, src) {
  const nominal = C.nominalRate(rate);
  // rate is the speed someone chose (FoneCore.chosenRate), never F1 TV's trim; following a
  // change of it is counted apart from the corrections. It reaches us a few hundred ms after
  // the picture changed speed, and off 1× nothing nudges the audio back (only seeks past
  // 0.3 s), so re-lock right away: one seek at the button press beats trailing by ~0.25 s.
  const speedChange = S.nominal != null && nominal !== S.nominal;
  S.nominal = nominal;
  const { st, rate: r, seek } = C.steer(S.steer, {
    t: performance.now() / 1000,
    m: target,
    rate: nominal,
    audio: audio.currentTime,
    paused: audio.paused,
    seeking: audio.seeking || (!audio.paused && audio.readyState < 3),
    coarse: C.isCoarse(src),
    precise: C.isPrecise(src),
    resync: S.resync || speedChange,
    buffered: ranges(audio.buffered),
  });
  S.steer = st;
  S.resync = false;
  // At 1× the gentle corrections go through Chrome's resampler (a brief, slight pitch
  // shift) instead of WSOLA, which clicks every time the rate changes. Pitch is only
  // preserved for real speed changes. (Flipping this also clicks, so only on change.)
  const pp = nominal !== 1;
  if (audio.preservesPitch !== pp) audio.preservesPitch = pp;
  if (speedChange) {
    countFix('speeds');
    diag('speed', { to: nominal, src });
  }
  if (seek != null) {
    diag('seek', { from: Math.round(audio.currentTime * 100) / 100, to: Math.round(seek * 100) / 100,
      target: Math.round(target * 100) / 100, rate: nominal, src, ahead: audioAhead() });
    audio.currentTime = seek;
    countFix('seeks');
  }
  if (audio.playbackRate !== r) {
    audio.playbackRate = r;
    if (!speedChange) countFix('rates');
  }
  if (audio.paused) audio.play().catch((e) => { S.error = `audio: ${e.message}`; });
}

// The engine's seeks (each one clicks), rate changes (resampled now, so silent; v0.2.1's
// clicked) and the speed changes it followed (pitch kept off 1×, so each one clicks; v0.2.6
// took F1 TV's trims for them), counted for Details and the e2e test.
const FIX_TOTALS = { seeks: 'nSeeks', rates: 'nRates', speeds: 'nSpeeds' };
function countFix(kind) {
  const f = S.fixes;
  f[kind].push(now());
  f[FIX_TOTALS[kind]]++;
}

function fixStats() {
  const f = S.fixes, t = now();
  for (const k of Object.keys(FIX_TOTALS)) while (f[k].length && t - f[k][0] > 60) f[k].shift();
  return { seeks: f.seeks.length, rates: f.rates.length, speeds: f.speeds.length, nSeeks: f.nSeeks, nRates: f.nRates,
    nSpeeds: f.nSpeeds, rate: audio.playbackRate, pp: audio.preservesPitch };
}

// The commentary's moment, for the danmaku the tab scrolls over the picture (danmaku.js): the
// wall time the audio playing now went out at, so they move with what you hear (paused,
// seeked, at speed); at `at` (unix s), advancing at `rate` while `run`.
function danmakuClock() {
  const pieces = S.timeline?.pieces;
  const w = pieces ? C.wallOf(pieces, audio.currentTime) : null;
  if (w == null) return null;
  return { w, at: Date.now() / 1000, rate: audio.playbackRate, run: !audio.paused && !audio.seeking && audio.readyState >= 3 };
}

function report(st) {
  st.dm = danmakuClock();
  const tw = now();
  if (st.state !== S.lastState) diag('state', { from: S.lastState, to: st.state, text: st.text });
  if (tw - DG.snapAt >= DIAG.SNAP) { DG.snapAt = tw; diagSnap(st); }
  flushDiag(tw);
  const t = performance.now();
  if (st.state === S.lastState && t - S.lastReport < 250) return;
  S.lastState = st.state;
  S.lastReport = t;
  chrome.runtime.sendMessage({ target: 'background', type: 'status', tab: S.tab, status: st }).catch(() => {});
}

function fileTitle(idx) {
  const f = S.timeline?.files?.[idx];
  return f ? f.title || f.name : null;
}

// ---- live: lock once, then follow F1 TV's playhead ----------------------------
// Both streams are live and both advance in real time, so after one alignment the
// commentary position is m0 + (p - p0): it moves only when you seek F1 TV or nudge.
// No per-tick re-derivation from F1 TV's live edge (it advances in segment-sized steps)
// or from the server's wall-clock mapping (delivery jitter) — those made it chase.
const LIVE = { LAG_DEFAULT: C.DEFAULT_LIVE_LATENCY - C.LIVE_EDGE_DELAY, IN: 0.4, OUT: 1.0 };
const EDGE_STEP = 6; // s: F1 TV's live edge advances in 5.76 s steps

function commentaryEdge(t) {
  const tl = S.timeline;
  if (!tl?.live) return null;
  const last = tl.pieces[tl.pieces.length - 1];
  if (!last) return null;
  return last.m + last.d + Math.min(3, t - tl.receivedAt); // grows in real time between refreshes
}

function liveTarget(t, vn) {
  const L = S.liveLock;
  // F1 TV's natural distance behind its own edge (its target latency); anything beyond
  // that is you having rewound. Learned as the smallest distance seen this session, from
  // the edges the player reports (none: taken to be at its edge, rewound by nothing). The
  // player reports that edge in 5.76 s steps (so 'behind' saws by that much): only a
  // distance clearly past one step counts as rewound.
  const behind = vn.seekEnd != null ? Math.max(0, vn.seekEnd - vn.p) : null;
  if (behind != null) S.minBehind = Math.min(S.minBehind ?? behind, behind);
  if (L && L.cid === S.cid && !(L.relock)) return L.m0 + (vn.p - L.p0);
  const edge = commentaryEdge(t);
  if (edge == null) return null; // commentary not live: caller falls back to the wall mapping
  const lag = C.DEFAULT_LIVE_LATENCY - S.delay; // delay ↔ seconds behind the commentary edge
  const rewound = behind == null ? 0 : Math.max(0, behind - S.minBehind - EDGE_STEP);
  const m0 = edge - lag - rewound;
  S.liveLock = { cid: S.cid, m0, p0: vn.p, at: t };
  return m0;
}

// ---- diagnostics -------------------------------------------------------------------
// What playback looked like, for debugging on machines nobody is watching over (a Mac after
// it slept, glitches at 2×): a snapshot every 5 s plus the notable moments (seeks, speed and
// state changes, audio stalls, the computer waking up), posted to the fone server
// (POST /api/diag, read back with GET /api/diag). Playback state only: no token, no cookie.
const DIAG = { SNAP: 5, FLUSH: 5, MAX: 300 };
const diagQ = [];
const DG = { snapAt: 0, flushAt: 0, skew: null, tick: null, hlsAt: new Map(), sent: 0, err: null, sdkSeen: new Set(), fatalSeen: new Set() };
let diagClient = null;
try { // offscreen documents have no chrome.storage; their localStorage is the extension's
  diagClient = localStorage.getItem('diagClient');
  if (!diagClient) localStorage.setItem('diagClient', (diagClient = `c${Math.random().toString(36).slice(2, 10)}`));
} catch { diagClient = 'anon'; }
const platform = navigator.userAgentData?.platform || navigator.platform || '';
// The build, for the log. (Offscreen documents get only part of chrome.runtime: no getManifest.)
let buildVersion = '';
fetch('manifest.json').then((r) => r.json()).then((m) => { buildVersion = m.version || ''; }).catch(() => {});

function diag(type, data) {
  diagQ.push({ t: Math.round(now() * 1000) / 1000, type, ...data });
  if (diagQ.length > DIAG.MAX) diagQ.splice(0, diagQ.length - DIAG.MAX);
}

function flushDiag(t) {
  if (!S.base || !diagQ.length || t - DG.flushAt < DIAG.FLUSH) return;
  DG.flushAt = t;
  const events = diagQ.splice(0, diagQ.length);
  let body;
  try {
    body = JSON.stringify({ client: diagClient, build: buildVersion, platform, events });
  } catch (e) { // never let the log break playback
    DG.err = `encode: ${e.message || e}`;
    return;
  }
  fetch(`${S.base}/api/diag`, {
    method: 'POST', credentials: 'omit', signal: AbortSignal.timeout(8000),
    headers: { 'content-type': 'application/json' }, body,
  }).then((r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    DG.sent += events.length;
    DG.err = null;
  }).catch((e) => {
    DG.err = e.message || String(e);
    diagQ.unshift(...events.slice(-100)); // the newest go again next time
  });
}

// A computer that slept: the wall clock jumped, the monotonic one (macOS) didn't move.
function diagClocks(t) {
  const mono = performance.now() / 1000;
  const skew = t - mono;
  if (DG.skew != null && Math.abs(skew - DG.skew) > 3) diag('wake', { slept: Math.round(skew - DG.skew) });
  if (DG.tick != null && mono - DG.tick > 3) diag('gap', { s: Math.round(mono - DG.tick) });
  DG.skew = skew;
  DG.tick = mono;
}

function audioAhead() {
  const b = audio.buffered, c = audio.currentTime;
  for (let i = 0; i < b.length; i++) if (c >= b.start(i) - 0.1 && c <= b.end(i)) return Math.round((b.end(i) - c) * 10) / 10;
  return 0;
}

// The moments the snapshots miss: F1 TV's player changing state (playing → internally-stopped:
// the player died), its audio track changing (fone's Track FX switch, or the user's menu) and
// what came of it, a rebuilt pipeline (new MSE handle), the onboard views coming and going.
// Each with the position and state around it, so a crash can be put next to its trigger.
function diagPlayer(prev, v) {
  const a = v.dx?.a, b = prev?.dx?.a;
  const r = v.dx?.r, q = prev?.dx?.r;
  const at = { p: Math.round((v.p || 0) * 10) / 10, ps: r?.ps ?? null, h: v.dx?.h ?? null, nv: v.dx?.nv ?? null };
  if (prev && q && r && q.ps !== r.ps) diag('pstate', { from: q.ps, to: r.ps, ...at, want: r.want, sdk: r.sdk, err: r.err, audio: a?.cur ?? null });
  if (a && (a.v !== b?.v || a.cur !== b?.cur || a.err !== b?.err)) {
    diag('audio', { from: b?.cur ?? null, to: a.cur, kind: a.kind, auto: a.auto, manual: a.manual, on: a.on, fx: a.fx, err: a.err, ...at });
  }
  if (prev && v.dx?.h != null && prev.dx?.h != null && v.dx.h !== prev.dx.h) diag('handle', { from: prev.dx.h, to: v.dx.h, ...at, audio: a?.cur ?? null, want: r?.want ?? null });
  const obs = (x) => (x?.dx?.ob ? x.dx.ob.map((o) => `${o.ch ?? '-'}:${o.state}`).join(',') : '');
  if (prev && obs(prev) !== obs(v)) diag('ob', { from: obs(prev), to: obs(v), ...at, err: v.dx?.obErr ?? null });
  // page.js's SDK call log and the core's fatal errors come with every report: each once
  const sdk = v.dx?.sdk;
  if (sdk) {
    for (const e of sdk.log || []) {
      const k = `${e.t}|${e.name}`;
      if (e.ms == null || DG.sdkSeen.has(k)) continue; // in flight, or sent
      DG.sdkSeen.add(k);
      diag('sdk', { ...e, pid: sdk.pid });
    }
    for (const e of sdk.fatal || []) {
      if (DG.fatalSeen.has(e.t)) continue;
      DG.fatalSeen.add(e.t);
      diag('fatal', { ...e, pid: sdk.pid });
    }
    if (DG.sdkSeen.size > 500) DG.sdkSeen = new Set([...DG.sdkSeen].slice(-200));
    if (prev?.dx?.sdk && prev.dx.sdk.pid !== sdk.pid) diag('player', { from: prev.dx.sdk.pid, to: sdk.pid, ...at });
    if (prev?.dx?.sdk && !prev.dx.sdk.dead && sdk.dead) diag('dead', { ...at, pid: sdk.pid });
  }
}

function diagSnap(st) {
  const v = S.video || {};
  diag('snap', {
    state: st.state, text: st.text, clock: st.clock, delay: st.delay, cid: st.cid, drift: st.drift ?? null,
    nominal: S.nominal, fixes: st.fixes ?? null, err: st.error ?? null,
    audio: { paused: audio.paused, rs: audio.readyState, rate: audio.playbackRate, pp: audio.preservesPitch,
      ct: Math.round(audio.currentTime * 10) / 10, ahead: audioAhead() },
    video: { p: v.p, rate: v.rate, paused: v.paused, waiting: v.waiting, src: v.src, live: v.live, seekEnd: v.seekEnd ?? null,
      dx: v.dx ?? null },
  });
}

// ---- stats dashboard clock ---------------------------------------------------------
// The dashboard (dashboard.html, beside F1 TV in its tab or in its own window) shows the
// timing data as of the moment on screen, E (real-world UTC). Replays know E exactly. Live,
// the edge clock only guesses F1 TV's latency, and a guess that's too short puts lap times
// on the dashboard before the car crosses the line on screen. But live, the picture is held
// in step with the commentary, whose real-world time is known: the wall time it went out
// at, minus the streamer's delay. Showing data a little late beats spoiling it, so hold back
// a bit more than the delay's error: 2 s once it's measured off this stream, 5 s on the
// channel's last calibration (that moved by 4 s from one day to the next). Without commentary
// in step, fall back to a long latency.
const DASH = { EVERY: 0.2, LIVE_LATENCY: 50, SAFETY_MEASURED: 2, SAFETY: 5, DELAY_TTL: 300 };

async function refreshDashDelay(gen) {
  S.dashDelayAt = now();
  try { // the streamer's delay behind real events (measured off their screen, or calibrated)
    const sync = await api(`/api/sync/${S.cid}?channel=${encodeURIComponent(S.channel)}&clock=multiviewer`);
    if (gen !== S.gen) return;
    S.dashDelay = sync.delay + (sync.source === 'measured' ? DASH.SAFETY_MEASURED : DASH.SAFETY);
  } catch { /* keep the last one */ }
}

function dashClock(t, v, vn, E, liveM) {
  if (t - S.dashAt < DASH.EVERY || S.content?.sessionKey == null) return;
  S.dashAt = t;
  let e = E;
  if (S.clock === 'edge') {
    if (t - S.dashDelayAt > DASH.DELAY_TTL) refreshDashDelay(S.gen);
    const w = liveM != null && S.synced && S.dashDelay != null ? C.wallOf(S.timeline?.pieces, liveM) : null;
    e = w != null ? w - S.dashDelay : t - DASH.LIVE_LATENCY - Math.max(0, (vn.seekEnd ?? vn.p) - vn.p);
  }
  if (!Number.isFinite(e)) return;
  chrome.runtime.sendMessage({
    target: 'dashboard', type: 'clock', tab: S.tab, E: e, playing: !v.paused, rate: v.rate || 1, at: t,
    sessionKey: S.content.sessionKey, cid: S.cid, server: S.base, live: S.clock === 'edge',
  }).catch(() => {});
}

function tick() {
  if (!S.key || !S.video) return;
  const t = now();
  diagClocks(t);
  const v = S.video;
  const base = {
    channel: S.channel, channels: S.channels.map((c) => c.name), server: S.base,
    clock: S.clock, delay: S.delay, delaySource: S.delaySource, cid: S.cid,
    title: S.content?.title, session: S.content?.sessionName, anchor: S.content?.anchor,
    playing: !v.paused && t - v.receivedAt <= 4, // the picture is moving (self-update waits)
    diag: { q: diagQ.length, sent: DG.sent, err: DG.err },
    livepos: livePosBrief(),
  };
  if (t - v.receivedAt > 4) { // tab closed/frozen, or the player stopped giving a position
    pauseAudio();
    return report({ ...base, state: 'loading', text: 'Waiting for the F1 TV player…' });
  }
  if (!S.timeline || !S.content || S.delay == null) {
    pauseAudio();
    return report({ ...base, state: S.error ? 'error' : 'loading', text: S.error || 'Connecting…' });
  }
  audio.volume = S.muted ? 0 : S.volume;

  if (domainMismatch(v)) {
    pauseAudio();
    return report({ ...base, state: 'syncing', text: 'Re-syncing…' });
  }
  const vn = C.videoNow(v, t);
  const E = C.eventTime(S.clock, S.content, vn.p, vn.seekEnd, t, { manualBase: S.manualBase });
  const W = E + S.delay;
  let map = C.mapWall(S.timeline.pieces, W);
  const liveM = S.clock === 'edge' ? liveTarget(t, vn) : null;
  dashClock(t, v, vn, E, liveM);
  if (S.clock === 'edge') {
    const m = liveM;
    if (m != null) {
      const edge = commentaryEdge(t);
      if (edge != null && m > edge - 1) {
        pauseAudio();
        Object.assign(base, { E, W, lead: edge - 2.5 - m, error: S.error, fixes: fixStats() });
        return report({ ...base, state: 'waiting', text: `Commentary ${Math.ceil(m - edge + 1)}s behind` });
      }
      const first = S.timeline.pieces[0];
      map = first && m < first.m
        ? { state: 'before', wait: first.m - m }
        : { state: 'ok', m, file: S.timeline.pieces.length ? S.timeline.pieces[S.timeline.pieces.length - 1].f : 0 };
    }
  }
  // How much commentary exists ahead of the playhead (live only) — drives auto-hold.
  const edge = S.timeline.live && S.timeline.edge != null ? S.timeline.edge + (t - S.timeline.receivedAt) : null;
  const mEdge = S.clock === 'edge' && map.state === 'ok' ? commentaryEdge(t) : null;
  const lead = mEdge != null ? mEdge - 2.5 - map.m
    : edge != null ? edge - 2.5 - W : null; // 2.5 s: segment + refresh slack
  Object.assign(base, { E, W, lead, error: S.error, fixes: fixStats() });

  if (map.state !== 'ok') {
    pauseAudio();
    if (map.state === 'after' && S.timeline.live) {
      return report({ ...base, state: 'waiting', text: `Commentary ${Math.ceil(map.past)}s behind` });
    }
    const text = {
      none: 'No recording covers this session',
      before: `Commentary starts in ${fmtDur(map.wait)}`,
      gap: `Stream dropped — back in ${fmtDur(map.wait)}`,
      after: 'Commentary recording ended',
    }[map.state];
    return report({ ...base, state: map.state, text });
  }

  S.lastTarget = map.m;
  base.file = fileTitle(map.file);
  if (v.paused || v.seeking || v.waiting) {
    pauseAudio();
    if (!v.waiting) preSeek(map.m);
    return report({ ...base, state: v.paused ? 'paused' : 'buffering', text: v.paused ? 'Paused' : 'Buffering…' });
  }
  drive(map.m, v.rate || 1, v.src);
  const drift = audio.currentTime - map.m;
  Object.assign(base, { m: map.m, audioTime: audio.currentTime });
  const ok = !audio.paused && !audio.seeking && audio.readyState >= 3;
  // Hysteresis so the status doesn't flicker between "In sync" and "Syncing…".
  const a = Math.abs(drift);
  S.synced = ok && (S.synced ? a < LIVE.OUT : a < LIVE.IN);
  const liveText = S.clock === 'edge' && S.liveLock ? 'Live' : 'In sync';
  report({ ...base, state: S.synced ? 'sync' : 'syncing', text: S.synced ? liveText : 'Syncing…', drift });
}

setInterval(tick, 100);

function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

// ---- messages ------------------------------------------------------------------

function nudge(delta) {
  if (S.delay == null) return;
  S.delay = Math.round((S.delay + delta) * 100) / 100;
  S.delaySource = 'content';
  S.resync = true; // jump to the new position now
  if (S.liveLock) S.liveLock.m0 += delta; // live: move the lock, don't re-derive it
  clearTimeout(S.saveTimer);
  const { cid, channel, clock, delay } = S;
  S.saveTimer = setTimeout(() => {
    api(`/api/sync/${cid}`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ channel, clock, delay }),
    }).catch((e) => { S.error = `save failed: ${e.message}`; });
  }, 800);
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.target !== 'offscreen') return;
  if (msg.type === 'relive') { // "Re-sync to live": re-anchor to the commentary's live edge
    if (S.liveLock) S.liveLock.relock = true;
    S.minBehind = undefined;
    S.resync = true;
    return;
  }
  if (msg.type === 'video') {
    // Several F1 TV tabs: follow the one that's playing; don't flap between them.
    if (S.tab != null && msg.tab !== S.tab && S.video) {
      const currentAlive = now() - S.video.receivedAt < 2;
      const currentPlaying = currentAlive && !S.video.paused;
      if (currentPlaying || (currentAlive && msg.video.paused)) return;
    }
    const prev = S.video;
    S.video = { ...msg.video, receivedAt: now() };
    diagPlayer(prev, S.video);
    if (S.content && domainMismatch(S.video)) { S.clockFinal = false; decideClock(S.gen).catch(() => {}); }
    S.volume = msg.settings.volume;
    S.muted = msg.settings.muted;
    if (!!msg.settings.anticipate !== !!S.anticipate) {
      S.anticipate = !!msg.settings.anticipate;
      if (S.delaySource === 'measured' && S.sync) { S.delay = measuredDelay(S.sync); S.resync = true; }
    }
    const key = `${msg.tab}|${msg.cid}|${msg.channel || ''}|${msg.settings.servers.join(',')}`;
    if (key !== S.key) attach(msg, key);
    // Paused, seeked or resumed: act now rather than on the next 100 ms tick (a quick
    // F1 TV seek can come and go between two ticks, skipping the silent pre-seek).
    else if (prev && (prev.paused !== S.video.paused || prev.seeking !== S.video.seeking || Math.abs(prev.p - S.video.p) > 1)) tick();
  } else if (msg.type === 'nudge') {
    nudge(msg.delta);
  } else if (msg.type === 'detach' && (msg.tab == null || msg.tab === S.tab)) {
    detach();
  }
});

// Test hook (headless e2e): read engine state without going through the page.
globalThis.__fone = { S, audio };
