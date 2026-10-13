"""Sponsor-reel detection per recording (S2).

The streamer plays the same pre-recorded sponsor clips (refs in fone/ads/) before and
after sessions; the extension mutes them. For every extracted recording we decode its
audio HLS to 8 kHz mono and run the streaming detector (ads_detect) over it:

  * growing files: each new segment is decoded and fed as soon as the extractor lists
    it, so a clip is known ~2-3 s of its audio after it starts;
  * finished files: the same path in big batches, once. The result is kept in
    <data_dir>/hls/<fid>/ads.json and reused until the refs or the detector change.

Times are file-local media seconds, the timebase of that file's audio HLS (a segment
starts at the running sum of the #EXTINF before it). Each batch of segments is decoded
on its own and padded/trimmed to its playlist duration, so errors never accumulate.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException

from .ads_detect import DEFAULT, SR, AdDetector, RefSet, read_refs, read_wav8
from .config import Config
from .extractor import Extractor, FileState, Segment

log = logging.getLogger("fone.ads")

REFS_DIR = Path(__file__).with_name("ads")
DETECTOR_VERSION = 1  # bump when detection changes in a way Params don't show
POLL_SECONDS = 0.5
BATCH_SEGMENTS = 150  # ~5 min of audio per decode while catching up
# A clip is recognised once ~1-2 s of it has been matched (up to ~3 s under loud
# talk-over). On a growing file, every clip that started before (matched edge - this)
# is therefore already in the spans.
RECOGNISE_S = 3.0
# aresample async: pad timestamp gaps with silence so sample index == media time.
DECODE = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "mpegts", "-i", "pipe:0",
          "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SR), "-af", "aresample=async=1:first_pts=0",
          "-f", "s16le", "pipe:1"]


def load_refs(d: Path | None = None) -> tuple[RefSet, str]:
    """The reference clips in d (fone/ads/) -> (fingerprints, version). refs.npz holds
    them as fingerprints (build_refs.py makes it; the clips' audio stays with whoever
    recorded it). Without one, the 8 kHz mono WAVs in d are fingerprinted on the fly,
    named after their files. The version follows the fingerprints and the detector
    settings, so stored results go stale when either changes."""
    d = REFS_DIR if d is None else d
    if (d / "refs.npz").is_file():
        refs = read_refs(d / "refs.npz", DEFAULT)
    else:
        refs = RefSet(DEFAULT)
        for w in sorted(d.glob("*.wav")):
            refs.add(w.stem, read_wav8(w))
    h = hashlib.sha1(f"{DETECTOR_VERSION} {DEFAULT} {refs.digest()}".encode())
    return refs, h.hexdigest()[:12]


def decode(paths: list[Path]) -> np.ndarray:
    """Consecutive HLS segments -> 8 kHz mono float32. One decoder over the concatenated
    bytes (MPEG-TS concatenates cleanly), so only the batch's first AAC frame lacks its
    predecessor."""
    data = b"".join(p.read_bytes() for p in paths)
    r = subprocess.run(DECODE, input=data, capture_output=True, timeout=300)
    if r.returncode:
        err = r.stderr.decode(errors="replace").strip()[-300:]
        log.warning("decode %s..%s failed: %s", paths[0].name, paths[-1].name, err)
    n = len(r.stdout) // 2
    return np.frombuffer(r.stdout[:2 * n], np.int16).astype(np.float32) / 32768


def fit(x: np.ndarray, n: int) -> np.ndarray:
    """Pad/trim decoded audio to its playlist duration so sample index stays media time."""
    if abs(len(x) - n) > SR // 4:
        log.warning("decoded %d samples where the playlist says %d", len(x), n)
    return x[:n] if len(x) >= n else np.concatenate([x, np.zeros(n - len(x), np.float32)])


def _epoch(out_dir: Path, segs: list[Segment]) -> tuple[int, int] | None:
    """Identity of an extraction run: the extractor wipes and rewrites a file's directory
    when it redoes it, which gives the first segment a new inode / mtime."""
    if not segs:
        return None
    try:
        s = (out_dir / segs[0].name).stat()
    except OSError:
        return None
    return s.st_ino, s.st_mtime_ns


@dataclass
class _Track:
    name: str
    out_dir: Path
    det: AdDetector | None = None
    epoch: tuple[int, int] | None = None
    n_segs: int = 0  # segments fed
    fed: int = 0  # samples fed (= playlist duration of those segments)
    final: bool = False
    spans: list[dict] = field(default_factory=list)
    analyzed_to: float = 0.0
    retry_at: float = 0.0


class AdService:
    def __init__(self, cfg: Config, extractor: Extractor):
        self.cfg, self.extractor = cfg, extractor
        self.refs: RefSet | None = None
        self.version = ""
        self._tracks: dict[str, _Track] = {}
        self._wanted: dict[str, float] = {}  # fid -> last API request (analyse those first)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.refs, self.version = await asyncio.to_thread(load_refs)
        log.info("ads: %d reference clips (version %s)", len(self.refs.refs), self.version)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ---- queries ---------------------------------------------------------

    def spans(self, fid: str) -> list[dict]:
        """[{"start", "end", "name"}] in file-local media seconds, by start."""
        tr = self._tracks.get(fid)
        return list(tr.spans) if tr else []

    def analyzed_to(self, fid: str) -> float:
        """Media time before which every clip start is known (file duration once final)."""
        tr = self._tracks.get(fid)
        return tr.analyzed_to if tr else 0.0

    def state(self, fid: str) -> dict | None:
        if fid not in self.extractor.files:
            return None
        self._wanted[fid] = time.time()
        return {"spans": self.spans(fid), "analyzedTo": self.analyzed_to(fid)}

    # ---- the work loop ---------------------------------------------------

    async def _loop(self) -> None:
        while True:
            busy = False
            try:
                busy = await self._tick()
            except Exception:
                log.exception("ad detection failed")
            await asyncio.sleep(0 if busy else POLL_SECONDS)

    async def _tick(self) -> bool:
        """One batch for every live file, then one for the most wanted finished file.
        Returns True while there is backlog left."""
        files = self.extractor.files
        for gone in set(self._tracks) - set(files):
            del self._tracks[gone]
            self._wanted.pop(gone, None)
        now = time.time()
        live, backlog = [], []
        for fid, st in list(files.items()):
            segs, ended = st.segments()
            tr = self._tracks.get(fid)
            if tr is None:
                tr = self._tracks[fid] = self._open(st, segs)
            if tr.final:
                if _epoch(st.out_dir, segs) == tr.epoch:
                    continue
                tr = self._tracks[fid] = _Track(st.rec.relpath, st.out_dir)  # being re-extracted
            finished = ended and st.status == "done"
            if not segs or now < tr.retry_at or (len(segs) == tr.n_segs and not finished):
                continue
            job = (fid, tr, segs, finished)
            (live if st.follow and st.status == "running" else backlog).append(job)
        for job in live:
            await self._run(*job)
        if backlog:
            def rank(job):
                rec = files[job[0]].rec if job[0] in files else None
                return self._wanted.get(job[0], 0.0), (rec.name_time or rec.mtime) if rec else 0.0
            await self._run(*max(backlog, key=rank))
        return bool(backlog) or any(len(segs) > tr.n_segs for _, tr, segs, _ in live)

    def _open(self, st: FileState, segs: list[Segment]) -> _Track:
        tr = _Track(st.rec.relpath, st.out_dir)
        if st.status != "done" or not segs:
            return tr  # an unfinished extraction is redone from scratch anyway
        try:
            doc = json.loads((st.out_dir / "ads.json").read_text())
        except (OSError, ValueError):
            return tr
        if doc.get("version") == self.version and doc.get("final") and doc.get("segments") == len(segs):
            tr.final, tr.spans, tr.analyzed_to = True, doc["spans"], doc["analyzedTo"]
            tr.epoch = _epoch(st.out_dir, segs)
        return tr

    async def _run(self, fid: str, tr: _Track, segs: list[Segment], finished: bool) -> None:
        try:
            await asyncio.to_thread(self._advance, tr, segs, finished)
        except Exception as e:  # segments vanished mid-read (re-extraction), disk trouble, ...
            log.warning("ads %s: %s", tr.name, e)
            tr.retry_at = time.time() + 5

    def _advance(self, tr: _Track, segs: list[Segment], finished: bool) -> None:
        """Decode and feed the next batch of segments (runs in a worker thread)."""
        epoch = _epoch(tr.out_dir, segs)
        if tr.det is None or len(segs) < tr.n_segs or epoch != tr.epoch:
            tr.det, tr.epoch, tr.n_segs, tr.fed, tr.spans, tr.analyzed_to = AdDetector(self.refs), epoch, 0, 0, [], 0.0
        new = segs[tr.n_segs: tr.n_segs + BATCH_SEGMENTS]
        x = np.zeros(0, np.float32)
        if new:
            x = fit(decode([tr.out_dir / s.name for s in new]), round(new[-1].end * SR) - tr.fed)
        last = finished and tr.n_segs + len(new) == len(segs)
        tr.det.feed(x, final=last)
        tr.n_segs += len(new)
        tr.fed += len(x)
        spans = [{"start": h.start, "end": h.end, "name": h.ref} for h in tr.det.hits]
        changed = spans != tr.spans
        tr.spans = spans
        tr.analyzed_to = round(tr.fed / SR if last else max(0.0, tr.det.matched - RECOGNISE_S), 3)
        if changed or last:
            self._save(tr, segs, last)
        if last:
            log.info("ads %s: %d clip(s) in %.0f s of audio (detector %.1f s cpu)",
                     tr.name, len(spans), tr.fed / SR, tr.det.cpu)
            tr.final, tr.det = True, None

    def _save(self, tr: _Track, segs: list[Segment], final: bool) -> None:
        if _epoch(tr.out_dir, segs) != tr.epoch:
            return  # the extractor started over meanwhile; don't leave stale results there
        doc = {"version": self.version, "final": final, "segments": tr.n_segs, "analyzedTo": tr.analyzed_to,
               "spans": tr.spans, "plays": [asdict(h) for h in tr.det.hits]}
        tmp = tr.out_dir / "ads.json.tmp"
        tmp.write_text(json.dumps(doc, ensure_ascii=False))
        os.replace(tmp, tr.out_dir / "ads.json")


def make_router(svc: AdService) -> APIRouter:
    router = APIRouter(prefix="/api/ads")

    @router.get("/{fid}")
    def ads(fid: str):
        state = svc.state(fid)
        if state is None:
            raise HTTPException(404, "unknown file")
        return state

    return router
