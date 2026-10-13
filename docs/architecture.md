# fone architecture

How the two parts fit together: the **server** (`server/fone`, FastAPI, one Docker image) and
the **Chrome extension** (`extension/`, Manifest V3). This page is the reference for the routes,
the data shapes and the messages between the extension's parts; the README says what it all
does for the viewer. Times are unix seconds (float, UTC) unless stated.

- [The four clocks](#the-four-clocks)
- [Server](#server)
  - [Recordings and the data directory](#recordings-and-the-data-directory)
  - [Routes](#routes)
  - [The timeline and the delay](#the-timeline-and-the-delay)
  - [The optional services](#the-optional-services)
- [Extension](#extension)
  - [Parts](#parts)
  - [Messages](#messages)
  - [Settings](#settings)

## The four clocks

Everything the extension does is a conversion between these (`extension/core.js`):

| | | |
|---|---|---|
| `p` | F1 TV player position | `<video>.currentTime` on the legacy Bitmovin player; on Tiledmedia (the current one) the content position that `page.js` reads from the player's own objects |
| `E` | the real-world moment the frame at `p` shows | depends on the *clock* chosen for the video, below |
| `W` | when the commentary about `E` aired | `E + delay`; the delay is the streamer's lag behind real time |
| `m` | position in the stitched commentary playlist | `W` mapped through the timeline's pieces (wall clock ↔ playlist) |

The clock (`FoneCore.chooseClock`) is one of:

| clock | `E` from | when |
|---|---|---|
| `multiviewer` | `p0 + p`, `p0` exact from MultiViewer's `session_start` and live timing's `SessionStatus: Started` | a session feed whose anchor MultiViewer has published |
| `preroll` | `p0 + p`, `p0 = scheduledStart + PREROLL[kind]` (the replay's fixed pre-roll per session type) | a session feed before MultiViewer publishes |
| `epoch` | `p` itself: the player hands out unix time | a replay player that exposes UTC directly |
| `edge` | measured back from the live edge | the video is live: after one alignment the commentary follows F1 TV's playhead (`m0 + (p − p0)`) and only moves on a seek or a nudge |
| `manual` | your own alignment | not a session feed (pre-/post-shows), or F1 TV doesn't know the content |

Two rules follow from this:

- **The dashboard shows the state at `E` and never later.** The server computes
  `/api/timing/.../state?t=E` from the messages that had arrived by `E`, and car positions are
  only interpolated up to `E`. Live, where `E` is a guess, the engine holds the dashboard a
  few seconds further back (2 s behind a measured delay, 5 s behind a calibrated one).
- **Danmaku are shown at their send time on the `W` clock**: the wall time of the audio
  playing right now, so they pause, jump and speed up with what you hear.

The streamer's delay is per channel and per clock family: only `multiviewer` and `preroll`
put the video on true UTC, `epoch` is a player clock that reads ~40 s late on live streams,
and `edge`/`manual` have no family. A calibration made on one clock is only reused on a
comparable one (`server/fone/store.py`, `CLOCK_FAMILY`).

## Server

`server/fone/app.py` builds everything at import (so the routers can be mounted) and starts the
services in FastAPI's lifespan. The core (recordings scanner, audio extractor, F1 TV mapper,
sync store, playback diagnostics) is always on; each optional service (timing, ads, screenclock,
video, backfill, danmaku) is a class with `start()`/`stop()` plus a `make_router(svc)`, switched
off with `FONE_<NAME>=0`, and one that fails to build or start is left out rather than taking
the commentary sync down (`/api/health` lists the running ones).

Configuration is environment variables only (`server/fone/config.py`; the README's
Configuration table lists them).

### Recordings and the data directory

`FONE_REC_DIR` (`/rec`) is scanned recursively every `FONE_SCAN_INTERVAL` s. A recording's
**channel is its parent folder** (BililiveRecorder's `{{ name }}/` template; `FONE_CHANNEL_ALIASES`
renames folders), its start time is parsed from the file name (`yyyyMMdd-HHmmss-fff-title.flv`
and the coarser variants in `recordings.py`) in `FONE_FILENAME_TZ`, and its `source` is
`recorder`, or `import` for files under `FONE_IMPORT_DIR` (backfill's replays, which only ever
fill gaps). A file still being written (mtime younger than `FONE_GROWING_IDLE_SECONDS`) is
tailed into ffmpeg so its audio is playable seconds after it was recorded.

`FONE_DATA_DIR` (`/data`, the `fone-data` volume) holds everything computed:

```
fone.sqlite3                 F1 TV content cache, delays (content_delay, channel_delay), backfill's tables
hls/<fid>/                   one directory per recording (fid = sha1 of its path, 16 hex chars)
  index.m3u8, seg_NNNNN.ts     audio-only HLS, copied, not re-encoded (FONE_SEGMENT_SECONDS each)
  run.json                     how the audio was mapped to the wall clock (live lag / end lag)
  ads.json                     sponsor clips found (ads service)
  screenclock.json             the measured screen delay (screenclock service)
timing/<session_key>/        live-timing streams, archive and own capture in the same format
  <Topic>.jsonStream, meta.json
timing/schedule.json         the OpenF1 calendar; timing/tracks/<meeting>.json (+ .corners.json)
video/<fid>.json             FLV keyframe index (video handoff)
danmaku/                     capture/<room>-<yyyymmdd>.jsonl and danmaku.sqlite3
diag/<day>.jsonl             playback diagnostics, 7 days
backfill/                    downloads in progress
```

### Routes

All reads are open to anyone who can reach the port. **Writes (anything but GET/HEAD/OPTIONS)
are refused with 403 when the request carries an `Origin` that isn't `chrome-extension://` or
`moz-extension://`** (`RefuseWebPageWrites`): the extension's own pages and scripts without an
Origin (curl) can write, a web page you visit cannot. CORS is `*` with private-network access
allowed, so a page on https://f1tv.formula1.com can read from a server on the LAN.

**Core**

| | |
|---|---|
| `GET /` | status page (HTML): every recording, its extraction status and analysis notes |
| `GET /api/health` | `{ok, recDir, files, now, services: [...]}` |
| `GET /api/channels` | `[{name, files, latest, live}]`, newest first; `live` = a file is still growing |
| `GET /api/f1tv/{cid}[?refresh=1]` | an F1 TV video (`f1.py`): `{contentId, title, subtype: "LIVE"\|"REPLAY"\|…, duration, meetingKey, sessionKey, meetingName, sessionName, kind: "practice"\|"sprint_qualifying"\|"qualifying"\|"sprint"\|"race"\|null, scheduledStart, scheduledEnd, isSessionFeed, anchor: "multiviewer"\|"preroll"\|null, p0, startedUtc, sessionStartVideo, channelOffsets: {channelId: seconds}, streams: [{channelId, type: "obc"\|"additional", title, number, name, team, color}]}` or `{…, error}`. Cached in SQLite; an anchored replay never changes, everything else is re-checked after 10 min |
| `GET /api/sync/{cid}?channel=&clock=` | the delay to start from: `{delay, source: "content"\|"measured"\|"channel"\|"default"}`; `measured` adds `{screen, reaction, confidence, file}` |
| `PUT /api/sync/{cid}` `{channel, clock, delay}` | save a tweak for that video (and clock) |
| `GET /api/timeline?channel=&from=&to=` | the commentary between two wall times, below |
| `GET /api/playlist.m3u8?files=a,b,…` | the files stitched into one HLS EVENT playlist (`#EXT-X-DISCONTINUITY` between files, `#EXT-X-ENDLIST` once all are finished) |
| `GET /hls/{fid}/seg_NNNNN.ts` | an audio segment |
| `POST /api/diag` `{client, build, platform, events: [{t, type, …}]}` · `GET /api/diag?client=&type=&since=&limit=` | playback diagnostics from the extension's engine; `tools/diag.py` reads them as a timeline |

**Timing** (`timing.py`, `timing_state.py`, `livefeed.py`), prefix `/api/timing`

| | |
|---|---|
| `GET /session/{key}/state?t=` | `{t, available: true, session: {key, name, type, meeting}, status: "Inactive"\|"Started"\|"Aborted"\|"Finished"\|"Finalised"\|"Ends", clock: {remaining: "MM:SS"\|null, lap, laps}, part: 1\|2\|3\|null, track: {status, flag: "GREEN"\|"YELLOW"\|"DOUBLE YELLOW"\|"SC"\|"VSC"\|"RED"\|"CHEQUERED", message}, weather: {air, track, humidity, rain, windSpeed, windDir}, tower: [{pos, num, code, name, team, color: "#RRGGBB", gap, interval, last, lastFlag: "overall"\|"personal"\|"none", best, tyre: "SOFT"\|"MEDIUM"\|"HARD"\|"INTERMEDIATE"\|"WET"\|"UNKNOWN", tyreAge, newTyre, pits, inPit, pitOut, retired, knockedOut}]}`; `{available: false, reason}` (HTTP 200) while there is no data |
| `GET /session/{key}/positions?from=&to=` | `{cars: {"<num>": [[t, x, y], …]}}`: on-track samples with their sensor time in the window (at most 120 s), plus the one just before `from`; F1's X/Y units |
| `GET /session/{key}/track` | `{available: true, outline: [[x, y], …], rotation, bounds: [x0, y0, x1, y1], source, corners: [{n, l, x, y, a}]}`: the outline from the fastest clean lap of the weekend, corner numbers from MultiViewer's circuit data (`[]` until fetched) |
| `POST /session/{key}/positions` `{messages: [{Timestamp, Entries: {num: {X, Y, Z, Status}}}]}` | decoded live `Position.z` from the browser (≤ 5000 per call; stored 0 once the session is archived) |
| `GET /content/{cid}` | the F1 TV mapping, as `/api/f1tv/{cid}` |
| `GET /status` | what is being captured, fetched and loaded |

**Other services**

| | |
|---|---|
| `GET /api/ads/{fid}` | `{spans: [{start, end, name}], analyzedTo}` in the file's own media seconds (its audio HLS timebase) |
| `GET /api/screenclock/{fid}` | `{delayScreen, n, confidence: "high"\|"medium"\|"low"\|"none", updated, basis, status, …}`; `delayScreen` = file wall time − the F1 feed UTC shown on screen |
| `GET /api/video/{fid}/info` | `{id, name, growing, t0, a0, mOffset, duration, edge, width, height, codec, wallStart}`; `m = v + mOffset` converts this playlist's time `v` into the file's audio-HLS time |
| `GET /api/video/{fid}/index.m3u8[?start=]` · `/seg/{a}_{b}.ts` | the recording's own picture and sound, cut at keyframes, no re-encode (VOD when finished, EVENT while growing); `202` + `Retry-After` while the FLV is being indexed |
| `GET /api/backfill/status` | `{enabled, importDir, rooms, periods, jobs}` |
| `POST /api/backfill/import` `{"bvid"}` or `{"from", "to", "channel"}` | import a replay by hand (409 unless `FONE_ROOMS` and `FONE_IMPORT_DIR` are set) |
| `GET /api/danmaku?channel=&from=&to=[&limit=]` | `{items: [{t, x, m: 1\|4\|5, s, c, u, ul?, ml?, wl?, g?, e?, em?, k?, p?}], live, room}`, oldest first, at most 6000 (see `danmaku.py` for the fields) |
| `GET /api/danmaku/status` | rooms listened to, capture and archive state |

### The timeline and the delay

`GET /api/timeline` answers

```
{channel, now,
 files: [{id, name, title, source, status, growing, provisional, inPlaylist,
          wallStart, wallEnd, duration, offset, error, adsAnalyzedTo}],
 pieces: [{w, m, d, f}],        wall time w ↔ playlist position m, for d seconds, in file index f
 mutes:  [{w, d, m, name}],     sponsor clips, on both clocks
 playlist, live, edge, complete}
```

- Files join the playlist in wall order up to and including the first one still growing or
  being extracted, so the playlist is append-only. An import (`source: "import"`) joins only
  once fully extracted and never holds the others back.
- Where two files cover the same wall time the recorder wins; imports only fill what nothing
  else covers; no two pieces overlap (`timeline.py`). A clipped remainder shorter than 2 s of
  an import (0.1 s of a recording) is dropped.
- `mutes` are the ads service's spans mapped through the pieces actually played; clips of one
  reel less than 3 s apart are merged.
- `complete` is false while any eligible file is still being written: the extension then
  refreshes the timeline often, otherwise once a minute.

`GET /api/sync/{cid}` picks the delay in this order: the video's own saved tweak (`content`),
then on a true-UTC clock the screen-clock measurement of the recording that covers the video
(`measured`: `delayScreen + FONE_REACTION_SECONDS`, only with high or medium confidence and
after the display zone was checked), then the channel's latest tweak on a comparable clock
(`channel`), then `FONE_DEFAULT_DELAY` (`default`).

### The optional services

**timing**: per-session live-timing state at any UTC. Sessions come from the OpenF1 calendar.
Each one is captured live over SignalR Core (`livefeed.py`, anonymous SSE transport) from
15 min before the scheduled start until `SessionStatus: Ends` (safety stop 4 h after the
start), and replaced by the static archive once F1 publishes it (~20 min after the session;
403 until then). Both are stored in the archive's format, one `<Topic>.jsonStream` per topic
(`HH:MM:SS.mmm<json>` lines, offsets from the session's `t0`), so one loader serves both.
Anonymous access has everything except car positions; those come from the viewer's own F1 TV
login through `POST /positions` (the server never sees the token). `docs/f1-data-sources.md`
has the endpoints and formats.

**ads**: sponsor-reel detection per recording against the reference clips in
`server/fone/ads/` (`refs.npz`: fingerprints of the clips, built by `fone/ads/build_refs.py` from 8 kHz mono WAVs, plus `ads.json`, the manifest). Growing files are analysed segment by
segment as the extractor lists them (a clip is known 2–3 s into its audio); finished files
once. The extension mutes while the target `m` is inside a `mutes` span.

**screenclock**: the streamer's delay measured off their stream. The streamer shows
MultiViewer's "Track time" (the feed's UTC in some display zone) on screen; keyframes are
OCRed with tesseract, the readings checked against a running clock, and the sub-second part
timed from the frame where the seconds digit ticks. Finished files once; growing files every
few minutes, incrementally. The raw readings are kept with the result, so a restart resumes.

**video**: the recording's own picture and sound as HLS, for keeping on watching the streamer
after F1 TV ends (`video_flv.py` indexes the FLV's keyframes; segments are the FLV bytes
between two keyframes, remuxed by ffmpeg with timestamps kept).

**backfill**: a live-status log per configured room (`FONE_ROOMS`, polled every 60 s); when a
stream ends and the channel's recordings leave a gap over 60 s, the streamer's official
replay (直播回放) is polled for up to 36 h, downloaded, de-duplicated, placed on the wall
clock (aligned against what was recorded, else from the room's live time) and written as a
recorder-style FLV into `FONE_IMPORT_DIR/<channel>/`.

**danmaku**: an anonymous connection to each room's danmaku feed (`danmaku_feed.py`), written
as JSON lines and indexed into SQLite, plus danmakus.com's archive for what the capture
didn't hear (no levels, colours or stickers there). Served by send time.

## Extension

### Parts

| where | files | role |
|---|---|---|
| content scripts on `https://f1tv.formula1.com/*`, isolated world | `core.js`, `player.js`, `dmcore.js`, `danmaku.js`, `content.js`, `layout.js` | `player.js` finds the main `<video>` and its true position; `content.js` draws the panel, binds the keys and reports the player to the engine; `layout.js` the split view with the stats iframe; `danmaku.js` the 弹幕 overlay |
| content scripts, **main** world | `page.js`, `layout_page.js` | the only place the Tiledmedia player's objects are reachable: position, speed, audio tracks, onboard views; fullscreen redirection for the split view |
| service worker | `background.js` (+ `dashproxy.js`, `f1token.js`) | relays messages between tabs and the engine, opens the offscreen document, fetches for pages that can't (mixed content, CSP), hands the F1 TV token to the engine, self-update |
| offscreen document | `offscreen.html`: `vendor/hls.light.min.js`, `core.js`, `livepos.js`, `offscreen.js` | **the audio engine**: picks the server, clock and delay, plays `m` with hls.js and keeps it on target (at 1× by tiny rate changes, otherwise by rare seeks); relays live car positions |
| extension pages | `dashboard.html` (`dashcore.js`, `dashboard.js`, `dashboard.css`), `options.html`/`options.js` | the stats (as an iframe behind the shrunken player, or a window of its own); the settings |

`core.js`, `dashcore.js` and `dmcore.js` are pure functions shared with the Node unit tests.

### Messages

`chrome.runtime` messages, routed by `target`:

| from → to | message | notes |
|---|---|---|
| content → background → engine | `{target: 'engine', type: 'video', cid, channel, video: {p, paused, seeking, waiting, rate, ready, live, seekEnd, src, at, dx}, settings: {servers, volume, muted}}` | the player's state, at most every 200 ms; the background forwards it as `target: 'offscreen'` with `tab` added. A new `tab|cid|channel|servers` key (re)attaches the engine |
| content → engine | `{type: 'nudge', delta}` · `{type: 'relive'}` · `{type: 'detach'}` | the delay buttons/keys; "Re-sync to live"; the video went away |
| background → engine | `{target: 'offscreen', type: 'detach', tab}` | the tab closed |
| engine → background → tab | `{target: 'background', type: 'status', tab, status}` → the tab receives `{type: 'fone-status', status}` | ≤ 4 times a second. `status`: `{state, text, channel, channels, server, clock, delay, delaySource, cid, title, session, anchor, playing, E, W, m, lead, drift, file, error, fixes, diag, livepos, dm}`; `state` is one of `loading`, `error`, `syncing`, `sync`, `waiting`, `before`, `gap`, `after`, `none`, `paused`, `buffering`; `dm = {w, at, rate, run}` is the danmaku clock |
| engine → dashboards (broadcast) | `{target: 'dashboard', type: 'clock', tab, E, playing, rate, at, sessionKey, cid, server, live}` | 5 times a second; dashboards run their own clock and ease onto each report |
| engine → background | `{target: 'background', type: 'f1-token'}` → the token or `null` | answered only to `offscreen.html` (never to a content script or any page); the token goes to `livetiming.formula1.com` and nowhere else |
| dashboard, danmaku → background | `{target: 'dash-fetch', url}` → `{ok, json}` or `{ok: false, error}`; `{target: 'dash-fetch', ping: true}` → `{pong: true}` | GETs the iframe can't make itself (https page, http server); only `/api/timing/…`, `/api/f1tv/<id>` and `/api/danmaku` |
| danmaku → background | `{target: 'dm-image', url}` → `{ok, data: 'data:image/…'}` | Bilibili stickers and emoji as data URLs (F1 TV's CSP); only `*.hdslb.com` |
| layout → background | `{target: 'background', type: 'popout-dashboard'}` | the stats in a window of their own |

`window.postMessage` between `layout.js` (the F1 TV tab) and the dashboard iframe:

| | |
|---|---|
| layout → dashboard | `{type: 'fone-layout', rect: {x, y, w, h}, vw, vh, fullscreen}` on load and every change (CSS px, viewport-relative) |
| dashboard → layout | `{type: 'fone-layout-cmd', cmd: 'off' \| 'popout' \| 'fullscreen' \| 'rect'}`; `{cmd: 'player', box}`; `{cmd: 'onboards', slots: [{x, y, w, h, channelId}, …]}` (at most two onboard rectangles, re-sent every 2 s, `[]` = none) |

DOM `CustomEvent`s between the isolated and the main world (JSON string `detail`):

| | |
|---|---|
| `page.js` → `player.js` | `fone-tm`: the Tiledmedia sample (`eventType`, `currentPosition`, `contentDuration`, `seekLowerBound`, `seekUpperBound`, `bufferLowerBound`, `bufferUpperBound`, `isAtLiveEdge`, `isWallclockTimeSourceBased`), plus `r` (speed) and `a` (audio tracks) |
| `content.js`, `layout.js` → `page.js` | `fone-cmd`: `{cmd: 'rate', value}` (replays only), `{cmd: 'audio', prefer: 'fx' \| 'none', cid}`, `{cmd: 'onboards', slots}`; each re-sent every 2 s and dropped by `page.js` after 10 s without it (extension reloaded or removed) |

The engine also talks to the server directly: `/api/health` to pick the first reachable
server, `/api/channels`, `/api/f1tv/{cid}`, `/api/sync/{cid}` (and `PUT` 800 ms after the last
nudge), `/api/timeline`, the playlist and segments, `POST /api/diag`, and, live,
`POST /api/timing/session/{key}/positions` from `livepos.js`.

### Settings

`chrome.storage.sync` (the options page and the panel): `servers` (tried in order), `channel`
(`''` = the most recently recorded), `volume`, `muted`, `autoHold`, `collapsed`, `speed`, `autoFx`,
and `danmaku` (the 弹幕 settings object). `chrome.storage.local` (per machine): `layout`
(`'split'` | `'off'`), `onboards` (the two picked drivers), and the self-update's `updateSeen`,
`lastReload`, `refreshF1tv`.

Self-update: a published build (`tools/publish_ext.sh`) carries `build.js` (read once at load),
`build.json`, which installed copies poll from the update channel (`fone-ext`, port 8789) and
reload into once it has been up for 75 s and no tab is playing, and `local.js` with that
install's default servers (`self.FONE_LOCAL.servers`; the repository's copy is an empty
placeholder, so a copy loaded straight from the repository asks for the server in its options
page and never updates itself).
