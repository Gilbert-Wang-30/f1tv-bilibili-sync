// Isolated-world view of the F1 TV player: which <video> is the main one, and
// where playback really is. Loaded before content.js; exposes globalThis.FonePlayer.
//
// Two players exist on f1tv.formula1.com:
//  * Tiledmedia (current default). <video>.currentTime is the core's own MSE clock,
//    NOT the replay position. Truth comes from page.js ('fone-tm' events):
//    content position = video.currentTime + learned offset (frame-precise), re-learned
//    after every seek; live UTC = cached wall-clock origin + content position.
//  * Bitmovin (legacy). <video>.currentTime is the position (epoch seconds on live).
// The on-screen time label is a cross-check and last-resort fallback.
//
// page.js also reports Tiledmedia's playback speed and audio tracks (sample fields r, a);
// rate() is the speed the engine should follow: the chosen one, without the player's own
// clock trims (FoneCore.chosenRate, core.js is loaded first).
(() => {
  'use strict';

  const S = {
    video: null,
    tm: null, // last page.js sample
    tmAt: 0,
    offs: [], // recent (content - mse) samples agreeing with the current offset
    off: null,
    pending: [], // candidate samples for a new offset (seek without a 'seeking' event)
    label: { v: null, changedAt: 0, atCheck: null },
    bad: 0,
    clockBad: false,
    lastCheck: 0,
    utcOrigin: null, // live: UTC seconds of content position 0 (cached; survives fallbacks)
    liveSeen: false, // this view has reported 'live' or 'finished-live'
    feed: null,
    last: null,
    audio: null, // page.js audio report (current track, auto/manual, ...)
    tracks: [], // [[name, lang, kind]] F1 TV's audio tracks for this content
    rate: null, // the chosen speed for this video, last we knew (rate())
  };
  const now = () => Date.now();
  const median = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];
  const LIVEISH = new Set(['live', 'finished-live']);

  const isTiledmedia = () => !!document.querySelector('tiledmedia-view');
  const visibleVideo = (view) => [...view.querySelectorAll(':scope > video')].find((v) => v.style.display !== 'none') || null;

  function largest(videos) {
    let best = null, bestArea = 0;
    for (const v of videos) {
      const r = v.getBoundingClientRect();
      const area = r.width * r.height;
      if (area > bestArea && (v.readyState > 0 || v.currentSrc)) { best = v; bestArea = area; }
    }
    return best;
  }

  function pickVideo() {
    if (isTiledmedia()) {
      const main = document.querySelector('tiledmedia-view[data-fone-main]');
      const v = main && visibleVideo(main);
      if (v) return v;
      // never one of our onboard views (page.js, data-fone-ob): those aren't the picture we sync to
      return largest([...document.querySelectorAll('tiledmedia-view:not([data-fone-ob])')].map(visibleVideo).filter(Boolean));
    }
    return document.querySelector('.player-container.shown .bitmovinplayer-container video')
      || largest(document.querySelectorAll('video'));
  }

  function resetOffset() {
    S.offs = [];
    S.off = null;
    S.pending = [];
    S.bad = 0;
    S.clockBad = false;
  }

  function setVideo(v) {
    if (v !== S.video) {
      resetOffset();
      S.utcOrigin = null; // another handle may be another feed/timeline
      S.liveSeen = false;
      S.rate = null; // a new MSE handle starts at 1x
    }
    S.video = v;
  }

  function adopt(samples) {
    const m = median(samples);
    // The content clock lags (cached, ≤0.5 s), never leads: take the upper envelope.
    S.off = Math.max(...samples.filter((x) => Math.abs(x - m) < 1));
  }

  function learn(d) {
    const v = S.video;
    const c = d.c;
    if (!v || !c || c.eventType === 'unknown' || !Number.isFinite(c.currentPosition) || d.mse == null) return;
    if (d.h !== v.getAttribute('handleID')) { resetOffset(); return; }
    if (v.paused || v.seeking || v.readyState < 3) return;
    const o = c.currentPosition / 1000 - d.mse;
    if (S.off != null && Math.abs(o - S.off) > 1) {
      // Either a stale sample after a main-thread stall (isolated) or a real seek the
      // element never announced (persistent). Switch only after 3 agreeing samples.
      S.pending.push(o);
      if (S.pending.length >= 3) {
        const recent = S.pending.slice(-3);
        if (Math.max(...recent) - Math.min(...recent) < 0.6) {
          S.offs = recent;
          S.pending = [];
          adopt(S.offs);
        }
      }
      return;
    }
    S.pending = [];
    S.offs.push(o);
    if (S.offs.length > 8) S.offs.shift();
    if (S.offs.length >= 2) adopt(S.offs);
  }

  function learnLive(d) {
    const c = d.c, w = d.w;
    if (!c) return;
    if (d.ch !== S.feed) { S.feed = d.ch; S.utcOrigin = null; } // feeds have their own timelines
    if (LIVEISH.has(c.eventType)) S.liveSeen = true;
    // The wall-clock report's own eventType isn't reliable; consistency of the three
    // bound differences is what proves the mapping.
    if (S.liveSeen && c.isWallclockTimeSourceBased && w) {
      const o1 = w.seekUpperBound - c.seekUpperBound;
      const o2 = w.seekLowerBound - c.seekLowerBound;
      const o3 = w.bufferUpperBound - c.bufferUpperBound;
      if (o1 > 1e12 && Math.abs(o1 - o2) < 20 && Math.abs(o1 - o3) < 20) {
        if (S.utcOrigin == null || Math.abs(o1 / 1000 - S.utcOrigin) > 1) S.utcOrigin = o1 / 1000;
      }
    }
  }

  document.addEventListener('fone-tm', (e) => {
    let d;
    try { d = JSON.parse(e.detail); } catch { return; }
    S.tm = d;
    S.tmAt = now();
    if (d.a) {
      if (d.a.tracks) S.tracks = d.a.tracks;
      S.audio = d.a;
    }
    learnLive(d);
    learn(d);
  });

  // The core pauses the element before every seek it performs; 'seeking'/'emptied'
  // cover the rest. Either way the old offset is meaningless afterwards.
  for (const ev of ['seeking', 'emptied', 'pause']) {
    document.addEventListener(ev, (e) => { if (e.target === S.video) resetOffset(); }, true);
  }

  // F1 TV's own position label ("00:12:06" or "12:06"); readable while the bar is hidden.
  function readLabel() {
    const el = document.querySelector('.tm-ui-playbacktimelabel:not(.tm-text-right):not(.tm-ui-playbacktimelabel-live)')
      || document.querySelector('.bmpui-ui-playbacktimelabel');
    if (!el) return null;
    const parts = el.textContent.trim().split(':');
    if (parts.length < 2 || parts.length > 3 || parts.some((x) => !/^\d+$/.test(x))) return null;
    const v = parts.reduce((acc, x) => acc * 60 + Number(x), 0);
    if (v !== S.label.v) S.label = { ...S.label, v, changedAt: now() };
    return v;
  }

  const labelFresh = () => now() - S.label.changedAt < 2000;

  function labelPosition(v) {
    const ui = readLabel();
    if (ui == null || (!v.paused && !labelFresh())) return null;
    const running = !v.paused && !v.seeking;
    return ui + (running ? Math.min(0.99, ((now() - S.label.changedAt) / 1000) * v.playbackRate) : 0);
  }

  // Once a second: does our position agree with F1 TV's label? Three disagreements in a
  // row switch to the label. While you drag the seek bar the label shows the drag target
  // and jumps around, so jumps (and stale labels) never count.
  function selfCheck(v, contentPos) {
    const t = now();
    if (t - S.lastCheck < 1000) return;
    S.lastCheck = t;
    const ui = readLabel();
    const prev = S.label.atCheck;
    S.label.atCheck = ui;
    if (ui == null || !labelFresh() || v.paused || v.seeking || contentPos == null) { S.bad = 0; S.clockBad = false; return; }
    const r = rate();
    if (prev != null && Math.abs(ui - prev - r) > 2) { S.bad = 0; return; } // scrubbing / seek in progress
    const d = contentPos - ui; // the label floors to whole seconds and updates a few times a second
    S.bad = d < -0.6 * r || d > 1 + 0.6 * r ? S.bad + 1 : 0;
    S.clockBad = S.bad >= 3;
  }

  // Content position -> what we hand the engine: UTC when live (and the origin is
  // known), content seconds otherwise. Never mix the two domains.
  // Live stays on the content timeline with its live edge (the engine works edge-relative);
  // the wall-clock origin is only shown in Details — it reads ~40 s late on live streams.
  function finish(contentPos, live, seekEnd, src) {
    return { p: contentPos, live, seekEnd, src, contentPos };
  }

  // Returns {p, live, seekEnd, src, contentPos} or null when we can't tell yet.
  function position() {
    const v = S.video;
    if (!v) return null;
    let out = null;
    if (isTiledmedia()) {
      const d = S.tm;
      const c = d && d.c;
      const live = S.liveSeen;
      if (c && now() - S.tmAt < 1500 && Number.isFinite(c.currentPosition) && c.eventType !== 'unknown') {
        const age = Math.min(0.3, (now() - d.at) / 1000) * rate(); // content seconds since the sample
        const cs = c.currentPosition / 1000;
        let p, src;
        const viaMse = S.off != null && !v.paused && !v.seeking ? v.currentTime + S.off : null;
        if (viaMse != null && Math.abs(viaMse - (cs + age)) < 1.5) { p = viaMse; src = 'tm+mse'; }
        else if (!v.paused) { p = cs + age; src = 'tm'; }
        else { p = cs; src = 'tm'; }
        const seekEnd = live && Number.isFinite(c.seekUpperBound) ? c.seekUpperBound / 1000 : null;
        out = finish(p, live, seekEnd, src);
      } else {
        const p = labelPosition(v); // page.js not reporting (blocked, or player not ready)
        if (p != null && !live) out = finish(p, live, null, 'label'); // live needs the edge: wait for the bridge
      }
    } else {
      const n = v.seekable ? v.seekable.length : 0;
      out = { p: v.currentTime, live: !Number.isFinite(v.duration), seekEnd: n ? v.seekable.end(n - 1) : null,
        src: 'video', contentPos: v.currentTime > 1e9 ? null : v.currentTime };
    }
    if (out) {
      selfCheck(v, out.contentPos);
      if (S.clockBad && !out.src.startsWith('label')) {
        const p = labelPosition(v);
        if (p != null && !out.live) out = finish(p, out.live, null, 'label (player clock disagreed)');
      }
    }
    S.last = out;
    return out;
  }

  // The playback speed someone chose (1, 1.5, 2, ...), not the element's trimmed rate:
  // Tiledmedia sets <video>.playbackRate = chosen speed × syncMultiplier (its view-sync PID),
  // and copied to the commentary every trim was a pitch-kept speed change, i.e. a click.
  // A plain <video> has no such PID: its rate is the speed.
  function rate() {
    const v = S.video;
    if (!v) return 1;
    const tiled = isTiledmedia();
    const r = tiled && S.tm && now() - S.tmAt < 1500 ? S.tm.r : null;
    // since when the SDK's speed has differed from the one page.js holds (chosenRate's DIP_HOLD):
    // its exact rate off the MSE handle, or, for the moment after a seek when the handle is
    // gone, the element's rate reading 1 (a rebuilt pipeline starts there)
    const dip = r?.want > 0 && (r.exact ? r.sdk > 0 && r.sdk !== r.want : r.want !== 1 && Math.abs((v.playbackRate || 1) - 1) < 0.05);
    if (dip) S.dipSince ??= now();
    else S.dipSince = null;
    S.rate = FoneCore.chosenRate({
      tiled, sdk: r?.sdk, exact: !!r?.exact, want: r?.want, el: v.playbackRate,
      live: tiled ? S.liveSeen : !Number.isFinite(v.duration),
      dipFor: S.dipSince == null ? 0 : (now() - S.dipSince) / 1000,
    }, S.rate);
    return S.rate;
  }

  // Tiledmedia state from page.js: content type, speed, audio. null when page.js is silent.
  function tm() {
    const d = S.tm;
    if (!d || now() - S.tmAt > 3000) return null;
    return { eventType: d.c ? d.c.eventType : null, r: d.r || null, audio: S.audio, tracks: S.tracks,
      ob: d.ob || null, obErr: d.obErr || null, h: d.h ?? null, nViews: d.nViews ?? null, sdk: d.sdk || null };
  }

  function diag() {
    const v = S.video;
    const d = S.tm;
    const c = d && d.c;
    const f = (x, k = 1) => (Number.isFinite(x) ? x.toFixed(k) : String(x));
    const parts = [
      `src ${S.last ? S.last.src : '–'}`,
      `p ${S.last ? f(S.last.contentPos ?? S.last.p) : '–'}`,
      `ui ${S.label.v ?? '?'}`,
      `video ${v ? f(v.currentTime) : '–'}`,
    ];
    if (isTiledmedia()) {
      parts.push(`tm ${c ? `${c.eventType} ${f(c.currentPosition / 1000)}/${f(c.contentDuration / 1000, 0)}` : 'no bridge'}`,
        `off ${f(S.off, 2)}`, `feed ${d?.title ?? '?'}`, `views ${d ? `${d.nViews}/${d.nVideos}` : '?'}`);
      if (S.liveSeen) parts.push(`utc0 ${S.utcOrigin == null ? '?' : new Date(S.utcOrigin * 1000).toISOString().slice(11, 19)}`);
    } else {
      parts.push(`len ${v ? f(v.duration) : '–'}`);
    }
    if (S.clockBad) parts.push('CLOCK MISMATCH');
    return parts.join(' · ');
  }

  globalThis.FonePlayer = { pickVideo, setVideo, position, rate, tm, diag, isTiledmedia };
})();
