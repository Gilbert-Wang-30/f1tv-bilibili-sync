// Runs in F1 TV's own JS world (manifest "world": "MAIN"): the only place the
// Tiledmedia player's objects are reachable. Content scripts live in an isolated world
// where custom-element getters like currentContentTime don't exist.
//
// F1 TV plays through Tiledmedia (WASM core). Its <video>.currentTime is a
// synthetic MSE clock that barely moves on seeks; the real replay position is
// <tiledmedia-view>.currentContentTime.currentPosition (ms). We publish it to
// content scripts as a 'fone-tm' CustomEvent with a JSON string detail, together with
// the playback speed and the audio tracks (fields r and a, see below).
//
// content.js steers us with 'fone-cmd' CustomEvents (JSON string detail):
//   {cmd: 'rate', value: 2}                    speed for replays; live always plays at 1x
//   {cmd: 'audio', prefer: 'fx'|'mute'|'none', cid}   F1 TV's audio: its no-commentary track
//                                              (a track switch), muted (no switch), untouched
// It re-sends both every 2 s; after LEASE ms without them we stop steering and undo
// our speed change (extension reloaded or removed).
// layout.js forwards the stats dashboard's driver onboards (see "onboard cameras"):
//   {cmd: 'onboards', slots: [{x, y, w, h, channelId}]}   at most 2, re-sent every 2 s; a
//                                              lease of its own (OB_LEASE), [] = none
(() => {
  'use strict';
  if (window.__fonePage) return;
  window.__fonePage = true;

  const FIELDS = ['eventType', 'currentPosition', 'contentDuration', 'seekLowerBound', 'seekUpperBound',
    'bufferLowerBound', 'bufferUpperBound', 'isAtLiveEdge', 'isWallclockTimeSourceBased'];
  const pick = (o) => (o ? Object.fromEntries(FIELDS.map((k) => [k, o[k]])) : null);
  const videoOf = (view) => [...view.querySelectorAll(':scope > video')].find((v) => v.style.display !== 'none') || null;
  const LEASE = 10000;
  const errText = (e) => String(e?.message || e).slice(0, 160);
  // The SDK's promises are answered by its WASM core; one that never answers must not
  // wedge our steering (busy flags) forever.
  const NO_ANSWER = 'the player did not answer';
  const within = (p, ms) => Promise.race([p, new Promise((_, no) => setTimeout(() => no(new Error(NO_ANSWER)), ms))]);
  // An SDK call as a promise, also when it throws before returning one.
  const call = (f) => { try { return Promise.resolve(f()); } catch (e) { return Promise.reject(e); } };

  // ---- the player's SDK: one call of ours at a time, every one logged ----------------------
  // F1 TV's player died six times in a minute at the start of a replay (2026-10-11), each time
  // right after fone fired a speed and an audio-track call into a player that said 'playing'
  // but had no video yet; the core then answered "no user interaction possible in current state
  // (internally-stopped)" while player.playerState still read 'playing' (the state enum has no
  // stopped value). So: one lock for the speed, audio and onboard calls (they used to overlap),
  // no call into a player that has died (until F1 TV makes a new one), the fatal error itself
  // caught by a listener, and a log of what was asked when, for the diagnostics.
  const SDK = {
    busy: null, // {name, t0}: our call in flight
    doneAt: 0, // when the last one answered
    log: [], // the last calls: {t, name, args, ps, h, nv, ms, ok | err, gi (gotInterrupted)}
    sent: 0, // how many of them the report carried already (the rest go out next)
    dead: new WeakSet(), // players the core stopped under (or F1 TV destroyed)
    seen: new WeakSet(), // players we listen to (fatal errors, state changes)
    player: null, // the main view's player, as last seen
    pid: null,
    fatal: [], // {t, code, msg, p, ps, h, nv, busy, last}: what the core reported
    states: [], // {t, s}: the player's state changes, from its listener (else our polling)
    f1tv: null, // {t0, cfg}: an applyDesiredState of F1 TV's own in flight
    inCall: false,
  };
  const LOG_MAX = 40;
  const DEAD = /internally-stopped|has been stopped|not registered|player is stopped/i;

  function note(kind, e) {
    const ring = SDK[kind];
    ring.push(e);
    if (ring.length > (kind === 'log' ? LOG_MAX : 12)) ring.shift();
  }

  const stateOf = (player) => { try { return player?.playerState ?? null; } catch { return null; } };
  function around(player) {
    const vid = view ? videoOf(view) : null;
    return { ps: stateOf(player), h: vid ? vid.getAttribute('handleID') : null, nv: document.querySelectorAll('tiledmedia-view').length,
      p: view?.currentContentTime?.currentPosition ?? null };
  }

  // Run one SDK call of ours: logged with the player's state before it and how it ended; a
  // core that answers "stopped" marks the player dead. `fn` makes the call; `ms` bounds it.
  async function sdkCall(name, args, player, fn, ms) {
    const t0 = Date.now();
    const e = { t: t0, name, args, ...around(player) };
    delete e.p;
    SDK.inCall = true;
    let p;
    try { p = call(fn); } finally { SDK.inCall = false; }
    try {
      const r = await within(p, ms);
      e.ms = Date.now() - t0;
      e.ok = true;
      if (r && typeof r === 'object' && 'gotInterrupted' in r) e.gi = !!r.gotInterrupted;
      return r;
    } catch (err) {
      e.ms = Date.now() - t0;
      e.err = errText(err);
      if (DEAD.test(e.err)) SDK.dead.add(player);
      throw err;
    } finally {
      note('log', e);
      SDK.doneAt = Date.now();
    }
  }
  const isDead = (player) => !player || player.destroyed === true || SDK.dead.has(player);
  const lock = (name) => { if (SDK.busy) return false; SDK.busy = { name, t0: Date.now() }; return true; };
  const unlock = () => { SDK.busy = null; };

  // F1 TV's own applyDesiredState calls, seen by wrapping the method on the instance (a
  // transparent pass-through: same arguments, the same promise back). We then know while one
  // is pending (the content load, a channel switch) and the log shows it next to ours.
  function watchPlayer(player) {
    if (!player || SDK.seen.has(player)) return;
    SDK.seen.add(player);
    try {
      const orig = player.applyDesiredState;
      if (typeof orig === 'function' && !Object.prototype.hasOwnProperty.call(player, 'applyDesiredState')) {
        player.applyDesiredState = function (...args) {
          const ours = SDK.inCall;
          const t0 = Date.now();
          const p = orig.apply(this, args);
          if (!ours) {
            const cfg = args[0];
            const tag = cfg == null ? 'none' : `${cfg.timingType ?? cfg.constructor?.name ?? 'config'}`;
            const e = { t: t0, name: 'f1tv.apply', args: tag, ...around(player) };
            delete e.p;
            SDK.f1tv = { t0, cfg: tag };
            Promise.resolve(p).then((r) => { e.ok = true; if (r && typeof r === 'object' && 'gotInterrupted' in r) e.gi = !!r.gotInterrupted; },
              (err) => { e.err = errText(err); }).finally(() => { e.ms = Date.now() - t0; note('log', e); if (SDK.f1tv?.t0 === t0) SDK.f1tv = null; });
          }
          return p;
        };
      }
    } catch { /* a non-writable method: no view of F1 TV's calls */ }
    try { // what the core says when it gives up: the decisive datum for the crash
      player.addPlayerFatalErrorListeners?.((err) => {
        try {
          note('fatal', { t: Date.now(), code: err?.code ?? null, msg: String(err?.tmmessage ?? err?.message ?? err).slice(0, 200),
            ...around(player), busy: SDK.busy ? `${SDK.busy.name} ${Date.now() - SDK.busy.t0}ms` : null, f1tv: SDK.f1tv?.cfg ?? null,
            last: SDK.log.slice(-5).map((x) => `${x.name}(${x.args}) ${x.ok ? 'ok' : x.err ? 'err' : '…'} ${x.t - Date.now()}ms`) });
          SDK.dead.add(player);
        } catch { /* never throw into the SDK */ }
      });
    } catch { /* older SDK */ }
    try {
      const Cfg = window.Tiledmedia?.StateChangedListenerConfig;
      if (Cfg && typeof player.addStateChangedListener === 'function') {
        player.addStateChangedListener(new Cfg((s) => {
          try { note('states', { t: Date.now(), s: typeof s === 'object' && s ? (s.state ?? s.newState ?? JSON.stringify(s).slice(0, 40)) : String(s) }); } catch { /* */ }
        }));
      }
    } catch { /* the listener's shape differs: our polling in emit() still sees the states */ }
  }

  function mainView() {
    // F1 TV binds its control bar to the main view — but only once, so in multi-view
    // the bound view can be swapped to another feed and stop showing a picture.
    const bound = document.querySelector('tiledmedia-ui-view')?.internalView;
    if (bound && bound.isConnected && bound.associatedPlayer && videoOf(bound)
        && bound.currentContentTime?.eventType !== 'unknown') return bound;
    const area = (x) => {
      const r = videoOf(x)?.getBoundingClientRect();
      return r ? r.width * r.height : 0;
    };
    // (never one of our onboard views, data-fone-ob: they play other feeds of the same player)
    return [...document.querySelectorAll('tiledmedia-view:not(.tmw-ghost):not([data-tmw-empty-view]):not([data-fone-ob])')]
      .filter((x) => x.associatedPlayer && Number.isFinite(x.currentContentTime?.currentPosition))
      .sort((a, b) => area(b) - area(a) || (a.dataset.type === 'obc') - (b.dataset.type === 'obc'))[0] || null;
  }

  let view = null;
  let sub = null; // {view, id}: a ViewListenerID belongs to the view that issued it
  let gen = 0;
  let lastListenerAt = 0;
  let lastEvent = null; // eventType of the main view's latest content-time report

  // ---- commands from content.js ---------------------------------------------------

  const want = { rate: null, fx: false, audio: 'none', cid: null, at: 0 };
  document.addEventListener('fone-cmd', (e) => {
    let m;
    try { m = JSON.parse(e.detail); } catch { return; }
    if (!m || typeof m !== 'object') return;
    if (m.cmd === 'onboards') { obCommand(m.slots); return; } // its own lease: not the speed/audio one
    want.at = Date.now();
    if (m.cmd === 'rate') {
      const r = Number(m.value);
      // Player.changePlaybackRate: "multiplier float from 0.5 to 2.0"
      if (!Number.isFinite(r)) return;
      const next = Math.min(2, Math.max(0.5, r));
      // A new request (or the same speed pressed again: force) starts over: no waiting out a
      // 30 s backoff from earlier failures, and the old error text goes.
      if (next !== want.rate || m.force) Object.assign(R, { pending: null, fails: 0, lastTry: Math.min(R.lastTry, Date.now() - 500), err: null });
      want.rate = next;
    } else if (m.cmd === 'audio') {
      const mode = m.prefer === 'fx' || m.prefer === 'mute' ? m.prefer : 'none';
      if (mode !== want.audio) { A.override.clear(); A.tries = 0; A.v++; } // an explicit request: start over
      want.audio = mode;
      want.fx = mode === 'fx';
      if (m.cid != null) want.cid = String(m.cid);
    }
  });
  // content.js re-sends every 2 s, but a hidden tab's timers run once a minute (Chrome): there
  // the lease holds as long as the tab is hidden, and runs from when it was shown again (as
  // the onboards' does). In 2026-10-11's data page.js undid the speed in a hidden, paused tab
  // every minute, and the picture dipped to 1x on every resume.
  let shownAt = 0;
  document.addEventListener('visibilitychange', () => { if (!document.hidden) shownAt = Date.now(); });
  const leased = () => want.at > 0 && (document.hidden || Date.now() - Math.max(want.at, shownAt) < LEASE);

  // ---- playback speed ---------------------------------------------------------------
  // Player.changePlaybackRate(m) -> core -> every MSE handle: handle.playbackRate = m and
  // <video>.playbackRate = handle.playbackRate * handle.syncMultiplier. The view-sync PID
  // rewrites that product on every step (MSEHandle.syncViews), so writing <video>.playbackRate
  // directly doesn't stick. A new MSE handle (content load, feed switch) starts at 1.

  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
  // The sync multiplier's usual range. It goes far beyond it at times (×0.75 .. ×1.25 after a
  // seek or a content load), so a speed read off the element is only ever a guess.
  const TRIM = 0.03;
  const snap = (r) => SPEEDS.find((s) => Math.abs(r / s - 1) < TRIM) ?? r;
  const R = { lastTry: 0, fails: 0, err: null, mine: false, target: null, pending: null, handle: null, handleAt: 0, lastAsk: null,
    suspect: false }; // the core's idea of the rate may differ from the picture's (a rebuilt handle, an ask that never applied)

  function handleOf(player, vid) {
    try {
      for (const h of player?.mseManager?.mseHandles?.values() ?? []) if (h?.videoElement === vid) return h;
    } catch { /* SDK internals moved */ }
    return null;
  }

  // The speed the SDK was asked to play at (without the sync trim): exact off the video's MSE
  // handle, else guessed from the element's rate. -> {rate, exact} or null
  function sdkRate(player, vid) {
    const h = handleOf(player, vid);
    if (h && Number.isFinite(h.playbackRate) && h.playbackRate > 0) return { rate: h.playbackRate, exact: true };
    return vid && vid.playbackRate > 0 ? { rate: snap(vid.playbackRate), exact: false } : null;
  }

  function rateTarget() {
    if (!want.rate || !leased()) return R.mine ? 1 : null; // hands off, after undoing our change
    if (lastEvent === 'vod') return want.rate;
    if (lastEvent === 'live' || lastEvent === 'finished-live') return 1;
    return null; // loading: wait until we know what this is
  }

  async function steerRate(player, vid) {
    R.target = rateTarget();
    if (SDK.busy || R.target == null || !vid || typeof player?.changePlaybackRate !== 'function') return;
    // Not while F1 TV's own applyDesiredState (content load, channel switch) is running, nor
    // into a player the core has stopped under (F1 TV rebuilds it; a new one resets us).
    if (player.playerState === 'loading' || player.playerState === 'idle' || isDead(player) || SDK.f1tv) return;
    const target = R.target;
    // No MSE handle under the main video yet (the player says 'playing' while it is still
    // starting): wait. Every player death seen so far came right after calls made then. (An SDK
    // whose internals moved, so that no handle is ever found: steer by the element's rate as
    // before, rather than not at all.)
    const h = handleOf(player, vid);
    if (!h && player?.mseManager?.mseHandles) return;
    if (h && h !== R.handle) {
      // A new MSE handle (a feed switch, a view added or selected: the player rebuilds them,
      // a player rebuilt after sleep). It plays at 1 while the core may still believe it plays
      // at target, and the picture dips to 1x until we ask again. Ask at once (the commentary
      // follows a dip that lasts), with nothing waited out.
      const rebuilt = R.handle != null;
      R.handle = h;
      R.handleAt = Date.now();
      if (rebuilt && target !== 1) Object.assign(R, { pending: null, lastTry: 0, fails: 0, suspect: true });
    }
    const cur = sdkRate(player, vid)?.rate;
    if (cur != null && Math.abs(cur - target) < 0.005) {
      if (target === 1) R.mine = false;
      Object.assign(R, { pending: null, fails: 0, err: null, suspect: false });
      return;
    }
    const t = Date.now();
    const p = R.pending;
    if (p?.target === target) {
      // Accepted: the core schedules the change (timeKeeper.schedulePlaybackRateChange),
      // perhaps only once playing again. Give it 3 s of playback; 6 s right after a seek or a
      // new handle, where the core was seen to take 3-4 s (asking again is harmless, but the
      // failure count would climb into the slow gates).
      if (player.playerState !== 'playing') p.at = t;
      const slow = t - R.handleAt < 10000 || t - A.seekAt < 10000;
      if (t - p.at < (slow ? 6000 : 3000)) return;
      R.pending = null; // accepted but never applied (or reset since): retry, more slowly
      R.fails++;
      R.suspect = true;
      R.err = `the player did not switch to ×${target}`;
    }
    // e.g. "no active content to set playback rate for" while a content loads
    if (t - R.lastTry < (R.fails >= 6 ? 30000 : R.fails >= 3 ? 5000 : 1000)) return;
    if (!lock('rate')) return;
    R.lastTry = t;
    // The core keeps its own idea of the rate across MSE handles, and a request for the rate
    // it believes it already plays at changes nothing (JS handles start at 1 after a rebuild,
    // so the picture then stays at 1x). The old cure, asking a neighbour (×1.98) first, could
    // itself become the believed rate after a second rebuild: on 2026-10-11 every retry re-asked
    // 1.48 for 7 minutes while the picture ran at 1x. With the picture at 1x anyway, asking 1
    // first costs nothing and is a real change for a core that believes target, then target is
    // a real change for any core: the belief and the picture agree again whatever it was.
    const reset = R.suspect && target !== 1 && cur != null && Math.abs(cur - 1) < 0.005;
    try {
      if (reset) {
        await sdkCall('rate', 1, player, () => player.changePlaybackRate(1), 10000);
        R.lastAsk = 1;
      }
      await sdkCall('rate', target, player, () => player.changePlaybackRate(target), 10000);
      R.lastAsk = target;
      if (target !== 1) R.mine = true;
      R.pending = { target, at: Date.now() };
    } catch (e) {
      if (isDead(player)) R.err = 'the player stopped; waiting for F1 TV to restart it';
      else { R.fails++; R.err = errText(e); }
    } finally {
      unlock();
    }
  }

  // ---- audio track ------------------------------------------------------------------
  // player.currentAudioTrack is the playing AudioStream ({_uid, _feedID, _trackID}, set by
  // the core). The choices are contentItem.crossFeedAudioTracks (what F1 TV's audio menu
  // lists); AudioTrack.name / .language are Promises (worker round trips). Selecting:
  // player.desiredAudioTrackSelection = new Tiledmedia.AudioTrackSelection(false, track);
  // await player.applyDesiredState() — exactly what F1 TV's menu does.
  // F1 TV picks the channel's audioDefault (e.g. "1033.eng": F1 LIVE English) on every
  // content load and channel switch, and only half-remembers a picked language, so
  // "Track FX" (language fx, INTERNATIONAL feed) has to be picked again each time.

  const FX_LANGS = new Set(['fx', 'cfx', 'zxx']); // zxx: "no linguistic content"
  const RADIO_LANGS = new Set(['tea', 'obc', 'tur', 'tr', 'teamradio']); // F1 TV: "Team Radio"
  const COMM_LANGS = new Set(('eng en deu ger de fra fre fr spa es nld dut nl por pt jpn ja jp ita it pol pl rus ru '
    + 'zho chi zh cmn yue kor ko ara ar hun hu ces cze cs swe sv dan da nor nob no fin fi ell gre el heb he hin hi '
    + 'tha th vie vi ind id msa may ms ron rum ro ukr uk slk slo sk slv sl hrv hr srp sr bul bg cat ca').split(' '));
  const NO_COMM = /\b(no|without|w\/o|non)[\s-]*comm?entar|comm?entar\w*[\s-]*(free|off)\b|ohne\s+kommentar|sans\s+commentaire|sin\s+comentario|clean\s+(feed|audio)|international\s+sound|world\s*feed\s+sound/i;
  const FX_NAME = /(^|[^a-z])(track\s*fx|s?fx|track\s*sound|natural\s*sound|nat\s*sound|ambient|ambience|effects|circuit\s*sound|stadium\s*sound)([^a-z]|$)/i;
  const COMM_NAME = /english|deutsch|german|fran[cç]ais|french|espa[nñ]ol|spanish|nederlands|dutch|portugu[eê]s|portuguese|japanese|日本語|italian|polski|commentary|kommentar|commentaire|comentario|commento|f1\s*live/i;

  // -> [kind, score]: kind fx|commentary|radio|other; score > 0 = a commentary-free choice
  function classify(name, lang) {
    const n = String(name ?? '').trim();
    const l = String(lang ?? '').trim().toLowerCase().split(/[-_]/)[0];
    if (NO_COMM.test(n)) return ['fx', 100];
    const fxName = FX_NAME.test(n) && !COMM_NAME.test(n);
    if (FX_LANGS.has(l)) return COMM_NAME.test(n) ? ['other', 0] : ['fx', fxName ? 95 : 90];
    if (RADIO_LANGS.has(l) || /team\s*radio/i.test(n)) return ['radio', 0];
    if (COMM_LANGS.has(l)) return fxName ? ['fx', 60] : ['commentary', 0];
    if (fxName) return ['fx', 80];
    if (COMM_NAME.test(n)) return ['commentary', 0];
    if (l === 'und') return ['fx', 30]; // F1 TV names und/cfx/fx tracks "FX"
    return ['other', 0];
  }

  const keyOf = (x) => (x ? `${x._uid},${x._feedID},${x._trackID}` : null);
  const A = {
    lists: new Map(), // content item uid -> {list: [{t, key, name, lang, kind, score}] | null while loading}
    extra: new Map(), // track key -> the same, for a playing track missing from those lists
    videoKey: null, switchAt: 0, curKey: null, curUid: null, curAt: 0, readySince: 0, seekAt: 0,
    auto: null, // {key, at}: the track we selected for the current video
    change: null, // the last change of the playing track: {at, fromUid, inputAt, ours, judged}
    tries: 0, err: null, inputAt: 0,
    due: 0, // since when a track switch is wanted and not done (the onboards wait for it)
    muted: null, // the player we muted (mode 'mute'), to unmute it when the mode goes
    mutedKey: null, // ...and for which video (F1 TV unmutes on a content load)
    override: new Set(), // content ids where you picked another track yourself
    v: 0, sentV: -1, sentAt: 0,
  };
  // F1 TV's audio menu needs a click or a key: a change without one isn't yours.
  for (const ev of ['pointerdown', 'keydown', 'touchstart']) {
    document.addEventListener(ev, (e) => { if (e.isTrusted) A.inputAt = Date.now(); }, { capture: true, passive: true });
  }
  // A seek or a reload of the main video: no track switch for a while after it.
  for (const ev of ['seeking', 'emptied', 'loadstart']) {
    document.addEventListener(ev, (e) => { if (view && e.target === videoOf(view)) A.seekAt = Date.now(); }, true);
  }

  async function loadTracks(cur) {
    let ci = null;
    try { ci = await cur.parentContentItem; } catch { /* fall back to the view's content */ }
    if (!ci && view?.currentVideo) ci = await view.currentVideo.parentContentItem;
    if (!ci) throw new Error('no content item');
    let tracks = await ci.crossFeedAudioTracks;
    if (!tracks?.length) tracks = (ci.feeds || []).flatMap((f) => f.audioTracks || []);
    return Promise.all(tracks.map(async (t) => {
      const [name, lang] = await Promise.all([t.name, t.language]);
      const [kind, score] = classify(name, lang);
      return { t, key: keyOf(t), name: String(name ?? ''), lang: String(lang ?? ''), kind, score };
    }));
  }

  function tracksFor(cur) {
    const uid = cur._uid;
    const e = A.lists.get(uid);
    if (e) return e.list;
    if (A.lists.size > 8) A.lists.clear();
    A.lists.set(uid, { list: null });
    loadTracks(cur).then((list) => {
      A.lists.set(uid, { list });
      A.v++;
    }, (err) => {
      A.err = `tracks: ${errText(err)}`;
      setTimeout(() => A.lists.delete(uid), 3000);
    });
    return null;
  }

  // The playing track when it isn't among the content item's cross-feed tracks.
  function trackInfo(cur, ck) {
    if (A.extra.has(ck)) return A.extra.get(ck); // null while loading or unknown
    if (A.extra.size > 50) A.extra.clear();
    A.extra.set(ck, null);
    Promise.resolve(cur.parentAudioTrack).then(async (t) => {
      const [name, lang] = await Promise.all([t.name, t.language]);
      const [kind, score] = classify(name, lang);
      A.extra.set(ck, { t, key: ck, name: String(name ?? ''), lang: String(lang ?? ''), kind, score });
      A.v++;
    }).catch(() => {});
    return null;
  }

  // The best commentary-free track: one on the feed whose picture is playing when it has a
  // good one (a track on another feed means the core pulling audio from a second feed, the
  // heavier switch), else the best anywhere.
  const SAME_FEED_MIN = 60;
  function bestFx(list, feed) {
    const ok = list.filter((x) => x.score > 0).sort((a, b) => b.score - a.score || (b.t._feedID === feed) - (a.t._feedID === feed));
    return ok.find((x) => x.t._feedID === feed && x.score >= SAME_FEED_MIN) || ok[0] || null;
  }

  // Mode 'mute': F1 TV's own sound off (setAudioIsMuted, the call its mute button makes: a
  // plain command, no pipeline change) once per video, since F1 TV unmutes on a content load;
  // back on when the mode goes, the lease ends, or the player changes under us.
  async function steerMute(player, vk) {
    const on = want.audio === 'mute' && leased();
    if (on && A.muted === player && A.mutedKey === vk) return;
    if (!on && !A.muted) return;
    if (!on && A.muted !== player) { A.muted = null; A.mutedKey = null; return; } // gone with its player
    if (isDead(player) || !lock('mute')) return;
    try {
      await sdkCall(on ? 'mute' : 'unmute', '', player, () => player.setAudioIsMuted(on), 5000);
      A.muted = on ? player : null;
      A.mutedKey = on ? vk : null;
      A.err = null;
    } catch (e) {
      A.err = errText(e);
    } finally {
      unlock();
      A.v++;
    }
  }

  async function steerAudio(player) {
    const now = Date.now();
    const vq = view.currentVideo;
    const vk = `${vq?._uid},${vq?._feedID},${view.dataset.channelId ?? ''}`;
    if (vk !== A.videoKey) { // content load, feed/channel switch, or another main view
      Object.assign(A, { videoKey: vk, switchAt: now, auto: null, change: null, tries: 0, readySince: 0 });
      A.v++;
    }
    const cur = player.currentAudioTrack || null;
    const ck = keyOf(cur);
    if (ck !== A.curKey) {
      // Who changed it? Us (applying, or the track we asked for), else F1 TV or you.
      A.change = { at: now, fromUid: A.curUid, inputAt: A.inputAt, ours: SDK.busy?.name === 'audio' || A.auto?.key === ck };
      Object.assign(A, { curKey: ck, curUid: cur?._uid ?? null, curAt: now });
      A.v++;
    }
    if (want.audio === 'mute' || A.muted) { A.due = 0; await steerMute(player, vk); if (want.audio === 'mute') { A.cur = cur ? A.cur : null; return; } }
    A.cur = null;
    if (!cur) return;
    const list = tracksFor(cur);
    if (!list) return;
    A.cur = list.find((x) => x.key === ck) || trackInfo(cur, ck);
    A.best = bestFx(list, vq?._feedID);
    if (!want.fx || !leased() || (want.cid && A.override.has(want.cid))) { A.due = 0; return; }

    const c = A.change;
    if (c && !c.ours && !c.judged) {
      // F1 TV's audio menu: a click or key just before, same content item, no switch around it.
      const yours = c.fromUid != null && c.fromUid === cur._uid && c.at - A.switchAt > 1500
        && c.inputAt > 0 && c.at - c.inputAt < 15000 && A.cur?.kind !== 'fx';
      if (!yours) c.judged = true; // F1 TV (content load, channel switch) or the player
      else if (now - c.at < 1500) return; // a feed switch may still show itself (and reset all this)
      else {
        c.judged = true;
        if (want.cid) A.override.add(want.cid); // don't fight you for this video
        A.v++;
        return;
      }
    }
    if (A.cur?.kind !== 'commentary' || !A.best || A.tries >= 3) { A.due = 0; return; } // already commentary-free (or unknown)
    if (!A.due) A.due = now;
    // Only into a settled player: a track switch is a pipeline change in the core, and the
    // deaths came on calls into one still starting. So: playing (or paused, visible) for a
    // while, with a video and an MSE handle under the main view and data in it, nothing seeking,
    // no handle change and no F1 TV applyDesiredState just now, none of our calls in flight.
    const st = player.playerState;
    if (st !== 'playing' && st !== 'paused') { A.readySince = 0; return; }
    if (!A.readySince) A.readySince = now;
    const vid = videoOf(view);
    const settled = now - A.readySince >= AUDIO_SETTLE && now - A.switchAt >= AUDIO_SETTLE && now - A.curAt >= 600
      && vid && vid.readyState >= 3 && !vid.seeking && handleOf(player, vid) && now - R.handleAt >= AUDIO_QUIET
      && now - A.seekAt >= AUDIO_QUIET && !SDK.f1tv && now - SDK.doneAt >= 1000 && !isDead(player);
    if (!settled) return;
    const Sel = window.Tiledmedia?.AudioTrackSelection;
    if (!Sel || typeof player.applyDesiredState !== 'function') { A.err = 'AudioTrackSelection unavailable'; A.due = 0; return; }
    if (!lock('audio')) return;
    A.tries++;
    const best = A.best;
    A.auto = { key: best.key, at: now };
    try {
      player.desiredAudioTrackSelection = new Sel(false, best.t);
      await sdkCall('audio', `${best.name || best.lang}${best.t._feedID === vq?._feedID ? '' : ' (other feed)'}`, player,
        () => player.applyDesiredState(), 15000); // no config: no seek (SDK docs)
      A.err = null;
    } catch (e) {
      A.err = errText(e);
      A.auto = null;
    } finally {
      unlock();
      A.v++;
    }
  }
  const AUDIO_SETTLE = 3000; // ms playing before we switch the track
  const AUDIO_QUIET = 2000; // ms since a seek / a new handle

  function audioReport() {
    const out = {
      v: A.v,
      cur: A.cur ? A.cur.name || A.cur.lang || '?' : null,
      lang: A.cur?.lang ?? null,
      kind: A.cur?.kind ?? null,
      auto: !!(A.auto && A.curKey === A.auto.key),
      manual: !!(want.cid && A.override.has(want.cid)),
      fx: A.best ? A.best.name || A.best.lang : null,
      on: want.fx,
      mode: want.audio,
      muted: !!A.muted, // F1 TV's sound off by us
      due: A.due ? Date.now() - A.due : 0, // ms a switch has been wanted and not done
      err: A.err,
    };
    const t = Date.now();
    if (A.v !== A.sentV || t - A.sentAt > 2000) {
      const list = (A.curKey && A.lists.get(A.curUid)?.list) || [];
      const seen = new Set();
      out.tracks = [];
      for (const x of list) {
        const id = `${x.name}|${x.lang}`;
        if (seen.has(id) || out.tracks.length >= 60) continue;
        seen.add(id);
        out.tracks.push([x.name, x.lang, x.kind]);
      }
      A.sentV = A.v;
      A.sentAt = t;
    }
    return out;
  }

  // ---- onboard cameras ----------------------------------------------------------------
  // The stats dashboard (an iframe of layout.js) shows up to two driver onboards in boxes of its
  // own; layout.js forwards where: {cmd: 'onboards', slots: [{x, y, w, h, channelId}]}, CSS px,
  // viewport-relative, channelId null = an empty slot. Each slot gets a fixed box above F1 TV's
  // player layer (div.video-grid, z 100) and the dashboard (z 99), holding a <tiledmedia-view> of
  // our own that is added to F1 TV's player and shows the feed with that channel_id from the
  // content the main view plays. That is how F1 TV's own multi-view adds a feed:
  //   view.desiredVideoSelection = new Tiledmedia.VideoSelection(feed); await player.applyDesiredState()
  // Every applyDesiredState (F1 TV's own too) sends all views' selections to the core, so ours
  // survive F1 TV's calls, and the core keeps all views of a player in sync. Picture only: the
  // audio selection is the player's, and we never set it (nor the main view's selection).
  //  * another driver: the same element, a new selection
  //  * F1 TV's main content changes (its channel switch adds a content item to the player):
  //    the same element, the same channel's feed in the new content
  //  * a new player (F1 TV rebuilt it; Player.destroy() takes every view off the old one, ours
  //    too): a new element, with none of the old player's state (addView refuses a view still
  //    on another player, so one that is gets taken off that one first)
  //  * a content without that channel: nothing shown, no retries until the content changes
  //  * no command for OB_LEASE ms while the tab is visible (extension reloaded, dashboard gone):
  //    all removed
  // One SDK call of ours at a time, each within() a timeout and never awaited by the 250 ms loop;
  // none while F1 TV's own applyDesiredState runs, nor in a hidden tab with the video paused;
  // failures back off (2 s .. 30 s). A view leaves its player as: selection null, apply,
  // removeView; its box leaves the page at once.

  const OB_MAX = 4; // the dashboard fits up to 3 onboard slots; one spare
  const OB_LEASE = 10000;
  const OB_BACKOFF = [2000, 5000, 10000, 30000];
  const OB_BOX = 'position:fixed;margin:0;padding:0;border:0;box-sizing:border-box;overflow:hidden;border-radius:10px;'
    + 'background:#000;pointer-events:none;';
  const OB_VIEW = 'display:block;position:absolute;left:0;top:0;width:100%;height:100%;margin:0;padding:0;';
  const OB = {
    want: [], // the last command: per slot {x, y, w, h, ch}, or null (empty)
    at: 0, // when it came
    shownAt: 0, // when the tab was last shown: a hidden tab's timers (the dashboard's too) are throttled
    slots: [], // per slot index, see newSlot()
    zombies: [], // our views on their way out of a player: {el, player, tries, retryAt}
    retryAt: 0,
    chans: null, // the main content's channels: {key, player, map: Map(channel id -> {feed, idx}) | null, n, busy, fails, retryAt, err}
    z: 101,
    zAt: 0,
    err: null,
  };

  document.addEventListener('visibilitychange', () => { if (!document.hidden) OB.shownAt = Date.now(); });
  // The commands keep coming (every 2 s). Chrome throttles a hidden tab's timers to once a minute,
  // so the lease only runs while the tab is visible (and from when it was shown again).
  const obLeased = (now) => OB.at > 0 && (document.hidden || now - Math.max(OB.at, OB.shownAt) < OB_LEASE);

  function obCommand(slots) {
    if (!Array.isArray(slots)) return;
    OB.at = Date.now();
    OB.want = slots.slice(0, OB_MAX).map((s) => {
      const r = s && typeof s === 'object' ? { x: s.x, y: s.y, w: s.w, h: s.h, ch: s.channelId } : null;
      const ok = r && [r.x, r.y, r.w, r.h].every((v) => typeof v === 'number' && Number.isFinite(v)) && r.w > 0 && r.h > 0;
      return ok && Number.isInteger(r.ch) ? r : null;
    });
  }

  const backoff = (n) => OB_BACKOFF[Math.min(n, OB_BACKOFF.length) - 1];
  const feedKey = (f) => (f ? f.uid ?? `${f._uid},${f._feedID}` : null);
  const sameFeed = (a, b) => !!a && !!b && (a === b || (typeof a.equals === 'function' ? a.equals(b) : feedKey(a) === feedKey(b)));
  const newSlot = (i) => ({
    i, ch: null, rect: null, box: null, css: '', el: null, adding: false, dead: false,
    uid: null, // the feed our view is set to
    ok: null, // ...which the player confirmed (its applyDesiredState answered)
    waitPlay: false, noPic: 0, state: 'loading', step: null, err: null, fails: 0, retryAt: 0,
  });

  function fail(s, e) {
    s.fails++;
    s.err = errText(e);
    s.retryAt = Date.now() + backoff(s.fails);
  }

  // F1 TV's current player: the main view's.
  function obPlayer() {
    for (const v of [view, document.querySelector('tiledmedia-ui-view')?.internalView]) {
      const p = v?.associatedPlayer;
      if (p && !p.destroyed && typeof p.addView === 'function' && typeof p.applyDesiredState === 'function') return p;
    }
    return null;
  }

  // What F1 TV's views of this player show: the main one's, else any (never ours).
  function mainSelection(player) {
    const of = (v) => (v && !v.hasAttribute('data-fone-ob') && v.associatedPlayer === player
      ? v.desiredVideoSelection?.selection ?? null : null);
    let s = of(view) || of(document.querySelector('tiledmedia-ui-view')?.internalView);
    if (s) return s;
    for (const v of document.querySelectorAll('tiledmedia-view:not([data-fone-ob])')) if ((s = of(v))) return s;
    return null;
  }

  // channel_id -> feed of the content item `sel` belongs to, exactly as F1 TV's multi-view finds a
  // channel's feed: JSON.parse(await feed.feedMetadata).channel_id over contentItem.feeds.
  async function loadChannels(sel) {
    const ci = Array.isArray(sel.feeds) ? sel : await within(call(() => sel.parentContentItem), 10000);
    const feeds = Array.isArray(ci?.feeds) ? ci.feeds : [];
    if (!feeds.length) throw new Error('the content has no feeds');
    const ids = await within(Promise.all(feeds.map(async (f) => {
      try { return Number(JSON.parse(await f.feedMetadata)?.channel_id); } catch { return NaN; }
    })), 10000);
    const map = new Map();
    ids.forEach((ch, idx) => { if (Number.isInteger(ch) && !map.has(ch)) map.set(ch, { feed: feeds[idx], idx }); });
    return { map, n: feeds.length };
  }

  // The main content's channels (cached per content item); null while F1 TV has no content.
  function channelsOf(player) {
    const sel = mainSelection(player);
    if (!sel) return null;
    const key = sel._uid != null && sel._uid !== '' ? `uid:${sel._uid}` : sel; // ContentItem/Feed/VideoQuality._uid
    let c = OB.chans;
    if (!c || c.key !== key || c.player !== player) {
      c = OB.chans = { key, player, map: null, n: 0, busy: false, fails: 0, retryAt: 0, err: null };
    }
    if (!c.map && !c.busy && Date.now() >= c.retryAt) {
      c.busy = true;
      loadChannels(sel).then((r) => Object.assign(c, { map: r.map, n: r.n, err: null, fails: 0 }), (e) => {
        c.fails++;
        c.err = `channels: ${errText(e)}`;
        c.retryAt = Date.now() + backoff(c.fails);
      }).finally(() => { c.busy = false; });
    }
    return c;
  }

  // Above F1 TV's player layer and the dashboard just below it (layout.js: the grid's z - 1).
  function obZ() {
    const t = Date.now();
    if (t - OB.zAt > 2000) {
      OB.zAt = t;
      const g = document.querySelector('div.video-grid');
      const z = g ? parseInt(getComputedStyle(g).zIndex, 10) : NaN;
      OB.z = Math.max(101, Number.isFinite(z) ? z + 1 : 0);
    }
    return OB.z;
  }

  // The slot's box: in the page (outside F1 TV's #app, like layout.js's iframe) only while it
  // holds one of our views.
  function placeBox(s) {
    if (s.dead) return;
    if (!s.el) { s.box?.remove(); return; }
    if (!s.box) {
      s.box = document.createElement('div');
      s.box.setAttribute('data-fone-ob-box', String(s.i));
    }
    const r = s.rect;
    const css = `${OB_BOX}left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;z-index:${obZ()};`;
    if (s.css !== css) { s.box.style.cssText = css; s.css = css; }
    if (s.el.parentNode !== s.box) s.box.appendChild(s.el);
    if (!s.box.isConnected) document.documentElement.appendChild(s.box);
  }

  // Our view out of its slot: off the screen at once, off its player when our turn comes.
  function retire(s) {
    const el = s.el;
    Object.assign(s, { el: null, uid: null, ok: null, waitPlay: false, noPic: 0 });
    if (el) {
      el.remove();
      if (el.associatedPlayer) OB.zombies.push({ el, player: el.associatedPlayer, tries: 0, retryAt: 0 });
    }
    placeBox(s);
  }

  function dropSlot(s) {
    retire(s);
    s.dead = true;
    s.box?.remove();
  }

  async function attach(s, player) {
    const el = document.createElement('tiledmedia-view');
    el.setAttribute('data-fone-ob', String(s.i));
    el.style.cssText = OB_VIEW;
    s.el = el;
    s.adding = true;
    placeBox(s); // in the page and sized before the player measures it ("may be displayed in low quality")
    let raw;
    const p = sdkCall('ob.add', s.ch, player, () => { raw = call(() => player.addView(el)); return raw; }, 10000);
    // Added after all (late, after we gave up on the answer, or after the slot moved on): take it
    // off again. (The SDK's own promise: sdkCall's gives up after 10 s.)
    raw?.then(() => { if (s.el !== el && el.associatedPlayer) OB.zombies.push({ el, player: el.associatedPlayer, tries: 0, retryAt: 0 }); }, () => {});
    try {
      await p;
      if (el.associatedPlayer !== player) throw new Error('the view did not join the player');
      if (s.el === el) Object.assign(s, { fails: 0, err: null });
    } catch (e) {
      if (s.el === el) {
        fail(s, e);
        s.el = null;
        el.remove();
        placeBox(s);
      }
    } finally {
      s.adding = false;
    }
  }

  async function applyFor(s, player, el, uid) {
    // no config: no seek. A late answer counts too (the core applies it whenever it gets to it).
    let raw;
    const p = sdkCall('ob.apply', s.ch, player, () => { raw = call(() => player.applyDesiredState()); return raw; }, 15000);
    const mine = () => s.el === el && s.uid === uid;
    raw?.then(() => { if (mine()) Object.assign(s, { ok: uid, err: null, fails: 0, retryAt: 0, waitPlay: false }); }, () => {});
    try {
      await p;
    } catch (e) {
      if (!mine()) return;
      fail(s, e);
      if (e?.message === NO_ANSWER) {
        // A hidden tab with the video paused sat on it for 45 s and more: again once it plays.
        s.waitPlay = true;
      } else {
        // Refused: take our selection back at once, so that F1 TV's own applyDesiredState (which
        // sends every view's selection) never trips over it, and start again with a new view.
        try { el.desiredVideoSelection = null; } catch { /* gone with its player */ }
        retire(s);
      }
    }
  }

  async function select(s, player, target) {
    const el = s.el;
    const uid = feedKey(target.feed);
    try {
      const Sel = window.Tiledmedia?.VideoSelection;
      if (typeof Sel !== 'function') throw new Error('Tiledmedia.VideoSelection unavailable');
      const sel = new Sel(target.feed);
      try { el.zDistance = Math.max(1, target.idx); } catch { /* as F1 TV's multi-view does: the feed's index */ }
      el.desiredVideoSelection = sel;
    } catch (e) {
      fail(s, e);
      return;
    }
    Object.assign(s, { uid, ok: null, waitPlay: false, noPic: 0 });
    await applyFor(s, player, el, uid);
  }

  async function bury(z, current) {
    const { el, player } = z;
    const forget = () => { const k = OB.zombies.indexOf(z); if (k >= 0) OB.zombies.splice(k, 1); };
    if (el.associatedPlayer !== player || player.destroyed) { forget(); return; } // off already (Player.destroy())
    try {
      if (el.desiredVideoSelection) {
        el.desiredVideoSelection = null;
        // (F1 TV's views keep theirs: applyDesiredState refuses only when every view has none)
        if (player === current) await sdkCall('ob.apply', 'bury', player, () => player.applyDesiredState(), 15000).catch(() => {});
      }
      await sdkCall('ob.remove', '', player, () => player.removeView(el), 10000);
      forget();
      OB.err = null;
    } catch (e) {
      OB.err = `remove: ${errText(e)}`;
      z.tries++;
      z.retryAt = Date.now() + backoff(z.tries);
      if (z.tries >= 4) forget(); // left with no selection: it shows nothing, and goes with its player
    }
  }

  // The slot's state for the report, and the SDK call it needs next (a function), if any.
  function slotOp(s, player, chans, ready, now) {
    const set = (state, step = null) => { s.state = state; s.step = step; return null; };
    const wait = (step) => set(s.err ? 'error' : 'loading', step);
    if (s.adding) return set('loading', 'attach');
    // F1 TV replaced its player, or took our view off it (Player.destroy()). A moment without any
    // player (F1 TV rebuilding it) leaves a view that is still on one alone.
    if (s.el && s.el.associatedPlayer !== player && (player || !s.el.associatedPlayer)) retire(s);
    if (!player) return wait('player');
    if (!chans) return set('loading', 'content'); // F1 TV is between contents
    if (!chans.map) return set(chans.err ? 'error' : 'loading', 'channels');
    const target = chans.map.get(s.ch);
    if (!target) {
      if (s.el) retire(s);
      s.err = null;
      return set('none');
    }
    if (now < s.retryAt) return set('error', s.step);
    if (!s.el) {
      wait('attach');
      return ready ? () => attach(s, player) : null;
    }
    if (!sameFeed(s.el.desiredVideoSelection?.selection, target.feed)) {
      wait('select');
      return ready ? () => select(s, player, target) : null;
    }
    if (s.ok !== s.uid) { // selected; the player hasn't confirmed it (didn't answer in time)
      wait('apply');
      const el = s.el;
      const uid = s.uid;
      return ready && (!s.waitPlay || player.playerState === 'playing') ? () => applyFor(s, player, el, uid) : null;
    }
    const v = videoOf(s.el);
    if (v && v.readyState >= 2) { s.noPic = 0; return set('ok'); }
    if (player.playerState !== 'playing' || document.hidden) s.noPic = 0; // (Chrome holds back a hidden tab's video)
    else if (!s.noPic) s.noPic = now;
    else if (now - s.noPic > 20000) { // the player took it, but nothing to see while it plays: a new view
      fail(s, new Error('no picture'));
      retire(s);
      return set('error', 'picture');
    }
    return set('loading', 'picture');
  }

  function steerOnboards() {
    const now = Date.now();
    const on = obLeased(now);
    for (let i = 0; i < OB_MAX; i++) {
      const w = on ? OB.want[i] : null;
      let s = OB.slots[i];
      if (!w) {
        if (s) { dropSlot(s); OB.slots[i] = null; }
        continue;
      }
      if (!s) s = OB.slots[i] = newSlot(i);
      if (s.ch !== w.ch) Object.assign(s, { ch: w.ch, fails: 0, retryAt: 0, err: null, waitPlay: false });
      s.rect = w;
      placeBox(s);
    }
    const slots = OB.slots.filter(Boolean);
    if (!slots.length && !OB.zombies.length) return;
    const player = obPlayer();
    const chans = player && slots.length ? channelsOf(player) : null;
    // Not while F1 TV's own applyDesiredState (content load, channel switch) runs, nor into a
    // dead player, nor in a hidden tab with the video paused (applyDesiredState didn't answer
    // there in 45 s), and after this video's audio switch (unless that is stuck for long): each
    // view added or selected rebuilds the player's pipeline, so the switch goes first.
    const ps = player?.playerState;
    const ready = !!player && (ps === 'playing' || (ps === 'paused' && !document.hidden)) && !isDead(player) && !SDK.f1tv
      && !(A.due && now - A.due < OB_AFTER_AUDIO);
    let op = null;
    for (const z of OB.zombies) {
      if (!op && now >= z.retryAt && (z.player !== player || ready)) op = () => bury(z, player);
    }
    for (const s of slots) {
      const o = slotOp(s, player, chans, ready, now);
      if (!op) op = o;
    }
    if (!op || now < OB.retryAt || !lock('ob')) return;
    call(op).catch((e) => {
      OB.err = errText(e);
      OB.retryAt = Date.now() + 5000; // a bug of ours: not every 250 ms
    }).finally(unlock);
  }
  const OB_AFTER_AUDIO = 20000; // ms the onboards give the audio switch before going ahead

  // For the engine's diagnostics: per slot of the last command, {ch, state: ok|loading|none|error|
  // empty, step (what it waits for), err, n (feeds in the content, when it lacks the channel)}.
  function obReport() {
    if (!obLeased(Date.now())) return [];
    return OB.want.map((w, i) => {
      const s = OB.slots[i];
      if (!w) return { ch: null, state: 'empty' };
      if (!s) return { ch: w.ch, state: 'loading' }; // until the next tick
      const o = { ch: s.ch, state: s.state };
      if (s.state !== 'ok' && s.step) o.step = s.step;
      const err = s.step === 'channels' ? OB.chans?.err : s.err;
      if (err && s.state !== 'ok' && s.state !== 'none') o.err = err;
      if (s.state === 'none') o.n = OB.chans?.n ?? null;
      return o;
    });
  }

  // ---- publishing ---------------------------------------------------------------------

  function emit(content, wall, src) {
    const vid = view && videoOf(view);
    if (content?.eventType) lastEvent = content.eventType;
    const player = view?.associatedPlayer;
    const sdk = player && vid ? sdkRate(player, vid) : null;
    document.dispatchEvent(new CustomEvent('fone-tm', {
      detail: JSON.stringify({
        at: Date.now(),
        src,
        mse: vid ? vid.currentTime : null, // sampled together with the content clock
        h: vid ? vid.getAttribute('handleID') : null,
        c: pick(content),
        w: pick(wall),
        ch: view?.dataset.channelId ?? null,
        title: view?.dataset.title ?? null,
        nViews: document.querySelectorAll('tiledmedia-view').length,
        nVideos: document.querySelectorAll('video').length,
        // speed: what we ask for (null = hands off), what the SDK plays at (exact: read off the
        // MSE handle, not guessed from the element), the element's rate (trim included)
        r: { want: R.target, sdk: sdk ? sdk.rate : null, exact: !!sdk?.exact, el: vid ? vid.playbackRate : null, err: R.err,
          ps: player?.playerState ?? null, fails: R.fails },
        a: audioReport(),
        // the player's SDK, for the diagnostics: our last calls (and F1 TV's applyDesiredState),
        // the player's id, whether the core stopped under it, what it said when it did
        sdk: { pid: SDK.pid, dead: isDead(player), busy: SDK.busy ? SDK.busy.name : null,
          log: SDK.log.slice(-10), fatal: SDK.fatal.slice(-5), states: SDK.states.slice(-10) },
        ob: obReport(), // the dashboard's onboard views
        obErr: OB.err || undefined, // taking a view off its player failed, or a bug of ours
      }),
    }));
  }

  async function follow(next) {
    const my = ++gen;
    view = next;
    lastEvent = null;
    document.querySelectorAll('[data-fone-main]').forEach((e) => e.removeAttribute('data-fone-main'));
    if (next) next.setAttribute('data-fone-main', '');
    const old = sub;
    sub = null;
    if (old) {
      try { await old.view.unregisterListener(old.id); } catch { /* view already gone */ }
    }
    if (!next || my !== gen) return;
    // Fresher (100 ms) than the 250 ms cache behind currentContentTime.
    const T = window.Tiledmedia;
    if (!T?.TimingListenerConfig || !next.addTimingListener) return;
    try {
      const cfg = new T.TimingListenerConfig((ev) => {
        if (view !== next) return;
        lastListenerAt = Date.now();
        emit(ev.contentTime, ev.wallclockTime, 'listener');
      });
      cfg.interval = 100;
      const id = await next.addTimingListener(cfg);
      if (my === gen) sub = { view: next, id };
      else await next.unregisterListener(id); // superseded while we awaited
    } catch {
      /* the heartbeat below still works */
    }
  }

  let lastTick = Date.now();
  setInterval(() => {
    // The renderer froze (the computer slept, or came back from it: 5-14 s gaps seen on wake):
    // content.js's 2 s commands froze with it, so that is not the extension going away. Keep
    // the leases for as long as we were frozen.
    const now = Date.now();
    const frozen = now - lastTick - 250;
    lastTick = now;
    if (frozen > 2000) { want.at += frozen; OB.at += frozen; }
    const next = mainView();
    if (next !== view) follow(next);
    const pl = view?.associatedPlayer ?? null;
    if (pl !== SDK.player) { // F1 TV made a new player (a content load, a rebuild after a crash): start over
      SDK.player = pl;
      SDK.pid = pl?.playerID ?? null;
      Object.assign(R, { pending: null, fails: 0, lastTry: 0, err: null, handle: null });
      A.err = null;
      if (A.muted && A.muted !== pl) { A.muted = null; A.mutedKey = null; }
      if (pl) watchPlayer(pl);
    }
    // The listener only fires when the position changes (so not while paused); the
    // heartbeat covers that, but must not interleave older cached values with it.
    if (view && Date.now() - lastListenerAt > 400) {
      try { emit(view.currentContentTime, view.currentWallclockTime, 'heartbeat'); } catch { /* torn down */ }
    }
    try { steerOnboards(); } catch (e) { OB.err = errText(e); } // with or without a main view (removals)
    const player = view?.associatedPlayer;
    if (!player) return;
    steerRate(player, videoOf(view)).catch(() => {});
    steerAudio(player).catch((e) => { A.err = errText(e); if (SDK.busy && (SDK.busy.name === 'audio' || SDK.busy.name === 'mute')) unlock(); });
  }, 250);
})();
