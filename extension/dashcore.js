// Pure logic for the stats dashboard (dashboard.js), shared with the Node tests.
//
// Times are real-world UTC seconds. E is the moment on the F1 TV screen. Nothing the dashboard
// shows may come from after E: the server computes /state at t = E, and car positions are only
// ever interpolated up to E.
(function (root) {
  'use strict';

  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

  // ---- clock -----------------------------------------------------------------------
  //
  // The engine reports E about 5 times a second, read off F1 TV's player, whose position saws
  // by ~50 ms (a sample-and-hold content clock). Stepping the map to every report would make
  // the cars stutter, so the dashboard runs its own clock at the reported speed and pulls it
  // a fraction of the way toward each report. A real jump (seek) or a pause, resume or speed
  // change snaps to the report at once.
  //   JUMP  an error this large is a seek, not jitter (paused: STEP, there's no jitter then)
  //   GAIN  share of the error taken per report (~1 s time constant at 5 Hz)
  //   HOLD  no report for this long (tab hidden, engine gone): stop where the picture
  //         probably is instead of running on into the future
  //   STALL a player that is buffering still says it plays, but its E stands still: judged
  //         over at least BASE seconds of reports, an E that moved less than SHARE of what the
  //         speed says means the picture is held, and the clock holds too
  const CLOCK = { JUMP: 1.0, STEP: 0.25, GAIN: 0.2, HOLD: 1.5, STALL: { BASE: 0.35, SHARE: 0.25 } };

  // A clock is {E, at, rate, playing, held, ref, on}: E at local time `at` (also when the last
  // report came); ref = the report {E, at} the stall check measures from; on = the engine said
  // playing (playing = on and not held).
  function clockAt(c, now) {
    if (!c) return null;
    if (!c.playing) return c.E;
    return c.E + clamp(now - c.at, 0, CLOCK.HOLD) * c.rate;
  }

  // msg: the engine's {E, at, rate, playing}. Returns {clock, jump, toggled}: jump = E moved by
  // more than jitter (or the first report), toggled = play/pause (or held) or speed changed.
  function clockStep(prev, msg, now) {
    if (!Number.isFinite(msg.E)) return { clock: prev, jump: false, toggled: false };
    const rate = msg.rate > 0 ? msg.rate : 1;
    const at = msg.at ?? now;
    let held = !!prev?.held, ref = prev?.ref;
    if (!msg.playing || !prev?.on || !ref || at < ref.at) { // (re)starting: measure from here
      held = false;
      ref = { E: msg.E, at };
    } else if (at - ref.at >= CLOCK.STALL.BASE) {
      held = msg.E - ref.E < CLOCK.STALL.SHARE * (at - ref.at) * rate;
      ref = { E: msg.E, at };
    }
    const playing = !!msg.playing && !held;
    const reported = playing ? msg.E + clamp(now - at, 0, CLOCK.HOLD) * rate : msg.E;
    const cur = clockAt(prev, now);
    const jump = cur == null || Math.abs(reported - cur) > (playing && prev.playing ? CLOCK.JUMP : CLOCK.STEP);
    const toggled = !!prev && (prev.playing !== playing || prev.rate !== rate);
    const E = jump || toggled || !playing ? reported : cur + (reported - cur) * CLOCK.GAIN;
    return { clock: { E, at: now, rate, playing, held, ref, on: !!msg.playing }, jump, toggled };
  }

  // A standalone window follows one F1 TV tab: the one it has while that one is alive and
  // playing, else the most recently heard playing tab, else it stays put (showing where that
  // tab stopped). tabs: Map id -> {msg, rx}.
  const TAB_ALIVE = 2.5;
  function pickTab(tabs, cur, now) {
    const alive = (t) => !!t && now - t.rx <= TAB_ALIVE;
    const c = tabs.get(cur);
    if (alive(c) && c.msg.playing) return cur;
    let best = null;
    for (const [id, t] of tabs) {
      if (alive(t) && t.msg.playing && (best == null || t.rx > tabs.get(best).rx)) best = id;
    }
    if (best != null) return best;
    if (c) return cur;
    for (const [id, t] of tabs) if (best == null || t.rx > tabs.get(best).rx) best = id;
    return best;
  }

  // ---- car positions ---------------------------------------------------------------
  //
  // /positions windows are fetched in fixed 30 s chunks (chunk k covers sensor times
  // [30k, 30k+30]) so seeks, prefetch and re-visits all share one cache. The server adds the
  // sample just before each window's start, so a chunk alone can interpolate from its first
  // frame.
  //   GAP   two samples further apart than this are a dropout (or a garage visit): don't
  //         draw a straight line across it
  //   HOLD  past the last sample, keep a car where it was this long, then drop it
  //   SETTLE  live: samples reach the server this long after their sensor time, so a chunk
  //         ending later than (fetch time - SETTLE) may still be filling up
  const POS = { CHUNK: 30, GAP: 2.5, HOLD: 1.2, SETTLE: 8, KEEP: 12 };
  const chunkOf = (t) => Math.floor(t / POS.CHUNK);

  // The chunks to have at E: the one holding E and the next (the lookahead: at 2x a chunk
  // lasts 15 s; paused near a chunk's end, the next sample after E lives in the next one).
  function chunksFor(E) {
    const k = chunkOf(E);
    return [k, k + 1];
  }

  // A chunk fetched at wall time `fetchedAt` is final once its end is safely in the past.
  const chunkComplete = (k, fetchedAt) => (k + 1) * POS.CHUNK <= fetchedAt - POS.SETTLE;

  // chunks: [{k, cars: {num: [[t, x, y], ...]}}] -> {num: one ascending, de-duplicated array}.
  function mergeChunks(chunks) {
    const out = {};
    for (const ch of [...chunks].sort((a, b) => a.k - b.k)) {
      for (const [num, pts] of Object.entries(ch.cars || {})) {
        const arr = (out[num] ||= []);
        for (const p of pts) if (!arr.length || p[0] > arr[arr.length - 1][0]) arr.push(p);
      }
    }
    return out;
  }

  // Where a car was at E, from its samples s ([[t, x, y], ...], ascending): interpolated
  // between the samples around E, held briefly after the last one, null when it has no
  // data there (garage, dropout, not loaded). Never anything from a sample after E alone.
  function carAt(s, E) {
    if (!s || !s.length) return null;
    let lo = 0, hi = s.length - 1, i = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (s[mid][0] <= E) { i = mid; lo = mid + 1; } else hi = mid - 1;
    }
    if (i < 0) return null;
    const a = s[i], b = s[i + 1];
    if (b && b[0] - a[0] <= POS.GAP) {
      const f = (E - a[0]) / (b[0] - a[0]);
      return [a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
    }
    return E - a[0] <= POS.HOLD ? [a[1], a[2]] : null;
  }

  // ---- session clock ---------------------------------------------------------------

  // "MM:SS" (minutes may run past 59) or "H:MM:SS" -> seconds
  function parseClock(s) {
    if (typeof s !== 'string') return null;
    const p = s.trim().split(':').map(Number);
    if (!p.length || p.length > 3 || p.some((x) => !Number.isFinite(x))) return null;
    return p.reduce((a, x) => a * 60 + x, 0);
  }

  const fmtClock = (sec) => {
    if (sec == null) return '';
    const s = Math.max(0, Math.round(sec));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = String(s % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${String(m).padStart(2, '0')}:${ss}`;
  };

  // The countdown between polls. The server sends it at the state's t rounded up to whole
  // seconds, so each poll says the true remaining time lies in (S-1, S]. Keeping the
  // intersection of those intervals (moved along by the elapsed time while the clock runs)
  // pins it within a fraction of a second after a few polls, so the display ticks on time
  // instead of whenever a poll lands. It shows the interval's top: that only ever moves down
  // as polls come in, so the display never flickers back up a second.
  function countdownStep(prev, st) {
    const S = parseClock(st?.clock?.remaining);
    if (S == null) return null;
    const running = st.status === 'Started' && S > 0;
    let lo = S - 1, hi = S;
    if (prev && prev.running === running && st.t >= prev.t && st.t - prev.t < 30) {
      const d = running ? st.t - prev.t : 0;
      const nlo = Math.max(lo, prev.lo - d), nhi = Math.min(hi, prev.hi - d);
      if (nlo <= nhi) { lo = nlo; hi = nhi; }
    }
    return { lo, hi, t: st.t, running };
  }

  function countdownAt(c, E) {
    if (!c) return null;
    const d = c.running ? Math.max(0, E - c.t) : 0;
    return Math.max(0, Math.ceil(c.hi - d - 1e-6));
  }

  // ---- reading the state -----------------------------------------------------------

  // Same split as the server (timing_state.session_kind).
  function sessionKind(session) {
    const typ = String(session?.type || '').toLowerCase(), name = String(session?.name || '').toLowerCase();
    if (typ === 'qualifying' || name.includes('qualifying') || name.includes('shootout')) return 'quali';
    if (typ === 'race' || name === 'race' || name === 'sprint') return 'race';
    return 'practice';
  }

  function partLabel(st) {
    if (!st?.part || sessionKind(st.session) !== 'quali') return '';
    const name = String(st.session?.name || '').toLowerCase();
    return (name.includes('sprint') || name.includes('shootout') ? 'SQ' : 'Q') + st.part;
  }

  // The tower's heading: the qualifying part (SQ1), else the session's short name.
  function towerTitle(st) {
    const part = partLabel(st);
    if (part) return part;
    const name = String(st?.session?.name || '');
    const fp = /^practice\s*(\d)$/i.exec(name);
    if (fp) return `FP${fp[1]}`;
    if (sessionKind(st?.session) === 'quali') return /sprint|shootout/i.test(name) ? 'SQ' : 'Q';
    return name.toUpperCase();
  }

  const ENDED = new Set(['Finished', 'Finalised', 'Ends']);

  // The flag chip: what the track looks like at E.
  //  * The session status wins over the track status: during a red-flag stop (status Aborted)
  //    race control goes on posting track states (TRACK CLEAR, sector yellows) that the TV
  //    doesn't show.
  //  * Inactive is both "not started yet" (before a session or a qualifying part) and "waiting
  //    for the restart" after a red flag (pit exit still closed, clock stopped); the second has
  //    lap times in the current part.
  function flagChip(st) {
    const f = st?.track?.flag;
    if (ENDED.has(st?.status) || f === 'CHEQUERED') return { cls: 'chequered', text: 'CHEQUERED' };
    if (st?.status === 'Aborted' || f === 'RED') return { cls: 'red', text: 'RED FLAG' };
    if (st?.status === 'Inactive') {
      const running = (st.tower || []).some((r) => !r.knockedOut && (r.best || r.last));
      return running ? { cls: 'red', text: 'RED FLAG' } : { cls: 'idle', text: 'NOT STARTED' };
    }
    switch (f) {
      case 'SC': return { cls: 'sc', text: 'SAFETY CAR' };
      case 'VSC': return { cls: 'sc', text: st.track.status === 'VSCEnding' ? 'VSC ENDING' : 'VIRTUAL SC' };
      case 'YELLOW': return { cls: 'yellow', text: 'YELLOW' };
      case 'DOUBLE YELLOW': return { cls: 'yellow', text: 'DOUBLE YELLOW' };
      default: return { cls: 'green', text: 'GREEN' };
    }
  }

  // "1:33.748" / "59.123" -> seconds
  function lapSeconds(s) {
    if (typeof s !== 'string' || !s) return null;
    const v = parseClock(s);
    return v != null && v > 0 ? v : null;
  }

  // Lapped cars come as "1L", "+1 LAP", "2 LAPS"; a race leader's gap is "LAP 23".
  function fmtGap(g) {
    if (!g) return '';
    const m = /^\+?\s*(\d+)\s*L(?:APS?)?$/i.exec(String(g).trim());
    return m ? `+${m[1]} LAP${m[1] === '1' ? '' : 'S'}` : String(g);
  }

  // The tower's gap and interval cells for one row.
  function gapCells(r, kind) {
    if (r.pos === 1) {
      const leads = kind === 'race' ? true : !!(r.best || r.last);
      return { gap: leads ? 'LEADER' : '', interval: '' };
    }
    return { gap: fmtGap(r.gap), interval: fmtGap(r.interval) };
  }

  // Index of the session's fastest best lap (purple), ignoring knocked-out drivers, whose time
  // is from an earlier qualifying part.
  function fastestRow(tower) {
    let best = -1, bt = Infinity;
    (tower || []).forEach((r, i) => {
      const t = r.knockedOut ? null : lapSeconds(r.best);
      if (t != null && t < bt) { bt = t; best = i; }
    });
    return best;
  }

  // The status mark at the end of a tower row: retired beats pit states.
  function rowMark(r) {
    if (r.retired) return { cls: 'ret', text: 'OUT' };
    if (r.inPit) return { cls: 'pit', text: 'IN PIT' };
    if (r.pitOut) return { cls: 'pitout', text: 'PIT OUT' };
    return null;
  }

  const TYRES = { SOFT: 'S', MEDIUM: 'M', HARD: 'H', INTERMEDIATE: 'I', WET: 'W' };
  const tyreLetter = (c) => TYRES[c] || '?';

  // Qualifying drop zone: after the part's last safe position (Q1 knocks out half of the cars
  // beyond 10, Q2 everyone beyond 10). null when there's no line to draw.
  function cutAfter(st) {
    if (sessionKind(st?.session) !== 'quali' || !(st.part === 1 || st.part === 2)) return null;
    const n = (st.tower || []).length;
    if (n <= 10) return null;
    return st.part === 1 ? n - Math.round((n - 10) / 2) : 10;
  }

  const COMPASS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  const compass = (deg) => (Number.isFinite(deg) ? COMPASS[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16] : '');

  // ---- layout ----------------------------------------------------------------------
  //
  // Tower columns in em of the tower's font size; a fixed template keeps the font size (set
  // from the panel's width) from changing with what's in the cells.
  const TOWER = {
    COLS: [['pos', 1.35], ['drv', 3.3], ['gap', 4.1], ['int', 4.1], ['last', 4.15], ['best', 4.15],
      ['tyre', 2.75], ['pits', 1.2], ['mark', 3.85]],
    GAP: 0.42, PADX: 0.55, PADY: 0.4, HEAD: 1.55, ROW_MIN: 1.5, ROW_MAX: 2.6, FS_MIN: 8, FS_MAX: 30,
  };
  const emOf = (cols) => cols.reduce((a, c) => a + c[1], 0) + TOWER.GAP * (cols.length - 1) + 2 * TOWER.PADX;
  TOWER.EM = emOf(TOWER.COLS);

  // The column widths measured in the font in use (dashboard.js measureTower): {key: em}.
  // A Mac falls back from Segoe UI to SF Pro, whose digits are wider, and fixed widths clipped
  // its lap times there. Keys and order stay; a missing or silly value keeps the default.
  function setTowerCols(widths) {
    TOWER.COLS = TOWER.COLS.map(([k, w]) => [k, widths?.[k] > 0.5 && widths[k] < 12 ? Math.round(widths[k] * 100) / 100 : w]);
    TOWER.EM = emOf(TOWER.COLS);
  }
  // A narrow column (the player box takes 77% of the width) drops what matters least, the pit
  // count and then the interval, before every lap time and gap shrinks below a size readable
  // at a glance.
  const TOWER_DROP = ['pits', 'int'];
  const TOWER_COMFY_FS = 11;

  function towerColumns(w) {
    let cols = TOWER.COLS;
    for (const key of TOWER_DROP) {
      if (w / emOf(cols) >= TOWER_COMFY_FS) break;
      cols = cols.filter(([k]) => k !== key);
    }
    return { cols, em: emOf(cols), hide: TOWER_DROP.filter((k) => !cols.some(([c]) => c === k)) };
  }

  // Font size and row height for a w x h tower holding n rows: as large as the width allows,
  // smaller only when the rows don't fit; spare height goes into taller rows (up to ROW_MAX).
  // Also which columns it shows (towerColumns).
  function towerMetrics(w, h, n) {
    const rows = Math.max(n || 0, 10);
    const { cols, em, hide } = towerColumns(w);
    const fsW = w / em;
    const fsH = h / (TOWER.HEAD + 2 * TOWER.PADY + rows * TOWER.ROW_MIN);
    const fs = clamp(Math.min(fsW, fsH), TOWER.FS_MIN, TOWER.FS_MAX);
    const rowH = clamp((h - fs * (TOWER.HEAD + 2 * TOWER.PADY)) / rows, fs * TOWER.ROW_MIN, fs * TOWER.ROW_MAX);
    return { fs, rowH, cols, hide };
  }

  // Height a tower needs at the font size its width allows.
  function towerNeed(w, n) {
    const fs = clamp(w / towerColumns(w).em, TOWER.FS_MIN, TOWER.FS_MAX);
    return fs * (TOWER.HEAD + 2 * TOWER.PADY + Math.max(n || 0, 10) * TOWER.ROW_MIN);
  }

  // Before the layout script reports the player's box: the contract's default (top-left,
  // 77% of the width, 16:9).
  function defaultRect(vw, vh) { // = layout.js boxFor: whole 16x9 px steps, full height at most
    let n = Math.round((vw * 0.77) / 16);
    if (n * 9 > vh) n = Math.floor(vh / 9);
    return { x: 0, y: 0, w: n * 16, h: n * 9 };
  }

  // The player's box in 'onboards' mode: the largest (from the usual 77% down to half the width)
  // that leaves a strip below it holding at least ONBOARDS_MIN full-height onboards beside a map
  // no wider than MAP_SLACK of what its track needs; failing that, the largest with the onboards
  // at all. null: no such box (a window too small or too narrow), the usual one stays.
  const ONBOARD_BOX = { MIN_SHARE: 0.5, ONBOARDS_MIN: 2, MAP_SLACK: 1.3 };
  function onboardBox({ vw, vh, rows, mapAspect }) {
    const top = defaultRect(vw, vh).w / 16;
    let fallback = null;
    for (let n = top; n * 16 >= vw * ONBOARD_BOX.MIN_SHARE; n--) {
      const rect = { x: 0, y: 0, w: 16 * n, h: 9 * n };
      const L = layout({ mode: 'iframe', vw, vh, rect, rows, mapAspect });
      if (L.kind !== 'L' || L.slots.length < ONBOARD_BOX.ONBOARDS_MIN) continue;
      const need = Math.max(SLOT.MAP.MIN, L.map.h * ((mapAspect > 0 ? mapAspect : 2) + SLOT.MAP.PAD));
      if (L.map.w <= need * ONBOARD_BOX.MAP_SLACK) return rect;
      fallback ??= rect;
    }
    return fallback;
  }

  // The conditions panel is a compact card: flag, clock and weather need little room, the map
  // and the onboards make better use of it. Beside the map it's about as wide as the strip is
  // tall (W: K x height within [MIN, MAX], at most SHARE of the strip); on top of a column
  // it's a short bar (BAR: K x the column's height within [MIN, MAX]).
  const COND = { W: { K: 1.1, MIN: 200, MAX: 300, SHARE: 0.4 }, BAR: { K: 0.1, MIN: 76, MAX: 120 } };

  // Driver onboards side by side at the right end of the strip under the player (iframe L
  // layout), up to OB_SLOTS. A slot is a slim header of ours (the driver picker) over a 16:9
  // video area where the page shows F1 TV's onboard view, above this frame; nothing of ours is
  // drawn there.
  //   HEAD   the header: at least K x the strip's height within [MIN, MAX] px
  //   video  whole 16x9 px steps (like layout.js's player box, so F1 TV's 16:9 view fills it
  //          with no seam), as tall as the strip leaves below the header; the header takes the
  //          rest of the strip's height (under one 9 px step)
  // MAP: the width the track needs at the strip's height (its aspect, + PAD of the height for
  // car labels and corner numbers), at least MIN; the onboards may squeeze it to FLEX of that
  // (the track then drawn a little smaller). STEPS: the smallest video, 16:9 steps of 16x9 px
  // (192x108); smaller isn't worth watching: no slots.
  const SLOT = { HEAD: { K: 0.085, MIN: 20, MAX: 26 }, MAP: { MIN: 240, PAD: 0.25, FLEX: 0.8 }, STEPS: 12 };
  const OB_SLOTS = 4;

  // The slots at the strip's right end, given the width the conditions take at its left:
  // [{x, y, w, h, video: {x, y, w, h}}] (the header is the slot above video.y), or [].
  // The strip under the player: [cond] g [map] g [slot] g [slot] ... Every onboard as tall as the
  // strip (no black bars above and below its video: the header takes the few px over), as many
  // as fit (up to OB_SLOTS) beside a map of at least FLEX of the width its track needs
  // (mapAspect: the outline's width over height, as drawn); the map gets whatever is left.
  function onboardSlots(strip, condW, g, mapAspect) {
    const head = Math.round(clamp(strip.h * SLOT.HEAD.K, SLOT.HEAD.MIN, SLOT.HEAD.MAX));
    const steps = Math.floor((strip.h - head) / 9);
    if (steps < SLOT.STEPS) return [];
    const w = 16 * steps, vh = 9 * steps, hh = strip.h - vh;
    const need = Math.max(SLOT.MAP.MIN, strip.h * ((mapAspect > 0 ? mapAspect : 2) + SLOT.MAP.PAD));
    const mapMin = Math.max(SLOT.MAP.MIN, need * SLOT.MAP.FLEX);
    for (let n = OB_SLOTS; n >= 1; n--) {
      if (strip.w - condW - 2 * g - n * w - (n - 1) * g < mapMin) continue;
      const right = strip.x + strip.w;
      return Array.from({ length: n }, (_, k) => {
        const x = right - (n - k) * w - (n - 1 - k) * g;
        return { x, y: strip.y, w, h: strip.h, head: hh, video: { x, y: strip.y + hh, w, h: vh } };
      });
    }
    return [];
  }

  // Conditions on top, then the tower, then the map, in one column (area a).
  function stack(a, n, g) {
    const condH = Math.round(clamp(a.h * COND.BAR.K, COND.BAR.MIN, COND.BAR.MAX));
    const mapMin = Math.max(a.h * 0.26, 150);
    const mapH = Math.round(Math.max(mapMin, a.h - condH - towerNeed(a.w, n) - 2 * g));
    const towerH = a.h - condH - mapH - 2 * g;
    return {
      cond: { x: a.x, y: a.y, w: a.w, h: condH },
      tower: { x: a.x, y: a.y + condH + g, w: a.w, h: towerH },
      map: { x: a.x, y: a.y + condH + towerH + 2 * g, w: a.w, h: mapH },
    };
  }

  // Panel boxes in CSS px: {kind, tower, cond, map, slots} (a panel that doesn't fit is null;
  // slots: onboardSlots, [] when there are none).
  //  iframe: the page lies behind F1 TV's player (rect); panels only go around it. The tower
  //    takes the column to its right, from the top down to the player's bottom edge; the strip
  //    below both holds the conditions, the map beside them and the onboard slots at the right
  //    end. When the strip is too short to read (wide windows), everything stacks in a
  //    full-height column; when there's no column but a tall strip, everything goes in the
  //    strip. No onboards in either (onboardBox: a smaller player box that has them).
  //  standalone: the tower beside conditions-over-map when the window is wide, else stacked.
  function layout({ mode, vw, vh, rect, rows, mapAspect }) {
    const n = rows || 20;
    const g = Math.round(clamp(Math.min(vw, vh) * 0.008, 6, 12));
    const none = { kind: 'none', tower: null, cond: null, map: null, slots: [] };
    if (vw < 120 || vh < 120) return none;
    if (mode === 'iframe') {
      const r = rect || defaultRect(vw, vh);
      const rx = Math.max(0, r.x + r.w), ry = Math.max(0, r.y + r.h);
      const col = { x: rx + g, y: g, w: vw - rx - 2 * g, h: vh - 2 * g };
      const strip = { x: g, y: ry + g, w: vw - 2 * g, h: vh - ry - 2 * g };
      const towerOk = col.w >= 220 && ry - g >= 240;
      const stripOk = strip.h >= Math.max(150, vh * 0.14) && strip.w >= 420;
      if (towerOk && stripOk) {
        const condW = Math.round(Math.min(clamp(strip.h * COND.W.K, COND.W.MIN, COND.W.MAX), strip.w * COND.W.SHARE));
        const slots = onboardSlots(strip, condW, g, mapAspect);
        const mapX = strip.x + condW + g;
        const mapEnd = slots.length ? slots[0].x - g : strip.x + strip.w;
        return {
          kind: 'L',
          tower: { ...col, h: ry - g }, // its bottom border on the player's
          cond: { x: strip.x, y: strip.y, w: condW, h: strip.h },
          map: { x: mapX, y: strip.y, w: mapEnd - mapX, h: strip.h },
          slots,
        };
      }
      if (col.w >= 220 && col.h >= 240) return { kind: 'column', ...stack(col, n, g), slots: [] };
      if (strip.h >= vh * 0.4 && strip.w >= 360) return { kind: 'strip', ...stack(strip, n, g), slots: [] };
      return none;
    }
    const a = { x: g, y: g, w: vw - 2 * g, h: vh - 2 * g };
    if (a.w >= 860 && a.h >= 420) {
      // the tower as wide as its height lets its font grow, at most about half the window
      const fsH = a.h / (TOWER.HEAD + 2 * TOWER.PADY + n * TOWER.ROW_MIN);
      const tw = Math.round(clamp(TOWER.EM * fsH, 360, a.w * 0.5));
      const rw = a.w - tw - g;
      const condH = Math.round(clamp(a.h * 0.13, 84, 130)); // a short bar over the map
      return {
        kind: 'grid',
        tower: { x: a.x, y: a.y, w: tw, h: a.h },
        cond: { x: a.x + tw + g, y: a.y, w: rw, h: condH },
        map: { x: a.x + tw + g, y: a.y + condH + g, w: rw, h: a.h - condH - g },
        slots: [],
      };
    }
    return { kind: 'stack', ...stack(a, n, g), slots: [] };
  }

  // ---- onboards ------------------------------------------------------------------------
  //
  // The drivers' onboard streams of one F1 TV content, from the fone server (/api/f1tv/{cid}
  // "streams": [{channelId, type: 'obc' | 'additional', title, number, name, team, color}]),
  // as picker entries [{num, channelId, code, color, name}] in the timing tower's order (its
  // codes and colours, so they match the tower), the rest by number. Only 'obc' (a driver's
  // onboard); 'additional' are the other feeds (F1 LIVE, TRACKER, ...). null: no list.
  // Channel ids are whole numbers, as the page matches them (page.js: Number.isInteger).
  function onboardList(streams, tower) {
    if (!Array.isArray(streams)) return null;
    const rows = new Map((tower || []).map((r, i) => [Number(r.num), { r, i }]));
    const out = new Map();
    const whole = (x) => (x !== null && x !== '' && Number.isInteger(Number(x)) && Number(x) > 0 ? Number(x) : null);
    for (const s of streams) {
      const num = whole(s?.number), channelId = whole(s?.channelId);
      if (s?.type !== 'obc' || num == null || channelId == null || out.has(num)) continue;
      const row = rows.get(num);
      out.set(num, {
        num, channelId, code: row?.r.code || s.title || String(num), color: row?.r.color || s.color || '',
        name: row?.r.name || s.name || '', order: row ? row.i : 1000 + num,
      });
    }
    return [...out.values()].sort((a, b) => a.order - b.order);
  }

  // The choice for slot i becomes num (null: none). A driver already in another slot moves
  // over: that slot takes this one's old driver (a swap), so a choice is never lost.
  function pickOnboard(picks, i, num) {
    const out = [...picks];
    const j = num == null ? -1 : out.findIndex((x, k) => k !== i && x === num);
    if (j >= 0) out[j] = out[i];
    out[i] = num;
    return out;
  }

  // chrome.storage's 'onboards' as OB_SLOTS of num|null, whatever was stored (two before 0.2.17).
  function onboardPicks(v) {
    return Array.from({ length: OB_SLOTS }, (_, i) => (Array.isArray(v) && Number.isInteger(v[i]) && v[i] > 0 ? v[i] : null));
  }

  // Width over height of the track as the map draws it in a wide box: turned by the server's
  // rotation, long side horizontal (fitTrack gives a tall one a quarter turn there).
  function trackAspect(outline, rotationDeg) {
    if (!outline || outline.length < 3) return null;
    const rad = ((rotationDeg || 0) * Math.PI) / 180, c = Math.cos(rad), s = Math.sin(rad);
    let u0 = Infinity, u1 = -Infinity, v0 = Infinity, v1 = -Infinity;
    for (const [x, y] of outline) {
      const u = x * c - y * s, v = x * s + y * c;
      u0 = Math.min(u0, u); u1 = Math.max(u1, u); v0 = Math.min(v0, v); v1 = Math.max(v1, v);
    }
    const a = (u1 - u0) / Math.max(1e-9, v1 - v0);
    return Math.max(a, 1 / a);
  }

  // ---- track map -------------------------------------------------------------------
  //
  // F1 X/Y units, y up. The server's rotation turns the outline's long side horizontal
  // (x' = x cos r - y sin r, y' = x sin r + y cos r); a portrait box gets a quarter turn more
  // when that fills it clearly better. Returns what project() needs, or null.
  function fitTrack(outline, rotationDeg, w, h, pad) {
    if (!outline || outline.length < 3 || w <= 2 * pad || h <= 2 * pad) return null;
    let best = null;
    for (const quarter of [0, 90]) {
      const rad = (((rotationDeg || 0) + quarter) * Math.PI) / 180;
      const c = Math.cos(rad), s = Math.sin(rad);
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      for (const [x, y] of outline) {
        const u = x * c - y * s, v = x * s + y * c;
        if (u < x0) x0 = u;
        if (u > x1) x1 = u;
        if (v < y0) y0 = v;
        if (v > y1) y1 = v;
      }
      const k = Math.min((w - 2 * pad) / Math.max(1, x1 - x0), (h - 2 * pad) / Math.max(1, y1 - y0));
      if (!best || k > best.k * 1.15) {
        best = { k, c, s, x0, y1, ox: (w - k * (x1 - x0)) / 2, oy: (h - k * (y1 - y0)) / 2 };
      }
    }
    return best;
  }

  function project(f, x, y) {
    const u = x * f.c - y * f.s, v = x * f.s + y * f.c;
    return [f.ox + (u - f.x0) * f.k, f.oy + (f.y1 - v) * f.k];
  }

  // Where a corner's number goes: gap px off the track, in the direction the circuit data
  // gives (c.a, degrees in the track's own y-up axes), turned with the map. Pixels rather
  // than track units, so the numbers hug the track at any map size.
  function cornerSpot(f, c, gap) {
    const a = (c.a * Math.PI) / 180;
    const [x0, y0] = project(f, c.x, c.y);
    const [x1, y1] = project(f, c.x + Math.cos(a), c.y + Math.sin(a));
    const n = Math.hypot(x1 - x0, y1 - y0) || 1;
    return [x0 + ((x1 - x0) / n) * gap, y0 + ((y1 - y0) / n) * gap];
  }

  // Car labels: each one beside its dot, on a side where it hits no other label or dot.
  // Dots come in priority order (race order); a label keeps last frame's side while that
  // side stays free, so labels don't flicker between sides as cars move. When no side is
  // free it goes where it overlaps least (inside the map), marked crowded (drawn fainter),
  // unless it's optional (a car in the pit lane): then it has none.
  // dots: [{id, x, y, w, h, optional}] (label size), r: dot radius, bounds: {w, h},
  // prev: Map id -> side. Returns Map id -> {side, x, y, crowded} (no entry: no label).
  const SIDES = ['e', 'w', 'n', 's', 'ne', 'se', 'nw', 'sw'];
  function labelBox(d, side, r) {
    const gap = r + 2;
    const x = side.includes('e') ? d.x + gap : side.includes('w') ? d.x - gap - d.w : d.x - d.w / 2;
    const y = side.includes('n') ? d.y - gap - d.h : side.includes('s') ? d.y + gap : d.y - d.h / 2;
    return { x, y, w: d.w, h: d.h };
  }

  function placeLabels(dots, r, bounds, prev) {
    const boxes = [];
    const out = new Map();
    const area = (a, b) => Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x))
      * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
    const inside = (b) => !bounds || (b.x >= 0 && b.y >= 0 && b.x + b.w <= bounds.w && b.y + b.h <= bounds.h);
    // how much of other labels and dots a label box would cover (0: free)
    const clash = (b, self) => boxes.reduce((a, o) => a + area(b, o), 0)
      + dots.reduce((a, o) => (o === self ? a : a + area(b, { x: o.x - r, y: o.y - r, w: 2 * r, h: 2 * r })), 0);
    for (const d of dots) {
      const last = prev && prev.get(d.id);
      const order = last ? [last, ...SIDES.filter((s) => s !== last)] : SIDES;
      let side = order.find((s) => { const b = labelBox(d, s, r); return inside(b) && clash(b, d) === 0; });
      const crowded = !side;
      if (crowded && d.optional) continue;
      if (crowded) {
        const fits = order.filter((s) => inside(labelBox(d, s, r)));
        const pick = fits.length ? fits : order;
        side = pick.reduce((a, s) => (clash(labelBox(d, s, r), d) < clash(labelBox(d, a, r), d) ? s : a));
      }
      const b = labelBox(d, side, r);
      boxes.push(b);
      out.set(d.id, { side, x: b.x, y: b.y, crowded });
    }
    return out;
  }

  root.FoneDash = {
    CLOCK, POS, TOWER, COND, SLOT, OB_SLOTS, TAB_ALIVE, clamp,
    clockAt, clockStep, pickTab,
    chunkOf, chunksFor, chunkComplete, mergeChunks, carAt,
    parseClock, fmtClock, countdownStep, countdownAt,
    sessionKind, partLabel, towerTitle, flagChip, lapSeconds, fmtGap, gapCells, fastestRow, rowMark, tyreLetter, cutAfter, compass,
    towerMetrics, towerColumns, towerNeed, setTowerCols, defaultRect, onboardBox, layout, onboardSlots, trackAspect,
    onboardList, pickOnboard, onboardPicks,
    fitTrack, project, cornerSpot, placeLabels, labelBox,
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
