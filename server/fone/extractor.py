"""Audio extraction (FLV -> audio-only HLS) and wall-clock mapping per recording.

Each recording gets `data/hls/<id>/index.m3u8` + `seg_NNNNN.ts`, produced by
`ffmpeg -c:a copy` (no re-encode). A file the recorder is still writing is
tailed by us and piped into ffmpeg's stdin, so commentary is playable seconds
after it was recorded; closing stdin when the recording ends lets ffmpeg
finalise normally (signals can't: with `-follow` ffmpeg ignores the first
SIGINT while blocked on reads, and a second one aborts the playlist write).

Wall-clock mapping: we need "this audio was recorded at real time W". Within a file
that is W = media time + one lag (see build_pieces). Two sources for the lag:
  * live lag  - while tailing at the live edge, each segment is finalised the
                moment its audio arrives, so (segment mtime - media end) is the
                recording's lag plus however late the CDN delivered it.
  * end lag   - for files we only saw after the fact: source mtime (last write)
                minus total duration. Accurate at the end, exact if no stalls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .recordings import Recording

log = logging.getLogger("fone.extractor")

# Containers ffmpeg can demux from a non-seekable pipe while they're still being written.
PIPEABLE = {".flv": "flv", ".ts": "mpegts"}
TAIL_POLL_SECONDS = 0.1
# Lag is (arrival time - media time). Audio can arrive late (network jitter, CDN
# bursts) but never early, so the lag is the low end of what we observed.
LAG_QUANTILE = 0.02  # lag = 2nd percentile of observed arrival lags
# A segment this much longer than the file's usual one has a hole in its audio (see
# build_pieces). Segment lengths otherwise vary by one audio frame (~21 ms).
MEDIA_GAP_SECONDS = 1.0
# Written before ffmpeg starts: whether this run tails the recording live (only then are
# its segment mtimes arrival times) and the lags earlier runs saw. A re-extraction reads it.
RUN_FILE = "run.json"


@dataclass
class Segment:
    name: str
    start: float  # file-local media time
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Piece:
    """A run of audio with constant lag: wall time = media time + lag."""

    wall: float  # wall-clock (unix seconds) of the first sample
    media: float  # file-local media time of the first sample
    duration: float


@dataclass
class FileState:
    rec: Recording
    out_dir: Path
    status: str = "idle"  # idle | queued | running | done | error
    follow: bool = False
    proc: asyncio.subprocess.Process | None = None
    stop_follow: bool = False  # set when the recorder is done with this file
    error: str | None = None
    failed_at: float = 0.0
    meta: dict | None = None  # contents of done.json once complete
    seed_lags: list[float] = field(default_factory=list)  # arrival lags seen before a re-extraction
    _pl_cache: tuple[float, list[Segment], bool] | None = field(default=None, repr=False)
    _lag_cache: tuple[float, list[tuple[int, float]]] | None = field(default=None, repr=False)

    @property
    def playlist(self) -> Path:
        return self.out_dir / "index.m3u8"

    def segments(self) -> tuple[list[Segment], bool]:
        """Parse our ffmpeg playlist. Returns (segments, ended)."""
        try:
            mtime = self.playlist.stat().st_mtime
        except OSError:
            return [], False
        if self._pl_cache and self._pl_cache[0] == mtime:
            return self._pl_cache[1], self._pl_cache[2]
        segs: list[Segment] = []
        ended = False
        pos = 0.0
        dur: float | None = None
        for line in self.playlist.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#EXTINF:"):
                dur = float(line[8:].split(",", 1)[0])
            elif line == "#EXT-X-ENDLIST":
                ended = True
            elif line and not line.startswith("#") and dur is not None:
                segs.append(Segment(line, pos, dur))
                pos += dur
                dur = None
        self._pl_cache = (mtime, segs, ended)
        return segs, ended


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def live_lags(segs: list[Segment], seg_dir: Path) -> list[tuple[int, float]]:
    """(segment index, lag) for segments produced in real time (i.e. at the live edge).

    During catch-up ffmpeg writes segments far faster than real time; those are
    skipped because their mtimes say nothing about when the audio was recorded.
    """
    out: list[tuple[int, float]] = []
    prev_w: float | None = None
    for i, s in enumerate(segs):
        try:
            w = (seg_dir / s.name).stat().st_mtime
        except OSError:
            prev_w = None
            continue
        if prev_w is not None and (w - prev_w) >= 0.6 * s.duration:
            out.append((i, w - s.end))
        prev_w = w
    return out


def build_pieces(segs: list[Segment], lags: list[tuple[int, float]], fallback_lag: float | None,
                 segment_seconds: float = 0.0) -> list[Piece]:
    """wall = media + one lag for the whole file, in pieces that leave out holes in the audio.

    Within a recording the media clock is the streamer's encoder clock, so the true
    mapping is linear. What varies is delivery: Bilibili's CDN latency see-saws by a few
    seconds for minutes at a time, so arrival lags (segment mtime - media end) wobble
    while the content timeline doesn't. Content can arrive late but never early, so the
    fastest observed delivery (a low percentile, robust to a stray sample) is the lag.
    Splitting on the wobble made the commentary jump ±3 s at every split.

    A hole in the timestamps doesn't move the lag either: ffmpeg's HLS muxer folds a
    timestamp jump into the duration of the segment it falls in, so media time keeps
    counting through the hole and wall = media + lag holds on both sides of it. That
    segment's audio still ends within one usual segment length of its start (ffmpeg cuts
    there), so the piece ends there and the next starts after the hole. No piece claims
    wall time without audio: the timeline can fill it from an import, backfill sees it
    missing. 录播姬's recordings have no such holes (it rewrites timestamp jumps away).
    The usual length is the configured one, or the file's median if that's longer (an
    extraction made with an earlier setting).
    """
    if not segs:
        return []
    if lags:
        values = sorted(lag for _, lag in lags)
        lag = values[min(len(values) - 1, int(len(values) * LAG_QUANTILE))]
    elif fallback_lag is not None:
        lag = fallback_lag
    else:
        return []
    usual = max(segment_seconds, statistics.median(s.duration for s in segs))
    pieces: list[Piece] = []
    start = segs[0].start
    for s, nxt in zip(segs, segs[1:]):
        if s.duration > usual + MEDIA_GAP_SECONDS:
            pieces.append(Piece(wall=start + lag, media=start, duration=s.start + usual - start))
            start = nxt.start
    pieces.append(Piece(wall=start + lag, media=start, duration=segs[-1].end - start))
    return pieces


class Extractor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.hls_root = cfg.data_dir / "hls"
        self.hls_root.mkdir(parents=True, exist_ok=True)
        self.files: dict[str, FileState] = {}
        self._sem = asyncio.Semaphore(cfg.max_parallel_extractions)
        self._tasks: set[asyncio.Task] = set()
        self._swept_at = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None  # the server's loop (seen by tick)

    # ---- bookkeeping ---------------------------------------------------

    def update(self, recs: list[Recording]) -> None:
        seen = set()
        for rec in recs:
            seen.add(rec.id)
            st = self.files.get(rec.id)
            if st is None:
                st = FileState(rec=rec, out_dir=self.hls_root / rec.id)
                st.meta = self._load_meta(st)
                if st.meta:
                    st.status = "done"
                self.files[rec.id] = st
            else:
                st.rec = rec
                # Recorder kept writing after we finalised (or resumed the file): redo it.
                if st.status == "done" and st.meta and st.meta["source_size"] != rec.size and self.is_growing(rec):
                    st.status, st.meta = "idle", None
        # A recording that vanished (the 3-week janitor, or you deleted it) takes its
        # extracted audio with it. An entirely empty scan means the NAS is unreachable,
        # not that everything was deleted, so keep the cache then.
        for gone in set(self.files) - seen:
            st = self.files.pop(gone)
            self._stop(st)
            if seen:
                shutil.rmtree(st.out_dir, ignore_errors=True)
        if seen and time.time() - self._swept_at > 600:
            self._swept_at = time.time()
            for d in self.hls_root.iterdir():  # leftovers from files deleted while we were down
                if d.is_dir() and d.name not in self.files:
                    shutil.rmtree(d, ignore_errors=True)

    def _load_meta(self, st: FileState) -> dict | None:
        try:
            meta = json.loads((st.out_dir / "done.json").read_text())
        except (OSError, ValueError):
            return None
        # Only a size change means new audio; mtime alone may be touched by other tools.
        return meta if meta.get("source_size") == st.rec.size else None

    def is_growing(self, rec: Recording, now: float | None = None) -> bool:
        return ((now or time.time()) - rec.mtime) < self.cfg.growing_idle_seconds

    def tick(self) -> None:
        """Called after every scan: start/finish extractions as needed."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass  # a test driving it by hand
        now = time.time()
        newest_by_channel: dict[str, float] = {}
        for st in self.files.values():
            if st.rec.source != "recorder":
                continue  # an imported replay never means the recorder reconnected
            t = st.rec.name_time or st.rec.mtime
            newest_by_channel[st.rec.channel] = max(newest_by_channel.get(st.rec.channel, 0), t)

        for st in self.files.values():
            rec = st.rec
            growing = self.is_growing(rec, now)
            if st.status == "running" and st.follow and not st.stop_follow:
                idle = now - rec.mtime
                superseded = (rec.name_time or rec.mtime) < newest_by_channel.get(rec.channel, 0)
                if (superseded and idle >= self.cfg.reconnect_idle_seconds) or not growing:
                    log.info("finalising %s (idle %.0fs, superseded=%s)", rec.relpath, idle, superseded)
                    st.stop_follow = True
            elif st.status in ("idle", "error") and growing:
                self.ensure(st)
            elif st.status == "idle" and (now - rec.mtime) < self.cfg.eager_days * 86400:
                self.ensure(st)

    def ensure(self, st: FileState) -> None:
        if st.status in ("queued", "running", "done"):
            return
        if st.status == "error" and time.time() - st.failed_at < 60:
            return  # back off after a failure
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # A sync route (/api/timeline asking for an old recording) runs on a worker
            # thread, where create_task fails, and the file would sit "queued" for good:
            # hand the job to the server's loop instead.
            if self._loop is None:
                raise
            st.status = "queued"
            self._loop.call_soon_threadsafe(self._start, st)
            return
        st.status = "queued"
        self._start(st)

    def _start(self, st: FileState) -> None:
        task = asyncio.create_task(self._run(st))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- the ffmpeg job --------------------------------------------------

    async def _run(self, st: FileState) -> None:
        try:
            follow = self.is_growing(st.rec)
            if follow:
                await self._extract(st, follow=True)
            else:
                async with self._sem:
                    await self._extract(st, follow=False)
        except Exception as e:
            # Can't spawn ffmpeg, disk trouble, ...: never leave the file "queued" or
            # "running" with nothing working on it (ensure() would skip it for good).
            log.exception("extract %s", st.rec.relpath)
            st.proc = None
            st.status, st.failed_at, st.error = "error", time.time(), f"{type(e).__name__}: {e}"[-500:]

    async def _extract(self, st: FileState, follow: bool) -> None:
        rec = st.rec
        # Re-extracting a file we were already tailing (e.g. after a restart) rewrites the
        # segments and their mtimes; keep the arrival lags they recorded.
        st.seed_lags = self._earlier_lags(st)
        shutil.rmtree(st.out_dir, ignore_errors=True)
        # exist_ok: the ad detector writes its results here from a worker thread, and a file
        # it adds while rmtree runs leaves the directory standing.
        st.out_dir.mkdir(parents=True, exist_ok=True)
        st.status, st.follow, st.stop_follow, st.error, st._pl_cache = "running", follow, False, None, None
        st._lag_cache = None
        follow = follow and rec.path.suffix.lower() in PIPEABLE
        _write_json(st.out_dir / RUN_FILE, {"tailed": follow, "seed_lags": st.seed_lags})
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-probesize", "10M", "-analyzeduration", "10M"]
        if follow:
            cmd += ["-f", PIPEABLE[rec.path.suffix.lower()], "-i", "pipe:0"]
        else:
            cmd += ["-nostdin", "-i", f"file:{rec.path}"]
        cmd += [
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "copy",
            "-f", "hls",
            "-hls_time", str(self.cfg.segment_seconds),
            "-hls_list_size", "0",
            "-hls_playlist_type", "event",
            "-hls_flags", "temp_file+independent_segments",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(st.out_dir / "seg_%05d.ts"),
            str(st.playlist),
        ]
        log.info("extract %s (follow=%s)", rec.relpath, follow)
        started = time.time()
        st.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if follow else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # Not communicate(): it closes stdin straight away, which would end the tail.
        pump = asyncio.create_task(self._tail(st, st.proc)) if follow else None
        stderr_task = asyncio.create_task(st.proc.stderr.read())
        await st.proc.wait()
        stderr = await stderr_task
        if pump:
            pump.cancel()
        code = st.proc.returncode
        st.proc = None
        segs, ended = st.segments()
        if code == 0 and segs and ended:
            st.meta = self._finish(st, segs)
            st.status = "done"
            log.info("done %s: %.0fs audio in %.1fs", rec.relpath, st.meta["duration"], time.time() - started)
        else:
            st.status = "error"
            st.failed_at = time.time()
            st.error = (stderr or b"").decode(errors="replace").strip()[-500:] or f"ffmpeg exit {code}"
            log.warning("extract failed %s: %s", rec.relpath, st.error)

    async def _tail(self, st: FileState, proc: asyncio.subprocess.Process) -> None:
        """`tail -f` the recording into ffmpeg until told the recorder is done with it."""
        loop = asyncio.get_running_loop()
        try:
            with open(st.rec.path, "rb") as f:
                while True:
                    chunk = await loop.run_in_executor(None, f.read, 1 << 18)
                    if chunk:
                        proc.stdin.write(chunk)
                        await proc.stdin.drain()
                    elif st.stop_follow:
                        break  # drained everything the recorder wrote
                    else:
                        await asyncio.sleep(TAIL_POLL_SECONDS)
        except (OSError, ConnectionResetError, BrokenPipeError) as e:
            log.warning("tail %s stopped: %s", st.rec.relpath, e)
        finally:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()

    def _finish(self, st: FileState, segs: list[Segment]) -> dict:
        rec = st.rec
        try:
            src = rec.path.stat()
            size, mtime = src.st_size, src.st_mtime
        except OSError:
            size, mtime = rec.size, rec.mtime
        meta = {
            "source_size": size,
            "source_mtime": mtime,
            "duration": segs[-1].end,
            "segments": len(segs),
            # Segment mtimes only mean "arrival time" if we tailed the file live; a
            # whole-file extraction that hiccups on NFS would otherwise fake a lag.
            "live_lags": live_lags(segs, st.out_dir) if st.follow else [],
            # Lags earlier runs saw (before a restart), kept apart: an older server reads
            # live_lags as one sample per segment and would pile these onto segment 0.
            "seed_lags": st.seed_lags,
            "end_lag": self._end_lag(rec, mtime, segs[-1].end),
        }
        _write_json(st.out_dir / "done.json", meta)
        return meta

    def _earlier_lags(self, st: FileState) -> list[float]:
        """Arrival lags earlier extractions of this file saw, from memory and from the
        directory we're about to wipe (all that's left of them after a restart)."""
        lags = set(st.seed_lags)
        try:
            done = _read_json(st.out_dir / "done.json")
            if done is not None:  # a finished run: its lags and the seeds it carried
                lags.update(float(v) for _, v in done.get("live_lags", []))
                lags.update(float(v) for v in done.get("seed_lags", []))
                return sorted(lags)
            run = _read_json(st.out_dir / RUN_FILE)
            if run is not None:
                lags.update(float(v) for v in run.get("seed_lags", []))
            # An interrupted run: its mtimes are arrival times only if it was tailing. One from
            # before run files (v0.2) is trusted: a whole-file run takes a minute or two, so
            # what a restart cut off there was a live tail.
            if run is None or run.get("tailed"):
                old_segs, _ = FileState(rec=st.rec, out_dir=st.out_dir).segments()
                lags.update(lag for _, lag in live_lags(old_segs, st.out_dir))
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("unreadable earlier extraction of %s", st.rec.relpath)
        return sorted(lags)

    def _end_lag(self, rec: Recording, mtime: float, duration: float) -> float:
        lag = mtime - duration
        if rec.name_time is not None and rec.name_precision is not None:
            lo = rec.name_time - 30
            hi = rec.name_time + rec.name_precision + 30
            if not lo <= lag <= hi:  # mtime was touched later; trust the filename instead
                lag = rec.name_time + (rec.name_precision / 2 if rec.name_precision >= 60 else 0)
        return lag

    # ---- mapping ---------------------------------------------------------

    def pieces(self, st: FileState) -> tuple[list[Piece], list[Segment], bool]:
        """Wall-clock pieces for a file, its segments, and whether the mapping is provisional."""
        segs, _ = st.segments()
        if not segs:
            return [], [], True
        seg_s = self.cfg.segment_seconds
        if st.meta:
            lags = [tuple(x) for x in st.meta["live_lags"]] + [(0, v) for v in st.meta.get("seed_lags", [])]
            return build_pieces(segs, lags, st.meta["end_lag"], seg_s), segs, False
        mtime = st.playlist.stat().st_mtime
        if st._lag_cache and st._lag_cache[0] == mtime:
            lags = st._lag_cache[1]
        else:
            lags = live_lags(segs, st.out_dir) + [(0, v) for v in st.seed_lags]
            st._lag_cache = (mtime, lags)
        # Need a few real-time segments before the live lag is trustworthy.
        if len(lags) >= 3:
            return build_pieces(segs, lags, None, seg_s), segs, False
        rec = st.rec
        guess = rec.name_time + (rec.name_precision / 2 if (rec.name_precision or 0) >= 60 else 0) \
            if rec.name_time is not None else rec.mtime - segs[-1].end
        return build_pieces(segs, [], guess, seg_s), segs, True

    def _stop(self, st: FileState) -> None:
        if st.proc and st.proc.returncode is None:
            if st.follow:
                st.stop_follow = True
            else:
                st.proc.terminate()

    async def shutdown(self) -> None:
        for st in self.files.values():
            self._stop(st)
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=5)
