"""Streamer screen delay from the MultiViewer "Track time" clock in the recording.

    delayScreen = file wall time of a frame - F1 feed UTC shown on that frame

The streamer shows MultiViewer's live-timing header on stream; its "Track time"
HH:MM:SS is the feed's UTC in some display zone. File wall time comes from the
extractor's audio mapping (wall = piece.wall + m, m = pts - a0), so the delay is
exactly what the commentary audio lags the real world by, minus reaction time.

Pipeline per recording (see screenclock_media for the frame/OCR side):
  1. one keyframe every `step` s -> OCR the clock -> d_i = wall_i - shown_i, taken
     modulo a day in each candidate display zone (the streamer's local zone and the
     circuit's); the zone giving the delay nearest the prior wins. Without zone
     info, modulo 15 min (all real zones are multiples of 15 min);
  2. a reading counts only if a neighbouring sample shows the clock advancing in
     real time with it (rejects clocks frozen after the session, paused replays,
     OCR junk); the densest 1.2 s cluster of those = inliers. The display shows
     floor(feed time), so the delay lies in (p90 - 1, p10];
  3. sub-second: decode ~1.3 s after some inlier keyframes, time the frame where
     the seconds digit changes (verified by OCR either side): delay = wall(tick) -
     value, +-half a frame. Result = median over ticks.
Finished files are analysed once; growing files incrementally every few minutes
(only new video is decoded). Raw readings are persisted with the result, so a
restart resumes and a changed audio mapping or zone list only re-runs step 2-3's
arithmetic, not the OCR.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import math
import os
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

from .config import Config
from .extractor import Extractor, FileState, Piece
from .screenclock_media import FlvIndex, Reader, Reading, Tick, decode, find_ticks, tesseract_available

log = logging.getLogger("fone.screenclock")

MOD = 900.0  # all real-world UTC offsets are multiples of 15 min
DAY = 86400
RESULT_FILE = "screenclock.json"
OPENF1_SESSIONS = "https://api.openf1.org/v1/sessions"

# (wall_from, wall_to) of the recording -> display zones (seconds east of UTC) the
# clock may be in besides the streamer's own, or None when unknown (lookup failed).
ZoneLookup = Callable[[float, float], Awaitable[list[int] | None]]


@dataclass(frozen=True)
class Settings:
    step: float = 30.0  # seconds of video between sampled keyframes
    locate_every: float = 180.0  # full-frame search at most once per this many seconds while no layout matches
    interval: float = 300.0  # growing files: re-run this often
    poll: float = 20.0
    jobs: int = 4  # parallel OCR threads
    refine_max: int = 12  # tick windows per run
    max_delay: float = 600.0
    extra_zones: tuple[int, ...] = ()  # always-allowed display zones besides the streamer's local one

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            step=float(env("FONE_SCREENCLOCK_STEP", "30")),
            locate_every=float(env("FONE_SCREENCLOCK_LOCATE_EVERY", "180")),
            interval=float(env("FONE_SCREENCLOCK_INTERVAL", "300")),
            jobs=int(env("FONE_SCREENCLOCK_JOBS", str(max(1, min(4, os.cpu_count() or 1))))),
            extra_zones=tuple(parse_offset(x) for x in env("FONE_SCREENCLOCK_TZ", "").split(",") if x.strip()),
        )


def parse_offset(spec: str) -> int:
    """"+08:00" / "-0330" / "08:00:00" -> seconds east of UTC."""
    s = spec.strip()
    sign = -1 if s.startswith("-") else 1
    parts = s.lstrip("+-").replace(":", "")
    if not parts.isdigit() or len(parts) not in (4, 6):
        raise ValueError(f"bad UTC offset {spec!r}")
    return sign * (int(parts[:2]) * 3600 + int(parts[2:4]) * 60)


def format_offset(off: int) -> str:
    return f"{'+' if off >= 0 else '-'}{abs(off) // 3600:02d}:{abs(off) % 3600 // 60:02d}"


# ---- estimation (pure) ------------------------------------------------------------


def delay_of(raw: float, zones: list[int] | None, prior: float) -> float:
    """wall - shown seconds-of-day -> delay. With zones: exact (mod 1 day) in the zone
    nearest the prior, so a MultiViewer replay of another session can't pass as a
    plausible delay. Without: modulo 15 min, allowing the clock to look 2 min ahead."""
    if zones is None:
        d = raw % MOD
        return d - MOD if d > MOD - 120 else d
    return min(((raw + off + DAY / 2) % DAY - DAY / 2 for off in zones), key=lambda x: abs(x - prior))


def clusters(vals: list[tuple[float, float]], width: float = 1.2) -> list[list[tuple[float, float]]]:
    """Greedy densest-window clustering of (delay, pts) pairs, largest first."""
    rest = sorted(vals)
    out = []
    while rest:
        ds = [d for d, _ in rest]
        best, bi = 0, 0
        for i in range(len(ds)):
            j = bisect.bisect_right(ds, ds[i] + width)
            if j - i > best:
                best, bi = j - i, i
        out.append(rest[bi:bi + best])
        rest = rest[:bi] + rest[bi + best:]
    return out


def runs(verified: dict[float, float], tol: float = 1.5) -> list[list[tuple[float, float]]]:
    """Consecutive verified readings with a constant (+-tol) delay: [(pts, delay)]."""
    out: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for p in sorted(verified):
        d = verified[p]
        if cur and abs(d - statistics.median(x for _, x in cur)) > tol:
            out.append(cur)
            cur = []
        cur.append((p, d))
    if cur:
        out.append(cur)
    return out


def _verify(readings: list[Reading], wall_of: Callable[[float], float], zones: list[int] | None,
            prior: float, step: float, max_delay: float) -> tuple[dict[float, float], dict[float, float]]:
    """(plausible, verified) pts -> delay. Plausible: a delay in range. Verified: also a
    neighbour (within 6 steps) shows the clock running in real time with it (same delay
    +-1.05 s); a frozen clock's delay grows by `step` per sample."""
    readable = sorted((r for r in readings if r.tod is not None), key=lambda r: r.pts)
    delays = {}
    for r in readable:
        d = delay_of(wall_of(r.pts) - r.tod, zones, prior)
        if -5 <= d <= max_delay:
            delays[r.pts] = d
    out = {}
    for i, r in enumerate(readable):
        if r.pts not in delays:
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(readable) and abs(readable[j].pts - r.pts) <= 6 * step:
                o = readable[j].pts
                if o in delays and abs(delays[o] - delays[r.pts]) <= 1.05:
                    out[r.pts] = delays[r.pts]
                    break
    return delays, out


def _implied_zone(readings: list[Reading], pts: set[float], wall_of, delay: float) -> int | None:
    offs = [round(((r.tod - (wall_of(r.pts) - delay)) % DAY) / 900) * 900 % DAY
            for r in readings if r.pts in pts and r.tod is not None]
    if not offs:
        return None
    off = statistics.mode(offs)
    return off - DAY if off > DAY / 2 else off


def estimate(readings: list[Reading], ticks: list[Tick], wall_of: Callable[[float], float],
             zones: list[int] | None, prior: float, step: float, latest: bool = False,
             max_delay: float = 600.0) -> dict:
    """Readings (+ tick timings) -> delay, confidence and the evidence behind it.

    `latest`: prefer the most recent steady delay over the dominant one (live use:
    the streamer may have re-synced their player mid-stream)."""
    visible = sum(r.visible for r in readings)
    readable = [r for r in readings if r.tod is not None]
    plausible, verified = _verify(readings, wall_of, zones, prior, step, max_delay)
    cl = clusters([(d, p) for p, d in verified.items()])
    main = cl[0] if cl else []
    ambiguous = len(cl) > 1 and len(cl[1]) >= 0.3 * len(cl[0])
    if ambiguous:  # a second steady delay (long MultiViewer replay?): the prior breaks the tie
        main = min((c for c in cl if len(c) >= 0.3 * len(cl[0])),
                   key=lambda c: abs(statistics.median(d for d, _ in c) - prior))
    segs = runs(verified)
    # Only with zones checked: modulo 15 min, a MultiViewer replay could pose as a re-sync.
    if latest and zones is not None and main and segs and len(segs[-1]) >= 5:
        last_med = statistics.median(d for _, d in segs[-1])
        if abs(last_med - statistics.median(d for d, _ in main)) > 1.2:
            main, ambiguous = [(d, p) for p, d in segs[-1]], False
    inliers = {p for _, p in main}
    out: dict = {
        "delay": None, "method": "none", "uncertainty": None, "confidence": "none", "status": "",
        "n": len(main), "readings": len(readings), "visible": visible, "readable": len(readable),
        "plausible": len(plausible), "verified": len(verified), "ticks": 0, "zone": None, "interval": None,
        "inliers": sorted(inliers),
        "segments": [{"from": s[0][0], "to": s[-1][0], "n": len(s),
                      "delay": round(statistics.median(d for _, d in s), 3)} for s in segs],
    }
    if main:
        ds = sorted(d for d, _ in main)

        def q(f: float) -> float:
            return ds[min(len(ds) - 1, int(f * (len(ds) - 1) + 0.5))]

        lo, hi = q(0.9) - 1.0, q(0.1)  # percentiles, so one edge reading can't pinch it
        if lo > hi:  # spread > 1 s (OCR off-by-one, drift): centre on the median
            lo = hi = statistics.median(ds) - 0.5
        coarse = (lo + hi) / 2
        out["interval"] = [round(lo, 3), round(hi, 3)]
        out["zone"] = _implied_zone(readings, inliers, wall_of, coarse)
        fine = [d for t in ticks if lo - 0.3 <= (d := delay_of(wall_of(t.pts) - t.value, zones, prior)) <= hi + 0.3]
        if len(fine) >= 3:
            med = statistics.median(fine)
            fine = [x for x in fine if abs(x - med) <= 0.3]
        if len(fine) >= 3 and len(main) >= 5:
            # Tick times wander +-0.12 s over minutes (MultiViewer re-syncing its clock):
            # the median, not the lower envelope.
            out.update(delay=statistics.median(fine), method="tick", ticks=len(fine),
                       uncertainty=round(max(0.04, (max(fine) - min(fine)) / 2) + 0.02, 3))
        elif len(main) >= 3:
            out.update(delay=coarse, method="coarse", uncertainty=0.5)
    # Of the readings that could be a delay at all, how many agree. (A replay of another
    # day, rejected by the zone check, is no evidence against the live clock.)
    frac = len(main) / len(plausible) if plausible else 0.0
    if out["method"] == "tick" and len(main) >= 10 and frac >= 0.5:
        out["confidence"] = "high"
    elif out["method"] != "none" and len(main) >= 5 and frac >= 0.5:
        out["confidence"] = "medium"
    elif out["method"] != "none":
        out["confidence"] = "low"
    if ambiguous and out["confidence"] in ("high", "medium"):
        out["confidence"] = "low"
    if zones is None and out["confidence"] == "high":
        out["confidence"] = "medium"  # display zone unchecked

    if not visible:
        out["status"] = "no_clock"  # MultiViewer's header never on screen
    elif out["method"] != "none":
        out["status"] = "ambiguous" if ambiguous else "ok"
    elif zones is not None and readable and (alt := _verify(readings, wall_of, None, prior, step, max_delay)[1]):
        out["status"] = "zone_mismatch"  # running clock, but in none of the candidate zones
        best = clusters([(d, p) for p, d in alt.items()])[0]
        out["zone"] = _implied_zone(readings, {p for _, p in best}, wall_of, statistics.median(d for d, _ in best))
    elif not verified:
        out["status"] = "clock_not_running"  # frozen (session over), paused, or unreadable
    else:
        out["status"] = "too_few_readings"
    return out


def wall_mapper(pieces: list[Piece]) -> Callable[[float], float] | None:
    """Audio media time m -> wall clock through the extractor's pieces (handles stalls)."""
    if not pieces:
        return None
    starts = [p.media for p in pieces]

    def wall(m: float) -> float:
        p = pieces[max(0, bisect.bisect_right(starts, m) - 1)]
        return p.wall + (m - p.media)

    return wall


# ---- one recording, analysed incrementally ------------------------------------------


@dataclass
class ClockJob:
    path: Path
    s: Settings
    index: FlvIndex = field(init=False)
    reader: Reader = field(default_factory=Reader)
    readings: list[Reading] = field(default_factory=list)
    ticks: list[Tick] = field(default_factory=list)
    refined: set[float] = field(default_factory=set)  # reading pts whose tick window was decoded
    sampled_to: float | None = None  # DTS (s) of the last sampled keyframe
    a0_saved: float | None = None  # a0 from persisted readings, until the file is indexed again
    ran_at: float = 0.0

    def __post_init__(self) -> None:
        self.index = FlvIndex(self.path)

    # Persisted so a restart neither re-reads the clock nor loses a live file's history.
    def dump(self) -> dict:
        return {"a0": self.a0, "sampledTo": self.sampled_to,
                "readings": [[round(r.pts, 3), r.tod, int(r.visible), int(r.searched)] for r in self.readings],
                "ticks": [[round(t.pts, 4), t.value] for t in self.ticks],
                "refined": sorted(round(p, 3) for p in self.refined)}

    def load(self, obs: dict) -> None:
        self.sampled_to, self.a0_saved = obs.get("sampledTo"), obs.get("a0")
        self.readings = [Reading(p, tod, bool(v), bool(s)) for p, tod, v, s in obs.get("readings", [])]
        self.ticks = [Tick(p, v) for p, v in obs.get("ticks", [])]
        self.refined = set(obs.get("refined", []))

    def _clear(self) -> None:
        self.reader, self.readings, self.ticks, self.refined, self.sampled_to = Reader(), [], [], set(), None

    def sample(self) -> int:
        """Read the clock on keyframes added since the last call. Returns how many."""
        before = self.index.size
        self.index.update()
        if self.index.size < before:  # file replaced
            self._clear()
        picks = self.index.pick(self.sampled_to, self.s.step)
        if not picks:
            return 0
        # Full-frame search (the expensive part) only for the first sample of each
        # locate_every bucket. Those frames go first, so every other frame of the batch
        # is read knowing all the layouts found (they'd race the search otherwise).
        search, rest = [], []
        prev = self.sampled_to
        for i in picks:
            t = self.index.kf_ms[i] / 1000
            first = prev is None or math.floor(t / self.s.locate_every) != math.floor(prev / self.s.locate_every)
            (search if first else rest).append(i)
            prev = t
        new = self._read(search, True) + self._read(rest, False)
        self.readings += new
        self.readings.sort(key=lambda r: r.pts)
        self.sampled_to = self.index.kf_ms[picks[-1]] / 1000
        return len(new)

    def _read(self, kfs: list[int], may_search: bool) -> list[Reading]:
        out: list[Reading] = []
        if not kfs:
            return out
        with ThreadPoolExecutor(max_workers=self.s.jobs) as ex:
            pending: deque = deque()
            for fr in decode(self.index.keyframe_stream(kfs)):
                pending.append(ex.submit(self.reader.read, fr, may_search))
                if len(pending) >= 2 * self.s.jobs:  # bound memory: 2 MB per 1080p frame
                    out.append(pending.popleft().result())
            out += [f.result() for f in pending]
        return out

    def refine(self, inlier_pts: list[float]) -> int:
        """Tick timing for a spread of not-yet-refined inlier keyframes."""
        if not self.reader.layouts:
            return 0
        todo = [p for p in inlier_pts if round(p, 3) not in self.refined and p + 1.5 <= self.index.end]
        k = min(self.s.refine_max, max(3, math.ceil(len(todo) / 3)), len(todo))
        if k <= 0:
            return 0
        picks = list(dict.fromkeys(todo[round(i * (len(todo) - 1) / max(k - 1, 1))] for i in range(k)))
        by_pts = {r.pts: r for r in self.readings}
        layouts = sorted(self.reader.layouts, key=lambda L: -L.hits)

        def one(p: float) -> list[Tick]:
            kf = bisect.bisect_right(self.index.kf_ms, round(p * 1000)) - 1
            L = next((L for L in layouts if L.id == by_pts[p].layout), layouts[0])
            return find_ticks(self.index, kf, L) if kf >= 0 else []

        with ThreadPoolExecutor(max_workers=self.s.jobs) as ex:
            found = [t for ts in ex.map(one, picks) for t in ts]
        self.ticks += found
        self.ticks.sort(key=lambda t: t.pts)
        self.refined |= {round(p, 3) for p in picks}
        return len(found)

    @property
    def a0(self) -> float:
        return self.index.a0 if self.index.a0 is not None else (self.a0_saved or 0.0)

    def wall_of_pts(self, wall_of_m: Callable[[float], float]) -> Callable[[float], float]:
        a0 = self.a0
        return lambda pts: wall_of_m(pts - a0)

    def estimate(self, wall_of_m: Callable[[float], float], zones: list[int] | None, prior: float,
                 latest: bool) -> dict:
        """Just the arithmetic over what was read so far: no file access."""
        return estimate(self.readings, self.ticks, self.wall_of_pts(wall_of_m), zones, prior, self.s.step,
                        latest, self.s.max_delay)

    def run(self, wall_of_m: Callable[[float], float], zones: list[int] | None, prior: float,
            latest: bool) -> dict:
        """Sample new video, refine against the current inliers, estimate. Blocking."""
        self.sample()
        est = self.estimate(wall_of_m, zones, prior, latest)
        if est["inliers"] and self.refine(est["inliers"]):
            est = self.estimate(wall_of_m, zones, prior, latest)
        return est


# ---- display zones ------------------------------------------------------------------


class OpenF1Zones:
    """Circuit UTC offsets of F1 sessions overlapping a wall-time span (OpenF1 schedule)."""

    def __init__(self, ttl: float = 6 * 3600):
        self.ttl = ttl
        self._years: dict[int, tuple[float, list[tuple[float, float, int]]]] = {}
        self._failed_at: dict[int, float] = {}
        self.http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "fone-commentary-sync/0.3"})

    async def _year(self, year: int) -> list[tuple[float, float, int]] | None:
        now = time.time()
        if (c := self._years.get(year)) and now - c[0] < self.ttl:
            return c[1]
        if now - self._failed_at.get(year, 0) < 600:
            return c[1] if c else None
        try:
            r = await self.http.get(OPENF1_SESSIONS, params={"year": year})
            r.raise_for_status()
            rows = [(datetime.fromisoformat(s["date_start"]).timestamp(),
                     datetime.fromisoformat(s["date_end"]).timestamp(), parse_offset(s["gmt_offset"]))
                    for s in r.json()
                    if s.get("date_start") and s.get("date_end") and s.get("gmt_offset") and not s.get("is_cancelled")]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("OpenF1 sessions %d: %s", year, e)
            self._failed_at[year] = now
            return c[1] if c else None
        self._years[year] = (now, rows)
        return rows

    async def __call__(self, lo: float, hi: float) -> list[int] | None:
        years = {datetime.fromtimestamp(t, timezone.utc).year for t in (lo, hi)}
        out: set[int] = set()
        for y in sorted(years):
            rows = await self._year(y)
            if rows is None:
                return None
            out |= {off for a, b, off in rows if a - 3 * 3600 <= hi and b + 3 * 3600 >= lo}
        return sorted(out)

    async def close(self) -> None:
        await self.http.aclose()


# ---- service --------------------------------------------------------------------------


def _public(res: dict) -> dict:
    return {k: v for k, v in res.items() if k != "obs"}


class ScreenClockService:
    """Measures `delayScreen` for every FLV recording the extractor has audio for."""

    def __init__(self, cfg: Config, extractor: Extractor, zones: ZoneLookup | None = None,
                 settings: Settings | None = None):
        self.cfg = cfg
        self.extractor = extractor
        self.s = settings or Settings.from_env()
        self._own_zones = zones is None
        self.zones: ZoneLookup = zones or OpenF1Zones()
        self.prior = cfg.default_delay  # only breaks ties between candidate zones / clusters
        self.jobs: dict[str, ClockJob] = {}
        self.results: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self.enabled = tesseract_available()

    async def start(self) -> None:
        if not self.enabled:
            log.warning("tesseract not installed: screen clock disabled")
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._own_zones:
            await self.zones.close()  # type: ignore[attr-defined]

    # ---- results -------------------------------------------------------------

    def result(self, fid: str) -> dict | None:
        """Public result for a file, None if the file is unknown."""
        st = self.extractor.files.get(fid)
        if st is None:
            return None
        res = self.results.get(fid) or self._load(st)
        if res is None:
            status = "pending" if self.enabled else "disabled"
            return {"delayScreen": None, "n": 0, "confidence": "none", "updated": None, "basis": status,
                    "status": status}
        return _public(res)

    def _load(self, st: FileState) -> dict | None:
        try:
            res = json.loads((st.out_dir / RESULT_FILE).read_text())
        except (OSError, ValueError):
            return None
        if res.get("sourceSize", 0) > st.rec.size:  # recording replaced since
            return None
        self.results[st.rec.id] = res
        return res

    def _save(self, st: FileState, res: dict) -> None:
        self.results[st.rec.id] = res
        # The extractor owns out_dir: it deletes and recreates it when
        # it (re)starts a file. Never create it; only write while it is in use. This runs
        # on the event loop, as does that delete+mkdir, so the two can't interleave.
        if st.status not in ("running", "done") or not st.out_dir.is_dir():
            return
        tmp = st.out_dir / (RESULT_FILE + ".tmp")
        try:
            tmp.write_text(json.dumps(res))
            os.replace(tmp, st.out_dir / RESULT_FILE)
        except OSError as e:
            log.warning("saving %s: %s", st.out_dir / RESULT_FILE, e)

    # ---- scheduling ----------------------------------------------------------

    def _live(self, st: FileState, now: float) -> bool:
        return self.extractor.is_growing(st.rec, now) or (st.status == "running" and st.follow)

    def _due(self, now: float) -> list[FileState]:
        """Files to (re)analyse, live ones first, then newest finished ones."""
        live, done = [], []
        for st in self.extractor.files.values():
            if st.status not in ("running", "done"):
                continue  # no audio mapping yet
            is_live = self._live(st, now)
            if st.status == "running" and not is_live:
                continue  # whole-file extraction under way: its mapping comes when it's done
            res = self.results.get(st.rec.id) or self._load(st)
            age = now - (res or {}).get("updated", 0)
            if is_live:
                job = self.jobs.get(st.rec.id)
                if now - (job.ran_at if job else (res or {}).get("updated", 0)) >= self.s.interval:
                    live.append(st)
            elif res is None or not res.get("final") or res.get("sourceSize") != st.rec.size:
                done.append(st)
            elif res.get("status") == "error" and age >= 3600:
                done.append(st)
            elif not res.get("zonesChecked") and age >= 1800:
                done.append(st)  # zone lookup was down: re-estimate (no OCR) once it's back
        done.sort(key=lambda st: -(st.rec.name_time or st.rec.mtime))
        return live + done

    async def _loop(self) -> None:
        while True:
            try:
                # Each file at most once per pass. A live file still waiting for its audio
                # mapping is due again at once, and analyse() returns without ever yielding
                # then: re-picking it here would spin forever and freeze the whole server
                # (including the extractor tail that would end the wait).
                tried: set[str] = set()
                while due := [st for st in self._due(time.time()) if st.rec.id not in tried]:
                    tried.add(due[0].rec.id)
                    await self.analyse(due[0])
                    await asyncio.sleep(0)  # let requests and the extractor in between files
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("screen clock pass failed")
            await asyncio.sleep(self.s.poll)

    def _job(self, st: FileState, prev: dict | None) -> ClockJob:
        job = self.jobs.get(st.rec.id)
        if job is None or job.path != st.rec.path:
            job = self.jobs[st.rec.id] = ClockJob(st.rec.path, self.s)
            if prev and prev.get("obs") and prev.get("sourceSize", 0) <= st.rec.size:
                job.load(prev["obs"])
        return job

    async def _zones(self, lo: float, hi: float) -> tuple[list[int] | None, bool | None]:
        """(candidate display zones or None if unknown, whether an F1 session overlaps)."""
        local = int(datetime.fromtimestamp(lo, self.cfg.filename_tz).utcoffset().total_seconds())
        try:
            circuit = await self.zones(lo, hi)
        except Exception:
            log.exception("zone lookup")
            circuit = None
        if circuit is None:
            return None, None
        return sorted({local, *self.s.extra_zones, *circuit}), bool(circuit)

    async def analyse(self, st: FileState) -> dict:
        """One incremental pass over a recording; persists and returns the result."""
        now = time.time()
        rec = st.rec
        live = self._live(st, now)
        final = not live and st.status == "done"
        prev = self.results.get(rec.id) or self._load(st)
        job = self._job(st, prev)
        job.ran_at = now
        res: dict = {"delayScreen": None, "n": 0, "confidence": "none", "updated": now, "basis": "",
                     "growing": live, "final": final, "sourceSize": rec.size}
        if rec.path.suffix.lower() != ".flv":
            res.update(status="unsupported", basis="screen clock reads FLV recordings only", final=True,
                       zonesChecked=True)
            self._save(st, res)
            return res
        pieces, segs, provisional = self.extractor.pieces(st)
        wall_of_m = wall_mapper(pieces)
        if wall_of_m is None or provisional:
            res.update(status="waiting", basis="audio wall-clock mapping not established yet", final=False)
            self.results[rec.id] = res
            job.ran_at = 0.0  # try again next poll, not next interval
            return res
        zones, session = await self._zones(wall_of_m(0.0), wall_of_m(segs[-1].end if segs else 0.0))
        checked = zones is not None
        if session is False:  # no F1 session anywhere near: no live clock to read, skip the OCR
            res.update(status="no_session", basis="no F1 session during this recording", zonesChecked=True)
            self._save(st, res)
            return res
        # A finished file already read to its end only needs the arithmetic redone.
        reread = not (prev and prev.get("final") and prev.get("sourceSize") == rec.size and prev.get("obs")
                      and prev.get("status") not in ("error", "no_session"))
        try:
            if reread:
                await asyncio.to_thread(job.run, wall_of_m, zones, self.prior, live)
        except Exception as e:  # unreadable file, ffmpeg/tesseract trouble: retried hourly
            log.exception("screen clock %s", rec.relpath)
            res.update(status="error", basis=f"{type(e).__name__}: {e}"[:200], final=True, zonesChecked=True)
            self._save(st, res)
            self.jobs.pop(rec.id, None)
            return res
        # The mapping may have moved while we decoded (live lags settle): estimate with
        # the current one; the arithmetic costs nothing.
        pieces, _, _ = self.extractor.pieces(st)
        est = job.estimate(wall_mapper(pieces) or wall_of_m, zones, self.prior, live)
        a0 = job.a0
        res.update(
            delayScreen=None if est["delay"] is None else round(est["delay"], 3),
            n=est["n"], confidence=est["confidence"], basis=self._basis(est, checked),
            status=est["status"] if est["readings"] else "no_video", method=est["method"],
            uncertainty=est["uncertainty"], ticks=est["ticks"],
            displayOffset=None if est["zone"] is None else format_offset(est["zone"]),
            zones=None if zones is None else [format_offset(z) for z in zones], zonesChecked=checked,
            analyzedTo=None if job.sampled_to is None else round(job.sampled_to - a0, 3),
            readings={k: est[k] for k in ("readings", "visible", "readable", "plausible", "verified")},
            segments=[{"from": round(s["from"] - a0, 2), "to": round(s["to"] - a0, 2), "n": s["n"],
                       "delay": s["delay"]} for s in est["segments"]],
            obs=job.dump(),
        )
        self._save(st, res)
        if final:
            self.jobs.pop(rec.id, None)  # the persisted readings are all a re-estimate needs
        log.info("screen clock %s: %s delay=%s (%s, n=%d)", rec.relpath, res["status"], res["delayScreen"],
                 res["confidence"], res["n"])
        return res

    @staticmethod
    def _basis(est: dict, checked: bool) -> str:
        zone = "" if est["zone"] is None else f", shown in UTC{format_offset(est['zone'])}"
        if not checked:
            zone += " (zone unchecked)"
        if est["method"] == "tick":
            return f"MultiViewer Track time: {est['ticks']} second ticks, {est['n']}/{est['readable']} readings{zone}"
        if est["method"] == "coarse":
            return f"MultiViewer Track time: {est['n']}/{est['readable']} readings, whole seconds{zone}"
        return {
            "no_clock": "no MultiViewer Track time on screen",
            "clock_not_running": "Track time visible but not running (session over or paused)",
            "zone_mismatch": f"running Track time in an unexpected zone{zone}",
        }.get(est["status"], est["status"])


def make_router(service: ScreenClockService) -> APIRouter:
    router = APIRouter(prefix="/api/screenclock")

    @router.get("/{fid}")
    def screenclock(fid: str):
        res = service.result(fid)
        if res is None:
            raise HTTPException(404, f"unknown file {fid}")
        return res

    return router
