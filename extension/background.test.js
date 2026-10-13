// node extension/background.test.js
// The self-update logic in background.js, run against a fake chrome.* API and clock.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, 'background.js'), 'utf8');

function boot({ running, served, storage = {} }) {
  const env = { now: 1_000_000, served, reloads: 0, refreshed: [], listeners: {}, intervals: [] };
  const local = { ...storage };
  const on = (name) => ({ addListener: (f) => { (env.listeners[name] ||= []).push(f); } });
  const chrome = {
    runtime: {
      id: 'fone', getURL: (p) => `chrome-extension://fone/${p}`,
      onMessage: on('message'), onInstalled: on('installed'), reload: () => { env.reloads++; },
      getContexts: async () => [], sendMessage: async () => {}, openOptionsPage() {},
    },
    tabs: {
      onRemoved: on('removed'), sendMessage: async () => {},
      query: async () => [{ id: 7 }], reload: (id) => env.refreshed.push(id),
    },
    action: { onClicked: on('action') },
    cookies: { getAll: async () => env.cookies || [] },
    alarms: { create() {}, onAlarm: on('alarm') },
    offscreen: { createDocument: async () => {} },
    storage: {
      sync: { get: async (d) => d },
      local: {
        get: async (keys) => {
          const ks = typeof keys === 'string' ? [keys] : keys;
          return Object.fromEntries(ks.filter((k) => k in local).map((k) => [k, local[k]]));
        },
        set: async (o) => { Object.assign(local, o); },
        remove: async (k) => { delete local[k]; },
      },
    },
  };
  const ctx = {
    chrome, console, URL, AbortSignal,
    Date: { now: () => env.now },
    setInterval: (f) => env.intervals.push(f),
    fetch: async (url) => ({ ok: env.served != null, json: async () => ({ build: env.served }), url }),
    importScripts: (f) => {
      if (f === 'f1token.js') return vm.runInContext(fs.readFileSync(path.join(__dirname, f), 'utf8'), ctx);
      if (f === 'local.js') { ctx.self.FONE_LOCAL = { servers: ['http://fone.test:8788'] }; return; } // a published build's defaults
      if (f !== 'build.js') return; // dashproxy.js: not under test here
      if (running == null) throw new Error(`NetworkError: ${f}`);
      ctx.self.FONE_BUILD = running;
    },
  };
  ctx.self = ctx;
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  env.local = local;
  env.check = async () => { await env.intervals[0](); await new Promise((r) => setImmediate(r)); };
  env.status = (tab, playing) => env.listeners.message.forEach((f) => f({ target: 'background', type: 'status', tab, status: { playing } }, {}));
  return env;
}

const tests = [];
const test = (name, f) => tests.push([name, f]);

test('up to date: never reloads', async () => {
  const e = boot({ running: 'b1', served: 'b1' });
  for (let i = 0; i < 5; i++) { e.now += 10000; await e.check(); }
  assert.equal(e.reloads, 0);
});

test('loaded from the repo (no build.js): no self-update at all', async () => {
  const e = boot({ running: null, served: 'b2' });
  for (let i = 0; i < 5; i++) { e.now += 10000; await e.check(); }
  assert.equal(e.reloads, 0);
});

test('a new build reloads once it has been served for 75 s (a Mac sync run), then refreshes F1 TV tabs', async () => {
  const e = boot({ running: 'b1', served: 'b2' });
  await e.check();
  assert.equal(e.reloads, 0, 'first sight only notes it');
  for (let i = 0; i < 7; i++) { e.now += 10000; await e.check(); }
  assert.equal(e.reloads, 0, 'not settled yet at 70 s');
  e.now += 10000; await e.check();
  assert.equal(e.reloads, 1);
  assert.equal(e.local.refreshF1tv, true);
  assert.deepEqual({ ...e.local.lastReload }, { build: 'b2', at: e.now });
  // the new build's background starts: refreshes F1 TV tabs once
  const e2 = boot({ running: 'b2', served: 'b2', storage: e.local });
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(e2.refreshed, [7]);
  assert.equal(e2.local.refreshF1tv, false);
  e2.now = e.now + 60000; await e2.check();
  assert.equal(e2.reloads, 0);
});

test('waits while a tab is playing, reloads once it stops', async () => {
  const e = boot({ running: 'b1', served: 'b2' });
  await e.check();
  for (let i = 0; i < 6; i++) { e.now += 10000; e.status(3, true); await e.check(); }
  assert.equal(e.reloads, 0);
  e.status(3, false);
  e.now += 16000; await e.check();
  assert.equal(e.reloads, 1);
});

test('a reload that came back on the old build does not loop; retries after 10 min', async () => {
  const e = boot({ running: 'b1', served: 'b2' });
  await e.check(); e.now += 75000; await e.check();
  assert.equal(e.reloads, 1);
  // the share served stale files: the reloaded background still runs b1
  const e2 = boot({ running: 'b1', served: 'b2', storage: e.local });
  e2.now = e.now;
  for (let i = 0; i < 30; i++) { e2.now += 10000; await e2.check(); }
  assert.equal(e2.reloads, 0, 'no reload loop within 10 min');
  e2.now += 5 * 60000; await e2.check();
  assert.equal(e2.reloads, 1, 'tries again later');
});

test('server unreachable: nothing happens', async () => {
  const e = boot({ running: 'b1', served: null });
  for (let i = 0; i < 5; i++) { e.now += 10000; await e.check(); }
  assert.equal(e.reloads, 0);
});


test('the F1 TV token goes to the offscreen document only', async () => {
  const e = boot({ running: 'b1', served: 'b1' });
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
  const jwt = `${b64({ alg: 'RS256' })}.${b64({ exp: Date.now() / 1000 + 3600 })}.c2ln`;
  e.cookies = [{ value: encodeURIComponent(JSON.stringify({ data: { subscriptionToken: jwt } })) }];
  const ask = (sender) => new Promise((resolve) => {
    let pending = false;
    for (const f of e.listeners.message) if (f({ target: 'background', type: 'f1-token' }, sender, resolve) === true) pending = true;
    if (!pending) resolve('no answer');
  });
  assert.equal(await ask({ id: 'fone', url: 'chrome-extension://fone/offscreen.html' }), jwt);
  assert.equal(await ask({ id: 'fone', url: 'https://f1tv.formula1.com/detail/1', tab: { id: 3 } }), null);
  assert.equal(await ask({ id: 'fone', url: 'chrome-extension://fone/options.html' }), null);
  assert.equal(await ask({ id: 'other', url: 'chrome-extension://other/offscreen.html' }), null);
});

(async () => {
  let failed = 0;
  for (const [name, f] of tests) {
    try { await f(); console.log(`ok - ${name}`); } catch (err) { failed++; console.log(`FAIL - ${name}\n  ${err.message}`); }
  }
  console.log(`${tests.length - failed} passed${failed ? `, ${failed} failed` : ''}`);
  process.exit(failed ? 1 : 0);
})();
