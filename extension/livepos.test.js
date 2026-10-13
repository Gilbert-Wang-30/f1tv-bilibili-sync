// node extension/livepos.test.js
// The pure parts of livepos.js (SignalR framing, Position.z decoding, batching, dedupe) and
// f1token.js (the login cookie), against recordings of the real feed in server/tests/fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');

require('./livepos.js');
require('./f1token.js');
const L = globalThis.FoneLivePos._internals;
const { parseLoginSession, jwtClaims } = globalThis.FoneF1Token;

const FIX = path.join(__dirname, '..', 'server', 'tests', 'fixtures', 'timing');
// 32 s of the real anonymous SSE stream (handshake, Subscribe keyframes, pings).
const SNAPSHOT = zlib.gunzipSync(fs.readFileSync(path.join(FIX, 'sse_snapshot_11730.txt.gz'))).toString('utf8');
// 3 min of the KL qualifying archive's Position.z (190 messages, 722 samples).
const POSZ = zlib.gunzipSync(fs.readFileSync(path.join(FIX, 'static/2026/2026-10-04_Bahrain_Grand_Prix/2026-10-03_Qualifying/Position.z.jsonStream.gz')))
  .toString('utf8').replace(/^﻿/, '').split(/\r?\n/).filter((l) => l.length > 12).map((l) => JSON.parse(l.slice(12)));
const inflateNode = (b64) => JSON.parse(zlib.inflateRawSync(Buffer.from(b64, 'base64')).toString('utf8'));

const tests = [];
const test = (name, f) => tests.push([name, f]);

// ---- framing -------------------------------------------------------------------

test('WebSocket messages split into frames on the record separator', () => {
  const msg = '{}\x1e{"type":6}\x1e{"type":1,"target":"feed","arguments":["Position.z","x","t"]}\x1e';
  assert.deepEqual(L.splitFrames(msg), [{}, { type: 6 }, { type: 1, target: 'feed', arguments: ['Position.z', 'x', 't'] }]);
  assert.deepEqual(L.splitFrames('{"type":6}\x1enot json\x1e\x1e  \x1e{"type":7}'), [{ type: 6 }, { type: 7 }]);
});

test('the real SSE stream parses the same in any chunking (CRLFs split, RS inside events)', () => {
  const whole = L.sseParser()(SNAPSHOT);
  assert.deepEqual(whole[0], {}); // handshake answer
  assert.equal(whole[1].type, 3);
  assert.equal(whole[1].result.SessionInfo.Key, 11730);
  assert.ok(!('Position.z' in whole[1].result), 'anonymous: no car positions');
  assert.ok(whole.slice(2).every((f) => f.type === 6) && whole.length >= 3);
  for (const size of [1, 2, 3, 7, 64, 4093]) {
    const feed = L.sseParser();
    const got = [];
    for (let i = 0; i < SNAPSHOT.length; i += size) got.push(...feed(SNAPSHOT.slice(i, i + size)));
    assert.deepEqual(got, whole, `chunks of ${size}`);
  }
});

test('SSE: several frames in one event, multi-line data, comments and an unfinished tail', () => {
  const feed = L.sseParser();
  assert.deepEqual(feed(':\r\ndata: {}\x1e{"type":6}\x1e\r\n\r\ndata: {"a":\r\ndata: 1}\x1e\r\n\r\ndata: {"type":'), [{}, { type: 6 }, { a: 1 }]);
  assert.deepEqual(feed('6}\x1e\r'), []);
  assert.deepEqual(feed('\n\r\n'), [{ type: 6 }]);
});

// ---- Position.z ------------------------------------------------------------------

test('inflateZ (DecompressionStream deflate-raw) matches zlib on every recorded message', async () => {
  assert.equal(POSZ.length, 190);
  for (const b64 of POSZ) assert.deepEqual(await L.inflateZ(b64), inflateNode(b64));
  const obj = { Position: [] };
  assert.equal(await L.inflateZ(obj), obj, 'already-decoded payloads pass through');
  await assert.rejects(L.inflateZ('bm90IGRlZmxhdGU='));
});

test('isoMicros keeps F1 timestamps exact to the microsecond', () => {
  const base = Date.parse('2026-10-09T12:34:17Z') * 1000;
  assert.equal(L.isoMicros('2026-10-09T12:34:17.2631795Z'), base + 263179); // 7 digits: 100 ns ticks
  assert.equal(L.isoMicros('2026-10-09T12:34:17.483146Z'), base + 483146);
  assert.equal(L.isoMicros('2026-10-09T12:34:17.48Z'), base + 480000);
  assert.equal(L.isoMicros('2026-10-09T12:34:17Z'), base);
  assert.equal(L.isoMicros('2026-10-09T12:34:17.5'), base + 500000, 'no zone = UTC, like the server');
  assert.ok(Number.isSafeInteger(L.isoMicros('2026-10-09T12:34:17.2631795Z')));
  for (const bad of ['', 'garbage', null, undefined, '2026-10-09 12:34:17Z', '2026-10-09T12:34:17+02:00']) {
    assert.ok(Number.isNaN(L.isoMicros(bad)), String(bad));
  }
});

test('cleanEntries forwards X, Y, Z and Status only', () => {
  const e = L.cleanEntries({
    1: { Status: 'OnTrack', X: -1138, Y: 3621, Z: 184, Extra: 'x' },
    44: { Status: 'OffTrack', X: '12', Y: '-7' },
    63: { Status: 'OnTrack', X: null, Y: 3 },
    81: null,
  });
  assert.deepEqual(e, { 1: { X: -1138, Y: 3621, Z: 184, Status: 'OnTrack' }, 44: { X: 12, Y: -7, Z: 0, Status: 'OffTrack' } });
});

// ---- batching -------------------------------------------------------------------

const samples = async (from, to) => {
  const out = [];
  for (const b64 of POSZ.slice(from, to)) out.push(...(await L.inflateZ(b64)).Position);
  return out;
};

test('the recorded feed relays as 722 time-ordered samples, re-sends dropped', async () => {
  const q = L.newQueue();
  let n = 0;
  for (const b64 of POSZ) n += L.enqueue(q, (await L.inflateZ(b64)).Position, 0);
  assert.equal(n, 722);
  assert.equal(q.items.length, 722);
  assert.ok(q.items.every((s, i) => i === 0 || s.t > q.items[i - 1].t));
  // A reconnect: the keyframe is the last message again and the feed resumes 5 back.
  n = L.enqueue(q, (await L.inflateZ(POSZ[189])).Position, 1);
  for (const b64 of POSZ.slice(185)) n += L.enqueue(q, (await L.inflateZ(b64)).Position, 1);
  assert.equal(n, 0);
  assert.equal(q.dupes, (await samples(189, 190)).length + (await samples(185, 190)).length);
  assert.equal(q.newestTs, (await samples(189, 190)).pop().Timestamp);
});

test('batches: oldest first, capped, settled only once sent; old samples dropped not sent', async () => {
  const q = L.newQueue();
  const all = await samples(0, 190);
  L.enqueue(q, all.slice(0, 100), 0); // received at t=0 ms
  L.enqueue(q, all.slice(100), 90000); // received 90 s later
  let b = L.nextBatch(q, 100000, 120000);
  assert.equal(b.length, 722);
  assert.equal(b[0].ts, all[0].Timestamp);
  assert.equal(L.settle(q, b.slice(0, 10)), 10, 'only what the server took');
  assert.equal(q.items.length, 712);
  b = L.nextBatch(q, 125000, 120000); // the first 100 are now 125 s old
  assert.equal(q.dropped, 90);
  assert.equal(b[0].ts, all[100].Timestamp);
  assert.equal(L.settle(q, b), 622);
  assert.equal(L.nextBatch(q, 125000, 120000).length, 0);
  // A batch that aged out while it was in flight settles harmlessly.
  L.enqueue(q, [{ Timestamp: '2026-10-03T10:00:00Z', Entries: { 1: { X: 1, Y: 2 } } }], 0);
  const inFlight = L.nextBatch(q, 0, 120000);
  L.nextBatch(q, 999999, 120000);
  assert.equal(L.settle(q, inFlight), 0);
});

test('a POST carries at most 1000 samples', () => {
  const q = L.newQueue();
  const t0 = Date.parse('2026-10-03T10:00:00Z');
  const many = Array.from({ length: 2500 }, (_, i) => ({ Timestamp: new Date(t0 + i * 250).toISOString(), Entries: { 1: { X: i, Y: 1 } } }));
  assert.equal(L.enqueue(q, many, 0), 2500);
  assert.equal(L.nextBatch(q, 0, 120000).length, 1000);
});

test('bad samples are counted, not queued', () => {
  const q = L.newQueue();
  assert.equal(L.enqueue(q, [{ Timestamp: 'x', Entries: { 1: { X: 1, Y: 1 } } }, { Timestamp: '2026-10-03T10:00:00Z', Entries: {} }, null], 0), 0);
  assert.equal(L.enqueue(q, 'not a list', 0), 0);
  assert.equal(q.bad, 3);
});

test('POST outcomes: retry server trouble, drop what a resend cannot fix', () => {
  assert.deepEqual([200, 204, 500, 502, 503, 429, 408, 400, 403, 404, 422].map(L.postOutcome),
    ['ok', 'ok', 'retry', 'retry', 'retry', 'retry', 'retry', 'drop', 'drop', 'drop', 'drop']);
  assert.deepEqual([0, 1, 2, 6, 99].map((f) => L.backoff([1, 2, 5, 10, 30, 60], f)), [1, 1, 2, 60, 60]);
});

test('status errors never show anything token-shaped', () => {
  const jwt = 'eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjF9.c2ln';
  assert.equal(L.scrub(`wss://x/signalrcore?access_token=${jwt}&id=1 failed`), 'wss://x/signalrcore?access_token=‹token›&id=1 failed');
  assert.equal(L.scrub(`Bearer ${jwt}`), 'Bearer ‹token›');
});

// ---- the login cookie --------------------------------------------------------------

const b64url = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
const jwt = (claims) => `${b64url({ alg: 'RS256', kid: 'k' })}.${b64url(claims)}.c2lnbmF0dXJl`;
const NOW = 1791600000;
const cookieOf = (token, extra = {}) => encodeURIComponent(JSON.stringify({ data: { subscriptionToken: token, ...extra } }));

test('login-session: the subscription token, however the cookie is encoded', () => {
  const t = jwt({ exp: NOW + 3600, SubscriptionStatus: 'active' });
  assert.deepEqual(parseLoginSession(cookieOf(t, { other: 'x' }), NOW), { token: t, exp: NOW + 3600 });
  assert.equal(parseLoginSession(encodeURIComponent(cookieOf(t)), NOW).token, t, 'encoded twice');
  assert.equal(parseLoginSession(JSON.stringify({ data: { subscriptionToken: t } }), NOW).token, t, 'not encoded');
  assert.equal(parseLoginSession(`"${cookieOf(t)}"`, NOW).token, t, 'quoted cookie value');
  assert.equal(parseLoginSession(cookieOf(jwt({ sub: 'no exp' })), NOW).exp, Infinity);
  assert.equal(jwtClaims(t).SubscriptionStatus, 'active');
});

test('login-session: expired, garbled or missing tokens give null', () => {
  for (const v of [cookieOf(jwt({ exp: NOW - 1 })), cookieOf(jwt({ exp: NOW + 10 })), cookieOf('not-a-jwt'), cookieOf(42),
    encodeURIComponent(JSON.stringify({ data: {} })), encodeURIComponent('{"data":'), '%E0%A4%A', '', null, undefined,
    encodeURIComponent(encodeURIComponent(encodeURIComponent(encodeURIComponent(JSON.stringify({ data: { subscriptionToken: jwt({}) } })))))]) {
    assert.equal(parseLoginSession(v, NOW), null, String(v).slice(0, 40));
  }
});

test('f1Token picks the freshest cookie, survives no access, and never logs', async () => {
  const logged = [];
  const saved = {};
  for (const k of ['log', 'info', 'warn', 'error', 'debug']) {
    saved[k] = console[k];
    console[k] = (...a) => logged.push(a);
  }
  const later = jwt({ exp: Date.now() / 1000 + 7200 });
  const sooner = jwt({ exp: Date.now() / 1000 + 600 });
  try {
    let cookies = [{ value: cookieOf(sooner) }, { value: 'garbage' }, { value: cookieOf(later) }];
    let query = null;
    globalThis.chrome = { cookies: { getAll: async (q) => { query = q; return cookies; } } };
    assert.equal(await globalThis.FoneF1Token.f1Token(), later);
    assert.deepEqual(query, { domain: 'formula1.com', name: 'login-session' });
    cookies = [];
    assert.equal(await globalThis.FoneF1Token.f1Token(), null);
    globalThis.chrome = { cookies: { getAll: async () => { throw new Error('no permission'); } } };
    assert.equal(await globalThis.FoneF1Token.f1Token(), null);
  } finally {
    Object.assign(console, saved);
    delete globalThis.chrome;
  }
  assert.deepEqual(logged, []);
});

test('start() refuses bad arguments and an F1 URL on the fone server (the token would go there)', () => {
  let asked = 0;
  const getToken = async () => { asked++; return 'eyJ.x.y'; };
  const P = globalThis.FoneLivePos;
  P.start({ sessionKey: 11379, server: 'http://fone.test:8788', f1Base: 'http://fone.test:8788/signalrcore', getToken });
  assert.deepEqual([P.status().state, P.status().lastError], ['error', 'f1Base must not be the fone server']);
  P.start({ sessionKey: 11379, server: 'http://fone.test:8788/', f1Base: 'http://fone.test:8788', getToken });
  assert.equal(P.status().lastError, 'f1Base must not be the fone server');
  P.start({ sessionKey: 'x', server: 'http://fone.test:8788', getToken });
  assert.equal(P.status().lastError, 'bad arguments');
  P.start({ sessionKey: 11379, server: 'fone.test:8788', getToken });
  assert.equal(P.status().lastError, 'bad arguments');
  P.start({ sessionKey: 11379, server: 'http://fone.test:8788' });
  assert.equal(P.status().lastError, 'bad arguments');
  assert.equal(asked, 0);
  assert.equal(L.F1_BASE, 'https://livetiming.formula1.com/signalrcore');
});

test('neither module ever logs (the token must not end up in a console)', () => {
  for (const f of ['livepos.js', 'f1token.js']) {
    const src = fs.readFileSync(path.join(__dirname, f), 'utf8');
    assert.ok(!/\bconsole\s*\.|\bdebugger\b/.test(src), `${f} logs`);
  }
});

(async () => {
  let failed = 0;
  for (const [name, f] of tests) {
    try {
      await f();
      console.log('ok -', name);
    } catch (e) {
      failed++;
      console.log('FAIL -', name, '\n', e);
    }
  }
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exitCode = failed ? 1 : 0;
})();
