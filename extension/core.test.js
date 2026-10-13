// node extension/core.test.js
require('./core.js');
const assert = require('node:assert/strict');
const C = globalThis.FoneCore;
let n = 0;
const test = (name, fn) => { fn(); n++; console.log('ok -', name); };

const replay = { p0: 1791014093.132, anchor: 'multiviewer', subtype: 'REPLAY', scheduledStart: 1791014400 };
const now = 1791100000;

test('replay uses the exact anchor', () => {
  assert.equal(C.chooseClock(replay, { p: 10, live: false, at: now }, now), 'multiviewer');
  assert.equal(C.eventTime('multiviewer', replay, 307.04, null, now), 1791014093.132 + 307.04);
});

test('a replay stays a replay even if the player briefly reports duration=Infinity', () => {
  assert.equal(C.chooseClock(replay, { p: 10, live: true, seekEnd: 30, at: now }, now), 'multiviewer');
});

test('a UTC position wins even on a replay (pseudo-VOD with epoch clock)', () => {
  assert.equal(C.chooseClock(replay, { p: 1.79e9, live: false, at: now }, now), 'epoch');
});

test('unanchored replay is manual', () => {
  assert.equal(C.chooseClock({ subtype: 'REPLAY', p0: null }, { p: 10, at: now }, now), 'manual');
});

test('live on the replay timeline is detected from the live edge', () => {
  const live = { p0: now - 4000, anchor: 'preroll', subtype: 'LIVE' };
  // edge at 3970 s into the stream => edge shows now-30 s: plausible
  assert.equal(C.chooseClock(live, { p: 3900, seekEnd: 3970, live: true, at: now }, now), 'edge');
  // player rebased its timeline to 0 at join: falls back to edge-relative
  assert.equal(C.chooseClock(live, { p: 50, seekEnd: 60, live: true, at: now }, now), 'edge');
  // a live player's wall clock is packaging time, not event time: still edge-relative
  assert.equal(C.chooseClock(live, { p: 1.79e9, seekEnd: 1.79e9 + 20, live: true, at: now }, now), 'edge');
});

test('a live item the player plays as a replay (no edge, not live) goes on the anchored timeline', () => {
  const live = { p0: now - 4000, anchor: 'preroll', subtype: 'LIVE' };
  assert.equal(C.chooseClock(live, { p: 3900, seekEnd: null, live: false, at: now }, now), 'preroll');
});

test('a player playing live but reporting no edge is still live: edge clock, taken to be at its edge', () => {
  // 2026-10-10 Singapore qualifying: the player went on reporting a live position (659 s into
  // the stream) without its edge; the replay's pre-roll anchor put that 9 minutes in the future
  const live = { p0: now - 4000 + 307, anchor: 'preroll', subtype: 'LIVE' };
  const v = { p: 659, seekEnd: null, live: true, at: now };
  assert.equal(C.chooseClock(live, v, now), 'edge');
  assert.equal(C.chooseClock({ subtype: 'REPLAY', p0: now - 4000, anchor: 'preroll' }, v, now), 'preroll');
  const vn = C.videoNow(v, now + 2);
  assert.equal(vn.seekEnd, null);
  assert.equal(C.eventTime('edge', live, vn.p, vn.seekEnd, now + 2), now + 2 - C.DEFAULT_LIVE_LATENCY);
});

test('edge clock: E is fixed while paused, advances in real time while playing', () => {
  const v = { p: 100, seekEnd: 160, live: true, paused: true, rate: 1, at: now };
  const a = C.videoNow(v, now), b = C.videoNow(v, now + 10);
  const ea = C.eventTime('edge', {}, a.p, a.seekEnd, now);
  const eb = C.eventTime('edge', {}, b.p, b.seekEnd, now + 10);
  assert.ok(Math.abs(ea - eb) < 1e-9);
  const playing = { ...v, paused: false };
  const c = C.videoNow(playing, now + 10);
  assert.ok(Math.abs(C.eventTime('edge', {}, c.p, c.seekEnd, now + 10) - (ea + 10)) < 1e-9);
});

test('mapWall: inside, before, gap, after', () => {
  const pieces = [{ w: 1000, m: 0, d: 100, f: 0 }, { w: 1200, m: 100, d: 50, f: 1 }];
  assert.deepEqual(C.mapWall(pieces, 1050), { state: 'ok', m: 50, piece: 0, file: 0 });
  assert.deepEqual(C.mapWall(pieces, 1210), { state: 'ok', m: 110, piece: 1, file: 1 });
  assert.deepEqual(C.mapWall(pieces, 990), { state: 'before', wait: 10 });
  assert.deepEqual(C.mapWall(pieces, 1150), { state: 'gap', wait: 50 });
  assert.deepEqual(C.mapWall(pieces, 1260), { state: 'after', past: 10 });
  assert.deepEqual(C.mapWall([], 5), { state: 'none' });
});

test('wallOf: inverse of mapWall inside pieces, null outside', () => {
  const pieces = [{ w: 1000, m: 0, d: 100, f: 0 }, { w: 1200, m: 100, d: 50, f: 1 }];
  assert.equal(C.wallOf(pieces, 50), 1050);
  assert.equal(C.wallOf(pieces, 110), 1210);
  assert.equal(C.wallOf(pieces, 150), null);
  assert.equal(C.wallOf([], 5), null);
  for (const W of [1000, 1099.5, 1200, 1249]) assert.equal(C.wallOf(pieces, C.mapWall(pieces, W).m), W);
});

test('nominalRate: the player\'s own sync trims are 1x, real speeds pass', () => {
  assert.equal(C.nominalRate(1.002), 1);
  assert.equal(C.nominalRate(0.985), 1);
  assert.equal(C.nominalRate(1.5), 1.5);
  assert.equal(C.nominalRate(0.95), 0.95);
  assert.equal(C.nominalRate(undefined), 1);
  assert.equal(C.nominalRate(0), 1);
});

// <video>.playbackRate on the real F1 TV page at 1x (v0.2.6): steady play, the seconds after
// a seek, right after a content load. The SDK's MSE handle said 1 throughout.
const TRIMS = [1.0302, 0.932, 0.75, 1.18, 1.046, 0.945, 0.8513, 1.25];

test('chosenRate: the SDK\'s own speed wins however far F1 TV trims the element', () => {
  // v0.2.6: the SDK's speed only while the element was within 3% of it, else the element's
  const v026 = (sdk, el) => (Math.abs(el / sdk - 1) < 0.03 ? sdk : C.SPEEDS.find((s) => Math.abs(el / s - 1) < 0.03) ?? el);
  assert.deepEqual(TRIMS.map((el) => v026(1, el)), TRIMS); // every one a pitch-kept "speed"
  for (const el of TRIMS) {
    assert.equal(C.chosenRate({ sdk: 1, exact: true, want: 1, el }, 1), 1, `x${el} at 1x`);
    assert.equal(C.chosenRate({ sdk: 2, exact: true, want: 2, el: 2 * el }, 2), 2, `x${el} at 2x`);
  }
  // a real speed change sets the SDK's speed: followed at once
  assert.equal(C.chosenRate({ sdk: 1.5, exact: true, want: 1.5, el: 1.5 * 0.932 }, 1), 1.5);
  // a new MSE handle, 2x not re-applied yet: ridden out for a moment (page.js asks again), then followed
  assert.equal(C.chosenRate({ sdk: 1, exact: true, want: 2, el: 1.25 }, 2), 2);
  assert.equal(C.chosenRate({ sdk: 1, exact: true, want: 2, el: 1.25, dipFor: 3 }, 2), 1);
  assert.equal(C.chosenRate({ sdk: 1.98, exact: true, want: 2, el: 1.98 }, 1), 2); // within 0.02 of the asked speed: that speed
  assert.equal(C.chosenRate({ sdk: 1.75, exact: true, want: 2, el: 1.75 }, 1), 1.75); // a speed set by something else
  // right after a seek the handle is gone for a moment: the element reads 1, the speed holds
  assert.equal(C.chosenRate({ tiled: true, exact: false, sdk: null, want: 1.5, el: 1, dipFor: 0.4 }, 1.5), 1.5);
  assert.equal(C.chosenRate({ tiled: true, exact: false, sdk: null, want: 1.5, el: 1, dipFor: 3 }, 1.5), 1);
});

test('chosenRate: live is 1x unless the SDK\'s own speed says otherwise', () => {
  assert.equal(C.chosenRate({ live: true, el: 1.25 }, null), 1);
  assert.equal(C.chosenRate({ live: true, sdk: 2, exact: false, want: 1, el: 2 }, 1), 1);
  assert.equal(C.chosenRate({ live: true, sdk: 1, exact: true, want: 1, el: 0.75 }, 1), 1);
});

test('chosenRate: no MSE handle: the speed page.js steers to, while the element is within the trim\'s reach', () => {
  for (const el of TRIMS) {
    assert.equal(C.chosenRate({ tiled: true, sdk: el, exact: false, want: 1, el }, 1), 1, `x${el} at 1x`);
    assert.equal(C.chosenRate({ tiled: true, sdk: 2 * el, exact: false, want: 2, el: 2 * el }, 2), 2, `x${el} at 2x`);
  }
  // asked for, but not applied yet (or refused): the video still plays at 1x
  assert.equal(C.chosenRate({ tiled: true, sdk: 1.0302, exact: false, want: 2, el: 1.0302 }, 1), 1);
  assert.equal(C.chosenRate({ tiled: true, sdk: 0.97, exact: false, want: 1.5, el: 0.97 }, 1), 1);
});

test('chosenRate: Tiledmedia, page.js silent: the last speed holds through trims, past them the nearest real speed', () => {
  for (const el of TRIMS) {
    assert.equal(C.chosenRate({ tiled: true, el }, 1), 1, `x${el} at 1x`);
    assert.equal(C.chosenRate({ tiled: true, el: 2 * el }, 2), 2, `x${el} at 2x`);
    assert.equal(C.chosenRate({ tiled: true, el }, null), 1, `x${el} at first`);
  }
  assert.equal(C.chosenRate({ tiled: true, el: 2 }, null), 2);
  assert.equal(C.chosenRate({ tiled: true, el: 1.47 }, 1), 1.5);
  assert.equal(C.chosenRate({ tiled: true, el: 1 }, 2), 1);
  assert.equal(C.chosenRate({ tiled: true, el: NaN }, 1.5), 1.5);
  // never a trim: always a speed a person can pick
  for (let el = 0.3; el < 3; el += 0.01) {
    for (const prev of [null, 1, 1.5, 2]) {
      assert.ok(C.SPEEDS.includes(C.chosenRate({ tiled: true, el }, prev)), `x${el.toFixed(2)} after ${prev}`);
    }
  }
});

test('chosenRate: a plain <video> (no PID) follows the speed someone set, however close to the last one', () => {
  // F1 TV's legacy Bitmovin player under a speed extension: nothing trims it on a replay, so the
  // trim band above would only swallow real changes (1x -> 1.25x is 25%, 1.75x -> 2x 14%)
  const changes = [[1, 1.25], [1, 0.75], [2, 1.5], [1.5, 1.25], [1.5, 1.75], [1.75, 2], [2, 1.75], [1, 1.1], [1.5, 1.7],
    [1.75, 1.8], [2, 1.95]];
  for (const [prev, el] of changes) assert.equal(C.chosenRate({ tiled: false, el }, prev), el, `${prev}x -> ${el}x`);
  // a hair off a real speed is that speed
  assert.equal(C.chosenRate({ tiled: false, el: 1.003 }, 1), 1);
  assert.equal(C.chosenRate({ tiled: false, el: 1.49 }, 1), 1.5);
  assert.equal(C.chosenRate({ tiled: false, el: 1.98 }, null), 2);
  assert.equal(C.chosenRate({ tiled: false, el: NaN }, 1.25), 1.25);
  assert.equal(C.chosenRate({ tiled: false, el: 1.25, live: true }, 1), 1); // live is 1x
});

test('isCoarse: only offset-less and label positions', () => {
  for (const s of ['tm', 'tm+utc', 'label', 'label (player clock disagreed)']) assert.ok(C.isCoarse(s), s);
  for (const s of ['tm+mse', 'tm+mse+utc', 'video', undefined, null]) assert.ok(!C.isCoarse(s), String(s));
});

test('isPrecise: only the element clock itself (MSE handle or plain <video>), never a coarse one', () => {
  for (const s of ['tm+mse', 'tm+mse+utc', 'video']) assert.ok(C.isPrecise(s), s);
  for (const s of ['tm', 'tm+utc', 'label', 'label (player clock disagreed)', undefined, null]) assert.ok(!C.isPrecise(s), String(s));
  for (const s of ['tm', 'tm+utc', 'tm+mse', 'tm+mse+utc', 'video', 'label']) assert.ok(!(C.isCoarse(s) && C.isPrecise(s)), s);
});

// Closed-loop harness: an <audio> that plays at whatever rate it is given and takes
// `seekLatency` s to land a seek, steered every 100 ms toward a measured target.
// `rate` is the nominal speed steer is given; `video` (optional) the true position, for maxErr
// (counted from t >= errFrom); `precise` the x.precise flag (false: the plain 1x bounds).
function loop({ secs, measure, coarse = () => false, precise = false, audio0 = 0, paused = false, seekLatency = 0.1, resyncAt = [],
  st = C.newSteer(), rate = () => 1, video = null, errFrom = -Infinity }) {
  const A = { t: audio0, paused, seekUntil: -1 };
  const log = { seeks: [], rates: [], maxErr: 0, rate: 1 };
  for (let i = 0; i <= secs * 10; i++) {
    const t = 1000 + i / 10;
    const seeking = t < A.seekUntil;
    const out = C.steer(st, { t, m: measure(t), rate: rate(t), audio: A.t, paused: A.paused, seeking, coarse: coarse(t), precise,
      resync: resyncAt.some((x) => Math.abs(x - t) < 0.05) });
    st = out.st;
    if (out.seek != null) { log.seeks.push({ t, to: out.seek }); A.t = out.seek; A.seekUntil = t + seekLatency; }
    if (out.rate !== log.rate) { log.rates.push({ t, rate: out.rate }); log.rate = out.rate; }
    A.paused = false;
    log.st = st; log.audio = A.t; // audio position at t
    if (video && t >= errFrom) log.maxErr = Math.max(log.maxErr, Math.abs(A.t - video(t)));
    if (!(t < A.seekUntil)) A.t += 0.1 * log.rate;
  }
  return log;
}
const truth = (t) => 500 + (t - 1000); // commentary position that matches the picture
// deterministic uniform noise in [-0.5, 0.5)
const lcg = (seed) => () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32 - 0.5;
// F1 TV's best position estimate: lags 0-50 ms, sawtooth with a ~5 s period
const sawtooth = (t) => truth(t) - 0.005 - 0.045 * (((t - 1000) / 5.3) % 1);

test('steer: steady playback on a sawtoothing position: no seeks, no rate changes', () => {
  const r = loop({ secs: 600, measure: sawtooth, audio0: truth(1000) });
  assert.equal(r.seeks.length, 0);
  assert.equal(r.rates.length, 0);
});

test('steer: where v0.2.1\'s 40 ms deadband chased the sawtooth every cycle, steer sits still', () => {
  // v0.2.1: rate = 1 -/+ max(1%, 0.3*|drift|) whenever |audio - measured| > 40 ms, every tick.
  // With the audio ~20 ms late (any seek landing) and a slight video/audio clock skew
  // (Tiledmedia trims the video rate), the sawtooth's peaks keep crossing 40 ms.
  const skew = 1.0003;
  const measure = (t) => truth(1000) + (t - 1000) * skew - 0.005 - 0.045 * (((t - 1000) / 5.3) % 1);
  let a = truth(1000) - 0.02, rate = 1, changes = 0;
  for (let i = 0; i <= 3000; i++) {
    const t = 1000 + i / 10, drift = a - measure(t), ad = Math.abs(drift);
    const next = ad <= 0.04 ? 1 : 1 + (drift > 0 ? -1 : 1) * Math.min(0.08, Math.max(0.01, ad * 0.3));
    if (next !== rate) changes++;
    rate = next;
    a += 0.1 * rate;
  }
  assert.ok(changes > 30, `v0.2.1 controller: ${changes} rate changes in 5 min`);
  const r = loop({ secs: 300, measure, audio0: truth(1000) - 0.02 });
  assert.equal(r.seeks.length, 0);
  assert.ok(r.rates.length <= 2, `steer: ${r.rates.length} rate changes in 5 min`);
});

test('steer: a real jump (video seek) re-locks and seeks at once, landing ahead by the lead', () => {
  const jumpAt = 1010;
  const r = loop({ secs: 20, measure: (t) => (t < jumpAt ? truth(t) : truth(t) + 120), audio0: truth(1000) });
  assert.equal(r.seeks.length, 1);
  assert.ok(r.seeks[0].t - jumpAt < 0.11, `seeked ${r.seeks[0].t - jumpAt}s after the jump`);
  assert.ok(Math.abs(r.seeks[0].to - (truth(r.seeks[0].t) + 120) - C.STEER.LEAD_NEAR) < 1e-6);
  assert.ok(Math.abs(r.audio - (truth(1020) + 120)) < 0.02, `landed ${r.audio - (truth(1020) + 120)}s off`);
});

test('steer: a nudge (resync) seeks right away even though it is under SNAP', () => {
  const r = loop({ secs: 5, measure: (t) => (t < 1002 ? truth(t) : truth(t) + 0.5), audio0: truth(1000), resyncAt: [1002] });
  assert.equal(r.seeks.length, 1);
  assert.ok(Math.abs(r.seeks[0].t - 1002) < 0.05);
});

test('steer: one stale sample is ignored, a persistent 0.3 s jump is followed after PERSIST', () => {
  const stale = loop({ secs: 10, measure: (t) => (Math.abs(t - 1005) < 0.05 ? truth(t) - 0.4 : truth(t)), audio0: truth(1000) });
  assert.equal(stale.seeks.length + stale.rates.length, 0);
  const step = loop({ secs: 10, measure: (t) => (t < 1005 ? truth(t) : truth(t) + 0.3), audio0: truth(1000) });
  assert.equal(step.seeks.length, 1);
  const lag = step.seeks[0].t - 1005;
  assert.ok(lag >= C.STEER.PERSIST - 0.01 && lag < C.STEER.PERSIST + 0.15, `followed after ${lag}s`);
});

test('steer: 120 ms behind -> one gentle speed-up, back to exactly 1x when within STOP', () => {
  const r = loop({ secs: 30, measure: truth, audio0: truth(1000) - 0.12 });
  assert.equal(r.seeks.length, 0);
  assert.equal(r.rates.length, 2, JSON.stringify(r.rates));
  assert.ok(r.rates[0].rate > 1 && r.rates[0].rate <= 1 + C.STEER.K_MAX);
  assert.equal(r.rates[1].rate, 1);
  assert.ok(Math.abs(r.audio - truth(1030)) <= C.STEER.STOP + 0.01);
});

// A precise position source (isPrecise) at 1x: the measured drift sits in one 5 ms bin for
// minutes (sd ~1 ms, 2026-10-11 race) and steps by ~45 ms now and then; every correction
// there is resampled, inaudible. STEER.PRECISE's tighter bounds and the slow TRIM stage.
test('steer on a precise source: a 19 ms residual (a slap-back echo for two hours that day) is trimmed, once, quietly', () => {
  const rnd = lcg(7);
  const measure = (t) => truth(t) + rnd() * 0.005; // 5 ms uniform noise
  // +19 ms: inside the plain START (60 ms) and the precise one (25 ms), the hysteresis alone never touches it
  const r = loop({ secs: 90, measure, precise: true, audio0: truth(1000) + 0.019, video: truth, errFrom: 1030 });
  assert.equal(r.seeks.length, 0);
  assert.ok(r.rates.length <= 2 && r.rates.length > 0, JSON.stringify(r.rates));
  assert.ok(Math.abs(r.rates[0].rate - (1 - C.STEER.TRIM.K)) < 1e-9, `trim at ${r.rates[0].rate}`); // 0.3% slower, resampled
  assert.ok(r.rates[0].t >= 1000 + C.STEER.TRIM.WAIT, `fired after ${r.rates[0].t - 1000} s`); // not before the offset has sat there
  assert.equal(r.rates[1].rate, 1);
  assert.ok(r.rates[1].t < 1030, `done at ${r.rates[1].t}`);
  assert.ok(r.maxErr < C.STEER.PRECISE.STOP, `drift ${r.maxErr.toFixed(4)} s during 1030-1090`); // and stays there
  // the same offset on the plain source: left alone, as before (nothing to trim by)
  const plain = loop({ secs: 90, measure, audio0: truth(1000) + 0.019 });
  assert.equal(plain.seeks.length + plain.rates.length, 0);
});

test('steer on a precise source: a 45 ms step (player.js relearning the element offset) is one correction, done within 15 s', () => {
  const stepAt = 1005;
  const video = (t) => (t < stepAt ? truth(t) : truth(t) + 0.045);
  const r = loop({ secs: 30, measure: video, precise: true, audio0: truth(1000), video });
  assert.equal(r.seeks.length, 0);
  assert.equal(r.rates.length, 2, JSON.stringify(r.rates)); // k on, then off: no trim afterwards either
  assert.ok(r.rates[0].rate > 1 && r.rates[0].rate <= 1 + C.STEER.PRECISE.K_MAX, JSON.stringify(r.rates));
  assert.equal(r.rates[1].rate, 1);
  assert.ok(r.rates[1].t - stepAt <= 15, `corrected ${(r.rates[1].t - stepAt).toFixed(1)} s after the step`);
  // the lock lags the step (TAU): a correction judged on it alone would quit ~13 ms short
  assert.ok(Math.abs(r.audio - video(1030)) <= C.STEER.PRECISE.STOP + 1e-6, `${(r.audio - video(1030)).toFixed(4)} s off at the end`);
  // on the plain source that step stays inside START for good (the slap-back echo)
  const plain = loop({ secs: 30, measure: video, audio0: truth(1000), video });
  assert.equal(plain.rates.length, 0);
  assert.ok(Math.abs(plain.audio - video(1030) + 0.045) < 1e-6);
});

test('steer: the plain source keeps the old bounds (120 ms behind: one speed-up at K_MAX, done within STOP, not PRECISE.STOP)', () => {
  const r = loop({ secs: 30, measure: truth, audio0: truth(1000) - 0.12 });
  assert.equal(r.seeks.length, 0);
  assert.equal(r.rates.length, 2, JSON.stringify(r.rates));
  assert.ok(Math.abs(r.rates[0].rate - (1 + C.STEER.K_MAX)) < 1e-9, JSON.stringify(r.rates)); // 2%, not 3%
  assert.equal(r.rates[1].rate, 1);
  const left = Math.abs(r.audio - truth(1030));
  assert.ok(left < C.STEER.STOP && left > C.STEER.PRECISE.STOP, `${left.toFixed(4)} s left`); // 15 ms band, not 6
  // the precise source: a 2.4% speed-up (within its 3%), done within 6 ms, still one episode
  const p = loop({ secs: 30, measure: truth, audio0: truth(1000) - 0.12, precise: true });
  assert.equal(p.seeks.length, 0);
  assert.deepEqual(p.rates.map((x) => +x.rate.toFixed(4)), [1.024, 1], JSON.stringify(p.rates)); // never a trim on top
  assert.ok(Math.abs(p.audio - truth(1030)) < C.STEER.PRECISE.STOP);
});

test('steer: the trim never fires while a correction runs, while paused, or off 1x', () => {
  // a correction running (the test above): rates are the correction's and 1, nothing at 1 ± 0.3%
  const p = loop({ secs: 30, measure: truth, audio0: truth(1000) - 0.12, precise: true });
  assert.ok(p.rates.every((x) => x.rate === 1 || Math.abs(x.rate - 1) > C.STEER.TRIM.K + 1e-9), JSON.stringify(p.rates));
  // paused 15 s, 15 ms ahead: nothing
  let st = C.newSteer();
  for (let i = 0; i <= 150; i++) {
    const t = 1000 + i / 10;
    const out = C.steer(st, { t, m: truth(t), rate: 1, audio: truth(t) + 0.015, paused: true, seeking: false, coarse: false, precise: true, resync: false });
    st = out.st;
    assert.equal(out.rate, 1);
    assert.equal(out.seek, undefined);
    assert.equal(st.k, 0);
  }
  // 15 ms ahead at 1.5x for a minute: the rate is 1.5 throughout, no seek
  const v = (t) => 500 + 1.5 * (t - 1000);
  const fast = loop({ secs: 60, measure: v, precise: true, audio0: v(1000) + 0.015, rate: () => 1.5 });
  assert.equal(fast.seeks.length, 0);
  assert.deepEqual([...new Set(fast.rates.map((x) => x.rate))], [1.5]);
});

test('steer on a precise source: a 12 ms offset that keeps coming back is trimmed without chatter', () => {
  // the measurement steps +12 ms every 20 s: each step is trimmed away (one on, one off), the
  // next needs another TRIM.WAIT of stable offset; never a bigger correction, never a seek
  const steps = 6;
  const video = (t) => truth(t) + 0.012 * Math.min(steps, Math.floor((t - 1000) / 20));
  const r = loop({ secs: 20 * steps, measure: video, precise: true, audio0: truth(1000), video });
  assert.equal(r.seeks.length, 0);
  assert.ok(r.rates.length > 0 && r.rates.length <= 2 * steps, `${r.rates.length} rate changes for ${steps} steps`);
  const on = [], off = [];
  r.rates.forEach((x, i) => {
    if (i % 2 === 0) { assert.ok(Math.abs(x.rate - (1 + C.STEER.TRIM.K)) < 1e-9, JSON.stringify(x)); on.push(x.t); }
    else { assert.equal(x.rate, 1); off.push(x.t); }
  });
  assert.equal(on.length, off.length);
  for (let i = 1; i < on.length; i++) assert.ok(on[i] - on[i - 1] >= C.STEER.TRIM.WAIT, `trims ${(on[i] - on[i - 1]).toFixed(1)} s apart`);
  assert.ok(r.maxErr < C.STEER.PRECISE.START, `max drift ${r.maxErr.toFixed(4)} s`); // the trim keeps up, no real correction needed
});

test('steer: resuming on a coarse position does not seek; the precise one re-locks quietly', () => {
  // audio paused exactly where the picture paused; the first samples after resume read
  // 300 ms ahead (player.js 'tm' with a stale age), the precise ones arrive 0.4 s later
  const r = loop({ secs: 10, paused: true, audio0: truth(1000), coarse: (t) => t < 1000.4,
    measure: (t) => (t < 1000.4 ? truth(t) + 0.3 : truth(t) - 0.02) });
  assert.equal(r.seeks.length, 0);
  assert.equal(r.rates.length, 0);
});

test('steer: after a seek it learns the seek latency (lead) from how the seek landed', () => {
  // seeks really take 0.2 s here, the default lead assumes 0.1 s: the first lands 0.1 s
  // late and needs a rate correction afterwards; the second, with the learnt lead, doesn't
  const r = loop({ secs: 15, measure: truth, audio0: truth(1000) - 5, seekLatency: 0.18 });
  assert.equal(r.seeks.length, 1);
  assert.equal(r.rates.length, 2);
  assert.ok(Math.abs(r.st.lead.near - 0.15) < 0.01, `lead ${r.st.lead.near}`); // half the miss
  const r2 = loop({ secs: 15, measure: truth, audio0: truth(1000) - 5, seekLatency: 0.18, st: C.holdSteer(r.st) });
  assert.equal(r2.seeks.length, 1);
  assert.equal(r2.rates.length, 0);
  assert.ok(Math.abs(r2.audio - truth(1015)) < 0.06);
});

test('steer: no new seek while one is landing; holdSteer forgets the lock', () => {
  const st = C.steer(C.newSteer(), { t: 1, m: 100, rate: 1, audio: 0, paused: true, seeking: false, coarse: false, resync: false });
  assert.ok(st.seek != null);
  const again = C.steer(st.st, { t: 1.1, m: 100.1, rate: 1, audio: st.seek, paused: false, seeking: true, coarse: false, resync: false });
  assert.equal(again.seek, undefined);
  assert.equal(again.rate, 1);
  assert.equal(C.holdSteer(again.st).lock, null);
  assert.deepEqual(C.holdSteer(again.st).lead, again.st.lead);
});

test('at 2x the audio rate is never nudged (pitch-preserving rate changes click)', () => {
  let st = C.newSteer();
  let audio = 100, t = 0, rates = new Set(), seeks = 0;
  for (let i = 0; i < 600; i++) {           // 60 s at 2x with a slow 0.5% clock skew + jitter
    t += 0.1;
    audio += 0.1 * 2 * 1.005;
    const m = 100 + t * 2 + (i % 7) * 0.01;
    const out = C.steer(st, { t, m, rate: 2, audio, paused: false, seeking: false, coarse: false, resync: false });
    st = out.st;
    rates.add(out.rate);
    if (out.seek != null) { seeks++; audio = out.seek; }
  }
  assert.deepEqual([...rates], [2]);
  assert.ok(seeks <= 3, `seeks ${seeks}`);
});

test('steer at the chosen 1x while F1 TV runs the element at x1.18, then x0.75: no speed change, a seek per excursion', () => {
  // after a seek on the real page: the SDK plays at 1x, its PID runs the element at x1.18 for
  // 3 s, then at x0.75 for 3 s; the content position (the truth) moves with the element
  const trimAt = (t) => (t >= 1005 && t < 1008 ? 1.18 : t >= 1008 && t < 1011 ? 0.75 : 1);
  const clip = (x) => Math.min(3, Math.max(0, x));
  const video = (t) => truth(t) + 0.18 * clip(t - 1005) - 0.25 * clip(t - 1008);
  const nominal = (t) => C.nominalRate(C.chosenRate({ sdk: 1, exact: true, want: 1, el: trimAt(t) }, 1));
  const r = loop({ secs: 30, audio0: truth(1000), video, rate: nominal,
    measure: (t) => video(t) - 0.005 - 0.045 * (((t - 1000) / 5.3) % 1) });
  // only resampled tweaks (pitch not kept at 1x), never a time-stretched x1.18 or x0.75
  assert.ok(r.rates.every((x) => Math.abs(x.rate - 1) <= C.STEER.K_MAX + 1e-9), JSON.stringify(r.rates));
  assert.ok(r.seeks.length >= 1 && r.seeks.length <= 2, JSON.stringify(r.seeks));
  assert.ok(r.maxErr < 0.5, `max error ${r.maxErr.toFixed(3)}s`);
  assert.ok(Math.abs(r.audio - video(1030)) < 0.03, `${(r.audio - video(1030)).toFixed(3)}s off at the end`);
});

// F1 TV's view-sync PID at a speed, as a Mac at 1.5x reported it (2026-10-10): the element
// alternately ~12% fast and ~12% slow for about 2 s each, so the picture swings ±0.2 s around
// its steady pace. v0.2.16 re-locked on every swing and seeked the commentary every ~2 s.
function swinging(speed, depth, half, t0 = 1000) {
  const tri = (t) => { // the picture's lead over its steady pace: a triangle wave
    const k = (t - t0) / half, i = Math.floor(k), f = k - i;
    return speed * depth * half * (i % 2 === 0 ? f : 1 - f) - (speed * depth * half) / 2;
  };
  const video = (t) => 500 + speed * (t - t0) + tri(t);
  return { video, measure: (t) => video(t) - 0.005 - 0.045 * (((t - t0) / 5.3) % 1) };
}

test('steer at 1.5x and 2x while F1 TV\'s view sync swings the picture: no seeks', () => {
  for (const [speed, depth, half] of [[1.5, 0.12, 2], [1.5, 0.15, 2.5], [2, 0.12, 2], [2, 0.15, 1.6]]) {
    const { video, measure } = swinging(speed, depth, half); // starts on a peak, the worst case
    const r = loop({ secs: 120, audio0: video(1000), video, measure, rate: () => speed });
    const label = `${speed}x, ±${depth * 100}% every ${half} s`;
    assert.equal(r.seeks.length, 0, `${label}: ${r.seeks.length} seeks in 2 min ${JSON.stringify(r.seeks.slice(0, 4))}`);
    assert.deepEqual([...new Set(r.rates.map((x) => x.rate))], [speed], label); // pitch-kept rate never nudged
    // off the picture by at most one swing, peak to peak (here under 0.3 wall seconds)
    assert.ok(r.maxErr <= speed * depth * half + 0.06, `${label}: max error ${r.maxErr.toFixed(3)} s`);
  }
});

test('steer at 1.5x: a real offset is still followed, a big jump at once', () => {
  const v = (t) => 500 + 1.5 * (t - 1000);
  const step = loop({ secs: 15, audio0: v(1000), rate: () => 1.5, measure: (t) => (t < 1005 ? v(t) : v(t) + 0.8) });
  assert.equal(step.seeks.length, 1, JSON.stringify(step.seeks));
  assert.ok(step.seeks[0].t - 1005 < C.STEER.FAST.PERSIST + 0.15, `followed after ${(step.seeks[0].t - 1005).toFixed(2)} s`);
  assert.ok(Math.abs(step.audio - (v(1015) + 0.8)) < 0.1, `${(step.audio - v(1015) - 0.8).toFixed(3)} s off at the end`);
  const jump = loop({ secs: 5, audio0: v(1000), rate: () => 1.5, measure: (t) => (t < 1002 ? v(t) : v(t) + 30) });
  assert.equal(jump.seeks.length, 1);
  assert.ok(jump.seeks[0].t - 1002 < 0.11, `seeked ${jump.seeks[0].t - 1002}s after the jump`);
});

test('steer at 2x: a seek lands where the picture will be (the lead is in wall seconds)', () => {
  // a seek takes 0.2 s (two of the loop's steps), the lead learnt at 1x says so; at 2x the
  // picture moves 0.4 s meanwhile (v0.2.19 landed 0.2 s behind it)
  const v = (t) => 500 + 2 * (t - 1000);
  const st = { ...C.newSteer(), lead: { near: 0.2, far: 0.3 } };
  const r = loop({ secs: 10, audio0: v(1000) - 5, rate: () => 2, measure: v, seekLatency: 0.15, st });
  assert.equal(r.seeks.length, 1, JSON.stringify(r.seeks));
  assert.ok(Math.abs(r.audio - v(1010)) < 0.02, `landed ${(r.audio - v(1010)).toFixed(3)} s off`);
  assert.ok(Math.abs(r.st.lead.near - 0.2) < 0.01, `lead ${r.st.lead.near}`); // nothing to learn
});

test('steer at 2x: a seek that landed badly gets one more, and only one', () => {
  // the lead says 0.1 s, the seek takes 0.3 s: at 2x the first lands 0.4 s behind
  const v = (t) => 500 + 2 * (t - 1000);
  const r = loop({ secs: 20, audio0: v(1000) - 5, rate: () => 2, measure: v, seekLatency: 0.25 });
  assert.equal(r.seeks.length, 2, JSON.stringify(r.seeks));
  assert.ok(Math.abs(r.audio - v(1020)) < 0.25, `${(r.audio - v(1020)).toFixed(3)} s off at the end`);
  // a seek that never lands well (the target jitters by 0.5 s): no third
  const jitter = (t) => v(t) + (Math.floor(t * 10) % 2 ? 0.25 : -0.25);
  const r2 = loop({ secs: 20, audio0: v(1000) - 5, rate: () => 2, measure: jitter, seekLatency: 0.25 });
  assert.ok(r2.seeks.length <= 2, JSON.stringify(r2.seeks));
});

test('steer on a plain <video> switched from 1x to 1.25x: followed at once, no seeks', () => {
  const video = (t) => truth(t) + 0.25 * Math.max(0, t - 1005);
  const nominal = (t) => C.nominalRate(C.chosenRate({ tiled: false, el: t < 1005 ? 1 : 1.25 }, 1));
  const r = loop({ secs: 60, audio0: truth(1000), video, rate: nominal, measure: video });
  assert.equal(r.seeks.length, 0, JSON.stringify(r.seeks));
  assert.deepEqual(r.rates.map((x) => x.rate), [1.25]);
  assert.ok(r.maxErr < 0.05, `max error ${r.maxErr.toFixed(3)}s`);
});

test('chosenRate: a brief SDK dip under the speed page.js holds is ridden out; a lasting one is followed', () => {
  const x = (sdk, dipFor, want = 1.5) => ({ tiled: true, sdk, exact: true, want, el: sdk, dipFor });
  assert.equal(C.chosenRate(x(1, 0.3), 1.5), 1.5); // a rebuilt handle at 1 for a moment: stay
  assert.equal(C.chosenRate(x(1, 2.4), 1.5), 1.5);
  assert.equal(C.chosenRate(x(1, 2.6), 1.5), 1); // still at 1 after 2.6 s: it's real
  assert.equal(C.chosenRate(x(1, 0.2), 1), 1); // not yet at the new speed someone picked: follow the SDK
  assert.equal(C.chosenRate(x(2, 0.2, 2), 1.5), 2); // the SDK reached it
  assert.equal(C.chosenRate({ ...x(1.2, 0.3), exact: false, el: 1.2 }, 1.5), 1.5); // guessed: the trim band decides
});

console.log(`${n} passed`);
