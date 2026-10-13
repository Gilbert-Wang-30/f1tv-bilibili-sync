// node extension/dmcore.test.js
require('./dmcore.js');
const assert = require('node:assert/strict');
const D = globalThis.FoneDmCore;
let n = 0;
const test = (name, fn) => { fn(); n++; console.log('ok -', name); };

test('normalize: defaults for anything missing or out of range', () => {
  assert.deepEqual(D.normalize(undefined), D.normalize({}));
  const s = D.normalize({ opacity: 5, area: 0.3, font: 0.1, speed: 9, stroke: 'neon', minUL: 99.6, keywords: ['  a ', '', 3, 'b'],
    block: { color: true, nope: true } });
  assert.equal(s.opacity, 1);
  assert.equal(s.area, D.DEFAULTS.area);
  assert.equal(s.font, 0.5);
  assert.equal(s.speed, 2);
  assert.equal(s.stroke, 'heavy');
  assert.equal(s.minUL, 60);
  assert.deepEqual(s.keywords, ['a', 'b']);
  assert.deepEqual(s.block, { ...D.DEFAULTS.block, color: true });
});

test('filter: types, colour, emoticons, super chats', () => {
  const plain = { t: 1, x: 'hi' }, top = { t: 1, x: 'top', m: 5 }, bottom = { t: 1, x: 'b', m: 4 };
  const colored = { t: 1, x: 'c', c: 0x00fffc }, white = { t: 1, x: 'w', c: 0xffffff };
  const emo = { t: 1, x: '老汉', e: { u: 'http://x/y.png', w: 162, h: 162 } }, inline = { t: 1, x: 'a[dog]', em: { '[dog]': 'u' } };
  const sc = { t: 1, x: 'sc', k: 'sc', p: 30 };
  const all = [plain, top, bottom, colored, white, emo, inline, sc];
  assert.deepEqual(all.filter(D.filter({})), all);
  assert.deepEqual(all.filter(D.filter({ block: { scroll: true } })), [top, bottom, sc]);
  assert.deepEqual(all.filter(D.filter({ block: { top: true, bottom: true } })), [plain, colored, white, emo, inline, sc]);
  assert.deepEqual(all.filter(D.filter({ block: { color: true } })), [plain, top, bottom, white, emo, inline, sc]);
  assert.deepEqual(all.filter(D.filter({ block: { emoticon: true } })), [plain, top, bottom, colored, white, inline, sc]); // inline emoji stay
  assert.deepEqual(all.filter(D.filter({ block: { sc: true } })), all.slice(0, -1));
});

test('filter: levels block below the threshold; a level we never saw (archive) never blocks', () => {
  const lo = { t: 1, x: 'a', ul: 3, wl: 2, ml: 0 }, hi = { t: 1, x: 'b', ul: 20, wl: 15, ml: 12 }, archive = { t: 1, x: 'c' };
  assert.deepEqual([lo, hi, archive].filter(D.filter({ minUL: 10 })), [hi, archive]);
  assert.deepEqual([lo, hi, archive].filter(D.filter({ minWealth: 5 })), [hi, archive]);
  assert.deepEqual([lo, hi, archive].filter(D.filter({ minMedal: 1 })), [hi, archive]); // no medal of this room = 0
});

test('filter: keywords, plain (any case) or /regex/; a broken regex blocks nothing', () => {
  const items = ['Verstappen 好快', '剧透：最后一圈', 'rus就当锻炼身体了', 'ok'].map((x) => ({ t: 1, x }));
  assert.deepEqual(items.filter(D.filter({ keywords: ['verstappen'] })).map((i) => i.x), items.slice(1).map((i) => i.x));
  assert.deepEqual(items.filter(D.filter({ keywords: ['/^剧透/', '/[a-z]+就/'] })).map((i) => i.x), ['Verstappen 好快', 'ok']);
  assert.equal(items.filter(D.filter({ keywords: ['/(/'] })).length, 4);
});

test('merge: sorted by when they show, each once', () => {
  const items = [], seen = new Set();
  assert.equal(D.merge(items, seen, [{ t: 5, x: 'b', u: '1' }, { t: 2, x: 'a' }, { t: 9, x: 'c' }]), 3);
  assert.equal(D.merge(items, seen, [{ t: 5, x: 'b', u: '1' }, { t: 5, x: 'b', u: '2' }, { t: 1, x: 'z' }, { x: 'no time' }]), 2);
  assert.deepEqual(items.map((i) => i.x), ['z', 'a', 'b', 'b', 'c']);
  assert.equal(D.lowerBound(items, 5 - D.LEAD), 2);
  assert.equal(D.lowerBound(items, 100), items.length);
});

test('clockAt: runs at the commentary rate while it plays, holds while paused', () => {
  const c = { w: 1000, at: 50, rate: 1.5, run: true };
  assert.equal(D.clockAt(c, 52), 1003);
  assert.equal(D.clockAt({ ...c, run: false }, 52), 1000);
  assert.equal(D.clockAt(null, 1), null);
  assert.equal(D.clockAt({ w: null }, 1), null);
});

test('lanes: a scrolling danmaku waits until the one ahead is fully in, and never catches it up', () => {
  const W = 1000, dur = 8;
  // 100 px wide: v = 1100/8 = 137.5 px/s, fully in after 100+GAP px = 0.84 s
  const prev = { t0: 0, w: 100, dur };
  assert.equal(D.scrollFree(prev, 0.5, W, 100, dur), false);
  assert.equal(D.scrollFree(prev, 1, W, 100, dur), true);
  // a much longer one is faster: it may only start once it can't reach the short one's tail
  assert.equal(D.scrollFree(prev, 1, W, 900, dur), false);
  assert.equal(D.scrollFree(prev, 4.1, W, 900, dur), true);
  const L = D.lanes(3);
  assert.deepEqual([0, 0, 0, 0].map(() => D.place(L, 'scroll', 0, W, 100, dur, 1, false)), [0, 1, 2, -1]); // full: dropped
  assert.equal(D.place(L, 'scroll', 1, W, 100, dur, 1, false), 0); // the top lane frees first
  const O = D.lanes(2);
  D.place(O, 'scroll', 0, W, 100, dur, 1, false);
  D.place(O, 'scroll', 0.2, W, 100, dur, 1, false);
  assert.equal(D.place(O, 'scroll', 0.3, W, 100, dur, 1, true), 0); // overlap: the lane that frees first
});

test('lanes: top and bottom danmaku stack until they expire; tall ones take two lanes', () => {
  const L = D.lanes(3);
  assert.equal(D.place(L, 'top', 0, 1000, 50, D.FIXED, 1, false), 0);
  assert.equal(D.place(L, 'top', 1, 1000, 50, D.FIXED, 1, false), 1);
  assert.equal(D.place(L, 'top', D.FIXED + 0.1, 1000, 50, D.FIXED, 1, false), 0);
  assert.equal(D.place(L, 'bottom', 0, 1000, 50, D.FIXED, 1, false), 0); // bottom lanes are their own
  assert.equal(D.place(L, 'scroll', 0, 1000, 160, 8, 2, false), 0);
  assert.equal(D.place(L, 'scroll', 0, 1000, 160, 8, 2, false), -1); // one lane left: no room for two
});

console.log(`${n} passed`);
