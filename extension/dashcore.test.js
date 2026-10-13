// node extension/dashcore.test.js
require('./dashcore.js');
const assert = require('node:assert/strict');
const D = globalThis.FoneDash;
let n = 0;
const test = (name, fn) => { fn(); n++; console.log('ok -', name); };
const near = (a, b, eps, msg) => assert.ok(Math.abs(a - b) <= eps, `${msg || ''} ${a} vs ${b} (±${eps})`);

const T0 = 1791549902; // mid-SQ1, Singapore 2026

// ---- clock ---------------------------------------------------------------------------

const report = (E, at, extra) => ({ E, at, rate: 1, playing: true, ...extra });

test('clock: the first report snaps and counts as a jump; then it extrapolates at the rate', () => {
  const { clock, jump } = D.clockStep(null, report(T0, 100), 100.01);
  assert.ok(jump);
  near(D.clockAt(clock, 100.01), T0 + 0.01, 1e-9);
  near(D.clockAt(clock, 100.51), T0 + 0.51, 1e-9);
});

test('clock: report jitter of ±50 ms is smoothed, not followed', () => {
  let c = D.clockStep(null, report(T0, 0), 0).clock;
  let maxStep = 0, prev = D.clockAt(c, 0), prevT = 0;
  for (let i = 1; i <= 100; i++) {
    const t = i * 0.2;
    const jitter = (i % 2 ? 1 : -1) * 0.05; // a sawtooth as bad as F1 TV's content clock
    const r = D.clockStep(c, report(T0 + t + jitter, t), t);
    assert.ok(!r.jump && !r.toggled);
    c = r.clock;
    for (let k = 0; k < 12; k++) { // ~60 fps between reports
      const ft = t + (k * 0.2) / 12;
      const e = D.clockAt(c, ft);
      maxStep = Math.max(maxStep, Math.abs(e - prev - (ft - prevT)));
      prev = e;
      prevT = ft;
    }
  }
  near(D.clockAt(c, 20), T0 + 20, 0.03, 'tracks the truth');
  assert.ok(maxStep < 0.02, `frame-to-frame wobble ${maxStep}`); // raw reports would wobble 100 ms
});

test('clock: a seek snaps at once; a pause freezes on the reported frame; resume and speed changes snap', () => {
  let c = D.clockStep(null, report(T0, 0), 0).clock;
  let r = D.clockStep(c, report(T0 + 600, 1), 1);
  assert.ok(r.jump);
  near(D.clockAt(r.clock, 1), T0 + 600, 1e-9);
  r = D.clockStep(r.clock, report(T0 + 600.4, 1.4, { playing: false }), 1.4);
  assert.ok(r.toggled && !r.jump);
  assert.equal(D.clockAt(r.clock, 50), T0 + 600.4);
  r = D.clockStep(r.clock, report(T0 + 600.4, 60), 60);
  assert.ok(r.toggled);
  r = D.clockStep(r.clock, report(T0 + 601, 60.5, { rate: 2 }), 60.5);
  assert.ok(r.toggled);
  near(D.clockAt(r.clock, 61), T0 + 602, 1e-9);
});

test('clock: paused, the picture stepped a little is a jump too (the state must follow)', () => {
  const paused = D.clockStep(null, report(T0, 0, { playing: false }), 0).clock;
  assert.ok(!D.clockStep(paused, report(T0, 1, { playing: false }), 1).jump);
  const r = D.clockStep(paused, report(T0 + 0.5, 2, { playing: false }), 2);
  assert.ok(r.jump);
  assert.equal(D.clockAt(r.clock, 9), T0 + 0.5);
});

test('clock: a buffering player says it plays but its E stands still: the clock holds', () => {
  let r = D.clockStep(null, report(T0, 0), 0);
  for (let t = 0.2; t < 1.01; t += 0.2) r = D.clockStep(r.clock, report(T0 + t, t), t);
  assert.ok(r.clock.playing);
  const E1 = T0 + 1;
  let maxLead = 0;
  for (let t = 1.2; t < 4; t += 0.2) { // stalled at E1 for ~3 s
    r = D.clockStep(r.clock, report(E1, t), t);
    for (let k = 0; k < 12; k++) maxLead = Math.max(maxLead, D.clockAt(r.clock, t + k / 60) - E1);
  }
  assert.ok(!r.clock.playing);
  assert.equal(D.clockAt(r.clock, 9), E1, 'holds on the held picture');
  assert.ok(maxLead < 0.6, `ran ${maxLead.toFixed(2)} s ahead before noticing`);
  for (let t = 4; t < 4.9; t += 0.2) r = D.clockStep(r.clock, report(E1 + (t - 3.9), t), t); // moving again
  assert.ok(r.clock.playing);
  // at 2x, reports 0.2 s apart are 0.4 s further on each: playing, not held
  let f = D.clockStep(null, report(T0, 0, { rate: 2 }), 0);
  for (let t = 0.2; t < 1.5; t += 0.2) f = D.clockStep(f.clock, report(T0 + 2 * t, t, { rate: 2 }), t);
  assert.ok(f.clock.playing);
});

test('clock: with no reports it stops after HOLD instead of running into the future', () => {
  const c = D.clockStep(null, report(T0, 0, { rate: 2 }), 0).clock;
  near(D.clockAt(c, 1), T0 + 2, 1e-9);
  assert.equal(D.clockAt(c, 30), T0 + D.CLOCK.HOLD * 2);
});

test('clock: reports without an E are ignored', () => {
  const c = D.clockStep(null, report(T0, 0), 0).clock;
  assert.equal(D.clockStep(c, report(null, 1), 1).clock, c);
});

test('pickTab: stays with a playing tab, moves to the one that plays, keeps a paused one', () => {
  const tabs = new Map([
    [1, { msg: { playing: true }, rx: 99.9 }],
    [2, { msg: { playing: false }, rx: 99.95 }],
  ]);
  assert.equal(D.pickTab(tabs, null, 100), 1);
  assert.equal(D.pickTab(tabs, 1, 100), 1);
  tabs.set(1, { msg: { playing: false }, rx: 99.9 });
  tabs.set(2, { msg: { playing: true }, rx: 99.95 });
  assert.equal(D.pickTab(tabs, 1, 100), 2);
  tabs.set(2, { msg: { playing: false }, rx: 99.95 });
  assert.equal(D.pickTab(tabs, 2, 100), 2, 'nothing plays: stay');
  assert.equal(D.pickTab(tabs, 2, 200), 2, 'dead and silent: still show where it stopped');
  assert.equal(D.pickTab(new Map(), null, 1), null);
});

// ---- positions -------------------------------------------------------------------------

test('chunks: fixed 30 s windows, the one holding E plus the next', () => {
  assert.equal(D.chunkOf(30), 1);
  assert.equal(D.chunkOf(29.999), 0);
  assert.deepEqual(D.chunksFor(T0), [Math.floor(T0 / 30), Math.floor(T0 / 30) + 1]);
  // live: a chunk is final only once its end is SETTLE seconds in the past
  assert.ok(!D.chunkComplete(10, 330 + D.POS.SETTLE - 1));
  assert.ok(D.chunkComplete(10, 330 + D.POS.SETTLE));
});

test('mergeChunks: one ascending array per car, overlap samples once', () => {
  const a = { k: 1, cars: { 44: [[29.9, 0, 0], [30.2, 1, 1], [59.8, 2, 2]] } };
  const b = { k: 2, cars: { 44: [[59.8, 2, 2], [60.1, 3, 3]], 1: [[60.5, 9, 9]] } };
  const m = D.mergeChunks([b, a]);
  assert.deepEqual(m[44].map((p) => p[0]), [29.9, 30.2, 59.8, 60.1]);
  assert.deepEqual(m[1], [[60.5, 9, 9]]);
});

test('carAt: interpolates between the samples around E, nothing before the first sample', () => {
  const s = [[10, 0, 0], [10.25, 100, -40], [10.5, 200, -80]];
  assert.equal(D.carAt(s, 9.99), null);
  assert.deepEqual(D.carAt(s, 10), [0, 0]);
  const p = D.carAt(s, 10.125);
  near(p[0], 50, 1e-9);
  near(p[1], -20, 1e-9);
  assert.deepEqual(D.carAt(s, 10.5), [200, -80]);
});

test('carAt: holds briefly after the last sample, then drops the car; no line across a dropout', () => {
  const s = [[10, 0, 0], [10.25, 100, 0], [20, 5000, 0]]; // in the garage from 10.25 to 20
  assert.deepEqual(D.carAt(s, 10.25 + D.POS.HOLD - 0.01), [100, 0]);
  assert.equal(D.carAt(s, 12), null);
  assert.deepEqual(D.carAt(s, 20), [5000, 0]);
  assert.equal(D.carAt([], 1), null);
  assert.equal(D.carAt(undefined, 1), null);
});

// ---- session clock ---------------------------------------------------------------------

const st = (t, remaining, status = 'Started') => ({ t, status, clock: { remaining } });

test('countdown: ticks once a second on time between polls that land anywhere', () => {
  // truth: 369.4 s left at T0, running; polls land at uneven moments
  const truth = (t) => 369.4 - (t - T0);
  const show = (t) => D.fmtClock(Math.ceil(truth(t) - 1e-6));
  const polls = Array.from({ length: 40 }, (_, i) => T0 + i * 1.07 + (i % 3) * 0.13);
  let c = null;
  let lastShown = Infinity, bad = 0, frames = 0;
  polls.forEach((t, i) => {
    c = D.countdownStep(c, st(t, show(t)));
    for (let E = t; E < (polls[i + 1] ?? t + 1); E += 1 / 60) { // frames until the next poll
      const v = D.countdownAt(c, E);
      assert.ok(v <= lastShown, 'never goes back up');
      lastShown = v;
      frames++;
      if (v !== Math.ceil(truth(E) - 1e-6)) bad++;
    }
  });
  assert.ok(bad / frames < 0.08, `${bad}/${frames} frames a second off`); // early polls: still a wide interval
});

test('countdown: a stopped clock (red flag) shows the server value and does not run', () => {
  const c = D.countdownStep(null, st(T0, '06:09', 'Aborted'));
  assert.equal(D.countdownAt(c, T0 + 300), 369);
  // the restart: re-anchored, running again
  const c2 = D.countdownStep(c, st(T0 + 400, '06:09'));
  assert.ok(c2.running);
  assert.equal(D.countdownAt(c2, T0 + 402), 367);
  assert.equal(D.countdownStep(null, { t: 1, clock: { remaining: null } }), null);
});

test('parseClock / fmtClock', () => {
  assert.equal(D.parseClock('06:09'), 369);
  assert.equal(D.parseClock('120:00'), 7200);
  near(D.parseClock('1:33.748'), 93.748, 1e-9);
  assert.equal(D.parseClock('x'), null);
  assert.equal(D.fmtClock(369), '06:09');
  assert.equal(D.fmtClock(7200), '2:00:00');
  near(D.lapSeconds('1:33.748'), 93.748, 1e-9);
  assert.equal(D.lapSeconds(null), null);
});

// ---- reading the state -----------------------------------------------------------------

const SQ = { key: 11379, name: 'Sprint Qualifying', type: 'Qualifying', meeting: 'Singapore Grand Prix' };
const row = (pos, extra) => ({ pos, num: String(pos), code: `D${pos}`, gap: null, interval: null, last: null, best: null,
  lastFlag: 'none', knockedOut: false, retired: false, inPit: false, pitOut: false, ...extra });

test('flagChip: the SQ1 red flag, its restart, the parts and the end (2026 Singapore SQ sequence)', () => {
  const timed = [row(1, { best: '1:33.748', last: '1:33.748' }), row(2)];
  const untimed = [row(1), row(2), row(3, { knockedOut: true, best: '1:35.136' })];
  const chip = (status, flag, tower, tstatus) => D.flagChip({ status, track: { flag, status: tstatus }, tower }).text;
  assert.equal(chip('Inactive', 'GREEN', [row(1)]), 'NOT STARTED'); // before the session
  assert.equal(chip('Started', 'GREEN', timed), 'GREEN');
  assert.equal(chip('Started', 'DOUBLE YELLOW', timed), 'DOUBLE YELLOW');
  assert.equal(chip('Aborted', 'RED', timed), 'RED FLAG');
  assert.equal(chip('Aborted', 'GREEN', timed, 'AllClear'), 'RED FLAG', 'TRACK CLEAR during the stop');
  assert.equal(chip('Aborted', 'DOUBLE YELLOW', timed), 'RED FLAG');
  assert.equal(chip('Inactive', 'GREEN', timed), 'RED FLAG', 'waiting for the restart');
  assert.equal(chip('Finished', 'CHEQUERED', timed), 'CHEQUERED');
  assert.equal(chip('Inactive', 'GREEN', untimed), 'NOT STARTED', 'before SQ2: only knocked-out cars have times');
  assert.equal(chip('Ends', 'CHEQUERED', timed), 'CHEQUERED');
  assert.equal(chip('Started', 'VSC', timed, 'VSCEnding'), 'VSC ENDING');
  assert.equal(chip('Started', 'VSC', timed, 'VSCDeployed'), 'VIRTUAL SC');
  assert.equal(chip('Started', 'SC', timed), 'SAFETY CAR');
  assert.equal(D.flagChip({ status: 'Started', track: { flag: 'RED' } }).cls, 'red');
  assert.equal(D.flagChip({}).text, 'GREEN');
});

test('partLabel / sessionKind', () => {
  assert.equal(D.partLabel({ part: 1, session: SQ }), 'SQ1');
  assert.equal(D.partLabel({ part: 3, session: { name: 'Qualifying', type: 'Qualifying' } }), 'Q3');
  assert.equal(D.partLabel({ part: null, session: SQ }), '');
  assert.equal(D.partLabel({ part: 1, session: { name: 'Practice 1', type: 'Practice' } }), '');
  assert.equal(D.sessionKind({ name: 'Sprint', type: 'Race' }), 'race');
  assert.equal(D.sessionKind({ name: 'Sprint Shootout', type: 'Qualifying' }), 'quali');
  assert.equal(D.sessionKind({ name: 'Practice 1', type: 'Practice' }), 'practice');
});

test('towerTitle: the part in qualifying, a short name otherwise', () => {
  assert.equal(D.towerTitle({ part: 2, session: SQ }), 'SQ2');
  assert.equal(D.towerTitle({ part: null, session: SQ }), 'SQ');
  assert.equal(D.towerTitle({ part: 1, session: { name: 'Qualifying', type: 'Qualifying' } }), 'Q1');
  assert.equal(D.towerTitle({ session: { name: 'Practice 2', type: 'Practice' } }), 'FP2');
  assert.equal(D.towerTitle({ session: { name: 'Race', type: 'Race' } }), 'RACE');
  assert.equal(D.towerTitle({ session: { name: 'Sprint', type: 'Race' } }), 'SPRINT');
  assert.equal(D.towerTitle(null), '');
});

test('gap cells: leader, lapped cars, qualifying with no time yet', () => {
  assert.deepEqual(D.gapCells(row(1, { gap: 'LAP 23', interval: 'LAP 23' }), 'race'), { gap: 'LEADER', interval: '' });
  assert.deepEqual(D.gapCells(row(1), 'quali'), { gap: '', interval: '' });
  assert.deepEqual(D.gapCells(row(1, { best: '1:33.748' }), 'quali'), { gap: 'LEADER', interval: '' });
  assert.deepEqual(D.gapCells(row(5, { gap: '+15.594', interval: '+0.312' }), 'quali'), { gap: '+15.594', interval: '+0.312' });
  assert.deepEqual(D.gapCells(row(19, { gap: '1L', interval: '+2 LAPS' }), 'race'), { gap: '+1 LAP', interval: '+2 LAPS' });
});

test('fastestRow skips knocked-out drivers (their time is from an earlier part)', () => {
  const tower = [row(1, { best: '1:33.900' }), row(2, { best: '1:34.100' }), row(11, { best: '1:33.249', knockedOut: true })];
  assert.equal(D.fastestRow(tower), 0);
  assert.equal(D.fastestRow([row(1), row(2)]), -1);
});

test('row marks: retired first, then in pit, then pit out', () => {
  assert.equal(D.rowMark(row(1, { retired: true, inPit: true })).text, 'OUT');
  assert.equal(D.rowMark(row(1, { inPit: true })).text, 'IN PIT');
  assert.equal(D.rowMark(row(1, { pitOut: true })).text, 'PIT OUT');
  assert.equal(D.rowMark(row(1)), null);
  assert.equal(D.tyreLetter('INTERMEDIATE'), 'I');
  assert.equal(D.tyreLetter('UNKNOWN'), '?');
});

test('cutAfter: the drop zone line in Q1/Q2 for 22 and 20 cars, none in Q3 or practice', () => {
  const tower = (k) => Array.from({ length: k }, (_, i) => row(i + 1));
  assert.equal(D.cutAfter({ session: SQ, part: 1, tower: tower(22) }), 16);
  assert.equal(D.cutAfter({ session: SQ, part: 2, tower: tower(22) }), 10);
  assert.equal(D.cutAfter({ session: SQ, part: 1, tower: tower(20) }), 15);
  assert.equal(D.cutAfter({ session: SQ, part: 3, tower: tower(22) }), null);
  assert.equal(D.cutAfter({ session: { name: 'Practice 1' }, part: null, tower: tower(22) }), null);
});

test('compass', () => {
  assert.equal(D.compass(250), 'WSW');
  assert.equal(D.compass(0), 'N');
  assert.equal(D.compass(359), 'N');
  assert.equal(D.compass(-90), 'W');
  assert.equal(D.compass(null), '');
});

// ---- layout ----------------------------------------------------------------------------

const inter = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
const inside = (a, vw, vh) => a.x >= 0 && a.y >= 0 && a.x + a.w <= vw + 1e-9 && a.y + a.h <= vh + 1e-9;
const panels = (L) => [...['tower', 'cond', 'map'].map((k) => L[k]), ...L.slots].filter(Boolean);
const box = (x, y, w, h) => ({ x, y, w, h });
const rectOf = ([w, h]) => ({ x: 0, y: 0, w, h });
const iframe = (vw, vh, rect) => D.layout({ mode: 'iframe', vw, vh, rect, rows: 22 });

// A slot: its header on top, a 16:9 video area (whole 16x9 px steps) right under it to the
// slot's bottom, as wide as the slot.
function slotOk(s, what) {
  const v = s.video;
  assert.ok(v.w % 16 === 0 && v.w / 16 === v.h / 9, `${what}: video ${v.w}x${v.h} is whole 16:9 steps`);
  assert.ok(v.x === s.x && v.w === s.w && v.y === s.y + s.head && v.y + v.h === s.y + s.h, `${what}: video from the header down to the slot's bottom`);
  assert.ok(s.head >= D.SLOT.HEAD.MIN && s.head <= D.SLOT.HEAD.MAX + 8, `${what}: header ${s.head} px`);
}

// The screens we know (layout.js's box: 77% of the width in 16x9 steps, full height on very
// wide ones), with how many onboards each one gets and their size (a track twice as wide as
// tall, the default before /track answers).
const SCREENS = {
  '1920x1080': { player: [1472, 828], slots: [3, 368, 207] },
  '1948x1147': { player: [1504, 846], slots: [2, 448, 252] }, // the user's PC
  '2560x1440': { player: [1968, 1107], slots: [3, 496, 279] },
  '1512x857': { player: [1168, 657], slots: [3, 288, 162] }, // the user's Mac, full screen
  '1248x739': { player: [960, 540], slots: [2, 288, 162] },
  '1366x768': { player: [1056, 594], slots: [3, 240, 135] },
  '2560x1080': { player: [1920, 1080], slots: null }, // full-height player: a column, no onboards
};

test('layout iframe 1920x1080: tower down to the player\'s bottom, compact conditions, three full-height onboards, the map the rest', () => {
  const L = iframe(1920, 1080, rectOf([1472, 828]));
  assert.equal(L.kind, 'L');
  assert.deepEqual(L.tower, box(1481, 9, 430, 819)); // bottom 828: the player's
  assert.deepEqual(L.cond, box(9, 837, 257, 234));
  assert.deepEqual(L.map, { ...box(275, 837, L.map.w, 234) });
  assert.deepEqual(L.slots.map((s) => [s.w, s.h, s.head, s.video.y, s.video.h]), [
    [368, 234, 27, 864, 207], [368, 234, 27, 864, 207], [368, 234, 27, 864, 207],
  ]);
  assert.equal(L.slots[2].x + 368, 1911);
  const m = D.towerMetrics(L.tower.w - 2, L.tower.h - 2, 22);
  assert.ok(m.fs >= 12 && !m.hide.length, `tower font ${m.fs}, all columns`);
  assert.ok(m.fs * (D.TOWER.HEAD + 2 * D.TOWER.PADY) + 22 * m.rowH <= L.tower.h - 2 + 1e-6, 'the rows fit the shorter tower');
});

test('layout iframe at the screens we know: tower on the player\'s bottom edge, full-height onboards, the map the rest', () => {
  for (const [name, x] of Object.entries(SCREENS)) {
    const [vw, vh] = name.split('x').map(Number), rect = rectOf(x.player);
    const L = iframe(vw, vh, rect);
    const g = Math.round(D.clamp(Math.min(vw, vh) * 0.008, 6, 12)); // the gap between panels
    if (!x.slots) {
      assert.equal(L.kind, 'column', name);
      assert.deepEqual(L.slots, [], `${name}: no onboards`);
    } else {
      const [n, w, h] = x.slots;
      assert.equal(L.kind, 'L', name);
      assert.equal(L.tower.y + L.tower.h, rect.y + rect.h, `${name}: tower bottom on the player's`);
      assert.equal(L.tower.x, rect.x + rect.w + g, `${name}: tower beside the player`);
      assert.equal(L.tower.x + L.tower.w, vw - g, `${name}: tower to the right edge`);
      assert.ok(L.cond.x === g && L.cond.y === rect.h + g && L.cond.y + L.cond.h === vh - g, `${name}: conditions at the strip's left`);
      assert.ok(L.cond.w >= D.COND.W.MIN && L.cond.w <= D.COND.W.MAX, `${name}: compact conditions (${L.cond.w})`);
      assert.equal(L.map.x, L.cond.x + L.cond.w + g, `${name}: the map right beside the conditions`);
      assert.equal(L.slots.length, n, `${name}: ${n} onboards`);
      const need = Math.max(D.SLOT.MAP.MIN, L.map.h * (2 + D.SLOT.MAP.PAD));
      assert.ok(L.map.w >= Math.max(D.SLOT.MAP.MIN, need * D.SLOT.MAP.FLEX) - 1e-9, `${name}: the map keeps most of its track's width (${L.map.w})`);
      assert.ok(n === D.OB_SLOTS || L.map.w - w - g < Math.max(D.SLOT.MAP.MIN, need * D.SLOT.MAP.FLEX), `${name}: no room left for one more onboard`);
      assert.equal(L.map.x + L.map.w + g, L.slots[0].x, `${name}: the map up to the onboards`);
      assert.equal(L.slots[n - 1].x + L.slots[n - 1].w, vw - g, `${name}: onboards at the right end`);
      L.slots.forEach((s, i) => {
        slotOk(s, `${name} slot ${i}`);
        assert.deepEqual([s.video.w, s.video.h], [w, h], `${name}: onboard size`);
        assert.ok(s.y === L.cond.y && s.h === L.cond.h, `${name}: as tall as the panels beside it`);
        if (i) assert.equal(s.x - L.slots[i - 1].x - w, g, `${name}: side by side`);
      });
    }
    for (const p of panels(L)) assert.ok(!inter(p, rect) && inside(p, vw, vh), `${name}: ${JSON.stringify(p)}`);
    for (const a of panels(L)) for (const b of panels(L)) assert.ok(a === b || !inter(a, b), `${name}: panels overlap`);
  }
});

test('layout iframe: a compact track leaves room for more onboards than a long one', () => {
  const at = (aspect) => D.layout({ mode: 'iframe', vw: 1920, vh: 1080, rect: rectOf([1472, 828]), rows: 22, mapAspect: aspect });
  const long = at(3.6), compact = at(1.2);
  assert.ok(compact.slots.length > long.slots.length, `${compact.slots.length} vs ${long.slots.length}`);
  for (const L of [long, compact]) L.slots.forEach((s, i) => slotOk(s, `slot ${i}`));
  assert.ok(D.trackAspect([[0, 0], [2100, 0], [2100, 1000], [0, 1000]], 0) === 2.1);
  assert.ok(Math.abs(D.trackAspect([[0, 0], [1000, 0], [1000, 2000], [0, 2000]], 0) - 2) < 1e-9, 'a tall one turned');
});

test('layout iframe: no onboards on short or narrow strips; the map takes their place', () => {
  // 4:3 and smaller windows: the map would get too narrow
  for (const [vw, vh] of [[1024, 768], [1000, 700]]) {
    const r = D.defaultRect(vw, vh), L = iframe(vw, vh, r);
    assert.equal(L.kind, 'L', `${vw}x${vh}`);
    assert.deepEqual(L.slots, [], `${vw}x${vh}`);
    assert.equal(L.map.x + L.map.w, vw - L.cond.x, `${vw}x${vh}: map to the right end`);
  }
  // a strip too short for a 192x108 video below its header
  assert.deepEqual(D.onboardSlots({ x: 6, y: 600, w: 1354, h: 127 }, 200, 6, 2), []);
  assert.ok(D.onboardSlots({ x: 6, y: 600, w: 1354, h: 128 }, 200, 6, 2).length >= 3);
  // wide windows: the player as tall as the window, the strip gone (column), no onboards
  const C = iframe(3440, 1440, rectOf([2560, 1440]));
  assert.equal(C.kind, 'column');
  assert.deepEqual(C.slots, []);
  // the very first layout, before layout.js reports: the default box, slots and all
  assert.equal(iframe(1920, 1080, null).slots.length, 3);
});

test('layout iframe: never under the player, whatever the window', () => {
  for (const [vw, vh] of [[1280, 720], [1366, 768], [1920, 1080], [1920, 1200], [2560, 1440], [2560, 1080], [3440, 1440], [1000, 1400], [1440, 900], [1024, 768]]) {
    for (const share of [0.6, 0.7, 0.77, 0.8]) {
      const w = Math.round(vw * share), rect = { x: 0, y: 0, w, h: Math.min(vh, Math.round((w * 9) / 16)) };
      const L = iframe(vw, vh, rect);
      for (const p of panels(L)) {
        assert.ok(!inter(p, rect), `${vw}x${vh} ${share}: ${JSON.stringify(p)}`);
        assert.ok(inside(p, vw, vh) && p.w > 0 && p.h > 0, `${vw}x${vh} ${share}: ${JSON.stringify(p)}`);
      }
      for (const a of panels(L)) for (const b of panels(L)) assert.ok(a === b || !inter(a, b), `${vw}x${vh}: panels overlap`);
      L.slots.forEach((s, i) => slotOk(s, `${vw}x${vh} ${share} slot ${i}`));
      if (L.kind === 'L') assert.equal(L.tower.y + L.tower.h, rect.h, `${vw}x${vh} ${share}: tower bottom`);
    }
  }
});

test('layout iframe 2560x1080 (player as tall as the window): everything stacks in the right column', () => {
  const rect = { x: 0, y: 0, w: 1792, h: 1008 };
  const L = D.layout({ mode: 'iframe', vw: 2560, vh: 1080, rect, rows: 22 });
  assert.equal(L.kind, 'column');
  assert.ok(L.cond.y < L.tower.y && L.tower.y < L.map.y);
  for (const p of panels(L)) assert.ok(!inter(p, rect) && inside(p, 2560, 1080), JSON.stringify(p));
  const m = D.towerMetrics(L.tower.w, L.tower.h, 22);
  assert.ok(m.fs >= 14, `tower font ${m.fs}`);
  assert.ok(L.map.h >= 1080 * 0.25);
  assert.ok(L.cond.h <= D.COND.BAR.MAX, `a short conditions bar (${L.cond.h})`);
});

test('layout iframe: a fullscreen player leaves no room; a portrait window uses the strip', () => {
  const F = D.layout({ mode: 'iframe', vw: 1920, vh: 1080, rect: { x: 0, y: 0, w: 1920, h: 1080 } });
  assert.equal(F.kind, 'none');
  assert.deepEqual(F.slots, []);
  const L = D.layout({ mode: 'iframe', vw: 1000, vh: 1600, rect: { x: 0, y: 0, w: 1000, h: 563 }, rows: 22 });
  assert.equal(L.kind, 'strip');
  assert.ok(L.cond.y > 563);
  assert.deepEqual(L.slots, []);
  // before the layout script reports: the contract's default box
  assert.deepEqual(D.defaultRect(1920, 1080), { x: 0, y: 0, w: 1472, h: 828 });
  assert.deepEqual(D.defaultRect(2560, 1080), { x: 0, y: 0, w: 1920, h: 1080 });
});

test('layout standalone: side by side when wide, one column when narrow; a compact conditions bar, no onboards', () => {
  const L = D.layout({ mode: 'standalone', vw: 1280, vh: 900, rows: 22 });
  assert.equal(L.kind, 'grid');
  assert.ok(L.tower.x < L.cond.x && L.cond.y < L.map.y);
  for (const p of panels(L)) assert.ok(inside(p, 1280, 900));
  assert.deepEqual([L.cond.h, L.map.h], [115, 764]); // was 177 and 702
  assert.deepEqual(L.slots, []);
  const S = D.layout({ mode: 'standalone', vw: 600, vh: 1000, rows: 22 });
  assert.equal(S.kind, 'stack');
  for (const p of panels(S)) assert.ok(inside(p, 600, 1000) && p.h > 0);
  assert.equal(S.cond.h, 99); // was 128
  assert.deepEqual(S.slots, []);
});

// ---- onboards --------------------------------------------------------------------------

// as the fone server lists them (/api/f1tv/{cid} streams)
const STREAMS = [
  { channelId: 1003, type: 'additional', title: 'F1 LIVE', number: null },
  { channelId: 1016, type: 'obc', title: 'NOR', number: 1, name: 'Lando Norris', team: 'McLaren', color: '#FF8000' },
  { channelId: 1011, type: 'obc', title: 'HAM', number: 44, name: 'Lewis Hamilton', team: 'Ferrari', color: '#E8002D' },
  { channelId: 1020, type: 'obc', title: 'VER', number: 3, name: 'Max Verstappen', team: 'Red Bull Racing', color: '#3671C6' },
  { channelId: 1004, type: 'additional', title: 'TRACKER', number: null },
  { channelId: 1030, type: 'obc', title: 'XYZ', number: 99, name: 'Someone New', color: '#123456' },
  { channelId: 1031, type: 'obc', title: 'NOR', number: 1 }, // listed twice: the first counts
];
const TOWER_ROWS = [
  { pos: 1, num: '44', code: 'HAM', color: '#ED1131', name: 'Lewis Hamilton' },
  { pos: 2, num: '3', code: 'VER', color: '#4781D7', name: 'Max Verstappen' },
  { pos: 3, num: '16', code: 'LEC', color: '#ED1131' }, // no onboard in this video
  { pos: 4, num: '1', code: 'NOR', color: '#F47600', name: 'Lando Norris' },
];

test('onboardList: driver onboards only, in the tower\'s order with its codes and colours, the rest by number', () => {
  const L = D.onboardList(STREAMS, TOWER_ROWS);
  assert.deepEqual(L.map((e) => [e.code, e.num, e.channelId]), [['HAM', 44, 1011], ['VER', 3, 1020], ['NOR', 1, 1016], ['XYZ', 99, 1030]]);
  assert.equal(L[0].color, '#ED1131', 'the tower\'s colour');
  assert.equal(L[3].color, '#123456', 'not on the tower: F1 TV\'s');
  // no tower yet: by number
  assert.deepEqual(D.onboardList(STREAMS, null).map((e) => e.num), [1, 3, 44, 99]);
  assert.equal(D.onboardList(undefined, TOWER_ROWS), null, 'a server without the list');
  assert.deepEqual(D.onboardList([], TOWER_ROWS), []);
  assert.deepEqual(D.onboardList([{ type: 'obc', title: 'BAD', number: 'x', channelId: 1 }, { type: 'obc', number: 5 }], null), []);
  // channel ids go to the page as whole numbers, whatever JSON brought them as
  assert.deepEqual(D.onboardList([{ type: 'obc', number: '4', channelId: '1016' }, { type: 'obc', number: 6, channelId: 'x' }], null)
    .map((e) => [e.num, e.channelId]), [[4, 1016]]);
});

test('pickOnboard: set, clear, and a driver from the other slot swaps the two', () => {
  assert.deepEqual(D.pickOnboard([null, null], 0, 1), [1, null]);
  assert.deepEqual(D.pickOnboard([1, 44], 1, 3), [1, 3]);
  assert.deepEqual(D.pickOnboard([1, 44], 0, null), [null, 44]);
  assert.deepEqual(D.pickOnboard([1, 44], 0, 44), [44, 1], 'swapped');
  assert.deepEqual(D.pickOnboard([null, 44], 0, 44), [44, null], 'moved');
  assert.deepEqual(D.pickOnboard([1, null], 1, null), [1, null]);
  // what storage hands back, whatever it holds
  assert.deepEqual(D.onboardPicks([1, 44]), [1, 44, null, null]); // two before 0.2.17
  assert.deepEqual(D.onboardPicks([null, 81, 4, 16]), [null, 81, 4, 16]);
  assert.deepEqual(D.onboardPicks(undefined), [null, null, null, null]);
  assert.deepEqual(D.onboardPicks(['1', -2, 3]), [null, null, 3, null]);
  assert.deepEqual(D.onboardPicks({ 0: 1 }), [null, null, null, null]);
});

// ---- track map -------------------------------------------------------------------------

const OUTLINE = [[925, -2265], [-13225, -4906], [-12000, 4089], [1170, 3000], [500, 0]];

test('fitTrack: the turned outline fits the box with its padding', () => {
  for (const [w, h] of [[760, 300], [300, 760], [500, 500]]) {
    const f = D.fitTrack(OUTLINE, -14, w, h, 20);
    for (const [x, y] of OUTLINE) {
      const [px, py] = D.project(f, x, y);
      assert.ok(px >= 20 - 1e-6 && px <= w - 20 + 1e-6 && py >= 20 - 1e-6 && py <= h - 20 + 1e-6, `${w}x${h}: ${px},${py}`);
    }
  }
  // a tall box gets the quarter turn (the long side vertical): a bigger scale
  const wide = D.fitTrack(OUTLINE, -14, 760, 300, 20), tall = D.fitTrack(OUTLINE, -14, 300, 760, 20);
  near(wide.k, tall.k, wide.k * 0.05, 'same scale either way round');
  assert.equal(D.fitTrack(OUTLINE, 0, 10, 10, 20), null);
});

test('project: y up in F1 units is up on screen', () => {
  const f = D.fitTrack([[0, 0], [100, 0], [100, 50], [0, 50]], 0, 220, 120, 10);
  const [, yLow] = D.project(f, 50, 0), [, yHigh] = D.project(f, 50, 50);
  assert.ok(yHigh < yLow);
});

test('placeLabels: no overlaps when there is room, sides stay put across frames', () => {
  const dots = [{ id: 'a', x: 100, y: 100, w: 30, h: 12 }, { id: 'b', x: 135, y: 100, w: 30, h: 12 }, { id: 'c', x: 100, y: 125, w: 30, h: 12 }];
  const out = D.placeLabels(dots, 5, { w: 400, h: 300 }, null);
  const boxes = dots.map((d) => ({ ...out.get(d.id), w: d.w, h: d.h }));
  for (const a of boxes) for (const b of boxes) assert.ok(a === b || !inter(a, b));
  for (const b of boxes) for (const d of dots) assert.ok(!inter(b, { x: d.x - 5, y: d.y - 5, w: 10, h: 10 }));
  assert.ok(boxes.every((b) => !b.crowded));
  // next frame, cars moved a little: same sides
  const moved = dots.map((d) => ({ ...d, x: d.x + 1 }));
  const prev = new Map([...out].map(([id, v]) => [id, v.side]));
  const again = D.placeLabels(moved, 5, { w: 400, h: 300 }, prev);
  for (const d of dots) assert.equal(again.get(d.id).side, out.get(d.id).side);
});

test('placeLabels: crowded cars still get a (faint) label, crowded optional ones none', () => {
  // a queue in the pit lane: 16 cars 3 px apart, the first two still racing
  const dots = Array.from({ length: 16 }, (_, i) => ({ id: String(i), x: 100, y: 100 + 3 * i, w: 30, h: 12, optional: i >= 2 }));
  const out = D.placeLabels(dots, 5, { w: 400, h: 300 }, null);
  assert.ok(out.has('0') && out.has('1'));
  for (const [id, l] of out) assert.ok(!l.crowded || !dots[Number(id)].optional, `optional ${id} placed crowded`);
  assert.ok(out.size < dots.length, `some optional labels dropped (${out.size} placed)`);
});

test('placeLabels: a crowded label stays inside the map, where it overlaps least', () => {
  // a car at the right edge, boxed in above, below and to the left
  const dots = [
    { id: 'n', x: 380, y: 135, w: 30, h: 12 }, { id: 's', x: 380, y: 165, w: 30, h: 12 },
    { id: 'w', x: 350, y: 150, w: 30, h: 12 }, { id: 'x', x: 392, y: 150, w: 30, h: 12 },
  ];
  const out = D.placeLabels(dots, 5, { w: 400, h: 300 }, null);
  const l = out.get('x');
  assert.ok(l.x >= 0 && l.x + 30 <= 400, `inside: ${JSON.stringify(l)}`);
});

test('placeLabels: a label that would leave the map goes on another side', () => {
  const out = D.placeLabels([{ id: 'a', x: 395, y: 150, w: 30, h: 12 }], 5, { w: 400, h: 300 }, null);
  assert.ok(out.get('a').x + 30 <= 400 && !out.get('a').crowded);
});

test('cornerSpot: the number sits gap px off the track, in the data\'s direction, turned with the map', () => {
  const square = [[0, 0], [1000, 0], [1000, 1000], [0, 1000]];
  const f = D.fitTrack(square, 0, 200, 200, 0); // y up in the data, down on screen
  const [tx, ty] = D.project(f, 1000, 500);
  const near = (p, q) => Math.abs(p[0] - q[0]) < 1e-6 && Math.abs(p[1] - q[1]) < 1e-6;
  assert.ok(near(D.cornerSpot(f, { x: 1000, y: 500, a: 0 }, 12), [tx + 12, ty])); // east
  assert.ok(near(D.cornerSpot(f, { x: 1000, y: 500, a: 90 }, 12), [tx, ty - 12])); // north: up on screen
  const r = D.fitTrack(square, 90, 200, 200, 0); // the map turned a quarter: east points up
  const [rx, ry] = D.project(r, 1000, 500);
  assert.ok(near(D.cornerSpot(r, { x: 1000, y: 500, a: 0 }, 12), [rx, ry - 12]));
  const far = D.fitTrack(square, 0, 2000, 2000, 0); // any scale: still gap px
  const [fx, fy] = D.project(far, 1000, 500), [sx, sy] = D.cornerSpot(far, { x: 1000, y: 500, a: 30 }, 12);
  assert.ok(Math.abs(Math.hypot(sx - fx, sy - fy) - 12) < 1e-6);
});

test('towerColumns: a narrow tower drops the pit count, then the interval, to keep the text readable', () => {
  const at = (w) => D.towerColumns(w);
  assert.deepEqual(at(560).hide, []); // a 1920 px screen's column: everything
  const mac = at(274); // a 1248 px window at 77%
  assert.ok(mac.hide.includes('pits') && 274 / mac.em >= 9.5);
  assert.ok(at(200).hide.length === 2 && at(200).cols.every(([k]) => k !== 'int' && k !== 'pits'));
  for (const w of [200, 274, 330, 430, 560, 900]) { // never drops lap times, tyres, gaps or the marks
    const keys = at(w).cols.map(([k]) => k);
    for (const k of ['pos', 'drv', 'gap', 'last', 'best', 'tyre', 'mark']) assert.ok(keys.includes(k), `${w}: ${k}`);
  }
  const m = D.towerMetrics(274, 700, 22);
  assert.ok(m.fs > 274 / D.TOWER.EM && m.cols.length < D.TOWER.COLS.length);
});

test('onboardBox: a short wide window shrinks the picture until onboards fit; a roomy one keeps 77%', () => {
  const at = (vw, vh, a = 2.1) => ({ box: D.onboardBox({ vw, vh, rows: 22, mapAspect: a }), def: D.defaultRect(vw, vh) });
  for (const [vw, vh] of [[2560, 1270], [1948, 993], [1920, 950]]) {
    const { box, def } = at(vw, vh);
    assert.equal(D.layout({ mode: 'iframe', vw, vh, rect: def, rows: 22, mapAspect: 2.1 }).slots.length, 0, `${vw}x${vh}: none at 77%`);
    assert.ok(box && box.w < def.w && box.w >= vw * 0.5 && box.w / 16 === box.h / 9, `${vw}x${vh}: ${JSON.stringify(box)}`);
    const L = D.layout({ mode: 'iframe', vw, vh, rect: box, rows: 22, mapAspect: 2.1 });
    assert.ok(L.kind === 'L' && L.slots.length >= 2, `${vw}x${vh}: ${L.slots.length} onboards`);
    L.slots.forEach((s, i) => slotOk(s, `${vw}x${vh} slot ${i}`));
    // the largest such: one step bigger has fewer than two, or a map far wider than its track
    const up = { x: 0, y: 0, w: box.w + 16, h: box.h + 9 };
    const U = D.layout({ mode: 'iframe', vw, vh, rect: up, rows: 22, mapAspect: 2.1 });
    const need = Math.max(D.SLOT.MAP.MIN, (U.map?.h || 0) * (2.1 + D.SLOT.MAP.PAD));
    assert.ok(U.kind !== 'L' || U.slots.length < 2 || U.map.w > need * 1.3, `${vw}x${vh}: the largest box`);
  }
  for (const [vw, vh] of [[1948, 1147], [1920, 1080], [1512, 857]]) {
    const { box, def } = at(vw, vh);
    assert.deepEqual(box, def, `${vw}x${vh}: onboards already fit at 77%`);
  }
  assert.equal(D.onboardBox({ vw: 900, vh: 500, rows: 22, mapAspect: 2 }), null, 'too small for any');
});

console.log(`${n} passed`);
