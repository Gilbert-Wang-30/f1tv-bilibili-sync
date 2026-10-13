// Live car positions (E3). F1 streams car positions (Position.z) only to signed-in F1 TV
// subscribers, so the fone server, which records everything else anonymously, can't get
// them. While a LIVE session is being watched this relays them from the viewer's own
// subscription: subscribe to Position.z on F1 live timing, inflate it, and POST batches to
// `{server}/api/timing/session/{key}/positions` every 2 s for the dashboard's track map.
//
// Runs in the offscreen document, which stays up as long as the commentary engine does
// (the background service worker gets suspended when idle). Offscreen documents have no
// chrome.cookies, so the token comes from the background through getToken(), fresh for
// every connection. It goes to livetiming.formula1.com and nowhere else: never to the fone
// server, never into status(), never logged.
//
// SignalR Core, JSON protocol. Two ways to present the token, tried in this order:
//   sse  negotiate + Server-Sent Events read through fetch(), `Authorization: Bearer` on
//        every request: the header scheme FastF1 uses against this server. A browser
//        WebSocket can't carry that header; fetch can.
//   ws   WebSocket without negotiate (formula1.com's own live page connects that way) and
//        the token as `access_token` in the URL, the way Microsoft's browser client sends it.
// Without an accepted token the server simply leaves Position.z out (no error: seen on
// 2026-10-09), so positions arriving is the only proof a mode works. One that brings none
// within `probation` while the session runs gives way to the other.
(function (root) {
  'use strict';

  const F1_BASE = 'https://livetiming.formula1.com/signalrcore';
  const RS = '\x1e'; // SignalR record separator: ends every JSON frame
  // SessionInfo and SessionStatus are tiny: they say which session the feed carries (F1
  // switches it on its own schedule, not the viewer's) and when that session is over.
  const TOPICS = ['SessionInfo', 'SessionStatus', 'Position.z'];
  const MODES = ['sse', 'ws'];
  const OVER = new Set(['Finalised', 'Ends']);
  // Positions stop a few minutes after the chequered flag: no positions then isn't a failure.
  const QUIET = new Set(['Finished', ...OVER]);
  const MAX_POST = 1000; // samples per POST (the server takes up to 5000)

  // Milliseconds. Tests shorten them through start({timing}).
  const TIMING = {
    tick: 2000, // POST interval; also how often the connection is checked
    maxAge: 120000, // samples the server hasn't taken by then are dropped, not sent late
    postTimeout: 10000,
    ping: 15000, // SignalR drops a client it hasn't heard from in 30 s
    silence: 45000, // the server pings every 15 s: this long without a frame = dead link
    probation: 20000, // positions come about once a second: none this long = token not taken
    stall: 60000, // positions stopped while the session runs: reconnect
    otherSession: 60000, // the feed is on another session: look again this often
    noToken: 60000, // signed out, login expired or refused: re-read the cookie this often
    authRetry: 300000, // retry a refused token, or both modes without positions, this late
    tokenTimeout: 5000,
    backoff: [1000, 2000, 5000, 10000, 30000, 60000],
  };

  const TEXT = {
    connecting: 'Connecting to F1 live timing…',
    reconnecting: 'Reconnecting to F1 live timing…',
    subscribed: 'Connected, waiting for car positions…',
    live: 'Relaying car positions',
    'no-token': 'Not signed in to F1 TV in this browser (or the login expired)',
    'auth-failed': 'F1 refused the F1 TV login: sign in again on f1tv.formula1.com',
    'no-positions': 'F1 sends no car positions to this login (needs F1 TV Access/Pro/Premium)',
    ended: 'Session over',
    archived: 'Session archived on the fone server: nothing left to relay',
    stopped: 'Stopped',
  };

  // ---- pure helpers (unit-tested) ------------------------------------------------

  // SignalR frames in one text message: one or more JSON objects, each ending in RS.
  function splitFrames(text) {
    const out = [];
    for (const part of String(text).split(RS)) {
      if (!part.trim()) continue;
      try {
        out.push(JSON.parse(part));
      } catch { /* not a frame: skip it, keep the rest */ }
    }
    return out;
  }

  // Server-Sent Events -> SignalR frames, fed text chunks as they arrive. A chunk can end
  // anywhere, even between the \r and \n of a line break, so normalise the joined buffer.
  function sseParser() {
    let buf = '';
    return (chunk) => {
      buf = (buf + chunk).replace(/\r\n/g, '\n');
      const events = buf.split('\n\n');
      buf = events.pop();
      const frames = [];
      for (const ev of events) {
        const data = ev.split('\n').filter((l) => l.startsWith('data:')).map((l) => l.slice(5).replace(/^ /, ''));
        frames.push(...splitFrames(data.join('\n')));
      }
      return frames;
    };
  }

  // '2026-10-09T12:34:17.2631795Z' -> microseconds since the epoch (exact: < 2^53), NaN if
  // malformed. F1 writes 1 to 7 fraction digits; Date.parse alone would cut to milliseconds.
  function isoMicros(s) {
    const m = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(?:Z|[+-]00:?00)?$/.exec(String(s).trim());
    if (!m) return NaN;
    return Date.parse(m[1] + 'Z') * 1000 + Number(((m[2] || '') + '000000').slice(0, 6));
  }

  // Position.z payload: base64 of raw-deflated JSON. DecompressionStream inflates natively.
  async function inflateZ(payload) {
    if (payload && typeof payload === 'object') return payload; // already decoded
    const bin = atob(payload);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
    return JSON.parse(await new Response(stream).text());
  }

  // Numbers only: Number(null) is 0, and one car without coordinates would make the server
  // reject the whole sample.
  const num = (v) => (typeof v === 'number' || (typeof v === 'string' && v.trim()) ? Number(v) : NaN);

  // The fields the server stores; anything else F1 may add isn't forwarded.
  function cleanEntries(entries) {
    const out = {};
    for (const [car, e] of Object.entries(entries || {})) {
      const x = num(e?.X), y = num(e?.Y), z = num(e?.Z);
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      out[car] = { X: Math.round(x), Y: Math.round(y), Z: Number.isFinite(z) ? Math.round(z) : 0, Status: String(e.Status || '') };
    }
    return out;
  }

  // Samples waiting for the fone server, oldest first. Dedupe is a watermark on the sample
  // time: the feed is time-ordered, and what a reconnect re-sends (the keyframe, overlap) is
  // never newer than what we already have. The server applies the same rule on its side.
  function newQueue() {
    return { items: [], newest: -Infinity, newestTs: null, dupes: 0, dropped: 0, bad: 0 };
  }

  function enqueue(q, position, now) {
    let added = 0;
    for (const p of Array.isArray(position) ? position : []) {
      const t = isoMicros(p?.Timestamp);
      if (Number.isNaN(t)) { q.bad++; continue; }
      if (t <= q.newest) { q.dupes++; continue; }
      const e = cleanEntries(p.Entries);
      if (!Object.keys(e).length) { q.bad++; continue; }
      q.items.push({ t, ts: p.Timestamp, e, rx: now });
      q.newest = t;
      q.newestTs = p.Timestamp;
      added++;
    }
    return added;
  }

  // The next POST: what's pending (oldest first, capped) once samples too old to be worth
  // sending are gone. Items leave the queue only when the server has them (settle).
  function nextBatch(q, now, maxAge) {
    let old = 0;
    while (old < q.items.length && now - q.items[old].rx > maxAge) old++;
    if (old) {
      q.items.splice(0, old);
      q.dropped += old;
    }
    return q.items.slice(0, MAX_POST);
  }

  // Removes a sent batch (by sample time, so a batch that aged out meanwhile is harmless).
  function settle(q, batch) {
    const last = batch.length ? batch[batch.length - 1].t : -Infinity;
    let n = 0;
    while (n < q.items.length && q.items[n].t <= last) n++;
    q.items.splice(0, n);
    return n;
  }

  // What to do after a POST answered with this status.
  function postOutcome(status) {
    if (status >= 200 && status < 300) return 'ok';
    if (status >= 500 || status === 408 || status === 425 || status === 429) return 'retry';
    return 'drop'; // a 4xx won't change by resending the same batch
  }

  const jitter = (ms) => Math.round(ms * (0.8 + Math.random() * 0.4));
  const backoff = (list, fails) => list[Math.min(Math.max(fails, 1), list.length) - 1];

  // Error text for status(): defensively strips anything token-shaped (JWTs, access_token=).
  function scrub(text) {
    return String(text).slice(0, 200)
      .replace(/eyJ[\w-]*\.[\w-]+\.[\w-]*/g, '‹token›')
      .replace(/access_token=[^&\s]*/g, 'access_token=‹token›');
  }

  // ---- transports ------------------------------------------------------------------
  // Both call h.open() once the link is up, h.frame(frame) per SignalR frame and h.close(info)
  // once, never synchronously from the opener. info: {opened, status?, reason?}.

  function openSse(base, token, h) {
    const ac = new AbortController();
    // credentials: AWS's load balancer pins a connection to one server with the AWSALBCORS
    // cookie negotiate sets; a request without it may reach a server that doesn't know the id.
    const req = (url, init = {}) => fetch(url, {
      ...init,
      headers: { Authorization: `Bearer ${token}`, ...init.headers },
      credentials: 'include',
      cache: 'no-store',
      signal: ac.signal,
    });
    let url = null;
    let opened = false;
    let done = false;
    const finish = (info) => {
      if (done) return;
      done = true;
      ac.abort();
      h.close(info);
    };
    (async () => {
      const neg = await req(`${base}/negotiate?negotiateVersion=1`, { method: 'POST' });
      if (!neg.ok) return finish({ opened, status: neg.status });
      const n = await neg.json();
      const id = n.connectionToken || n.connectionId; // negotiate v1 answers both; the token is the id to use
      if (typeof id !== 'string' || !id) return finish({ opened, reason: 'negotiate gave no connection id' });
      url = `${base}?id=${encodeURIComponent(id)}`;
      const res = await req(url, { headers: { Accept: 'text/event-stream' } });
      if (!res.ok || !res.body) return finish({ opened, status: res.status });
      opened = true;
      h.open();
      const parse = sseParser();
      const dec = new TextDecoder();
      const reader = res.body.getReader();
      while (!done) {
        const { value, done: eof } = await reader.read();
        if (eof) break;
        for (const f of parse(dec.decode(value, { stream: true }))) {
          if (done) break;
          h.frame(f);
        }
      }
      finish({ opened, reason: 'stream ended' });
    })().catch(() => finish({ opened, reason: 'network error' })); // never the error's text: it can hold the URL
    return {
      send(msg) {
        if (!opened || done) return;
        req(url, { method: 'POST', body: JSON.stringify(msg) + RS, headers: { 'Content-Type': 'text/plain;charset=UTF-8' } })
          .then((res) => { if (!res.ok) finish({ opened, status: res.status }); }) // 404: the server forgot us
          .catch(() => { /* the stream read notices a dead link */ });
      },
      close() { finish({ opened, reason: 'closed' }); },
    };
  }

  function openWs(base, token, h) {
    let ws = null;
    let opened = false;
    let done = false;
    const finish = (info) => {
      if (done) return;
      done = true;
      if (ws) {
        ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
        try { ws.close(1000); } catch { /* already closing */ }
      }
      h.close(info);
    };
    try {
      ws = new WebSocket(`${base.replace(/^http/, 'ws')}?access_token=${encodeURIComponent(token)}`);
    } catch {
      setTimeout(() => finish({ opened, reason: 'WebSocket refused the URL' }), 0);
      return { send() {}, close() { finish({ opened, reason: 'closed' }); } };
    }
    ws.onopen = () => { opened = true; h.open(); };
    ws.onmessage = (e) => { if (typeof e.data === 'string') for (const f of splitFrames(e.data)) if (!done) h.frame(f); };
    // A refused upgrade (401) looks like any other failure here: code 1006, nothing else.
    ws.onclose = (e) => finish({ opened, reason: `closed (${e.code})` });
    return {
      send(msg) { if (opened && !done && ws.readyState === 1) ws.send(JSON.stringify(msg) + RS); },
      close() { finish({ opened, reason: 'closed' }); },
    };
  }

  // ---- the relay ----------------------------------------------------------------------

  let R = null; // the running relay
  let last = null; // the last one stopped, for status()

  function setState(r, state, text) {
    r.state = state;
    r.text = text || TEXT[state] || state;
  }

  function setError(r, text) {
    r.lastError = scrub(text);
    r.lastErrorAt = Date.now();
  }

  // Sleep that stop() (or an archived answer) cuts short, so no timer outlives the relay.
  function nap(r, ms) {
    return new Promise((resolve) => {
      r.wake = () => {
        clearTimeout(r.napTimer);
        r.wake = null;
        resolve();
      };
      r.napTimer = setTimeout(r.wake, ms);
    });
  }

  async function freshToken(r) {
    let timer;
    try {
      const t = await Promise.race([
        Promise.resolve().then(() => r.getToken()),
        new Promise((resolve) => { timer = setTimeout(resolve, r.T.tokenTimeout, null); }),
      ]);
      return typeof t === 'string' && t.trim() ? t.trim() : null;
    } catch {
      return null; // background asleep or gone: same as no token, try again later
    } finally {
      clearTimeout(timer);
    }
  }

  // Connect, reconnect, change modes, wait: until stopped or the session is over.
  async function loop(r) {
    let fails = 0;
    while (!r.stopped) {
      if (r.archived) return setState(r, 'archived');
      const token = await freshToken(r);
      if (r.stopped) return;
      if (!token) {
        setState(r, 'no-token');
        await nap(r, r.T.noToken);
        continue;
      }
      if (token === r.refused && Date.now() - r.refusedAt < r.T.authRetry) {
        setState(r, 'auth-failed'); // a new login replaces the cookie: look again in a minute
        await nap(r, r.T.noToken);
        continue;
      }
      const res = await connect(r, r.mode, token);
      if (r.stopped) return;
      if (r.archived) return setState(r, 'archived');
      if (res.why === 'ended') return setState(r, 'ended');
      if (res.why === 'other-session') {
        fails = 0;
        setState(r, 'waiting', `F1's live timing is on session ${r.feedSession ?? '?'}, not ${r.sessionKey}`);
        await nap(r, r.T.otherSession);
      } else if (res.why === 'unauthorized') {
        r.refused = token; // compared, never shown; dropped when the relay stops
        r.refusedAt = Date.now();
      } else if (res.why === 'no-positions') {
        r.tried.add(r.mode);
        r.verified = false;
        const next = MODES.find((m) => !r.tried.has(m));
        if (next) {
          r.mode = next;
        } else {
          r.tried.clear();
          r.mode = MODES[0];
          setState(r, 'no-positions');
          await nap(r, r.T.authRetry);
        }
      } else { // dropped, stalled, failed
        fails = res.positions ? 1 : fails + 1;
        // Can't connect at all this way (a proxy blocking SSE or WebSockets?): try the other.
        if (!res.opened && fails >= 3 && !r.verified) {
          r.mode = MODES[(MODES.indexOf(r.mode) + 1) % MODES.length];
          fails = 0;
        }
        setState(r, 'reconnecting');
        await nap(r, jitter(backoff(r.T.backoff, fails)));
      }
    }
  }

  // One connection. Resolves {why, opened, positions} when it is over; why is one of
  // ended | other-session | unauthorized | no-positions | dropped | stalled | failed | stopped.
  function connect(r, mode, token) {
    return new Promise((resolve) => {
      const now = Date.now();
      const c = { mode, opened: false, ours: false, positions: 0, expectSince: 0, lastFrame: now, lastPing: now, lastPos: 0 };
      let link = null;
      c.end = (why, detail) => {
        if (c.done) return;
        c.done = true;
        if (r.conn === c) r.conn = null;
        if (detail) setError(r, detail);
        if (link) link.close();
        resolve({ why, opened: c.opened, positions: c.positions });
      };
      c.send = (msg) => { if (link) link.send(msg); };
      r.conn = c;
      r.connects++;
      setState(r, r.connects > 1 ? 'reconnecting' : 'connecting');
      link = (mode === 'ws' ? openWs : openSse)(r.base, token, {
        open() {
          c.opened = true;
          c.lastFrame = Date.now();
          c.send({ protocol: 'json', version: 1 });
        },
        frame(f) {
          c.lastFrame = r.lastMessageAt = Date.now();
          onFrame(r, c, f);
        },
        close(info) {
          const refused = info.status === 401 || info.status === 403;
          const why = refused ? 'unauthorized' : info.opened ? 'dropped' : 'failed';
          c.end(why, info.status ? `F1 live timing: HTTP ${info.status}` : info.reason && `F1 live timing: ${info.reason}`);
        },
      });
    });
  }

  function onFrame(r, c, f) {
    if (f.type === undefined) { // handshake answer
      if (f.error) return c.end('failed', `F1 live timing handshake: ${f.error}`);
      return c.send({ type: 1, invocationId: '1', target: 'Subscribe', arguments: [TOPICS] });
    }
    if (f.type === 3 && f.invocationId === '1') return onSubscribed(r, c, f);
    if (f.type === 1 && f.target === 'feed') {
      const [topic, payload] = f.arguments || [];
      if (topic === 'Position.z') onPositions(r, c, payload);
      else if (topic === 'SessionStatus') onStatus(r, c, payload);
      else if (topic === 'SessionInfo' && payload?.Key != null && Number(payload.Key) !== r.sessionKey) {
        r.feedSession = payload.Key; // the feed moved on to the next session
        c.end('other-session');
      }
      return;
    }
    if (f.type === 7) { // the server closes the connection
      const refused = /401|403|unauthori|forbidden|expired|token/i.test(String(f.error || ''));
      c.end(refused ? 'unauthorized' : 'dropped', `F1 live timing closed the connection${f.error ? `: ${f.error}` : ''}`);
    }
  }

  // Subscribe's answer: the current state of each topic.
  function onSubscribed(r, c, f) {
    if (f.error) return c.end('failed', `F1 live timing subscribe: ${f.error}`);
    const res = f.result || {};
    r.feedSession = res.SessionInfo?.Key ?? null;
    if (r.feedSession == null || Number(r.feedSession) !== r.sessionKey) return c.end('other-session');
    c.ours = true;
    r.feedStatus = null;
    c.expectSince = Date.now();
    if (onStatus(r, c, res.SessionStatus)) return;
    if (res['Position.z'] != null) onPositions(r, c, res['Position.z']);
    if (!c.positions) setState(r, 'subscribed');
  }

  // Returns true once the session is over (the connection is ended).
  function onStatus(r, c, st) {
    const s = st?.Status;
    if (!s) return false;
    r.feedStatus = s;
    if (OVER.has(s)) {
      c.end('ended');
      return true;
    }
    if (QUIET.has(s)) c.expectSince = 0;
    else if (!c.expectSince) c.expectSince = Date.now();
    return false;
  }

  function onPositions(r, c, payload) {
    if (!c.ours) return;
    c.positions++;
    c.lastPos = r.lastPositionAt = Date.now();
    r.messages++;
    if (r.state !== 'live') {
      r.verified = true; // this mode's token presentation works: keep it
      r.tried.clear();
      setState(r, 'live');
    }
    // One at a time, so samples are queued in the order they arrived.
    r.decoding = r.decoding.then(async () => {
      try {
        const z = await inflateZ(payload);
        r.samples += enqueue(r.queue, z?.Position, Date.now());
      } catch {
        r.queue.bad++;
      }
    });
  }

  // Every tick: keep the connection honest, then post what's queued.
  function tick(r) {
    const now = Date.now();
    const c = r.conn;
    if (c) watch(r, c, now);
    flush(r, now, false);
  }

  function watch(r, c, now) {
    if (now - c.lastFrame > r.T.silence) return c.end(c.opened ? 'dropped' : 'failed', 'F1 live timing went silent');
    if (c.opened && now - c.lastPing >= r.T.ping) {
      c.lastPing = now;
      c.send({ type: 6 });
    }
    if (!c.ours || !c.expectSince) return;
    if (!c.positions && now - c.expectSince > r.T.probation) return c.end('no-positions');
    if (c.positions && now - Math.max(c.lastPos, c.expectSince) > r.T.stall) c.end('stalled', 'car positions stopped mid-session');
  }

  async function flush(r, now, final) {
    if (!final && (r.posting || now < r.postAfter)) return;
    const batch = nextBatch(r.queue, now, r.T.maxAge);
    if (!batch.length) return;
    r.posting = true;
    let outcome = 'retry';
    let what = 'fone server unreachable';
    try {
      const res = await fetch(`${r.server}/api/timing/session/${r.sessionKey}/positions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: batch.map((s) => ({ Timestamp: s.ts, Entries: s.e })) }),
        // Chrome sends cookies on extension requests to any host it may access, unless told
        // not to: fone needs none, so nothing but the positions goes there.
        credentials: 'omit',
        signal: AbortSignal.timeout(r.T.postTimeout),
      });
      outcome = postOutcome(res.status);
      what = `fone server: HTTP ${res.status}`;
      if (outcome === 'ok') {
        const body = await res.json().catch(() => ({}));
        r.relayed += settle(r.queue, batch);
        r.stored += Number(body.stored) || 0;
        r.posts++;
        r.postFails = 0;
        // The archive replaced the live capture: the session is long over.
        if (body.reason === 'archived' && !r.archived) {
          r.archived = true;
          if (r.conn) r.conn.end('stopped');
          if (r.wake) r.wake();
        }
      }
    } catch { /* timeout or network: retry */ }
    r.posting = false;
    if (outcome === 'ok') return;
    r.postErrors++;
    r.postFails++;
    setError(r, what);
    if (outcome === 'drop') r.queue.dropped += settle(r.queue, batch);
    r.postAfter = Date.now() + Math.min(30000, r.T.tick * 2 ** (r.postFails - 1));
  }

  // ---- API --------------------------------------------------------------------------

  const origin = (url) => { try { return new URL(url).origin; } catch { return null; } };

  // Starts relaying for a LIVE session; calling it again with the same session and server
  // changes nothing (callers may call it on every refresh), anything else restarts.
  // opts: {sessionKey, server, getToken: async () => token|null}; tests also pass
  // {f1Base} (a mock of F1's SignalR URL) and {timing} (shorter intervals).
  function start(opts = {}) {
    const sessionKey = Number(opts.sessionKey);
    const server = String(opts.server || '').replace(/\/+$/, '');
    const base = String(opts.f1Base || F1_BASE).replace(/\/+$/, '');
    if (R && R.sessionKey === sessionKey && R.server === server) {
      if (typeof opts.getToken === 'function') R.getToken = opts.getToken;
      return status();
    }
    stop();
    const bad = !(sessionKey > 0) || !/^https?:\/\//.test(server) || typeof opts.getToken !== 'function' ? 'bad arguments'
      // The token goes wherever f1Base points: never to the fone server, whatever a caller mixes up.
      : !origin(base) || origin(base) === origin(server) ? 'f1Base must not be the fone server' : null;
    if (bad) {
      last = { state: 'error', text: `Not started: ${bad}`, lastError: bad };
      return status();
    }
    const r = R = {
      sessionKey, server, base, getToken: opts.getToken,
      T: { ...TIMING, ...opts.timing },
      stopped: false, archived: false, state: 'connecting', text: TEXT.connecting,
      mode: MODES[0], verified: false, tried: new Set(), refused: null, refusedAt: 0,
      conn: null, queue: newQueue(), decoding: Promise.resolve(),
      feedSession: null, feedStatus: null, startedAt: Date.now(), lastMessageAt: null, lastPositionAt: null,
      connects: 0, messages: 0, samples: 0, relayed: 0, stored: 0, posts: 0, postErrors: 0,
      posting: false, postAfter: 0, postFails: 0, lastError: null, lastErrorAt: null,
      wake: null, napTimer: null, timer: null,
    };
    r.timer = setInterval(() => tick(r), r.T.tick);
    loop(r);
    return status();
  }

  function stop() {
    const r = R;
    if (!r) return;
    R = null;
    r.stopped = true;
    clearInterval(r.timer);
    if (r.wake) r.wake();
    if (r.conn) r.conn.end('stopped');
    r.refused = null;
    r.getToken = null;
    setState(r, 'stopped');
    last = r;
    flush(r, Date.now(), true); // the last couple of seconds, best effort
  }

  // A snapshot for the panel/tests: counters and state, never the token.
  function status() {
    const r = R || last;
    if (!r) return { state: 'idle', text: 'Not started' };
    if (!r.queue) return { ...r };
    const q = r.queue;
    return {
      state: r.state, text: r.text, sessionKey: r.sessionKey, server: r.server,
      mode: r.conn ? r.conn.mode : r.mode, verified: r.verified,
      feedSession: r.feedSession, feedStatus: r.feedStatus,
      startedAt: r.startedAt, lastMessageAt: r.lastMessageAt, lastPositionAt: r.lastPositionAt, newestSample: q.newestTs,
      connects: r.connects, messages: r.messages, samples: r.samples, duplicates: q.dupes, bad: q.bad,
      relayed: r.relayed, stored: r.stored, pending: q.items.length, dropped: q.dropped,
      posts: r.posts, postErrors: r.postErrors, lastError: r.lastError, lastErrorAt: r.lastErrorAt,
    };
  }

  root.FoneLivePos = {
    start, stop, status,
    _internals: { splitFrames, sseParser, isoMicros, inflateZ, cleanEntries, newQueue, enqueue, nextBatch, settle,
      postOutcome, backoff, scrub, TIMING, TOPICS, F1_BASE },
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
