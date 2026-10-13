// Pure sync math, shared by the offscreen engine and the Node tests.
//
// Time domains:
//   p  F1 TV <video>.currentTime
//   E  real-world moment the frame at p shows (unix seconds)
//   W  wall-clock at which the commentary for E was recorded:  W = E + delay
//   m  position in the stitched commentary playlist (from the server's pieces)
(function (root) {
  'use strict';

  // F1 TV's live latency, only used when its player gives us nothing better.
  const DEFAULT_LIVE_LATENCY = 30;
  // Live default: commentary this far behind its live edge when F1 TV is at its live edge
  // (delay = latency - lag), roughly where the Bilibili web player sits.
  const LIVE_EDGE_LAG = 7;
  const LIVE_EDGE_DELAY = DEFAULT_LIVE_LATENCY - LIVE_EDGE_LAG;

  // How we turn p into E.
  //   multiviewer  replay (or live) on the same timeline as the replay; exact anchor
  //   preroll      same timeline, anchor estimated from the scheduled start
  //   epoch        player exposes unix time directly
  //   edge         live, unknown timeline: measure back from the live edge
  //   manual       not a session feed; you line it up yourself
  function chooseClock(content, v, now) {
    if (!v) return 'manual';
    const live = content?.subtype === 'LIVE' || (v.live && content?.subtype !== 'REPLAY');
    // Live: work relative to the live edges (F1 TV's edge ↔ the commentary's edge, the
    // way the Bilibili player is in step with F1 TV). Players' wall clocks on live streams
    // are stamped when F1 packages the stream (~40 s after the event), so E from them
    // put the commentary ~40 s in the future.
    // A player playing it live is live even when it reports no edge (then it's taken to be
    // at its edge): a live stream's timeline isn't the replay's (2026-10-10 Singapore
    // qualifying: position 0 ~8.5 min before the replay's pre-roll start), so the anchored
    // timeline put the picture 9 minutes in the future and held the video "until the
    // commentary caught up". Only a LIVE item the player plays as a replay (no edge, not
    // live: after the session) goes on the anchored timeline.
    if (live && (v.seekEnd != null || v.live)) return 'edge';
    if (v.p > 1e9) return 'epoch'; // a replay player handing us UTC directly
    if (!content || content.error) return 'manual';
    const anchored = content.p0 != null;
    const kind = content.anchor === 'multiviewer' ? 'multiviewer' : 'preroll';
    return anchored ? kind : 'manual';
  }

  // Extrapolate the reported video state to `now`.
  function videoNow(v, now) {
    const running = !(v.paused || v.seeking || v.waiting);
    const dt = Math.max(0, now - v.at);
    return {
      p: running ? v.p + dt * (v.rate || 1) : v.p,
      // A live edge keeps advancing in real time even while you're paused.
      seekEnd: v.seekEnd == null ? null : v.seekEnd + (v.live ? dt : 0),
    };
  }

  function eventTime(clock, content, p, seekEnd, now, opts) {
    const o = opts || {};
    switch (clock) {
      case 'multiviewer':
      case 'preroll':
        return content.p0 + p;
      case 'epoch':
        return p;
      case 'edge': // (no edge reported: at it)
        return now - (o.liveLatency ?? DEFAULT_LIVE_LATENCY) - (seekEnd == null ? 0 : seekEnd - p);
      default: // manual: anchor p=0 somewhere sensible and let the delay do the rest
        return (o.manualBase ?? content?.scheduledStart ?? 0) + p;
    }
  }

  // The other way round: the wall-clock time at which commentary position m was live
  // (null outside every piece).
  function wallOf(pieces, m) {
    for (const pc of pieces || []) if (m >= pc.m && m < pc.m + pc.d) return pc.w + (m - pc.m);
    return null;
  }

  // Find commentary position for wall-clock W. Pieces are sorted by w.
  function mapWall(pieces, W) {
    if (!pieces || !pieces.length) return { state: 'none' };
    let lo = 0, hi = pieces.length - 1, idx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (pieces[mid].w <= W) { idx = mid; lo = mid + 1; } else hi = mid - 1;
    }
    if (idx < 0) return { state: 'before', wait: pieces[0].w - W };
    const pc = pieces[idx];
    const off = W - pc.w;
    if (off < pc.d) return { state: 'ok', m: pc.m + off, piece: idx, file: pc.f };
    const next = pieces[idx + 1];
    if (next) return { state: 'gap', wait: next.w - W };
    return { state: 'after', past: off - pc.d };
  }

  // ---- the speed the commentary follows -------------------------------------------
  //
  // F1 TV's player trims <video>.playbackRate itself (Tiledmedia's per-view sync PID:
  // element rate = chosen speed × syncMultiplier). That is clock correction, not a
  // speed anyone chose; the position samples already carry it and the lock absorbs it.
  // The trims aren't small: at 1x the element ran at ×1.03 and ×0.93 in steady play,
  // ×0.75 and ×1.18 in the seconds after a seek, ×1.25 right after a content load. v0.2.6
  // took every trim past 3% for a speed, so the commentary went into WSOLA time-stretch
  // at ×0.945, ×1.18, ... and each of those rate changes (and preservesPitch flips) clicked.
  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2]; // the SDK's 0.5-2 range; fone offers 1, 1.5, 2
  // How far the PID was seen to push the element off the chosen speed (±25%), with margin.
  const TRIM_REACH = 0.3;
  // s: how long chosenRate rides out the SDK leaving the speed page.js holds. Dips of 1.8-2.5 s
  // were seen (2026-10-11: a rebuilt handle answered slowly) and cost two clicks each at 1.5.
  const DIP_HOLD = 2.5;
  const nearestSpeed = (r) => SPEEDS.reduce((a, b) => (Math.abs(Math.log(r / b)) < Math.abs(Math.log(r / a)) ? b : a));
  // A plain <video> (F1 TV's legacy Bitmovin player) has no PID: on a replay its rate is the
  // speed someone set (a speed extension; F1 TV's own menu is off there), 1.1x or 1.7x as well
  // as 1.25x, and is followed as is. Only within 2% of a real speed is it that speed: a speed
  // control's 0.05 steps (1.7, 1.8, 1.95) sit 2.5-2.9% off 1.75 and 2, and snapping those would
  // leave the commentary drifting 0.05 s a second, a seek (a click) every few seconds.
  const PLAIN_SNAP = 0.02;

  // x: {tiled: F1 TV's Tiledmedia player (else a plain <video>), sdk: the speed page.js says
  //     the SDK plays at (null when page.js is silent), exact: sdk was read off the video's MSE
  //     handle (not guessed from the element), want: the speed page.js is steering the player
  //     to (null: hands off or loading), el: <video>.playbackRate, live}; prev: the last answer
  //     for this video (null at first).
  // Returns the chosen speed: a speed someone picked, never a trim.
  function chosenRate(x, prev) {
    // page.js holds a speed (want) we already follow, and the SDK's number left it only a moment
    // ago: a rebuilt MSE handle (a feed switch, an onboard view added) plays at 1 until page.js
    // asks again, well under a second. Following that dip costs two commentary jumps and two
    // pitch-mode flips; riding it out costs a few hundred ms of drift. One that lasts is real.
    // The same right after a seek, when the handle is gone for a moment and all there is is the
    // element reading 1 (2026-10-11: every seek at 1.5x was followed down to 1x and back).
    const dipping = x.exact ? x.sdk > 0 && x.sdk !== x.want : x.tiled && x.want !== 1 && Math.abs((x.el || 1) - 1) < 0.05;
    if (x.want > 0 && prev === x.want && dipping && (x.dipFor || 0) < DIP_HOLD) return x.want;
    // The SDK's own number is the chosen speed however far the element is trimmed: a real
    // speed change (fone's panel, or anything else calling Player.changePlaybackRate) sets it.
    // Within 0.02 of the speed page.js asks for it is that speed (page.js used to ask for
    // target-0.02 to wake a stale core; anything else that close is a rounding).
    if (x.exact && x.sdk > 0) return x.want > 0 && Math.abs(x.sdk - x.want) < 0.025 ? x.want : x.sdk;
    if (x.live) return 1; // live is 1x (page.js keeps Tiledmedia there)
    const guess = x.want > 0 ? x.want : prev > 0 ? prev : 1;
    if (!(x.el > 0)) return guess;
    if (!x.tiled) return SPEEDS.find((s) => Math.abs(x.el / s - 1) < PLAIN_SNAP) ?? x.el;
    // Tiledmedia without the SDK's own number: the element's rate carries the trim. Within the
    // PID's reach of the speed page.js is steering to (or the last one we knew) it is that
    // speed; beyond it the speed really changed, to the nearest one a person can pick.
    return Math.abs(x.el / guess - 1) <= TRIM_REACH ? guess : nearestSpeed(x.el);
  }

  // The engine's last guard: within 3% of 1 is 1 (resampled steering, not WSOLA).
  const SYNC_ADJUST = 0.03;
  const nominalRate = (r) => (!(r > 0) || Math.abs(r - 1) < SYNC_ADJUST ? 1 : r);

  // ---- steering the commentary <audio> --------------------------------------------
  //
  // Every correction is audible in Chrome (measured on rendered PCM): a seek clicks, and
  // with preservesPitch on so does every playbackRate change of ≥0.2% (the renderer
  // restarts its WSOLA time-stretcher; ≤0.1% and resampled changes are clean).
  // Meanwhile the position we steer by is noisy in a structured way: F1 TV's content
  // clock is a sample-and-hold of the element clock (250 ms timeupdates) read by a
  // 100 ms listener, so even the best estimate saws by ~50 ms with a period of seconds,
  // and right after a seek or pause it is only good to ±0.3 s until player.js has
  // relearned the element offset. v0.2.1's 40 ms deadband sat at that sawtooth's
  // height and copied F1 TV's own rate trims, so any slow drift or trim became rate
  // changes at the sawtooth's period: a pop every few seconds.
  //
  // So in steady playback the audio is the master clock:
  //  * a lock (target m at time t, advancing at the nominal rate) follows the
  //    measurement slowly (TAU) and re-locks only on real jumps: > SNAP at once,
  //    > JUMP once it persists, a deliberate move (nudge, clock or playlist change), or
  //    the position turning precise;
  //  * the audio is steered to the lock with hysteresis: nothing until it is START off,
  //    then one gentle fixed rate (resampled, see offscreen.js) until within STOP; a
  //    hard seek only when it is SEEK off (a real jump).
  // The lock advances at the chosen speed even while F1 TV trims the element (chosenRate):
  // a short trim is averaged away; one that persists (×1.18, ×0.75 for seconds after a
  // seek) re-locks like a jump and costs a seek, where following it cost a WSOLA click on
  // every change of the trim.
  // FAST: the bounds off 1×, in wall seconds (content seconds / speed), never tighter than
  // 1×'s. There only seeks correct the audio, and F1 TV's view sync swings the element
  // deeper: alternately ~12-18% fast and slow for about 2 s each, the picture ±0.2-0.4 s
  // around its steady pace (a Mac at 1.5×, 2026-10-10). With 1×'s bounds each swing
  // re-locked on its peak, so the next one ran the other way: a seek every ~2 s, for good.
  // A swing outlasts neither PERSIST nor, once the lock sits in its middle, JUMP; a few
  // hundred ms off the picture can't be heard, a seek can. A seek that landed badly (seeks
  // take 0.1-0.3 s, while the target runs at 2×) still gets one more, tighter, right after
  // a re-lock: SEEK_SETTLING for TRIES seeks, never a string of them.
  // PRECISE: 1×'s correction bounds on an exact position source (isPrecise: the element
  // clock read off the MSE handle, or a plain <video>), which has none of the sawtooth: on
  // race day (2026-10-11, Windows) the measured drift sat in one 5 ms bin for minutes
  // (sd ~1 ms) and only stepped, by ~45 ms, when player.js relearned the element offset.
  // At 1× every correction is resampled (preservesPitch is off there), so tighter is
  // cheap: corrections from 25 ms off (not 60) down to 6 ms (not 15), a 0.3% floor for the
  // small ones and 3% for a far one. A correction there is also done only once the audio is
  // within STOP of the measurement itself, not just of the lock: the lock trails a step by
  // TAU, and a 45 ms step judged on the lock alone stopped 13 ms short (then waited on the
  // trim below), judged on both it ends 6 ms off in one go, 12.6 s after the step. The
  // hysteresis still keeps whatever sits inside START for good (+19 ms for two hours that
  // day), and that is a slap-back echo against F1 TV's own audio during interviews (heard
  // from ~20 ms).
  // TRIM: a slow second stage for that residual. Once the drift has sat past TRIM.ON with
  // one sign for TRIM.WAIT of consecutive precise samples, with nothing else going on (no
  // correction running, not settling after a re-lock or a seek, not paused), the audio runs
  // 0.3% off (resampled, inaudible: 19 ms takes ~5 s) until it is within TRIM.OFF or has
  // crossed over, then 1× again. The wait starts over whenever the drift dips inside
  // TRIM.ON, changes sign, is corrected, or the lock moves (a seek or re-lock), so a drift
  // that straddles TRIM.ON costs two rate changes every TRIM.WAIT at the very worst, never
  // a chatter.
  const STEER = {
    SNAP: 1.0, JUMP: 0.2, PERSIST: 1.0, TAU: 6,
    SEEK: 0.3, SEEK_SETTLING: 0.15, SEEK_COARSE: 1.0, SETTLE: 2, RESEEK_GAP: 0.5,
    START: 0.06, START_COARSE: 0.5, STOP: 0.015,
    FIX_TIME: 5, K_MIN: 0.005, K_MAX: 0.02,
    PRECISE: { START: 0.025, STOP: 0.006, K_MIN: 0.003, K_MAX: 0.03 },
    TRIM: { ON: 0.010, OFF: 0.003, WAIT: 8, K: 0.003 },
    LEAD_NEAR: 0.1, LEAD_FAR: 0.3, LEAD_MAX: 0.8, LEAD_CHECK: 0.4,
    FAST: { SNAP: 0.6, JUMP: 0.3, PERSIST: 3, SEEK: 0.3, SEEK_SETTLING: 0.1, TRIES: 2 },
  };
  // A bound at speed r: 1×'s, or FAST's in wall seconds when that is wider.
  const bound = (k, r) => (r === 1 ? STEER[k] : Math.max(STEER[k], STEER.FAST[k] * (k === 'PERSIST' ? 1 : r)));
  // A 1× correction bound: PRECISE's on an exact position source, else the plain one.
  const fine = (k, precise) => (precise ? STEER.PRECISE[k] : STEER[k]);

  // Positions only good to a few hundred ms: player.js before it has learned the
  // element offset ('tm'), or F1 TV's on-screen time label.
  const isCoarse = (src) => typeof src === 'string' && (src === 'tm' || src === 'tm+utc' || src.startsWith('label'));
  // Positions good to a few ms: the element clock itself, read off the video's MSE handle
  // (player.js 'tm+mse') or straight from a plain <video>; not the 250 ms-cached 'tm'.
  const isPrecise = (src) => src === 'tm+mse' || src === 'tm+mse+utc' || src === 'video';

  const inRanges = (ranges, x) => !ranges || ranges.some(([a, b]) => x >= a && x < b - 0.5);

  // st.trim: the slow stage's wait, {since, sign} while the drift has sat past TRIM.ON with
  // that sign (on: true once its correction runs); null otherwise.
  function newSteer() {
    return { lock: null, pend: null, coarse: false, jumped: false, since: -Infinity, k: 0, trim: null,
      lead: { near: STEER.LEAD_NEAR, far: STEER.LEAD_FAR }, seekAt: -Infinity, seekKind: null, leadCheck: false };
  }

  // Video paused, seeking or buffering: re-acquire the lock on resume; keep what we
  // learned about seek latency.
  const holdSteer = (st) => ({ ...st, lock: null, pend: null, jumped: false, k: 0, trim: null, leadCheck: false });

  // One control step (offscreen.js calls it every 100 ms while the video plays).
  // x: {t: monotonic s, m: measured target, rate: nominal speed (chosenRate), audio:
  //     audio.currentTime, paused: audio paused, seeking: audio seek in flight or
  //     starved, coarse: imprecise position source, precise: exact position source
  //     (isPrecise), resync: target moved on purpose, buffered: [[start, end], ...] of
  //     the audio (optional)}
  // Returns {st, rate, seek?}: the playback rate to use, and rarely where to seek to.
  function steer(prev, x) {
    const st = { ...prev };
    const r = x.rate;
    const precise = !!x.precise && !x.coarse;
    let relock = !st.lock || x.resync || (st.coarse && !x.coarse);
    if (!relock) {
      const pred = st.lock.m + (x.t - st.lock.t) * r;
      const e = x.m - pred;
      const ae = Math.abs(e);
      if (ae > bound('SNAP', r)) relock = true;
      else if (ae > bound('JUMP', r) && !x.coarse) {
        // a stale sample, or a real small jump: believe it once it persists
        if (!st.pend || st.pend.sign !== Math.sign(e)) st.pend = { since: x.t, sign: Math.sign(e) };
        if (x.t - st.pend.since >= bound('PERSIST', r)) relock = true;
        else st.lock = { m: pred, t: x.t };
      } else {
        st.pend = null;
        st.lock = { m: pred + e * Math.min(1, (x.t - st.lock.t) / STEER.TAU), t: x.t };
      }
    }
    if (relock) Object.assign(st, { lock: { m: x.m, t: x.t }, pend: null, jumped: true, since: x.t, leadCheck: false, tries: 0, trim: null });
    st.coarse = x.coarse;

    const L = st.lock.m;
    const d = x.audio - L; // > 0: commentary ahead
    const a = Math.abs(d);
    // A seek is landing (or the buffer is refilling): let it, unless it is headed far
    // off (audio.currentTime already reads the seek's destination).
    if (x.seeking && a <= STEER.SNAP) return { st, rate: r };

    if (st.leadCheck && !x.paused && !x.coarse && x.t - st.seekAt >= STEER.LEAD_CHECK) {
      // how did the last seek land? move that kind of seek's lead by half the miss (the lead
      // is in wall seconds: the target runs r content seconds a second while a seek loads)
      const lead = st.lead[st.seekKind];
      st.lead = { ...st.lead, [st.seekKind]: Math.min(STEER.LEAD_MAX, Math.max(0, lead - d / (2 * r))) };
      st.leadCheck = false;
    }
    // Right after a jump, a resume or a seek that landed badly, one more (buffered,
    // quick) seek beats seconds of rate correction; in steady playback only a real jump.
    const settling = (st.jumped || x.paused || x.t - st.since < STEER.SETTLE)
      && (r === 1 || (st.tries || 0) < STEER.FAST.TRIES);
    const limit = x.coarse ? STEER.SEEK_COARSE : settling ? bound('SEEK_SETTLING', r) : bound('SEEK', r);
    if (a > limit) {
      if (st.jumped || x.t - st.seekAt > STEER.RESEEK_GAP) {
        // land a little ahead: the target keeps moving while the seek loads, and a seek
        // outside the buffer has to fetch first
        const kind = inRanges(x.buffered, L) ? 'near' : 'far';
        Object.assign(st, { seekAt: x.t, since: x.t, seekKind: kind, k: 0, trim: null, jumped: false, leadCheck: !x.coarse, tries: (st.tries || 0) + 1 });
        return { st, rate: r, seek: L + st.lead[kind] * r };
      }
      st.k = 0; st.trim = null;
      return { st, rate: r }; // a seek just landed: re-seek in a moment rather than nudge
    }
    st.jumped = false;
    if (x.paused) {
      st.k = 0; st.trim = null;
      return { st, rate: r };
    }
    // At any speed but 1× the audio must keep its pitch (WSOLA time-stretch), and every
    // WSOLA rate change is an audible click — so off 1× we only ever seek (above).
    if (r !== 1) {
      st.k = 0; st.trim = null;
      return { st, rate: r };
    }
    if (st.k === 0) {
      if (a > (x.coarse ? STEER.START_COARSE : fine('START', precise))) {
        st.k = -Math.sign(d) * Math.min(fine('K_MAX', precise), Math.max(fine('K_MIN', precise), a / STEER.FIX_TIME));
        st.trim = null;
      } else if (precise && !settling && a >= STEER.TRIM.ON) {
        // the residual the hysteresis leaves: trim it once it has sat there for TRIM.WAIT
        if (!st.trim || st.trim.sign !== Math.sign(d)) st.trim = { since: x.t, sign: Math.sign(d) };
        else if (x.t - st.trim.since >= STEER.TRIM.WAIT) {
          st.k = -Math.sign(d) * STEER.TRIM.K;
          st.trim = { ...st.trim, on: true };
        }
      } else {
        st.trim = null;
      }
    } else {
      // Done, or overshot. The lock lags a step it is following (TAU), and a correction
      // that stops within STOP of the lock alone keeps the rest of the lag for good: on a
      // precise source (the measurement good to ~1 ms) the audio must be within STOP of the
      // measurement as well, so the residual is STOP at most.
      const done = st.trim?.on ? a < STEER.TRIM.OFF
        : a < fine('STOP', precise) && (!precise || Math.abs(x.audio - x.m) < STEER.PRECISE.STOP);
      if (done || Math.sign(d) === Math.sign(st.k)) { st.k = 0; st.trim = null; }
    }
    return { st, rate: r * (1 + st.k) };
  }

  root.FoneCore = { chooseClock, videoNow, eventTime, mapWall, wallOf, chosenRate, nominalRate, isCoarse, isPrecise, newSteer, holdSteer, steer,
    STEER, SPEEDS, TRIM_REACH, DEFAULT_LIVE_LATENCY, LIVE_EDGE_DELAY };
})(typeof globalThis !== 'undefined' ? globalThis : this);
