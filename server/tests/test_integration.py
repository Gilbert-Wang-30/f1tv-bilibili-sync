"""The integrated server: imports only fill what the recorder missed, ad mutes on the
timeline, /api/sync's measured screen delay, every service's routes mounted (and each
switchable off). Real ffmpeg extraction and real ad detection on synthetic recordings."""

import importlib
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fone.extractor import Piece
from fone.timeline import MIN_CLIP_SECONDS, MIN_FILL_SECONDS, mutes, uncovered, union
from test_ads import synth_refs, synth_refs_dir, write_wav  # noqa: F401 (fixtures: the reel references are synthetic)
from test_ads_detect import B, C, clip, talk

SH = ZoneInfo("Asia/Shanghai")
CHANNEL = "胡闹P房"
SR8 = 8000


def name_ms(t: float, title: str = "【胡闹P房】16点 巴林 正赛") -> str:
    """Recorder-style millisecond name (also what backfill writes)."""
    dt = datetime.fromtimestamp(t, SH)
    return dt.strftime("%Y%m%d-%H%M%S-") + f"{dt.microsecond // 1000:03d}-{title}.flv"


def make_flv(path: Path, seconds: float | None = None, wav: Path | None = None) -> None:
    """Tiny video + AAC audio (a tone, or the given WAV), like a recording."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tone = f"sine=frequency=440:sample_rate=48000:duration={seconds}"
    audio = ["-i", str(wav)] if wav else ["-f", "lavfi", "-i", tone]
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *audio,
                    "-f", "lavfi", "-i", "color=c=black:s=64x36:r=10",
                    "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
                    "-c:v", "libx264", "-g", "20", "-shortest", "-f", "flv", str(tmp)], check=True)
    os.replace(tmp, path)


def place(path: Path, wall_start: float, duration: float) -> None:
    """A finished file's wall start is its last write minus its duration."""
    os.utime(path, (wall_start + duration, wall_start + duration))


def flv_with_audio(path: Path, x: np.ndarray, wall_start: float) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    wav = path.with_suffix(".wav")
    write_wav(wav, x)
    make_flv(path, wav=wav)
    wav.unlink()
    dur = len(x) / SR8
    place(path, wall_start, dur)
    return dur


@pytest.fixture
def boot(tmp_path, monkeypatch):
    """boot(**env) -> (fone.app module, rec channel dir, import channel dir). Services that
    would call out (OpenF1, F1 live timing, Bilibili, OCR) are not started unless asked."""

    def make(nested: bool = True, start: tuple[str, ...] = ("ads", "video", "backfill"), **env):
        rec, data = tmp_path / "rec", tmp_path / "data"
        imp = rec / "fone-import" if nested else tmp_path / "import"
        (rec / CHANNEL).mkdir(parents=True, exist_ok=True)
        for k, v in {"FONE_REC_DIR": rec, "FONE_DATA_DIR": data, "FONE_IMPORT_DIR": imp,
                     "FONE_SCAN_INTERVAL": "0.3", "FONE_GROWING_IDLE_SECONDS": "4", **env}.items():
            monkeypatch.setenv(k, str(v))
        import fone.app

        mod = importlib.reload(fone.app)

        async def idle():
            return None

        for name, svc in mod.SERVICES.items():
            if name not in start:
                monkeypatch.setattr(svc, "start", idle)
        return mod, rec / CHANNEL, imp / CHANNEL

    return make


def wait_for(fn, timeout=60, every=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (v := fn()) is not None:
            return v
        time.sleep(every)
    raise AssertionError("timed out")


def timeline(client, lo, hi):
    r = client.get("/api/timeline", params={"channel": CHANNEL, "from": lo, "to": hi})
    assert r.status_code == 200, r.text
    return r.json()


def all_done(client, lo, hi, n, ads_done=False):
    def ready():
        tl = timeline(client, lo, hi)
        files = tl["files"]
        if len(files) != n or any(f["status"] != "done" for f in files):
            return None
        if ads_done and any(f["inPlaylist"] and (f["adsAnalyzedTo"] or 0) < f["duration"] - 0.01 for f in files):
            return None
        return tl
    return wait_for(ready)


def assert_no_overlap(pieces):
    assert [p["w"] for p in pieces] == sorted(p["w"] for p in pieces)
    for a, b in zip(pieces, pieces[1:]):
        assert a["w"] + a["d"] <= b["w"] + 1e-6, (a, b)


# ---- pure helpers ------------------------------------------------------------------


def test_uncovered_clips_and_keeps_untouched_pieces():
    p = Piece(wall=100.0, media=10.0, duration=100.0)
    assert uncovered(p, []) == [p]
    assert uncovered(p, [(0, 50), (250, 300)]) == [p]  # no overlap: the very same piece
    got = uncovered(p, [(90, 120), (150, 160), (199, 260)])
    assert got == [Piece(120.0, 30.0, 30.0), Piece(160.0, 70.0, 39.0)]
    assert uncovered(p, [(0, 300)]) == []
    # an import's sliver left by a little misalignment is not worth two seeks; a recording's is kept
    assert uncovered(p, [(101, 200)]) == [] and MIN_FILL_SECONDS > 1
    assert uncovered(p, [(101, 200)], min_len=MIN_CLIP_SECONDS) == [Piece(100.0, 10.0, 1.0)]
    assert union([(5, 6), (0, 2), (1, 3), (3, 4)]) == [(0, 4), (5, 6)]


def test_mutes_merge_a_reel_and_follow_the_played_pieces():
    spans = [{"start": 30.0, "end": 47.0, "name": "A"}, {"start": 48.4, "end": 85.0, "name": "B"},
             {"start": 85.5, "end": 103.6, "name": "C"}, {"start": 500.0, "end": 547.0, "name": "A"}]
    played = [Piece(1000.0, 0.0, 60.0), Piece(1080.0, 70.0, 460.0)]  # media 60-70 played by another file
    got = mutes(spans, played, offset=1000.0)
    assert got == [
        {"w": 1030.0, "d": 30.0, "m": 1030.0, "name": "A+B+C"},
        {"w": 1080.0, "d": pytest.approx(33.6), "m": 1070.0, "name": "A+B+C"},
        {"w": 1510.0, "d": 30.0, "m": 1500.0, "name": "A"},  # cut where the played piece ends
    ]
    assert mutes(spans, [], 0.0) == []


# ---- timeline -----------------------------------------------------------------------


@pytest.mark.parametrize("nested", [True, False], ids=["import-dir-under-rec", "import-dir-elsewhere"])
def test_imports_only_fill_what_the_recorder_missed(boot, nested):
    app, chan, imp = boot(nested=nested)
    t = time.time() - 3 * 3600
    a, b, i, j = (chan / name_ms(t + 60, "A"), chan / name_ms(t + 150, "B"), imp / name_ms(t + 30, "replay"),
                  imp / name_ms(t + 65, "covered"))
    for path, start, dur in ((a, t + 60, 30), (b, t + 150, 30), (i, t + 30, 180), (j, t + 65, 20)):
        make_flv(path, dur)
        place(path, start, dur)
    with TestClient(app.app) as client:
        tl = all_done(client, t - 100, t + 400, 4)
        playlist = client.get(tl["playlist"]).text
    files = {f["name"]: f for f in tl["files"]}
    fa, fb, fi, fj = (files[p.name] for p in (a, b, i, j))
    assert [fa["source"], fb["source"], fi["source"], fj["source"]] == ["recorder", "recorder", "import", "import"]
    assert not fj["inPlaylist"] and fj["offset"] is None  # entirely covered by A
    assert tl["playlist"] == f"/api/playlist.m3u8?files={fi['id']},{fa['id']},{fb['id']}"
    assert fi["offset"] == 0 and abs(fa["offset"] - fi["duration"]) < 1e-6
    assert abs(fb["offset"] - fi["duration"] - fa["duration"]) < 1e-6

    pieces = tl["pieces"]
    assert_no_overlap(pieces)
    idx = {f["name"]: n for n, f in enumerate(tl["files"])}
    assert [p["f"] for p in pieces] == [idx[i.name], idx[a.name], idx[i.name], idx[b.name], idx[i.name]]
    # recordings untouched; the import plays only the holes, at the right media time
    by_file = lambda f: [p for p in pieces if p["f"] == idx[f["name"]]]  # noqa: E731
    assert len(by_file(fa)) == 1 and by_file(fa)[0]["m"] == fa["offset"]
    for p in by_file(fi):
        assert abs((p["m"] - fi["offset"]) - (p["w"] - fi["wallStart"])) < 1e-6
    holes = [(round(p["w"] - t), round(p["w"] + p["d"] - t)) for p in by_file(fi)]
    assert holes == [(30, 60), (90, 150), (180, 210)], holes
    # together: one unbroken run of commentary
    for x, y in zip(pieces, pieces[1:]):
        assert abs(x["w"] + x["d"] - y["w"]) < 0.1, (x, y)
    assert playlist.count("#EXT-X-DISCONTINUITY") == 2 and playlist.rstrip().endswith("#EXT-X-ENDLIST")
    assert tl["mutes"] == [] and tl["complete"]


def test_mutes_come_from_ads_in_what_is_played(boot, synth_refs):
    app, chan, imp = boot()
    t = time.time() - 3 * 3600
    gap = np.zeros(SR8, np.float32)  # 1 s of gated mic between two clips of the reel
    rec_audio = np.concatenate([talk(10, seed=1), clip(C), gap, clip(B), talk(10, seed=2)])
    pre = np.concatenate([talk(5, seed=3), clip(C), talk(20, seed=4)])
    pre_s = len(pre) / SR8
    r = chan / name_ms(t, "live")
    i = imp / name_ms(t - pre_s, "replay")
    rec_dur = flv_with_audio(r, rec_audio, t)
    # the replay: an earlier stretch the recorder missed, then the same audio as the recording
    flv_with_audio(i, np.concatenate([pre, rec_audio]), t - pre_s)
    with TestClient(app.app) as client:
        tl = all_done(client, t - 600, t + 600, 2, ads_done=True)
    files = {f["source"]: f for f in tl["files"]}
    fr, fi = files["recorder"], files["import"]
    assert_no_overlap(tl["pieces"])
    c_len, b_len = len(clip(C)) / SR8, len(clip(B)) / SR8
    want = [  # (wall, duration, playlist m, name)
        (fi["wallStart"] + 5, c_len, fi["offset"] + 5, C),
        (fr["wallStart"] + 10, c_len + 1 + b_len, fr["offset"] + 10, f"{C}+{B}"),
    ]
    got = tl["mutes"]
    assert len(got) == 2, got  # the replay's own copy of the reel lies under the recording: no mute
    for m, (w, d, pm, name) in zip(got, want):
        assert m["name"] == name
        assert abs(m["w"] - w) < 0.3 and abs(m["m"] - pm) < 0.3 and abs(m["d"] - d) < 0.5, (m, w, d, pm)
    assert abs(fr["duration"] - rec_dur) < 0.2


# ---- sync -----------------------------------------------------------------------------


def test_sync_priority_manual_measured_channel_default(boot):
    app, chan, _ = boot(FONE_REACTION_SECONDS="0.9")
    t = time.time() - 3 * 3600
    r = chan / name_ms(t, "quali")
    make_flv(r, 60)
    place(r, t, 60)
    q = {"channel": CHANNEL, "clock": "multiviewer"}
    with TestClient(app.app) as client:
        fid = all_done(client, t - 600, t + 600, 1)["files"][0]["id"]
        sc = app.screenclock
        # session (scheduled) 40 s before its commentary, which this recording holds
        app.store.put_content("111", {"contentId": "111", "scheduledStart": t - 35, "scheduledEnd": t + 10})
        app.store.put_content("222", {"contentId": "222", "scheduledStart": t - 86400, "scheduledEnd": t - 80000})
        app.store.put_content("333", {"contentId": "333", "p0": t - 50, "duration": 30})

        assert client.get("/api/sync/111", params=q).json() == {"delay": 40.0, "source": "default"}
        sc.results[fid] = {"delayScreen": 38.997, "n": 154, "confidence": "high", "updated": t, "basis": "test"}
        got = client.get("/api/sync/111", params=q).json()
        assert got == {"delay": 39.897, "source": "measured", "screen": 38.997, "reaction": 0.9,
                       "confidence": "high", "file": fid}
        assert client.get("/api/sync/333", params=q).json()["source"] == "measured"  # replay span (p0)
        assert client.get("/api/sync/333", params={**q, "clock": "preroll"}).json()["source"] == "measured"
        assert client.get("/api/sync/222", params=q).json()["source"] == "default"  # not covered
        # Measured against true UTC: no use to F1 TV's player clock, which reads ~45 s late.
        assert client.get("/api/sync/111", params={**q, "clock": "epoch"}).json() == {"delay": 40.0,
                                                                                      "source": "default"}

        client.put("/api/sync/999", json={**q, "delay": 41.5})  # a replay calibration -> replays start from it
        assert client.get("/api/sync/222", params=q).json() == {"delay": 41.5, "source": "channel"}
        assert client.get("/api/sync/111", params=q).json()["source"] == "measured"  # beats the channel
        sc.results[fid] = {**sc.results[fid], "confidence": "low"}
        assert client.get("/api/sync/111", params=q).json() == {"delay": 41.5, "source": "channel"}
        sc.results[fid] = {**sc.results[fid], "confidence": "medium", "zonesChecked": False}  # OpenF1 was down
        assert client.get("/api/sync/111", params=q).json() == {"delay": 41.5, "source": "channel"}
        sc.results[fid] = {**sc.results[fid], "zonesChecked": True}
        assert client.get("/api/sync/111", params=q).json()["source"] == "measured"
        sc.results[fid] = {**sc.results[fid], "confidence": "medium"}
        client.put("/api/sync/111", json={**q, "delay": 42.0})  # tuned by ear for this video
        assert client.get("/api/sync/111", params=q).json() == {"delay": 42.0, "source": "content"}


# ---- wiring ---------------------------------------------------------------------------


def test_every_service_is_mounted_and_each_can_be_switched_off(boot):
    app, _, imp = boot(start=(), FONE_ROOMS="23954427=胡闹P房")
    with TestClient(app.app) as client:
        assert client.get("/api/health").json()["services"] == ["ads", "backfill", "danmaku", "screenclock", "timing", "video"]
        assert client.get("/api/ads/nope").json() == {"detail": "unknown file"}
        assert client.get("/api/screenclock/nope").json() == {"detail": "unknown file nope"}
        assert client.get("/api/video/nope/info").status_code == 404
        assert client.get("/api/timing/session/0/state", params={"t": 1}).status_code == 422
        assert client.get("/api/timing/status").status_code == 200
        bf = client.get("/api/backfill/status").json()
        assert bf["enabled"] and bf["importDir"] == str(imp.parent)
        assert [r["room"] for r in bf["rooms"]] == [23954427] and bf["rooms"][0]["channel"] == CHANNEL
        assert client.post("/api/backfill/import", json={"bvid": "nope"}).status_code == 400
        assert client.get("/api/danmaku/status").json()["rooms"]["23954427"]["channel"] == CHANNEL
        assert client.get("/api/danmaku", params={"channel": CHANNEL, "from": 0, "to": 1}).json()["items"] == []

    app, _, _ = boot(start=(), FONE_VIDEO="0", FONE_ADS="off", FONE_BACKFILL="false", FONE_DANMAKU="0")
    with TestClient(app.app) as client:
        assert client.get("/api/health").json()["services"] == ["screenclock", "timing"]
        assert client.get("/api/ads/nope").json() == {"detail": "Not Found"}
        assert client.get("/api/video/nope/info").json() == {"detail": "Not Found"}
        assert client.get("/api/backfill/status").status_code == 404
        assert client.get("/api/danmaku/status").status_code == 404
        tl = timeline(client, 0, 1)
        assert tl["mutes"] == [] and tl["files"] == []


def test_a_broken_service_does_not_take_the_server_down(boot):
    app, _, _ = boot(start=("ads",), FONE_SCREENCLOCK_TZ="not-a-zone")  # can't even be built

    async def boom():
        raise RuntimeError("no refs")

    app.ads.start = boom  # fails to start
    with TestClient(app.app) as client:
        health = client.get("/api/health").json()
        assert health["ok"] and "screenclock" not in health["services"]
        assert client.get("/api/screenclock/nope").json() == {"detail": "Not Found"}
        assert timeline(client, 0, 1)["files"] == []


# ---- race day -------------------------------------------------------------------------


def test_live_recording_with_every_local_service_running(boot, tmp_path):
    """A recording still being written while ads, screen clock and video all work on it
    (only the services that call out stay off). The commentary path must stay live and
    responsive throughout: the screen clock used to spin on a live file's first seconds
    (no audio mapping yet) and freeze the whole server."""
    import dataclasses
    import faulthandler

    from test_api import make_flv as make_realtime_flv

    app, chan, _ = boot(start=("ads", "video", "screenclock"))

    async def beijing(lo, hi):  # no OpenF1 from tests
        return [8 * 3600]

    sc = app.screenclock
    sc.zones, sc._own_zones = beijing, False
    sc.s = dataclasses.replace(sc.s, poll=0.2)  # look at the file during its first seconds
    started = time.time()
    path = chan / name_ms(started, "live")
    # A frozen event loop would hang this test (and the TestClient's shutdown) for good:
    # end the run instead, leaving the stacks behind (pytest's capture swallows stderr).
    frozen = open(tmp_path / "frozen-server-stacks.txt", "w")
    faulthandler.dump_traceback_later(150, exit=True, file=frozen)
    try:
        with TestClient(app.app) as client:
            writer = make_realtime_flv(path, 16, realtime=True)
            try:
                tl = wait_for(lambda: (t := timeline(client, started - 600, started + 600))["live"]
                              and not t["files"][0]["provisional"] and t or None, timeout=30)
                fid = tl["files"][0]["id"]
                assert not tl["complete"] and tl["files"][0]["status"] == "running"
                time.sleep(3)
                tl2 = timeline(client, started - 600, started + 600)
                assert 1.0 < tl2["edge"] - tl["edge"] < 6.0  # still tailing in real time
                assert tl2["files"][0]["adsAnalyzedTo"] is not None
                assert client.get(f"/api/video/{fid}/info").status_code in (200, 202)
                assert client.get(f"/api/screenclock/{fid}").json()["status"] in ("waiting", "pending", "no_clock",
                                                                               "no_video")
            finally:
                writer.wait(timeout=30)
            done = wait_for(lambda: (t := timeline(client, started - 600, started + 600))["files"][0]["status"] == "done"
                            and t["complete"] and t or None, timeout=40)
            assert abs(done["files"][0]["duration"] - 16) < 0.5 and not done["live"]
            assert wait_for(lambda: sc.result(fid)["final"] or None, timeout=40)
            assert wait_for(lambda: app.ads.state(fid)["analyzedTo"] >= done["files"][0]["duration"] - 0.01
                            or None, timeout=40)
    finally:
        faulthandler.cancel_dump_traceback_later()
        frozen.close()


def test_a_wholly_overlapped_growing_file_keeps_the_timeline_incomplete(boot, monkeypatch):
    """Right after a reconnect the new file can lie entirely under the previous one's
    wall span. It isn't played yet, but the timeline mustn't call itself complete: the
    extension would drop to its 60 s refresh just as the stream resumes."""
    from fone.extractor import FileState, Segment
    from fone.recordings import Recording

    app, chan, _ = boot(start=())
    t = time.time() - 120
    walls = {"a": (t, 100.0), "b": (t + 95, 3.0)}
    recs = []
    for fid, status in (("a", "done"), ("b", "running")):
        rec = Recording(id=fid, relpath=f"{CHANNEL}/{fid}.flv", path=chan / f"{fid}.flv", channel=CHANNEL,
                        name=f"{fid}.flv", title=fid, size=1, mtime=time.time(), name_time=walls[fid][0],
                        name_precision=0.001)
        app.extractor.files[fid] = FileState(rec=rec, out_dir=chan / fid, status=status)
        recs.append(rec)

    def pieces(st):
        w, d = walls[st.rec.id]
        return [Piece(w, 0.0, d)], [Segment("seg_00000.ts", 0.0, d)], False

    monkeypatch.setattr(app.recordings, "scan", lambda *a: recs)  # the scanner keeps "finding" them
    monkeypatch.setattr(app.extractor, "pieces", pieces)
    monkeypatch.setattr(app.extractor, "is_growing", lambda rec, now=None: rec.id == "b")
    with TestClient(app.app) as client:
        tl = timeline(client, t - 60, t + 300)
    assert [f["inPlaylist"] for f in tl["files"]] == [True, False]
    assert not tl["complete"]


def test_web_pages_cannot_change_anything(boot):
    """CORS * plus private-network access lets any page you visit reach this server.
    Reads stay open; writes only from the extension (or curl, which sends no Origin)."""
    app, _, _ = boot(start=(), FONE_ROOMS="23954427=胡闹P房")
    q = {"channel": CHANNEL, "clock": "multiviewer"}
    body = {**q, "delay": 12.0}
    evil = {"Origin": "https://evil.example"}
    ext = {"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"}
    pos = {"messages": [{"Timestamp": "2026-10-03T08:00:00.000Z",
                         "Entries": {"1": {"X": 1, "Y": 2, "Z": 0, "Status": "OnTrack"}}}]}
    with TestClient(app.app) as client:
        for h in (evil, {"Origin": "null"}):
            assert client.put("/api/sync/123", json=body, headers=h).status_code == 403
            assert client.post("/api/timing/session/11731/positions", json=pos, headers=h).status_code == 403
            assert client.post("/api/backfill/import", json={"bvid": "BV1xx411c7mD"}, headers=h).status_code == 403
        assert client.get("/api/sync/123", params=q).json()["source"] == "default"
        assert not (app.cfg.data_dir / "timing" / "11731").exists()
        assert client.get("/api/backfill/status").json()["jobs"] == []
        # the preflight still answers (the browser then gets the 403 above, with CORS headers)
        pre = client.options("/api/sync/123", headers={**evil, "Access-Control-Request-Method": "PUT"})
        assert pre.status_code == 200
        r = client.put("/api/sync/123", json=body, headers=evil)
        assert r.headers.get("access-control-allow-origin") == "*"
        # reads from anywhere, writes from the extension and from scripts
        assert client.get("/api/health", headers=evil).status_code == 200
        assert client.put("/api/sync/123", json=body, headers=ext).status_code == 200
        assert client.put("/api/sync/456", json=body).status_code == 200
        assert client.get("/api/sync/123", params=q).json() == {"delay": 12.0, "source": "content"}
