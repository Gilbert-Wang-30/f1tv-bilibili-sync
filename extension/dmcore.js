// Danmaku (弹幕) math shared by danmaku.js and the Node tests: which danmaku show (the
// settings' filters), when (on the commentary's clock), and where (lanes).
//
// Items come from the fone server (GET /api/danmaku): {t: send time (unix s), x: text,
// m: mode (1 scroll, 4 bottom, 5 top), s: size, c: colour, u: user hash} and, when fone heard
// them live: ul user level, ml fan-medal level (this room's), wl honour level (荣耀等级), g guard,
// e {u, w, h} an emoticon danmaku's image, em {"[code]": url} inline emoji; k: 'sc' a super chat.
(function (root) {
  'use strict';

  // Shown this long before their send time: live viewers' players run ~2 s behind the
  // recorder whose audio we play, so this is where they appeared to them.
  const LEAD = 2;
  // A scrolling danmaku crosses the picture in DURATION s at speed 1; top and bottom ones
  // stay FIXED s. Bilibili's are about the same.
  const DURATION = 8;
  const FIXED = 4;
  const GAP = 16; // px between two scrolling danmaku in a lane
  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2];
  const AREAS = [0.25, 0.5, 0.75, 1];
  const STROKES = ['heavy', 'outline', 'shadow'];

  // Bilibili's player settings, by the same names. Levels: 0 = no blocking.
  const DEFAULTS = {
    on: true,
    opacity: 0.9, // 不透明度
    area: 0.5, // 显示区域 (share of the picture's height, from the top)
    font: 1, // 字号 (× each danmaku's own size, scaled to the picture)
    speed: 1, // 弹幕速度
    bold: true, // 粗体
    stroke: 'heavy', // 描边类型: 重墨 / 描边 / 45°投影
    overlap: false, // 允许重叠: when every lane is taken, overlap instead of dropping
    block: { scroll: false, top: false, bottom: false, color: false, emoticon: false, sc: false }, // 屏蔽类型
    minUL: 0, // 屏蔽用户等级低于
    minWealth: 0, // 屏蔽荣耀等级低于
    minMedal: 0, // 屏蔽粉丝牌等级低于 (this room's medal; none counts as 0)
    keywords: [], // 关键词屏蔽 (plain text, or /regex/)
  };

  const clampNum = (v, lo, hi, d) => (Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d);

  // Whatever is stored, a complete and sane settings object.
  function normalize(s) {
    const o = s && typeof s === 'object' ? s : {};
    const block = { ...DEFAULTS.block };
    for (const k of Object.keys(block)) if (typeof o.block?.[k] === 'boolean') block[k] = o.block[k];
    return {
      on: typeof o.on === 'boolean' ? o.on : DEFAULTS.on,
      opacity: clampNum(o.opacity, 0.1, 1, DEFAULTS.opacity),
      area: AREAS.includes(o.area) ? o.area : DEFAULTS.area,
      font: clampNum(o.font, 0.5, 2, DEFAULTS.font),
      speed: clampNum(o.speed, 0.5, 2, DEFAULTS.speed),
      bold: typeof o.bold === 'boolean' ? o.bold : DEFAULTS.bold,
      stroke: STROKES.includes(o.stroke) ? o.stroke : DEFAULTS.stroke,
      overlap: typeof o.overlap === 'boolean' ? o.overlap : DEFAULTS.overlap,
      block,
      minUL: Math.round(clampNum(o.minUL, 0, 60, 0)),
      minWealth: Math.round(clampNum(o.minWealth, 0, 60, 0)),
      minMedal: Math.round(clampNum(o.minMedal, 0, 40, 0)),
      keywords: Array.isArray(o.keywords) ? o.keywords.filter((k) => typeof k === 'string' && k.trim()).map((k) => k.trim()).slice(0, 200) : [],
    };
  }

  // "/re/flags" a regular expression (a bad one matches nothing), anything else plain text.
  function keywordMatchers(keywords) {
    return keywords.map((k) => {
      const m = /^\/(.+)\/([gimsuy]*)$/.exec(k);
      if (m) {
        try { const re = new RegExp(m[1], m[2].replace('g', '')); return (x) => re.test(x); } catch { return () => false; }
      }
      const low = k.toLowerCase();
      return (x) => x.toLowerCase().includes(low);
    });
  }

  // A filter for these settings: (item) -> show it? A level we don't know (danmaku from the
  // archive, recorded before fone listened itself) never blocks.
  function filter(settings) {
    const s = normalize(settings);
    const kw = keywordMatchers(s.keywords);
    return (it) => {
      if (it.k === 'sc') { if (s.block.sc) return false; } else {
        const mode = it.m || 1;
        if ((mode === 1 && s.block.scroll) || (mode === 5 && s.block.top) || (mode === 4 && s.block.bottom)) return false;
      }
      if (s.block.color && it.c != null && it.c !== 0xffffff && it.k !== 'sc') return false;
      if (s.block.emoticon && it.e) return false;
      if (s.minUL && Number.isFinite(it.ul) && it.ul < s.minUL) return false;
      if (s.minWealth && Number.isFinite(it.wl) && it.wl < s.minWealth) return false;
      if (s.minMedal && Number.isFinite(it.ml) && it.ml < s.minMedal) return false;
      return !kw.some((f) => f(it.x));
    };
  }

  const key = (it) => `${it.t}|${it.u || ''}|${it.x}`;
  const showAt = (it) => it.t - LEAD;

  // Where to insert / start looking: the first index whose showAt is >= w (items sorted by t).
  function lowerBound(items, w) {
    let lo = 0, hi = items.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (showAt(items[mid]) < w) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  // Merge new items into a sorted, de-duplicated list; returns how many were new.
  function merge(items, seen, fresh) {
    let n = 0;
    for (const it of fresh) {
      if (!it || typeof it.t !== 'number' || typeof it.x !== 'string') continue;
      const k = key(it);
      if (seen.has(k)) continue;
      seen.add(k);
      items.splice(lowerBound(items, showAt(it)), 0, it);
      n++;
    }
    return n;
  }

  // The commentary's clock as the engine last reported it ({w, at, rate, run}), at wall time
  // `now` (s): where the commentary is, in its own wall-clock time.
  function clockAt(c, now) {
    if (!c || !Number.isFinite(c.w)) return null;
    return c.run ? c.w + Math.max(0, now - c.at) * (c.rate || 1) : c.w;
  }

  // ---- lanes ----------------------------------------------------------------------------
  //
  // The display area is cut into lanes one line high. A scrolling danmaku enters at the
  // right edge and leaves at the left after `dur` seconds of the clock it runs on, so a long
  // one moves faster (Bilibili's way). It takes the topmost lane where (a) the previous one
  // has fully entered (plus GAP) and (b) it won't catch that one up before it leaves; top
  // and bottom danmaku take the first lane whose last one has expired. When no lane is free,
  // the danmaku is dropped, or (overlap) put in the lane that frees first.
  function lanes(n) {
    return { n, scroll: new Array(n).fill(null), top: new Array(n).fill(null), bottom: new Array(n).fill(null) };
  }

  // Scrolling: x(t) = W - v (t - t0) with v = (W + w) / dur. Is lane i free at t0 for one w px wide?
  function scrollFree(prev, t0, W, w, dur) {
    if (!prev) return true;
    const vp = (W + prev.w) / prev.dur;
    const v = (W + w) / dur;
    if (vp * (t0 - prev.t0) < prev.w + GAP) return false; // not fully in yet
    if (v <= vp) return true;
    // our head reaches the left edge (x = 0) no sooner than its tail leaves it
    return t0 + W / v >= prev.t0 + (W + prev.w) / vp;
  }

  // When lane `prev` frees for a newcomer (for overlap mode's least-bad lane).
  const scrollFreesAt = (prev) => (prev ? prev.t0 + prev.dur : -Infinity);

  // Place one danmaku (span lanes tall) at clock time t0; returns its first lane or -1.
  function place(L, kind, t0, W, w, dur, span, overlap) {
    const rows = L[kind];
    const free = (i) => {
      for (let j = i; j < i + span; j++) {
        const prev = rows[j];
        if (kind === 'scroll' ? !scrollFree(prev, t0, W, w, dur) : prev && prev.t0 + prev.dur > t0) return false;
      }
      return true;
    };
    let at = -1;
    for (let i = 0; i + span <= L.n && at < 0; i++) if (free(i)) at = i;
    if (at < 0 && overlap && L.n >= span) {
      let best = Infinity;
      for (let i = 0; i + span <= L.n; i++) {
        const f = Math.max(...rows.slice(i, i + span).map((p) => (kind === 'scroll' ? scrollFreesAt(p) : p ? p.t0 + p.dur : -Infinity)));
        if (f < best) { best = f; at = i; }
      }
    }
    if (at >= 0) for (let j = at; j < at + span; j++) rows[j] = { t0, w, dur };
    return at;
  }

  root.FoneDmCore = { LEAD, DURATION, FIXED, GAP, SPEEDS, AREAS, STROKES, DEFAULTS, normalize, filter, key, showAt,
    lowerBound, merge, clockAt, lanes, place, scrollFree };
})(typeof globalThis !== 'undefined' ? globalThis : this);
