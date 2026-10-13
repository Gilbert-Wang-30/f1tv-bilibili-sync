// node relay_node.js — livepos.js in Node 22 (its fetch, WebSocket and DecompressionStream
// are the browser's APIs) against the mock F1 SignalR server (mock_f1.py) and a dev fone
// server. Run by run.sh; env MOCK, FONE, LIVEPOS, FIXTURE (the recording the mock plays, .gz or
// not); optional ONLY (run tests whose name contains it), RATE (mock messages per second),
// WAIT (seconds a scenario may take; 30).
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const zlib = require('node:zlib');

const MOCK = process.env.MOCK || 'http://fone-dev-livepos-mock:8080';
const FONE = process.env.FONE || 'http://fone-dev-livepos-server:8788';
// The flaky proxy in front of fone, on another port: livepos refuses an F1 URL on fone's origin.
const FLAKY = MOCK.replace(/:8080$/, ':8081');
require(process.env.LIVEPOS || '/ext/livepos.js');
const P = globalThis.FoneLivePos;

// Every request livepos makes to the fone server, to prove the token never goes there.
const realFetch = globalThis.fetch;
const foneRequests = [];
globalThis.fetch = (url, init = {}) => {
  const u = String(url);
  if (u.includes('/api/timing/')) foneRequests.push({ at: Date.now(), url: u, init: JSON.stringify({ ...init, signal: undefined }) });
  return realFetch(url, init);
};

const FAST = { tick: 250, ping: 1000, silence: 6000, probation: 2000, stall: 3000, otherSession: 1000, noToken: 500,
  authRetry: 3000, backoff: [200, 400, 800, 1600], postTimeout: 5000 };
const TOKEN = 'eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.dGVzdC1vbmx5'; // fake, test-only
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The recording the mock plays, decoded here independently (zlib) for the expectations.
const raw = fs.readFileSync(process.env.FIXTURE);
const SAMPLES = (process.env.FIXTURE.endsWith('.gz') ? zlib.gunzipSync(raw) : raw).toString('utf8').replace(/^\uFEFF/, '')
  .split(/\r?\n/).filter((l) => l.length > 12)
  .flatMap((l) => JSON.parse(zlib.inflateRawSync(Buffer.from(JSON.parse(l.slice(12)), 'base64')).toString('utf8')).Position);

const N = SAMPLES.length; // 722 in the repo's 3-minute recording

async function mock(path, body) {
  if (body && process.env.RATE && body.rate === undefined) body = { ...body, rate: Number(process.env.RATE) };
  const r = await realFetch(MOCK + path, body ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) } : {});
  return r.json();
}

// The mock's log once every connection it saw is closed (it notices a client leaving within ~0.2 s).
async function closedLog() {
  for (let i = 0; i < 30; i++) {
    const log = await mock('/_mock/log');
    if (log.conns.every((c) => c.closed_by)) return log;
    await sleep(100);
  }
  return mock('/_mock/log');
}

async function until(what, cond, ms = Number(process.env.WAIT || 30) * 1000) {
  const t = Date.now();
  while (Date.now() - t < ms) {
    if (cond(P.status())) return P.status();
    await sleep(100);
  }
  throw new Error(`timed out waiting for ${what}: ${JSON.stringify(P.status())}`);
}

function run(key, extra = {}) {
  foneRequests.length = 0;
  return P.start({ sessionKey: key, server: FONE, f1Base: `${MOCK}/signalrcore`, getToken: async () => TOKEN, timing: FAST, ...extra });
}

// What livepos POSTed: every sample exactly once, all of them.
function postedTimestamps() {
  const ts = [];
  for (const r of foneRequests) {
    const body = JSON.parse(JSON.parse(r.init).body);
    for (const m of body.messages) ts.push(m.Timestamp);
  }
  return ts;
}

function assertAllOnce() {
  const ts = postedTimestamps();
  assert.equal(new Set(ts).size, ts.length, 'a sample was posted twice');
  assert.deepEqual(ts, SAMPLES.map((s) => s.Timestamp), 'posted samples differ from the recording');
}

function assertNoTokenToFone() {
  for (const r of foneRequests) {
    assert.ok(!r.url.includes(TOKEN) && !r.init.includes(TOKEN), 'the token went to the fone server');
    assert.ok(!/authorization|bearer|access_token/i.test(r.url + r.init), 'credentials-like data went to fone');
  }
}

const isoSec = (s) => {
  const m = /^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?Z$/.exec(s);
  return Date.parse(m[1] + 'Z') / 1000 + Number('0.' + ((m[2] || '0') + '000000').slice(0, 6));
};

// The dev server's GET /positions must serve back exactly what the recording has
// (on-track samples, X/Y not both 0, plus the one sample before the window).
async function assertServed(key) {
  const t = SAMPLES.map((s) => isoSec(s.Timestamp));
  const windows = [];
  for (let a = t[0] - 1; a < t[t.length - 1]; a += 100) windows.push([a, a + 100]); // the server answers <= 120 s
  const step = Math.max(1, Math.floor(windows.length / 8));
  for (const [from, to] of windows.filter((_, i) => i % step === 0 || i === windows.length - 1)) {
    const got = (await (await realFetch(`${FONE}/api/timing/session/${key}/positions?from=${from}&to=${to}`)).json()).cars;
    const want = {};
    const perCar = {};
    SAMPLES.forEach((s, i) => {
      for (const [num, e] of Object.entries(s.Entries)) (perCar[num] ||= []).push([t[i], e.X, e.Y, e.Status === 'OnTrack' && (e.X || e.Y)]);
    });
    for (const [num, pts] of Object.entries(perCar)) {
      const lo = pts.findIndex((p) => p[0] >= from);
      const i = Math.max(0, (lo < 0 ? pts.length : lo) - 1);
      const j = pts.filter((p) => p[0] <= to).length;
      const keep = pts.slice(i, j).filter((p) => p[3]).map((p) => [p[0], p[1], p[2]]);
      if (keep.length) want[num] = keep;
    }
    assert.deepEqual(Object.keys(got).sort(), Object.keys(want).sort(), 'cars served');
    for (const num of Object.keys(want)) {
      assert.equal(got[num].length, want[num].length, `car ${num} samples`);
      got[num].forEach((p, k) => {
        assert.ok(Math.abs(p[0] - want[num][k][0]) < 0.001, `car ${num} time`);
        assert.deepEqual(p.slice(1), want[num][k].slice(1), `car ${num} position`);
      });
    }
  }
}

const tests = [];
const test = (name, f) => tests.push([name, f]);

test('SSE + Authorization header: every sample relayed once, served back by fone', async () => {
  await mock('/_mock/reset', { session_key: 90001, tokens: [TOKEN] });
  const cpu = process.cpuUsage();
  run(90001);
  const st = await until('the session end', (s) => s.state === 'ended' && s.pending === 0);
  P.stop();
  const used = process.cpuUsage(cpu);
  // Upper bound (includes this harness): the real feed sends about one message per second.
  console.log(`   cpu: ${((used.user + used.system) / 1000 / st.messages).toFixed(2)} ms per Position.z message (${st.messages} messages, inflate + queue + POSTs)`);
  assert.equal(st.relayed, N);
  assert.equal(st.stored, N);
  assert.equal(st.mode, 'sse');
  assert.equal(st.connects, 1);
  assert.equal(st.lastError, null);
  assert.ok(st.posts >= 2, 'batched over time, not one POST');
  assertAllOnce();
  assertNoTokenToFone();
  const log = await mock('/_mock/log');
  const c = log.conns[0];
  assert.equal(log.conns.length, 1);
  assert.deepEqual([c.transport, c.presented, c.token, c.authorized, c.handshake], ['sse', 'header', TOKEN, true, true]);
  assert.deepEqual(c.subscribe, ['SessionInfo', 'SessionStatus', 'Position.z']);
  assert.ok(c.post_auth.length >= 2 && c.post_auth.every(Boolean), 'POSTs carry the same bearer');
  assert.equal(log.negotiates[0].token, TOKEN);
  assert.ok(!JSON.stringify(P.status()).includes(TOKEN), 'status() shows the token');
  await assertServed(90001);
});

test('a dropped connection reconnects; the keyframe and overlap it re-sends are not posted again', async () => {
  await mock('/_mock/reset', { session_key: 90002, tokens: [TOKEN], drop_after: 60, overlap: 5 });
  run(90002);
  const st = await until('the session end', (s) => s.state === 'ended' && s.pending === 0);
  P.stop();
  assert.equal(st.connects, 2);
  assert.equal(st.relayed, N);
  assert.ok(st.duplicates >= 20, `duplicates seen: ${st.duplicates}`);
  assertAllOnce();
  const log = await closedLog();
  assert.deepEqual(log.conns.map((c) => c.closed_by), ['server', 'client']);
  await assertServed(90002);
});

test('a refused token (401) is replaced by a fresh one from getToken', async () => {
  const stale = TOKEN.replace('dGVzdC1vbmx5', 'c3RhbGU');
  await mock('/_mock/reset', { session_key: 90003, tokens: [TOKEN], refuse: [stale] });
  const handed = [stale, TOKEN];
  let calls = 0;
  run(90003, { getToken: async () => handed[Math.min(calls++, 1)] });
  const st = await until('the session end', (s) => s.state === 'ended' && s.pending === 0);
  P.stop();
  assert.equal(calls, 2);
  assert.equal(st.relayed, N);
  const log = await mock('/_mock/log');
  assert.deepEqual(log.negotiates.map((n) => n.state), ['refused', 'ok']);
  assert.equal(st.lastError, 'F1 live timing: HTTP 401');
});

test('the same token refused again: waits (auth-failed), no reconnect storm', async () => {
  await mock('/_mock/reset', { session_key: 90009, tokens: [], refuse: [TOKEN] });
  run(90009);
  await until('auth-failed', (s) => s.state === 'auth-failed');
  await sleep(1500);
  P.stop();
  const st = P.status();
  const log = await mock('/_mock/log');
  assert.equal(log.negotiates.length, 1, 'one attempt with a known-bad token');
  assert.equal(st.relayed, 0);
});

test('header ignored on SSE: no positions within probation, the WebSocket (access_token) takes over', async () => {
  await mock('/_mock/reset', { session_key: 90004, tokens: [TOKEN], auth_sse: 'none', auth_ws: 'query' });
  run(90004);
  const st = await until('the session end', (s) => s.state === 'ended' && s.pending === 0);
  P.stop();
  assert.equal(st.mode, 'ws');
  assert.equal(st.verified, true);
  assert.equal(st.relayed, N);
  assertAllOnce();
  const log = await closedLog();
  assert.deepEqual(log.conns.map((c) => [c.transport, c.authorized, c.presented]), [['sse', false, null], ['ws', true, 'query']]);
  assert.equal(log.conns[0].closed_by, 'client');
});

test('no positions in either mode: no-positions, retried later, nothing posted', async () => {
  await mock('/_mock/reset', { session_key: 90005, tokens: [TOKEN], auth_sse: 'none', auth_ws: 'none' });
  run(90005);
  await until('no-positions', (s) => s.state === 'no-positions', 15000);
  let log = await mock('/_mock/log');
  assert.deepEqual(log.conns.map((c) => c.transport), ['sse', 'ws']);
  await sleep(FAST.authRetry + 1000);
  log = await mock('/_mock/log');
  P.stop();
  assert.ok(log.conns.length >= 3 && log.conns[2].transport === 'sse', 'tried again after authRetry');
  assert.equal(foneRequests.length, 0);
});

test('the feed is on another session: waits, looks again, posts nothing', async () => {
  await mock('/_mock/reset', { session_key: 11731, tokens: [TOKEN] });
  run(90006);
  const st = await until('waiting', (s) => s.state === 'waiting');
  await sleep(2500);
  P.stop();
  const log = await closedLog();
  assert.equal(st.feedSession, 11731);
  assert.ok(log.conns.length >= 2, 'looked again');
  assert.ok(log.conns.every((c) => c.closed_by === 'client'));
  assert.equal(P.status().samples, 0);
  assert.equal(foneRequests.length, 0);
});

test('positions stop mid-session while pings go on: reconnects and resumes', async () => {
  await mock('/_mock/reset', { session_key: 90007, tokens: [TOKEN], stall_after: 50 });
  run(90007);
  const st = await until('the session end', (s) => s.state === 'ended' && s.pending === 0);
  P.stop();
  assert.equal(st.connects, 2);
  assert.equal(st.relayed, N);
  assert.equal(st.lastError, 'car positions stopped mid-session');
  assertAllOnce();
});

test('fone server failing: batches retried in order, nothing lost or doubled', async () => {
  await mock('/_mock/reset', { session_key: 90008, tokens: [TOKEN], fail_fone: 4, rate: 20 });
  run(90008, { server: `${FLAKY}/flaky` });
  const st = await until('the session end', (s) => s.state === 'ended' && s.pending === 0);
  P.stop();
  assert.equal(st.postErrors, 4);
  assert.equal(st.relayed, N);
  assert.equal(st.stored, N);
  const log = await mock('/_mock/log');
  assert.deepEqual(log.fone.slice(0, 4).map((f) => f.status), [503, 503, 503, 503]);
  assert.ok(log.fone.every((f) => !f.leak && !f.authorization));
  await assertServed(90008);
});

test('fone answers "archived": the relay stops and closes the F1 connection', async () => {
  await mock('/_mock/reset', { session_key: 90011, tokens: [TOKEN], end: false, fone_archived: true });
  run(90011, { server: `${FLAKY}/flaky` });
  const st = await until('archived', (s) => s.state === 'archived');
  await sleep(1000);
  const log = await closedLog();
  P.stop();
  assert.equal(st.stored, 0);
  assert.equal(log.conns.length, 1, 'no reconnect after archived');
  assert.equal(log.conns[0].closed_by, 'client');
});

test('stop(): the connection closes, nothing more is sent, nothing keeps running', async () => {
  await mock('/_mock/reset', { session_key: 90010, tokens: [TOKEN], rate: 10, end: false });
  run(90010);
  await until('positions relayed', (s) => s.relayed > 0);
  P.stop();
  const at = Date.now();
  await sleep(1500);
  const log = await closedLog();
  assert.equal(P.status().state, 'stopped');
  assert.equal(log.conns.length, 1);
  assert.equal(log.conns[0].closed_by, 'client');
  assert.ok(foneRequests.every((r) => r.at <= at + 50), 'a POST after stop()');
  const before = log.conns[0].pings;
  await sleep(2000);
  assert.equal((await mock('/_mock/log')).conns[0].pings, before, 'still pinging after stop()');
});

(async () => {
  let failed = 0;
  const selected = tests.filter(([name]) => !process.env.ONLY || name.includes(process.env.ONLY));
  for (const [name, f] of selected) {
    try {
      await f();
      console.log('ok -', name);
    } catch (e) {
      failed++;
      P.stop();
      console.log('FAIL -', name, '\n', e);
    }
  }
  console.log(`${selected.length - failed}/${selected.length} passed`);
  process.exitCode = failed ? 1 : 0;
  // Nothing may keep the process alive once everything is stopped: no stray timer, socket or stream.
  setTimeout(() => { console.log('FAIL - handles still open after stop()'); process.exit(1); }, 8000).unref();
})();
