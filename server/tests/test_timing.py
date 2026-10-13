"""Timing service against a real archived session: 2026 Kuala Lumpur qualifying (11730).

Fixtures in fixtures/timing/static/ mirror livetiming.formula1.com/static/ for that session,
downloaded once: the streams the dashboard reads (big ones gzipped), Position.z trimmed to
the ~3 minutes around the fastest lap, and the year index trimmed to the meeting.
"""

import asyncio
import gzip
import json
import math
import random
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fone import config
from fone.timing import STATIC, TimingService, TimingSettings, _Attempt, make_router
from fone.timing_state import OFFSET_LEN, SessionTimeline, iso, parse_offset, secs

FIX = Path(__file__).parent / "fixtures"
TFIX = FIX / "timing"
STATIC_DIR = TFIX / "static"
KEY = 11730
Q_PATH = "2026/2026-10-04_Bahrain_Grand_Prix/2026-10-03_Qualifying/"
T0 = iso("2026-10-03T07:47:14.597Z")  # AudioStreams keyframe Utc


def static_file(rel: str) -> bytes | None:
    f = STATIC_DIR / rel
    if f.is_file():
        return f.read_bytes()
    gz = f.with_name(f.name + ".gz")
    return gzip.decompress(gz.read_bytes()) if gz.is_file() else None


def archive_handler(request: httpx.Request, overrides: dict | None = None) -> httpx.Response:
    url = str(request.url)
    if url.startswith(STATIC + "/"):
        rel = url[len(STATIC) + 1:]
        data = (overrides or {}).get(rel, static_file(rel))
        if data is None:  # what the archive answers for anything missing or not yet published
            return httpx.Response(403, content=b"<Error><Code>AccessDenied</Code></Error>")
        return httpx.Response(200, content=data)
    if "api.openf1.org/v1/sessions" in url:
        return httpx.Response(200, content=(FIX / "openf1_sessions_1308.json").read_bytes())
    return httpx.Response(404)


def make_service(data_dir: Path, handler=archive_handler, **settings) -> TimingService:
    cfg = replace(config.load(), data_dir=data_dir)
    opts = {"record_live": False, "refresh_seconds": 0, **settings}
    return TimingService(cfg, None, TimingSettings(**opts), transport=httpx.MockTransport(handler))


def schedule_rows() -> list[dict]:
    return json.loads((FIX / "openf1_sessions_1308.json").read_text())


@pytest.fixture(scope="session")
def archived_root(tmp_path_factory) -> Path:
    """Data dir with 11730 fetched (through the real download code) from the mocked archive."""
    root = tmp_path_factory.mktemp("timing-archive")
    svc = make_service(root)
    assert asyncio.run(svc.fetch_archive(KEY)) == "ok"
    return root


def clone(archived_root: Path, tmp_path: Path) -> Path:
    shutil.copytree(archived_root / "timing", tmp_path / "timing")
    return tmp_path


@pytest.fixture
def svc(archived_root, tmp_path) -> TimingService:
    return make_service(clone(archived_root, tmp_path))


def utc(s: str) -> float:
    return iso(s + "Z")


# ---- archive ------------------------------------------------------------------


def test_archive_fetch_installs_the_session(archived_root):
    d = archived_root / "timing" / str(KEY)
    meta = json.loads((d / "meta.json").read_text())
    assert meta["source"] == "archive" and meta["complete"] is True
    assert meta["t0"] == pytest.approx(T0, abs=1e-6)
    assert meta["meeting"] == 1308 and meta["path"] == Q_PATH
    assert (d / "TimingData.jsonStream").stat().st_size > 500_000
    # Listed in the Index but not served (403) -> skipped, not fatal.
    assert not (d / "TimingStats.jsonStream").exists()
    assert not list((archived_root / "timing").glob(".*"))  # staging dir cleaned up


def test_unpublished_archive(tmp_path):
    svc = make_service(tmp_path, handler=lambda r: archive_handler(r, {Q_PATH + "Index.json": None}))
    assert asyncio.run(svc.fetch_archive(KEY)) == "not published"
    assert svc.store.meta(KEY) is None


def test_archive_path_derived_for_a_session_the_index_does_not_list_yet(tmp_path):
    svc = make_service(tmp_path)
    svc._set_schedule(schedule_rows())
    # The race isn't archived, so the year index has no Path for it: built from a sibling's.
    assert asyncio.run(svc.archive_path(11731)) == "2026/2026-10-04_Bahrain_Grand_Prix/2026-10-04_Race/"
    assert asyncio.run(svc.archive_path(KEY)) == Q_PATH
    assert asyncio.run(svc.archive_path(99999)) is None


# ---- state ----------------------------------------------------------------------


def test_state_at_the_end_of_q3_matches_the_official_result(svc):
    st = svc.state(KEY, utc("2026-10-03T09:04:30"))
    assert st["available"] and st["status"] == "Finalised" and st["part"] == 3
    assert st["session"] == {"key": KEY, "name": "Qualifying", "type": "Qualifying", "meeting": "Bahrain Grand Prix"}
    assert st["track"]["flag"] == "CHEQUERED" and st["clock"]["remaining"] == "00:00"
    official = {r["driver_number"]: r for r in json.loads((TFIX / "openf1_session_result_11730.json").read_text())}
    assert len(st["tower"]) == len(official) == 22
    assert [r["pos"] for r in st["tower"]] == list(range(1, 23))
    for row in st["tower"]:
        o = official[int(row["num"])]
        assert row["pos"] == o["position"], row
        # Each driver's time is from the part they went out in (the TV tower's convention).
        part = max(i for i, d in enumerate(o["duration"]) if d is not None)
        assert secs(row["best"]) == pytest.approx(o["duration"][part], abs=1e-6), row
        assert (secs(row["gap"]) or 0.0) == pytest.approx(o["gap_to_leader"][part], abs=1e-6), row
        assert row["knockedOut"] == (o["position"] > 10)
    ver = st["tower"][0]
    assert (ver["code"], ver["name"], ver["team"], ver["color"]) == ("VER", "Max Verstappen", "Red Bull Racing", "#4781D7")
    assert ver["tyre"] == "SOFT" and ver["newTyre"] and ver["tyreAge"] == 3
    assert st["weather"] == {"air": 32.7, "track": 53.4, "humidity": 50.2, "rain": False, "windSpeed": 0.4,
                             "windDir": 274}


def test_state_during_the_session(svc):
    st = svc.state(KEY, utc("2026-10-03T08:05:00"))
    assert (st["status"], st["part"], st["track"]["flag"]) == ("Started", 1, "GREEN")
    assert st["clock"] == {"remaining": "13:00", "lap": None, "laps": None}  # Q1 = 18 min from 08:00
    st = svc.state(KEY, utc("2026-10-03T08:05:00.5"))
    assert st["clock"]["remaining"] == "13:00"  # counts down in whole seconds, rounded up
    # Q1 chequered flag: race control's latest message shows for a minute.
    st = svc.state(KEY, utc("2026-10-03T08:18:30"))
    assert st["status"] == "Finished" and st["track"]["flag"] == "CHEQUERED"
    assert st["track"]["message"] == "FIRST CAR TO TAKE THE FLAG - CAR 31 (OCO)"
    assert svc.state(KEY, utc("2026-10-03T08:20:00"))["track"]["message"] is None
    q2 = svc.state(KEY, utc("2026-10-03T08:39:00"))
    assert q2["part"] == 2 and sum(r["knockedOut"] for r in q2["tower"]) == 6


def test_no_state_before_the_first_message(svc):
    st = svc.state(KEY, T0 - 5)
    assert st["available"] is False and st["reason"]


def truncated_copy(src: Path, dst: Path, cut: float) -> None:
    """The session as it would look if nothing after offset `cut` had happened yet."""
    dst.mkdir()
    shutil.copy(src / "meta.json", dst / "meta.json")
    for f in src.glob("*.jsonStream"):
        keep, last = [], -math.inf
        for ln in f.read_bytes().split(b"\n"):
            body = ln.removeprefix(b"\xef\xbb\xbf")
            if len(body) <= OFFSET_LEN:
                continue
            last = max(last, parse_offset(body[:OFFSET_LEN].decode()))  # the reader's clamp
            if last > cut:
                break
            keep.append(body + b"\n")
        (dst / f.name).write_bytes(b"".join(keep))


def test_state_never_depends_on_anything_after_t(svc, tmp_path):
    full = svc.timeline(KEY)
    random.seed(7)
    for i in range(12):
        t = T0 + random.uniform(60, 4800)
        cut_dir = tmp_path / f"cut{i}"
        truncated_copy(svc.store.dir(KEY), cut_dir, t - T0)
        past = SessionTimeline(cut_dir, T0)
        assert json.dumps(full.state(t), sort_keys=True) == json.dumps(past.state(t), sort_keys=True)


def test_positions_window(svc):
    start = utc("2026-10-03T08:58:00")
    cars = svc.positions(KEY, start, start + 300)["cars"]  # asks for 5 min, gets the 120 s cap
    assert len(cars) >= 8
    for samples in cars.values():
        ts = [s[0] for s in samples]
        assert ts == sorted(ts) and ts[-1] <= start + 120 and ts[1] >= start
        assert all(len(s) == 3 and isinstance(s[1], int) and isinstance(s[2], int) for s in samples)
    assert svc.positions(KEY, start, start - 1)["cars"] == {}


def test_track_outline_from_the_fastest_lap(svc):
    tr = svc.track(KEY)
    assert tr["available"] and tr["source"]["driver"] == "3" and tr["source"]["lap"] == 95.13
    pts = tr["outline"]
    assert 300 < len(pts) < 450  # ~3.7 Hz over a 95 s lap
    length = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    assert 3000 < length / 10 < 7000  # metres (F1 units are decimetres)
    assert math.dist(pts[0], pts[-1]) < 0.02 * length
    x0, y0, x1, y1 = tr["bounds"]
    assert all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in pts)
    assert -90 <= tr["rotation"] <= 90
    assert (svc.store.root / "tracks" / "1308.json").exists()
    assert svc.track(KEY) == tr  # served from the per-meeting cache


def test_track_for_a_session_without_positions_comes_from_the_weekend(svc):
    # A live race capture: anonymous feed, no car positions of its own.
    race = svc.store.dir(11731)
    race.mkdir()
    svc.store.write_meta(11731, {"key": 11731, "source": "live", "t0": T0 + 86400, "meeting": 1308})
    tr = svc.track(11731)
    assert tr["available"] and tr["source"]["session"] == KEY


def test_loaded_sessions_are_an_lru(archived_root, tmp_path):
    svc = make_service(clone(archived_root, tmp_path), max_loaded=1)
    other = svc.store.dir(11729)
    shutil.copytree(svc.store.dir(KEY), other)
    first = svc.timeline(KEY)
    assert svc.timeline(KEY) is first
    svc.timeline(11729)
    assert list(svc._loaded) == [11729]
    assert svc.timeline(KEY) is not first


def test_archive_replaces_a_live_capture(tmp_path):
    svc = make_service(tmp_path)
    t = utc("2026-10-03T08:30:00")
    msg = {"Timestamp": "2026-10-03T08:30:00.100Z", "Entries": {"1": {"X": 1, "Y": 2, "Z": 3, "Status": "OnTrack"}}}
    assert svc.ingest_positions(KEY, [msg])["stored"] == 1
    assert svc.store.meta(KEY)["source"] == "live"
    assert svc.positions(KEY, t - 1, t + 1)["cars"] == {"1": [[t + 0.1, 1, 2]]}
    # A still-generating archive doesn't displace the capture...
    partial = make_service(tmp_path, handler=lambda r: archive_handler(
        r, {Q_PATH + "ArchiveStatus.json": b'{"Status":"Generating"}'}))
    assert asyncio.run(partial.fetch_archive(KEY)) == "partial"
    assert svc.store.meta(KEY)["source"] == "live"
    # ...the complete one does, and readers switch over.
    capture_writer = svc._writer(KEY)
    assert asyncio.run(svc.fetch_archive(KEY)) == "ok"
    track_status = (svc.store.dir(KEY) / "TrackStatus.jsonStream").read_bytes()
    capture_writer.write("TrackStatus", {"Status": "5", "Message": "Red"}, t)  # a capture still running
    assert (svc.store.dir(KEY) / "TrackStatus.jsonStream").read_bytes() == track_status
    assert svc.store.meta(KEY)["source"] == "archive"
    assert svc.positions(KEY, t - 1, t + 1)["cars"] == {}  # the archive's (trimmed) positions start later
    assert svc.state(KEY, t)["part"] == 2
    assert svc.ingest_positions(KEY, [msg]) == {"ok": True, "stored": 0, "reason": "archived"}


def test_position_uploads_are_deduplicated_and_validated(tmp_path):
    svc = make_service(tmp_path)

    def m(ts, x):
        return {"Timestamp": ts, "Entries": {"44": {"X": x, "Y": 7, "Z": 0, "Status": "OnTrack"}}}

    batch = [m("2026-10-03T08:00:01.000Z", 10), m("2026-10-03T08:00:00.000Z", 5), {"Timestamp": "bad"}, {"x": 1}]
    assert svc.ingest_positions(KEY, batch)["stored"] == 2
    assert svc.ingest_positions(KEY, batch)["stored"] == 0  # a second tab re-sending the same
    assert svc.ingest_positions(KEY, [m("2026-10-03T08:00:02.000Z", 20)])["stored"] == 1
    t = utc("2026-10-03T08:00:00")
    assert [s[1] for s in svc.positions(KEY, t, t + 5)["cars"]["44"]] == [5, 10, 20]


# ---- schedule -------------------------------------------------------------------


def test_capture_and_archive_plan(tmp_path):
    svc = make_service(tmp_path, record_live=True)
    svc._set_schedule(schedule_rows())
    race = utc("2026-10-04T07:00:00")
    assert svc.plan(race - 20 * 60)[0] == []
    assert svc.plan(race - 14 * 60)[0] == [11731]
    assert svc.plan(race + 3.9 * 3600)[0] == [11731]
    assert svc.plan(race + 4 * 3600)[0] == []
    off = make_service(tmp_path)
    off._set_schedule(schedule_rows())
    assert off.plan(race - 60)[0] == []  # recording disabled

    q_end = utc("2026-10-03T09:00:00")
    assert KEY not in svc.plan(q_end + 5 * 60)[1]  # archive appears ~20 min after the end
    fetches = svc.plan(q_end + 11 * 60)[1]
    assert KEY in fetches and 11731 not in fetches
    svc._attempts[KEY] = _Attempt(q_end + 11 * 60, "not published")
    assert KEY not in svc.plan(q_end + 14 * 60)[1] and KEY in svc.plan(q_end + 16 * 60)[1]
    assert KEY not in svc.plan(q_end + 4 * 86400)[1]  # too old to fetch unasked

    # A capture that saw "Ends" isn't restarted; an archived session is left alone.
    svc.store.write_meta(11731, {"key": 11731, "source": "live", "t0": race, "ended": True})
    assert svc.plan(race + 3600)[0] == []
    svc.store.write_meta(KEY, {"key": KEY, "source": "archive", "t0": T0, "complete": True})
    assert KEY not in svc.plan(q_end + 16 * 60)[1]


def test_old_sessions_are_pruned(svc):
    meta = svc.store.meta(KEY)
    assert svc.prune(meta["fetched"] + 29 * 86400) == []
    svc.timeline(KEY)
    assert svc.prune(meta["fetched"] + 31 * 86400) == [KEY]
    assert not svc.store.dir(KEY).exists() and KEY not in svc._loaded


def test_schedule_refresh_is_cached_on_disk(tmp_path):
    svc = make_service(tmp_path)
    asyncio.run(svc._refresh_schedule(time.time()))
    assert {11727, 11728, 11729, 11730, 11731} <= set(svc.schedule)
    assert make_service(tmp_path, handler=lambda r: httpx.Response(500)).schedule.keys() == svc.schedule.keys()


# ---- routes -----------------------------------------------------------------------


class StubMapper:
    async def content(self, cid, refresh=False):
        return {"contentId": cid, "sessionKey": KEY}


def app_for(svc: TimingService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_):
        await svc.start()
        yield
        await svc.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(make_router(svc))
    return app


@pytest.fixture
def client(archived_root, tmp_path):
    def handler(request):  # empty calendar: the background loop has nothing to do on its own
        if "api.openf1.org" in str(request.url):
            return httpx.Response(200, json=[])
        return archive_handler(request)

    svc = make_service(clone(archived_root, tmp_path), handler=handler, retry_seconds=0.2)
    svc.mapper = StubMapper()
    with TestClient(app_for(svc)) as c:
        yield c, svc


def test_routes(client):
    c, svc = client
    t = utc("2026-10-03T08:58:00")
    st = c.get(f"/api/timing/session/{KEY}/state", params={"t": t}).json()
    assert st["available"] and st["part"] == 3 and st["t"] == t
    pos = c.get(f"/api/timing/session/{KEY}/positions", params={"from": t, "to": t + 10}).json()
    assert pos["cars"]
    assert c.get(f"/api/timing/session/{KEY}/track").json()["available"]
    assert c.get("/api/timing/content/1000011024").json()["sessionKey"] == KEY
    assert c.get("/api/timing/content/abc").status_code == 400
    assert c.get("/api/timing/session/0/state", params={"t": t}).status_code == 422
    assert c.get(f"/api/timing/session/{KEY}/positions", params={"to": t}).status_code == 422
    body = {"messages": [{"Timestamp": "2026-10-04T07:00:00.000Z",
                          "Entries": {"1": {"X": 1, "Y": 2, "Z": 0, "Status": "OnTrack"}}}]}
    assert c.post("/api/timing/session/11731/positions", json=body).json() == {"ok": True, "stored": 1}
    assert c.get("/api/timing/status").json()["sessions"]["11731"]["source"] == "live"


def test_track_gets_the_circuits_corner_numbers(archived_root, tmp_path):
    asked = []

    def handler(request):
        url = str(request.url)
        if "api.openf1.org" in url:
            return httpx.Response(200, json=[])
        if "api.multiviewer.app" in url:
            asked.append(url)
            return httpx.Response(200, json={"corners": [
                {"number": 1, "angle": 40.1, "trackPosition": {"x": 431.13, "y": 3476.58}},
                {"number": 2, "letter": "a", "angle": -116.3, "trackPosition": {"x": -195.21, "y": 3722.21}}]})
        return archive_handler(request)

    svc = make_service(clone(archived_root, tmp_path), handler=handler)
    with TestClient(app_for(svc)) as c:
        url = f"/api/timing/session/{KEY}/track"
        assert c.get(url).json()["corners"] == []  # the first ask fetches them in the background
        deadline = time.time() + 5
        while not (tr := c.get(url).json())["corners"] and time.time() < deadline:
            time.sleep(0.05)
        assert tr["corners"] == [{"n": 1, "l": "", "x": 431.1, "y": 3476.6, "a": 40.1},
                                 {"n": 2, "l": "a", "x": -195.2, "y": 3722.2, "a": -116.3}]
        info = svc.timeline(KEY).info()
        assert asked == [f"https://api.multiviewer.app/api/v1/circuits/{info['Meeting']['Circuit']['Key']}/2026"]
        assert c.get(url).json()["corners"] == tr["corners"] and len(asked) == 1  # cached per meeting
        assert tr["outline"] and tr["available"]


def test_track_without_corner_data_still_serves_the_outline(archived_root, tmp_path):
    def handler(request):
        if "api.openf1.org" in str(request.url):
            return httpx.Response(200, json=[])
        if "api.multiviewer.app" in str(request.url):
            return httpx.Response(404)
        return archive_handler(request)

    svc = make_service(clone(archived_root, tmp_path), handler=handler)
    with TestClient(app_for(svc)) as c:
        url = f"/api/timing/session/{KEY}/track"
        for _ in range(5):
            tr = c.get(url).json()
            time.sleep(0.05)
        assert tr["available"] and tr["outline"] and tr["corners"] == []


def wait_attempt(svc, key, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        a = svc._attempts.get(key)
        if a and a.result != "downloading" and key not in svc._fetching:
            return a.result
        time.sleep(0.05)
    raise AssertionError("no fetch attempt")


def test_missing_sessions_report_why_and_fetch_in_the_background(client):
    c, svc = client
    get = lambda key: c.get(f"/api/timing/session/{key}/state", params={"t": T0}).json()  # noqa: E731
    assert get(11729) == {"available": False, "reason": "downloading the timing archive"}
    # FP3 is in the year index but not in the fixtures: the archive answers 403, as before publication.
    assert wait_attempt(svc, 11729) == "not published"
    assert get(11729)["reason"] == "timing archive not published yet"
    get(424242)
    assert wait_attempt(svc, 424242) == "unknown session"
    assert get(424242)["reason"] == "no timing data for this session"
