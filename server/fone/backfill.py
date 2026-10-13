"""Backfill: when the recorder missed part of a stream, import the streamer's official replay.

  live log  poll each configured room every 60 s; store [live_time, last seen live] periods
  gaps      once a period has ended and its recordings are extracted, compare it with the
            channel's recording coverage (fone's wall-clock pieces); > 60 s missing -> a job
  job       poll the uploader's '直播回放' series every 10 min for up to 36 h (publish delay:
            median ~1 h, max seen 18.4 h; some streams never get one); match by title hour +
            duration, pinned by our live_time; download the pages covering the gap (audio of
            every page: cheap, and what places later pages exactly); cut the repeats; place the
            replay on the wall clock (alignment against what was recorded, else live_time / S0);
            write a recorder-style FLV into the import dir, where fone's scanner finds it like
            any other recording.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import backfill_bili as bb
from .backfill_dedup import Timeline, find_joins, load_part
from .backfill_start import SR, AlignIndex, Placement, align, danmaku_origin, decode_8k, estimate_start, \
    parse_dmks_live
from .backfill_writer import build_import
from .config import Config
from .store import Store

if TYPE_CHECKING:
    from .extractor import Extractor, FileState

log = logging.getLogger("fone.backfill")

ACTIVE = ("waiting", "downloading", "processing")
MAX_ATTEMPTS = 5
WINDOWS = 8  # recording windows to align against
WINDOW_S = 30.0
BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")


# ---- settings ------------------------------------------------------------------------------------


def parse_rooms(spec: str) -> dict[int, str]:
    """"23954427=胡闹P房,123=other" -> {23954427: "胡闹P房", 123: "other"}."""
    out = {}
    for pair in filter(None, (x.strip() for x in spec.split(","))):
        room, _, channel = pair.partition("=")
        if room.strip().isdigit() and channel.strip():
            out[int(room.strip())] = channel.strip()
    return out


@dataclass(frozen=True)
class Settings:
    rooms: dict[int, str] = field(default_factory=dict)  # room id -> channel (recorder folder name)
    import_dir: Path | None = None
    video_height: int | None = 1080  # None = audio only
    live_interval: float = 60.0
    series_interval: float = 600.0
    give_up_after: float = 36 * 3600.0
    min_gap: float = 60.0
    settle: float = 1800.0  # longest wait for an ended period's recordings to finish extracting
    danmakus: bool = True  # third-party danmakus.com for S0 / missing live_time
    keep_work: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        video = env.get("FONE_BACKFILL_VIDEO", "1080").strip().lower()
        return cls(
            rooms=parse_rooms(env.get("FONE_ROOMS", "")),
            import_dir=Path(env["FONE_IMPORT_DIR"]) if env.get("FONE_IMPORT_DIR") else None,
            video_height=None if video in ("", "0", "none", "audio") else int(video.rstrip("p")),
            min_gap=float(env.get("FONE_BACKFILL_MIN_GAP", "60")),
            give_up_after=float(env.get("FONE_BACKFILL_GIVE_UP_HOURS", "36")) * 3600,
            danmakus=env.get("FONE_BACKFILL_DANMAKUS", "1") not in ("0", "false", "no"),
            keep_work=env.get("FONE_BACKFILL_KEEP_WORK", "0") in ("1", "true", "yes"),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.rooms) and self.import_dir is not None


# ---- persistence (tables in fone's sqlite DB) ------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS bf_period (
    room INTEGER NOT NULL,
    start REAL NOT NULL,        -- live_time (开播)
    last_live REAL NOT NULL,    -- last poll that saw it live
    stop REAL,                  -- first poll that saw it over (NULL while live)
    title TEXT NOT NULL,        -- room title when first seen
    uid INTEGER NOT NULL,
    channel TEXT NOT NULL,
    checked REAL,               -- when gap detection ran (NULL = pending)
    PRIMARY KEY (room, start)
);
CREATE TABLE IF NOT EXISTS bf_job (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    state TEXT NOT NULL,        -- waiting | downloading | processing | done | failed | expired
    data TEXT NOT NULL,         -- JSON: window, gaps, room, uid, liveTime, title, bvid, replay, pages, result ...
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bf_series (
    uid INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL,
    updated REAL NOT NULL
);
"""

PERIOD_COLS = ("room", "start", "last_live", "stop", "title", "uid", "channel", "checked")


@dataclass
class Job:
    id: int
    channel: str
    state: str
    data: dict
    error: str | None = None
    attempts: int = 0
    next_try: float = 0.0
    created: float = 0.0
    updated: float = 0.0

    def to_dict(self) -> dict:
        d = self.data
        return {"id": self.id, "channel": self.channel, "state": self.state, "error": self.error,
                "attempts": self.attempts, "nextTry": self.next_try, "created": self.created, "updated": self.updated,
                "source": d.get("source"), "window": d.get("window"), "gaps": d.get("gaps"), "bvid": d.get("bvid"),
                "title": d.get("title"), "liveTime": d.get("liveTime"), "replay": d.get("replay"),
                "pages": d.get("selected"), "progress": d.get("progress"), "placement": d.get("placement"),
                "joins": d.get("joins"), "warnings": d.get("warnings"), "result": d.get("result")}


class BackfillDB:
    """Backfill's tables, living in fone's sqlite DB (shares Store's connection and lock)."""

    def __init__(self, store: Store):
        self.store = store
        store.ensure_schema(SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        return self.store.query(sql, args)

    # periods
    def upsert_live(self, room: int, start: float, now: float, title: str, uid: int, channel: str) -> None:
        self._q("INSERT INTO bf_period (room, start, last_live, title, uid, channel) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (room, start) DO UPDATE SET last_live = excluded.last_live, stop = NULL",
                (room, start, now, title, uid, channel))

    def close_period(self, room: int, start: float, stop: float) -> None:
        self._q("UPDATE bf_period SET stop = ? WHERE room = ? AND start = ? AND stop IS NULL", (stop, room, start))

    def periods(self, room: int | None = None, open_only: bool = False, pending: bool = False,
                since: float | None = None, limit: int = 1000) -> list[dict]:
        where, args = [], []
        if room is not None:
            where.append("room = ?")
            args.append(room)
        if open_only:
            where.append("stop IS NULL")
        if pending:
            where.append("stop IS NOT NULL AND checked IS NULL")
        if since is not None:
            where.append("start >= ?")
            args.append(since)
        sql = f"SELECT {', '.join(PERIOD_COLS)} FROM bf_period"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._q(sql + " ORDER BY start DESC LIMIT ?", (*args, limit))
        return [dict(zip(PERIOD_COLS, r)) for r in rows]

    def mark_checked(self, room: int, start: float, now: float) -> None:
        self._q("UPDATE bf_period SET checked = ? WHERE room = ? AND start = ?", (now, room, start))

    # jobs
    def add_job(self, channel: str, data: dict, now: float, state: str = "waiting") -> Job:
        jid = self.store.insert(
            "INSERT INTO bf_job (channel, state, data, created, updated) VALUES (?, ?, ?, ?, ?)",
            (channel, state, json.dumps(data, ensure_ascii=False), now, now))
        return Job(jid, channel, state, data, created=now, updated=now)

    def save(self, job: Job, now: float | None = None) -> None:
        job.updated = time.time() if now is None else now
        self._q("UPDATE bf_job SET state = ?, data = ?, error = ?, attempts = ?, next_try = ?, updated = ? "
                "WHERE id = ?", (job.state, json.dumps(job.data, ensure_ascii=False), job.error, job.attempts,
                                 job.next_try, job.updated, job.id))

    def _job(self, r: tuple) -> Job:
        return Job(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5], r[6], r[7], r[8])

    _JOB = "SELECT id, channel, state, data, error, attempts, next_try, created, updated FROM bf_job"

    def job(self, jid: int) -> Job | None:
        rows = self._q(self._JOB + " WHERE id = ?", (jid,))
        return self._job(rows[0]) if rows else None

    def jobs(self, states: tuple[str, ...] | None = None, limit: int = 50) -> list[Job]:
        if states:
            rows = self._q(self._JOB + f" WHERE state IN ({','.join('?' * len(states))}) ORDER BY id DESC LIMIT ?",
                           (*states, limit))
        else:
            rows = self._q(self._JOB + " ORDER BY id DESC LIMIT ?", (limit,))
        return [self._job(r) for r in rows]

    def next_due(self, now: float) -> Job | None:
        rows = self._q(self._JOB + f" WHERE state IN ({','.join('?' * len(ACTIVE))}) AND next_try <= ? "
                       "ORDER BY next_try, id LIMIT 1", (*ACTIVE, now))
        return self._job(rows[0]) if rows else None

    # series ids never change
    def series_id(self, uid: int) -> int | None:
        rows = self._q("SELECT series_id FROM bf_series WHERE uid = ?", (uid,))
        return rows[0][0] if rows else None

    def set_series_id(self, uid: int, sid: int) -> None:
        self._q("INSERT OR REPLACE INTO bf_series VALUES (?, ?, ?)", (uid, sid, time.time()))


# ---- coverage ------------------------------------------------------------------------------------


def subtract(span: tuple[float, float], covered: list[tuple[float, float]], min_len: float) -> list[list[float]]:
    """Parts of span not in any covered interval, longer than min_len."""
    lo, hi = span
    gaps, pos = [], lo
    for a, b in sorted(covered):
        if b <= pos:
            continue
        if a > pos:
            gaps.append([pos, min(a, hi)])
        pos = max(pos, b)
        if pos >= hi:
            break
    if pos < hi:
        gaps.append([pos, hi])
    return [g for g in gaps if g[1] - g[0] > min_len]


def measure(tl: Timeline, windows: list[tuple[list[Path], float]], work: Path) -> tuple[Placement | None, list[dict]]:
    """Align recording windows (HLS segment files, wall time of their first sample) against the
    replay's pages. Heavy (decodes every page to 8 kHz): run it in a thread."""
    index = AlignIndex([decode_8k([p.path], work / f"p{n + 1}_8k.f32") for n, p in enumerate(tl.parts)])
    q = [(decode_8k(paths)[: int(WINDOW_S * SR)], w0) for paths, w0 in windows]
    return align(index, tl, q)


# ---- the service ---------------------------------------------------------------------------------


class BackfillService:
    def __init__(self, cfg: Config, extractor: Extractor, store: Store, settings: Settings | None = None,
                 bili: bb.BiliClient | None = None, downloader: bb.Downloader | None = None,
                 http: httpx.AsyncClient | None = None):
        self.cfg, self.extractor = cfg, extractor
        self.settings = settings or Settings.from_env()
        self.db = BackfillDB(store)
        self.bili = bili or bb.BiliClient()
        self.downloader = downloader or bb.Downloader()
        self.http = http or httpx.AsyncClient(timeout=30, follow_redirects=True)
        self.work_root = cfg.data_dir / "backfill"
        self.rooms: dict[int, dict] = {}  # last poll per room
        self.current: dict | None = None  # the job step running now
        self._series: dict[int, tuple[float, list[bb.Replay]]] = {}
        self._playurl: dict[int, tuple[float, dict]] = {}
        self._tasks: list[asyncio.Task] = []
        self._wake = asyncio.Event()
        self._swept_at = 0.0

    async def start(self) -> None:
        if not self.settings.enabled:
            log.info("backfill disabled (FONE_ROOMS / FONE_IMPORT_DIR not set)")
            return
        self.work_root.mkdir(parents=True, exist_ok=True)
        self._tasks = [asyncio.create_task(self._live_loop()), asyncio.create_task(self._job_loop())]
        log.info("backfill watching rooms %s -> %s", self.settings.rooms, self.settings.import_dir)

    def _sweep_partial_imports(self, older_than: float = 3600.0) -> None:
        """Imports cut off mid-copy by a restart (<name>.flv.part, GBs each). Nothing else
        removes them: the scanner ignores them and the janitor only deletes media files.
        A copy takes about a minute, so an hour-old one is dead."""
        for p in self.settings.import_dir.glob("*/*.part"):  # type: ignore[union-attr]
            try:
                if time.time() - p.stat().st_mtime > older_than:
                    p.unlink()
                    log.info("removed partial import %s", p)
            except OSError as e:
                log.warning("partial import %s: %s", p, e)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self.bili.close()
        await self.downloader.close()
        await self.http.aclose()

    # ---- live log ----------------------------------------------------------------------------

    async def _live_loop(self) -> None:
        while True:
            try:
                await self.poll_rooms()
            except Exception:
                log.exception("live poll failed")
            try:
                self.check_periods()
            except Exception:
                log.exception("gap check failed")
            if time.time() - self._swept_at > 3600:
                self._swept_at = time.time()
                try:
                    await asyncio.to_thread(self._sweep_partial_imports)
                except OSError as e:
                    log.warning("partial import sweep: %s", e)
            await asyncio.sleep(self.settings.live_interval)

    async def poll_rooms(self) -> None:
        for room, channel in self.settings.rooms.items():
            try:
                st = await self.bili.room_info(room)
            except bb.RiskControl as e:
                log.warning("live poll: %s", e)
                self.rooms[room] = {**self.rooms.get(room, {}), "error": str(e)}
                return  # don't hammer the API while it's suspicious of us
            except (bb.BiliError, httpx.HTTPError, ValueError) as e:
                log.warning("live poll room %s: %s", room, e)
                self.rooms[room] = {**self.rooms.get(room, {}), "error": str(e)}
                continue
            self.record(st, channel, time.time())

    def record(self, st: bb.RoomStatus, channel: str, now: float) -> None:
        """One poll result -> periods. live_status 2 (rerun loop) is not live."""
        self.rooms[st.room] = {"room": st.room, "channel": channel, "uid": st.uid, "live": st.live,
                               "liveStatus": st.live_status, "liveTime": st.live_time, "title": st.title, "polled": now}
        for p in self.db.periods(st.room, open_only=True):
            if not (st.live and st.live_time == p["start"]):  # ended, or restarted with a new 开播
                self.db.close_period(st.room, p["start"], now)
        if st.live and st.live_time:
            self.db.upsert_live(st.room, st.live_time, now, st.title, st.uid, channel)

    # ---- gap detection -----------------------------------------------------------------------

    def _files(self, channel: str, lo: float, hi: float) -> list[FileState]:
        out = []
        for st in self.extractor.files.values():
            rec = st.rec
            if rec.channel != channel:
                continue
            start = rec.name_time if rec.name_time is not None else rec.mtime - 6 * 3600
            if rec.mtime >= lo and start <= hi:
                out.append(st)
        return out

    def coverage(self, channel: str, lo: float, hi: float) -> list[tuple[float, float]]:
        """Wall spans the channel's files (recordings and earlier imports) cover."""
        spans = []
        for st in self._files(channel, lo, hi):
            pieces, _, _ = self.extractor.pieces(st)
            spans += [(p.wall, p.wall + p.duration) for p in pieces]
        return spans

    def gaps(self, channel: str, lo: float, hi: float) -> list[list[float]]:
        return subtract((lo, hi), self.coverage(channel, lo, hi), self.settings.min_gap)

    def check_periods(self, now: float | None = None) -> list[Job]:
        now = time.time() if now is None else now
        created = []
        if not self.extractor.files:
            # No recordings known at all: the first scan hasn't finished (we start alongside
            # it) or the NAS is unreachable. Either way the coverage would read as nothing,
            # i.e. the whole stream missing: a full replay download for nothing.
            return created
        for p in self.db.periods(pending=True):
            lo, hi = p["start"], p["last_live"]
            files = self._files(p["channel"], lo, hi)
            busy = [st for st in files if st.status in ("idle", "queued", "running")]
            for st in busy:
                if st.status == "idle":
                    self.extractor.ensure(st)
            if busy and now - p["stop"] < self.settings.settle:
                continue  # let the extractor finish so the coverage is final
            gaps = self.gaps(p["channel"], lo, hi)
            self.db.mark_checked(p["room"], p["start"], now)
            if gaps:
                log.info("room %s live %s: %d gap(s), %.0fs missing", p["room"], p["start"], len(gaps),
                         sum(b - a for a, b in gaps))
                created.append(self.submit(p["channel"], window=(gaps[0][0], gaps[-1][1]), gaps=gaps, room=p["room"],
                                           uid=p["uid"], live_time=p["start"], title=p["title"], source="auto",
                                           period=(p["start"], p["last_live"]), now=now))
        return created

    # ---- jobs --------------------------------------------------------------------------------

    def room_for(self, channel: str) -> int | None:
        return next((r for r, c in self.settings.rooms.items() if c == channel), None)

    def submit(self, channel: str, window: tuple[float, float] | None = None, *, bvid: str | None = None,
               gaps: list | None = None, room: int | None = None, uid: int | None = None,
               live_time: float | None = None, title: str | None = None, source: str = "manual",
               period: tuple[float, float] | None = None, now: float | None = None) -> Job:
        now = time.time() if now is None else now
        if bvid:
            for j in self.db.jobs(ACTIVE):
                if j.data.get("bvid") == bvid:
                    return j  # already on it
        data = {"source": source, "window": list(window) if window else None, "gaps": gaps, "bvid": bvid,
                "room": room or self.room_for(channel), "uid": uid, "liveTime": live_time, "title": title,
                "period": list(period) if period else None}
        job = self.db.add_job(channel, data, now)
        self._wake.set()
        return job

    async def _job_loop(self) -> None:
        while True:
            try:
                job = self.db.next_due(time.time())
                if job is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), 30)
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self.step(job)
            except Exception:  # sqlite trouble, a corrupt row: keep the loop (and later jobs) alive
                log.exception("backfill job loop")
                await asyncio.sleep(60)

    async def step(self, job: Job) -> None:
        """Advance one job by one state, persisting the outcome."""
        now = time.time()
        self.current = {"job": job.id, "state": job.state, "since": now}
        try:
            if job.state == "waiting":
                await self._find(job)
            elif job.state == "downloading":
                await self._download(job)
            elif job.state == "processing":
                await self._process(job)
            job.error = job.error if job.state == "expired" else None
        except bb.RiskControl as e:
            job.error, job.next_try = str(e), time.time() + 900
        except bb.DownloadBusy as e:
            job.error, job.next_try = str(e), time.time() + 300
        except Exception as e:  # noqa: BLE001 - one bad job must not stop the loop
            log.exception("backfill job %s (%s) failed", job.id, job.state)
            job.attempts += 1
            job.error = f"{type(e).__name__}: {e}"
            if job.attempts >= MAX_ATTEMPTS:
                job.state = "failed"
            else:
                job.next_try = time.time() + 300 * 2 ** (job.attempts - 1)
        finally:
            self.current = None
        if job.state in ("done", "failed", "expired") and not self.settings.keep_work and job.data.get("bvid"):
            if not any(j.data.get("bvid") == job.data["bvid"] for j in self.db.jobs(ACTIVE) if j.id != job.id):
                shutil.rmtree(self.work_root / job.data["bvid"], ignore_errors=True)
        self.db.save(job)

    async def _uid(self, job: Job) -> int:
        d = job.data
        if d.get("uid"):
            return d["uid"]
        room = d.get("room")
        if room is None:
            raise ValueError(f"no room configured for channel {job.channel}")
        d["uid"] = self.rooms.get(room, {}).get("uid") or (await self.bili.room_info(room)).uid
        return d["uid"]

    async def replays(self, uid: int, now: float | None = None) -> list[bb.Replay]:
        """The '直播回放' series, at most one listing per series_interval."""
        now = time.time() if now is None else now
        hit = self._series.get(uid)
        if hit and now - hit[0] < self.settings.series_interval:
            return hit[1]
        sid = self.db.series_id(uid)
        if sid is None:
            sid = await self.bili.series_id(uid)
            self.db.set_series_id(uid, sid)
        arcs = await self.bili.series_archives(uid, sid, since=now - 14 * 86400)
        reps = bb.parse_replays(arcs)
        self._series[uid] = (now, reps)
        return reps

    def _sessions(self, room: int | None, around: float) -> list[tuple[float, float]]:
        if room is None:
            return []
        return [(p["start"], p["last_live"]) for p in self.db.periods(room, since=around - 3 * 86400)
                if p["stop"] is not None or p["last_live"] > p["start"]]

    async def _find(self, job: Job) -> None:
        d, now = job.data, time.time()
        uid = await self._uid(job)
        reps = await self.replays(uid)
        if d.get("bvid"):
            r = next((r for r in reps if r.bvid == d["bvid"]), None)
            if r is None:
                raise ValueError(f"{d['bvid']} is not in the 直播回放 series of uid {uid} (last 14 days)")
            window = d.get("window") or [r.slot, r.slot + 3600 + r.content]
            hits = [h for h in bb.match(reps, *window, sessions=self._sessions(d.get("room"), r.slot) or None)
                    if h.replay.bvid == r.bvid] or [bb.Hit(r, "given", r.slot, 0.0)]
        else:
            window = d["window"]
            found = bb.match(reps, *window, title=d.get("title"), live_start=d.get("liveTime"),
                             sessions=self._sessions(d.get("room"), window[0]) or None)
            hits = [h for h in found if h.kind == "sure"]
            if not hits and d.get("source") == "manual" and len(found) == 1:
                hits = found  # asked for explicitly, and only one replay can be meant
            if not hits:
                if now > window[1] + self.settings.give_up_after:
                    job.state, job.error = "expired", "no replay was published"
                else:
                    job.next_try = now + self.settings.series_interval
                return
            for extra in hits[1:]:  # a restart splits a stream into several replays
                self.submit(job.channel, tuple(window), bvid=extra.replay.bvid, gaps=d.get("gaps"), room=d.get("room"),
                            uid=uid, title=extra.replay.live_title, source=d.get("source", "auto"))
        hit = hits[0]
        r = hit.replay
        exact = r.exact_start
        d.update(bvid=r.bvid, replay={"bvid": r.bvid, "title": r.title, "liveTitle": r.live_title, "slot": r.slot,
                                      "duration": r.duration, "pubdate": r.pubdate, "iv": r.iv},
                 replayStart=hit.start, startExact=exact is not None, title=d.get("title") or r.live_title)
        if exact is not None:
            d["liveTime"] = exact
        # The live log may have missed the real end (fone was down): only keep what's still missing.
        span = (hit.start, hit.start + r.content) if exact is not None else None
        if span and d.get("gaps") is not None:
            lo, hi = max(window[0], span[0]), min(window[1], span[1])
            gaps = self.gaps(job.channel, lo, hi) if hi > lo else []
            if not gaps:
                job.state, d["result"] = "done", {"skipped": "nothing missing inside the replay's span"}
                return
            d["gaps"], d["window"] = gaps, [gaps[0][0], gaps[-1][1]]
        job.state, job.next_try = "downloading", 0.0

    async def _dash(self, bvid: str, cid: int) -> dict:
        hit = self._playurl.get(cid)
        if hit and time.time() - hit[0] < 30 * 60:
            return hit[1]
        dash = await self.bili.playurl(bvid, cid)
        self._playurl[cid] = (time.time(), dash)
        return dash

    async def _download(self, job: Job) -> None:
        d = job.data
        bvid = d["bvid"]
        if not d.get("pages"):
            d["pages"] = [{k: p[k] for k in ("cid", "page", "part", "duration")} for p in await self.bili.pagelist(bvid)]
            self.db.save(job)
        pages = d["pages"]
        if d.get("window"):
            iv = d["replay"]["iv"]
            lo, hi = (d["replayStart"],) * 2 if d.get("startExact") else (iv[0][0], iv[-1][1])
            sel = bb.pages_for_window(lo, hi, pages, *d["window"])
        else:
            sel = pages
        if not sel:
            job.state, d["result"] = "done", {"skipped": "the window is outside the replay"}
            return
        d["selected"] = [sel[0]["page"], sel[-1]["page"]]
        work = self.work_root / bvid
        work.mkdir(parents=True, exist_ok=True)
        want = [(p, "audio") for p in pages]
        if self.settings.video_height:
            want += [(p, "video") for p in sel]
        progress = d.setdefault("progress", {})
        last_save = [time.monotonic()]
        for p, kind in want:
            dest = work / f"p{p['page']}_{kind}.m4s"
            key = f"p{p['page']}_{kind}"

            calls = [0]

            async def urls(p=p, kind=kind, calls=calls) -> list[str]:
                if calls[0]:  # asked again: the signed URLs expired or got refused
                    self._playurl.pop(p["cid"], None)
                calls[0] += 1
                dash = await self._dash(bvid, p["cid"])
                return bb.stream_urls(bb.pick_stream(dash, kind, self.settings.video_height))

            def on_progress(have: int, total: int | None, key=key) -> None:
                progress[key] = {"bytes": have, "total": total}
                self.current = {"job": job.id, "state": "downloading", "file": key, "bytes": have, "total": total}
                if time.monotonic() - last_save[0] > 30:
                    last_save[0] = time.monotonic()
                    self.db.save(job)

            size = await self.downloader.fetch(urls, dest, on_progress)
            progress[key] = {"bytes": size, "total": size}
        job.state, job.next_try = "processing", 0.0

    # ---- processing --------------------------------------------------------------------------

    def _recording_windows(self, channel: str, lo: float, hi: float) -> list[tuple[list[Path], float]]:
        """Up to WINDOWS 30-s stretches of extracted recording audio inside [lo, hi], each as
        (HLS segment files, recorder-frame wall time of their first sample)."""
        spans = []  # (wall a, wall b, st, piece, segs)
        for st in self._files(channel, lo, hi):
            if st.status != "done":
                continue
            pieces, segs, provisional = self.extractor.pieces(st)
            if provisional:
                continue
            for pc in pieces:
                a, b = max(pc.wall, lo) + 5, min(pc.wall + pc.duration, hi) - 5 - WINDOW_S
                if b > a:
                    spans.append((a, b, st, pc, segs))
        total = sum(b - a for a, b, *_ in spans)
        if not total:
            return []
        out = []
        for k in range(WINDOWS):
            x = total * (k + 0.5) / WINDOWS
            for a, b, st, pc, segs in spans:
                if x > b - a:
                    x -= b - a
                    continue
                m = pc.media + (a + x - pc.wall)
                i = next((n for n, s in enumerate(segs) if s.end > m), None)
                if i is None:
                    break
                j = i
                while j + 1 < len(segs) and segs[j].end < segs[i].start + WINDOW_S:
                    j += 1
                if segs[j].end > pc.media + pc.duration + 1e-6 or segs[i].start < pc.media - 1e-6:
                    break  # would straddle a stall
                out.append(([st.out_dir / s.name for s in segs[i:j + 1]], pc.wall + segs[i].start - pc.media))
                break
        return out

    async def _s0(self, uid: int, slot: float, cid: int) -> tuple[float | None, list[float], float | None]:
        """(S0, LIVE events, danmakus live_time) from danmakus.com + the replay's danmaku XML."""
        lives = await bb.dmks_lives(self.http, uid)
        live = next((x for x in lives if x["start"] < slot + 3600 and (x["stop"] or time.time()) > slot), None)
        if live is None:
            return None, [], None
        live_time = live["start"] if slot <= live["start"] < slot + 3600 else None
        if not live["danmaku"]:
            return None, [], live_time
        full = parse_dmks_live(await bb.dmks_live_full(self.http, live["id"]))
        s0, _ = danmaku_origin(await self.bili.dm_xml(cid), full.messages)
        return s0, [e for e in full.live_events if slot <= e < slot + 3600], live_time

    async def _process(self, job: Job) -> None:
        d = job.data
        bvid, pages = d["bvid"], d["pages"]
        work = self.work_root / bvid
        parts = await asyncio.to_thread(
            lambda: [load_part(work / f"p{p['page']}_audio.m4s", work / f"p{p['page']}.aac") for p in pages])
        joins, warnings = await asyncio.to_thread(find_joins, parts)
        tl = Timeline(parts, joins)
        d["joins"] = [j.to_dict() for j in joins]
        d["warnings"] = warnings

        # Where it sits on the wall clock: alignment against what the recorder caught first.
        content = tl.keep_seconds(tl.keep())
        start = d.get("liveTime")
        lo, hi = (start, start + content + 60) if start else (d["replay"]["slot"], d["replay"]["slot"] + 3600 + content)
        windows = self._recording_windows(job.channel, lo, hi)
        aligned = None
        if windows:
            aligned, rows = await asyncio.to_thread(measure, tl, windows, work)
            d["align"] = [{k: v for k, v in r.items() if k != "start"} for r in rows]
        s0, events = None, []
        if aligned is None and self.settings.danmakus:
            try:
                s0, events, dmks_live = await self._s0(await self._uid(job), d["replay"]["slot"], pages[0]["cid"])
                start = start if start is not None else dmks_live
            except (httpx.HTTPError, ValueError, KeyError, IndexError, bb.BiliError) as e:
                log.warning("S0 lookup failed: %s", e)
        place: Placement | None = estimate_start(tl.fragments, aligned, start, s0, events)
        if place is None:
            raise ValueError("no way to place the replay on the wall clock (no recording overlap, no live_time)")
        d["placement"] = place.to_dict()

        first, last = d["selected"][0] - 1, d["selected"][1] - 1
        videos = {n: work / f"p{n + 1}_video.m4s" for n in range(first, last + 1)} if self.settings.video_height else None
        out_dir = self.settings.import_dir / job.channel  # type: ignore[operator]
        res = await asyncio.to_thread(build_import, tl, place, first, last, videos, work / f"build_{job.id}", out_dir,
                                      d.get("title") or d["replay"]["liveTitle"], self.cfg.filename_tz)
        shutil.rmtree(work / f"build_{job.id}", ignore_errors=True)
        d["result"] = res.to_dict()
        job.state = "done"
        log.info("backfill job %s: imported %s (%.0fs, start %s)", job.id, res.path.name, res.duration, place.method)

    # ---- status ------------------------------------------------------------------------------

    def status(self) -> dict:
        return {
            "enabled": self.settings.enabled,
            "importDir": str(self.settings.import_dir) if self.settings.import_dir else None,
            "rooms": [{"room": r, "channel": c, **self.rooms.get(r, {})} for r, c in self.settings.rooms.items()],
            "periods": self.db.periods(limit=20),
            "jobs": [j.to_dict() for j in self.db.jobs(limit=30)],
            "current": self.current,
        }


# ---- routes --------------------------------------------------------------------------------------


class ImportBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    bvid: str | None = None
    channel: str | None = None
    start: float | None = Field(None, alias="from")  # JSON says from/to (Python keywords)
    end: float | None = Field(None, alias="to")


def make_router(service: BackfillService) -> APIRouter:
    router = APIRouter(prefix="/api/backfill")

    @router.get("/status")
    def status():
        return service.status()

    @router.post("/import")
    def manual_import(body: ImportBody):
        s = service.settings
        if not s.enabled:
            raise HTTPException(409, "backfill is not configured (FONE_ROOMS, FONE_IMPORT_DIR)")
        channels = sorted(set(s.rooms.values()))
        channel = body.channel or (channels[0] if len(channels) == 1 else None)
        if channel not in channels:
            raise HTTPException(400, f"channel must be one of {channels}")
        if body.bvid:
            if not BVID_RE.match(body.bvid):
                raise HTTPException(400, "bvid must look like BV1xxxxxxxxx")
            window = (body.start, body.end) if body.start is not None and body.end is not None else None
            job = service.submit(channel, window, bvid=body.bvid)
        elif body.start is not None and body.end is not None:
            if not 0 < body.end - body.start <= 24 * 3600:
                raise HTTPException(400, "need 0 < to - from <= 24 h")
            job = service.submit(channel, (body.start, body.end))
        else:
            raise HTTPException(400, 'give {"bvid"} or {"from", "to", "channel"}')
        return {"ok": True, "job": job.to_dict()}

    return router

