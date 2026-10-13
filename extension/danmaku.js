// Danmaku (弹幕) from the streamer's live room, scrolling over F1 TV's picture in step with the
// commentary, with Bilibili's settings: 显示区域, 不透明度, 字号, 速度, 粗体, 描边, 允许重叠,
// 屏蔽类型 (滚动/顶部/底部/彩色/表情/SC), 等级屏蔽 (用户等级, 荣耀等级, 粉丝牌) and 关键词屏蔽.
//
// Items come from the fone server (GET /api/danmaku, through the service worker: the page is
// https, the server plain http), a window around the commentary's moment, refreshed at the
// live edge. The clock is the engine's (offscreen.js danmakuClock, in every status): the wall
// time of the audio playing now, so the danmaku pause, jump and speed up with what you hear.
// The maths (filters, lanes) is in dmcore.js. Settings: chrome.storage.sync 'danmaku'.
(function () {
  'use strict';
  if (globalThis.FoneDanmaku) return;
  const D = globalThis.FoneDmCore;

  const FETCH = { BEHIND: 20, AHEAD: 90, EVERY: 4, LIVE_EVERY: 1.5, LIVE_OVERLAP: 10, RETRY: 10 };
  const CATCHUP = 3; // s: a danmaku that comes due (or arrives) this late still shows, from the right edge
  const JUMP = [0.3, 1.5]; // s behind / ahead of the smoothed clock: a seek or a nudge, start over
  const SMOOTH = 0.3; // s: the drawn clock eases onto the engine's reports
  const MAX_ON_SCREEN = 200;
  const RECT_EVERY = 200;
  const FONT = '"SimHei", "Microsoft YaHei", "PingFang SC", "Heiti SC", "Noto Sans CJK SC", Arial, sans-serif';

  let settings = D.normalize(null);
  let pass = D.filter(settings);
  let status = null;
  const items = []; // sorted by when they show
  const seen = new Set();
  const net = { channel: null, server: null, a: null, b: null, busy: false, at: 0, err: null, live: false };

  // ---- the stage (over the picture) and the settings panel --------------------------------

  const host = document.createElement('div');
  host.id = 'fone-danmaku';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `
<style>
  :host { all: initial; }
  [hidden] { display: none !important; }
  .stage { position: fixed; overflow: hidden; pointer-events: none; z-index: 2147483000; contain: strict; }
  .dm { position: absolute; left: 0; white-space: pre; line-height: 1.25; color: #fff; will-change: transform; font-family: ${FONT}; }
  .dm img { vertical-align: middle; }
  .dm img.emo { display: block; }
  .dm.sc { padding: 0 .45em; border-radius: .35em; background: rgba(42, 96, 178, .78); }
  .dm.sc b { margin-right: .4em; color: #ffd76a; }
  .panel { position: fixed; top: 14px; right: 330px; z-index: 2147483647; width: 330px; max-height: calc(100vh - 28px); overflow: auto;
    background: rgba(24, 25, 28, .96); color: #e7e7e7; border: 1px solid rgba(255,255,255,.12); border-radius: 10px; padding: 12px 14px;
    font: 12.5px/1.4 system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; box-shadow: 0 8px 28px rgba(0,0,0,.5); }
  .ph { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .ph b { font-size: 14px; font-weight: 600; } .sub { color: #8a8a8a; flex: 1; font-size: 11.5px; }
  .sec { border-top: 1px solid rgba(255,255,255,.08); padding: 9px 0 4px; }
  .lab { color: #9a9a9a; margin-bottom: 6px; } .hint { color: #777; font-size: 11px; }
  button { all: unset; cursor: pointer; padding: 3px 9px; border-radius: 5px; background: rgba(255,255,255,.08); color: #ddd; font: 12px system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; }
  button:hover { background: rgba(255,255,255,.16); }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chips button.on { background: rgba(0, 174, 236, .22); color: #4fc3f7; box-shadow: inset 0 0 0 1px rgba(0,174,236,.6); }
  .seg { display: inline-flex; gap: 2px; background: rgba(255,255,255,.06); border-radius: 6px; padding: 2px; }
  .seg button { background: none; padding: 2px 9px; } .seg button.on { background: #00aeec; color: #fff; }
  .sl, .row { display: grid; grid-template-columns: 64px 1fr 44px; align-items: center; gap: 8px; margin: 4px 0; }
  .row { grid-template-columns: 64px 1fr; }
  .sl output { text-align: right; color: #bbb; font-variant-numeric: tabular-nums; }
  input[type=range] { width: 100%; accent-color: #00aeec; }
  .ck { display: inline-flex; align-items: center; gap: 5px; margin: 4px 14px 2px 0; cursor: pointer; }
  .ck input { accent-color: #00aeec; }
  .kwadd { display: flex; gap: 6px; } .kwadd input { flex: 1; min-width: 0; background: #111; color: #eee; border: 1px solid #444; border-radius: 5px; padding: 3px 6px; font: 12px system-ui; }
  .kwlist { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
  .kwlist span { background: rgba(255,255,255,.08); border-radius: 4px; padding: 1px 4px 1px 7px; }
  .kwlist span button { padding: 0 5px; background: none; color: #999; }
  .foot { display: flex; align-items: center; justify-content: space-between; border-top: 1px solid rgba(255,255,255,.08); padding-top: 9px; margin-top: 4px; }
  .x { padding: 0 7px; font-size: 16px; background: none; color: #999; }
</style>
<div class="stage" id="stage" hidden></div>
<div class="panel" id="panel" hidden>
  <div class="ph"><b>弹幕设置</b><span class="sub" id="psub"></span><button class="x" id="pclose" title="关闭 (Esc)">×</button></div>
  <div class="sec"><div class="lab">弹幕 <span class="hint">· 快捷键 D</span></div>
    <div class="chips"><button id="pon">开启</button></div></div>
  <div class="sec"><div class="lab">屏蔽类型</div>
    <div class="chips" id="block">
      <button data-block="scroll">滚动</button><button data-block="top">顶部</button><button data-block="bottom">底部</button>
      <button data-block="color">彩色</button><button data-block="emoticon">表情</button><button data-block="sc">醒目留言</button>
    </div></div>
  <div class="sec"><div class="lab">等级屏蔽 <span class="hint">低于所选等级的不显示</span></div>
    <div class="sl"><span>用户等级</span><input type="range" data-k="minUL" min="0" max="60" step="1"><output></output></div>
    <div class="sl"><span>荣耀等级</span><input type="range" data-k="minWealth" min="0" max="60" step="1"><output></output></div>
    <div class="sl"><span>粉丝牌</span><input type="range" data-k="minMedal" min="0" max="40" step="1"><output></output></div>
    <div class="hint" id="lvlNote"></div></div>
  <div class="sec"><div class="lab">关键词屏蔽</div>
    <div class="kwadd"><input id="kw" placeholder="关键词，或 /正则/" maxlength="100"><button id="kwadd">添加</button></div>
    <div class="kwlist" id="kwlist"></div></div>
  <div class="sec"><div class="lab">显示设置</div>
    <div class="sl"><span>不透明度</span><input type="range" data-k="opacity" min="0.1" max="1" step="0.05"><output></output></div>
    <div class="row"><span>显示区域</span><div class="seg" id="area"><button data-area="0.25">1/4</button><button data-area="0.5">半屏</button><button data-area="0.75">3/4</button><button data-area="1">满屏</button></div></div>
    <div class="sl"><span>字号</span><input type="range" data-k="font" min="0.5" max="2" step="0.05"><output></output></div>
    <div class="sl"><span>速度</span><input type="range" data-k="speed" min="0.5" max="2" step="0.25"><output></output></div>
    <div class="row"><span>描边类型</span><div class="seg" id="stroke"><button data-stroke="heavy">重墨</button><button data-stroke="outline">描边</button><button data-stroke="shadow">45°投影</button></div></div>
    <label class="ck"><input type="checkbox" data-k="bold">粗体</label><label class="ck"><input type="checkbox" data-k="overlap">允许重叠</label>
  </div>
  <div class="foot"><button id="reset">恢复默认</button><span class="hint" id="pstat"></span></div>
</div>`;
  const $ = (id) => root.getElementById(id);
  const stage = $('stage');
  const panel = $('panel');

  function mount() {
    const fs = document.fullscreenElement;
    const parent = fs && fs.tagName !== 'VIDEO' ? fs : document.documentElement;
    if (host.parentNode !== parent) parent.appendChild(host);
  }
  document.addEventListener('fullscreenchange', mount);
  mount();

  // ---- the picture ------------------------------------------------------------------------

  // The picture's box on screen: the main video, its 16:9 (or whatever it is) frame inside
  // the element, letter- or pillar-boxed as the player shows it.
  function pictureRect() {
    const v = globalThis.FonePlayer?.pickVideo();
    if (!v) return null;
    const r = v.getBoundingClientRect();
    if (r.width < 80 || r.height < 60) return null;
    const vw = v.videoWidth || 16, vh = v.videoHeight || 9;
    const k = Math.min(r.width / vw, r.height / vh);
    const w = Math.round(vw * k), h = Math.round(vh * k);
    return { x: Math.round(r.left + (r.width - w) / 2), y: Math.round(r.top + (r.height - h) / 2), w, h };
  }

  // ---- drawing ------------------------------------------------------------------------------

  const S = {
    disp: null, // the clock as drawn (eases onto the engine's)
    lastFrame: 0,
    rect: null,
    seenRect: null, // pictureRect(), measured every RECT_EVERY ms
    rectAt: 0,
    geo: null, // {W, H, laneH, n, scale}: what the lanes were cut for
    lanes: null,
    active: [], // {it, el, kind, t0, dur, w, y}
    shown: new Set(), // keys spawned since the last rebuild
  };

  const STROKE = {
    heavy: (c) => `1px 0 1px ${c}, 0 1px 1px ${c}, 0 -1px 1px ${c}, -1px 0 1px ${c}`,
    outline: (c) => `-1px -1px 0 ${c}, 1px -1px 0 ${c}, -1px 1px 0 ${c}, 1px 1px 0 ${c}`,
    shadow: (c) => `1px 1px 2px ${c}, 0 0 1px ${c}`,
  };
  const hex = (c) => `#${(c >>> 0 & 0xffffff).toString(16).padStart(6, '0')}`;
  const dark = (c) => (((c >> 16) & 255) * 0.299 + ((c >> 8) & 255) * 0.587 + (c & 255) * 0.114) < 60;

  // Stickers and emoji come from Bilibili's CDN, which F1 TV's Content-Security-Policy doesn't
  // allow the page to load: the service worker fetches them (dashproxy.js) as data: URLs.
  const IMG = new Map(); // url -> data: URL, a promise of one, or null (failed)
  const imgStats = { ok: 0, fail: 0, err: null };
  function loadImage(url) {
    if (!IMG.has(url)) {
      const done = (d, err) => {
        IMG.set(url, d);
        if (d) imgStats.ok++; else { imgStats.fail++; imgStats.err = err; }
        return d;
      };
      IMG.set(url, chrome.runtime.sendMessage({ target: 'dm-image', url })
        .then((r) => done(r?.ok ? r.data : null, r?.error), (e) => done(null, e?.message)));
    }
    return Promise.resolve(IMG.get(url));
  }
  const imagesOf = (it) => [...(it.e ? [it.e.u] : []), ...Object.values(it.em || {})];

  function geometry(rect) {
    const scale = Math.min(1.6, Math.max(0.6, rect.h / 810)) * settings.font;
    const laneH = Math.max(12, Math.round(25 * scale * 1.25));
    const n = Math.max(1, Math.floor((rect.h * settings.area) / laneH));
    return { W: rect.w, H: rect.h, laneH, n, scale };
  }

  // A fixed-size box from the start (the lanes are cut by width), filled when the image is here;
  // the text instead if it can't be had.
  function image(src, w, h, alt, cls) {
    const img = document.createElement('img');
    img.width = w;
    img.height = h;
    img.alt = '';
    if (cls) img.className = cls;
    const fill = (data) => {
      if (data) img.src = data;
      else img.replaceWith(document.createTextNode(alt));
    };
    const have = IMG.get(src);
    if (typeof have === 'string') img.src = have;
    else loadImage(src).then(fill);
    return img;
  }

  function make(it, g) {
    const el = document.createElement('div');
    el.className = it.k === 'sc' ? 'dm sc' : 'dm';
    const px = Math.round((it.s || 25) * g.scale);
    el.style.fontSize = `${px}px`;
    el.style.fontWeight = settings.bold ? '700' : '400';
    const color = it.c != null && it.k !== 'sc' ? it.c : 0xffffff;
    if (color !== 0xffffff) el.style.color = hex(color);
    el.style.textShadow = STROKE[settings.stroke](dark(color) ? '#fff' : 'rgba(0,0,0,.92)');
    let span = 1;
    if (it.e && it.k !== 'sc') { // an emoticon danmaku: the sticker itself, two lanes tall
      span = 2;
      const h = g.laneH * 2 - 4;
      el.append(image(it.e.u, it.e.w > 0 && it.e.h > 0 ? Math.round((h * it.e.w) / it.e.h) : h, h, it.x, 'emo'));
    } else {
      if (it.k === 'sc') {
        const b = document.createElement('b');
        b.textContent = `SC${it.p ? ` ¥${it.p}` : ''}`;
        el.append(b);
      }
      const em = it.em || {};
      const codes = Object.keys(em);
      const re = codes.length ? new RegExp(codes.map((c) => c.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g') : null;
      let i = 0;
      for (const m of re ? it.x.matchAll(re) : []) {
        if (m.index > i) el.append(it.x.slice(i, m.index));
        const side = Math.round(px * 1.1);
        el.append(image(em[m[0]], side, side, m[0]));
        i = m.index + m[0].length;
      }
      if (i < it.x.length) el.append(it.x.slice(i));
    }
    return { el, span };
  }

  function clear() {
    for (const a of S.active) a.el.remove();
    S.active = [];
    S.shown.clear();
    S.lanes = S.geo ? D.lanes(S.geo.n) : null;
  }

  function spawn(it, t0, rate) {
    if (S.active.length >= MAX_ON_SCREEN) return;
    const g = S.geo;
    const { el, span } = make(it, g);
    el.style.visibility = 'hidden';
    stage.append(el);
    const w = el.offsetWidth;
    const kind = it.k === 'sc' || it.m === 5 ? 'top' : it.m === 4 ? 'bottom' : 'scroll';
    // durations on the clock the danmaku run on: at 2x it runs twice as fast, so the same
    // time on screen is twice as long on it
    const dur = (kind === 'scroll' ? D.DURATION / settings.speed : D.FIXED) * rate;
    const lane = D.place(S.lanes, kind, t0, g.W, w, dur, Math.min(span, g.n), settings.overlap);
    if (lane < 0) { el.remove(); return; }
    const y = kind === 'bottom' ? Math.round(g.H * settings.area) - (lane + span) * g.laneH : lane * g.laneH;
    el.style.top = `${Math.max(0, y)}px`;
    el.style.visibility = '';
    S.active.push({ it, el, kind, t0, dur, w, x: null });
  }

  // Start over at clock time `w`: everything that would be on screen now, where it would be.
  function rebuild(w, rate) {
    clear();
    if (!S.geo || w == null) return;
    const longest = Math.max(D.DURATION / settings.speed, D.FIXED) * rate;
    for (let i = D.lowerBound(items, w - longest); i < items.length && D.showAt(items[i]) <= w; i++) {
      const it = items[i];
      S.shown.add(D.key(it));
      if (pass(it)) spawn(it, D.showAt(it), rate);
    }
  }

  function frame(ts) {
    requestAnimationFrame(frame);
    const dt = S.lastFrame ? Math.min(1, (ts - S.lastFrame) / 1000) : 0;
    S.lastFrame = ts;
    const c = status?.dm;
    const target = D.clockAt(c, Date.now() / 1000);
    if (ts - S.rectAt > RECT_EVERY) { S.rectAt = ts; S.seenRect = settings.on ? pictureRect() : null; }
    const rect = settings.on && target != null ? S.seenRect : null;
    if (!rect) {
      if (!stage.hidden) { stage.hidden = true; clear(); S.disp = null; }
      return;
    }
    const rate = c.rate || 1;
    let jumped = S.disp == null || target - S.disp > JUMP[1] || S.disp - target > JUMP[0];
    if (jumped) S.disp = target;
    else if (c.run) {
      S.disp += dt * rate;
      S.disp += (target - S.disp) * Math.min(1, dt / SMOOTH);
    } // paused: hold still where they are (the pause reaches us up to 250 ms late; easing back would creep)
    if (!S.rect || rect.x !== S.rect.x || rect.y !== S.rect.y || rect.w !== S.rect.w || rect.h !== S.rect.h) {
      Object.assign(stage.style, { left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.w}px`, height: `${rect.h}px` });
      const g = geometry(rect);
      if (!S.geo || g.W !== S.geo.W || g.H !== S.geo.H) jumped = true;
      S.rect = rect;
      S.geo = g;
    }
    stage.hidden = false;
    stage.style.opacity = String(settings.opacity);
    const w = S.disp;
    if (jumped) rebuild(w, rate);
    // what came due since (or arrived late, live): from the right edge, now
    for (let i = D.lowerBound(items, w - CATCHUP); i < items.length && D.showAt(items[i]) <= w; i++) {
      const it = items[i];
      const k = D.key(it);
      if (S.shown.has(k)) continue;
      S.shown.add(k);
      if (pass(it)) spawn(it, Math.max(D.showAt(it), w - 0.25), rate);
    }
    const W = S.geo.W;
    S.active = S.active.filter((a) => {
      const p = (w - a.t0) / a.dur;
      if (p >= 1 || p < -0.05) { a.el.remove(); return false; }
      const x = a.kind === 'scroll' ? W - (W + a.w) * p : (W - a.w) / 2;
      if (a.x == null || Math.abs(x - a.x) >= 0.25) {
        a.x = x;
        a.el.style.transform = `translate3d(${x.toFixed(1)}px,0,0)`;
      }
      return true;
    });
    if (S.shown.size > 20000) S.shown = new Set(S.active.map((a) => D.key(a.it)));
  }
  requestAnimationFrame(frame);

  // ---- fetching -------------------------------------------------------------------------------

  async function poll() {
    const st = status;
    if (!settings.on || !st?.channel || !st.server || !st.dm) return;
    if (st.channel !== net.channel || st.server !== net.server) {
      Object.assign(net, { channel: st.channel, server: st.server, a: null, b: null, at: 0, err: null, live: false });
      items.length = 0;
      seen.clear();
      S.disp = null; // rebuild on the next frame
    }
    if (net.busy) return;
    const nowS = Date.now() / 1000;
    if (net.err && nowS - net.at < FETCH.RETRY) return; // a server without danmaku (404), or down
    const w = D.clockAt(st.dm, nowS);
    const want = [w + D.LEAD - FETCH.BEHIND, w + D.LEAD + FETCH.AHEAD];
    const live = want[1] > nowS - FETCH.LIVE_OVERLAP; // reaches the present: its end keeps growing
    let from = want[0];
    const to = Math.min(want[1], nowS + 5);
    if (net.a != null && want[0] >= net.a && want[0] <= net.b) {
      if (want[1] <= net.b && !live) return; // all there already
      if (live && nowS - net.at < FETCH.LIVE_EVERY) return;
      from = Math.max(want[0], net.b - (live ? FETCH.LIVE_OVERLAP : 0));
    } else if (nowS - net.at < 0.5) return;
    if (to <= from) return;
    net.busy = true;
    try {
      const url = `${net.server}/api/danmaku?channel=${encodeURIComponent(net.channel)}&from=${from.toFixed(3)}&to=${to.toFixed(3)}`;
      const r = await chrome.runtime.sendMessage({ target: 'dash-fetch', url });
      if (!r?.ok) throw new Error(r?.error || 'no answer');
      if (st.channel === net.channel) {
        const fresh = (r.json.items || []).filter((it) => it && !seen.has(D.key(it)));
        D.merge(items, seen, fresh);
        for (const it of fresh) for (const u of imagesOf(it)) loadImage(u); // here before they show
        net.live = !!r.json.live;
        const b = Math.min(to, nowS);
        if (net.a == null || from > net.b || to < net.a) { net.a = from; net.b = b; } else { net.a = Math.min(net.a, from); net.b = Math.max(net.b, b); }
      }
      net.err = null;
    } catch (e) {
      net.err = e.message || String(e);
    } finally {
      net.busy = false;
      net.at = Date.now() / 1000;
    }
  }
  setInterval(() => { poll().catch(() => {}); }, 500);

  // ---- settings -------------------------------------------------------------------------------

  let saveTimer = 0;
  function apply(next, save) {
    const prev = settings;
    settings = D.normalize(next);
    pass = D.filter(settings);
    const redraw = ['font', 'area', 'speed', 'bold', 'stroke', 'overlap'].some((k) => prev[k] !== settings[k])
      || JSON.stringify(prev.block) !== JSON.stringify(settings.block) || prev.minUL !== settings.minUL
      || prev.minWealth !== settings.minWealth || prev.minMedal !== settings.minMedal
      || prev.keywords.join('\n') !== settings.keywords.join('\n') || prev.on !== settings.on;
    if (redraw) { S.geo = null; S.rect = null; S.disp = null; S.rectAt = 0; } // cut lanes again, rebuild on the next frame
    renderPanel();
    if (save) {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        try { chrome.storage.sync.set({ danmaku: settings }).catch(() => {}); } catch { /* extension reloaded */ }
      }, 250);
    }
  }

  const set = (patch) => apply({ ...settings, ...patch }, true);
  const levelText = (v) => (v ? `<${v}` : '关');
  const OUT = {
    opacity: (v) => `${Math.round(v * 100)}%`, font: (v) => `${Math.round(v * 100)}%`, speed: (v) => `${v}×`,
    minUL: levelText, minWealth: levelText, minMedal: levelText,
  };

  function renderPanel() {
    $('pon').classList.toggle('on', settings.on);
    $('pon').textContent = settings.on ? '开启' : '关闭';
    root.querySelectorAll('[data-block]').forEach((b) => b.classList.toggle('on', settings.block[b.dataset.block]));
    root.querySelectorAll('input[type=range][data-k]').forEach((r) => {
      if (root.activeElement !== r) r.value = settings[r.dataset.k];
      r.nextElementSibling.textContent = OUT[r.dataset.k](Number(r.value));
    });
    root.querySelectorAll('input[type=checkbox][data-k]').forEach((c) => { c.checked = settings[c.dataset.k]; });
    root.querySelectorAll('[data-area]').forEach((b) => b.classList.toggle('on', Number(b.dataset.area) === settings.area));
    root.querySelectorAll('[data-stroke]').forEach((b) => b.classList.toggle('on', b.dataset.stroke === settings.stroke));
    const list = $('kwlist');
    list.replaceChildren(...settings.keywords.map((k, i) => {
      const s = document.createElement('span');
      const x = document.createElement('button');
      x.textContent = '×';
      x.title = '删除';
      x.addEventListener('click', () => set({ keywords: settings.keywords.filter((_, j) => j !== i) }));
      s.append(k, x);
      return s;
    }));
    renderStat();
  }

  function renderStat() {
    const levels = items.length && !items.some((it) => Number.isFinite(it.ul));
    $('lvlNote').textContent = levels ? '这段弹幕来自存档，没有等级信息：等级屏蔽对它不起作用' : '';
    $('pstat').textContent = net.err ? `弹幕: ${net.err}` : `${S.active.length} on screen · ${items.length} loaded${net.live ? ' · live' : ''}`;
    $('psub').textContent = status?.channel ? `${status.channel}${net.live ? ' · 直播中' : ''}` : '';
    host.dataset.state = JSON.stringify({ on: settings.on, items: items.length, shown: S.active.length, live: net.live,
      err: net.err, clock: S.disp, rect: S.rect, lanes: S.geo?.n ?? 0,
      texts: S.active.slice(-30).map((a) => a.it.x) }); // for the e2e test
  }
  setInterval(() => { if (!panel.hidden || host.isConnected) renderStat(); }, 1000);

  $('pclose').addEventListener('click', () => { panel.hidden = true; });
  $('pon').addEventListener('click', () => set({ on: !settings.on }));
  root.querySelectorAll('[data-block]').forEach((b) => b.addEventListener('click', () => set({ block: { ...settings.block, [b.dataset.block]: !settings.block[b.dataset.block] } })));
  root.querySelectorAll('input[type=range][data-k]').forEach((r) => r.addEventListener('input', () => set({ [r.dataset.k]: Number(r.value) })));
  root.querySelectorAll('input[type=checkbox][data-k]').forEach((c) => c.addEventListener('change', () => set({ [c.dataset.k]: c.checked })));
  root.querySelectorAll('[data-area]').forEach((b) => b.addEventListener('click', () => set({ area: Number(b.dataset.area) })));
  root.querySelectorAll('[data-stroke]').forEach((b) => b.addEventListener('click', () => set({ stroke: b.dataset.stroke })));
  $('reset').addEventListener('click', () => apply({ ...D.DEFAULTS, on: settings.on }, true));
  function addKeyword() {
    const k = $('kw').value.trim();
    if (k && !settings.keywords.includes(k)) set({ keywords: [...settings.keywords, k] });
    $('kw').value = '';
  }
  $('kwadd').addEventListener('click', addKeyword);
  $('kw').addEventListener('keydown', (e) => { if (e.key === 'Enter') addKeyword(); e.stopPropagation(); }); // typing isn't F1 TV's or fone's keys
  panel.addEventListener('keydown', (e) => { if (e.key === 'Escape') panel.hidden = true; e.stopPropagation(); });
  // typing in the panel isn't F1 TV's hotkeys (space would pause the video)
  for (const ev of ['keyup', 'keypress']) panel.addEventListener(ev, (e) => e.stopPropagation());

  // D (Bilibili's key) turns the danmaku on and off, on a video page
  window.addEventListener('keydown', (e) => {
    if (e.code !== 'KeyD' || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || e.repeat || !status) return;
    const t = e.composedPath()[0];
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    e.preventDefault();
    e.stopPropagation();
    set({ on: !settings.on });
  }, true);

  // ---- wiring ---------------------------------------------------------------------------------

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg?.type === 'fone-status') status = msg.status;
  });
  try {
    chrome.storage.sync.get('danmaku').then(({ danmaku }) => apply(danmaku, false), () => {});
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area === 'sync' && ch.danmaku && JSON.stringify(ch.danmaku.newValue) !== JSON.stringify(settings)) apply(ch.danmaku.newValue, false);
    });
  } catch { /* no extension API (reloaded) */ }
  renderPanel();

  globalThis.FoneDanmaku = {
    toggle: () => set({ on: !settings.on }),
    openSettings: () => { panel.hidden = !panel.hidden; renderPanel(); },
    state: () => ({ on: settings.on, items: items.length, shown: S.active.length, live: net.live, err: net.err }),
    // for the playback diagnostics (content.js diagOf): enough to tell from afar whether the
    // danmaku are drawn, and over the picture
    diag: () => {
      const v = globalThis.FonePlayer?.pickVideo();
      const r = v?.getBoundingClientRect();
      const box = (b) => (b ? [b.x, b.y, b.w ?? b.width, b.h ?? b.height].map(Math.round) : null);
      return { on: settings.on, n: items.length, shown: S.active.length, lanes: S.geo?.n ?? 0, live: net.live, err: net.err,
        rect: box(S.rect), video: box(r), vsize: v ? [v.videoWidth, v.videoHeight] : null, img: { ...imgStats } };
    },
  };
})();
