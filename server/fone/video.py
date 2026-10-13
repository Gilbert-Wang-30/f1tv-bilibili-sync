"""A recording's video (with its audio) as HLS, cut from the FLV on demand (S4).

For the post-session handoff: keep watching the streamer's own picture from the exact
moment the commentary had reached. No re-encode and nothing running per viewer:

  * video_flv indexes the FLV once (keyframe DTS + byte offset, codec headers) and
    then incrementally while the recorder is still writing it;
  * index.m3u8 lists the whole file as ~4 s segments cut at keyframes, with exact
    #EXTINF (keyframe DTS deltas). Finished file: VOD + ENDLIST. Growing file: EVENT,
    listing only segments whose closing keyframe is on disk, so every URL is immutable;
  * seg/<a>_<b>.ts = the FLV bytes from keyframe a to keyframe b behind the FLV header
    and sequence headers, piped through `ffmpeg -c copy -copyts -f mpegts` (~0.2 s).
    Timestamps stay the FLV's own, so the player's time v is FLV time minus t0.

Time bases (the KL qualifying file: t0 = 0, a0 = 0.080):
  v  player time of this playlist      = FLV time - t0   (t0: first keyframe DTS)
  m  the file's audio-HLS media time   = FLV time - a0   (a0: first coded audio frame;
     the extractor's playlist starts at the first audio packet it copies)
  => m = v + mOffset, mOffset = t0 - a0.
hls.js anchors its clock on the first fragment it loads (min of its first audio and
video PTS), which lands within one audio frame (~21 ms) of the keyframe's DTS.

Indexes of finished files persist in <data_dir>/video/<fid>.json (re-reading a 3 h
recording from the NAS takes seconds) and are dropped with the recording.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .config import Config
from .extractor import Extractor, FileState
from .video_flv import FlvIndex, Segment, Snapshot

log = logging.getLogger("fone.video")

SEG_TARGET_MS = 4000
# Catching up on this much new data is quick enough to do inside a request; more (a
# server restart in the middle of a live recording) runs in the background while the
# playlist keeps serving what is already indexed.
INLINE_UPDATE_BYTES = 32 << 20
WAIT_SECONDS = 1.0  # a request waits this long for indexing before answering 202
RETRY_AFTER = 2
SAVE_EVERY_BYTES = 256 << 20  # persist a growing file's index this often
MAX_SEGMENT_BYTES = 64 << 20  # one request can't make us remux an hour of video
REMUX_PARALLEL = 3
REMUX_TIMEOUT = 30.0
CACHE_BYTES = 48 << 20  # recently served segments (players re-request around seeks)
SWEEP_SECONDS = 600
SEG_NAME = re.compile(r"^(\d+)_(\d+|end)\.ts$")
REMUX = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-probesize", "1M", "-analyzeduration", "0",
         "-f", "flv", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
         "-copyts", "-mpegts_copyts", "1", "-f", "mpegts", "pipe:1"]
PROBE = ["ffprobe", "-v", "error", "-f", "flv", "-i", "pipe:0", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height", "-of", "json"]


class Indexing(Exception):
    """The index isn't ready yet: answer 202 and let the player retry."""

    def __init__(self, progress: float):
        super().__init__(f"indexing {progress:.0%}")
        self.progress = progress


@dataclass
class _Entry:
    fid: str
    path: Path
    index: FlvIndex
    snap: Snapshot | None = None  # set once the first pass has reached the end of the file
    task: asyncio.Task | None = None  # the one running update (never two on one index)
    restored: bool = False
    probe: dict | None = None  # {"codec","width","height"}
    error: str | None = None
    saved_pos: int = 0
    cuts: tuple[Snapshot | None, bool, list[Segment]] = (None, False, [])  # segments of the last snapshot
    size: int = 0  # file size when the current update started
    indexed_size: int = 0  # ... and when the last finished one started: all of it is indexed,
    # except a tag the recorder never finished (a crash mid-write), which never will be


@dataclass
class _View:
    """Everything a route needs, computed against one snapshot."""

    st: FileState
    entry: _Entry
    snap: Snapshot
    growing: bool
    finished: bool  # not growing and indexed to the last byte
    segments: list[Segment] = field(default_factory=list)


class VideoService:
    def __init__(self, cfg: Config, extractor: Extractor):
        self.cfg = cfg
        self.extractor = extractor
        self.root = cfg.data_dir / "video"
        self._entries: dict[str, _Entry] = {}
        self._sem = asyncio.Semaphore(REMUX_PARALLEL)
        self._cache: OrderedDict[tuple, bytes] = OrderedDict()
        self._cache_bytes = 0
        self._procs: set[asyncio.subprocess.Process] = set()
        self._sweeper: asyncio.Task | None = None

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        tasks = [t for t in [self._sweeper, *(e.task for e in self._entries.values())] if t]
        for t in tasks:
            t.cancel()
        for p in list(self._procs):
            with contextlib.suppress(ProcessLookupError):
                p.kill()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ---- index lifecycle ---------------------------------------------------------

    async def _view(self, fid: str) -> _View:
        st = self.extractor.files.get(fid)
        if st is None:
            raise LookupError("unknown recording")
        path = st.rec.path
        if path.suffix.lower() != ".flv":
            raise LookupError("video is only served from FLV recordings")
        entry = self._entries.get(fid)
        try:
            size = path.stat().st_size
        except OSError as e:
            raise LookupError(f"recording unreadable: {e}") from e
        if entry is None or entry.path != path or (entry.snap and size < entry.snap.pos):
            entry = self._entries[fid] = _Entry(fid, path, FlvIndex(path))  # new, or replaced on disk
        if entry.snap is None or size > entry.indexed_size:
            if entry.task is None or entry.task.done():
                entry.size, entry.error = size, None
                entry.task = asyncio.create_task(self._index(entry))
            if entry.snap is None or size - entry.index.pos <= INLINE_UPDATE_BYTES:
                await asyncio.wait({entry.task}, timeout=WAIT_SECONDS)  # doesn't cancel on timeout
        growing = self.extractor.is_growing(st.rec)
        if entry.snap is None:
            if entry.error and entry.task.done():
                raise LookupError(entry.error)
            raise Indexing(min(1.0, entry.index.scanned / max(1, entry.size)))
        if not entry.snap.kf_ts:
            if growing:
                raise Indexing(1.0)  # live, and the first keyframe isn't on disk yet
            raise LookupError("no video keyframes in this recording")
        idle = entry.task is None or entry.task.done()
        finished = not growing and idle and entry.indexed_size >= size
        view = _View(st, entry, entry.snap, growing, finished)
        snap0, fin0, segs = entry.cuts
        if snap0 is not entry.snap or fin0 != finished:
            segs = entry.snap.segments(SEG_TARGET_MS, finished)
            entry.cuts = (entry.snap, finished, segs)
        view.segments = segs
        return view

    async def _index(self, entry: _Entry) -> None:
        try:
            if not entry.restored:
                entry.restored = True
                await asyncio.to_thread(self._restore, entry)
            t = time.time()
            n = await asyncio.to_thread(entry.index.update)
            snap = entry.index.snapshot()
            if entry.snap is None:
                log.info("indexed %s: %d tags, %d keyframes in %.1fs", entry.path.name, n, len(snap.kf_ts), time.time() - t)
            if entry.index.corrupt_at is not None and (entry.snap is None or entry.snap.pos < entry.index.corrupt_at):
                log.warning("%s: broken FLV tag at byte %d; video stops there", entry.path.name, entry.index.corrupt_at)
            entry.snap = snap
            entry.indexed_size = entry.size
            entry.error = None
            if entry.probe is None and snap.kf_ts:
                entry.probe = await self._probe(entry.path, snap)
            st = self.extractor.files.get(entry.fid)
            done = st is not None and not self.extractor.is_growing(st.rec)
            if (done and snap.pos > entry.saved_pos) or snap.pos - entry.saved_pos > SAVE_EVERY_BYTES:
                await asyncio.to_thread(self._save, entry, snap.pos)
        except (OSError, ValueError) as e:
            # A NAS hiccup keeps serving the last snapshot; a file that never indexed reports it.
            entry.error = str(e)
            log.warning("index %s: %s", entry.path.name, e)
        except Exception as e:
            entry.error = f"indexing failed: {e}"
            log.exception("index %s", entry.path.name)

    def _json_path(self, fid: str) -> Path:
        return self.root / f"{fid}.json"

    def _restore(self, entry: _Entry) -> None:
        try:
            d = json.loads(self._json_path(entry.fid).read_text())
            idx = FlvIndex.from_json(entry.path, d["index"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        if idx.still_matches():
            entry.index, entry.probe, entry.saved_pos = idx, d.get("probe"), idx.pos

    def _save(self, entry: _Entry, pos: int) -> None:
        if entry.index.pos != pos:
            return  # moved on since; the next update saves
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._json_path(entry.fid)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"index": entry.index.to_json(), "probe": entry.probe}))
        os.replace(tmp, path)
        entry.saved_pos = pos

    async def _probe(self, path: Path, snap: Snapshot) -> dict:
        """Codec and picture size of the first GOP (ffprobe on header + headers + GOP)."""
        start = snap.kf_pos[0]
        end = min(snap.kf_pos[1] if len(snap.kf_pos) > 1 else snap.pos, start + (8 << 20))
        data = snap.header + snap.headers_at(start) + await asyncio.to_thread(_read, path, start, end)
        try:
            out = await self._run(PROBE, data, timeout=15)
            s = json.loads(out)["streams"][0]
            return {"codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height")}
        except (RuntimeError, ValueError, KeyError, IndexError, TimeoutError) as e:
            log.warning("probe %s: %s", path.name, e)
            return {"codec": None, "width": None, "height": None}

    async def _run(self, cmd: list[str], data: bytes, timeout: float) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._procs.add(proc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(data), timeout)
        except BaseException:  # timeout, cancelled request, shutdown: never leave ffmpeg behind
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise
        finally:
            self._procs.discard(proc)
        if proc.returncode != 0 or not out:
            raise RuntimeError(err.decode(errors="replace").strip()[-300:] or f"exit {proc.returncode}")
        return out

    # ---- cleanup -------------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        while True:
            try:
                self.sweep()  # on the loop: it touches the entries and their tasks
            except Exception:
                log.exception("video sweep failed")
            await asyncio.sleep(SWEEP_SECONDS)

    def sweep(self) -> None:
        """Forget recordings that are gone (the janitor, or deleted by hand). An empty
        file list means the NAS is unreachable, not that everything was deleted."""
        known = self.extractor.files
        if not known:
            return
        for fid in [f for f in self._entries if f not in known]:
            entry = self._entries.pop(fid)
            if entry.task:
                entry.task.cancel()
        for k in [k for k in self._cache if k[0] not in known]:
            self._cache_bytes -= len(self._cache.pop(k))
        if not self.root.is_dir():
            return
        for p in self.root.iterdir():
            fid = p.name.split(".", 1)[0]
            if fid not in known or p.name.endswith(".tmp") and time.time() - p.stat().st_mtime > 300:
                with contextlib.suppress(OSError):
                    p.unlink()

    # ---- what the routes serve -----------------------------------------------------

    async def info(self, fid: str) -> dict:
        v = await self._view(fid)
        snap, probe = v.snap, v.entry.probe or {}
        t0 = snap.kf_ts[0] / 1000
        a0 = snap.first_audio_ts / 1000 if snap.first_audio_ts is not None else None
        m_offset = round(t0 - a0, 3) if a0 is not None else None
        duration = round(sum(s.duration for s in v.segments), 3)
        edge = duration if v.finished else round((snap.last_ts - snap.kf_ts[0]) / 1000, 3)
        return {
            "id": fid, "name": v.st.rec.name, "growing": v.growing, "t0": t0, "a0": a0, "mOffset": m_offset,
            "duration": duration, "edge": edge, "width": probe.get("width"), "height": probe.get("height"),
            "codec": probe.get("codec"), "wallStart": self._wall_at(v.st, m_offset),
        }

    def _wall_at(self, st: FileState, m: float | None) -> float | None:
        """Wall clock of audio media time m (v = 0 when m = mOffset), from the extractor's pieces."""
        if m is None:
            return None
        try:
            pieces, _, _ = self.extractor.pieces(st)
        except OSError:  # its playlist is being rewritten
            return None
        if not pieces:
            return None
        p = next((p for p in reversed(pieces) if p.media <= m), pieces[0])
        return round(p.wall + (m - p.media), 3)

    async def playlist(self, fid: str, start: float | None = None) -> str:
        v = await self._view(fid)
        if not v.segments:
            raise Indexing(0.0)  # growing, and the first segment hasn't closed yet
        target = max(s.duration for s in v.segments)
        lines = ["#EXTM3U", "#EXT-X-VERSION:6", f"#EXT-X-TARGETDURATION:{max(1, int(target + 0.999))}",
                 "#EXT-X-MEDIA-SEQUENCE:0", f"#EXT-X-PLAYLIST-TYPE:{'VOD' if v.finished else 'EVENT'}",
                 "#EXT-X-INDEPENDENT-SEGMENTS"]
        if start is not None:  # for players that can't be given a start position
            lines.append(f"#EXT-X-START:TIME-OFFSET={start:.3f},PRECISE=YES")
        for s in v.segments:
            lines += [f"#EXTINF:{s.duration:.3f},", f"seg/{s.name}"]
        if v.finished:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    async def segment(self, fid: str, name: str) -> tuple[bytes, bool]:
        """MPEG-TS for seg/<a>_<b>.ts. Returns (body, immutable)."""
        m = SEG_NAME.match(name)
        if not m:
            raise LookupError("bad segment name")
        v = await self._view(fid)
        snap = v.snap
        a = int(m.group(1))
        b = None if m.group(2) == "end" else int(m.group(2))
        if not (0 <= a < len(snap.kf_pos)) or (b is not None and not a < b < len(snap.kf_pos)):
            raise LookupError("no such segment")
        if b is None and not v.finished:
            raise LookupError("the last segment exists once the recording has finished")
        start, end = snap.kf_pos[a], (snap.kf_pos[b] if b is not None else snap.pos)
        if end - start > MAX_SEGMENT_BYTES:
            raise LookupError("segment too long")
        key = (fid, a, b, end)
        if (hit := self._cache.get(key)) is not None:
            self._cache.move_to_end(key)
            return hit, b is not None
        async with self._sem:  # read inside it too: queued requests mustn't each hold up to 64 MB
            body = await asyncio.to_thread(_read, v.entry.path, start, end)
            out = await self._run(REMUX, snap.header + snap.headers_at(start) + body, timeout=REMUX_TIMEOUT)
        self._cache[key] = out
        self._cache_bytes += len(out)
        while self._cache_bytes > CACHE_BYTES and len(self._cache) > 1:
            self._cache_bytes -= len(self._cache.popitem(last=False)[1])
        return out, b is not None


def _read(path: Path, start: int, end: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(end - start)
    if len(data) != end - start:
        raise OSError(f"short read at {start}")
    return data


def _indexing(e: Indexing) -> JSONResponse:
    return JSONResponse({"status": "indexing", "progress": round(e.progress, 3)}, status_code=202,
                        headers={"Retry-After": str(RETRY_AFTER), "Cache-Control": "no-store"})


def make_router(svc: VideoService) -> APIRouter:
    router = APIRouter(prefix="/api/video")

    @router.get("/{fid}/info")
    async def info(fid: str):
        try:
            return JSONResponse(await svc.info(fid), headers={"Cache-Control": "no-store"})
        except Indexing as e:
            return _indexing(e)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e

    @router.get("/{fid}/index.m3u8")
    async def playlist(fid: str, start: float | None = None):
        try:
            text = await svc.playlist(fid, start)
        except Indexing as e:
            return _indexing(e)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        return PlainTextResponse(text, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})

    @router.get("/{fid}/seg/{name}")
    async def segment(fid: str, name: str):
        try:
            body, immutable = await svc.segment(fid, name)
        except Indexing as e:
            return _indexing(e)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except (RuntimeError, OSError, TimeoutError) as e:
            log.warning("segment %s/%s: %s", fid, name, e)
            raise HTTPException(500, f"remux failed: {e}") from e
        cache = "public, max-age=86400, immutable" if immutable else "no-cache"
        return Response(body, media_type="video/mp2t", headers={"Cache-Control": cache})

    return router
