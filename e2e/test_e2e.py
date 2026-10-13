"""Headless Chrome + the real extension + the real server, against a mock F1 TV page.

Phase A (replay): KL qualifying replay (real content id; real MultiViewer anchor)
  with a finished commentary recording -> sync, seek, pause, nudge, a speed set on the
  plain <video> (1.25x) followed, end-of-recording.
Phase A2 (Tiledmedia): F1 TV's real player (mock of its SDK): content clock, seeks, view
  switches; F1 TV audio switched to track sound (FX) on load / feed switch, a manual pick
  kept; 2x on replays (Player.changePlaybackRate) with the commentary at 2x, live kept at 1x;
  the player's own rate trims (x1.004 .. x1.18, x0.75) never followed as a speed.
Phase B (live):   commentary still being recorded and the video ahead of it
  -> 'waiting', auto-hold pauses F1 TV, resumes once the commentary catches up.
Phase A3 (onboards, run last): the dashboard's driver onboards in F1 TV's own player (mock):
  from the dashboard iframe through layout.js (rects, channel, switch, empty slot, split off),
  then page.js alone: F1 TV's channel switch, a player rebuild, a channel the content lacks,
  SDK calls that never answer or are refused, the commands stopping (lease), a hidden tab.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request

from playwright.async_api import async_playwright

SERVER = os.environ.get("FONE_SERVER", "http://fone-dev-e2e-server:8788")
CHANNEL = "胡闹P房"
MEDIA = "/media"
REPLAY_CID = "1000011024"  # 2026 Bahrain GP in Malaysia - Qualifying
RACE_CID = "1000011022"  # 2026 Bahrain GP in Malaysia - Race (now a replay; run.sh seeds it as LIVE)
LIVE_CID = "1000010385"  # 2026 Azerbaijan GP - Race (just a second anchored content id)

PAGE = """<!doctype html><title>F1 TV</title><body style="margin:0;background:#000">
<video id="v" src="/media/video.mp4" muted autoplay playsinline style="width:960px;height:540px"></video></body>"""

# What F1 TV really runs: the Tiledmedia player. The main <video>.currentTime is the
# core's own MSE clock; the replay position lives on <tiledmedia-view>.currentContentTime
# and jumps on seek while the element's clock keeps running. Plus an onboard tile decoy,
# and F1 TV's audio menu (one button).
TM_PAGE = """<!doctype html><title>F1 TV</title><body style="margin:0;background:#000">
<tiledmedia-ui-view><div class="tm-ui-controlbar tm-hidden">
  <span class="tm-ui-playbacktimelabel" id="lbl">00:00:00</span><span class="tm-ui-playbacktimelabel tm-text-right">01:19:29</span>
</div></tiledmedia-ui-view>
<tiledmedia-view id="mainview" data-channel-id="1033" data-title="F1 LIVE" data-type="pres" data-static>
  <video id="v" handleID="7" src="/media/video.mp4" muted autoplay playsinline style="width:960px;height:540px;display:initial"></video>
</tiledmedia-view>
<tiledmedia-view id="obcview" data-channel-id="1009" data-title="VER" data-type="obc" data-static>
  <video handleID="8" src="/media/video.mp4" muted autoplay playsinline style="width:320px;height:180px;display:initial"></video>
</tiledmedia-view>
<div style="position:fixed;left:400px;bottom:20px;z-index:5"><button id="pick-eng" onclick="tmPickAudio('English')">English</button></div>
<script>
  // Mirrors the real Tiledmedia Web SDK surface (checked against F1 TV's bundle):
  // TimingListenerConfig(callback) with .interval/.callback; addTimingListener -> ViewListenerID
  // object; unregisterListener(id) rejects ids it didn't issue; no removeTimingListener;
  // currentContentTime undefined until the first timing event; eventType vod|live|finished-live.
  // ?grid=1: the views in a fixed div.video-grid (z 100) like F1 TV's player, for the split view.
  if (new URLSearchParams(location.search).get('grid') === '1') {
    const g = document.createElement('div');
    g.className = 'video-grid';
    g.style.cssText = 'position:fixed;left:0;top:0;width:100%;height:100%;z-index:100;background:#000';
    g.append(document.getElementById('mainview'), document.getElementById('obcview'));
    document.body.prepend(g);
  }
  window.__off = 0; window.__event = 'vod'; window.__origin = 0; window.__scrub = null;
  if (new URLSearchParams(location.search).get('ev') === 'live') { window.__event = 'live'; window.__origin = Date.now() - 45000; }
  window.__active = new Set();
  const loadedAt = Date.now();
  const mainVid = () => document.getElementById('v');
  // the real core reports a cached value up to 250 ms behind
  window.contentPos = () => Math.floor((mainVid().currentTime + window.__off) * 4) / 4;
  class ViewListenerID { constructor(viewID, id) { this.viewID = viewID; this.id = id; this.listenerID = id; } }

  // ---- player, content items, tracks (vod chunk: Player 8175ff, ContentItem 3986, Feed 2260,
  // AudioTrack 2443, AudioStream 2402, AudioTrackSelection 4017, MSE handle 7283/7500/7519) ----
  const later = (x, ms = 5) => new Promise((r) => setTimeout(() => r(x), ms)); // a worker round trip
  class ContentItem { constructor(uid) { this._uid = uid; this.feeds = []; this._stype = 'contentItem'; }
    get contentID() { return this._uid; }
    get crossFeedAudioTracks() { return later(this.feeds.flatMap((f) => f.audioTracks)); } }
  class Feed { constructor(ci, ch) { this._uid = ci._uid; this._feedID = ci.feeds.length; this._ci = ci; this._ch = ch; this.audioTracks = []; this._stype = 'feed'; }
    get feedIdx() { return this._feedID; }
    get uid() { return `${this._uid},${this._feedID}`; }
    equals(o) { return o instanceof Feed && this._uid === o._uid && this._feedID === o._feedID; }
    get feedMetadata() { return later(JSON.stringify({ channel_id: this._ch })); }
    get parentContentItem() { return later(this._ci); } }
  class AudioStream { constructor(t) { this._uid = t._uid; this._feedID = t._feedID; this._trackID = t._trackID; this._subTrackID = 0; this._track = t; this._stype = 'audioStream'; }
    get trackIdx() { return this._trackID; }
    get parentAudioTrack() { return later(this._track); }
    get parentContentItem() { return later(this._track._feed._ci); } }
  class AudioTrack { constructor(f, name, lang) { this._uid = f._uid; this._feedID = f._feedID; this._trackID = f.audioTracks.length; this._feed = f; this._n = name; this._l = lang; this.streams = [new AudioStream(this)]; this._stype = 'audioTrack'; }
    get trackIdx() { return this._trackID; }
    get name() { return later(this._n); }      // Promises on the main thread (worker messages)
    get language() { return later(this._l); }
    get parentFeed() { return later(this._feed); } }
  class VideoQuality { constructor(f) { this._uid = f._uid; this._feedID = f._feedID; this._trackID = 0; this._feed = f; }
    get parentContentItem() { return later(this._feed._ci); } }
  class AudioTrackSelection { constructor(disable, selection, mode = 'selected-track-existing-audio-during-switch') {
    if (!(selection instanceof ContentItem || selection instanceof Feed || selection instanceof AudioTrack || selection === null))
      throw new Error('selection in the AudioTrackSelection must be a ContentItem, Feed, AudioTrack or null');
    this.disable = disable; this.selection = selection; this.preferredLanguage = ''; this.audioTrackSelectionMode = mode; } }
  class DesiredStateConfig { static maintainPosition() { return new DesiredStateConfig(); } }
  class VideoSelection { constructor(selection) {
    if (!(selection instanceof ContentItem || selection instanceof Feed || selection instanceof VideoQuality))
      throw new Error('selection in the VideoSelection must be a ContentItem, Feed or VideoQuality');
    this.selection = selection; this._stype = 'VideoSelection'; } }
  // <video>.playbackRate = chosen rate x view-sync multiplier, rewritten on every sync step:
  // a direct write to the element doesn't survive. A new handle starts at 1x.
  class MSEHandle { constructor(id, video) { this.mseHandleID = id; this.videoElement = video; this.playbackRate = 1; this.syncMultiplier = 1;
      video.setAttribute('handleID', String(id)); video.playbackRate = 1; }
    changePlaybackRate(m) { this.playbackRate = m; this.videoElement.playbackRate = this.playbackRate * this.syncMultiplier; }
    syncViews(m) { this.syncMultiplier = m; this.videoElement.playbackRate = this.playbackRate * this.syncMultiplier; } }
  window.__rateCalls = []; window.__audioLog = []; window.__syncSeq = [1]; window.__muteCalls = []; window.__noHandle = false;
  const INTL = [['English Int.', 'eng'], ['Deutsch', 'deu'], ['Francais', 'fra'], ['Nederlands', 'nld'], ['Portugues', 'por'], ['日本語', 'jpn'], ['Track FX', 'fx']];
  // (1016 NOR and 1020 LEC: driver onboards with no audio of their own here)
  const OWN = { 1033: [['English', 'eng']], 1025: INTL, 1003: [], 1009: [['VER', 'tea'], ['English', 'eng'], ['Deutsch', 'deu']], 1016: [], 1020: [] };
  const AUDIO_DEFAULT = { 1033: '1033.eng', 1025: '1025.eng', 1003: '1033.eng', 1009: '1009' }; // content API additionalStreams
  let contentSeq = 0, nextHandle = 7, playerSeq = 0;
  // F1 TV loads one content item per channel (contentId "<cid>-<channel>"): the channel's own
  // feed first, the other feeds' audio alongside (contentItem.crossFeedAudioTracks).
  function makeContent(ch) {
    const ci = new ContentItem(`${location.pathname.split('/')[2]}-${ch}#${++contentSeq}`);
    for (const c of [ch, ...[1033, 1025, 1009, 1016, 1020].filter((x) => x !== ch)]) {
      const f = new Feed(ci, c); ci.feeds.push(f);
      for (const [n, l] of OWN[c]) f.audioTracks.push(new AudioTrack(f, n, l));
    }
    return ci;
  }
  function defaultTrack(ci, ch) { // F1 TV: the channel's audioDefault "channel.lang", else that feed's first track
    const [c, lang] = AUDIO_DEFAULT[ch].split('.');
    const f = ci.feeds.find((x) => x._ch === +c) || ci.feeds[0];
    return (lang && f.audioTracks.find((t) => t._l === lang)) || f.audioTracks[0] || null;
  }
  // Onboard views (SDK vod chunk: Player.addView/removeView/applyDesiredState/destroy 8218-8420,
  // dispatcher addView 4501, VideoSelection 2386): addView refuses a view still associated with a
  // player; applyDesiredState sends every associated view's desiredVideoSelection (and refuses
  // when all are null); destroy() takes every view off. A view selected on a feed gets a <video>
  // (its own MSE handle) playing the mock stream, data-ch = the feed's channel; F1 TV's views in
  // this page (data-static) keep their own <video>s. __obHang / __obFail hold or refuse addView,
  // removeView, apply (F1 TV's calls too); __obRelease() lets the held ones through, late.
  window.__obHang = new Set(); window.__obFail = new Set(); window.__obHeld = []; window.__obCalls = [];
  window.__mainSelWrites = 0; window.__audioSelWrites = 0;
  function obCall(kind, fn) {
    window.__obCalls.push(kind);
    if (window.__obFail.has(kind)) return later(null, 5).then(() => { throw new Error(`mock: ${kind} refused`); });
    if (window.__obHang.has(kind)) return new Promise((res, rej) => window.__obHeld.push(() => fn().then(res, rej)));
    return fn();
  }
  window.__obRelease = () => { window.__obHang.clear(); const held = window.__obHeld.splice(0); held.forEach((go) => go()); return held.length; };
  function showPicture(player, v, s) {
    const f = s?.selection instanceof Feed ? s.selection : null;
    if (!f) { dropPicture(player, v); return; }
    let vid = v.querySelector(':scope > video');
    if (!vid) {
      vid = document.createElement('video');
      Object.assign(vid, { muted: true, autoplay: true, playsInline: true, loop: true });
      vid.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;display:initial';
      vid.addEventListener('loadedmetadata', () => { try { vid.currentTime = mainVid().currentTime; } catch { /* */ } }, { once: true });
      vid.src = '/media/video.mp4';
      const id = nextHandle++;
      player.mseManager.mseHandles.set(id, new MSEHandle(id, vid));
      v.appendChild(vid);
    }
    vid.dataset.ch = String(f._ch);
    vid.dataset.feed = f.uid;
  }
  function dropPicture(player, v) {
    for (const vid of v.querySelectorAll(':scope > video')) {
      for (const [id, h] of player.mseManager.mseHandles) if (h.videoElement === vid) player.mseManager.mseHandles.delete(id);
      vid.removeAttribute('src'); vid.load(); vid.remove();
    }
  }
  class Player {
    constructor() { this.playerID = ++playerSeq; this.mseManager = { mseHandles: new Map() }; this.playerState = 'loading'; this._coreRate = 1;
      this._views = new Set(); this._destroyed = false; this._stopped = false; this._fatal = new Map(); this.audioMuted = false;
      this.desiredAudioTrackSelection = new AudioTrackSelection(false, null); this.currentAudioTrack = null; }
    // The core's state after a fatal error: every command refused with this message while
    // playerState still reads 'playing' (seen 2026-10-11); F1 TV's app rebuilds the player.
    _refuse(what) { if (this._stopped) throw new Error(`code: -1, message: error when calling Player.${what}: no user interaction possible in current state (internally-stopped)`); }
    addPlayerFatalErrorListeners(cb) { const id = this._fatal.size + 1; this._fatal.set(id, cb); return id; }
    setAudioIsMuted(m) { this._refuse('setAudioIsMuted'); window.__muteCalls.push(!!m); this.audioMuted = !!m; return later(null, 20); }
    get desiredAudioTrackSelection() { return this._audioSel; }
    set desiredAudioTrackSelection(s) { this._audioSel = s; window.__audioSelWrites++; }
    get destroyed() { return this._destroyed; }
    addView(...views) {
      return obCall('addView', async () => {
        for (const v of views) {
          if (!(v instanceof TMView)) throw new Error('Player.addView() must be called with a View object');
          if (v.associatedPlayer !== undefined) throw new Error('The view is already associated to a Player, cannot add it to another (or the same) player again');
        }
        if (this._destroyed) throw new Error('The player is not registered, cannot register a view to it');
        await later(null, 20);
        for (const v of views) { v._player = this; this._views.add(v); }
      });
    }
    removeView(...views) {
      return obCall('removeView', async () => {
        for (const v of views) if (v.associatedPlayer !== this) throw new Error('cannot call removeView() with a view that is not associated with this player');
        await later(null, 20);
        for (const v of views) { this._views.delete(v); v._player = undefined; dropPicture(this, v); }
      });
    }
    async destroy() {
      for (const v of [...this._views]) { this._views.delete(v); v._player = undefined; if (!v.hasAttribute('data-static')) dropPicture(this, v); }
      this._destroyed = true; this.playerState = 'idle';
    }
    applyDesiredState(cfg) {
      return obCall('apply', async () => {
        this._refuse('applyDesiredState');
        if (cfg !== undefined && !(cfg instanceof DesiredStateConfig)) throw new Error('Player.applyDesiredState() must be called with a DesiredStateConfig object or undefined');
        if (this._destroyed) throw new Error('cannot call applyDesiredState() after the player has been stopped');
        if (!this._views.size) throw new Error('cannot call applyDesiredState() before adding at least 1 view to the player');
        const views = [...this._views].map((v) => [v, v.desiredVideoSelection]); // sent with the call
        if (!views.some(([, s]) => s)) throw new Error('cannot call applyDesiredState(), all views have null desiredVideoSelection');
        const sel = this.desiredAudioTrackSelection;
        await later(null, 150);
        const s = sel.selection;
        const t = sel.disable ? null : s instanceof AudioTrack ? s : s instanceof Feed ? s.audioTracks[0] : defaultTrack(this._content, this._ch);
        this.currentAudioTrack = t ? t.streams[0] : null; // PLAYER_SET_CURRENT_AUDIO_STREAM
        window.__audioLog.push(t ? t._n : null);
        for (const [v, vs] of views) if (!v.hasAttribute('data-static') && v._player === this) showPicture(this, v, vs);
        this.playerState = 'playing';
        return { gotInterrupted: false };
      });
    }
    async changePlaybackRate(m) { // "multiplier float from 0.5 to 2.0"
      this._refuse('changePlaybackRate');
      if (typeof m !== 'number') throw new Error('Player.changePlaybackRate() must be called with a number');
      window.__rateCalls.push(m);
      await later(null, 40);
      if (m < 0.5 || m > 2) throw new Error(`unable to change playback rate, playback rate should be between 0.5 and 2, but was [${m}]`);
      // window.__staleCore: a core that keeps its own rate across MSE handles, so asking
      // for the rate it believes it already plays at changes nothing (JS handles start at 1)
      if (window.__staleCore && m === this._coreRate) return;
      this._coreRate = m;
      for (const h of this.mseManager.mseHandles.values()) h.changePlaybackRate(m);
    }
  }
  let P = window.__player = new Player(); // a new one when F1 TV rebuilds its player (tmRebuildPlayer)
  let syncK = 0; // the view-sync PID (MSEHandle.syncViews), set window.__syncSeq for trims
  setInterval(() => { const m = window.__syncSeq[syncK++ % window.__syncSeq.length]; for (const h of P.mseManager.mseHandles.values()) h.syncViews(m); }, 500);

  let nextView = 1;
  class TMView extends HTMLElement {
    constructor() { super(); this.viewID = nextView++; this.timers = new Map(); this.nextId = 0; this._player = undefined; this._sel = null; this.zDistance = 0; }
    get associatedPlayer() { return this._player; }
    get desiredVideoSelection() { return this._sel; }
    set desiredVideoSelection(s) {
      if (s !== null && !(s instanceof VideoSelection)) throw new Error('desiredVideoSelection must be a VideoSelection or null');
      this._sel = s;
      if (this.id === 'mainview') window.__mainSelWrites++;
    }
    get currentVideo() { return this._vq ?? null; }
    get currentContentTime() {
      if (Date.now() - loadedAt < 300 || !this._player) return undefined; // (the SDK's timing cache goes with the player)
      if (this.dataset.type === 'obc') return { eventType: 'vod', currentPosition: 1000 * this.querySelector('video').currentTime, contentDuration: 4769000 };
      const c = contentPos() * 1000, live = window.__event !== 'vod';
      return { eventType: window.__event, currentPosition: c, contentDuration: 4769000, seekLowerBound: 0,
               seekUpperBound: live ? (window.__stair ? Math.floor((c + 20000) / (window.__stair * 1000)) * window.__stair * 1000 : c + 20000) : 4769000, bufferLowerBound: 0, bufferUpperBound: c + 10000,
               isAtLiveEdge: false, isCasting: false, timeshiftInMs: 0, isWallclockTimeSourceBased: live };
    }
    get currentWallclockTime() {
      const c = this.currentContentTime;
      if (!c || window.__event === 'vod') return c && { ...c, eventType: 'unknown' };
      const o = window.__origin;
      // currentPosition runs ahead of the picture on the real thing; eventType isn't reliable
      return { eventType: 'unknown', currentPosition: c.currentPosition + o + 3000, seekLowerBound: c.seekLowerBound + o,
               seekUpperBound: c.seekUpperBound + o, bufferUpperBound: c.bufferUpperBound + o, bufferLowerBound: o };
    }
    async addTimingListener(cfg) {
      const id = new ViewListenerID(this.viewID, this.nextId++);
      let lastPos = null;
      this.timers.set(id.id, setInterval(() => {
        const ct = this.currentContentTime;
        if (!ct || (cfg.onlySendOnChangedCurrentPosition && ct.currentPosition === lastPos)) return;
        lastPos = ct.currentPosition;
        cfg.callback({ contentTime: ct, wallclockTime: this.currentWallclockTime });
      }, cfg.interval));
      window.__active.add(`${this.viewID}:${id.id}`);
      return id;
    }
    async unregisterListener(id) {
      if (!(id instanceof ViewListenerID)) throw new Error('unregisterListener: listenerID is not a ViewListenerID');
      if (id.viewID !== this.viewID) return; // the real core silently mis-handles foreign ids
      clearInterval(this.timers.get(id.id)); this.timers.delete(id.id);
      window.__active.delete(`${this.viewID}:${id.id}`);
    }
  }
  customElements.define('tiledmedia-view', TMView);
  customElements.define('tiledmedia-ui-view', class extends HTMLElement { get internalView() { return document.getElementById('mainview'); } });
  for (const v of document.querySelectorAll('tiledmedia-view')) { v._player = P; P._views.add(v); } // F1 TV's views, on its player
  window.Tiledmedia = { AudioTrackSelection, DesiredStateConfig, ContentItem, Feed, VideoSelection,
    TimingListenerConfig: class { constructor(e) { this.interval = 250; this.onlySendOnChangedCurrentPosition = true; this.callback = async () => {}; if (e) this.callback = e; } } };
  window.tmSeek = (x) => { window.__off = x - mainVid().currentTime; };   // content jumps, element clock doesn't
  setInterval(() => { const s = Math.floor(window.__scrub ?? contentPos()); const z = (n) => String(n).padStart(2, '0');
    document.getElementById('lbl').textContent = `${z(Math.floor(s / 3600))}:${z(Math.floor(s % 3600 / 60))}:${z(s % 60)}`; }, 200);

  // F1 TV (vod chunk 21225-21366): every content load / channel switch = a new content item,
  // a new MSE handle for the video, the channel's audioDefault, applyDesiredState.
  window.tmSwitchFeed = (ch, title) => {
    const ci = makeContent(ch), main = document.getElementById('mainview');
    P._content = ci; P._ch = ch; P.playerState = 'loading';
    main.dataset.channelId = ch; main.dataset.title = title; main._vq = new VideoQuality(ci.feeds[0]);
    main.desiredVideoSelection = new VideoSelection(ci.feeds[0]); // vod chunk 21241: s.desiredVideoSelection = new _i(o)
    document.getElementById('obcview')._vq = new VideoQuality(ci.feeds.find((f) => f._ch === 1009));
    for (const [id, h] of P.mseManager.mseHandles) if (h.videoElement === mainVid()) P.mseManager.mseHandles.delete(id);
    // (__noHandle: the player reports 'playing' before the video has its MSE handle, as the real
    // one does while starting; tmGiveHandle() then creates it)
    if (!window.__noHandle) { const id = nextHandle++; P.mseManager.mseHandles.set(id, new MSEHandle(id, mainVid())); }
    P.desiredAudioTrackSelection = new AudioTrackSelection(false, defaultTrack(ci, ch));
    return P.applyDesiredState(contentSeq > 1 ? DesiredStateConfig.maintainPosition() : undefined);
  };
  window.tmGiveHandle = () => { const id = nextHandle++; P.mseManager.mseHandles.set(id, new MSEHandle(id, mainVid())); return id; };
  // The core dying: the listeners hear {code, tmmessage}, every command is refused from then on,
  // playerState keeps saying 'playing'. (F1 TV's app then tears the player down: tmRebuildPlayer.)
  window.tmFatal = async (code = -1, msg = 'CORE_GENERIC_ERROR: the core stopped (internally-stopped)') => {
    P._stopped = true;
    for (const cb of P._fatal.values()) { try { await cb({ code, tmmessage: msg }); } catch { /* a listener must not break the SDK */ } }
  };
  // F1 TV's audio menu (vod 23991-24003): first cross-feed track with that name.
  window.tmPickAudio = async (name) => {
    const tracks = await P._content.crossFeedAudioTracks;
    const all = await Promise.all(tracks.map(async (t) => ({ name: await t.name, language: await t.language, feed: t })));
    const n = all.find((e) => e.name === name);
    P.desiredAudioTrackSelection = new AudioTrackSelection(false, n.feed);
    await P.applyDesiredState();
  };
  // F1 TV rebuilding its player (content change, or after an error): Player.stop()/destroy() take
  // every view off the old one, ours too; a new player gets F1 TV's views and loads the content.
  window.tmRebuildPlayer = async (ms = 300) => {
    const old = P, title = document.getElementById('mainview').dataset.title;
    await old.destroy();
    await later(null, ms);
    P = window.__player = new Player();
    await P.addView(...document.querySelectorAll('tiledmedia-view[data-static]'));
    P.mseManager.mseHandles.set(99, new MSEHandle(99, document.querySelector('#obcview video')));
    return window.tmSwitchFeed(old._ch, title);
  };
  window.__curAudio = () => P.currentAudioTrack?._track._n ?? null;
  window.__sdkRate = () => [...P.mseManager.mseHandles.values()].find((h) => h.videoElement === mainVid())?.playbackRate ?? null;
  P.mseManager.mseHandles.set(99, new MSEHandle(99, document.querySelector('#obcview video')));
  window.tmSwitchFeed(1033, 'F1 LIVE');
</script></body>"""


def api(path, data=None, method=None):
    req = urllib.request.Request(SERVER + path, data=json.dumps(data).encode() if data else None,
                                 method=method, headers={"content-type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=20))


passed = 0


def dump_diag(secs=40):
    """What the engine did lately (its diagnostics on the server), for a failure's record."""
    try:
        evs = api(f"/api/diag?since={time.time() - secs}&type=seek,state,speed,sdk,fatal,handle,pstate,audio,snap")["events"]
        for e in evs[-30:]:
            print("   diag:", json.dumps({k: v for k, v in e.items() if k not in ("client", "build", "platform", "rx")}, ensure_ascii=False)[:260])
    except Exception as ex:
        print("   (no diag:", ex, ")")


def check(cond, msg):
    global passed
    if not cond:
        print("FAIL:", msg)
        dump_diag()
        sys.exit(1)
    passed += 1
    print("ok -", msg)


async def status(page):
    raw = await page.evaluate("() => document.getElementById('fone-overlay')?.dataset.status")
    return json.loads(raw) if raw else {}


async def wait_status(page, pred, timeout=30, what="condition"):
    deadline = time.time() + timeout
    st = {}
    while time.time() < deadline:
        st = await status(page)
        if st and pred(st):
            return st
        await asyncio.sleep(0.2)
    keys = ("state", "text", "drift", "E", "m", "audioTime", "lead", "holding", "error")
    print("last status:", json.dumps({k: st.get(k) for k in keys if k in st}, ensure_ascii=False), json.dumps(st, ensure_ascii=False)[:500])
    dump_diag()
    raise AssertionError(f"timed out waiting for {what}")


async def video(page, js):
    return await page.evaluate(f"() => {{ const v = document.getElementById('v'); return {js}; }}")


async def wait_js(page, js, pred, timeout, what):
    deadline = time.time() + timeout
    v = None
    while time.time() < deadline:
        v = await page.evaluate(js)
        if pred(v):
            return v
        await asyncio.sleep(0.2)
    print("last value:", json.dumps(v, ensure_ascii=False)[:600])
    raise AssertionError(f"timed out waiting for {what}")


PANEL = "document.getElementById('fone-overlay').shadowRoot"
DETAILS = f"() => [...{PANEL}.getElementById('kv').children].map((e) => e.textContent).join('\\n')"

# Phase A3 (onboards), in every frame before the page's scripts: page.js's reports (fone-tm) and the
# onboard commands it gets (fone-cmd); and onboard commands the real dashboard might send by
# itself are kept from layout.js (dropped at the window before its listener), so that only the
# test's own (marked __e2e) drive the views.
OB_INIT = """(() => {
  window.__tmN = 0; window.__tm = null; window.__obCmds = []; window.__dashOb = [];
  window.addEventListener('message', (e) => {
    const d = e.data;
    if (d && d.type === 'fone-layout-cmd' && d.cmd === 'onboards' && !d.__e2e) { e.stopImmediatePropagation(); window.__dashOb.push(d); }
  }, true);
  document.addEventListener('fone-tm', (e) => { window.__tmN++; try { window.__tm = JSON.parse(e.detail); } catch {} });
  document.addEventListener('fone-cmd', (e) => { try { const m = JSON.parse(e.detail); if (m.cmd === 'onboards') window.__obCmds.push(m); } catch {} });
})()"""

# Our onboard boxes and views, F1 TV's (mock) player and page.js's report.
OB_STATE = """() => {
  const P = window.__player;
  const r = (el) => { const b = el.getBoundingClientRect(); return [b.left, b.top, b.width, b.height].map((v) => Math.round(v * 10) / 10); };
  const boxes = [...document.querySelectorAll('[data-fone-ob-box]')].map((b) => {
    const v = b.querySelector('tiledmedia-view[data-fone-ob]');
    const vid = v && v.querySelector(':scope > video');
    const cs = getComputedStyle(b);
    return { slot: +b.dataset.foneObBox, rect: r(b), z: cs.zIndex, pe: cs.pointerEvents, radius: cs.borderTopLeftRadius,
      bg: cs.backgroundColor, overflow: cs.overflow, display: cs.display, parent: b.parentElement === document.documentElement,
      view: v ? { rect: r(v), id: v.viewID, mark: v.__e2e ?? null, player: v.associatedPlayer === P, inPlayer: P._views.has(v),
        ch: v.desiredVideoSelection?.selection?._ch ?? null, uid: v.desiredVideoSelection?.selection?._uid ?? null, z: v.zDistance } : null,
      video: vid ? { ch: vid.dataset.ch, t: vid.currentTime, ready: vid.readyState, paused: vid.paused } : null };
  }).sort((a, b) => a.slot - b.slot);
  const ours = [...P._views].filter((v) => v.hasAttribute('data-fone-ob'));
  return { boxes, ours: ours.length, views: P._views.size, nOb: document.querySelectorAll('tiledmedia-view[data-fone-ob]').length,
    ob: window.__tm?.ob ?? null, tmN: window.__tmN, main: document.querySelector('[data-fone-main]')?.id ?? null,
    mainSel: window.__mainSelWrites, audioSel: window.__audioSelWrites, audio: __curAudio(), content: P._content?._uid ?? null,
    feeds: P._content?.feeds.length ?? 0, calls: window.__obCalls.length, callLog: window.__obCalls.slice(-6), player: P.playerID,
    timing: window.__active.size, frameZ: document.getElementById('fone-dash') ? getComputedStyle(document.getElementById('fone-dash')).zIndex : null };
}"""


def ob_states(s):
    return [o.get("state") for o in (s.get("ob") or [])]


def rect_of(slot):
    return [slot["x"], slot["y"], slot["w"], slot["h"]]


async def main():
    replay = api(f"/api/f1tv/{REPLAY_CID}")
    check(replay["anchor"] == "multiviewer", f"server anchors the replay via MultiViewer (p0={replay['p0']})")
    p0 = replay["p0"]
    meta = json.load(open(f"{MEDIA}/meta.json"))

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(), channel="chromium", headless=True,  # full Chromium: the headless shell has no extensions
            args=["--load-extension=/ext", "--disable-extensions-except=/ext", "--autoplay-policy=no-user-gesture-required"],
        )
        sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
        # (track sound for the audio-track tests below; the default is to mute F1 TV instead)
        await sw.evaluate(f"chrome.storage.sync.set({{servers: ['{SERVER}'], f1Audio: 'fx'}})")

        blob = open(f"{MEDIA}/video.mp4", "rb").read()

        async def f1tv(route):
            url = route.request.url
            if not url.endswith("/media/video.mp4"):
                # F1 TV's own policy allows images from formula1.com and data: only (Bilibili's
                # stickers reach the danmaku as data: URLs, through the service worker)
                return await route.fulfill(body=TM_PAGE if "tm=1" in url else PAGE, content_type="text/html",
                                           headers={"content-security-policy": "img-src 'self' data:"})
            # Chrome only seeks freely in media served with byte ranges.
            rng = route.request.headers.get("range", "bytes=0-")
            a, _, b = rng.split("=", 1)[1].partition("-")
            a, b = int(a), int(b) if b else len(blob) - 1
            await route.fulfill(status=206, body=blob[a:b + 1], headers={
                "content-type": "video/mp4", "accept-ranges": "bytes",
                "content-range": f"bytes {a}-{b}/{len(blob)}"})

        await ctx.route("https://f1tv.formula1.com/**", f1tv)
        page = await ctx.new_page()
        if os.environ.get("E2E_ONLY") == "onboards":  # dev shortcut: Phase A3 alone
            await onboards(ctx, page)
            await ctx.close()
            print(f"\n{passed} checks passed (E2E_ONLY=onboards)")
            return

        # ---------------- Phase A: replay ----------------
        await page.goto(f"https://f1tv.formula1.com/detail/{REPLAY_CID}/2026-bahrain-gp-in-malaysia-qualifying?action=play")
        await page.wait_for_function("() => document.getElementById('v').readyState >= 3")
        await video(page, "v.currentTime = 120")
        st = await wait_status(page, lambda s: s.get("state") == "sync", 40, "first sync")
        check(st["clock"] == "multiviewer", "clock = exact MultiViewer anchor")
        check(st["delay"] == 40 and st["delaySource"] == "default", "uses the default 40 s delay before calibration")

        await asyncio.sleep(3)  # let rate-correction settle
        st = await status(page)
        vt = await video(page, "v.currentTime")
        check(abs(st["E"] - (p0 + vt)) < 0.6, f"on-screen time = p0 + video time (err {st['E'] - (p0 + vt):+.3f}s)")
        check(abs(st["W"] - st["E"] - 40) < 1e-6, "commentary time = on-screen time + delay")
        check(abs(st["m"] - (st["W"] - meta["wall_start"])) < 0.05, "commentary position = W - recording start")
        check(abs(st["drift"]) < 0.25, f"audio within 250 ms of target (drift {st['drift'] * 1000:+.0f} ms)")

        # seek forward 10 minutes
        await video(page, "v.currentTime = 720")
        t0 = time.time()
        st = await wait_status(page, lambda s: s.get("state") == "sync" and s["E"] - p0 > 715, 15, "sync after seek")
        check(time.time() - t0 < 6, f"re-synced {time.time() - t0:.1f}s after a 10-minute seek")
        check(abs(st["m"] - (p0 + 720 + 40 - meta["wall_start"])) < 3, "audio jumped to the matching commentary")

        # pause / resume
        await video(page, "v.pause()")
        st = await wait_status(page, lambda s: s.get("state") == "paused", 3, "paused")
        check(True, "commentary pauses with the video")
        await video(page, "v.play()")
        await wait_status(page, lambda s: s.get("state") == "sync", 8, "resume")
        check(True, "commentary resumes with the video")

        # hotkey nudge persists to the server as this video's calibration
        await page.keyboard.press("]")
        await page.keyboard.press("]")
        st = await wait_status(page, lambda s: abs(s.get("delay", 0) - 41) < 1e-6, 3, "nudged delay")
        await asyncio.sleep(1.5)
        saved = api(f"/api/sync/{REPLAY_CID}?channel={urllib.request.quote(CHANNEL)}&clock=multiviewer")
        check(saved == {"delay": 41.0, "source": "content"}, "two ']' presses → delay 41.0 s, saved for this video")
        other = api(f"/api/sync/999?channel={urllib.request.quote(CHANNEL)}&clock=multiviewer")
        check(other["delay"] == 41.0 and other["source"] == "channel", "...and becomes the starting point for other sessions")
        # the fine steps: 0.1 s (= / -) and 0.02 s (. / ,), for nulling an echo by ear
        for key, want in (("=", 41.1), (",", 41.08), (",", 41.06), ("-", 40.96)):
            await page.keyboard.press(key)
            st = await wait_status(page, lambda s, w=want: abs(s.get("delay", 0) - w) < 1e-6, 3, f"delay {want} after '{key}'")
        check(abs(st["delay"] - 40.96) < 1e-6, "fine nudges: '=' +0.1, ',' -0.02 twice, '-' -0.1 -> delay 40.96 s")
        for key in ("]", "]", "-", "=", ".", ".", "."):  # back to 41.0 with the panel's 1 ms rounding
            await page.keyboard.press(key)
        await wait_status(page, lambda s: abs(s.get("delay", 0) - 42.02) < 1e-6, 3, "delay back at 42.02")
        await page.keyboard.press("[")
        await page.keyboard.press("[")
        await page.keyboard.press(",")
        st = await wait_status(page, lambda s: abs(s.get("delay", 0) - 41.0) < 1e-6, 3, "delay back at 41.0")
        check(True, "...and the steps add up exactly (41.00 s again)")

        # A plain <video> (F1 TV's legacy Bitmovin player) has no sync PID trimming its rate: a speed
        # set on it (a speed extension; F1 TV's own menu is off there) is followed at once with pitch
        # kept, even one within Tiledmedia's trim reach of the last (1x -> 1.25x).
        f0 = (await status(page))["fixes"]
        await video(page, "v.playbackRate = 1.25")
        await wait_status(page, lambda s: s.get("fixes", {}).get("pp") and s["fixes"]["rate"] == 1.25, 5, "commentary at 1.25x")
        await asyncio.sleep(4)
        st = await status(page)
        f1 = st["fixes"]
        check(st["state"] == "sync" and abs(st["drift"]) < 0.25 and f1["rate"] == 1.25 and f1["pp"]
              and f1["nSpeeds"] - f0["nSpeeds"] == 1 and f1["nSeeks"] - f0["nSeeks"] <= 1,
              f"plain <video> at 1.25x: commentary x{f1['rate']:.3f} (pitch kept {f1['pp']}), {f1['nSpeeds'] - f0['nSpeeds']} speed "
              f"change, {f1['nSeeks'] - f0['nSeeks']} seeks, drift {st['drift'] * 1000:+.0f} ms")
        await video(page, "v.playbackRate = 1")
        st = await wait_status(page, lambda s: s.get("state") == "sync" and s.get("fixes", {}).get("pp") is False
                               and abs(s["fixes"]["rate"] - 1) <= 0.021, 5, "commentary back at 1x")  # steer's tweaks: <= 2%
        check(True, f"...and back at 1x, resampled again (pitch not kept, drift {st['drift'] * 1000:+.0f} ms)")

        await danmaku(page, sw, meta)

        # past the end of the recording
        end_p = meta["wall_start"] + meta["duration"] - 41 - p0 + 30
        await video(page, f"v.currentTime = {end_p}")
        await wait_status(page, lambda s: s.get("state") == "after", 8, "after end")
        check(True, "reports when the commentary recording has ended")

        # ---------------- Phase A2: the real F1 TV player (Tiledmedia) ----------------
        await page.goto(f"https://f1tv.formula1.com/detail/{REPLAY_CID}/2026-bahrain-gp-in-malaysia-qualifying?action=play&tm=1")
        await page.wait_for_function("() => document.getElementById('v').readyState >= 3")
        await asyncio.sleep(2)
        await page.evaluate("() => tmSeek(720)")
        st = await wait_status(page, lambda s: s.get("state") == "sync" and s["E"] - p0 > 715, 30, "sync on Tiledmedia")
        await asyncio.sleep(2)
        st = await status(page)
        content, element = await page.evaluate("() => [contentPos(), document.getElementById('v').currentTime]")
        check(content - element > 600, f"mock reproduces F1 TV: element clock {element:.0f}s vs content {content:.0f}s")
        check(abs(st["E"] - (p0 + content)) < 0.8, f"Tiledmedia: on-screen time follows the content clock, not <video> (err {st['E'] - (p0 + content):+.2f}s)")
        check(abs(st["drift"]) < 0.25, f"Tiledmedia: audio in sync (drift {st['drift'] * 1000:+.0f} ms)")
        if os.environ.get("E2E_DIAG_PROBE"):
            for _ in range(3):
                await asyncio.sleep(6)
                print("DIAG-PROBE", json.dumps((await status(page)).get("diag")), json.dumps(api("/api/diag?limit=3"))[:600], flush=True)
        # F1 TV starts every content on the channel's audioDefault (F1 LIVE English commentary).
        await wait_js(page, "() => __curAudio()", lambda n: n == "Track FX", 10, "FX on load")
        log = await page.evaluate("() => window.__audioLog")
        check(log[0] == "English" and log[-1] == "Track FX",
              f"track sound: F1 TV's English commentary switched to 'Track FX' on load ({' -> '.join(log)})")
        kv = await wait_js(page, DETAILS, lambda t: "Track FX (auto)" in t, 5, "Details audio row")
        check("English Int. (eng)" in kv and "Track FX (fx)" in kv and "VER (tea)" in kv,
              "Details: 'audio Track FX (auto)' and F1 TV's tracks listed")
        # The SDK owns <video>.playbackRate (rate x sync multiplier, rewritten every sync step).
        await video(page, "v.playbackRate = 2")
        await asyncio.sleep(1.2)
        check(await video(page, "v.playbackRate") == 1, "mock reproduces the SDK: a direct <video>.playbackRate write doesn't stick")
        # The real player trims <video>.playbackRate itself (Tiledmedia's view-sync PID). v0.2.1
        # copied each trim to the commentary, and every rate change of the <audio> popped.
        # (Counted from a precise, settled position: after the 12-minute seek above the position
        # is coarse ('tm') for a few seconds, and the engine re-locks once it is precise again.)
        await page.evaluate(f"() => {PANEL}.getElementById('details').open = true")
        await wait_js(page, f"() => {PANEL}.getElementById('kv').innerText", lambda t: "src tm+mse" in t, 15, "a precise position source")
        await asyncio.sleep(2.5)
        st = await wait_status(page, lambda s: s.get("state") == "sync", 10, "sync before trims")
        f0 = st["fixes"]
        await page.evaluate("() => { window.__syncSeq = [1.004, 0.997, 1.002, 0.999]; }")
        await asyncio.sleep(6)
        st = await status(page)
        await page.evaluate("() => { window.__syncSeq = [1]; }")
        f1 = st["fixes"]
        check(st["state"] == "sync" and f1["nSeeks"] == f0["nSeeks"] and f1["nRates"] - f0["nRates"] <= 2 and not f1["pp"],
              f"Tiledmedia: 12 player rate trims in 6 s -> {f1['nRates'] - f0['nRates']} commentary rate changes, "
              f"{f1['nSeeks'] - f0['nSeeks']} seeks, pitch kept {f1['pp']} (drift {st['drift'] * 1000:+.0f} ms)")
        # The trims aren't always small: in the seconds after a seek or a content load the real
        # element ran at x1.18, x0.75, x1.25 while the SDK still played at 1x. v0.2.6 took every
        # trim past 3% for a speed: the commentary time-stretched (pitch kept) at x1.18, x0.945,
        # ..., and each of those rate changes, and each preservesPitch flip, clicked.
        st = await wait_status(page, lambda s: s.get("state") == "sync", 10, "sync before big trims")
        f0, pps, worst = st["fixes"], set(), 0
        await page.evaluate("() => { window.__syncSeq = [1.18]; }")
        for i in range(30):  # 3 s at x1.18, then 3 s at x0.75
            if i == 15:
                await page.evaluate("() => { window.__syncSeq = [0.75]; }")
            await asyncio.sleep(0.2)
            st = await status(page)
            pps.add(st["fixes"]["pp"])
            worst = max(worst, abs(st.get("drift") or 0))
        el, sdk = await page.evaluate("() => [document.getElementById('v').playbackRate, __sdkRate()]")
        await page.evaluate("() => { window.__syncSeq = [1]; }")
        f1 = st["fixes"]
        check(el == 0.75 and sdk == 1 and pps == {False} and f1.get("nSpeeds") == f0.get("nSpeeds"),
              f"Tiledmedia: element at x1.18 then x0.75 for 6 s, SDK at 1x -> pitch kept {sorted(pps)}, "
              f"{f1.get('nSpeeds', 0) - f0.get('nSpeeds', 0)} speed changes, {f1['nRates'] - f0['nRates']} rate tweaks, "
              f"{f1['nSeeks'] - f0['nSeeks']} seeks")
        st = await wait_status(page, lambda s: s.get("state") == "sync" and abs(s["drift"]) < 0.25, 10, "sync after big trims")
        content = await page.evaluate("() => contentPos()")
        check(worst < 1.0 and abs(st["E"] - (p0 + content)) < 0.8,
              f"...and sync held (worst drift {worst * 1000:.0f} ms while trimmed, on-screen err {st['E'] - (p0 + content):+.2f}s)")
        # (synthetic commentary covers replay positions ~-300..1500 s)
        await page.evaluate("() => tmSeek(1450)")
        t0 = time.time()
        st = await wait_status(page, lambda s: s.get("state") == "sync" and abs(s["E"] - p0 - 1450) < 10, 10, "follow a Tiledmedia seek")
        check(time.time() - t0 < 6, f"Tiledmedia: followed a 12-minute forward seek in {time.time() - t0:.1f}s")
        await page.evaluate("() => tmSeek(300)")
        st = await wait_status(page, lambda s: s.get("state") == "sync" and abs(s["E"] - p0 - 300) < 10, 10, "follow a backwards seek")
        check(True, "Tiledmedia: followed a backwards seek")
        # The stats dashboard in a window of its own follows the engine's on-screen time, and
        # shows the session's timing as of that moment (the server's copy of F1's archive).
        dash = await ctx.new_page()
        await dash.goto(f"chrome-extension://{sw.url.split('/')[2]}/dashboard.html")

        async def dash_wait(pred, timeout, what):
            deadline, d = time.time() + timeout, {}
            while time.time() < deadline:
                d = await dash.evaluate("() => globalThis.__dash?.snapshot()") or {}
                s = await status(page)
                if d.get("E") is not None and s.get("E") is not None and pred(d, s):
                    return d, s
                await asyncio.sleep(0.2)
            raise AssertionError(f"timed out waiting for {what}: dashboard {json.dumps(d)[:400]}")

        d, s = await dash_wait(lambda d, s: abs(d["E"] - s["E"]) < 1.0, 15, "the dashboard to follow the on-screen time")
        check(True, f"stats window follows the on-screen time (err {d['E'] - s['E']:+.2f}s)")
        d, s = await dash_wait(lambda d, s: d["rows"] >= 20 and d.get("stateT") is not None and abs(d["stateT"] - d["E"]) < 3,
                               60, "the dashboard's timing tower")
        check(d["stateT"] <= d["E"] + 0.5, f"stats: {d['rows']}-driver tower as of the on-screen time, never past it "
              f"(state t {d['stateT'] - d['E']:+.2f}s), {d['part'] or 'no part'} {d['clock']}, flag {d['flag']}")
        await page.evaluate("() => tmSeek(900)")
        t0 = time.time()
        d, s = await dash_wait(lambda d, s: abs(d["E"] - p0 - 900) < 3 and abs(d["E"] - s["E"]) < 1.0, 10, "the dashboard to follow a seek")
        check(time.time() - t0 < 4, f"stats followed a 10-minute seek in {time.time() - t0:.1f}s (err {d['E'] - s['E']:+.2f}s)")
        await dash.close()
        # Dragging the seek bar: the label shows the drag target while the video keeps playing.
        await page.evaluate("() => { window.__scrub = 1400; }")
        worst = 0
        for _ in range(16):
            await asyncio.sleep(0.25)
            st = await status(page)
            content = await page.evaluate("() => contentPos()")
            worst = max(worst, abs(st["E"] - (p0 + content)))
        await page.evaluate("() => { window.__scrub = null; }")
        check(worst < 1.0, f"Tiledmedia: holding the seek bar for 4 s doesn't move the commentary (worst err {worst:.2f}s)")
        # Feed/view switch: the old timing listener must be unregistered, not leaked.
        await page.evaluate("() => { document.getElementById('v').style.display = 'none'; }")
        await asyncio.sleep(1.5)
        active = await page.evaluate("() => [...window.__active]")
        await page.evaluate("() => { document.getElementById('v').style.display = 'initial'; }")
        await asyncio.sleep(1.5)
        active2 = await page.evaluate("() => [...window.__active]")
        check(len(active) == 1 and len(active2) == 1 and active != active2,
              f"Tiledmedia: exactly one timing listener across view switches ({active} -> {active2})")
        st = await wait_status(page, lambda s: s.get("state") == "sync", 10, "sync after view switch back")
        content = await page.evaluate("() => contentPos()")
        check(abs(st["E"] - (p0 + content)) < 1.0, "Tiledmedia: back in sync after switching views back")
        check(await page.evaluate("() => window.__rateCalls.length") == 0, "speed 1x: page.js never touched the player's rate")

        # Channel switch: F1 TV loads the channel's content item and its audioDefault again.
        await page.evaluate("() => tmSwitchFeed(1003, 'TRACKER')")
        check(await page.evaluate("() => __curAudio()") == "English", "mock: a channel switch puts F1 TV back on English commentary")
        t0 = time.time()
        await wait_js(page, "() => __curAudio()", lambda n: n == "Track FX", 10, "FX after a feed switch")
        check(True, f"track sound: 'Track FX' re-selected {time.time() - t0:.1f}s after a feed switch")
        st = await wait_status(page, lambda s: s.get("state") == "sync", 10, "sync after a feed switch")
        content = await page.evaluate("() => contentPos()")
        check(abs(st["E"] - (p0 + content)) < 1.0, "still in sync after the feed switch")

        # ---- 2x on a replay: Player.changePlaybackRate; the commentary follows at 2x ----
        await page.evaluate("() => { window.__syncSeq = [1.004, 0.997, 1.002, 0.999]; }")  # the PID keeps trimming
        await wait_js(page, f"() => !{PANEL}.getElementById('speedRow').hidden", bool, 5, "speed buttons on a replay")
        await page.keyboard.press(">")
        await page.keyboard.press(">")
        speed = await sw.evaluate("chrome.storage.sync.get('speed').then((s) => s.speed)")
        check(speed == 2, "'>' twice -> speed 2x, saved in chrome.storage")
        await wait_js(page, "() => __sdkRate()", lambda r: r == 2, 5, "SDK at 2x")
        calls = await page.evaluate("() => window.__rateCalls")
        el = await video(page, "v.playbackRate")
        check(calls[-1] == 2 and set(calls) <= {1.5, 2} and abs(el / 2 - 1) < 0.01,
              f"Tiledmedia plays at 2x: changePlaybackRate{calls}, element x{el:.3f} (2 x sync trim)")
        st = await wait_status(page, lambda s: s.get("state") == "sync" and abs(s["fixes"]["rate"] - 2) < 0.05, 15, "commentary at 2x")
        await asyncio.sleep(2)
        f0 = (await status(page))["fixes"]
        await asyncio.sleep(6)
        st = await status(page)
        content = await page.evaluate("() => contentPos()")
        f1 = st["fixes"]
        check(st["state"] == "sync" and abs(f1["rate"] - 2) < 0.05 and f1["pp"],
              f"2x: commentary audio plays at x{f1['rate']:.3f}, pitch kept")
        check(abs(st["drift"]) < 0.25 and abs(st["E"] - (p0 + content)) < 1.2,
              f"2x: in sync (drift {st['drift'] * 1000:+.0f} ms, on-screen err {st['E'] - (p0 + content):+.2f}s)")
        check(f1["nSeeks"] - f0["nSeeks"] <= 1 and f1["nRates"] - f0["nRates"] <= 4 and f1["nSpeeds"] == f0["nSpeeds"],
              f"2x with sync trims: {f1['nRates'] - f0['nRates']} commentary rate changes, {f1['nSeeks'] - f0['nSeeks']} seeks, "
              f"{f1['nSpeeds'] - f0['nSpeeds']} speed changes in 6 s")
        # At a speed the real view sync swings the element: ~13% fast for 2 s, ~13% slow for 2 s
        # (a Mac at 1.5x, 2026-10-10), the picture ±0.25 s around its pace. v0.2.16 re-locked on
        # each swing's peak and seeked the commentary every ~2 s, both ways, for as long as it lasted.
        await page.evaluate("() => { window.__syncSeq = [1.13, 1.13, 1.13, 1.13, 0.87, 0.87, 0.87, 0.87]; }")
        f0, worst = (await status(page))["fixes"], 0
        for i in range(80):  # 16 s: four swings each way
            await asyncio.sleep(0.2)
            st = await status(page)
            worst = max(worst, abs(st.get("drift") or 0))
        await page.evaluate("() => { window.__syncSeq = [1.004, 0.997, 1.002, 0.999]; }")
        f1 = st["fixes"]
        check(st["state"] == "sync" and f1["nSeeks"] == f0["nSeeks"] and f1["nSpeeds"] == f0["nSpeeds"] and abs(f1["rate"] - 2) < 0.05
              and worst < 0.75,
              f"2x while the view sync swings the picture ±13% every 2 s: {f1['nSeeks'] - f0['nSeeks']} seeks, "
              f"{f1['nSpeeds'] - f0['nSpeeds']} speed changes in 16 s, commentary within {worst:.2f}s of the picture")
        await page.evaluate("() => tmSeek(600)")
        t0 = time.time()
        await wait_status(page, lambda s: s.get("state") == "sync" and abs(s["E"] - p0 - 600) < 20, 10, "follow a seek at 2x")
        dt = time.time() - t0
        await asyncio.sleep(1.5)
        st = await status(page)
        content = await page.evaluate("() => contentPos()")
        check(abs(st["E"] - (p0 + content)) < 1.2 and abs(st["fixes"]["rate"] - 2) < 0.05 and abs(st["drift"]) < 0.25,
              f"2x: followed a 5-minute seek in {dt:.1f}s and stays at 2x (err {st['E'] - (p0 + content):+.2f}s, drift {st['drift'] * 1000:+.0f} ms)")
        # A feed switch gives the video a fresh MSE handle at 1x: page.js puts 2x back.
        sp0 = (await status(page))["fixes"].get("nSpeeds", 0)
        reset = await page.evaluate("() => { const done = tmSwitchFeed(1025, 'INTERNATIONAL'); const r = __sdkRate(); return done.then(() => r); }")
        check(reset == 1, "mock: the feed switch gives the video a new MSE handle at 1x")
        t0 = time.time()
        await wait_js(page, "() => __sdkRate()", lambda r: r == 2, 6, "2x after feed switch")
        check(True, f"2x re-applied {time.time() - t0:.1f}s after the SDK reset it")
        await wait_js(page, "() => __curAudio()", lambda n: n == "Track FX", 10, "FX on INTERNATIONAL")
        check(await page.evaluate("() => window.__audioLog.slice(-2)") == ["English Int.", "Track FX"],
              "track sound: INTERNATIONAL's 'English Int.' -> 'Track FX'")
        # F1 TV audio 'muted' (the default): setAudioIsMuted, the call F1 TV's own mute button makes,
        # and no track switch at all; back to 'fx' unmutes and switches again
        a0 = await page.evaluate("() => window.__audioSelWrites")
        await sw.evaluate("chrome.storage.sync.set({f1Audio: 'mute'})")
        await wait_js(page, "() => window.__muteCalls.slice(-1)[0]", lambda m: m is True, 8, "F1 TV muted")
        await page.evaluate("() => tmSwitchFeed(1033, 'F1 LIVE')")  # a content load: F1 TV unmutes, fone mutes again
        await wait_js(page, "() => window.__muteCalls.length", lambda n: n >= 2, 8, "muted again after a content load")
        await asyncio.sleep(2)
        a1 = await page.evaluate("() => window.__audioSelWrites")
        check(await page.evaluate("() => [window.__player.audioMuted, __curAudio()]") == [True, "English"] and a1 - a0 <= 1,
              f"F1 TV audio 'muted': player muted, its track left alone ({a1 - a0 - 1} fone track writes; F1 TV's own load aside)")
        await sw.evaluate("chrome.storage.sync.set({f1Audio: 'fx'})")
        await wait_js(page, "() => window.__muteCalls.slice(-1)[0]", lambda m: m is False, 8, "F1 TV unmuted")
        await wait_js(page, "() => __curAudio()", lambda n: n == "Track FX", 12, "FX again after 'fx'")
        check(True, "...and 'track sound' unmutes and picks Track FX again")
        await page.evaluate("() => tmSwitchFeed(1025, 'INTERNATIONAL')")
        await wait_js(page, "() => __curAudio()", lambda n: n == "Track FX", 12, "FX on INTERNATIONAL again")
        st = await wait_status(page, lambda s: s.get("state") == "sync" and abs(s["fixes"]["rate"] - 2) < 0.05, 10, "2x sync after feed switch")
        # ...and the commentary rode out the moment at 1x (v0.2.16 followed it: two jumps).
        check(st["fixes"].get("nSpeeds", 0) == sp0, f"feed switch at 2x: the commentary stayed at 2x through the new handle's dip "
              f"({st['fixes'].get('nSpeeds', 0) - sp0} speed changes)")
        # If the core keeps its own rate across MSE handles, "2" again is a no-op for it: page.js
        # asks 1 (the picture is at 1x anyway: a real change for a core that believes 2) then 2.
        await page.evaluate("() => { window.__staleCore = true; }")
        n0 = await page.evaluate("() => window.__rateCalls.length")
        await page.evaluate("() => tmSwitchFeed(1003, 'TRACKER')")
        t0 = time.time()
        await wait_js(page, "() => __sdkRate()", lambda r: r == 2, 12, "2x with a core that ignores a repeated rate")
        calls = await page.evaluate(f"() => window.__rateCalls.slice({n0})")
        check(calls[-2:] == [1, 2] and 1.98 not in calls,
              f"core ignoring a repeated 2x: page.js asked 1 then 2 ({calls}), 2x back {time.time() - t0:.1f}s after the switch")
        # Two rebuilds in quick succession (a seek while onboard views attach, 2026-10-11): v0.2.26
        # re-asked the same neighbour value for 7 minutes; now it converges on each.
        n0 = await page.evaluate("() => window.__rateCalls.length")
        await page.evaluate("() => { tmSwitchFeed(1025, 'INTERNATIONAL'); setTimeout(() => tmSwitchFeed(1003, 'TRACKER'), 500); }")
        t0 = time.time()
        await wait_js(page, "() => __sdkRate()", lambda r: r == 2, 8, "2x after two rebuilds 500 ms apart")
        await asyncio.sleep(3.5)
        calls = await page.evaluate(f"() => window.__rateCalls.slice({n0})")
        check(await page.evaluate("() => __sdkRate()") == 2 and len(calls) <= 8,
              f"two rebuilds 500 ms apart: back at 2x {time.time() - t0:.1f}s later with {len(calls)} asks ({calls}), none since")
        await page.evaluate("() => { window.__staleCore = false; }")
        await wait_status(page, lambda s: s.get("state") == "sync" and abs(s["fixes"]["rate"] - 2) < 0.05, 10, "2x sync after TRACKER")

        # ---- a player still starting, a core that died: no calls into either ----
        # 'playing' before the video has its MSE handle: fone asks nothing (every player death seen
        # on 2026-10-11 came right after calls made then); once the handle is there it goes on.
        r0, a0, o0 = await page.evaluate("() => [window.__rateCalls.length, window.__audioSelWrites, window.__obCalls.length]")
        await page.evaluate("() => { window.__noHandle = true; return tmSwitchFeed(1025, 'INTERNATIONAL'); }")
        await asyncio.sleep(4.5)
        r1, a1, o1 = await page.evaluate("() => [window.__rateCalls.length, window.__audioSelWrites, window.__obCalls.length]")
        # (the one audio write and the one apply: F1 TV's own content load, not fone)
        check(r1 == r0 and a1 - a0 <= 1 and o1 - o0 <= 1,
              f"a player that says playing without a video handle: no speed or audio call for 4.5 s ({r1 - r0} rate asks, {a1 - a0 - 1} fone track writes, {o1 - o0 - 1} fone SDK calls)")
        await page.evaluate("() => { window.__noHandle = false; tmGiveHandle(); }")
        t0 = time.time()
        await wait_js(page, "() => __sdkRate()", lambda r: r == 2, 8, "2x once the handle exists")
        await wait_js(page, "() => __curAudio()", lambda n: n == "Track FX", 12, "FX once the player settled")
        check(True, f"...and once it has one: 2x after {time.time() - t0:.1f}s, then Track FX (settled player)")
        diag_t0 = time.time()
        evs = api(f"/api/diag?type=sdk&since={diag_t0 - 60}")["events"]
        names = [e.get("name") for e in evs if e.get("client")]
        check("f1tv.apply" in names and "rate" in names and "audio" in names,
              f"diagnostics: the SDK calls are logged, F1 TV's own applyDesiredState too ({sorted(set(names))})")
        # The core dies (F1 TV's app rebuilds the player ~100 ms later; here 3 s): the fatal error
        # reaches the diagnostics, nothing more is asked of the dead player, the new one gets 2x.
        await page.evaluate("() => tmFatal(-1, 'the core stopped (internally-stopped)')")
        await asyncio.sleep(0.5)
        r0 = await page.evaluate("() => window.__rateCalls.length")
        await asyncio.sleep(3)
        r1 = await page.evaluate("() => window.__rateCalls.length")
        check(r1 == r0, f"core stopped: no more speed calls into the dead player ({r1 - r0} in 3 s)")
        deadline = time.time() + 12  # the engine posts its diagnostics every 5 s
        evs = []
        while time.time() < deadline and not evs:
            evs = [e for e in api(f"/api/diag?type=fatal&since={diag_t0}")["events"] if e.get("code") == -1 and "internally-stopped" in str(e.get("msg"))]
            if not evs:
                await asyncio.sleep(1)
        check(bool(evs), f"diagnostics: the core's fatal error was recorded ({[(e.get('code'), e.get('msg'), e.get('busy'), e.get('last')) for e in evs][-1:]})")
        await page.evaluate("() => tmRebuildPlayer(300)")
        t0 = time.time()
        await wait_js(page, "() => __sdkRate()", lambda r: r == 2, 10, "2x on the rebuilt player")
        await wait_status(page, lambda s: s.get("state") == "sync", 10, "sync on the rebuilt player")
        deadline = time.time() + 12
        clean = None
        while time.time() < deadline and clean is None:  # the error text goes with the dead player
            snaps = [e for e in api(f"/api/diag?type=snap&since={t0}")["events"] if (e.get("video") or {}).get("dx", {}).get("r")]
            if snaps and snaps[-1]["video"]["dx"]["r"].get("sdk") == 2:
                clean = snaps[-1]["video"]["dx"]["r"].get("err") in (None, "")
            await asyncio.sleep(1)
        check(clean, f"F1 TV rebuilt the player: 2x again {time.time() - t0:.1f}s later, the error text gone")
        # The renderer froze (the computer slept): the 2 s commands froze too, which is not the
        # extension going away; the speed is not undone when it comes back.
        n0 = await page.evaluate("() => window.__rateCalls.length")
        await page.evaluate("() => { const t = Date.now(); while (Date.now() - t < 12000) { /* frozen */ } }")
        await asyncio.sleep(2)
        calls = await page.evaluate(f"() => window.__rateCalls.slice({n0})")
        check(1 not in calls and await page.evaluate("() => __sdkRate()") == 2, f"a 12 s renderer freeze: the speed stays at 2x ({calls or 'no asks'})")
        await page.evaluate("() => { window.__syncSeq = [1]; }")
        # The panel's buttons do the same.
        await page.click("#fone-overlay button[data-s='1.5']")
        await wait_js(page, "() => __sdkRate()", lambda r: r == 1.5, 5, "1.5x from the panel")
        st = await wait_status(page, lambda s: s.get("state") == "sync" and abs(s["fixes"]["rate"] - 1.5) < 0.04, 10, "commentary at 1.5x")
        check(True, f"panel 1.5x button: F1 TV and the commentary at 1.5x (drift {st['drift'] * 1000:+.0f} ms)")
        await page.click("#fone-overlay button[data-s='2']")
        await wait_js(page, "() => __sdkRate()", lambda r: r == 2, 5, "2x from the panel")

        # You pick another track in F1 TV's menu: that's kept for this video.
        await page.click("#pick-eng")
        await wait_js(page, "() => __curAudio()", lambda n: n == "English", 3, "manual pick")
        await asyncio.sleep(5)
        check(await page.evaluate("() => __curAudio()") == "English", "a manual pick (English) is not overridden")
        kv = await wait_js(page, DETAILS, lambda t: "English (your pick" in t, 3, "Details manual row")
        check(True, "Details: audio 'English (your pick — kept for this video)'")
        await page.evaluate("() => tmSwitchFeed(1033, 'F1 LIVE')")
        await asyncio.sleep(4)
        check(await page.evaluate("() => __curAudio()") == "English", "...and after a feed switch, still left alone for this video")
        # Live on Tiledmedia: edge-relative. The player's wall clock on live reads ~40 s late
        # (packaging time), so E comes from "now - latency - distance behind the live edge"
        # and the live default delay keeps the commentary a few seconds behind ITS edge.
        await page.goto(f"https://f1tv.formula1.com/detail/{RACE_CID}/x?action=play&tm=1&ev=live")
        await page.wait_for_function("() => document.getElementById('v').readyState >= 3")
        await page.evaluate("() => { window.__event = 'live'; window.__origin = Date.now() - 5000 - contentPos() * 1000; }")
        st = await wait_status(page, lambda s: s.get("clock") == "edge", 20, "edge clock on Tiledmedia live")
        await asyncio.sleep(1.5)
        st = await status(page)
        # mock: live edge is 20 s ahead of the playhead
        check(abs(st["E"] - (time.time() - 30 - 20)) < 1.5 and st["delaySource"] == "live" and abs(st["delay"] - 23) < 1e-6,
              f"Tiledmedia live: edge-to-edge clock, live default delay {st['delay']} (E err {st['E'] - (time.time() - 50):+.2f}s), wall clock ignored")
        # Live lock: F1 TV's live edge advances in 6 s steps; the commentary must not chase it.
        await page.evaluate("() => { window.__stair = 6; }")
        st = await wait_status(page, lambda s: s.get("state") == "sync", 30, "live lock in sync")
        seeks0 = (st.get("fixes") or {}).get("nSeeks", 0)
        m0, t0 = st.get("m"), time.time()
        await asyncio.sleep(12)
        st = await status(page)
        seeks1 = (st.get("fixes") or {}).get("nSeeks", 0)
        adv = st["m"] - m0
        check(st["state"] == "sync" and seeks1 - seeks0 == 0 and abs(adv - (time.time() - t0)) < 1.0,
              f"live lock: stepped F1 TV live edge -> 0 commentary seeks in 12 s, advanced {adv:.1f}s ({st['text']})")
        # Rewind F1 TV 30 s: the commentary goes back 30 s with it.
        before = st["m"]
        await page.evaluate("() => tmSeek(contentPos() - 30)")
        st = await wait_status(page, lambda s: s.get("state") == "sync" and s.get("m") is not None and abs((before - s["m"]) - 30) < 3, 10, "live lock follows F1 TV seek")
        check(True, f"live lock: rewinding F1 TV 30 s moved the commentary back {before - st['m']:.1f}s")
        await page.evaluate("() => { window.__stair = 0; }")
        # The replay speed setting (2x) doesn't apply to live.
        await page.keyboard.press(">")
        await asyncio.sleep(1)
        calls, rate = await page.evaluate("() => [window.__rateCalls, __sdkRate()]")
        hidden = await page.evaluate(f"() => {PANEL}.getElementById('speedRow').hidden")
        check(calls == [] and rate == 1 and hidden, f"live: plays at 1x though replays are set to 2x (calls {calls}, no speed buttons)")
        await page.evaluate("() => { window.__event = 'finished-live'; }")
        await asyncio.sleep(3)
        st = await status(page)
        check(st["clock"] == "edge" and abs(st["E"] - (time.time() - 50)) < 1.5,
              f"Tiledmedia: when the live event finishes, timing stays edge-relative (err {st['E'] - (time.time() - 50):+.2f}s)")

        await sw.evaluate("chrome.storage.sync.set({speed: 1})")

        # Diagnostics: the engine's log of playback reached the server (GET /api/diag), with F1
        # TV's own side of the speed in it (what page.js reads off the player).
        await asyncio.sleep(6)  # one more flush
        engine_diag = (await status(page)).get("diag")
        dg = api("/api/diag?limit=5000")
        evs = dg["events"]
        types = {e["type"] for e in evs}
        snaps = [e for e in evs if e["type"] == "snap"]
        tm_snaps = [e for e in snaps if (e.get("video") or {}).get("dx")]
        check({"snap", "state", "speed", "seek"} <= types and tm_snaps and "ps" in tm_snaps[-1]["video"]["dx"]["r"]
              and all(e.get("build") for e in evs) and len(dg["clients"]) == 1,
              f"diagnostics reach the server: {len(evs)} events {sorted(types)}, F1 TV's speed report "
              f"{json.dumps(tm_snaps[-1]['video']['dx']['r']) if tm_snaps else None} (engine: {engine_diag})")

        # ---------------- Phase B: live commentary, video ahead of it ----------------
        live = api(f"/api/f1tv/{LIVE_CID}")
        tl = api(f"/api/timeline?channel={urllib.request.quote(CHANNEL)}&from={time.time() - 600}&to={time.time() + 600}")
        check(tl["live"], "server sees the growing recording as live")
        # Choose a delay that puts p=60 twelve seconds past the commentary's live edge.
        delay = (tl["edge"] + 12) - (live["p0"] + 60)
        api(f"/api/sync/{LIVE_CID}", {"channel": CHANNEL, "clock": "multiviewer", "delay": delay}, "PUT")
        await page.goto(f"https://f1tv.formula1.com/detail/{LIVE_CID}/2026-azerbaijan-gp-race?action=play")
        await page.wait_for_function("() => document.getElementById('v').readyState >= 3")
        await video(page, "v.currentTime = 60")
        st = await wait_status(page, lambda s: s.get("state") == "waiting", 20, "waiting for live commentary")
        check(st["lead"] < 0, f"video is ahead of the live commentary (lead {st['lead']:.1f}s)")
        await wait_status(page, lambda s: s.get("holding"), 5, "auto-hold")
        check(await video(page, "v.paused"), "auto-hold paused F1 TV")
        t0 = time.time()
        await wait_status(page, lambda s: s.get("state") == "sync", 45, "caught up")
        waited = time.time() - t0
        check(not await video(page, "v.paused"), f"F1 TV resumed by itself after {waited:.0f}s, in sync with live commentary")
        await asyncio.sleep(4)
        st = await status(page)
        check(st["state"] == "sync" and abs(st["drift"]) < 0.25 and st["lead"] > 0,
              f"stays in sync just behind the live edge (lead {st['lead']:.1f}s, drift {st['drift'] * 1000:+.0f} ms)")

        await onboards(ctx, page)
        await ctx.close()
    print(f"\n{passed} checks passed")


DM_HOST = "document.getElementById('fone-danmaku')"
DM_LEAD, DM_DUR = 2, 8  # dmcore.js: shown 2 s before their send time, 8 s across the picture


async def dm_state(page):
    raw = await page.evaluate(f"() => {DM_HOST}?.dataset.state || null")
    return json.loads(raw) if raw else {}


def dm_index(text):
    return int(text[1:5]) if text.startswith("弹") else None


async def danmaku(page, sw, meta):
    """The room's danmaku over the picture (danmaku.js), on the commentary's clock: the run.sh
    fixture has "弹NNNN" sent at wall_start + 2 N (ul N % 20, "好快" in every third)."""
    ws = meta["wall_start"]
    show = lambda i: ws + 2 * i - DM_LEAD
    s = await wait_js(page, f"() => {DM_HOST}?.dataset.state || null",
                      lambda raw: bool(raw) and json.loads(raw)["shown"] >= 3, 15, "danmaku on screen")
    s = await dm_state(page)
    st = await status(page)
    clock = s["clock"]
    # (the overlay's state is written once a second: up to 1 s older than the status)
    check(abs(clock - st["W"]) < 1.6, f"danmaku clock follows the commentary (overlay {clock:.2f}, engine W {st['W']:.2f})")
    idx = sorted(i for i in map(dm_index, s["texts"]) if i is not None)
    newest = max(i for i in range(meta["duration"] // 2) if show(i) <= clock)
    check(idx and all(clock - DM_DUR - 0.6 <= show(i) <= clock + 0.6 for i in idx) and newest - idx[-1] <= 1,
          f"danmaku on screen are the ones sent around the commentary's moment ({idx}, newest due #{newest}), none early")
    imgs = await wait_js(page, f"""() => [...{DM_HOST}.shadowRoot.querySelectorAll('.dm img')]
        .map((i) => [i.className, i.src.slice(0, 11), i.complete && i.naturalWidth > 0])""",
        lambda v: any(c == "emo" and ok for c, _, ok in v) and any(c == "" and ok for c, _, ok in v), 20, "stickers and emoji drawn")
    check(all(src == "data:image/" for _, src, ok in imgs if ok),
          f"stickers and [dog] emoji drawn as images despite the page's img-src policy ({len(imgs)} images, as data: URLs)")
    rect = s["rect"]
    vr = await page.evaluate("() => { const r = document.getElementById('v').getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; }")
    check(rect and rect["x"] >= vr[0] - 1 and rect["y"] >= vr[1] - 1 and rect["x"] + rect["w"] <= vr[0] + vr[2] + 1
          and rect["y"] + rect["h"] <= vr[1] + vr[3] + 1 and s["lanes"] >= 3,
          f"over the picture: {rect} inside the video {vr}, {s['lanes']} lanes in the top half")

    # paused: the danmaku stop where they are; playing again, they go on
    pos = f"() => [...{DM_HOST}.shadowRoot.querySelectorAll('.dm')].map((e) => e.style.transform).join('|')"
    await video(page, "v.pause()")
    await wait_status(page, lambda s: s.get("state") == "paused", 3, "paused for danmaku")
    await asyncio.sleep(0.6)
    a = await page.evaluate(pos)
    await asyncio.sleep(1.0)
    b = await page.evaluate(pos)
    check(a and a == b, "paused: the danmaku hold still")
    await video(page, "v.play()")
    await wait_status(page, lambda s: s.get("state") == "sync", 8, "resume after danmaku pause")
    await asyncio.sleep(1.0)
    check(await page.evaluate(pos) != b, "...and move on with the commentary")

    # Bilibili's filters, from chrome.storage.sync like the settings panel saves them
    await sw.evaluate("chrome.storage.sync.set({danmaku: {minUL: 10}})")
    await asyncio.sleep(1.5)
    idx = [i for i in map(dm_index, (await dm_state(page))["texts"]) if i is not None]
    check(idx and all(i % 20 >= 10 for i in idx), f"用户等级 < 10 blocked: only levels 10+ on screen ({[i % 20 for i in idx]})")
    await sw.evaluate("chrome.storage.sync.set({danmaku: {keywords: ['好快']}})")
    await asyncio.sleep(1.5)
    texts = (await dm_state(page))["texts"]
    check(texts and not any("好快" in t for t in texts), f"关键词 '好快' blocked ({texts[-4:]})")
    await sw.evaluate("chrome.storage.sync.set({danmaku: {}})")

    # D turns them off and on; ⚙ in fone's panel opens Bilibili-style settings
    await page.keyboard.press("d")
    s = await wait_js(page, f"() => JSON.parse({DM_HOST}.dataset.state || '{{}}')", lambda s: s.get("on") is False and s["shown"] == 0, 3, "danmaku off")
    check(await page.evaluate(f"() => {DM_HOST}.shadowRoot.getElementById('stage').hidden"), "D: danmaku off, nothing drawn")
    await page.keyboard.press("d")
    await wait_js(page, f"() => JSON.parse({DM_HOST}.dataset.state || '{{}}')", lambda s: s.get("on") and s["shown"] >= 2, 5, "danmaku on")
    check(True, "D again: back on")
    await page.evaluate(f"() => {PANEL}.getElementById('dmSet').click()")
    labels = await page.evaluate(f"() => {DM_HOST}.shadowRoot.getElementById('panel').hidden ? null : {DM_HOST}.shadowRoot.getElementById('panel').innerText")
    check(labels and all(k in labels for k in ("显示区域", "不透明度", "用户等级", "荣耀等级", "关键词屏蔽", "描边类型")),
          "⚙ opens the 弹幕设置 panel (显示区域, 不透明度, 等级屏蔽, 关键词, 描边…)")
    await page.evaluate(f"() => {DM_HOST}.shadowRoot.getElementById('pclose').click()")

    # a seek: the danmaku of the new moment, at once
    vt = await video(page, "v.currentTime")
    await video(page, f"v.currentTime = {vt + 60}")
    await wait_status(page, lambda s: s.get("state") == "sync", 10, "sync after a seek (danmaku)")
    await asyncio.sleep(1.5)
    s = await dm_state(page)
    idx = sorted(i for i in map(dm_index, s["texts"]) if i is not None)
    check(idx and all(s["clock"] - DM_DUR - 0.6 <= show(i) <= s["clock"] + 0.6 for i in idx),
          f"after a 60 s seek: the danmaku of the new moment ({idx[:3]}…)")


async def onboards(ctx, page):
    """Phase A3: the dashboard's driver onboards, shown by page.js with F1 TV's own player (mock)."""
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.add_init_script(OB_INIT)
    await page.goto(f"https://f1tv.formula1.com/detail/{REPLAY_CID}/2026-bahrain-gp-in-malaysia-qualifying?action=play&tm=1&grid=1")
    await page.wait_for_function("() => document.getElementById('v').readyState >= 3")
    await wait_js(page, "() => __curAudio()", lambda n: n == "Track FX", 15, "FX before the onboards")
    ob = lambda: page.evaluate(OB_STATE)  # noqa: E731

    async def ob_wait(pred, timeout, what):
        return await wait_js(page, OB_STATE, lambda s: bool(s.get("ob") is not None and pred(s)), timeout, what)

    # ---- from the dashboard iframe, through layout.js ----
    await page.keyboard.press("KeyS")  # the split view: F1 TV's player top-left, the real dashboard around it
    dash = await (await page.wait_for_selector("#fone-dash", state="attached", timeout=10000)).content_frame()
    await dash.wait_for_load_state("load")
    base = await ob()
    A = {"x": 1000, "y": 10, "w": 272, "h": 153, "channelId": 1016}
    B = {"x": 1000, "y": 173, "w": 272, "h": 153, "channelId": 1009}

    async def dash_post(slots):
        await dash.evaluate("(s) => parent.postMessage({type: 'fone-layout-cmd', cmd: 'onboards', slots: s, __e2e: 1}, '*')", slots)

    t0 = time.time()
    await dash_post([A, B])
    st = await ob_wait(lambda s: ob_states(s) == ["ok", "ok"], 15, "both onboards shown")
    shown = time.time() - t0
    fwd, held = await page.evaluate("() => [window.__obCmds.at(-1), window.__dashOb.length]")
    check(fwd == {"cmd": "onboards", "slots": [A, B]},
          f"layout.js passes the dashboard's onboards command on to page.js (fone-cmd; {held} of the dashboard's own held back)")
    bx = st["boxes"]
    check(len(bx) == 2 and bx[0]["rect"] == rect_of(A) and bx[1]["rect"] == rect_of(B)
          and all(b["view"]["rect"] == b["rect"] for b in bx) and all(b["parent"] for b in bx),
          f"two boxes at the dashboard's rectangles {bx[0]['rect']} {bx[1]['rect']}, a view filling each ({shown:.1f}s to 'ok')")
    check(all(b["z"] == "101" and b["pe"] == "none" and b["radius"] == "10px" and b["bg"] == "rgb(0, 0, 0)"
              and b["overflow"] == "hidden" for b in bx) and st["frameZ"] == "99",
          "boxes: fixed at z 101 (player layer 100, dashboard 99), pointer-events none, black, rounded 10px, clipped")
    check(all(b["view"]["player"] and b["view"]["inPlayer"] for b in bx) and [b["view"]["ch"] for b in bx] == [1016, 1009]
          and [b["video"]["ch"] for b in bx] == ["1016", "1009"] and st["ours"] == 2 and st["views"] == 4,
          "our own tiledmedia-views, added to F1 TV's player, selected on the feeds with channel_id 1016 and 1009")
    t1 = [b["video"]["t"] for b in bx]
    await asyncio.sleep(1)
    st = await ob()
    check(all(b["video"]["t"] > t + 0.5 and not b["video"]["paused"] for b, t in zip(st["boxes"], t1)),
          "...and their pictures play")
    cx, cy = A["x"] + A["w"] / 2, A["y"] + A["h"] / 2
    on_top = await page.evaluate("""([x, y]) => { const b = document.querySelector('[data-fone-ob-box="0"]'); b.style.pointerEvents = 'auto';
        const e = document.elementFromPoint(x, y); b.style.pointerEvents = 'none'; return !!e && b.contains(e); }""", [cx, cy])
    under = await page.evaluate("([x, y]) => document.elementFromPoint(x, y)?.id", [cx, cy])
    check(on_top and under == "fone-dash", "drawn over the dashboard, while clicks there still reach the dashboard")
    check(st["mainSel"] == base["mainSel"] and st["audioSel"] == base["audioSel"] and st["audio"] == "Track FX" and st["main"] == "mainview",
          "F1 TV's main view selection and the audio selection untouched (Track FX kept)")

    # another driver in slot 0: the same view, a new selection
    await page.evaluate("() => { document.querySelector('tiledmedia-view[data-fone-ob=\"0\"]').__e2e = 'slot0'; }")
    await dash_post([{**A, "channelId": 1020}, B])
    st = await ob_wait(lambda s: s["ob"][0] == {"ch": 1020, "state": "ok"} and s["boxes"][0]["video"]["ch"] == "1020", 10, "slot 0 on 1020")
    bx = st["boxes"]
    check(bx[0]["view"]["mark"] == "slot0" and bx[0]["view"]["ch"] == 1020 and bx[1]["view"]["ch"] == 1009 and st["ours"] == 2,
          "another driver (1016 -> 1020): the same view with a new selection, the other slot untouched")

    # empty slot 1: selection null, apply, removeView, box gone
    await page.evaluate("() => { window.__e2eGone = document.querySelector('tiledmedia-view[data-fone-ob=\"1\"]'); }")
    await dash_post([{**A, "channelId": 1020}, {**B, "channelId": None}])
    st = await ob_wait(lambda s: s["ob"] == [{"ch": 1020, "state": "ok"}, {"ch": None, "state": "empty"}] and s["ours"] == 1
                       and len(s["boxes"]) == 1 and s["nOb"] == 1, 10, "slot 1 emptied")
    gone = await page.evaluate("() => [__e2eGone.desiredVideoSelection, __e2eGone.associatedPlayer === undefined, __e2eGone.isConnected, __e2eGone.querySelectorAll('video').length]")
    check(gone == [None, True, False, 0] and st["callLog"][-2:] == ["apply", "removeView"] and st["views"] == 3,
          f"an emptied slot: its view's selection cleared, applied, the view removed from the player and the page ({st['callLog'][-3:]})")
    check(st["mainSel"] == base["mainSel"] and st["audioSel"] == base["audioSel"] and st["audio"] == "Track FX",
          "...F1 TV's main view and audio selections still untouched")

    # the split turned off: layout.js forwards slots: [], everything goes
    await page.keyboard.press("KeyS")
    st = await ob_wait(lambda s: s["nOb"] == 0 and not s["boxes"] and s["ours"] == 0 and s["frameZ"] is None, 10, "split off")
    fwd = await page.evaluate("() => window.__obCmds.at(-1)")
    check(fwd == {"cmd": "onboards", "slots": []} and st["ob"] == [] and st["views"] == 2,
          "split off: layout.js forwards slots: [], page.js takes every onboard view off the player and out of the page")

    # ---- page.js on its own (fone-cmd as layout.js sends it), the dashboard re-sending every 2 s ----
    keep = {"slots": None, "at": 0.0}

    async def ob_send(slots):
        await page.evaluate("(s) => document.dispatchEvent(new CustomEvent('fone-cmd', {detail: JSON.stringify({cmd: 'onboards', slots: s})}))", slots)
        keep["at"] = time.time()

    async def keepalive():
        while True:
            await asyncio.sleep(2)
            if keep["slots"] is not None:
                try:
                    await ob_send(keep["slots"])
                except Exception:
                    pass

    async def resend(slots):
        keep["slots"] = slots
        await ob_send(slots)

    ka = asyncio.create_task(keepalive())
    S0 = {"x": 960, "y": 20, "w": 300, "h": 169, "channelId": 1016}
    S1 = {"x": 960, "y": 200, "w": 300, "h": 169, "channelId": 1009}
    await resend([S0, S1])
    st = await ob_wait(lambda s: ob_states(s) == ["ok", "ok"], 15, "onboards from fone-cmd")
    await page.evaluate("() => document.querySelectorAll('tiledmedia-view[data-fone-ob]').forEach((v) => { v.__e2e = 'v' + v.dataset.foneOb; })")
    b0 = st

    # F1 TV switches its main channel: a new content item; the onboards follow it, same views
    await page.evaluate("() => tmSwitchFeed(1025, 'INTERNATIONAL')")
    st = await ob_wait(lambda s: s["content"] != b0["content"] and ob_states(s) == ["ok", "ok"]
                       and all(b["view"]["uid"] == s["content"] for b in s["boxes"]), 10, "onboards on the new content")
    check([b["view"]["mark"] for b in st["boxes"]] == ["v0", "v1"] and [b["view"]["ch"] for b in st["boxes"]] == [1016, 1009]
          and [b["video"]["ch"] for b in st["boxes"]] == ["1016", "1009"] and st["mainSel"] == b0["mainSel"] + 1,
          "F1 TV's channel switch (a new content item): the same views move to its 1016/1009 feeds; the main view's selection is F1 TV's alone")
    await wait_js(page, "() => __curAudio()", lambda n: n == "Track FX", 10, "FX on INTERNATIONAL")

    # F1 TV rebuilds its player: destroy() takes our views off; new views on the new player
    await page.evaluate("() => { window.__mains = new Set(); window.__mainsIv = setInterval(() => __mains.add(document.querySelector('[data-fone-main]')?.id ?? 'none'), 50); }")
    p0 = st["player"]
    old_ids = [b["view"]["id"] for b in st["boxes"]]
    await page.evaluate("() => tmRebuildPlayer()")
    st = await ob_wait(lambda s: s["player"] != p0 and s["ours"] == 2 and ob_states(s) == ["ok", "ok"]
                       and all(b["view"]["player"] for b in s["boxes"]), 15, "onboards on the new player")
    mains = await page.evaluate("() => { clearInterval(__mainsIv); return [...__mains]; }")
    check([b["view"]["ch"] for b in st["boxes"]] == [1016, 1009] and not set(old_ids) & {b["view"]["id"] for b in st["boxes"]}
          and st["views"] == 4 and st["nOb"] == 2 and set(mains) <= {"mainview", "none"} and st["timing"] == 1,
          f"F1 TV rebuilt its player: new views of ours on the new one, same drivers (main followed: {sorted(mains)}, one timing listener)")
    await wait_js(page, "() => __curAudio()", lambda n: n == "Track FX", 10, "FX after the rebuild")

    # a driver this content doesn't have: nothing shown, no SDK calls
    await resend([S0, {**S1, "channelId": 1099}])
    st = await ob_wait(lambda s: s["ob"][1].get("state") == "none" and s["ours"] == 1 and len(s["boxes"]) == 1, 10, "1099: none")
    await asyncio.sleep(0.5)
    c0 = (await ob())["calls"]
    await asyncio.sleep(4)
    st = await ob()
    check(st["calls"] == c0 and st["ob"][1] == {"ch": 1099, "state": "none", "n": st["feeds"]} and st["ob"][0]["state"] == "ok",
          f"a channel the content lacks (1099): 'none' (of {st['feeds']} feeds), nothing shown, no SDK calls in 4 s")

    # SDK promises that never answer: the 250 ms loop keeps running
    base = await ob()
    await page.evaluate("() => { __obHang.add('addView'); __obHang.add('apply'); }")
    await resend([S0, S1])
    await ob_wait(lambda s: s["ob"][1] == {"ch": 1009, "state": "loading", "step": "attach"}, 5, "slot 1 attaching")
    t0 = time.time()
    S0b = {**S0, "y": 40}
    await resend([S0b, S1])
    st = await ob_wait(lambda s: s["boxes"][0]["rect"] == rect_of(S0b), 2, "slot 0 moved while addView hangs")
    moved = time.time() - t0
    n0 = st["tmN"]
    await asyncio.sleep(2)
    st = await ob()
    eng = await status(page)
    rate = (st["tmN"] - n0) / 2
    st = await ob_wait(lambda s: s["ob"][1].get("state") == "error" and "did not answer" in s["ob"][1].get("err", ""), 14, "addView timed out")
    waited = time.time() - t0
    check(moved < 1.0 and rate >= 2 and eng.get("state") == "sync" and 8.5 <= waited <= 13 and st["ob"][0]["state"] == "ok",
          f"addView never answering: slot 0 still moved in {moved:.2f}s, {rate:.0f} reports/s, engine '{eng.get('state')}'; "
          f"slot 1 'error: the player did not answer' after {waited:.1f}s")
    held = await page.evaluate("() => __obRelease()")  # the core answers after all, late
    st = await ob_wait(lambda s: s["ob"] == [{"ch": 1016, "state": "ok"}, {"ch": 1009, "state": "ok"}] and s["ours"] == 2
                       and s["views"] == 4, 15, "recovered once the player answered")
    await asyncio.sleep(1)
    st = await ob()
    check(st["ours"] == 2 and st["views"] == 4 and st["nOb"] == 2 and held >= 1,
          f"...and once it answered ({held} held call), the late-added view was taken off again: 2 of ours on the player, 2 boxes")

    await page.evaluate("() => __obHang.add('apply')")
    await page.evaluate("() => { window.__e2eApply = document.querySelector('tiledmedia-view[data-fone-ob=\"1\"]'); }")
    t0 = time.time()
    await resend([S0b, {**S1, "channelId": 1020}])
    st = await ob_wait(lambda s: s["ob"][1].get("state") == "error" and "did not answer" in s["ob"][1].get("err", ""), 20, "apply timed out")
    waited = time.time() - t0
    kept = await page.evaluate("() => [__e2eApply.isConnected, __e2eApply.desiredVideoSelection?.selection?._ch ?? null]")
    held = await page.evaluate("() => __obRelease()")
    st = await ob_wait(lambda s: s["ob"][1] == {"ch": 1020, "state": "ok"} and s["boxes"][1]["video"]["ch"] == "1020", 10, "apply answered late")
    check(13 <= waited <= 18 and kept == [True, 1020] and held >= 1 and st["ours"] == 2,
          f"applyDesiredState never answering: 'error' after {waited:.1f}s with the selection kept; settled when the player answered")

    # a refused apply: our selection taken back at once (F1 TV's own applies carry every view's)
    await page.evaluate("() => { __obFail.add('apply'); window.__e2eRefused = document.querySelector('tiledmedia-view[data-fone-ob=\"1\"]'); }")
    await resend([S0b, {**S1, "channelId": 1009}])
    st = await ob_wait(lambda s: s["ob"][1].get("state") == "error" and "refused" in s["ob"][1].get("err", ""), 10, "apply refused")
    refused = await wait_js(page, "() => [__e2eRefused.desiredVideoSelection, __e2eRefused.isConnected, __e2eRefused.associatedPlayer === undefined]",
                            lambda r: r == [None, False, True], 5, "the refused view off the player")
    await page.evaluate("() => __obFail.clear()")
    st = await ob_wait(lambda s: s["ob"][1] == {"ch": 1009, "state": "ok"}, 15, "recovered after a refusal")
    check(refused == [None, False, True] and st["ours"] == 2 and st["views"] == 4,
          "a refused applyDesiredState: our selection cleared at once, the view taken off; a new view once the player takes it")
    st = await ob()
    check(st["mainSel"] == base["mainSel"] and st["audioSel"] == base["audioSel"] and st["audio"] == "Track FX",
          "through hangs and refusals: F1 TV's main view and audio selections untouched")

    # the commands stop (extension reloaded, dashboard gone): removed after the lease
    keep["slots"] = None
    st = await ob_wait(lambda s: s["nOb"] == 0 and not s["boxes"] and s["ours"] == 0, 16, "lease expiry")
    gap = time.time() - keep["at"]
    check(9.0 <= gap <= 12.5 and st["ob"] == [] and st["views"] == 2,
          f"commands stopped: onboard views removed {gap:.1f}s after the last one (lease 10 s)")

    # a hidden tab (Chrome throttles its timers, the dashboard's 2 s re-sends too): the lease waits,
    # and nothing is set up while the video is paused there (F1 TV's applyDesiredState didn't answer
    # in 45 s in that state); shown again, the lease runs from then. (document.hidden is overridden
    # in the page's world only: page.js's view of it.)
    await resend([S0b, S1])
    await ob_wait(lambda s: ob_states(s) == ["ok", "ok"], 15, "onboards back")
    await page.evaluate("""() => { for (const [k, v] of [['hidden', true], ['visibilityState', 'hidden']])
        Object.defineProperty(document, k, { configurable: true, get: () => v });
      document.dispatchEvent(new Event('visibilitychange')); __player.playerState = 'paused'; }""")
    keep["slots"] = None
    await ob_send([S0b, {**S1, "channelId": 1020}])  # the last command before the throttling
    c0 = (await ob())["calls"]
    await asyncio.sleep(12)
    st = await ob()
    check(len(st["boxes"]) == 2 and [b["view"]["ch"] for b in st["boxes"]] == [1016, 1009] and st["calls"] == c0
          and st["ob"][1] == {"ch": 1020, "state": "loading", "step": "select"},
          "hidden tab, video paused, no command for 12 s: the onboards stay, the new driver waits (no SDK calls)")
    await page.evaluate("""() => { delete document.hidden; delete document.visibilityState;
      document.dispatchEvent(new Event('visibilitychange')); __player.playerState = 'playing'; }""")
    shown = time.time()
    st = await ob_wait(lambda s: s["ob"][1] == {"ch": 1020, "state": "ok"} and s["boxes"][1]["video"]["ch"] == "1020", 6, "shown: the switch goes through")
    switched = time.time() - shown
    st = await ob_wait(lambda s: s["nOb"] == 0 and not s["boxes"] and s["ours"] == 0, 16, "lease expiry after being shown")
    gap = time.time() - shown
    ka.cancel()
    check(switched < 3 and 9.0 <= gap <= 12.5 and st["views"] == 2,
          f"shown again: the switch went through in {switched:.1f}s; with no command, removed {gap:.1f}s after the tab was shown")
    ours = [e for e in errors if "page.js" in e or "layout.js" in e or "onboard" in e.lower()]
    for e in errors[:5]:
        print("page error:", e[:300])
    check(not ours, f"no page errors from page.js/layout.js ({len(errors)} page errors in all)")


asyncio.run(main())
