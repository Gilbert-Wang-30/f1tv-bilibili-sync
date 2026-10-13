"""Video HLS from the FLV keyframe index: index vs ffprobe, segments that decode and join
cleanly, time bases (v, m) against the real extractor, growing files, persistence, and
frame accuracy on the real KL qualifying recording."""

import asyncio
import os
import random
import re
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fone import config, recordings, video
from fone.extractor import Extractor
from fone.video import VideoService, make_router
from fone.video_flv import FlvIndex, Snapshot

SH = ZoneInfo("Asia/Shanghai")
CHANNEL = "胡闹P房"
KL_FLV = Path(os.environ.get(
    "FONE_TEST_KL_FLV",
    "/mnt/nas/media/library/recordings/fone/胡闹P房/20261003-150312-650-【胡闹P房】16点 巴林 排位赛.flv"))
FLASHES = (5.0, 13.0)  # source seconds: a white video frame and a 1 kHz beep start here


def make_flv(path: Path, seconds: float, *, audio_delay: float = 0.0, realtime: bool = False,
             vcodec: str = "libx264", gop: int = 25):
    """testsrc2 (every frame different) with white flash frames, plus silence with beeps at
    the same source times. B-frames on, like the streams we record (PTS = DTS + CTS)."""
    flash = "+".join(f"between(t,{t},{t + 0.03})" for t in FLASHES)
    beep = "+".join(f"between(t,{t},{t + 0.1})" for t in FLASHES)
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
    if realtime:
        cmd += ["-re"]
    cmd += ["-f", "lavfi", "-i", f"testsrc2=size=160x90:rate=25:d={seconds},"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='{flash}'"]
    if audio_delay:
        cmd += ["-itsoffset", str(audio_delay)]
    cmd += ["-f", "lavfi", "-i", f"aevalsrc='({beep})*0.5*sin(2*PI*1000*t)':s=48000:d={seconds}",
            "-c:v", vcodec, "-g", str(gop), "-bf", "2", "-c:a", "aac", "-b:a", "64k",
            "-flush_packets", "1", "-f", "flv", str(path)]
    if vcodec == "libx264":
        cmd[cmd.index("-bf"):cmd.index("-bf")] = ["-tune", "zerolatency"] if realtime else ["-sc_threshold", "0"]
    return subprocess.Popen(cmd) if realtime else subprocess.run(cmd, check=True)


def probe_packets(path, stream: str, extra=()) -> list[dict]:
    out = subprocess.run(["ffprobe", "-v", "error", *extra, "-select_streams", stream,
                          "-show_entries", "packet=pts_time,dts_time,pos,flags", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    pkts = []
    for line in out.splitlines():
        pts, dts, pos, flags = (line.split(",") + [""] * 4)[:4]
        pkts.append({"pts": float(pts), "dts": float(dts), "pos": int(pos) if pos.isdigit() else None,
                     "key": flags.startswith("K")})
    return pkts


def flash_pts(src, copyts=True, extra=()) -> list[float]:
    """PTS of the white frames (mean luma > 200), from ffmpeg's showinfo."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", *extra, *(["-copyts"] if copyts else []), "-i", str(src),
                        "-map", "0:v:0", "-vf", "showinfo", "-f", "null", "-"], capture_output=True, text=True)
    return [float(m.group(1)) for m in re.finditer(r"pts_time:([0-9.]+).*?mean:\[(\d+)", p.stderr)
            if int(m.group(2)) > 200]


def beep_starts(src, copyts=True, extra=()) -> list[float]:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", *extra, *(["-copyts"] if copyts else []), "-i", str(src),
                        "-map", "0:a:0", "-af", "silencedetect=n=-30dB:d=0.5", "-f", "null", "-"],
                       capture_output=True, text=True)
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", p.stderr)]
    return ends[:len(FLASHES)]  # (the end of the stream also ends a silence)


# ---- the index ------------------------------------------------------------------


@pytest.fixture(scope="module")
def flv(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("flv") / "a.flv"
    make_flv(path, 20, audio_delay=0.3)
    return path


def test_index_matches_ffprobe(flv):
    idx = FlvIndex(flv)
    idx.update()
    vid = probe_packets(flv, "v:0")
    aud = probe_packets(flv, "a:0")
    keys = [p for p in vid if p["key"]]
    assert idx.kf_ts == [round(p["dts"] * 1000) for p in keys]
    assert idx.kf_pos == [p["pos"] for p in keys]
    assert len(keys) == 20  # one per second
    assert idx.video_frames == len(vid) and idx.first_video_ts == round(vid[0]["dts"] * 1000)
    assert idx.first_audio_ts == round(aud[0]["dts"] * 1000) and idx.first_audio_ts >= 300
    assert idx.pos == flv.stat().st_size and idx.corrupt_at is None
    assert len(idx.video_headers) == 1 and len(idx.audio_headers) == 1
    assert vid[0]["pts"] > vid[0]["dts"]  # B-frames: the case where PTS != DTS


def test_incremental_updates_equal_one_pass(flv, tmp_path):
    """A file written in arbitrary pieces (tags cut anywhere) indexes exactly like the whole."""
    full = FlvIndex(flv)
    full.update()
    data = flv.read_bytes()
    grow = tmp_path / "g.flv"
    grow.write_bytes(b"")
    idx = FlvIndex(grow)
    rnd = random.Random(7)
    written = 0
    with open(grow, "ab") as f:
        while written < len(data):
            n = rnd.choice([1, 5, 11, 500, 4000, 70000])
            f.write(data[written:written + n])
            f.flush()
            written += n
            idx.update()
            assert idx.pos <= written
            assert idx.kf_pos == full.kf_pos[:len(idx.kf_pos)]
    idx.update()
    assert idx.to_json() == full.to_json()


def test_corrupt_tag_stops_the_index(flv, tmp_path):
    full = FlvIndex(flv)
    full.update()
    data = bytearray(flv.read_bytes())
    cut = full.kf_pos[10]
    data[cut] = 0x55  # not a tag type
    bad = tmp_path / "bad.flv"
    bad.write_bytes(bytes(data))
    idx = FlvIndex(bad)
    idx.update()
    assert idx.corrupt_at == cut and idx.pos == cut and idx.kf_pos == full.kf_pos[:10]
    assert idx.update() == 0  # never reads past it


def snap(kf, last_video, frames=100) -> Snapshot:
    return Snapshot(b"", (), (), tuple(kf), tuple(range(len(kf))), 0, kf[0], last_video, frames, last_video, 0)


def test_segments_cut_at_keyframes_and_only_grow():
    kf = [1000, 3000, 4200, 5000, 9000, 10000, 12000, 13900, 16000]
    s = snap(kf, 17960, frames=(17960 - 1000) // 40 + 1)
    live = s.segments(4000, finished=False)
    assert [(x.a, x.b) for x in live] == [(0, 3), (3, 4), (4, 7)]
    assert [x.start for x in live] == [0.0, 4.0, 8.0]
    assert [x.duration for x in live] == [4.0, 4.0, 4.9]
    done = s.segments(4000, finished=True)
    assert done[:len(live)] == live  # EVENT playlist only ever appends
    assert (done[-1].a, done[-1].b, done[-1].name, done[-1].start) == (7, None, "7_end.ts", 12.9)
    assert abs(done[-1].duration - (17960 - 13900 + 40) / 1000) < 1e-9  # to the last frame's end
    # a growing prefix of the keyframes gives a prefix of the segments
    for n in range(1, len(kf)):
        part = snap(kf[:n], kf[n - 1]).segments(4000, finished=False)
        assert part == live[:len(part)]


# ---- the service ----------------------------------------------------------------


def name_for(t: float, title: str = "【胡闹P房】16点 巴林 排位赛") -> str:
    return datetime.fromtimestamp(t, SH).strftime("%Y%m%d-%H%M%S-%f")[:-3] + f"-{title}.flv"


def app_for(svc: VideoService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_):
        await svc.start()
        yield
        await svc.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(make_router(svc))
    return app


class Env:
    def __init__(self, tmp_path: Path, growing_idle: float = 3.0):
        self.rec = tmp_path / "rec"
        (self.rec / CHANNEL).mkdir(parents=True)
        self.cfg = replace(config.load(), rec_dir=self.rec, data_dir=tmp_path / "data",
                           growing_idle_seconds=growing_idle, channel_aliases={})
        self.ex = Extractor(self.cfg)
        self.svc = VideoService(self.cfg, self.ex)
        self.app = app_for(self.svc)

    def rescan(self) -> None:
        self.ex.update(recordings.scan(self.cfg.rec_dir, self.cfg.filename_tz, self.cfg.channel_aliases))

    def add(self, src: Path, mtime: float | None = None, start: float | None = None) -> str:
        start = start if start is not None else time.time() - 7200
        dst = self.rec / CHANNEL / name_for(start)
        shutil.copy(src, dst)
        if mtime is not None:
            os.utime(dst, (mtime, mtime))
        self.rescan()
        return recordings.file_id(dst.relative_to(self.rec).as_posix())


def get_ok(client, url, timeout=30):
    """GET, riding out 202s the way a player would (Retry-After)."""
    deadline = time.time() + timeout
    while True:
        r = client.get(url)
        if r.status_code != 202:
            assert r.status_code == 200, (url, r.status_code, r.text)
            return r
        assert r.headers["retry-after"] and 0 <= r.json()["progress"] <= 1
        assert time.time() < deadline, "still indexing"
        time.sleep(0.2)


def playlist_segments(text: str) -> list[tuple[float, float, str]]:
    """[(start, duration, uri)] in playlist time."""
    out, t, d = [], 0.0, None
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            d = float(line[8:].split(",")[0])
        elif line and not line.startswith("#"):
            out.append((t, d, line))
            t += d
    return out


def save_hls(client, fid: str, text: str, out: Path) -> list[tuple[float, float, str]]:
    out.mkdir(parents=True, exist_ok=True)
    segs = playlist_segments(text)
    for _, _, uri in segs:
        (out / uri.replace("seg/", "")).write_bytes(get_ok(client, f"/api/video/{fid}/{uri}").content)
    (out / "index.m3u8").write_text(text.replace("seg/", ""))
    return segs


def test_finished_recording(flv, tmp_path):
    env = Env(tmp_path)
    end = time.time() - 3600
    fid = env.add(flv, mtime=end)
    # The extractor's audio HLS is what m means; extract it for real.
    asyncio.run(env.ex._extract(env.ex.files[fid], follow=False))
    assert env.ex.files[fid].status == "done"
    with TestClient(env.app) as client:
        info = get_ok(client, f"/api/video/{fid}/info").json()
        idx = FlvIndex(flv)
        idx.update()
        assert info["growing"] is False and info["codec"] == "h264" and (info["width"], info["height"]) == (160, 90)
        assert info["t0"] == idx.kf_ts[0] / 1000 and info["a0"] == idx.first_audio_ts / 1000
        assert info["mOffset"] == round(info["t0"] - info["a0"], 3) and info["mOffset"] < -0.25  # audio starts 0.3 s late
        assert abs(info["duration"] - 20) < 0.2 and info["edge"] == info["duration"]
        # wallStart: wall clock of v = 0, through the extractor's pieces (end lag here)
        piece = env.ex.pieces(env.ex.files[fid])[0][0]
        assert abs(info["wallStart"] - (piece.wall + info["mOffset"] - piece.media)) < 1e-3

        text = get_ok(client, f"/api/video/{fid}/index.m3u8").text
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and text.rstrip().endswith("#EXT-X-ENDLIST")
        hls = tmp_path / "hls"
        segs = save_hls(client, fid, text, hls)
        assert len(segs) == 5 and segs[-1][2].endswith("_end.ts")
        assert abs(sum(d for _, d, _ in segs) - info["duration"]) < 1e-3

        # Every segment decodes, and the joins are seamless: exactly the FLV's frames.
        p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(hls / "index.m3u8"), "-map", "0", "-f", "null", "-"],
                           capture_output=True, text=True)
        assert p.returncode == 0 and p.stderr == "", p.stderr
        src_v = probe_packets(flv, "v:0")
        k0 = next(i for i, x in enumerate(src_v) if x["key"])
        got_v = [x for _, _, uri in segs for x in probe_packets(hls / uri.replace("seg/", ""), "v:0")]
        assert [round(x["pts"], 3) for x in got_v] == [round(x["pts"], 3) for x in src_v[k0:]]
        src_a = [x for x in probe_packets(flv, "a:0") if x["pos"] > idx.kf_pos[0]]
        got_a = [x for _, _, uri in segs for x in probe_packets(hls / uri.replace("seg/", ""), "a:0")]
        # (TS packs several AAC frames per PES and the later ones get exact 1024-sample steps
        # instead of the FLV's whole milliseconds: same frames, same times to within 1 ms)
        assert len(got_a) == len(src_a) and max(abs(x["pts"] - y["pts"]) for x, y in zip(got_a, src_a)) < 0.0015
        # Playlist time is FLV time - t0: each segment starts at its keyframe's DTS.
        for start, _, uri in segs:
            first = probe_packets(hls / uri.replace("seg/", ""), "v:0")[0]
            assert first["key"] and abs(first["dts"] - (start + info["t0"])) < 1e-3

        # The time bases against real decodes: a flash frame and a beep made at the same
        # source instant (the audio was muxed 0.3 s late, so 0.3 s apart in the FLV).
        flv_flash, flv_beep = flash_pts(flv), beep_starts(flv)
        assert len(flv_flash) == len(flv_beep) == len(FLASHES)
        served_flash = flash_pts(hls / "index.m3u8")  # player timeline + t0 (copyts)
        assert served_flash == flv_flash
        v_flash = [t - info["t0"] for t in served_flash]
        audio_pl = env.ex.files[fid].playlist
        m_beep = beep_starts(audio_pl, copyts=False)  # the audio HLS timeline: m
        assert len(m_beep) == len(FLASHES)
        for v, m, f, b in zip(v_flash, m_beep, flv_flash, flv_beep):
            assert abs(m - (b - info["a0"])) < 0.002  # m = FLV time - a0
            assert abs((m - v) - ((b - f) + info["mOffset"])) < 0.002  # m = v + mOffset

        # A segment is the same bytes every time (immutable URLs), and cacheable.
        uri = segs[1][2]
        r1 = client.get(f"/api/video/{fid}/{uri}")
        assert r1.content == (hls / uri.replace("seg/", "")).read_bytes()
        assert "immutable" in r1.headers["cache-control"] and r1.headers["content-type"] == "video/mp2t"
        assert "EXT-X-START:TIME-OFFSET=7.500" in client.get(f"/api/video/{fid}/index.m3u8?start=7.5").text


def test_bad_requests(flv, tmp_path):
    env = Env(tmp_path)
    fid = env.add(flv, mtime=time.time() - 3600)
    (env.rec / CHANNEL / "x.mp4").write_bytes(b"\0" * 100)
    env.rescan()
    mp4 = next(f for f, st in env.ex.files.items() if st.rec.name == "x.mp4")
    with TestClient(env.app) as client:
        get_ok(client, f"/api/video/{fid}/info")
        for url in ("/api/video/nope/info", f"/api/video/{mp4}/index.m3u8", f"/api/video/{fid}/seg/3_1.ts",
                    f"/api/video/{fid}/seg/0_999.ts", f"/api/video/{fid}/seg/..%2Fdone.json", f"/api/video/{fid}/seg/0_2.mp4"):
            assert client.get(url).status_code == 404, url


def test_202_while_indexing(flv, tmp_path, monkeypatch):
    env = Env(tmp_path)
    fid = env.add(flv, mtime=time.time() - 3600)
    gate = threading.Event()
    real = FlvIndex.update

    def slow(self):
        gate.wait(10)
        return real(self)

    monkeypatch.setattr(FlvIndex, "update", slow)
    monkeypatch.setattr(video, "WAIT_SECONDS", 0.05)
    with TestClient(env.app) as client:
        for url in ("info", "index.m3u8", "seg/0_4.ts"):
            r = client.get(f"/api/video/{fid}/{url}")
            assert r.status_code == 202 and r.headers["retry-after"] == str(video.RETRY_AFTER)
            assert r.json() == {"status": "indexing", "progress": 0.0}
        gate.set()
        assert get_ok(client, f"/api/video/{fid}/info").json()["duration"] > 19


def test_index_persists_and_goes_with_the_recording(flv, tmp_path, monkeypatch):
    env = Env(tmp_path)
    fid = env.add(flv, mtime=time.time() - 3600)
    with TestClient(env.app) as client:
        first = get_ok(client, f"/api/video/{fid}/info").json()
    saved = env.cfg.data_dir / "video" / f"{fid}.json"
    assert saved.exists()

    # Restart: the stored index is reused, not rebuilt from the first byte.
    starts = []
    real = FlvIndex.update
    monkeypatch.setattr(FlvIndex, "update", lambda self: (starts.append(self.pos), real(self))[1])
    with TestClient(app_for(VideoService(env.cfg, env.ex))) as client:
        assert get_ok(client, f"/api/video/{fid}/info").json() == first
    assert starts == [flv.stat().st_size]
    monkeypatch.undo()

    # A replaced file (same name, different content) is not mistaken for the old one.
    path = env.ex.files[fid].rec.path
    make_flv(tmp_path / "b.flv", 8)
    shutil.copy(tmp_path / "b.flv", path)
    os.utime(path, (time.time() - 3600,) * 2)
    env.rescan()
    svc3 = VideoService(env.cfg, env.ex)
    assert abs(asyncio.run(_info(svc3, fid))["duration"] - 8) < 0.2

    # The janitor deletes the recording: its index goes too. An unreachable NAS (empty scan) keeps it.
    env.ex.update([])
    env.svc.sweep()
    assert saved.exists()
    (env.rec / CHANNEL / path.name).unlink()
    other = env.add(flv, mtime=time.time() - 3600, start=time.time() - 100000)
    env.svc.sweep()
    assert not saved.exists() and other in env.ex.files


async def _info(svc: VideoService, fid: str) -> dict:
    deadline = time.time() + 30
    while True:
        try:
            return await svc.info(fid)
        except video.Indexing:
            assert time.time() < deadline
            await asyncio.sleep(0.1)


def test_growing_recording(tmp_path):
    """A recording still being written: EVENT playlist of closed segments only, growing,
    then VOD with the tail once the recorder is done with it. URLs never change."""
    env = Env(tmp_path, growing_idle=3.0)
    path = env.rec / CHANNEL / name_for(time.time())
    writer = make_flv(path, 14, realtime=True)
    with TestClient(env.app) as client:
        try:
            deadline = time.time() + 30
            while True:
                env.rescan()
                fid = recordings.file_id(path.relative_to(env.rec).as_posix())
                r = client.get(f"/api/video/{fid}/index.m3u8") if fid in env.ex.files else None
                if r is not None and r.status_code == 200 and len(playlist_segments(r.text)) >= 2:
                    break
                assert time.time() < deadline
                time.sleep(0.3)
            live = r.text
            assert "#EXT-X-PLAYLIST-TYPE:EVENT" in live and "#EXT-X-ENDLIST" not in live and "_end.ts" not in live
            info = client.get(f"/api/video/{fid}/info").json()
            assert info["growing"] and info["edge"] > info["duration"] - 1e-6
            first_uri = playlist_segments(live)[0][2]
            first = client.get(f"/api/video/{fid}/{first_uri}")
            assert first.status_code == 200
            p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "mpegts", "-i", "pipe:0", "-f", "null", "-"],
                               input=first.content, capture_output=True)
            assert p.returncode == 0 and not p.stderr
            time.sleep(2)
            env.rescan()
            later = client.get(f"/api/video/{fid}/info").json()
            assert later["edge"] > info["edge"]
        finally:
            writer.wait(timeout=40)
        deadline = time.time() + 30
        while True:
            env.rescan()
            r = client.get(f"/api/video/{fid}/index.m3u8")
            if r.status_code == 200 and r.text.rstrip().endswith("#EXT-X-ENDLIST"):
                break
            assert time.time() < deadline
            time.sleep(0.5)
        done = r.text
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in done
        live_segs, done_segs = playlist_segments(live), playlist_segments(done)
        assert done_segs[:len(live_segs)] == live_segs and done_segs[-1][2].endswith("_end.ts")
        assert abs(sum(d for _, d, _ in done_segs) - 14) < 0.3
        assert client.get(f"/api/video/{fid}/{first_uri}").content == first.content
        assert client.get(f"/api/video/{fid}/info").json()["growing"] is False


def test_hevc_enhanced_flv(tmp_path):
    """HEVC in enhanced FLV (FourCC tags), as newer encoders and CDNs send it."""
    path = tmp_path / "h.flv"
    try:
        make_flv(path, 6, vcodec="libx265")
    except subprocess.CalledProcessError:
        pytest.skip("no libx265 / enhanced FLV muxing in this ffmpeg")
    idx = FlvIndex(path)
    idx.update()
    keys = [p for p in probe_packets(path, "v:0") if p["key"]]
    assert idx.kf_pos == [p["pos"] for p in keys] and len(keys) >= 1 and idx.video_headers
    env = Env(tmp_path)
    fid = env.add(path, mtime=time.time() - 3600)
    with TestClient(env.app) as client:
        info = get_ok(client, f"/api/video/{fid}/info").json()
        assert info["codec"] == "hevc"
        text = get_ok(client, f"/api/video/{fid}/index.m3u8").text
        hls = tmp_path / "hls"
        save_hls(client, fid, text, hls)
        p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(hls / "index.m3u8"), "-f", "null", "-"],
                           capture_output=True, text=True)
        assert p.returncode == 0 and not p.stderr, p.stderr


# ---- the real recording ---------------------------------------------------------


def frame_md5s(src, extra_in=(), extra_out=()) -> list[tuple[float, str]]:
    """(pts seconds, md5 of the decoded picture) per frame."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", *extra_in, "-i", str(src), "-map", "0:v:0", *extra_out,
                        "-f", "framemd5", "-"], capture_output=True, text=True, check=True)
    out = []
    for line in p.stdout.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        f = [x.strip() for x in line.split(",")]
        out.append((int(f[2]), f[5]))
    tb = re.search(r"#tb 0: (\d+)/(\d+)", p.stdout)
    return [(pts * int(tb.group(1)) / int(tb.group(2)), md5) for pts, md5 in out]


@pytest.mark.skipif(not KL_FLV.exists(), reason="KL qualifying recording not mounted")
def test_kl_frame_accuracy(tmp_path):
    """Frame shown at player time v == the FLV's frame at FLV time v + t0 (ffmpeg -ss),
    at a few points across the 3 h KL qualifying recording."""
    cfg = replace(config.load(), rec_dir=KL_FLV.parent.parent, data_dir=tmp_path, channel_aliases={})
    ex = Extractor(cfg)
    ex.update(recordings.scan(cfg.rec_dir, cfg.filename_tz, {}))
    fid = next(f for f, st in ex.files.items() if st.rec.path == KL_FLV)
    svc = VideoService(cfg, ex)
    start_time = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0",
                                       str(KL_FLV)], capture_output=True, text=True).stdout.strip(", \n"))

    async def run():
        info = await _info(svc, fid)
        assert info["growing"] is False and info["codec"] == "h264" and (info["width"], info["height"]) == (1920, 1080)
        assert (info["t0"], info["a0"], info["mOffset"]) == (0.0, 0.08, -0.08)
        assert abs(info["duration"] - 10378.5) < 1
        segs = playlist_segments(await svc.playlist(fid))
        for v in (60.0, 3601.3, 7200.4, 10301.7):
            k = max(i for i, (s, _, _) in enumerate(segs) if s <= v)
            start, dur, uri = segs[k]
            assert start <= v < start + dur
            # B-frames: the picture on screen just after a cut can come from the previous
            # segment (decode order), so decode both, back to back like the player does.
            body = b""
            for _, _, u in segs[max(0, k - 1): k + 1]:
                body += (await svc.segment(fid, u.replace("seg/", "")))[0]
            seg = tmp_path / f"{k}.ts"
            seg.write_bytes(body)
            frames = frame_md5s(seg, extra_in=["-copyts"])
            # The picture on screen at v: the last frame presented at or before FLV time v + t0.
            pts, md5 = max((f for f in frames if f[0] <= v + info["t0"] + 1e-6), key=lambda f: f[0])
            assert v + info["t0"] - pts < 0.04 - 1e-6  # within one 25 fps frame
            # ffmpeg's own accurate seek in the FLV (input -ss counts from the file's start_time)
            ref = frame_md5s(KL_FLV, extra_in=["-ss", f"{pts - start_time - 0.001:.3f}"], extra_out=["-frames:v", "1"])
            assert ref[0][1] == md5, (v, pts)
            if v in (3601.3, 7200.4):  # live picture here: the match is the one frame, not a lookalike
                i = [f[0] for f in frames].index(pts)
                assert frames[i - 1][1] != md5 != frames[i + 1][1]

    asyncio.run(run())
