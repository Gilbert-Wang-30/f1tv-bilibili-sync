import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fone import backfill as bf
from fone import backfill_bili as bb
from fone import backfill_dedup, config, recordings
from fone.backfill import BackfillService, Settings, make_router, parse_rooms, subtract
from fone.backfill_dedup import JoinFinder, Timeline, load_part
from fone.backfill_writer import import_name
from fone.extractor import Extractor, Piece, Segment
from fone.store import Store
from test_backfill_synth import PKT, ff, synth, write_frames  # noqa: F401  (fixture)

FIX = Path(__file__).parent / "fixtures" / "backfill"
ROOM, UID, CHANNEL = 23954427, 1313650809, "胡闹P房"
KL_LIVE = 1791010991.0
KL_TITLE = "【胡闹P房】16点 巴林 排位赛"


# ---- fakes ----------------------------------------------------------------------------------------


@dataclass
class Rec:
    channel: str
    name_time: float | None
    mtime: float
    relpath: str = "x.flv"


@dataclass
class FileState:
    rec: Rec
    status: str = "done"
    out_dir: Path = Path("/nowhere")
    pieces: list = field(default_factory=list)
    segs: list = field(default_factory=list)
    provisional: bool = False


class FakeExtractor:
    def __init__(self):
        self.files: dict[str, FileState] = {}
        self.ensured = []

    def add(self, fid, pieces, status="done", channel=CHANNEL, seg=2.0, out_dir=Path("/nowhere"), provisional=False):
        end = max(p.media + p.duration for p in pieces)
        segs = [Segment(f"seg_{i:05d}.ts", i * seg, seg) for i in range(int(end / seg))]
        lo = min(p.wall for p in pieces)
        hi = max(p.wall + p.duration for p in pieces)
        self.files[fid] = FileState(Rec(channel, lo, hi), status, out_dir, pieces, segs, provisional)
        return self.files[fid]

    def pieces(self, st):
        return st.pieces, st.segs, st.provisional

    def ensure(self, st):
        self.ensured.append(st)


class FakeBili(bb.BiliClient):
    def __init__(self, archives=None, pages=None):
        super().__init__(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(599))))
        self.archives = archives if archives is not None else \
            json.loads((FIX / "series_archives.json").read_text())["data"]["archives"]
        self.page_list = pages or json.loads((FIX / "pagelist.json").read_text())["data"]
        self.statuses: list = []
        self.raise_next: Exception | None = None
        self.log: list[str] = []

    async def api(self, url, params=None, referer=""):
        raise AssertionError(f"unexpected API call {url}")

    def _hit(self, what):
        self.log.append(what)
        if self.raise_next:
            e, self.raise_next = self.raise_next, None
            raise e

    async def room_info(self, room):
        self._hit("room")
        return self.statuses.pop(0)

    async def series_id(self, mid):
        self._hit("series_id")
        return 4169347

    async def series_archives(self, mid, sid, since, max_pages=3):
        self._hit("archives")
        return self.archives

    async def pagelist(self, bvid):
        self._hit("pagelist")
        return self.page_list

    async def playurl(self, bvid, cid):
        self._hit("playurl")
        return {"audio": [{"id": 30280, "baseUrl": f"https://cdn/{cid}-30280.m4s"}],
                "video": [{"id": 80, "codecid": 7, "height": 1080, "bandwidth": 1, "baseUrl": f"https://cdn/{cid}-30080.m4s"}]}


class FakeDownloader:
    """Hands out local files instead of CDN downloads (but asks for the URLs like the real one)."""

    def __init__(self, files: dict[str, Path]):
        self.files, self.asked = files, []

    async def fetch(self, urls_fn, dest, progress=None):
        self.asked += await urls_fn()
        shutil.copyfile(self.files[dest.name], dest)
        if progress:
            progress(dest.stat().st_size, dest.stat().st_size)
        return dest.stat().st_size

    async def close(self):
        pass


def make_service(tmp_path, extractor=None, bili=None, downloader=None, **settings):
    cfg = replace(config.load(), data_dir=tmp_path / "data", rec_dir=tmp_path / "rec")
    store = Store(cfg.data_dir / "fone.sqlite3")
    s = Settings(rooms={ROOM: CHANNEL}, import_dir=tmp_path / "import", danmakus=False, **settings)
    return BackfillService(cfg, extractor or FakeExtractor(), store, s, bili=bili or FakeBili(),
                           downloader=downloader or FakeDownloader({}),
                           http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(599))))


def status(live, live_time=KL_LIVE, title=KL_TITLE):
    return bb.RoomStatus(ROOM, UID, 1 if live else 0, live_time if live else None, title)


# ---- settings & helpers ---------------------------------------------------------------------------


def test_settings_from_env():
    s = Settings.from_env({"FONE_ROOMS": "23954427=胡闹P房, 5=x ,bad", "FONE_IMPORT_DIR": "/rec/fone-import",
                           "FONE_BACKFILL_VIDEO": "720p"})
    assert s.rooms == {23954427: "胡闹P房", 5: "x"} and s.import_dir == Path("/rec/fone-import")
    assert s.video_height == 720 and s.enabled and s.danmakus
    assert Settings.from_env({"FONE_BACKFILL_VIDEO": "none"}).video_height is None
    assert not Settings.from_env({}).enabled
    assert parse_rooms("") == {}


def test_subtract():
    assert subtract((0, 100), [(10, 20), (15, 50), (90, 200)], 5) == [[0, 10], [50, 90]]
    assert subtract((0, 100), [(-5, 3), (97, 100)], 5) == [[3, 97]]
    assert subtract((0, 100), [(0, 100)], 5) == []
    assert subtract((0, 100), [], 60) == [[0, 100]]


# ---- live log & gap detection ---------------------------------------------------------------------


def test_live_log_periods(tmp_path):
    svc = make_service(tmp_path)
    svc.record(status(True), CHANNEL, KL_LIVE + 30)
    svc.record(status(True), CHANNEL, KL_LIVE + 90)
    (p,) = svc.db.periods(ROOM)
    assert (p["start"], p["last_live"], p["stop"], p["title"], p["uid"]) == (KL_LIVE, KL_LIVE + 90, None, KL_TITLE, UID)
    # Restart: a new 开播 closes the old period and opens a new one.
    svc.record(status(True, KL_LIVE + 600, "new title"), CHANNEL, KL_LIVE + 610)
    new, old = svc.db.periods(ROOM)
    assert old["stop"] == KL_LIVE + 610 and new["start"] == KL_LIVE + 600 and new["stop"] is None
    # Rerun loop (live_status 2) is not live.
    svc.record(bb.RoomStatus(ROOM, UID, 2, None, "rerun"), CHANNEL, KL_LIVE + 9000)
    assert all(p["stop"] is not None for p in svc.db.periods(ROOM))
    assert svc.rooms[ROOM]["liveStatus"] == 2 and not svc.rooms[ROOM]["live"]
    assert len(svc.db.periods(pending=True)) == 2


def test_poll_rooms_backs_off_on_risk_control(tmp_path):
    bili = FakeBili()
    bili.raise_next = bb.RiskControl("HTTP 412")
    svc = make_service(tmp_path, bili=bili)
    asyncio.run(svc.poll_rooms())
    assert "412" in svc.rooms[ROOM]["error"] and svc.db.periods(ROOM) == []
    bili.statuses = [status(True)]
    asyncio.run(svc.poll_rooms())
    assert svc.db.periods(ROOM)[0]["start"] == KL_LIVE


def end_period(svc, start=KL_LIVE, length=3600, now=None):
    svc.record(status(True, start), CHANNEL, start + 60)
    svc.record(status(True, start), CHANNEL, start + length)
    svc.record(status(False), CHANNEL, now or start + length + 60)


def test_gap_detection_waits_for_extraction_then_schedules(tmp_path):
    ex = FakeExtractor()
    svc = make_service(tmp_path, extractor=ex)
    end_period(svc)
    S = KL_LIVE
    # Recorder joined 8 s late (ignored, < 60 s) and lost 250 s mid-stream.
    f = ex.add("a", [Piece(S + 8, 0, 992), Piece(S + 1250, 992, 2350)], status="running")
    ex.add("other", [Piece(S, 0, 3600)], channel="someone else")
    assert svc.check_periods(now=S + 3700) == []  # still extracting
    f.status = "done"
    (job,) = svc.check_periods(now=S + 3720)
    assert job.data["gaps"] == [[S + 1000, S + 1250]] and job.data["window"] == [S + 1000, S + 1250]
    assert job.data["liveTime"] == S and job.data["title"] == KL_TITLE and job.data["source"] == "auto"
    assert svc.check_periods(now=S + 3800) == []  # checked once


def test_gap_detection_gives_up_waiting_and_ensures_idle_files(tmp_path):
    ex = FakeExtractor()
    svc = make_service(tmp_path, extractor=ex)
    end_period(svc)
    idle = ex.add("old", [Piece(KL_LIVE, 0, 600)], status="idle")
    assert svc.check_periods(now=KL_LIVE + 3700) == [] and ex.ensured == [idle]
    (job,) = svc.check_periods(now=KL_LIVE + 3660 + svc.settings.settle + 1)
    assert job.data["gaps"] == [[KL_LIVE + 600, KL_LIVE + 3600]]


def test_no_recordings_known_yet_schedules_nothing(tmp_path):
    """Startup races the first scan of the NAS (and an unreachable NAS scans as empty):
    no files at all must not read as "the whole stream is missing"."""
    ex = FakeExtractor()
    svc = make_service(tmp_path, extractor=ex)
    end_period(svc)
    assert svc.check_periods(now=KL_LIVE + 4000) == [] and svc.status()["jobs"] == []
    ex.add("a", [Piece(KL_LIVE + 5, 0, 1000)])  # the scan lands: now the real gap counts
    (job,) = svc.check_periods(now=KL_LIVE + 4000)
    assert job.data["gaps"] == [[KL_LIVE + 1005, KL_LIVE + 3600]]


def test_partial_imports_are_swept(tmp_path):
    svc = make_service(tmp_path)
    d = svc.settings.import_dir / CHANNEL
    d.mkdir(parents=True)
    old, fresh, done = d / "a.flv.part", d / "b.flv.part", d / "c.flv"
    for p in (old, fresh, done):
        p.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    os.utime(done, (time.time() - 7200, time.time() - 7200))
    svc._sweep_partial_imports()
    assert not old.exists() and fresh.exists() and done.exists()


def test_fully_recorded_stream_schedules_nothing(tmp_path):
    ex = FakeExtractor()
    svc = make_service(tmp_path, extractor=ex)
    end_period(svc)
    ex.add("a", [Piece(KL_LIVE + 5, 0, 3600)])
    assert svc.check_periods(now=KL_LIVE + 4000) == []
    assert svc.status()["jobs"] == []


# ---- jobs -----------------------------------------------------------------------------------------


def test_job_finds_the_replay_pinned_by_live_time(tmp_path):
    bili = FakeBili()
    svc = make_service(tmp_path, bili=bili)
    gap = [KL_LIVE + 1000, KL_LIVE + 1250]
    job = svc.submit(CHANNEL, tuple(gap), gaps=[gap], room=ROOM, uid=UID, live_time=KL_LIVE, title=KL_TITLE,
                     source="auto")
    asyncio.run(svc.step(job))
    job = svc.db.job(job.id)
    assert job.state == "downloading" and job.error is None
    assert job.data["bvid"] == "BV1ePHq6DE8B" and job.data["startExact"] and job.data["liveTime"] == KL_LIVE
    # The series listing is cached: a second job doesn't list again.
    job2 = svc.submit(CHANNEL, tuple(gap), gaps=[gap], room=ROOM, uid=UID, live_time=KL_LIVE, source="auto")
    asyncio.run(svc.step(job2))
    assert bili.log.count("archives") == 1 and bili.log.count("series_id") == 1


def test_job_waits_for_the_replay_then_expires(tmp_path):
    svc = make_service(tmp_path, give_up_after=3600)
    t = time.time()
    job = svc.submit(CHANNEL, (t - 600, t - 300), gaps=[[t - 600, t - 300]], room=ROOM, uid=UID, live_time=t - 900)
    asyncio.run(svc.step(job))
    job = svc.db.job(job.id)
    assert job.state == "waiting" and job.next_try > t + 500
    old = svc.submit(CHANNEL, (t - 9000, t - 8000), room=ROOM, uid=UID, live_time=t - 9500)
    asyncio.run(svc.step(old))
    assert svc.db.job(old.id).state == "expired"


def test_manual_window_takes_the_one_possible_replay(tmp_path):
    """No live log for this stream: the window at the very end of the 15点 slot's stream can only
    'maybe' overlap (its exact start is unknown), but nothing else could be meant."""
    bili = FakeBili()
    bili.statuses = [status(False)]  # the uploader's uid comes from the room
    svc = make_service(tmp_path, bili=bili)
    end = KL_LIVE + 10379
    job = svc.submit(CHANNEL, (end - 200, end - 100))
    asyncio.run(svc.step(job))
    job = svc.db.job(job.id)
    assert job.state == "downloading" and job.data["bvid"] == "BV1ePHq6DE8B" and not job.data["startExact"]
    assert job.data["uid"] == UID and bili.log[0] == "room"


def test_job_errors_back_off_and_eventually_fail(tmp_path):
    bili = FakeBili()
    svc = make_service(tmp_path, bili=bili)
    job = svc.submit(CHANNEL, (KL_LIVE, KL_LIVE + 100), room=ROOM, uid=UID)
    bili.raise_next = bb.RiskControl("code -352")
    asyncio.run(svc.step(job))
    j = svc.db.job(job.id)
    assert j.state == "waiting" and j.attempts == 0 and j.next_try > time.time() + 800 and "352" in j.error
    for n in range(1, 6):
        bili.raise_next = bb.BiliError("boom")
        svc._series.clear()
        asyncio.run(svc.step(j))
        j = svc.db.job(job.id)
        assert j.attempts == n
    assert j.state == "failed" and "boom" in j.error


def test_end_to_end_job_from_a_synthetic_replay(tmp_path, synth, monkeypatch):
    """Gap -> series match -> download -> dedup -> place by live_time -> FLV in the import dir."""
    monkeypatch.setattr(backfill_dedup, "FRAGMENT_S", (25, 50))  # synthetic fragments are ~30 s
    arcs = [{"title": f"【直播回放】{KL_TITLE} 2026年10月03日15点场", "bvid": "BV1synthetic", "duration": 110,
             "pubdate": int(KL_LIVE + 3600)}]
    pages = [{"cid": 11, "page": 1, "part": f"0-{KL_TITLE}", "duration": 65},
             {"cid": 12, "page": 2, "part": f"1-{KL_TITLE}", "duration": 45}]
    files = {"p1_audio.m4s": synth.audio[0], "p2_audio.m4s": synth.audio[1],
             "p1_video.m4s": synth.video[0], "p2_video.m4s": synth.video[1]}
    dl = FakeDownloader(files)
    ex = FakeExtractor()
    svc = make_service(tmp_path, extractor=ex, bili=FakeBili(arcs, pages), downloader=dl)
    end_period(svc, length=110, now=KL_LIVE + 170)
    ex.add("rec", [Piece(KL_LIVE + 3, 0, 37)])  # the recorder lost everything after 40 s
    (job,) = svc.check_periods(now=KL_LIVE + 4000)
    for _ in range(3):
        asyncio.run(svc.step(svc.db.job(job.id)))
    job = svc.db.job(job.id)
    assert job.state == "done", job.error
    res = job.data["result"]
    out = Path(res["path"])
    assert out.parent == tmp_path / "import" / CHANNEL and out.exists() and res["video"] == "copy"
    assert out.name.startswith("20261003-150312-") and out.name.endswith(f"-{KL_TITLE}.flv")
    # live_time pins the start: capture = live_time - 0.395, recorder frame = + ARRIVAL_LATENCY.
    assert job.data["placement"]["method"] == "live_time"
    assert abs(res["start"] - (KL_LIVE - 0.395 + 2.09)) < 0.002
    assert [j["kind"] for j in job.data["joins"]] == ["fragment-join", "part-boundary"]
    assert job.data["selected"] == [1, 2]
    assert sorted(dl.asked) == sorted(["https://cdn/11-30280.m4s", "https://cdn/12-30280.m4s",
                                       "https://cdn/11-30080.m4s", "https://cdn/12-30080.m4s"])
    assert not (svc.work_root / "BV1synthetic").exists()  # work files cleaned up
    st = svc.status()
    assert st["jobs"][0]["state"] == "done" and st["jobs"][0]["result"]["path"] == str(out)


def test_recording_windows_stay_inside_pieces(tmp_path):
    ex = FakeExtractor()
    svc = make_service(tmp_path, extractor=ex)
    W = KL_LIVE
    ex.add("a", [Piece(W, 0, 300), Piece(W + 400, 300, 300)], out_dir=tmp_path / "hls" / "a")
    ex.add("p", [Piece(W, 0, 600)], provisional=True)
    wins = svc._recording_windows(CHANNEL, W - 100, W + 2000)
    assert len(wins) == 8
    for paths, wall0 in wins:
        n = [int(p.stem.split("_")[1]) for p in paths]
        assert n == list(range(n[0], n[0] + len(n))) and len(n) >= 15
        assert all(p.parent == tmp_path / "hls" / "a" for p in paths)
        media0 = n[0] * 2.0
        piece = Piece(W, 0, 300) if media0 < 300 else Piece(W + 400, 300, 300)
        assert abs(wall0 - (piece.wall + media0 - piece.media)) < 1e-9
        assert piece.media <= media0 and media0 + len(n) * 2.0 <= piece.media + piece.duration
    assert svc._recording_windows(CHANNEL, W + 5000, W + 6000) == []


def test_alignment_through_a_real_extracted_recording(tmp_path, synth, monkeypatch):
    """The production path: fone's extractor turns a recording into HLS + a wall map; windows are
    cut from those segments and aligned against the replay's pages. The recorder caught the
    stream from its 30th second (packets copied, as 录播姬 does)."""
    monkeypatch.setattr(bf, "WINDOW_S", 10.0)  # the synthetic fragments are only ~30 s long
    W = KL_LIVE  # truth: recorder-frame wall time of the replay's content 0
    first = synth.cuts[0][0]
    c0, dur = first * PKT, (synth.stream.n - first) * PKT
    (tmp_path / "rec" / CHANNEL).mkdir(parents=True)
    write_frames(synth.stream, list(range(first, synth.stream.n)), tmp_path / "rec.aac")
    flv = tmp_path / "rec" / CHANNEL / import_name(W + c0, "rec", ZoneInfo("Asia/Shanghai"))
    ff("-f", "aac", "-i", str(tmp_path / "rec.aac"), "-c", "copy", "-f", "flv", str(flv))
    os.utime(flv, (W + c0 + dur, W + c0 + dur))
    cfg = replace(config.load(), rec_dir=tmp_path / "rec", data_dir=tmp_path / "data")

    async def extract():
        ex = Extractor(cfg)
        ex.update(recordings.scan(cfg.rec_dir, cfg.filename_tz))
        (st,) = ex.files.values()
        ex.ensure(st)
        while st.status not in ("done", "error"):
            await asyncio.sleep(0.05)
        return ex

    ex = asyncio.run(extract())
    svc = BackfillService(cfg, ex, Store(cfg.data_dir / "fone.sqlite3"),
                          Settings(rooms={ROOM: CHANNEL}, import_dir=tmp_path / "import"), bili=FakeBili())
    windows = svc._recording_windows(CHANNEL, W - 60, W + 200)
    assert len(windows) == 8 and all(w0 >= W + c0 for _, w0 in windows)
    parts = [load_part(p, tmp_path / f"p{k}.aac") for k, p in enumerate(synth.audio)]
    jf = JoinFinder(parts, fragment_s=(25, 50))
    tl = Timeline(parts, jf.select(jf.candidates())[0])
    place, rows = bf.measure(tl, windows, tmp_path)
    assert place.measured == [1, 2], rows
    assert abs(place.offsets[1] - W) < 0.005 and abs(place.offsets[2] - W) < 0.005
    assert sum(r["ok"] for r in rows) >= 4


def test_jobs_and_periods_survive_a_restart(tmp_path):
    svc = make_service(tmp_path)
    end_period(svc)
    job = svc.submit(CHANNEL, (1.0, 2.0), bvid="BV1ePHq6DE8B")
    again = make_service(tmp_path)
    assert again.db.job(job.id).data["bvid"] == "BV1ePHq6DE8B"
    assert again.db.periods(ROOM)[0]["start"] == KL_LIVE


# ---- routes ---------------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    svc = make_service(tmp_path)
    app = FastAPI()
    app.include_router(make_router(svc))
    return TestClient(app), svc


def test_status_route(api):
    client, svc = api
    svc.record(status(True), CHANNEL, KL_LIVE + 60)
    st = client.get("/api/backfill/status").json()
    assert st["enabled"] and st["rooms"][0]["channel"] == CHANNEL and st["rooms"][0]["live"]
    assert st["periods"][0]["start"] == KL_LIVE and st["jobs"] == [] and st["current"] is None


def test_import_route(api):
    client, svc = api
    r = client.post("/api/backfill/import", json={"bvid": "BV1ePHq6DE8B"})
    assert r.status_code == 200 and r.json()["job"]["state"] == "waiting"
    jid = r.json()["job"]["id"]
    assert client.post("/api/backfill/import", json={"bvid": "BV1ePHq6DE8B"}).json()["job"]["id"] == jid
    r = client.post("/api/backfill/import", json={"from": KL_LIVE, "to": KL_LIVE + 600, "channel": CHANNEL})
    assert r.status_code == 200 and r.json()["job"]["window"] == [KL_LIVE, KL_LIVE + 600]
    assert r.json()["job"]["source"] == "manual"
    assert client.post("/api/backfill/import", json={"bvid": "nope"}).status_code == 400
    assert client.post("/api/backfill/import", json={"from": 1, "to": 2, "channel": "x"}).status_code == 400
    assert client.post("/api/backfill/import", json={"from": 5, "to": 2}).status_code == 400
    assert client.post("/api/backfill/import", json={}).status_code == 400
    assert len(client.get("/api/backfill/status").json()["jobs"]) == 2


def test_import_route_when_not_configured(tmp_path):
    svc = make_service(tmp_path)
    svc.settings = replace(svc.settings, import_dir=None)
    app = FastAPI()
    app.include_router(make_router(svc))
    r = TestClient(app).post("/api/backfill/import", json={"bvid": "BV1ePHq6DE8B"})
    assert r.status_code == 409
