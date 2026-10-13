# F1 data sources, as used by fone

The public F1 data fone reads to anchor replays, drive the stats dashboard and measure the
streamer's delay: F1 TV's content API, MultiViewer's API, F1 live timing (the SignalR feed and
its static archive) and OpenF1. Each section says which part of the code uses what; the
endpoints and fields in the "as used by fone" parts are exercised by the code and its tests
(`server/tests/fixtures` holds real answers). Paragraphs marked **unverified notes** come from
a general F1-data reference this page was cut down from and are not exercised by the code:
treat them as leads, not facts.

None of these APIs is documented or supported by its owner; they are what F1 TV, MultiViewer
and F1's own live-timing page use, and they change without notice. A `User-Agent` naming the
project is sent on every request.

- [How the anchor is built](#how-the-anchor-is-built)
- [F1 TV content API](#f1-tv-content-api)
- [MultiViewer](#multiviewer)
- [F1 live timing: the static archive](#f1-live-timing-the-static-archive)
- [F1 live timing: the SignalR Core feed](#f1-live-timing-the-signalr-core-feed)
- [Topics and the fields fone reads](#topics-and-the-fields-fone-reads)
- [OpenF1](#openf1)

## How the anchor is built

`server/fone/f1.py` turns an F1 TV content id into real-world time:

```
F1 TV content API  ->  meeting/session keys, scheduled start, LIVE | REPLAY, the stream list
MultiViewer API    ->  session_start: the replay position (s) of the official SessionStatus: Started
F1 live timing     ->  the UTC of that Started moment (SessionData.json, StatusSeries)

event_utc(p) = p0 + p,   p0 = started_utc - session_start
```

Checked across the 2026 sessions, the anchor is consistent to under 0.5 s. Until MultiViewer
publishes (live, and the first hours after a session) the fallback is
`scheduledStart + PREROLL[kind]`, the replay's fixed pre-roll measured from every 2026 session
(`−307.0` s qualifying, `−307.3` race, `−249.7` sprint, `−1162.3` sprint qualifying, `−1165`
practice).

## F1 TV content API

*As used by fone* (`f1.py`): one GET per content id, no login.

```
https://f1tv.formula1.com/2.0/R/ENG/WEB_DASH/ALL/CONTENT/VIDEO/{contentId}/F1_TV_Pro_Annual/2
```

`resultObj.containers[0].metadata` holds what fone reads:

| field | |
|---|---|
| `title` | e.g. `"Bahrain Grand Prix - Qualifying"`; the session name is the part after the last ` - ` |
| `contentSubtype` | `LIVE`, `REPLAY`, … |
| `duration` | seconds |
| `emfAttributes.MeetingKey`, `.MeetingSessionKey` | the keys live timing and OpenF1 use |
| `emfAttributes.Meeting_Name` / `Global_Meeting_Name` | |
| `emfAttributes.sessionStartDate`, `.sessionEndDate` | epoch **milliseconds**, sometimes as strings |
| `additionalStreams[]` | the content's channels: `channelId` (what the player's feeds carry), `type` (`obc` = a driver's onboard camera; the other feeds are `additional`), `title`, `driverFirstName`, `driverLastName`, `racingNumber`, `teamName`, `hex` (team colour) |

F1 TV hides the metadata of content that hasn't gone live yet (the answer isn't 200). fone
then searches this week's OpenF1 sessions for one whose MultiViewer record carries this
content id (`_find_nearby_session`).

## MultiViewer

*As used by fone* (`f1.py`, `timing.py`). MultiViewer for F1 publishes, per session, where the
official session start falls in each F1 TV stream, and circuit geometry in the live-timing
position units.

```
https://api.multiviewer.app/api/v1/meetings/{meetingKey}/sessions/{sessionKey}
```

| field | |
|---|---|
| `session_start` | replay position (s) of `SessionStatus: Started` in the main feed; absent until MultiViewer has synced the session, usually some hours after it |
| `f1tv_content_id` | the main (session) feed's content id; fone treats a content as a session feed only when this matches |
| `session_info.Path`, `.Name` | the live-timing archive path and the session name |
| `sync_offsets.sync_offsets[]` | per stream `{streamData: {channelId, contentId}, diff}`: how far each channel runs from the main feed (seconds); onboards run ~7 s behind |

```
https://api.multiviewer.app/api/v1/circuits/{circuitKey}/{year}
```

`corners[]`: `{number, letter, trackPosition: {x, y}, angle}` in the same X/Y units as
`Position.z`, used to number the corners on the track map. The circuit key and year come from
live timing's `SessionInfo` (`Meeting.Circuit.Key`, `StartDate`).

## F1 live timing: the static archive

*As used by fone* (`timing.py`, `f1.py`). Every session's timing feed is published as flat
files about 20 minutes after it ends (403 until then), no auth, under

```
https://livetiming.formula1.com/static/{year}/Index.json
https://livetiming.formula1.com/static/{path}Index.json                  path from the year index or MultiViewer
https://livetiming.formula1.com/static/{path}SessionData.json
https://livetiming.formula1.com/static/{path}{Topic}.jsonStream         StreamPath per topic
https://livetiming.formula1.com/static/{path}{Topic}.json               KeyFramePath per topic
```

Example path: `2026/2026-10-04_Bahrain_Grand_Prix/2026-10-03_Qualifying/`. Files may start
with a UTF-8 BOM (`utf-8-sig`).

- The **year index** lists `Meetings[]` (`Key`, …) with `Sessions[]` (`Key`, `Path`, `Name`, …).
  A session's `Path` appears only once it is archived; before that fone derives it from a
  sibling session's meeting directory plus the local date and the session name with spaces
  as underscores.
- The **session index** carries `Feeds: {Topic: {KeyFramePath, StreamPath}}`.
- A **`.jsonStream`** is one message per line: `HH:MM:SS.mmm` (the offset from the session's
  `t0`) immediately followed by the JSON. `t0` is the `Utc` of a stream in the `AudioStreams`
  or `ContentStreams` keyframe; failing that, every `SessionData` entry's `Utc` minus its
  line's offset. The state at UTC `t` is every line with offset ≤ `t − t0`, merged in order
  (merge rules below).
- `SessionData.json` (keyframe): `StatusSeries[{Utc, SessionStatus}]`: the `Started` entry is
  the anchor's UTC. `ArchiveStatus.json`: `{Status: "Complete"}` once the archive is whole.

Streams fone downloads: `SessionInfo`, `SessionData`, `SessionStatus`, `DriverList`,
`TimingData`, `TimingAppData`, `TimingStats`, `TrackStatus`, `RaceControlMessages`,
`WeatherData`, `ExtrapolatedClock`, `LapCount`, `PitLaneTimeCollection`, `Position.z`, plus the
keyframes `AudioStreams`, `ContentStreams`, `ArchiveStatus`, `SessionInfo`. Skipped on purpose:
`CarData.z` (telemetry, big), `TimingDataF1` (a copy of `TimingData`), `DriverRaceInfo`,
`TopThree`, `LapSeries`, `TyreStintSeries`, `CurrentTyres`.

**Unverified notes.** `https://livetiming.formula1.com/static/Index.json` lists all seasons.
`CarData.z` decodes like `Position.z` (below) to `{Entries: [{Utc, Cars: {num: {Channels:
{0: RPM, 2: speed km/h, 3: gear, 4: throttle %, 5: brake (0/100), 45: DRS}}}}]}`; DRS 8 =
eligible, 10/12/14 = open.

## F1 live timing: the SignalR Core feed

*As used by fone* (`server/fone/livefeed.py` server side, anonymous; `extension/livepos.js`
browser side, with the viewer's F1 TV token).

```
https://livetiming.formula1.com/signalrcore
```

ASP.NET Core SignalR, JSON protocol, frames terminated by `\x1e`. The old `/signalr`
(classic SignalR, `clientProtocol=1.5`) answers 401 now.

1. `POST /signalrcore/negotiate?negotiateVersion=1` → `{connectionToken, connectionId, …}`.
2. Open the transport with `?id=<connectionToken>`: the server uses **Server-Sent Events**
   (`GET`, `Accept: text/event-stream`; client → server frames go by `POST` to the same URL
   with `Content-Type: text/plain`). `livepos.js` can also use a WebSocket.
3. Handshake: send `{"protocol":"json","version":1}`; the answer is `{}` (or `{error}`).
4. Subscribe: `{"type":1,"invocationId":"1","target":"Subscribe","arguments":[[topics…]]}`.
   Its completion (`type: 3`) carries `result: {Topic: current state, …}`, the **keyframes**
   (each may carry a `_kf: true` marker to drop).
5. From then on `{"type":1,"target":"feed","arguments":[topic, payload, utc]}` streams the
   deltas. `utc` is the archive's clock: an archived message at offset `o` has `Utc == t0 + o`,
   so a capture and the archive line up.
6. `{"type":6}` is a ping, both ways; the server drops a client it hasn't heard from in
   30 s (fone pings every 15 s). `{"type":7}` is a close.

Anonymously the feed carries every topic **except `Position.z` and `CarData.z`**: those are
simply left out of the answers (no error) unless the request carries an F1 TV subscriber's
token. The feed shows whatever session F1 is currently running (it switches ~15 min before a
session), so a client checks `SessionInfo.Key` against the session it wants.

The token is the `subscriptionToken` (a JWT) inside the URL-encoded JSON of the
`login-session` cookie on `.formula1.com` (`extension/f1token.js`). The browser presents it
either as `Authorization: Bearer` on the negotiate and SSE requests, or as `access_token=` in
the WebSocket URL; which one the server honours has varied, so `livepos.js` tries both and
judges by whether positions arrive. The token never leaves the browser except to
`livetiming.formula1.com`.

### `Position.z` decoding

Payloads are base64 of raw-DEFLATE'd JSON (`zlib`, `wbits = −15`; `DecompressionStream('deflate-raw')`
in the browser):

```
{"Position": [{"Timestamp": "2026-10-03T15:01:33.172Z",
               "Entries": {"1": {"Status": "OnTrack", "X": -1437, "Y": 4284, "Z": 7}, …}}, …]}
```

`Timestamp` is the sensor time, 0–1.7 s before the message arrives: the map uses it. `Status`
is `OnTrack` or `OffTrack`; `X`/`Y`/`Z` are F1's own units on a per-circuit origin
(unverified note: 1/10 m).

### Merge rules

A delta merges into the topic's state the way SignalR's live-timing clients do
(`timing_state.merge`): dicts merge recursively; a dict applied to a list updates it by index
(`{"3": {…}}`); a `_deleted` key lists keys (or indices) to remove; anything else replaces.

## Topics and the fields fone reads

What `timing_state.py` takes from each topic for the dashboard's state; the rest of every
message is kept on disk but unused.

| topic | fields |
|---|---|
| `SessionInfo` | `Key`, `Name`, `Type`, `Meeting.Key`, `Meeting.Name`, `Meeting.Circuit.Key`, `Path`, `StartDate` |
| `SessionStatus` | `Status`: `Inactive`, `Started`, `Aborted`, `Finished`, `Finalised`, `Ends` |
| `DriverList` | per number: `Tla`, `FirstName`, `LastName`, `FullName`, `BroadcastName`, `TeamName`, `TeamColour` (hex, no `#`) |
| `TimingData` | `SessionPart` (qualifying); `Lines[num]`: `Position` (string) or `Line`, `LastLapTime {Value, OverallFastest, PersonalFastest}`, `BestLapTime {Value}`, `NumberOfPitStops`, `InPit`, `PitOut`, `Retired`, `KnockedOut`; gaps per session type: race `GapToLeader`, `IntervalToPositionAhead {Value}`; qualifying `BestLapTimes[part]`, `Stats[part] {TimeDiffToFastest, TimeDifftoPositionAhead}`; practice `TimeDiffToFastest`, `TimeDiffToPositionAhead` |
| `TimingAppData` | `Lines[num].Stints[] {Compound, TotalLaps, New}` (`New` is the string `"true"`) |
| `TrackStatus` | `Status`: `1` AllClear, `2` Yellow, `4` SC, `5` Red, `6` VSC deployed, `7` VSC ending (unverified note: `3` exists, meaning unknown); `Message` |
| `RaceControlMessages` | `Messages[] {Category, Flag, Scope: Track \| Sector \| Driver, Sector, Message, Utc}`; flags `GREEN`, `YELLOW`, `DOUBLE YELLOW`, `CLEAR`, `RED`, `CHEQUERED`, … |
| `WeatherData` | `AirTemp`, `TrackTemp`, `Humidity`, `Rainfall`, `WindSpeed`, `WindDirection` (strings) |
| `ExtrapolatedClock` | `Remaining` (`H:MM:SS`), `Extrapolating`, `Utc` |
| `LapCount` | `CurrentLap`, `TotalLaps` |
| `Position.z` | see above |

**Unverified notes** on topics fone stores but doesn't read: `TimingStats` (personal bests,
best sectors, speed traps), `PitLaneTimeCollection` (pit-lane times), `TeamRadio`,
`ChampionshipPrediction`, `Heartbeat` (keepalive with the server's UTC; fone subscribes to it
so a quiet feed still delivers a message every few seconds).

## OpenF1

*As used by fone* (`timing.py`, `f1.py`, `screenclock.py`): the season calendar, no auth.

```
https://api.openf1.org/v1/sessions?year=2026
https://api.openf1.org/v1/sessions?date_start>=2026-10-01T00:00:00&date_start<=2026-10-09T00:00:00
```

Fields read: `session_key`, `meeting_key`, `session_name`, `session_type`, `date_start`,
`date_end` (ISO 8601 with offset), `gmt_offset` (`"03:00:00"`), `year`, `is_cancelled`,
`country_name`, `location`. The timing service refreshes the calendar every 6 h (cached in
`timing/schedule.json`) to know when to start capturing; the screen-clock reader uses each
session's `gmt_offset` as a candidate display zone for the "Track time" clock.

**Unverified notes.** The free tier is rate-limited (about 3 requests/s, 30/min); historical
data goes back to 2023; `session_key=latest` / `meeting_key=latest` address the current
event; every endpoint takes `field=value` and `field>=x`-style filters. The other endpoints
(`/laps`, `/car_data`, `/location`, `/position`, `/intervals`, `/stints`, `/pit`, `/weather`,
`/race_control`, `/drivers`, `/meetings`, …) are derived from the same live-timing feed and
are not used here. OpenF1's `circuit_info_url` points at the MultiViewer circuit data above.
