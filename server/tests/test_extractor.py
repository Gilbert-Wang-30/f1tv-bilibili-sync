import asyncio
import json
import os
import random
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from fone import config
from fone.extractor import LAG_QUANTILE, RUN_FILE, Extractor, FileState, Segment, build_pieces, live_lags
from fone.recordings import Recording


def segs(n, d=2.0):
    return [Segment(f"seg_{i:05d}.ts", i * d, d) for i in range(n)]


def segs_of(durations):
    out, pos = [], 0.0
    for i, d in enumerate(durations):
        out.append(Segment(f"seg_{i:05d}.ts", pos, d))
        pos += d
    return out


def test_fallback_lag_single_piece():
    pieces = build_pieces(segs(10), [], fallback_lag=1000.0)
    assert len(pieces) == 1
    assert pieces[0].wall == 1000.0 and pieces[0].media == 0 and pieces[0].duration == 20.0


def test_no_information_no_pieces():
    assert build_pieces(segs(10), [], None) == []


def test_delivery_jitter_is_one_piece_at_the_low_percentile():
    random.seed(1)
    s = segs(200)
    # Audio arrives up to 2.5 s late, never early.
    lags = [(i, 5000.0 + random.uniform(0, 2.5)) for i in range(5, 200)]
    pieces = build_pieces(s, lags, None)
    assert len(pieces) == 1
    assert abs(pieces[0].wall - 5000.0) < 0.3
    assert pieces[0].duration == 400.0


def test_short_cdn_burst_does_not_split():
    s = segs(200)
    lags = [(i, 5000.0 + (4.0 if 80 <= i < 90 else 0.0)) for i in range(200)]  # 20 s of late delivery
    assert len(build_pieces(s, lags, None)) == 1


def test_cdn_see_saw_is_one_piece_at_the_low_percentile():
    """Bilibili's CDN delivers ±3 s late for minutes at a time while the content timeline
    stays linear (2026-10-09 sprint qualifying: 3 pieces 4 s apart, against one constant
    screen-clock delay). One piece, at the fastest delivery seen."""
    random.seed(2)
    s = segs(1800)  # an hour
    # 4 min fast, 4 min slow, ... on top of the usual sub-second burst jitter
    lags = [(i, 5000.0 + (3.0 if (i // 120) % 2 else -3.0) + random.uniform(0, 1.0)) for i in range(0, 1800, 2)]
    pieces = build_pieces(s, lags, None)
    values = sorted(v for _, v in lags)
    assert len(pieces) == 1 and pieces[0].media == 0 and pieces[0].duration == 3600.0
    assert pieces[0].wall == values[int(len(values) * LAG_QUANTILE)]
    assert 4997.0 <= pieces[0].wall < 4997.1


def test_a_lag_step_alone_does_not_split():
    # A sustained 10 s shift in arrival lags over continuous media. Arrival lags can't tell
    # a stall the recorder spliced out from the CDN's see-saw, so neither moves the lag;
    # what does show a hole is the media timeline (below).
    s = segs(200)
    lags = [(i, 5000.0 if i < 100 else 5010.0) for i in range(200)]
    pieces = build_pieces(s, lags, None)
    assert len(pieces) == 1 and pieces[0].wall == 5000.0 and pieces[0].duration == 400.0


def test_catch_up_segments_get_the_file_lag():
    s = segs(50)
    lags = [(i, 7000.0) for i in range(20, 50)]
    pieces = build_pieces(s, lags, None)
    assert len(pieces) == 1 and pieces[0].media == 0 and abs(pieces[0].wall - 7000.0) < 1e-6


# What ffmpeg (8.1) writes for 2 s segments over audio with a 10 s timestamp jump after media
# 7.04: the segment it falls in spans it (6.016 -> 17.0, 1.02 s of audio), then one-packet
# segments until its 2 s grid catches up.
JUMP = [2.005333] * 3 + [10.984] + [0.021333] * 4 + [0.917333] + [2.005333] * 3 + [0.021]


def test_a_hole_in_the_media_is_left_out_at_the_same_lag():
    s = segs_of(JUMP)
    for lags, fallback in (([(i, 5000.0) for i in range(len(s))], None), ([], 5000.0)):
        p1, p2 = build_pieces(s, lags, fallback, 2.0)
        # the timestamps already count the hole: one lag on both sides
        assert p1.wall - p1.media == pytest.approx(5000.0) and p2.wall - p2.media == pytest.approx(5000.0)
        assert p1.media == 0 and p1.duration == pytest.approx(s[3].start + 2.005333)  # keeps the audio before it
        assert p2.media == s[4].start == pytest.approx(17.0)
        assert p2.wall + p2.duration == pytest.approx(5000.0 + s[-1].end)
        assert p2.wall - (p1.wall + p1.duration) == pytest.approx(10.984 - 2.005333)  # uncovered


def test_only_real_holes_cut():
    # Segments a frame apart, the last one short; an import's 30 ms join gaps; a short one
    # in between; an extraction made with an earlier, longer setting: no hole in any.
    for durations in ([2.005333, 1.984] * 50 + [0.4], [2.04] * 30, [4.005333] * 30,
                      [2.005333] * 5 + [1.5] + [2.005333] * 5):
        assert len(build_pieces(segs_of(durations), [], 1000.0, 2.0)) == 1
    # A file that is mostly hole: its median segment is one packet; the setting still knows.
    mostly_hole = [2.005333] * 10 + [62.0] + [0.021333] * 30 + [2.005333] * 10
    assert len(build_pieces(segs_of(mostly_hole), [], 1000.0, 2.0)) == 2


def ff(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def flv(path: Path, seconds: float, gap_after: float | None = None, gap: float = 0.0) -> Path:
    """A recording-like FLV (AAC only); with a timestamp jump of `gap` s after `gap_after` s."""
    a = path.with_suffix(".a.m4a")
    ff("-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={gap_after or seconds}",
       "-c:a", "aac", "-b:a", "96k", str(a))
    if gap_after is None:
        ff("-i", str(a), "-c", "copy", "-f", "flv", str(path))
        return path
    b = path.with_suffix(".b.m4a")
    ff("-f", "lavfi", "-i", f"sine=frequency=660:sample_rate=48000:duration={seconds - gap_after}",
       "-c:a", "aac", "-b:a", "96k", str(b))
    lst = path.with_suffix(".ffconcat")  # the way backfill_writer keeps its join gaps
    lst.write_text(f"ffconcat version 1.0\nfile '{a}'\nduration {gap_after + gap}\nfile '{b}'\n")
    ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-f", "flv", str(path))
    return path


def recording(path: Path, fid: str = "r") -> Recording:
    s = path.stat()
    return Recording(id=fid, relpath=f"c/{path.name}", path=path, channel="c", name=path.name, title="",
                     size=s.st_size, mtime=s.st_mtime, name_time=None, name_precision=None)


def test_ffmpeg_keeps_counting_media_time_through_a_timestamp_jump(tmp_path):
    """What build_pieces relies on, with the real muxer: playlist media time = timestamps."""
    src = flv(tmp_path / "jump.flv", 14.0, gap_after=7.0, gap=10.0)
    end = 1_800_000_000.0
    os.utime(src, (end, end))
    ex = Extractor(replace(config.load(), data_dir=tmp_path / "data"))
    st = FileState(rec=recording(src), out_dir=ex.hls_root / "r")
    asyncio.run(ex._extract(st, follow=False))
    assert st.status == "done", st.error
    pieces, s, provisional = ex.pieces(st)
    assert not provisional and s[-1].end == pytest.approx(24.0, abs=0.05)  # 7 s + 10 s + 7 s
    p1, p2 = pieces
    lag = end - s[-1].end  # end lag: the file's last write is its last sample
    assert p1.wall - p1.media == pytest.approx(lag, abs=1e-6) and p2.wall - p2.media == pytest.approx(lag, abs=1e-6)
    assert p2.media == pytest.approx(17.0, abs=0.03)  # the audio after the jump, where its timestamps put it
    assert 7.0 <= p1.duration <= 9.1  # the jump's segment keeps its audio; most of the hole stays out
    assert p2.wall - (p1.wall + p1.duration) >= 8.0


def fake_tail(out_dir: Path, lag: float, n: int = 10, tailed: bool | None = True, done: bool = False) -> None:
    """What an earlier, interrupted extraction left: segments written as their audio arrived
    (lag), its run file (None: from before run files), maybe its done.json."""
    out_dir.mkdir(parents=True)
    s = segs(n)
    (out_dir / "index.m3u8").write_text("#EXTM3U\n" + "".join(f"#EXTINF:2.0,\n{x.name}\n" for x in s))
    for x in s:
        (out_dir / x.name).write_bytes(b"")
        os.utime(out_dir / x.name, (lag + x.end, lag + x.end))
    if tailed is not None:
        (out_dir / RUN_FILE).write_text(json.dumps({"tailed": tailed, "seed_lags": []}))
    if done:
        (out_dir / "done.json").write_text(json.dumps({"live_lags": [[i, lag] for i in range(1, n)], "end_lag": 0}))


@pytest.mark.parametrize("earlier", ["tail", "v0.2 tail", "v0.2 done"])
def test_re_extraction_keeps_the_lags_of_the_earlier_live_tail(tmp_path, earlier):
    """A restart mid-stream, the stream over by the time we're back: the file is extracted
    again at full speed, so its new segments say nothing about arrival. The tail before the
    restart did (or the done.json of a finished one, when the recorder appends to its file),
    and keeps saying it through later re-extractions."""
    src = flv(tmp_path / "rec.flv", 20.0)
    L = 1_800_000_000.0
    os.utime(src, (L + 60, L + 60))  # last write a minute after the audio ends: end lag L + 40
    cfg = replace(config.load(), data_dir=tmp_path / "data")
    ex = Extractor(cfg)
    st = FileState(rec=recording(src), out_dir=ex.hls_root / "r")
    fake_tail(st.out_dir, L, tailed=True if earlier == "tail" else None, done=earlier == "v0.2 done")
    if earlier == "v0.2 done":
        for x in segs(10):  # its lags only in done.json
            os.utime(st.out_dir / x.name, (L, L))
    asyncio.run(ex._extract(st, follow=False))
    assert st.status == "done", st.error
    # Seeds sit apart from live_lags, which an older server (a rollback) reads per segment.
    assert st.meta["live_lags"] == [] and st.meta["seed_lags"] == [L]
    assert st.meta["end_lag"] == pytest.approx(L + 40, abs=0.1)
    assert json.loads((st.out_dir / RUN_FILE).read_text()) == {"tailed": False, "seed_lags": [L]}
    assert [p.wall for p in ex.pieces(st)[0]] == [L]
    # Another restart, then another redo (the recorder appending, say): only done.json is left.
    ex2 = Extractor(cfg)
    st2 = FileState(rec=recording(src), out_dir=ex2.hls_root / "r")
    asyncio.run(ex2._extract(st2, follow=False))
    assert st2.status == "done" and [p.wall for p in ex2.pieces(st2)[0]] == [L]


def test_a_whole_file_runs_mtimes_never_become_lags(tmp_path):
    """An interrupted whole-file extraction's segment mtimes are just when we wrote them,
    not when the audio arrived: one NFS stall among them looks like a lag (an hour late
    here). The end lag maps the redo."""
    src = flv(tmp_path / "rec.flv", 20.0)
    L = 1_800_000_000.0
    os.utime(src, (L + 20, L + 20))
    ex = Extractor(replace(config.load(), data_dir=tmp_path / "data"))
    st = FileState(rec=recording(src), out_dir=ex.hls_root / "r")
    fake_tail(st.out_dir, 0.0, tailed=False)
    for i, x in enumerate(segs(10)):  # written in one burst, but for a 5 s stall
        t = L + 3600 + (5.0 if i >= 5 else 0.0)
        os.utime(st.out_dir / x.name, (t, t))
    assert len(live_lags(segs(10), st.out_dir)) == 1
    asyncio.run(ex._extract(st, follow=False))
    assert st.status == "done" and st.meta["live_lags"] == []
    assert ex.pieces(st)[0][0].wall == pytest.approx(L, abs=0.1)


def test_seed_lags_map_a_resumed_live_tail_at_once(tmp_path):
    """Back from a restart mid-stream: until the new tail reaches the live edge its segments
    are catch-up, but the lags from before the restart already place it."""
    ex = Extractor(replace(config.load(), data_dir=tmp_path / "data"))
    st = FileState(rec=rec("r"), out_dir=ex.hls_root / "r", status="running", follow=True)
    fake_tail(st.out_dir, 0.0)
    for x in segs(10):  # catch-up: written in one burst
        os.utime(st.out_dir / x.name, (1_800_000_000.0, 1_800_000_000.0))
    assert ex.pieces(st)[2]  # nothing to go by: provisional
    st.seed_lags, st._lag_cache = [5000.0, 5000.4, 5001.0], None
    pieces, _, provisional = ex.pieces(st)
    assert not provisional and [p.wall for p in pieces] == [5000.0]


def test_live_lags_ignores_catch_up_burst(tmp_path):
    s = segs(30)
    t0 = 1_800_000_000.0
    lag = t0 - 40.0  # recording started 40 s before extraction began
    for i, seg in enumerate(s):
        (tmp_path / seg.name).write_bytes(b"")
        # first 20 segments (40 s of backlog) written in a 0.5 s burst, then real time
        w = t0 + i * 0.025 if i < 20 else lag + seg.end
        os.utime(tmp_path / seg.name, (w, w))
    out = live_lags(s, tmp_path)
    assert out, "expected real-time segments"
    assert all(i >= 20 for i, _ in out)
    assert all(abs(v - lag) < 1e-3 for _, v in out)


def rec(fid):
    return Recording(id=fid, relpath=f"c/{fid}.flv", path=Path(f"/nowhere/{fid}.flv"), channel="c", name=f"{fid}.flv",
                     title="", size=1, mtime=0, name_time=None, name_precision=None)


def test_cache_follows_recordings_but_survives_an_unreachable_nas(tmp_path):
    ex = Extractor(replace(config.load(), data_dir=tmp_path))
    for fid in ("a", "b"):
        (ex.hls_root / fid).mkdir(parents=True)
    (ex.hls_root / "orphan").mkdir()
    ex.update([rec("a"), rec("b")])
    assert not (ex.hls_root / "orphan").exists()  # deleted while the server was down
    ex.update([])  # NAS offline: scan sees nothing
    assert (ex.hls_root / "a").exists() and (ex.hls_root / "b").exists()
    ex.update([rec("a"), rec("b")])
    ex.update([rec("a")])  # janitor removed b
    assert (ex.hls_root / "a").exists() and not (ex.hls_root / "b").exists()


def test_an_import_does_not_end_the_live_recording(tmp_path):
    """Only a newer *recording* means the recorder reconnected; a backfilled replay whose
    name sorts later must not finalise the file being followed."""
    import time as _t

    ex = Extractor(replace(config.load(), data_dir=tmp_path))
    now = _t.time()
    live = replace(rec("live"), mtime=now - 10, name_time=now - 3600)  # idle 10 s, still growing
    imp = replace(rec("imp"), mtime=now - 7200, name_time=now - 60, source="import")
    ex.update([live, imp])
    ex.files["live"].status, ex.files["live"].follow = "running", True
    ex.files["imp"].status = "done"
    ex.tick()
    assert not ex.files["live"].stop_follow
    newer = replace(rec("next"), mtime=now, name_time=now - 5)  # the recorder's next file
    ex.update([live, imp, newer])
    ex.files["next"].status = "done"
    ex.tick()
    assert ex.files["live"].stop_follow


def test_a_job_that_cannot_start_ends_in_error_not_stuck_queued(tmp_path, monkeypatch):
    """ensure() skips queued/running files, so a job that dies before ffmpeg runs (can't
    spawn it, disk trouble) must say so, or the file never gets audio until a restart."""
    import asyncio

    from fone import extractor as ex_mod

    async def no_ffmpeg(*a, **k):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(ex_mod.asyncio, "create_subprocess_exec", no_ffmpeg)
    ex = Extractor(replace(config.load(), data_dir=tmp_path))
    ex.update([rec("a")])
    st = ex.files["a"]
    st.out_dir.mkdir(parents=True)
    (st.out_dir / "ads.json.tmp").write_text("{}")  # another writer's file: no reason to fail either

    async def run():
        ex.ensure(st)
        await asyncio.wait(ex._tasks, timeout=5)

    asyncio.run(run())
    assert st.status == "error" and "Too many open files" in st.error and st.failed_at > 0


def test_ensure_from_a_worker_thread_hands_the_job_to_the_loop(tmp_path, monkeypatch):
    """FastAPI runs the sync /api/timeline route on a worker thread; asking for an old
    recording there used to raise "no running event loop" (HTTP 500) after marking the
    file "queued", so it never got audio until a restart."""
    import asyncio

    from fone import extractor as ex_mod

    started = []

    async def fake_run(self, st):
        started.append(st.rec.id)
        st.status = "done"

    monkeypatch.setattr(ex_mod.Extractor, "_run", fake_run)
    ex = Extractor(replace(config.load(), data_dir=tmp_path))
    ex.update([rec("old")])
    st = ex.files["old"]

    async def run():
        ex.tick()  # the scanner: the extractor learns the server's loop (too old to queue)
        await asyncio.to_thread(ex.ensure, st)
        for _ in range(100):
            if st.status == "done":
                break
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert started and started[-1] == "old" and st.status == "done"
