// The stats dashboard: timing tower, track map and track conditions for the moment on the
// F1 TV screen, and nothing else. Runs as an iframe behind F1 TV's shrunken player
// (?mode=iframe; layout.js puts it there and reports the player's box), or in a window of its
// own. The offscreen engine broadcasts the on-screen moment E about 5 times a second; every
// number shown comes from the fone server at E, never from later (spoiler-free).
// In the iframe it also holds two onboard slots: a driver picker each, while the page shows
// F1 TV's onboard views in them (the 'onboards' command to the page, below).
// Without the engine (debugging, tests): ?server=<url>&session=<key>&E=<utc>[&rate=1][&play=1][&cid=<F1 TV id>]
'use strict';

const D = globalThis.FoneDash;
const Q = new URLSearchParams(location.search);
const MODE = Q.get('mode') === 'iframe' ? 'iframe' : 'standalone';
const now = () => Date.now() / 1000;
const $ = (id) => document.getElementById(id);
const FONT = getComputedStyle(document.documentElement).getPropertyValue('--font').trim() || 'sans-serif';
// ?E=: a simulated clock instead of the engine's reports
const SIM = Q.has('E')
  ? { E: Number(Q.get('E')), at: now(), rate: Number(Q.get('rate')) || 1, playing: Q.get('play') === '1' }
  : null;
document.body.classList.add(MODE);

const S = {
  myTab: MODE === 'iframe' ? undefined : null, // iframe: the tab we're in (undefined until known)
  tabs: new Map(), // standalone: tab id -> {msg, rx}, every F1 TV tab that reports
  follow: null, // the tab whose player we show
  clock: null, // FoneDash clock: E now
  server: null, key: null, cid: null, sess: null, // the session shown; sess = `${server}|${key}`
  cids: new Map(), cidBusy: new Set(), // F1 TV content id -> session key (null: not a session)
  state: null, na: null, stale: false, err: '', failures: 0, gen: 0, inflight: null, timer: 0, countdown: null, freeze: false,
  track: null, trackTimer: 0, fit: null,
  chunks: new Map(), cars: {}, carsVer: 0, posReq: null,
  layoutMsg: null, L: null, rows: 22, sides: new Map(), drawn: '', ver: 0,
};

// ---- server ----------------------------------------------------------------------------
//
// In the F1 TV tab the page around us is https and the fone server plain http on the home
// network: Chrome blocks the iframe's requests to it as mixed content (an IP literal on the
// LAN gets through, the Tailscale name doesn't). The service worker has no page around it,
// so in iframe mode the GETs go through it (dashproxy.js, loaded by background.js). A window
// of our own fetches directly, like the options page.
const net = { proxy: false };
const netReady = (async () => {
  if (MODE !== 'iframe') return;
  try {
    net.proxy = !!(await chrome.runtime.sendMessage({ target: 'dash-fetch', ping: true }))?.pong;
  } catch { /* no proxy installed: fetch directly */ }
})();

async function getJSON(url, signal) {
  await netReady;
  if (net.proxy) {
    const r = await chrome.runtime.sendMessage({ target: 'dash-fetch', url });
    if (signal?.aborted) throw new DOMException('stale', 'AbortError');
    if (!r?.ok) throw new Error(r?.error || 'no answer from the service worker');
    return r.json;
  }
  const timeout = AbortSignal.timeout(8000);
  const opts = { cache: 'no-store', signal: signal ? AbortSignal.any([signal, timeout]) : timeout };
  // An iframe without the proxy can still reach a server Chrome knows is on the home network
  // (localhost is no mixed content to begin with).
  if (MODE === 'iframe' && !/^(localhost|127\.|\[::1\])/.test(new URL(url).host)) opts.targetAddressSpace = 'local';
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${new URL(url).pathname} → HTTP ${r.status}`);
  return r.json();
}

const api = (path) => `${S.server}/api/timing/session/${S.key}/${path}`;

// ---- the clock: which player we follow ------------------------------------------------

if (MODE === 'iframe') {
  // A click anywhere in a frame moves keyboard focus into it, and F1 TV's keys and fone's
  // hotkeys (content.js, on the top page) would stop working until the next click on the
  // player. Nothing here needs focus: cancel the click's focus move.
  document.addEventListener('mousedown', (e) => e.preventDefault(), true);
  // Only our own tab's player: the engine may be following F1 TV in another tab.
  Promise.resolve()
    .then(() => chrome.tabs.getCurrent())
    .then((tab) => { S.myTab = tab ? tab.id : null; })
    .catch(() => { S.myTab = null; });
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.target === 'dashboard' && msg.type === 'clock' && !SIM) onReport(msg);
});

function onReport(msg) {
  const t = now();
  if (MODE === 'iframe' && S.myTab !== null) {
    if (msg.tab !== S.myTab) return; // another tab's player, or our tab id isn't known yet
  } else {
    // A window of its own: follow one player, the one that plays (FoneDash.pickTab).
    S.tabs.set(msg.tab, { msg, rx: t });
    for (const [id, x] of S.tabs) if (t - x.rx > 60) S.tabs.delete(id);
    const pick = D.pickTab(S.tabs, S.follow, t);
    if (pick !== S.follow) {
      S.follow = pick;
      S.clock = null; // a different player: its E has nothing to do with the last one's
    }
    if (msg.tab !== S.follow) return;
  }
  follow(msg, t);
}

function follow(msg, t) {
  setSession(msg.server, msg.sessionKey, msg.cid);
  const { clock, jump, toggled } = D.clockStep(S.clock, msg, t);
  if (!clock) return;
  const first = !S.clock;
  S.clock = clock;
  if (first) renderNotes();
  if (jump) onJump();
  if (jump || toggled) pollNow();
  else if (S.sess && clock.playing && !S.timer && !S.inflight) pollNow(); // back after a silence
}

// A seek. Gone back: what's shown is from the future now, so hide it until the state at the
// new E lands; and the rows jump to their new places rather than slide there.
function onJump() {
  const E = D.clockAt(S.clock, now());
  S.countdown = null;
  S.freeze = true;
  if (S.state && S.state.t > E + 1) {
    S.state = null;
    renderState();
  }
}

// ---- the session ----------------------------------------------------------------------

function setSession(server, key, cid) {
  server = server ? String(server).replace(/\/+$/, '') : null;
  S.cid = cid || null;
  wantStreams(server, S.cid);
  if (key == null && cid && server) {
    const k = `${server}|${cid}`;
    if (S.cids.has(k)) key = S.cids.get(k);
    else resolveCid(server, cid, k);
  }
  const sess = server && key ? `${server}|${key}` : null;
  if (sess === S.sess && server === S.server) return;
  if (S.inflight) S.inflight.ctl.abort();
  clearTimeout(S.timer);
  clearTimeout(S.trackTimer);
  S.chunks.clear();
  Object.assign(S, {
    sess, server, key: sess ? key : null, gen: S.gen + 1, inflight: null, timer: 0, trackTimer: 0,
    state: null, na: null, stale: false, failures: 0, countdown: null, freeze: true,
    track: null, fit: null, cars: {}, carsVer: S.carsVer + 1, posReq: null, sides: new Map(),
  });
  renderState();
  drawTrack();
  if (sess) {
    loadTrack(sess);
    pollNow();
  }
}

// The engine knows the F1 TV content id; the server knows which timing session it shows.
async function resolveCid(server, cid, k) {
  if (S.cidBusy.has(k)) return;
  S.cidBusy.add(k);
  try {
    const c = await getJSON(`${server}/api/timing/content/${encodeURIComponent(cid)}`);
    S.cids.set(k, c?.sessionKey ?? null);
    S.cidBusy.delete(k);
  } catch {
    setTimeout(() => S.cidBusy.delete(k), 10000); // try again with a later report
  }
  renderNotes();
}

// ---- /state: about once a second while playing, at once on a seek, pause or resume -----

function pollNow() {
  S.gen++; // answers to older requests are dropped
  clearTimeout(S.timer);
  S.timer = setTimeout(pollState, 0);
}

async function pollState() {
  S.timer = 0;
  if (!S.sess || !S.clock || document.hidden) return;
  const gen = S.gen;
  if (S.inflight) {
    if (S.inflight.gen === gen) return; // one at a time: its answer schedules the next
    S.inflight.ctl.abort(); // asked for an E that's no longer on screen
  }
  const E = D.clockAt(S.clock, now());
  const req = { gen, ctl: new AbortController() };
  S.inflight = req;
  const started = performance.now();
  let ok = false;
  try {
    const st = await getJSON(api(`state?t=${E.toFixed(3)}`), req.ctl.signal);
    if (gen !== S.gen) return;
    ok = true;
    takeState(st);
  } catch (e) {
    if (gen !== S.gen) return;
    S.failures++;
    S.stale = true; // keep showing the last good state, marked
    S.err = e.message || String(e);
    renderCond();
    if (!S.state) renderNotes();
  } finally {
    if (S.inflight === req) S.inflight = null;
    if (gen === S.gen) scheduleNext(ok, performance.now() - started);
  }
}

function scheduleNext(ok, ms) {
  clearTimeout(S.timer);
  S.timer = 0;
  const c = S.clock;
  const moving = c?.playing && now() - c.at <= D.CLOCK.HOLD;
  if (ok && !moving) return; // paused (or the engine went quiet): the state at E stays put
  // 1 s of session time per poll, so a bit more often at 1.5x and 2x
  const every = D.clamp(1000 / (c?.rate || 1), 500, 1000);
  const wait = ok ? Math.max(50, every - ms) : Math.min(8000, 1000 * 2 ** Math.min(S.failures - 1, 3));
  S.timer = setTimeout(pollState, wait);
}

function takeState(st) {
  S.failures = 0;
  S.stale = false;
  S.err = '';
  if (!st || st.available === false) {
    Object.assign(S, { state: null, na: st?.reason || 'no data', countdown: null });
  } else {
    S.countdown = D.countdownStep(S.countdown, st);
    S.state = st;
    S.na = null;
    const rows = st.tower?.length || 0;
    if (rows && rows !== S.rows) {
      S.rows = rows;
      applyLayout();
    }
  }
  renderState();
}

// ---- /track and /positions --------------------------------------------------------------

async function loadTrack(sess, tries = 0) {
  let retry = 30;
  try {
    const tr = await getJSON(api('track'));
    if (sess !== S.sess) return;
    if (tr?.available !== false && Array.isArray(tr?.outline) && tr.outline.length > 2) {
      S.track = tr;
      // the map takes the width this track needs; the onboards the rest (FoneDash.onboardSlots)
      const aspect = D.trackAspect(tr.outline, tr.rotation);
      if (aspect !== S.aspect) {
        S.aspect = aspect;
        applyLayout(); // draws the track too
      } else drawTrack();
      // The server fetches a circuit's corner numbers on first ask: look again shortly.
      if (!tr.corners?.length && tries < 3) S.trackTimer = setTimeout(() => loadTrack(sess, tries + 1), 5000);
      return;
    }
  } catch {
    retry = 10;
  }
  if (sess === S.sess) S.trackTimer = setTimeout(() => loadTrack(sess), retry * 1000);
}

// Positions come in fixed 30 s chunks (FoneDash.chunksFor): the one holding E and the next,
// one request at a time. Live, a chunk near the edge is still filling up: refetched every
// few seconds while it's needed.
function needChunks(E) {
  if (!S.sess || S.posReq) return;
  const t = now();
  for (const k of D.chunksFor(E)) {
    const ch = S.chunks.get(k);
    if (ch && (ch.complete || t - ch.fetchedAt < (ch.failed ? 5 : 3))) continue;
    loadChunk(k);
    return;
  }
}

async function loadChunk(k) {
  const sess = S.sess, req = { sess, k };
  S.posReq = req;
  const from = k * D.POS.CHUNK;
  let ch;
  try {
    const r = await getJSON(api(`positions?from=${from}&to=${from + D.POS.CHUNK}`));
    const at = now();
    ch = { k, cars: r?.cars || {}, fetchedAt: at, complete: r?.available !== false && D.chunkComplete(k, at) };
  } catch {
    ch = { k, cars: {}, fetchedAt: now(), complete: false, failed: true };
  }
  if (S.posReq === req) S.posReq = null;
  if (sess !== S.sess) return;
  const old = S.chunks.get(k);
  if (ch.failed && old) { // keep what we had; try again in a moment
    Object.assign(old, { fetchedAt: ch.fetchedAt, failed: true });
    return;
  }
  S.chunks.set(k, ch);
  if (S.chunks.size > D.POS.KEEP) { // keep the ones nearest to where we are
    const here = S.clock ? D.chunkOf(D.clockAt(S.clock, now())) : k;
    const far = [...S.chunks.keys()].sort((a, b) => Math.abs(b - here) - Math.abs(a - here));
    for (const x of far.slice(0, S.chunks.size - D.POS.KEEP)) S.chunks.delete(x);
  }
  S.cars = D.mergeChunks([...S.chunks.values()]);
  S.carsVer++;
}

// ---- layout ---------------------------------------------------------------------------

// layout.js reports where F1 TV's player sits, in the page's CSS px; we fill the page's
// viewport, so they're ours too (scaled, should the two ever disagree about its size). The
// report is kept as sent and scaled when drawn: the first one can arrive while this frame is
// still 0x0, and scaling it then would pin the player box at nothing.
window.addEventListener('message', (e) => {
  const d = e.data;
  if (MODE !== 'iframe' || d?.type !== 'fone-layout' || (e.source !== window.parent && e.source !== window)) return;
  const r = d.rect;
  S.layoutMsg = r && [r.x, r.y, r.w, r.h].every(Number.isFinite) ? { r, vw: d.vw, vh: d.vh } : null;
  applyLayout();
});

function playerRect() {
  const m = S.layoutMsg;
  if (!m) return null;
  const kx = m.vw > 0 && innerWidth > 0 ? innerWidth / m.vw : 1;
  const ky = m.vh > 0 && innerHeight > 0 ? innerHeight / m.vh : 1;
  return { x: m.r.x * kx, y: m.r.y * ky, w: m.r.w * kx, h: m.r.h * ky };
}
window.addEventListener('resize', applyLayout);
// Moved to a screen with another pixel ratio: redraw the map at the new resolution.
(function watchDpr() {
  matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`).addEventListener('change', () => {
    applyLayout();
    watchDpr();
  }, { once: true });
})();

function place(el, box) {
  el.hidden = !box;
  if (box) Object.assign(el.style, { left: `${box.x}px`, top: `${box.y}px`, width: `${box.w}px`, height: `${box.h}px` });
}

function applyLayout() {
  const L = D.layout({ mode: MODE, vw: innerWidth, vh: innerHeight, rect: playerRect(), rows: S.rows, mapAspect: S.aspect });
  S.L = L;
  place($('tower'), L.tower);
  place($('cond'), L.cond);
  place($('map'), L.map);
  L.slots.forEach((s, i) => $(`slot${i}`).style.setProperty('--head', `${s.head}px`));
  for (let i = 0; i < D.OB_SLOTS; i++) place($(`slot${i}`), L.slots[i] || null);
  closeMenu();
  if (L.tower) {
    const m = D.towerMetrics(L.tower.w - 2, L.tower.h - 2, S.rows);
    const tower = $('tower');
    tower.style.setProperty('--fs', `${m.fs.toFixed(2)}px`);
    tower.style.setProperty('--rowh', `${m.rowH.toFixed(2)}px`);
    tower.style.setProperty('--cols', m.cols.map(([, w]) => `${w}em`).join(' '));
    for (const k of ['pits', 'int']) tower.classList.toggle(`no-${k}`, m.hide.includes(k));
  }
  if (L.map) sizeMap(L.map);
  document.body.dataset.layout = L.kind;
  S.ver++;
  renderSlots();
  postOnboards();
  askPlayer();
}

// ---- timing tower ---------------------------------------------------------------------

// Column widths from the font in use, not fixed ems: a Mac falls back from Segoe UI to SF Pro,
// whose digits are wider, and fixed widths clipped its lap times ("2:06.68") while leaving the
// tyre column half empty. Each column as wide as the widest thing it shows, measured with the
// tower's own styles (tabular digits, weights, the badges' small caps), plus a hair.
const TOWER_SAMPLES = {
  pos: ['<span class="pos">22</span>'],
  drv: ['<span class="drv"><i></i><b>WWW</b></span>', '<span class="drv"><i></i><b>MAG</b></span>'],
  gap: ['<span class="gap">+88.888</span>', '<span class="gap">+88 LAPS</span>', '<span class="gap lead">LEADER</span>'],
  int: ['<span class="int">+88.888</span>', '<span class="int">+8 LAPS</span>'],
  last: ['<span class="last">8:88.888</span>', '<span class="last long">88:88.888</span>'],
  best: ['<span class="best">8:88.888</span>', '<span class="best long">88:88.888</span>'],
  tyre: ['<span class="tyre"><span class="ico"><b>W</b></span><span class="age">88</span></span>'],
  pits: ['<span class="pits">8</span>'],
  mark: ['<span class="mark"><i class="pitout">PIT OUT</i></span>', '<span class="mark"><i class="pit">IN PIT</i></span>',
    '<span class="mark"><i class="ret">OUT</i></span>'],
};
function measureTower() {
  const probe = document.createElement('div');
  // at 100 px, so a width in px / 100 is the column's width in em
  probe.style.cssText = 'position:absolute;left:-20000px;top:0;visibility:hidden;font-size:100px;white-space:nowrap;';
  const keys = Object.keys(TOWER_SAMPLES);
  // and each header, styled as .thead's ("PIT" is wider than any pit count)
  const HEAD = { gap: 'Gap', int: 'Int', last: 'Last', best: 'Best', tyre: 'Tyre', pits: 'Pit' };
  const th = (k) => (HEAD[k] ? `<br><span style="font-size:.6em;font-weight:700;letter-spacing:.1em;text-transform:uppercase">${HEAD[k]}</span>` : '');
  probe.innerHTML = keys.map((k) => `<div data-k="${k}">${TOWER_SAMPLES[k].join('<br>')}${th(k)}</div>`).join('');
  $('tower').appendChild(probe);
  for (const el of probe.querySelectorAll('div > span')) el.style.display = 'inline-flex';
  const widths = {};
  for (const k of keys) {
    const spans = probe.querySelectorAll(`[data-k="${k}"] > span`);
    widths[k] = Math.max(...[...spans].map((s) => s.getBoundingClientRect().width)) / 100 + 0.12;
  }
  probe.remove();
  D.setTowerCols(widths);
  S.towerEm = widths;
}

const rowEls = new Map(); // car number -> {el, refs, v: what's shown}

function rowEl(num) {
  let x = rowEls.get(num);
  if (x) return x;
  const el = document.createElement('div');
  el.className = 'row';
  el.innerHTML = '<span class="pos"></span><span class="drv"><i></i><b></b></span><span class="gap"></span>'
    + '<span class="int"></span><span class="last"></span><span class="best"></span>'
    + '<span class="tyre"><span class="ico"><b></b></span><span class="age"></span></span>'
    + '<span class="pits"></span><span class="mark"></span>';
  const q = (s) => el.querySelector(s);
  x = {
    el, v: {},
    pos: q('.pos'), bar: q('.drv i'), code: q('.drv b'), gap: q('.gap'), int: q('.int'), last: q('.last'),
    best: q('.best'), ico: q('.ico'), letter: q('.ico b'), age: q('.age'), pits: q('.pits'), mark: q('.mark'),
  };
  $('rows').append(el);
  rowEls.set(num, x);
  return x;
}

// Touch the DOM only for what changed (the tower re-renders every second).
function put(x, key, value, apply) {
  if (x.v[key] === value) return;
  x.v[key] = value;
  apply(value);
}

function renderTower() {
  const st = S.state;
  const tower = st?.tower || [];
  const kind = D.sessionKind(st?.session);
  const fastest = D.fastestRow(tower);
  const t = $('tower');
  if (S.freeze) t.classList.add('noanim');
  const seen = new Set();
  tower.forEach((r, i) => {
    const num = String(r.num);
    seen.add(num);
    const x = rowEl(num);
    put(x, 'i', i, (v) => { x.el.style.setProperty('--i', v); x.el.classList.toggle('odd', v % 2 === 1); });
    put(x, 'dim', `${kind === 'quali' && r.knockedOut}|${!!r.retired}`, () => {
      x.el.classList.toggle('ko', kind === 'quali' && !!r.knockedOut);
      x.el.classList.toggle('ret', !!r.retired);
    });
    put(x, 'pos', r.pos ?? '', (v) => { x.pos.textContent = v; });
    put(x, 'color', r.color || '', (v) => { x.bar.style.setProperty('--team', v || '#777'); });
    put(x, 'code', r.code || num, (v) => { x.code.textContent = v; x.el.title = r.name || v; });
    const g = D.gapCells(r, kind);
    put(x, 'gap', g.gap, (v) => { x.gap.textContent = v; x.gap.classList.toggle('lead', v === 'LEADER'); });
    put(x, 'int', g.interval, (v) => { x.int.textContent = v; });
    const flag = r.last ? r.lastFlag : 'none';
    put(x, 'last', `${r.last || ''}|${flag}`, () => {
      x.last.textContent = r.last || '';
      x.last.classList.toggle('ob', flag === 'overall');
      x.last.classList.toggle('pb', flag === 'personal');
      x.last.classList.toggle('long', (r.last || '').length > 8); // a lap through a red-flag stop
    });
    put(x, 'best', `${r.best || ''}|${i === fastest}`, () => {
      x.best.textContent = r.best || '';
      x.best.classList.toggle('ob', i === fastest);
      x.best.classList.toggle('long', (r.best || '').length > 8);
    });
    const tyre = String(r.tyre || 'UNKNOWN');
    put(x, 'tyre', `${tyre}|${!!r.newTyre}`, () => {
      x.ico.className = `ico ${tyre.toLowerCase()}${r.newTyre ? ' new' : ''}`;
      x.ico.title = `${tyre.toLowerCase()}, ${r.newTyre ? 'new' : 'used'}`;
      x.letter.textContent = D.tyreLetter(tyre);
    });
    put(x, 'age', tyre === 'UNKNOWN' && !r.tyreAge ? '' : r.tyreAge ?? '', (v) => { x.age.textContent = v; });
    put(x, 'pits', r.pits ?? '', (v) => { x.pits.textContent = v; });
    const mark = D.rowMark(r);
    put(x, 'mark', mark ? mark.text : '', () => {
      x.mark.replaceChildren();
      if (!mark) return;
      const b = document.createElement('i');
      b.className = mark.cls;
      b.textContent = mark.text;
      x.mark.append(b);
    });
  });
  for (const [num, x] of rowEls) {
    if (!seen.has(num)) {
      x.el.remove();
      rowEls.delete(num);
    }
  }
  $('ttl').textContent = st ? D.towerTitle(st) : '';
  $('rows').style.height = `calc(var(--rowh) * ${tower.length})`;
  const cut = st ? D.cutAfter(st) : null;
  $('cut').hidden = cut == null || cut >= tower.length;
  if (cut != null) $('cut').style.setProperty('--k', cut);
  if (S.freeze && st) {
    S.freeze = false;
    requestAnimationFrame(() => requestAnimationFrame(() => t.classList.remove('noanim')));
  }
}

// What the tower says when it has no rows to show.
function renderNotes() {
  let main = '', sub = '';
  if (!S.clock) {
    main = MODE === 'iframe' ? 'Waiting for the player…' : 'Waiting for F1 TV…';
    sub = MODE === 'iframe' ? '' : 'Play a session on F1 TV with fone on.';
  } else if (!S.server) {
    main = 'Waiting for the fone server…';
  } else if (!S.key && S.cid && !S.cids.has(`${S.server}|${S.cid}`)) {
    main = 'Looking up the session…';
  } else if (!S.key) {
    main = 'No timing for this video';
    sub = 'Live timing exists for F1 sessions only.';
  } else if (S.na) {
    main = 'No timing data yet';
    sub = S.na;
  } else if (S.stale) {
    main = 'Can’t reach the fone server';
    sub = S.err;
  }
  const note = $('towerNote');
  note.hidden = !main || !!S.state;
  if (note.dataset.v !== `${main}|${sub}`) {
    note.dataset.v = `${main}|${sub}`;
    note.replaceChildren();
    const m = document.createElement('span');
    m.textContent = main;
    note.append(m);
    if (sub) {
      const s = document.createElement('small');
      s.textContent = sub;
      note.append(s);
    }
  }
  $('thead').hidden = !S.state;
}

// ---- conditions -----------------------------------------------------------------------

const fmtTemp = (x) => (Number.isFinite(x) ? `${x.toFixed(1)}°C` : '–');

function renderCond() {
  const st = S.state;
  const w = st?.weather || {};
  const meeting = String(st?.session?.meeting || '').replace(/\bGrand Prix\b/i, 'GP'); // a compact card
  $('sess').textContent = [meeting, st?.session?.name].filter(Boolean).join(' · ');
  const chip = st ? D.flagChip(st) : { cls: 'idle', text: '–' };
  $('flag').className = `flag ${chip.cls}`;
  $('flag').textContent = chip.text;
  // over the clock: the qualifying part, or LAP over the race's lap count (renderClock)
  const part = st ? D.partLabel(st) || (st.clock?.lap != null ? 'LAP' : '') : '';
  $('part').hidden = !part;
  $('part').textContent = part;
  $('air').textContent = fmtTemp(w.air);
  $('trk').textContent = fmtTemp(w.track);
  $('hum').textContent = Number.isFinite(w.humidity) ? `${Math.round(w.humidity)}%` : '–';
  $('rain').textContent = st && w.rain != null ? (w.rain ? 'Yes' : 'No') : '–';
  $('rain').classList.toggle('wet', !!w.rain);
  const wind = Number.isFinite(w.windSpeed);
  $('wind').textContent = wind ? `${w.windSpeed.toFixed(1)} m/s ${D.compass(w.windDir)}` : '–';
  // the arrow points where the wind blows (north up); the text says where it comes from
  // (the attribute: an <svg> has no .hidden property)
  $('windArrow').toggleAttribute('hidden', !wind || !Number.isFinite(w.windDir));
  if (Number.isFinite(w.windDir)) $('windArrow').style.transform = `rotate(${w.windDir + 180}deg)`;
  renderClock(S.clock ? D.clockAt(S.clock, now()) : null);
}

function renderClock(E) {
  const st = S.state;
  let text = '';
  if (st?.clock?.lap != null) text = `${st.clock.lap}${st.clock.laps ? `/${st.clock.laps}` : ''}`; // under LAP
  else if (st) {
    const sec = S.countdown && E != null ? D.countdownAt(S.countdown, E) : D.parseClock(st.clock?.remaining);
    text = sec == null ? '' : D.fmtClock(sec);
  }
  const clock = $('clock');
  if (clock.textContent !== text) clock.textContent = text;
  // stale: the last request failed, or the newest state lags the picture (slow server)
  const stale = S.stale || !!(st && E != null && S.clock?.playing && E - st.t > 6);
  if ($('stale').hidden === stale) $('stale').hidden = !stale;
}

function renderState() {
  renderTower();
  renderNotes();
  renderCond();
  renderSlots(); // driver codes and colours as on the tower
  S.ver++;
}

// ---- track map ------------------------------------------------------------------------

const bg = $('mapBg'), fg = $('mapFg');
const bgx = bg.getContext('2d'), fgx = fg.getContext('2d');
const map = { w: 0, h: 0, dpr: 1, widths: new Map(), fs: 0, dots: [] };

function mapGeom() {
  const m = Math.min(map.w, map.h);
  const r = D.clamp(m * 0.022, 4, 9);
  const fs = Math.round(D.clamp(m * 0.052, 10, 17));
  return { r, fs, lw: D.clamp(m * 0.03, 4, 14), pad: r + fs * 1.6 };
}

function sizeMap(box) {
  map.dpr = window.devicePixelRatio || 1;
  map.w = Math.max(0, box.w - 2);
  map.h = Math.max(0, box.h - 2);
  for (const c of [bg, fg]) {
    c.width = Math.round(map.w * map.dpr);
    c.height = Math.round(map.h * map.dpr);
  }
  drawTrack();
}

function drawTrack() {
  bgx.setTransform(map.dpr, 0, 0, map.dpr, 0, 0);
  bgx.clearRect(0, 0, map.w, map.h);
  S.fit = null;
  S.drawn = '';
  const note = $('mapNote');
  note.hidden = !!S.track || !S.sess;
  note.textContent = 'No track map yet';
  if (!S.track || !map.w) return;
  const g = mapGeom();
  S.fit = D.fitTrack(S.track.outline, S.track.rotation, map.w, map.h, g.pad);
  if (!S.fit) return;
  const path = new Path2D();
  S.track.outline.forEach(([x, y], i) => {
    const [px, py] = D.project(S.fit, x, y);
    if (i) path.lineTo(px, py); else path.moveTo(px, py);
  });
  path.closePath();
  bgx.lineJoin = 'round';
  bgx.lineCap = 'round';
  bgx.strokeStyle = '#1c1f26';
  bgx.lineWidth = g.lw + 4;
  bgx.stroke(path);
  bgx.strokeStyle = '#3b404c';
  bgx.lineWidth = g.lw;
  bgx.stroke(path);
  drawCorners(g);
}

// The corner numbers (几号弯), as on MultiViewer's map: small discs just off the track, on the
// side the circuit data gives. On the background canvas, under the cars and their labels.
function drawCorners(g) {
  const corners = S.track.corners || [];
  if (!corners.length) return;
  const fs = Math.round(D.clamp(g.fs * 0.82, 10, 14));
  const rad = fs * 0.72;
  const gap = g.lw / 2 + rad + 2;
  bgx.font = `600 ${fs}px system-ui, -apple-system, "Segoe UI", sans-serif`;
  bgx.textAlign = 'center';
  bgx.textBaseline = 'middle';
  for (const c of corners) {
    const [x, y] = D.cornerSpot(S.fit, c, gap);
    const label = `${c.n}${c.l || ''}`;
    const w = Math.max(rad * 2, bgx.measureText(label).width + fs * 0.5);
    bgx.fillStyle = '#272b34';
    bgx.strokeStyle = '#454b58';
    bgx.lineWidth = 1;
    bgx.beginPath();
    bgx.roundRect(x - w / 2, y - rad, w, rad * 2, rad);
    bgx.fill();
    bgx.stroke();
    bgx.fillStyle = '#c4c9d2';
    bgx.fillText(label, x, y + 0.5);
  }
}

function labelWidth(code, fs) {
  if (map.fs !== fs) {
    map.widths.clear();
    map.fs = fs;
  }
  let w = map.widths.get(code);
  if (w == null) {
    w = fgx.measureText(code).width;
    map.widths.set(code, w);
  }
  return w;
}

// Every animation frame: the cars at E (FoneDash.carAt never looks past E).
function drawCars(E) {
  const key = `${E.toFixed(3)}|${S.carsVer}|${S.ver}`;
  if (!S.L?.map || key === S.drawn) return;
  S.drawn = key;
  fgx.setTransform(map.dpr, 0, 0, map.dpr, 0, 0);
  fgx.clearRect(0, 0, map.w, map.h);
  map.dots = [];
  if (!S.fit) return;
  const g = mapGeom();
  const st = S.state && S.state.t <= E + 1 ? S.state : null;
  const rows = new Map((st?.tower || []).map((r, i) => [String(r.num), { r, i }]));
  const quali = D.sessionKind(st?.session) === 'quali';
  fgx.font = `700 ${g.fs}px ${FONT}`;
  const dots = [];
  for (const [num, samples] of Object.entries(S.cars)) {
    const row = rows.get(num);
    const r = row?.r;
    if (quali && r?.knockedOut) continue; // out of the session: parked in the pit lane
    const p = D.carAt(samples, E);
    if (!p) continue; // garage, dropout, not loaded
    const [x, y] = D.project(S.fit, p[0], p[1]);
    const code = r?.code || num;
    // In the pit lane (or stopped for good): faint, and labelled only where there's room.
    const faint = !!r && (r.inPit || r.retired);
    dots.push({
      id: num, x, y, w: labelWidth(code, g.fs), h: g.fs, code, faint, optional: faint,
      order: (faint ? 1000 : 0) + (row ? row.i : 100 + Number(num)),
      color: r?.color || '#9aa0aa',
    });
  }
  dots.sort((a, b) => a.order - b.order);
  map.dots = dots;
  const labels = D.placeLabels(dots, g.r, { w: map.w, h: map.h }, S.sides);
  S.sides = new Map([...labels].map(([id, v]) => [id, v.side]));
  // back to front: the leader ends up on top
  for (let i = dots.length - 1; i >= 0; i--) {
    const d = dots[i];
    fgx.globalAlpha = d.faint ? 0.45 : 1;
    fgx.beginPath();
    fgx.arc(d.x, d.y, g.r, 0, 2 * Math.PI);
    fgx.fillStyle = d.color;
    fgx.fill();
    fgx.lineWidth = 1.5;
    fgx.strokeStyle = 'rgba(0, 0, 0, .85)';
    fgx.stroke();
  }
  fgx.textBaseline = 'middle';
  fgx.lineJoin = 'round';
  fgx.lineWidth = 3;
  fgx.strokeStyle = 'rgba(7, 8, 11, .9)';
  for (let i = dots.length - 1; i >= 0; i--) {
    const d = dots[i], l = labels.get(d.id);
    if (!l) continue;
    fgx.globalAlpha = d.faint ? 0.5 : l.crowded ? 0.7 : 1;
    fgx.strokeText(d.code, l.x, l.y + d.h / 2);
    fgx.fillStyle = '#fff';
    fgx.fillText(d.code, l.x, l.y + d.h / 2);
  }
  fgx.globalAlpha = 1;
}

// ---- onboard slots (iframe) -----------------------------------------------------------
//
// Two driver onboards at the right end of the strip (FoneDash.layout's slots). Ours is each
// slot's header with a driver picker; the page shows F1 TV's onboard view in the slot's video
// area, above this frame, told where and which channel by the 'onboards' command (below).
// A choice is kept per slot by racing number, which stays the same from one session to the
// next (chrome.storage.local 'onboards': [num|null, num|null]); the channel that shows the
// driver is the content's own, from the fone server's list of its streams (/api/f1tv/{cid}).
const OB = {
  picks: D.onboardPicks(null), chosen: false, // chosen: on this page, before storage answered
  key: null, // `${server}|${cid}`: the content the streams below are from
  // 'none' (no content to ask about), 'loading', 'ok', 'missing' (a server that doesn't list
  // streams), 'error' (no answer; asked again shortly)
  status: 'none', streams: null, timer: 0,
  menu: -1, menuTimer: 0, idleTimer: 0, // the slot whose picker is open
  posted: null, // the last 'onboards' command (tests)
};
// A live content may gain streams once its session runs: ask again every few minutes, and
// sooner after a failure.
const OB_REFRESH = 300, OB_RETRY = 20;

function wantStreams(server, cid) {
  if (MODE !== 'iframe') return;
  const key = server && /^\d+$/.test(cid || '') ? `${server}|${cid}` : null;
  if (key === OB.key) return;
  clearTimeout(OB.timer);
  Object.assign(OB, { key, status: key ? 'loading' : 'none', streams: null, timer: 0 });
  if (key) loadStreams(key, `${server}/api/f1tv/${cid}`);
  onboardsChanged();
}

async function loadStreams(key, url) {
  let again = OB_REFRESH;
  try {
    const c = await getJSON(url);
    if (key !== OB.key) return;
    const streams = Array.isArray(c?.streams) ? c.streams : null;
    const status = streams ? 'ok' : 'missing';
    if (status !== OB.status || JSON.stringify(streams) !== JSON.stringify(OB.streams)) {
      Object.assign(OB, { status, streams });
      onboardsChanged();
    }
  } catch {
    if (key !== OB.key) return;
    again = OB_RETRY;
    if (OB.status !== 'ok') { // a list we have stays
      OB.status = 'error';
      onboardsChanged();
    }
  }
  OB.timer = setTimeout(() => { if (key === OB.key) loadStreams(key, url); }, again * 1000);
}

// The drivers with an onboard in this content, in the tower's order.
const onboards = () => (OB.status === 'ok' && D.onboardList(OB.streams, S.state?.tower)) || [];

// The channel showing slot i's driver here (null: none chosen, or none of theirs).
function slotChannel(i, list = onboards()) {
  const num = OB.picks[i];
  return num == null ? null : list.find((e) => e.num === num)?.channelId ?? null;
}

const OB_TIPS = {
  none: '', loading: '',
  missing: 'This fone server doesn’t list F1 TV’s onboard streams yet',
  error: 'Can’t reach the fone server for the onboard streams',
  ok: 'No driver onboards in this video',
};

function renderSlots() {
  const list = onboards();
  const rows = new Map((S.state?.tower || []).map((r) => [Number(r.num), r]));
  for (let i = 0; i < D.OB_SLOTS; i++) {
    const el = $(`slot${i}`);
    if (el.hidden) continue;
    const num = OB.picks[i];
    const on = list.length > 0;
    const set = on && num != null;
    const e = set ? list.find((x) => x.num === num) : null;
    const row = rows.get(num);
    const v = {
      set, on, code: set ? e?.code || row?.code || String(num) : 'Onboard', num: set ? String(num) : '',
      color: (set && (e?.color || row?.color)) || '',
      // quiet: a driver with no onboard in this video, or no list to pick from
      hint: set && !e ? 'no onboard' : on ? '' : { missing: 'unavailable', error: 'unavailable', ok: 'no onboards' }[OB.status] || '',
      tip: on ? (set && !e ? `No onboard of ${row?.name || `#${num}`} in this video` : 'Choose a driver’s onboard camera') : OB_TIPS[OB.status],
    };
    const key = JSON.stringify(v);
    if (el.dataset.v === key) continue;
    el.dataset.v = key;
    const b = el.querySelector('.pick');
    b.disabled = !on;
    b.classList.toggle('set', set);
    b.querySelector('i').style.setProperty('--team', v.color || '#777');
    b.querySelector('b').textContent = v.code;
    b.querySelector('.num').textContent = v.num;
    el.querySelector('.hint').textContent = v.hint;
    el.classList.toggle('off', !on);
    el.title = v.tip;
  }
}

// A choice, a new list or a new layout: headers, and the page's views.
function onboardsChanged() {
  if (OB.menu >= 0 && !onboards().length) closeMenu();
  renderSlots();
  postOnboards();
}

function choose(i, num) {
  closeMenu();
  if (i < 0) return;
  OB.chosen = true;
  OB.picks = D.pickOnboard(OB.picks, i, num);
  try {
    chrome.storage.local.set({ onboards: OB.picks }).catch(() => {});
  } catch { /* the extension was reloaded under us */ }
  onboardsChanged();
}

// The picker: the drivers in tower order, column after column, and None. It opens above the
// slot's header, over the tower's column: below the header is the slot's video and left of
// the column the player, both shown by the page above this frame.
function openMenu(i) {
  closeMenu();
  const slot = S.L?.slots[i], col = S.L?.tower, list = onboards();
  if (!slot || !col || !list.length) return;
  OB.menu = i;
  $('menuTtl').textContent = `Onboard ${i + 1}`;
  $('menuOpts').replaceChildren(...list.map((e) => optEl(e, i)));
  $('menuNone').classList.toggle('on', OB.picks[i] == null);
  const bottom = slot.y - 4, room = bottom - col.y;
  let cols = 2; // rows of 24 px + 2 px gap, ~70 px for the title, None and padding
  while (cols < 4 && Math.ceil(list.length / cols) * 26 + 70 > room) cols++;
  $('menuOpts').style.setProperty('--rows', Math.ceil(list.length / cols));
  const menu = $('menu');
  Object.assign(menu.style, { left: '0px', bottom: `${innerHeight - bottom}px`, maxHeight: `${room}px` });
  menu.hidden = false;
  // under the slot's button where the column allows, never over the player
  menu.style.left = `${Math.max(col.x, Math.min(slot.x, col.x + col.w - menu.offsetWidth))}px`;
  $(`slot${i}`).classList.add('open');
  menuAlive();
}

function optEl(e, i) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'opt';
  b.dataset.num = e.num;
  b.title = e.name;
  b.classList.toggle('on', OB.picks[i] === e.num);
  b.classList.toggle('other', OB.picks.some((x, k) => k !== i && x === e.num)); // picking it swaps
  const bar = document.createElement('i');
  bar.style.setProperty('--team', e.color || '#777');
  const code = document.createElement('b');
  code.textContent = e.code;
  const num = document.createElement('span');
  num.textContent = e.num;
  b.append(bar, code, num);
  return b;
}

function closeMenu() {
  clearTimeout(OB.menuTimer);
  clearTimeout(OB.idleTimer);
  if (OB.menu < 0) return;
  $(`slot${OB.menu}`).classList.remove('open');
  OB.menu = -1;
  $('menu').hidden = true;
}

// No focus here (a click doesn't move it into this frame), so no Escape. A click elsewhere on
// the stats closes the picker, and so does leaving it for the rest of the stats (MENU.LEFT).
// Leaving it for the player or an onboard view (the page's, above this frame) sends this frame
// no event at all, not even pointerleave, so a picker that hears nothing from the pointer for
// a while (MENU.IDLE) closes too.
const MENU = { LEFT: 1500, IDLE: 6000 };
const menuLeft = () => {
  clearTimeout(OB.menuTimer);
  OB.menuTimer = setTimeout(closeMenu, MENU.LEFT);
};
const menuBack = () => clearTimeout(OB.menuTimer);
function menuAlive() {
  clearTimeout(OB.idleTimer);
  OB.idleTimer = setTimeout(closeMenu, MENU.IDLE);
}
document.addEventListener('pointermove', () => { if (OB.menu >= 0) menuAlive(); }, { passive: true });
for (let i = 0; i < D.OB_SLOTS; i++) {
  const el = $(`slot${i}`);
  // the whole slot: its empty video area too (with an onboard there, the page's view takes the click)
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    if (OB.menu === i) closeMenu();
    else openMenu(i);
  });
  el.addEventListener('pointerenter', menuBack);
  el.addEventListener('pointerleave', () => { if (OB.menu === i) menuLeft(); });
}
$('menu').addEventListener('click', (e) => {
  e.stopPropagation();
  const b = e.target.closest('button[data-num]');
  if (b) choose(OB.menu, b.dataset.num ? Number(b.dataset.num) : null);
});
$('menu').addEventListener('pointerenter', menuBack);
$('menu').addEventListener('pointerleave', menuLeft);
document.addEventListener('click', closeMenu);

// ---- stats mode -------------------------------------------------------------------------
// 'stats': the picture as large as layout.js makes it (77% of the width), onboards only where
// the strip under it has room. 'onboards': the picture shrinks until the strip holds onboards
// beside the map (FoneDash.onboardBox); a window much wider than tall otherwise gets none.
// Kept per browser (chrome.storage.local 'statsMode'); the switch in the map's corner and
// layout.js's O key flip it. We ask layout.js for the box ('player' command; null: its own).
const STATS_MODES = ['stats', 'onboards'];
const MODE_ST = { mode: 'stats', asked: undefined };

function setStatsMode(m, save) {
  if (!STATS_MODES.includes(m) || m === MODE_ST.mode) return;
  MODE_ST.mode = m;
  if (save) {
    try { chrome.storage.local.set({ statsMode: m }).catch(() => {}); } catch { /* reloaded */ }
  }
  applyLayout();
}

function askPlayer() {
  const btn = $('obToggle');
  btn.hidden = MODE !== 'iframe' || !S.L?.map;
  btn.classList.toggle('on', MODE_ST.mode === 'onboards');
  if (MODE !== 'iframe' || window.parent === window) return;
  const box = MODE_ST.mode === 'onboards'
    ? D.onboardBox({ vw: innerWidth, vh: innerHeight, rows: S.rows, mapAspect: S.aspect }) : null;
  // in the page's px, should ours differ (as postOnboards); whole 16x9 steps either way
  const m = S.layoutMsg, k = m?.vw > 0 && innerWidth > 0 ? m.vw / innerWidth : 1;
  const steps = box ? Math.round((box.w * k) / 16) : 0;
  const ask = box ? { w: 16 * steps, h: 9 * steps } : null;
  const key = JSON.stringify(ask);
  if (key === MODE_ST.asked) return;
  MODE_ST.asked = key;
  window.parent.postMessage({ type: 'fone-layout-cmd', cmd: 'player', box: ask }, '*');
}

$('obToggle').addEventListener('click', (e) => {
  e.stopPropagation();
  setStatsMode(MODE_ST.mode === 'onboards' ? 'stats' : 'onboards', true);
});

// To the page: each slot's video area in its CSS px, and the channel to show there (null:
// none); slots: [] when the layout has none. On every change, and every 2 s while there are
// slots, so a page that reloaded or rebuilt its player gets them again. The iframe covers the
// page's viewport: our px are the page's, scaled back should the two ever disagree about its
// size (as playerRect scales the player's box to ours). Boxes and channel ids, nothing
// private, so any origin: layout.js checks that it comes from our frame and origin.
function postOnboards() {
  if (MODE !== 'iframe' || window.parent === window) return;
  const m = S.layoutMsg;
  const kx = m?.vw > 0 && innerWidth > 0 ? m.vw / innerWidth : 1;
  const ky = m?.vh > 0 && innerHeight > 0 ? m.vh / innerHeight : 1;
  const list = onboards();
  const slots = (S.L?.slots || []).map(({ video: v }, i) => ({
    x: v.x * kx, y: v.y * ky, w: v.w * kx, h: v.h * ky, channelId: slotChannel(i, list),
  }));
  OB.posted = slots;
  window.parent.postMessage({ type: 'fone-layout-cmd', cmd: 'onboards', slots }, '*');
}

// ---- frame loop -----------------------------------------------------------------------

function frame() {
  requestAnimationFrame(frame);
  if (!S.clock) return;
  const E = D.clockAt(S.clock, now());
  if (S.sess) needChunks(E);
  renderClock(E);
  drawCars(E);
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && S.sess) pollNow();
});

// ---- debug clock (no engine) ------------------------------------------------------------

function simReport() {
  const t = now();
  const E = SIM.playing ? SIM.E + (t - SIM.at) * SIM.rate : SIM.E;
  follow({ E, at: t, rate: SIM.rate, playing: SIM.playing, sessionKey: Number(Q.get('session')) || null,
    cid: Q.get('cid'), server: Q.get('server'), live: false }, t);
}

// Tests: seek, pause, resume, speed, the way the engine would report them.
function simSet(patch) {
  const t = now();
  Object.assign(SIM, { E: SIM.playing ? SIM.E + (t - SIM.at) * SIM.rate : SIM.E, at: t }, patch);
  simReport();
}

globalThis.__dash = {
  S, simSet,
  snapshot: () => ({
    mode: MODE, layout: S.L?.kind, myTab: S.myTab, follow: S.follow, proxy: net.proxy,
    E: S.clock ? D.clockAt(S.clock, now()) : null, playing: S.clock?.playing ?? null,
    stateT: S.state?.t ?? null, na: S.na, stale: S.stale, staleShown: !$('stale').hidden, rows: rowEls.size,
    flag: $('flag').textContent, clock: $('clock').textContent, part: $('part').textContent,
    chunks: [...S.chunks.keys()], cars: Object.keys(S.cars).length, track: !!S.track,
    dots: map.dots.map((d) => ({ id: d.id, code: d.code, x: Math.round(d.x * 10) / 10, y: Math.round(d.y * 10) / 10 })),
    note: $('towerNote').hidden ? '' : $('towerNote').textContent,
    panels: {
      ...Object.fromEntries(['tower', 'cond', 'map'].map((k) => [k, S.L?.[k] || null])),
      ...Object.fromEntries(Array.from({ length: D.OB_SLOTS }, (_, i) => [`slot${i}`, S.L?.slots[i] || null])),
    },
    onboards: {
      picks: OB.picks, status: OB.status, list: onboards().map((e) => e.code), channels: OB.picks.map((_, i) => slotChannel(i)),
      heads: OB.picks.map((_, i) => ($(`slot${i}`).hidden ? null : $(`slot${i}`).querySelector('.shead').textContent)),
      disabled: OB.picks.map((_, i) => $(`slot${i}`).querySelector('.pick').disabled),
      menu: OB.menu < 0 ? null : {
        slot: OB.menu, box: (({ x, y, width: w, height: h }) => ({ x, y, w, h }))($('menu').getBoundingClientRect()),
        items: [...$('menuOpts').children].map((b) => b.querySelector('b').textContent),
      },
      posted: OB.posted,
    },
  }),
};

// ---- start ----------------------------------------------------------------------------

measureTower();
applyLayout(); // posts the onboard slots once
renderState();
document.fonts?.ready.then(() => { measureTower(); applyLayout(); });
// layout.js posts the player's box when this frame loads; ask again in case that came first.
if (MODE === 'iframe' && window.parent !== window) window.parent.postMessage({ type: 'fone-layout-cmd', cmd: 'rect' }, '*');
if (MODE === 'iframe') {
  try {
    chrome.storage.local.get('onboards').then(({ onboards: v }) => {
      if (OB.chosen) return;
      OB.picks = D.onboardPicks(v);
      onboardsChanged();
    }, () => {});
    chrome.storage.local.get('statsMode').then(({ statsMode }) => setStatsMode(statsMode, false), () => {});
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area === 'local' && ch.statsMode) setStatsMode(ch.statsMode.newValue, false);
    });
  } catch { /* no extension API */ }
  setInterval(() => { if (S.L?.slots.length) postOnboards(); }, 2000);
}
if (SIM) {
  simReport();
  setInterval(simReport, 200);
}
requestAnimationFrame(frame);
