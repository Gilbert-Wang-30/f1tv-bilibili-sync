"""Ad service: the real KL qualifying recording (both reels, exact edges, nothing else),
growing files (incremental, latency), persistence, re-extraction and the route."""

import asyncio
import json
import os
import shutil
import subprocess
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fone import ads, config, recordings
from fone.ads import AdService, load_refs, make_router
from fone.ads_detect import SR, write_refs
from fone.extractor import Extractor, FileState
from fone.recordings import Recording
from test_ads_detect import A, B, C, clip, mix, talk, write_wav

FIXTURES = Path(__file__).parent / "fixtures" / "ads"
KL_FLV = Path(os.environ.get(
    "FONE_TEST_KL_FLV",
    "/mnt/nas/media/library/recordings/fone/胡闹P房/20261003-150312-650-【胡闹P房】16点 巴林 排位赛.flv"))
TOL = 0.2  # allowed edge error vs. ground truth, seconds
SH = ZoneInfo("Asia/Shanghai")


class Files:
    """The part of the extractor AdService reads."""

    def __init__(self, *states: FileState):
        self.files = {s.rec.id: s for s in states}


def rec_for(path: Path, fid: str = "f1") -> Recording:
    return Recording(id=fid, relpath=f"c/{path.name}", path=path, channel="c", name=path.name, title="",
                     size=1, mtime=0, name_time=None, name_precision=None)


@pytest.fixture(scope="module")
def synth_refs_dir(tmp_path_factory) -> Path:
    """The synthetic test clips as a reference set: fone/ads/refs.npz describes the
    streamer's real clips, which the synthetic recordings below don't contain."""
    d = tmp_path_factory.mktemp("refs")
    write_refs(d / "refs.npz", [(n, clip(n)) for n in (A, B, C)])
    return d


@pytest.fixture
def synth_refs(synth_refs_dir, monkeypatch):
    monkeypatch.setattr(ads, "REFS_DIR", synth_refs_dir)


def make_hls(x: np.ndarray, out_dir: Path) -> None:
    """Encode like the recorder (AAC 48 kHz stereo), segment like the extractor."""
    wav = out_dir.parent / f"{out_dir.name}.wav"
    out_dir.mkdir(parents=True)
    write_wav(wav, x)
    subprocess.run([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(wav),
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "0", "-hls_playlist_type", "event",
        "-hls_flags", "temp_file+independent_segments", "-hls_segment_type", "mpegts",
        "-hls_segment_filename", str(out_dir / "seg_%05d.ts"), str(out_dir / "index.m3u8"),
    ], check=True)


async def until(fn, timeout: float = 60, every: float = 0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if v := fn():
            return v
        await asyncio.sleep(every)
    raise AssertionError("timed out")


def assert_matches(spans: list[dict], truth: list[dict], tol: float = TOL) -> None:
    """One span per true play, both edges within tol, and nothing else."""
    assert [s["name"] for s in spans] == [t["name"] for t in truth], spans
    for s, t in zip(spans, truth):
        assert abs(s["start"] - t["start"]) < tol and abs(s["end"] - t["end"]) < tol, (s, t)


def found(spans: list[dict], t: dict) -> bool:
    return any(s["name"] == t["name"] and abs(s["start"] - t["start"]) < TOL for s in spans)


# ---- the real KL qualifying recording ------------------------------------------


@pytest.fixture(scope="session")
def kl(tmp_path_factory):
    """The KL recording's audio HLS, made once by the real Extractor (finished-file path)."""
    if not os.access(KL_FLV, os.R_OK):
        pytest.skip(f"KL test recording not readable at {KL_FLV} (mount the NAS into the container)")
    cfg = replace(config.load(), rec_dir=KL_FLV.parent.parent, data_dir=tmp_path_factory.mktemp("kl"))
    rec = next(r for r in recordings.scan(cfg.rec_dir, cfg.filename_tz) if r.path == KL_FLV)

    async def extract() -> FileState:
        ex = Extractor(cfg)
        ex.update([rec])
        st = ex.files[rec.id]
        ex.ensure(st)
        await until(lambda: st.status in ("done", "error"), timeout=600, every=0.5)
        return st

    st = asyncio.run(extract())
    assert st.status == "done", st.error
    return cfg, rec, json.loads((FIXTURES / "kl_groundtruth.json").read_text())["plays"]


def test_kl_finished_file_both_reels_and_nothing_else(kl):
    cfg, rec, truth = kl
    ex = Extractor(cfg)
    ex.update([rec])
    st = ex.files[rec.id]
    assert st.status == "done"
    duration = st.segments()[0][-1].end
    (st.out_dir / "ads.json").unlink(missing_ok=True)

    async def analyse(timeout: float) -> AdService:
        svc = AdService(cfg, ex)
        await svc.start()
        try:
            await until(lambda: svc.analyzed_to(rec.id) >= duration - 0.01, timeout=timeout)
        finally:
            await svc.stop()
        return svc

    t = time.time()
    svc = asyncio.run(analyse(600))
    took = time.time() - t
    state = svc.state(rec.id)
    assert_matches(state["spans"], truth)
    doc = json.loads((st.out_dir / "ads.json").read_text())
    assert doc["final"] and doc["spans"] == state["spans"] and doc["version"] == svc.version
    print(f"\nKL: {duration / 3600:.2f} h of audio analysed in {took:.1f} s wall")

    # A restart reuses the stored result instead of analysing again.
    stamp = (st.out_dir / "ads.json").stat().st_mtime_ns
    again = asyncio.run(analyse(5))
    assert again.spans(rec.id) == state["spans"]
    assert (st.out_dir / "ads.json").stat().st_mtime_ns == stamp

    app = FastAPI()
    app.include_router(make_router(svc))
    client = TestClient(app)
    r = client.get(f"/api/ads/{rec.id}")
    assert r.status_code == 200
    assert r.json() == {"spans": state["spans"], "analyzedTo": state["analyzedTo"]}
    assert set(r.json()["spans"][0]) == {"start", "end", "name"}
    assert client.get("/api/ads/0123456789abcdef").status_code == 404


def test_kl_growing_file_knows_each_clip_within_4s(kl, tmp_path):
    """Replay the KL reel as if live: the playlist grows one 2-s segment per poll."""
    cfg, rec, truth = kl
    src = Extractor(cfg)
    src.update([rec])
    segs, _ = src.files[rec.id].segments()
    reel = [t for t in truth if t["start"] < 3100]
    join = next(i for i, s in enumerate(segs) if s.start > reel[0]["start"] - 60)  # we join 1 min before it
    stop = next(i for i, s in enumerate(segs) if s.start > reel[-1]["end"] + 20)
    out = tmp_path / "hls" / rec.id
    out.mkdir(parents=True)
    for s in segs[:stop]:
        (out / s.name).symlink_to(src.files[rec.id].out_dir / s.name)
    grow = FileState(rec=rec, out_dir=out, status="running", follow=True)
    svc = AdService(cfg, Files(grow))
    svc.refs, svc.version = load_refs()
    clock = [1_800_000_000.0]

    def publish(n: int, ended: bool = False) -> None:
        lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0"]
        for s in segs[:n]:
            lines += [f"#EXTINF:{s.duration:.6f},", s.name]
        grow.playlist.write_text("\n".join(lines + (["#EXT-X-ENDLIST"] if ended else [])) + "\n")
        clock[0] += 1  # FileState caches the playlist by mtime
        os.utime(grow.playlist, (clock[0], clock[0]))

    known_at: dict[int, float] = {}  # truth index -> HLS edge when its span first appeared

    async def drive() -> None:
        publish(join)
        while await svc._tick():  # catch up on what was recorded before we joined
            pass
        for n in range(join + 1, stop + 1):
            publish(n)
            await svc._tick()
            edge = segs[n - 1].end
            state = svc.state(rec.id)
            assert state["analyzedTo"] <= edge
            for i, t in enumerate(reel):
                if i not in known_at and found(state["spans"], t):
                    known_at[i] = edge
                # The analyzedTo promise: every clip that started before it is known.
                if t["start"] < state["analyzedTo"]:
                    assert found(state["spans"], t), (t, state)
        grow.status = "done"
        publish(stop, ended=True)
        await svc._tick()

    asyncio.run(drive())
    lat = [known_at[i] - t["start"] for i, t in enumerate(reel)]
    print(f"\nseconds of clip on the HLS when it was recognised: {[round(x, 2) for x in lat]}")
    assert max(lat) <= 4.0
    final = svc.state(rec.id)
    assert_matches(final["spans"], reel)
    assert final["analyzedTo"] == pytest.approx(segs[stop - 1].end, abs=1e-3)


# ---- synthetic recordings ------------------------------------------------------------


def test_growing_recording_end_to_end(tmp_path, synth_refs):
    """A recorder writing in real time: the extractor tails it, the clip is known a few
    seconds after it starts, and its edges are exact once the recording ends."""
    cfg = replace(config.load(), rec_dir=tmp_path / "rec", data_dir=tmp_path / "data", scan_interval=0.3,
                  growing_idle_seconds=4, reconnect_idle_seconds=2)
    chan = cfg.rec_dir / "胡闹P房"
    chan.mkdir(parents=True)
    lead, c = 6.0, clip(C)
    x = mix([talk(lead, 21), c, talk(4, 22)])
    wav = tmp_path / "src.wav"
    write_wav(wav, x)
    path = chan / (datetime.now(SH).strftime("%Y%m%d-%H%M%S-%f")[:-3] + "-【胡闹P房】ads test.flv")

    async def main():
        ex = Extractor(cfg)
        svc = AdService(cfg, ex)
        await svc.start()

        async def scanner():
            while True:
                ex.update(recordings.scan(cfg.rec_dir, cfg.filename_tz))
                ex.tick()
                await asyncio.sleep(cfg.scan_interval)

        scan = asyncio.create_task(scanner())
        writer = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-re", "-i", str(wav),
            "-f", "lavfi", "-i", f"color=c=black:s=64x36:r=10:d={len(x) / SR:.2f}",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-c:v", "libx264", "-tune", "zerolatency", "-g", "20", "-shortest",
            "-flush_packets", "1", "-f", "flv", str(path))
        written = asyncio.create_task(writer.wait())
        first_bytes = seen = provisional = None
        try:
            deadline = time.time() + 120
            while time.time() < deadline:
                now = time.time()
                if first_bytes is None and path.exists() and path.stat().st_size > 0:
                    first_bytes = now
                fid = next(iter(ex.files), None)
                if fid and seen is None and (spans := svc.spans(fid)):
                    seen, provisional = now, spans[0]
                st = ex.files.get(fid) if fid else None
                if written.done() and st and st.status == "done" and \
                        svc.analyzed_to(fid) >= st.segments()[0][-1].end - 0.01:
                    return fid, st, svc, first_bytes, seen, provisional
                await asyncio.sleep(0.05)
            raise AssertionError("timed out")
        finally:
            scan.cancel()
            await svc.stop()
            await ex.shutdown()
            if not written.done():
                writer.kill()
                await written

    fid, st, svc, first_bytes, seen, provisional = asyncio.run(main())
    latency = seen - (first_bytes + lead)
    print(f"\nclip known {latency:.2f} s after it started playing (wall clock)")
    # Recognition needs ~1-2 s of the clip, and the extractor publishes 2-s segments.
    assert latency < 5.0
    (span,) = svc.spans(fid)
    assert span["name"] == C == provisional["name"]
    assert abs(span["start"] - lead) < 0.1 and abs(span["end"] - (lead + len(c) / SR)) < 0.1
    doc = json.loads((st.out_dir / "ads.json").read_text())
    assert doc["final"] and doc["spans"] == [span]


def test_redone_extraction_and_new_refs_are_reanalysed(tmp_path, synth_refs):
    cfg = replace(config.load(), rec_dir=tmp_path / "rec", data_dir=tmp_path / "data")
    out = tmp_path / "data" / "hls" / "f1"
    make_hls(mix([talk(5, 31), clip(C), talk(5, 32)]), out)
    st = FileState(rec=rec_for(tmp_path / "x.flv"), out_dir=out, status="done")
    files = Files(st)
    end = lambda: st.segments()[0][-1].end - 0.01  # noqa: E731

    async def main():
        svc = AdService(cfg, files)
        assert svc.state("f1") == {"spans": [], "analyzedTo": 0.0}
        assert svc.state("nope") is None
        await svc.start()
        try:
            await until(lambda: svc.analyzed_to("f1") >= end())
            first = svc.spans("f1")
            # The recorder appended to the file: the extractor wipes the dir and redoes it.
            st.status = "running"
            shutil.rmtree(out)
            make_hls(mix([talk(12, 33), clip(C), talk(5, 34)]), out)
            st.status = "done"
            await until(lambda: (s := svc.spans("f1")) and abs(s[0]["start"] - 12) < 0.1 and
                        svc.analyzed_to("f1") >= end())
            return first, svc.spans("f1"), svc.version
        finally:
            await svc.stop()

    first, second, version = asyncio.run(main())
    assert [(s["name"], round(s["start"])) for s in first] == [(C, 5)]
    assert [(s["name"], round(s["start"])) for s in second] == [(C, 12)]

    # Results from other refs / detector settings are not trusted.
    doc = json.loads((out / "ads.json").read_text())
    assert doc["version"] == version
    doc.update(version="stale", spans=[])
    (out / "ads.json").write_text(json.dumps(doc))

    async def reload():
        svc = AdService(cfg, files)
        await svc.start()
        try:
            return await until(lambda: svc.analyzed_to("f1") >= end() and svc.spans("f1"))
        finally:
            await svc.stop()

    assert [(s["name"], round(s["start"])) for s in asyncio.run(reload())] == [(C, 12)]
    assert json.loads((out / "ads.json").read_text())["version"] == version
