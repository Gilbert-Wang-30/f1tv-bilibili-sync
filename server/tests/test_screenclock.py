"""Screen clock: estimator scenarios, FLV indexing, and real OCR on recordings.

Timebase in these tests: a frame's file wall time is fone's audio mapping applied to
its media time, wall = wallStart + (pts - a0) with a0 the first audio packet (audio
HLS m=0). The KL recording's wallStart is 1791010992.737 and a0 = 0.08 s; the
research measured delayScreen 38.997 s against exactly that mapping.
"""

import asyncio
import contextlib
import dataclasses
import json
import math
import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("numpy")  # in the image from v0.3 on (ads + screen clock)

from fone import config  # noqa: E402
from fone import screenclock as sc  # noqa: E402
from fone.extractor import FileState, Piece, Segment  # noqa: E402
from fone.recordings import Recording  # noqa: E402
from fone.screenclock_media import FlvIndex, Reading, Tick, decode, tesseract_available  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "screenclock"
# 45 s of the KL qualifying recording (pts 3990-4035, original timestamps kept): only
# MultiViewer's header strip, on black. Survives the recording's 3-week janitor.
HEADER_CLIP = FIXTURES / "mv_header_kl.flv"
KL_NAME = "fone/胡闹P房/20261003-150312-650-【胡闹P房】16点 巴林 排位赛.flv"
KL = next((p for p in (Path(os.environ.get("FONE_KL_FLV", "/nonexistent")), Path("/rec") / KL_NAME,
                       Path("/mnt/nas/media/library/recordings") / KL_NAME) if p.is_file()), None)
KL_WALL = 1791010992.737  # fone's wall clock of the KL file's audio m=0
KL_A0 = 0.08  # its first audio packet (FLV pts)
KL_DELAY = 38.997
BEIJING = 8 * 3600

needs_ocr = pytest.mark.skipif(not tesseract_available() or not shutil.which("ffmpeg"),
                               reason="needs tesseract + ffmpeg (apk add tesseract-ocr tesseract-ocr-data-eng)")
needs_kl = pytest.mark.skipif(KL is None, reason="KL recording not mounted (set FONE_KL_FLV or mount /rec)")


# ---- estimator on synthetic readings ------------------------------------------------

WALL0 = 1791010992.737


def tod(utc: float, tz: int = BEIJING) -> int:
    return int((utc + tz) // 1) % 86400


def make(spans, step=30.0, tz=BEIJING, ocr_err=0.0, seed=1):
    """Readings + ticks for spans of (from_s, to_s, kind, param): 'live' (param = delay),
    'replay' (param = feed UTC at from_s), 'frozen' (param = feed UTC shown), 'none'."""
    rnd = random.Random(seed)
    readings, ticks = [], []
    t = 0.08
    while t < spans[-1][1]:
        wall = WALL0 + t
        r = Reading(t, searched=True)
        for a, b, kind, p in spans:
            if a <= t < b:
                if kind == "live":
                    r.visible, r.tod = True, tod(wall - p, tz)
                    if rnd.random() < 0.3:  # the seconds digit changes when feed time is whole
                        n = math.ceil(wall - p)
                        ticks.append(Tick(n + p - WALL0 + rnd.uniform(-0.02, 0.02), tod(n, tz)))
                elif kind == "replay":
                    r.visible, r.tod = True, tod(p + (t - a), tz)
                elif kind == "frozen":
                    r.visible, r.tod = True, tod(p, tz)
                break
        if r.tod is not None and rnd.random() < ocr_err:
            r.tod = rnd.randrange(86400)  # OCR junk
        readings.append(r)
        t += step + rnd.uniform(-0.6, 0.6)  # keyframe phase jitter
    return readings, ticks


def est(spans, zones=(BEIJING,), latest=False, **kw):
    readings, ticks = make(spans, **kw)
    return sc.estimate(readings, ticks, lambda pts: WALL0 + pts, None if zones is None else list(zones), 40.0, 30.0,
                       latest=latest)


def test_live_clock_ticks_give_the_delay():
    e = est([(0, 7200, "live", 39.1)])
    assert e["status"] == "ok" and e["method"] == "tick" and e["confidence"] == "high"
    assert abs(e["delay"] - 39.1) < 0.03
    assert e["interval"][0] - 1e-6 <= 39.1 <= e["interval"][1] + 1e-6
    assert e["zone"] == BEIJING


def test_ocr_junk_is_outvoted():
    e = est([(0, 7200, "live", 39.1)], ocr_err=0.05)
    assert e["confidence"] == "high" and abs(e["delay"] - 39.1) < 0.03


def test_frozen_clock_after_the_session_is_ignored():
    e = est([(0, 4500, "live", 39.1), (4500, 9900, "frozen", WALL0 + 4500 - 39.1)])
    assert abs(e["delay"] - 39.1) < 0.03
    assert all(s["to"] < 4500 for s in e["segments"])


def test_frozen_clock_only():
    e = est([(0, 5400, "frozen", WALL0 - 3000)])
    assert e["status"] == "clock_not_running" and e["delay"] is None and e["confidence"] == "none"


def test_no_clock():
    e = est([(0, 3600, "none", 0)])
    assert e["status"] == "no_clock" and e["delay"] is None


def test_replay_of_another_session_rejected_by_zone():
    spans = [(0, 2700, "live", 39.1), (2700, 9000, "replay", WALL0 - 6 * 86400 + 1234.5)]
    e = est(spans)
    assert e["status"] == "ok" and abs(e["delay"] - 39.1) < 0.03 and e["confidence"] in ("high", "medium")
    # Zone-agnostic (mod 15 min) the replay looks like a second steady delay: the
    # prior breaks the tie, but it's flagged.
    e = est(spans, zones=None)
    assert e["status"] == "ambiguous" and e["confidence"] == "low" and abs(e["delay"] - 39.1) < 0.03


def test_circuit_zone_candidate():
    # Track time shown in circuit time (UTC+3) while the streamer is in UTC+8.
    e = est([(0, 3600, "live", 41.0)], zones=(BEIJING, 3 * 3600), tz=3 * 3600)
    assert e["status"] == "ok" and abs(e["delay"] - 41.0) < 0.03 and e["zone"] == 3 * 3600
    e = est([(0, 3600, "live", 41.0)], zones=(BEIJING,), tz=3 * 3600)
    assert e["status"] == "zone_mismatch" and e["delay"] is None and e["zone"] == 3 * 3600


def test_resync_mid_stream_latest_for_live_files():
    spans = [(0, 4300, "live", 39.1), (4300, 7200, "live", 34.6)]
    assert abs(est(spans)["delay"] - 39.1) < 0.03  # finished file: the dominant delay
    assert abs(est(spans, latest=True)["delay"] - 34.6) < 0.03  # growing file: the current one


def test_short_glimpse_of_the_clock_is_low_confidence():
    e = est([(0, 3000, "none", 0), (3000, 3180, "live", 39.1), (3180, 7200, "none", 0)])
    assert e["delay"] is not None and abs(e["delay"] - 39.1) < 0.6 and e["confidence"] in ("low", "medium")


def test_delay_of_zones():
    shown = tod(WALL0 - 39.1)  # Track time on screen in UTC+8, floor of the feed time
    assert 39.1 <= sc.delay_of(WALL0 - shown, [BEIJING], 40) < 40.1
    assert 39.1 <= sc.delay_of(WALL0 - shown, [-5 * 3600, BEIJING], 40) < 40.1
    assert 39.1 <= sc.delay_of(WALL0 - shown, None, 40) < 40.1  # modulo 15 min
    assert abs(sc.delay_of(WALL0 - shown, [0, 3 * 3600], 40) - 39.1) > 3000  # no candidate fits


def test_offsets_parse_and_format():
    assert sc.parse_offset("+08:00") == 28800 and sc.parse_offset("08:00:00") == 28800
    assert sc.parse_offset("-04:30") == -16200 and sc.parse_offset("-0330") == -12600
    assert sc.format_offset(28800) == "+08:00" and sc.format_offset(-16200) == "-04:30"
    with pytest.raises(ValueError):
        sc.parse_offset("8")


def test_wall_mapper_follows_pieces():
    w = sc.wall_mapper([Piece(1000.0, 0.0, 100.0), Piece(1110.0, 100.0, 50.0)])  # 10 s stall at m=100
    assert w(0) == 1000.0 and w(99.5) == 1099.5 and w(100) == 1110.0 and w(160) == 1170.0
    assert sc.wall_mapper([]) is None


# ---- FLV index ---------------------------------------------------------------------


def make_flv(path: Path, seconds: float, color="black", size="320x180") -> Path:
    subprocess.run([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=25:d={seconds}",
        "-c:a", "aac", "-b:a", "64k", "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
        "-pix_fmt", "yuv420p", "-shortest", "-f", "flv", str(path),
    ], check=True)
    return path


def first_audio_pts(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "packet=pts_time",
                          "-read_intervals", "%+#1", "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                         check=True)
    return float(out.stdout.split()[0])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_index_of_a_growing_file_only_takes_complete_tags(tmp_path):
    src = make_flv(tmp_path / "src.flv", 12)
    full = FlvIndex(src)
    full.update()
    assert [k - full.kf_ms[0] for k in full.kf_ms] == [0, 2000, 4000, 6000, 8000, 10000]
    assert abs(full.a0 - first_audio_pts(src)) < 1e-6

    data = src.read_bytes()
    grow = tmp_path / "grow.flv"
    idx = FlvIndex(grow)
    seen = []
    with open(grow, "wb") as f:
        for i in range(0, len(data), 7919):  # chunks never aligned with tags
            f.write(data[i:i + 7919])
            f.flush()
            idx.update()
            assert idx.kf_ms == full.kf_ms[:len(idx.kf_ms)]
            seen.append(len(idx.kf_ms))
    assert seen[-1] == 6 and seen[0] < 6 and idx.pos == full.pos and idx.a0 == full.a0
    assert idx.pick(None, 3.9) == [0, 2, 4] and idx.pick(4.0, 3.9) == [4]

    frames = list(decode(full.keyframe_stream([1, 3, 5])))
    assert [round(f.pts) for f in frames] == [2, 6, 10] and frames[0].img.shape == (180, 320)
    window = list(decode(full.window_stream(2, 1.0), vf="crop=32:16:8:8"))
    assert 24 <= len(window) <= 27 and window[0].img.shape == (16, 32)

    grow.write_bytes(data[:5000])  # replaced by a shorter file: start over
    idx.update()
    assert idx.kf_ms == full.kf_ms[:1] or idx.kf_ms == []


# ---- real OCR --------------------------------------------------------------------------


class FakeExtractor:
    """What the service needs from Extractor: files, is_growing, pieces."""

    def __init__(self):
        self.files: dict[str, FileState] = {}
        self.growing: set[str] = set()
        self.wall: dict[str, float] = {}

    def add(self, path: Path, data_dir: Path, wall0: float, status="done") -> FileState:
        fid = f"f{len(self.files)}"
        rec = Recording(id=fid, relpath=f"c/{path.name}", path=path, channel="c", name=path.name, title="",
                        size=path.stat().st_size, mtime=path.stat().st_mtime, name_time=None, name_precision=None)
        st = FileState(rec=rec, out_dir=data_dir / "hls" / fid, status=status)
        st.out_dir.mkdir(parents=True, exist_ok=True)
        self.files[fid] = st
        self.wall[fid] = wall0
        return st

    def is_growing(self, rec, now=None):
        return rec.id in self.growing

    def pieces(self, st):
        return [Piece(self.wall[st.rec.id], 0.0, 1e6)], [Segment("seg_00000.ts", 0.0, 60.0)], False


async def beijing_only(lo, hi):
    return [BEIJING]  # the KL circuit's zone (Kuala Lumpur, UTC+8)


def service(tmp_path, ex, **kw):
    cfg = dataclasses.replace(config.load(), data_dir=tmp_path)
    return sc.ScreenClockService(cfg, ex, zones=beijing_only, settings=sc.Settings(jobs=8, **kw))


def refresh(st: FileState, status: str) -> None:
    p = st.rec.path
    st.rec = Recording(**{**st.rec.__dict__, "size": p.stat().st_size, "mtime": p.stat().st_mtime})
    st.status = status


@needs_ocr
def test_header_clip_measures_the_delay():
    job = sc.ClockJob(HEADER_CLIP, sc.Settings(jobs=8, step=5))
    job.index.update()
    wall0 = KL_WALL + (job.index.a0 - KL_A0)  # this clip's audio m=0 on the KL wall clock
    e = job.run(lambda m: wall0 + m, [BEIJING], 40.0, False)
    assert e["status"] == "ok" and e["method"] == "tick", e
    assert abs(e["delay"] - KL_DELAY) < 0.3, e
    assert e["zone"] == BEIJING and e["n"] >= 8


@needs_ocr
def test_black_video_has_no_clock(tmp_path):
    ex = FakeExtractor()
    st = ex.add(make_flv(tmp_path / "black.flv", 90, size="1920x1080"), tmp_path, 1.8e9)
    svc = service(tmp_path, ex)
    res = asyncio.run(svc.analyse(st))
    assert res["status"] == "no_clock" and res["delayScreen"] is None and res["confidence"] == "none"
    assert res["final"] and res["n"] == 0 and res["readings"]["readings"] == 3
    saved = json.loads((st.out_dir / "screenclock.json").read_text())
    assert saved["status"] == "no_clock" and "obs" in saved
    assert svc._due(time.time()) == []  # finished: never analysed again


@needs_ocr
def test_service_loop_and_route(tmp_path):
    ex = FakeExtractor()
    st = ex.add(make_flv(tmp_path / "black.flv", 30, size="1920x1080"), tmp_path, 1.8e9)
    idle = ex.add(make_flv(tmp_path / "idle.flv", 2), tmp_path, 1.8e9, status="queued")
    svc = service(tmp_path, ex, poll=0.05)
    app = FastAPI()
    app.include_router(sc.make_router(svc))

    async def run():
        await svc.start()
        for _ in range(300):
            if st.rec.id in svc.results:
                break
            await asyncio.sleep(0.1)
        await svc.stop()

    asyncio.run(run())
    with TestClient(app) as client:
        r = client.get(f"/api/screenclock/{st.rec.id}")
        assert r.status_code == 200
        body = r.json()
        assert {"delayScreen", "n", "confidence", "updated", "basis"} <= body.keys() and "obs" not in body
        assert body["status"] == "no_clock"
        assert client.get(f"/api/screenclock/{idle.rec.id}").json()["status"] == "pending"
        assert client.get("/api/screenclock/nope").status_code == 404


@needs_ocr
def test_never_creates_the_extractors_output_dir(tmp_path):
    ex = FakeExtractor()
    st = ex.add(make_flv(tmp_path / "black.flv", 20, size="1920x1080"), tmp_path, 1.8e9)
    shutil.rmtree(st.out_dir)  # extractor is about to (re)create it without exist_ok
    svc = service(tmp_path, ex)
    res = asyncio.run(svc.analyse(st))
    assert not st.out_dir.exists()
    assert svc.result(st.rec.id)["status"] == res["status"] == "no_clock"


@needs_ocr
@needs_kl
def test_kl_recording(tmp_path):
    """The whole 2.9 h KL file, as the service sees a finished recording."""
    ex = FakeExtractor()
    st = ex.add(KL, tmp_path, KL_WALL)
    svc = service(tmp_path, ex)
    t = time.time()
    res = asyncio.run(svc.analyse(st))
    took = time.time() - t
    assert res["status"] == "ok" and res["method"] == "tick" and res["confidence"] == "high", res
    assert abs(res["delayScreen"] - KL_DELAY) < 0.3, res["delayScreen"]
    assert res["displayOffset"] == "+08:00" and res["final"] and res["n"] >= 50 and res["ticks"] >= 10
    # The clock runs during the session only (16:00 local, m ~ 3000-7600): the frozen
    # clock before and after it is not evidence.
    assert 2900 < res["segments"][0]["from"] < 3300 and 7300 < res["segments"][-1]["to"] < 7700
    assert took < 120, took

    # Restart: the persisted result is served and the file isn't analysed again.
    again = service(tmp_path, ex)
    assert again.result(st.rec.id)["delayScreen"] == res["delayScreen"]
    assert again._due(time.time()) == []


def tags(path: Path):
    """(pos, type, ts_ms, end, b0, b1) of every complete FLV tag."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        h = f.read(13)
        pos = int.from_bytes(h[5:9], "big") + 4
        while pos + 13 <= size:
            f.seek(pos)
            th = f.read(13)
            end = pos + 15 + int.from_bytes(th[1:4], "big")
            if end > size:
                return
            yield pos, th[0] & 0x1F, int.from_bytes(th[4:7], "big") | th[7] << 24, end, th[11], th[12]
            pos = end


def head_end(path: Path) -> int:
    """End of the FLV header + metadata + codec sequence headers (first media frame)."""
    for pos, typ, _, _, _, b1 in tags(path):
        if typ in (8, 9) and b1 != 0:
            return pos
    raise AssertionError("no media")


def cut(path: Path, t0: float, t1: float) -> bytes:
    """Recorder-style FLV bytes: the file's head, then whole tags from the first keyframe
    at/after t0 up to the first keyframe at/after t1 (original timestamps)."""
    head = head_end(path)
    start = end = None
    for pos, typ, ts, _, b0, b1 in tags(path):
        if typ == 9 and b0 >> 4 == 1 and b1 == 1:
            if start is None and ts >= t0 * 1000:
                start = pos
            elif start is not None and ts >= t1 * 1000:
                end = pos
                break
    with open(path, "rb") as f:
        data = f.read(head)
        f.seek(start)
        return data + f.read(end - start)


@needs_ocr
@pytest.mark.parametrize("source", ["header_clip", "kl"])
def test_rolling_on_a_growing_file(tmp_path, monkeypatch, source):
    if source == "kl":
        if KL is None:
            pytest.skip("KL recording not mounted")
        data, step = cut(KL, 3990, 4590), 30.0  # 10 min of the session, ~95 MB
    else:
        data, step = HEADER_CLIP.read_bytes(), 5.0
    decoded = []
    real_decode = sc.decode

    def counting(feed, vf=""):
        for fr in real_decode(feed, vf):
            if not vf:  # sampled keyframes (tick windows are cropped)
                decoded.append(fr.pts)
            yield fr

    monkeypatch.setattr(sc, "decode", counting)
    path = tmp_path / "growing.flv"
    path.write_bytes(b"")
    ex = FakeExtractor()
    st = ex.add(path, tmp_path, 0.0, status="running")
    ex.growing.add(st.rec.id)
    svc = service(tmp_path, ex, step=step)
    cuts = [len(data) // 3 + 12345, 2 * len(data) // 3 - 777, len(data)]  # not on tag boundaries
    results, prefix = [], []
    written = 0
    for n, upto in enumerate(cuts):
        with open(path, "ab") as f:
            f.write(data[written:upto])
        written = upto
        if n:
            svc.jobs[st.rec.id].ran_at -= svc.s.interval  # the rolling interval has passed
        if n == len(cuts) - 1:
            ex.growing.discard(st.rec.id)
            refresh(st, "done")
        else:
            refresh(st, "running")
        if n == 0:  # wall mapping of this file's m=0 on the KL clock, now that a0 is on disk
            probe = FlvIndex(path)
            probe.update()
            ex.wall[st.rec.id] = KL_WALL + (probe.a0 - KL_A0)
        assert svc._due(time.time()) == [st]
        res = asyncio.run(svc.analyse(st))
        results.append(res)
        job_readings = [r.pts for r in svc.jobs[st.rec.id].readings] if st.rec.id in svc.jobs else None
        if job_readings is not None:
            assert job_readings[:len(prefix)] == prefix  # earlier readings kept, not redone
            prefix = job_readings
        if n < len(cuts) - 1:
            assert res["growing"] and not res["final"]
            assert svc._due(time.time()) == []  # next rolling pass only after the interval
        if res["delayScreen"] is not None:
            assert abs(res["delayScreen"] - KL_DELAY) < 0.3, (n, res)

    assert len(decoded) == len(set(decoded)), "a keyframe was decoded twice"
    to = [r["analyzedTo"] for r in results]
    assert to[0] < to[1] < to[2]
    final = results[-1]
    assert final["final"] and final["status"] == "ok" and final["method"] == "tick", final
    assert abs(final["delayScreen"] - KL_DELAY) < 0.3
    assert final["readings"]["readings"] == len(decoded)


# ---- zones and scheduling ----------------------------------------------------------------


def test_openf1_zones_from_the_schedule():
    import httpx

    calls = []
    sessions = json.loads((Path(__file__).parent / "fixtures" / "openf1_sessions_1308.json").read_text())
    sessions.append({**sessions[0], "date_start": "2026-10-05T04:00:00+00:00", "date_end": "2026-10-05T05:00:00+00:00",
                     "gmt_offset": "-05:00:00", "is_cancelled": True})

    def handler(request):
        calls.append(request.url.params["year"])
        return httpx.Response(200 if calls[-1] == "2026" else 500, json=sessions)  # other years: down

    async def run():
        z = sc.OpenF1Zones()
        z.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        kl = await z(KL_WALL, KL_WALL + 10378)  # the KL quali recording
        month_before = await z(KL_WALL - 30 * 86400, KL_WALL - 30 * 86400 + 7200)
        cancelled = await z(1791176400.0, 1791180000.0)  # 2026-10-05 04:00-05:00Z
        down = await z(1.73e9, 1.73e9 + 3600)  # 2024: lookup fails
        again = await z(1.73e9, 1.73e9 + 3600)  # failure remembered, no new request
        await z.close()
        return kl, month_before, cancelled, down, again

    kl, month_before, cancelled, down, again = asyncio.run(run())
    assert kl == [BEIJING] and month_before == [] and cancelled == []
    assert down is None and again is None and calls.count("2024") == 1 and calls.count("2026") == 1


def test_whole_file_extraction_in_progress_is_left_alone(tmp_path):
    ex = FakeExtractor()
    path = tmp_path / "x.flv"
    path.write_bytes(b"FLV")
    st = ex.add(path, tmp_path, 1.8e9, status="running")  # extractor reading a finished file
    svc = service(tmp_path, ex)
    assert svc._due(time.time()) == []
    st.follow = True  # tailing a live recording: rolling analysis
    assert svc._due(time.time()) == [st]


def test_waiting_for_the_audio_mapping_never_blocks_the_event_loop(tmp_path):
    """A live recording's first seconds: ffmpeg hasn't written a segment, so there is no
    wall mapping yet. The loop must retry next poll, not spin (it used to: analyse()
    returned without yielding and the file was due again at once, freezing the server)."""
    class Waiting(FakeExtractor):
        def pieces(self, st):
            return [], [], True

    ex = Waiting()
    path = tmp_path / "live.flv"
    path.write_bytes(b"FLV")
    st = ex.add(path, tmp_path, 1.8e9, status="running")
    st.follow = True
    ex.growing.add(st.rec.id)
    svc = service(tmp_path, ex, poll=0.05)
    # A spinning loop runs nothing else, this test included: take the file away after
    # 2 s from another thread so a regression fails here instead of hanging.
    threading.Timer(2.0, ex.files.clear).start()
    beats = []

    async def run():
        task = asyncio.create_task(svc._loop())
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            beats.append(time.monotonic())
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(beats) > 30, f"event loop starved: {len(beats)} heartbeats in 1 s"
    assert svc.results[st.rec.id]["status"] == "waiting"


@needs_ocr
def test_no_f1_session_means_no_ocr(tmp_path, monkeypatch):
    def no_decoding(*a, **k):
        raise AssertionError("decoded video")

    async def no_session(lo, hi):
        return []

    monkeypatch.setattr(sc, "decode", no_decoding)
    ex = FakeExtractor()
    st = ex.add(HEADER_CLIP, tmp_path, 1.8e9)
    svc = service(tmp_path, ex)
    svc.zones = no_session
    res = asyncio.run(svc.analyse(st))
    assert res["status"] == "no_session" and res["delayScreen"] is None and res["final"]
    assert svc._due(time.time()) == []


@needs_ocr
def test_zone_lookup_down_then_back(tmp_path, monkeypatch):
    """Without zone info the clock is read modulo 15 min (capped confidence); once the
    lookup works the result is re-estimated from the saved readings, without decoding."""
    async def down(lo, hi):
        return None

    ex = FakeExtractor()
    st = ex.add(HEADER_CLIP, tmp_path, 0.0)
    probe = FlvIndex(HEADER_CLIP)
    probe.update()
    ex.wall[st.rec.id] = KL_WALL + (probe.a0 - KL_A0)
    svc = service(tmp_path, ex, step=5)
    svc.zones = down
    res = asyncio.run(svc.analyse(st))
    assert res["zonesChecked"] is False and res["zones"] is None and "zone unchecked" in res["basis"]
    assert res["status"] == "ok" and abs(res["delayScreen"] - KL_DELAY) < 0.3
    assert svc._due(time.time()) == []
    assert svc._due(time.time() + 1801) == [st]

    def no_decoding(*a, **k):
        raise AssertionError("decoded video")

    monkeypatch.setattr(sc, "decode", no_decoding)
    restarted = service(tmp_path, ex, step=5)  # also from disk, as after a restart
    res2 = asyncio.run(restarted.analyse(st))
    assert res2["zonesChecked"] is True and res2["zones"] == ["+08:00"] and res2["displayOffset"] == "+08:00"
    assert res2["delayScreen"] == res["delayScreen"] and res2["status"] == "ok"
    assert restarted._due(time.time() + 7200) == []
