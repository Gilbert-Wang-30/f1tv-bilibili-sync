"""Live capture: SignalR-over-SSE parsing, a recorded snapshot of the real anonymous feed,
and equivalence of a live-captured session (feed + browser position uploads) with its archive."""

import asyncio
import gzip
import heapq
import json
import math
import os
import random
from contextlib import aclosing

import httpx
import pytest
from fastapi.testclient import TestClient

from fone import livefeed
from fone.timing import CaptureWriter, LiveCapture, ScheduledSession, SessionStore
from fone.timing_state import SessionTimeline, inflate, iso_ms, read_stream
from test_timing import KEY, Q_PATH, T0, TFIX, app_for, archived_root, clone, make_service, utc  # noqa: F401

# 32 s of the real feed at 2026-10-04 00:17Z: keyframes of the finished KL qualifying + pings.
SNAPSHOT = gzip.decompress((TFIX / "sse_snapshot_11730.txt.gz").read_bytes())
QUALI = ScheduledSession(KEY, 1308, "Qualifying", "Qualifying", utc("2026-10-03T08:00:00"),
                         utc("2026-10-03T09:00:00"), 2026)


class FakeSignalR:
    """Serves one SSE body per connection (in odd-sized chunks) and records client posts."""

    def __init__(self, bodies: list[bytes], chunk: int = 4093):
        self.bodies = list(bodies)
        self.chunk = chunk
        self.posts: list[str] = []
        self.connections = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/negotiate"):
            return httpx.Response(200, json={"connectionId": "c", "connectionToken": f"t{self.connections}",
                                             "negotiateVersion": 1, "availableTransports": []})
        if request.method == "GET" and request.url.path.endswith("/signalrcore"):
            self.connections += 1
            body = self.bodies.pop(0) if self.bodies else b""

            async def chunks():
                for i in range(0, len(body), self.chunk):
                    yield body[i:i + self.chunk]

            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())
        if request.method == "POST" and request.url.path.endswith("/signalrcore"):
            self.posts.append(request.content.decode())
            return httpx.Response(200)
        return httpx.Response(404)


def sse(frames: list[dict]) -> bytes:
    return b":\r\n" + b"".join(b"data: " + json.dumps(f).encode() + b"\x1e\r\n\r\n" for f in frames)


def keyframes(topics: dict) -> dict:
    return {"type": 3, "invocationId": "1", "result": {k: {**v, "_kf": True} for k, v in topics.items()}}


def feed(msgs) -> list[dict]:
    return [{"type": 1, "target": "feed", "arguments": [topic, payload, iso_ms(T0 + off)]} for off, topic, payload in msgs]


def archive_messages(d) -> list[tuple[float, str, object]]:
    """Every non-position message of a session directory in delivery order: (offset, topic, payload)."""
    per_topic = []
    for f in sorted(d.glob("*.jsonStream")):
        topic = f.name.removesuffix(".jsonStream")
        if topic == "Position.z":
            continue
        rows, last = [], -math.inf
        for i, (off, raw) in enumerate(read_stream(f)):
            last = max(last, off)  # the reader's clamp
            rows.append((last, topic, i, raw))
        per_topic.append(rows)
    return [(off, topic, json.loads(raw)) for off, topic, _, raw in heapq.merge(*per_topic)]


def collect(fake: FakeSignalR) -> list:
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as c:
            async with aclosing(livefeed.stream(c)) as events:
                return [ev async for ev in events]

    return asyncio.run(run())


# ---- protocol -----------------------------------------------------------------


def test_sse_frames_split_on_the_record_separator_across_chunks():
    text = (':\r\ndata: {}\x1e\r\n\r\ndata: {"type":6}\x1e{"type":6}\x1e\r\n\r\n'
            'data: {"type":1,"target":"feed","arguments":["Heartbeat",{"Utc":"x"},"2026-10-03T08:00:00.1234567Z"]}'
            '\x1e\r\n\r\n')
    parser, frames = livefeed.SseFrames(), []
    for i in range(0, len(text), 3):
        frames += parser.feed(text[i:i + 3])
    assert frames[:3] == [{}, {"type": 6}, {"type": 6}]
    assert frames[3]["arguments"][0] == "Heartbeat" and len(frames) == 4


def test_recorded_snapshot_of_the_real_feed():
    fake = FakeSignalR([SNAPSHOT], chunk=1000)
    events = collect(fake)
    kf = events[0]
    assert isinstance(kf, livefeed.Keyframes)
    assert kf.topics["SessionInfo"]["Key"] == KEY and kf.topics["SessionInfo"]["Path"] == Q_PATH
    assert {"DriverList", "TimingData", "TimingAppData", "TrackStatus", "RaceControlMessages", "WeatherData",
            "SessionStatus", "ExtrapolatedClock"} <= set(kf.topics)
    assert "Position.z" not in kf.topics  # anonymous: no car positions
    assert "_kf" not in kf.topics["TimingData"]
    assert len(events) >= 2 and all(isinstance(e, livefeed.Ping) for e in events[1:])
    handshake, subscribe = (json.loads(p.rstrip("\x1e")) for p in fake.posts[:2])
    assert handshake == {"protocol": "json", "version": 1}
    assert subscribe["target"] == "Subscribe" and subscribe["arguments"] == [livefeed.TOPICS]


def test_capture_connected_after_the_end_keeps_the_final_state(tmp_path):
    kf = collect(FakeSignalR([SNAPSHOT]))[0]
    store = SessionStore(tmp_path)
    now = T0 + 6 * 3600
    cap = LiveCapture(KEY, CaptureWriter(store, KEY), store)
    cap.handle(kf, now)
    assert cap.ours and cap.done  # SessionStatus is already "Ends"
    meta = store.meta(KEY)
    assert (meta["source"], meta["t0"], meta["meeting"], meta["path"]) == ("live", now, 1308, Q_PATH)
    st = SessionTimeline(store.dir(KEY), now).state(now + 1)
    assert st["status"] == "Ends" and len(st["tower"]) == 22
    assert (st["tower"][0]["code"], st["tower"][0]["best"]) == ("VER", "1:35.130")
    assert SessionTimeline(store.dir(KEY), now).state(now - 1)["available"] is False  # nothing before receipt

    race = LiveCapture(11731, CaptureWriter(store, 11731), store)
    race.handle(kf, now)  # the feed still shows qualifying: not ours, nothing written
    assert not race.ours and store.meta(11731) is None


def test_capture_stops_when_the_feed_moves_on(tmp_path):
    store = SessionStore(tmp_path)
    cap = LiveCapture(KEY, CaptureWriter(store, KEY), store)
    cap.handle(livefeed.Keyframes({"SessionInfo": {"Key": KEY}}), T0)
    cap.handle(livefeed.Feed("TrackStatus", {"Status": "2", "Message": "Yellow"}, T0 + 1), T0 + 1)
    cap.handle(livefeed.Ping(), T0 + 2)
    assert not cap.done
    cap.handle(livefeed.Feed("SessionInfo", {"Key": 11731}, T0 + 3), T0 + 3)
    assert cap.done
    assert (store.dir(KEY) / "TrackStatus.jsonStream").read_text() == '00:00:01.000{"Status":"2","Message":"Yellow"}\n'


# ---- live capture == archive ------------------------------------------------------


def upload_positions(c: TestClient, archive_dir, batch: int = 8) -> int:
    """What the extension does: inflate Position.z and POST batches (~2 s each)."""
    msgs = [m for _, raw in read_stream(archive_dir / "Position.z.jsonStream") for m in inflate(json.loads(raw))["Position"]]
    stored = 0
    for i in range(0, len(msgs), batch):
        r = c.post(f"/api/timing/session/{KEY}/positions", json={"messages": msgs[i:i + batch]})
        assert r.status_code == 200, r.text
        stored += r.json()["stored"]
    assert stored == len(msgs)
    return stored


def assert_same_session(a, b):
    random.seed(11)
    moments = [T0 + random.uniform(5, 4800) for _ in range(40)] + [utc("2026-10-03T09:04:30")]
    assert len(b.state(KEY, moments[-1])["tower"]) == 22
    for t in moments:
        assert json.dumps(a.state(KEY, t), sort_keys=True) == json.dumps(b.state(KEY, t), sort_keys=True), t
    w = utc("2026-10-03T08:57:20")
    for k in range(4):
        assert a.positions(KEY, w + 60 * k, w + 60 * k + 60) == b.positions(KEY, w + 60 * k, w + 60 * k + 60)
    assert a.track(KEY)["outline"] == b.track(KEY)["outline"]


def test_live_capture_plus_uploads_rebuild_the_archived_session(archived_root, tmp_path):
    a = make_service(clone(archived_root, tmp_path / "a"))
    msgs = archive_messages(a.store.dir(KEY))
    info = next(m for m in msgs if m[1] == "SessionInfo")
    fake = FakeSignalR([sse([{}, keyframes({"SessionInfo": info[2]}), *feed(m for m in msgs if m is not info)])])
    b = make_service(tmp_path / "b", handler=fake.handler)
    b.clock = lambda: T0
    asyncio.run(b._capture(QUALI))
    meta = b.store.meta(KEY)
    assert (meta["source"], meta["ended"], meta["meeting"], meta["path"]) == ("live", True, 1308, Q_PATH)
    assert fake.connections == 1  # SessionStatus "Ends" closed the capture
    with TestClient(app_for(b)) as c:
        upload_positions(c, a.store.dir(KEY))
    assert_same_session(a, b)


def test_reconnect_mid_session_resumes_from_keyframes(archived_root, tmp_path):
    a = make_service(clone(archived_root, tmp_path / "a"))
    msgs = archive_messages(a.store.dir(KEY))
    info = next(m for m in msgs if m[1] == "SessionInfo")
    cut = utc("2026-10-03T08:40:00") - T0  # the connection drops at the end of Q2
    before = [m for m in msgs if m[0] <= cut and m is not info]
    after = [m for m in msgs if m[0] > cut]
    tl = a.timeline(KEY)
    state_at_cut = {name: topic.at(cut) for name, topic in tl.topics.items()}  # what the server re-sends
    fake = FakeSignalR([
        sse([{}, keyframes({"SessionInfo": info[2]}), *feed(before)]),
        sse([{}, keyframes(state_at_cut), *feed(after)]),
    ])
    b = make_service(tmp_path / "b", handler=fake.handler, retry_seconds=0)
    b.clock = lambda: T0 if fake.connections < 2 else T0 + cut
    asyncio.run(b._capture(QUALI))
    assert fake.connections == 2 and b.store.meta(KEY)["ended"]
    with TestClient(app_for(b)) as c:
        upload_positions(c, a.store.dir(KEY))
    assert_same_session(a, b)


@pytest.mark.skipif(not os.environ.get("FONE_LIVE_TEST"), reason="set FONE_LIVE_TEST=1 to use the real feed")
def test_real_feed_answers_anonymously():
    async def first_event():
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, read=20)) as c:
            async with aclosing(livefeed.stream(c)) as events:
                async for ev in events:
                    return ev

    ev = asyncio.run(asyncio.wait_for(first_event(), 20))
    assert isinstance(ev, livefeed.Keyframes)
    assert isinstance(ev.topics["SessionInfo"]["Key"], int) and "TimingData" in ev.topics
