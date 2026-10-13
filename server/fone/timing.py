"""F1 timing for the dashboard: per-session state, car positions and track map at any UTC.

Two sources, one on-disk format (`<data_dir>/timing/<session_key>/<Topic>.jsonStream`,
see timing_state.py), told apart by `meta.json`:
  * live    - our own SignalR capture (livefeed.py) of every session on the OpenF1
              calendar, from 15 min before the scheduled start until SessionStatus "Ends"
              (safety stop 4 h after the start). Anonymous access has everything except
              car positions; the extension forwards those from the viewer's F1 TV login
              (POST /positions), so the server never sees the token.
  * archive - livetiming.formula1.com/static/<path>, published ~20 min after a session
              (403 until then), no auth. Replaces the live capture once it appears.
UTC of a message = t0 + its offset; t0 is the archive's AudioStreams/ContentStreams
"Utc", or for a capture the moment its first message arrived. Feed messages are written
at their own timestamp (the archive's clock), so captures and archives line up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Path as PathParam, Query
from pydantic import BaseModel, Field

from . import livefeed
from .config import Config
from .timing_state import (
    POSITION_TOPIC, SessionTimeline, deflate, format_offset, iso, read_stream, track_outline,
)

log = logging.getLogger("fone.timing")

STATIC = "https://livetiming.formula1.com/static"
OPENF1_SESSIONS = "https://api.openf1.org/v1/sessions"
# The circuit's numbered corners (MultiViewer's circuit data, in the car positions' X/Y units).
MV_CIRCUIT = "https://api.multiviewer.app/api/v1/circuits/{circuit}/{year}"
UA = {"User-Agent": "fone/0.3"}
# Streams fetched from the archive: what the dashboard reads, plus best-sector and pit-lane
# data for later. Skipped on purpose: CarData.z (telemetry, big), TimingDataF1 (copy of
# TimingData), DriverRaceInfo, TopThree, LapSeries, TyreStintSeries, CurrentTyres.
ARCHIVE_STREAMS = [
    "SessionInfo", "SessionData", "SessionStatus", "DriverList", "TimingData", "TimingAppData",
    "TimingStats", "TrackStatus", "RaceControlMessages", "WeatherData", "ExtrapolatedClock",
    "LapCount", "PitLaneTimeCollection", "Position.z",
]
ARCHIVE_KEYFRAMES = ["AudioStreams", "ContentStreams", "ArchiveStatus", "SessionInfo"]
REQUIRED_STREAMS = {"SessionInfo", "TimingData"}
MAX_POSITION_WINDOW = 120.0
MAX_UPLOAD_MESSAGES = 5000


@dataclass(frozen=True)
class TimingSettings:
    record_live: bool = True
    lead_seconds: float = 15 * 60  # capture starts this long before the scheduled start
    max_record_seconds: float = 4 * 3600  # safety stop, counted from the scheduled start
    archive_days: float = 3.0  # archives of sessions that ended this recently are fetched unasked
    keep_days: float = 30.0  # stored sessions older than this are deleted (re-fetched on demand)
    max_loaded: int = 4  # sessions kept in memory (a race is ~30 MB)
    tick_seconds: float = 30.0
    retry_seconds: float = 60.0  # live reconnects; on-demand archive retries
    refresh_seconds: float = 0.5  # how often a loaded live capture re-reads its files
    static_base: str = STATIC
    schedule_url: str = OPENF1_SESSIONS
    live_base: str = livefeed.BASE

    @classmethod
    def from_env(cls) -> TimingSettings:
        env = os.environ.get
        return cls(
            record_live=env("FONE_TIMING_RECORD", "1") not in ("0", "false", "no"),
            lead_seconds=float(env("FONE_TIMING_LEAD_MINUTES", "15")) * 60,
            max_record_seconds=float(env("FONE_TIMING_MAX_RECORD_HOURS", "4")) * 3600,
            archive_days=float(env("FONE_TIMING_ARCHIVE_DAYS", "3")),
            keep_days=float(env("FONE_TIMING_KEEP_DAYS", "30")),
            max_loaded=int(env("FONE_TIMING_MAX_LOADED", "4")),
        )


@dataclass
class ScheduledSession:
    key: int
    meeting: int
    name: str
    type: str
    start: float
    end: float
    year: int
    gmt_offset: str = "00:00:00"

    @classmethod
    def from_openf1(cls, r: dict) -> ScheduledSession:
        start = iso(r["date_start"])
        return cls(key=int(r["session_key"]), meeting=int(r["meeting_key"]), name=r.get("session_name") or "",
                   type=r.get("session_type") or "", start=start,
                   end=iso(r["date_end"]) if r.get("date_end") else start + 2 * 3600,
                   year=int(r.get("year") or datetime.fromtimestamp(start, timezone.utc).year),
                   gmt_offset=r.get("gmt_offset") or "00:00:00")


# ---- storage -------------------------------------------------------------------


class SessionStore:
    """`<root>/<key>/` per session: topic streams + meta.json {source, t0, meeting, path, ...}."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._meta_lock = threading.Lock()

    def dir(self, key: int) -> Path:
        return self.root / str(key)

    def meta(self, key: int) -> dict | None:
        try:
            return json.loads((self.dir(key) / "meta.json").read_text())
        except (OSError, ValueError):
            return None

    def write_meta(self, key: int, meta: dict, directory: Path | None = None) -> None:
        d = directory or self.dir(key)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"meta.json.{uuid.uuid4().hex[:8]}"
        tmp.write_text(json.dumps(meta))
        os.replace(tmp, d / "meta.json")

    def update_meta(self, key: int, **fields) -> None:
        with self._meta_lock:
            meta = self.meta(key)
            if meta is not None:
                meta.update(fields)
                self.write_meta(key, meta)

    def keys(self) -> list[int]:
        return sorted(int(p.name) for p in self.root.iterdir() if p.name.isdigit() and (p / "meta.json").exists())

    def staging(self, key: int) -> Path:
        d = self.root / f".{key}.{uuid.uuid4().hex[:8]}.tmp"
        d.mkdir(parents=True)
        return d

    def install(self, key: int, staged: Path) -> None:
        """Swap a fully written directory in for the session (old one deleted)."""
        target = self.dir(key)
        old = None
        if target.exists():
            old = self.root / f".{key}.{uuid.uuid4().hex[:8]}.old"
            os.rename(target, old)
        os.rename(staged, target)
        if old:
            shutil.rmtree(old, ignore_errors=True)

    def sweep(self) -> None:
        """Leftovers of downloads interrupted by a restart."""
        for p in self.root.glob(".*.tmp"):
            shutil.rmtree(p, ignore_errors=True)
        for p in self.root.glob(".*.old"):
            shutil.rmtree(p, ignore_errors=True)


class CaptureWriter:
    """Appends live messages to a session directory. Thread-safe: the feed (event loop) and
    browser position uploads (request threads) write through the same writer."""

    def __init__(self, store: SessionStore, key: int):
        self.store = store
        self.key = key
        self.dir = store.dir(key)
        meta = store.meta(key) or {}
        self.t0: float | None = meta["t0"] if meta.get("source") == "live" else None
        # Once the archive is installed nothing may append to its directory.
        self.closed = meta.get("source") == "archive"
        self._files: dict[str, Any] = {}
        self._lock = threading.Lock()

    def write(self, topic: str, payload: Any, utc: float) -> None:
        # .z topics stay base64+deflate text, exactly as the archive stores them.
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            if self.closed:
                return
            if self.t0 is None:
                self.t0 = utc
                self.store.write_meta(self.key, {"key": self.key, "source": "live", "t0": utc, "ended": False,
                                                 "created": time.time()})
            fh = self._files.get(topic)
            if fh is None:
                fh = self._files[topic] = open(self.dir / f"{topic}.jsonStream", "a", encoding="utf-8")
            fh.write(format_offset(utc - self.t0) + line + "\n")
            fh.flush()

    def close(self) -> None:
        with self._lock:
            self.closed = True
            for fh in self._files.values():
                fh.close()
            self._files.clear()


class LiveCapture:
    """Turns one connection's events into stream lines for one session.

    The feed carries whatever session is current, so nothing is written until the keyframes
    say it is ours. Keyframes carry no timestamp; they are stamped with our receive time,
    which is never before the moment they describe (no spoilers), at most a network
    latency after it.
    """

    def __init__(self, key: int, writer: CaptureWriter, store: SessionStore):
        self.key = key
        self.writer = writer
        self.store = store
        self.ours = False
        self.done = False

    def handle(self, ev: livefeed.Keyframes | livefeed.Feed | livefeed.Ping, now: float) -> None:
        if isinstance(ev, livefeed.Keyframes):
            info = ev.topics.get("SessionInfo") or {}
            self.ours = info.get("Key") == self.key
            if not self.ours:
                return
            for topic, payload in ev.topics.items():
                self.writer.write(topic, payload, now)
            self.store.update_meta(self.key, meeting=(info.get("Meeting") or {}).get("Key"), path=info.get("Path"),
                                   name=info.get("Name"), type=info.get("Type"))
            # Connected after the end: the final state is still worth having, but we're done.
            self.done = (ev.topics.get("SessionStatus") or {}).get("Status") == "Ends"
            return
        if not isinstance(ev, livefeed.Feed) or not self.ours:
            return
        if ev.topic == "SessionInfo" and isinstance(ev.payload, dict) and ev.payload.get("Key") not in (None, self.key):
            self.done = True  # the feed moved on to the next session
            return
        self.writer.write(ev.topic, ev.payload, ev.utc)
        if ev.topic == "SessionStatus" and isinstance(ev.payload, dict) and ev.payload.get("Status") == "Ends":
            self.done = True


# ---- the service -----------------------------------------------------------------


@dataclass
class _Attempt:
    at: float
    result: str


@dataclass
class _Loaded:
    timeline: SessionTimeline
    ident: tuple = field(default=())


class TimingService:
    def __init__(self, cfg: Config, mapper=None, settings: TimingSettings | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.mapper = mapper
        self.settings = settings or TimingSettings.from_env()
        self.store = SessionStore(cfg.data_dir / "timing")
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self.schedule: dict[int, ScheduledSession] = {}
        self._schedule_at = -1e18
        self._year_index: dict[int, tuple[float, dict]] = {}
        self._loaded: OrderedDict[int, _Loaded] = OrderedDict()
        self._lock = threading.Lock()
        self._key_locks: dict[int, threading.Lock] = {}
        self._writers: dict[int, CaptureWriter] = {}
        self._captures: dict[int, asyncio.Task] = {}
        self._fetching: dict[int, asyncio.Task] = {}
        self._attempts: dict[int, _Attempt] = {}
        self._last_upload: dict[int, float] = {}
        self._track_miss: dict[int, float] = {}
        self._corner_miss: dict[int, float] = {}
        self._corner_tasks: dict[int, asyncio.Task] = {}
        self._pruned_at = -1e18
        self._fetch_sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self.clock = time.time  # live capture's notion of now (tests replay old sessions)
        self._load_schedule_cache()

    # ---- lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.store.sweep()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._captures.values(), *self._fetching.values()) if t]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        for w in self._writers.values():
            w.close()
        self._writers.clear()
        if self._http:
            await self._http.aclose()
            self._http = None
        self._loop = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30, read=60), headers=UA, follow_redirects=True,
                                           transport=self._transport)
        return self._http

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("timing tick failed")
            await asyncio.sleep(self.settings.tick_seconds)

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        await self._refresh_schedule(now)
        captures, fetches = self.plan(now)
        for key in captures:
            self._captures[key] = asyncio.create_task(self._capture(self.schedule[key]))
        for key in fetches:
            self._spawn_fetch(key)
        if now - self._pruned_at > 3600:
            self._pruned_at = now
            await asyncio.to_thread(self.prune, now)

    def prune(self, now: float) -> list[int]:
        """Delete sessions stored more than keep_days ago (the archive can be fetched again)."""
        gone = []
        for key in self.store.keys():
            meta = self.store.meta(key) or {}
            stored = meta.get("fetched") or meta.get("created") or 0
            if now - stored > self.settings.keep_days * 86400 and key not in self._captures:
                with self._key_lock(key):
                    self._evict(key)
                    shutil.rmtree(self.store.dir(key), ignore_errors=True)
                gone.append(key)
        return gone

    def plan(self, now: float) -> tuple[list[int], list[int]]:
        """Which sessions to start capturing and which archives to (re)try, at `now`."""
        s = self.settings
        captures, fetches = [], []
        for sess in sorted(self.schedule.values(), key=lambda x: x.start):
            live_window = s.record_live and sess.start - s.lead_seconds <= now < sess.start + s.max_record_seconds
            since_end = now - sess.end
            archive_window = 10 * 60 <= since_end < s.archive_days * 86400  # published ~20 min after the end
            if not (live_window or archive_window):
                continue
            meta = self.store.meta(sess.key) or {}
            archived = meta.get("source") == "archive"
            if live_window and not archived and not meta.get("ended") and sess.key not in self._captures:
                captures.append(sess.key)
            if not archive_window or (archived and meta.get("complete")) or sess.key in self._fetching:
                continue
            last = self._attempts.get(sess.key)
            interval = 5 * 60 if since_end < 3 * 3600 else 30 * 60
            if last is None or now - last.at >= interval:
                fetches.append(sess.key)
        return captures, fetches

    # ---- schedule
    def _load_schedule_cache(self) -> None:
        try:
            rows = json.loads((self.store.root / "schedule.json").read_text())
            self._set_schedule(rows)
        except (OSError, ValueError):
            pass

    def _set_schedule(self, rows: list[dict]) -> None:
        sched = {}
        for r in rows:
            try:
                if not r.get("is_cancelled"):
                    s = ScheduledSession.from_openf1(r)
                    sched[s.key] = s
            except (KeyError, TypeError, ValueError):
                continue
        self.schedule = sched

    async def _refresh_schedule(self, now: float) -> None:
        if now - self._schedule_at < (6 * 3600 if self.schedule else 600):
            return
        self._schedule_at = now
        year = datetime.fromtimestamp(now, timezone.utc).year
        try:
            r = await self._client().get(self.settings.schedule_url, params={"year": year})
            r.raise_for_status()
            rows = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("schedule refresh failed: %s", e)
            return
        if isinstance(rows, list) and rows:
            self._set_schedule(rows)
            tmp = self.store.root / "schedule.json.tmp"
            tmp.write_text(json.dumps(rows))
            os.replace(tmp, self.store.root / "schedule.json")

    # ---- live capture
    def _writer(self, key: int) -> CaptureWriter:
        with self._lock:
            w = self._writers.get(key)
            if w is None:
                w = self._writers[key] = CaptureWriter(self.store, key)
            return w

    async def _capture(self, sess: ScheduledSession) -> None:
        deadline = sess.start + self.settings.max_record_seconds
        cap = LiveCapture(sess.key, self._writer(sess.key), self.store)
        client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=60), headers=UA, transport=self._transport)
        log.info("live capture of %s %s starts", sess.key, sess.name)
        finished = False
        fails = 0
        try:
            while self.clock() < deadline and not cap.done:
                got_any = False
                try:
                    async with aclosing(livefeed.stream(client, livefeed.TOPICS, self.settings.live_base)) as events:
                        async for ev in events:
                            got_any = True
                            cap.handle(ev, self.clock())
                            if cap.done or not cap.ours or self.clock() >= deadline:
                                break
                except (httpx.HTTPError, livefeed.FeedError, ValueError, KeyError, TypeError) as e:
                    log.warning("live feed for %s: %s", sess.key, e)
                if cap.done:
                    break
                fails = 0 if got_any and cap.ours else fails + 1
                if cap.ours:  # dropped mid-session: every second counts, reconnect fast (with backoff)
                    await asyncio.sleep(min(self.settings.retry_seconds, 2.0 * 2 ** min(fails, 5)))
                else:  # not our session yet: the feed switches ~15 min before the start
                    await asyncio.sleep(self.settings.retry_seconds)
            finished = True
        finally:
            await client.aclose()
            self._captures.pop(sess.key, None)
            if finished:
                self.store.update_meta(sess.key, ended=True, endedAt=time.time())
                log.info("live capture of %s ended (%s)", sess.key, "Ends" if cap.done else "deadline")

    # ---- archive
    async def _year_sessions(self, year: int) -> dict[int, dict]:
        cached = self._year_index.get(year)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        out: dict[int, dict] = {}
        try:
            r = await self._client().get(f"{self.settings.static_base}/{year}/Index.json")
            if r.status_code == 200:
                for m in json.loads(r.content.decode("utf-8-sig")).get("Meetings", []):
                    for s in m.get("Sessions", []):
                        out[int(s["Key"])] = {**s, "Meeting": m.get("Key")}
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("archive index %s: %s", year, e)
            return cached[1] if cached else {}
        self._year_index[year] = (time.time(), out)
        return out

    async def archive_path(self, key: int) -> str | None:
        meta = self.store.meta(key) or {}
        if meta.get("path"):
            return meta["path"]
        sess = self.schedule.get(key)
        now_year = datetime.now(timezone.utc).year
        years = [sess.year] if sess else list(range(now_year, 2017, -1))
        for year in years:
            found = (await self._year_sessions(year)).get(key)
            if found and found.get("Path"):
                return found["Path"]
        if sess:  # the year index lists a session's path only once archived: derive it from a sibling's
            for s in (await self._year_sessions(sess.year)).values():
                if s.get("Meeting") == sess.meeting and s.get("Path"):
                    meeting_dir = s["Path"].rstrip("/").rsplit("/", 1)[0]
                    h, m, *_ = (int(x) for x in sess.gmt_offset.lstrip("+").split(":"))
                    sign = -1 if sess.gmt_offset.startswith("-") else 1
                    local = datetime.fromtimestamp(sess.start, timezone.utc) + sign * timedelta(hours=abs(h), minutes=m)
                    return f"{meeting_dir}/{local:%Y-%m-%d}_{sess.name.replace(' ', '_')}/"
        return None

    async def fetch_archive(self, key: int) -> str:
        """Download a session from the static archive and install it.
        Returns ok | partial | not published | unknown session | error: ..."""
        if self._fetch_sem is None:
            self._fetch_sem = asyncio.Semaphore(2)
        async with self._fetch_sem:
            path = await self.archive_path(key)
            if not path:
                return "unknown session"
            base = f"{self.settings.static_base}/{path.strip('/')}/"
            client = self._client()
            try:
                r = await client.get(base + "Index.json")
                if r.status_code in (403, 404):
                    return "not published"
                r.raise_for_status()
                feeds = json.loads(r.content.decode("utf-8-sig")).get("Feeds", {})
                staged = self.store.staging(key)
                try:
                    got = await self._download(client, base, feeds, staged)
                    # Off the event loop: installing waits for readers of this session.
                    result = await asyncio.to_thread(self._finish_download, key, path, staged, got)
                finally:
                    if staged.exists():
                        shutil.rmtree(staged, ignore_errors=True)
            except (httpx.HTTPError, ValueError, KeyError, OSError) as e:
                log.warning("archive %s: %s", key, e)
                return f"error: {e}"
        if result in ("ok", "partial"):
            log.info("archive %s installed (%s)", key, result)
        return result

    async def _download(self, client: httpx.AsyncClient, base: str, feeds: dict, staged: Path) -> dict:
        async def get(name: str) -> bytes | None:
            r = await client.get(base + name)
            if r.status_code in (403, 404):
                return None
            r.raise_for_status()
            return r.content

        jobs: list[tuple[str, str]] = []
        for topic in ARCHIVE_STREAMS:
            if topic in feeds:
                jobs.append((feeds[topic]["StreamPath"], f"{topic}.jsonStream"))
        for topic in ARCHIVE_KEYFRAMES:
            if topic in feeds:
                jobs.append((feeds[topic]["KeyFramePath"], f"{topic}.json"))
        sem = asyncio.Semaphore(4)

        async def one(src: str, dst: str) -> tuple[str, bytes | None]:
            async with sem:
                return dst, await get(src)

        got = dict(await asyncio.gather(*(one(s, d) for s, d in jobs)))
        missing = {t for t in REQUIRED_STREAMS if not got.get(f"{t}.jsonStream")}
        if missing:
            raise ValueError(f"archive lacks {sorted(missing)}")
        for name, data in got.items():
            if data is None:
                continue
            if name.endswith(".jsonStream") and not data.endswith(b"\n"):
                data += b"\r\n"  # the reader only takes complete lines
            (staged / name).write_bytes(data)
        status = _keyframe(staged / "ArchiveStatus.json").get("Status")
        return {"t0": _archive_t0(staged), "status": status, "info": _keyframe(staged / "SessionInfo.json")}

    def _finish_download(self, key: int, path: str, staged: Path, got: dict) -> str:
        complete = got["status"] in (None, "Complete")
        current = self.store.meta(key) or {}
        if not complete and current.get("source") == "live":
            return "partial"  # keep the capture until the archive is whole
        info = got["info"]
        self.store.write_meta(key, {
            "key": key, "source": "archive", "t0": got["t0"], "path": path, "complete": complete,
            "meeting": (info.get("Meeting") or {}).get("Key"), "name": info.get("Name"), "type": info.get("Type"),
            "fetched": time.time(),
        }, directory=staged)
        with self._key_lock(key):
            w = self._writers.pop(key, None)
            if w:
                w.close()
            self.store.install(key, staged)
            self._evict(key)
        return "ok" if complete else "partial"

    def _spawn_fetch(self, key: int) -> None:
        if key in self._fetching:
            return
        task = asyncio.create_task(self._fetch_task(key))
        self._fetching[key] = task

    async def _fetch_task(self, key: int) -> None:
        try:
            capture = self._captures.get(key)
            result = await self.fetch_archive(key)
            if result == "ok" and capture:
                capture.cancel()  # Ends never came; the archive supersedes the capture
            self._attempts[key] = _Attempt(time.time(), result)
        finally:
            self._fetching.pop(key, None)

    def _kick_fetch(self, key: int) -> None:
        """From any thread: fetch the archive soon, unless tried within retry_seconds."""
        loop = self._loop
        last = self._attempts.get(key)
        if loop is None or key in self._fetching or (last and time.time() - last.at < self.settings.retry_seconds):
            return
        self._attempts[key] = _Attempt(time.time(), "downloading")
        try:
            loop.call_soon_threadsafe(self._spawn_fetch, key)
        except RuntimeError:
            pass  # loop closed: shutting down

    # ---- loaded sessions (LRU)
    def _key_lock(self, key: int) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def _evict(self, key: int) -> None:
        with self._lock:
            self._loaded.pop(key, None)

    def _ident(self, key: int) -> tuple:
        try:
            st = self.store.dir(key).stat()
        except OSError:
            return ()
        meta = self.store.meta(key) or {}
        return (st.st_ino, meta.get("source"), meta.get("t0"))

    def timeline(self, key: int) -> SessionTimeline | None:
        with self._lock:
            hit = self._loaded.get(key)
            if hit:
                self._loaded.move_to_end(key)
        if hit and hit.ident == self._ident(key):
            return hit.timeline
        with self._key_lock(key):
            meta = self.store.meta(key)
            if not meta or meta.get("t0") is None:
                return None
            ident = self._ident(key)
            started = time.perf_counter()
            tl = SessionTimeline(self.store.dir(key), float(meta["t0"]), self.settings.refresh_seconds)
            log.info("loaded timing %s (%s) in %.2fs", key, meta.get("source"), time.perf_counter() - started)
            with self._lock:
                self._loaded[key] = _Loaded(tl, ident)
                self._loaded.move_to_end(key)
                while len(self._loaded) > self.settings.max_loaded:
                    self._loaded.popitem(last=False)
            return tl

    def _missing_reason(self, key: int) -> str:
        now = time.time()
        sess = self.schedule.get(key)
        if sess and now < sess.start - self.settings.lead_seconds:
            return "session has not started"
        if key in self._captures:
            return "waiting for the live timing feed"
        self._kick_fetch(key)
        last = self._attempts.get(key)
        if last is None or last.result == "downloading" or key in self._fetching:
            return "downloading the timing archive"
        if last.result == "not published":
            return "timing archive not published yet"
        if last.result == "unknown session":
            return "no timing data for this session"
        return f"timing archive unavailable ({last.result})"

    # ---- queries
    def state(self, key: int, t: float) -> dict:
        tl = self.timeline(key)
        if tl is None:
            return {"available": False, "reason": self._missing_reason(key)}
        return tl.state(t)

    def positions(self, key: int, t_from: float, t_to: float) -> dict:
        tl = self.timeline(key)
        if tl is None:
            return {"cars": {}, "available": False, "reason": self._missing_reason(key)}
        t_to = min(t_to, t_from + MAX_POSITION_WINDOW)
        return {"cars": tl.positions(t_from, t_to) if t_to >= t_from else {}}

    def _meeting(self, key: int) -> int | None:
        meta = self.store.meta(key) or {}
        if meta.get("meeting"):
            return int(meta["meeting"])
        sess = self.schedule.get(key)
        return sess.meeting if sess else None

    def track(self, key: int) -> dict:
        meeting = self._meeting(key)
        if meeting is None:
            tl = self.timeline(key)
            meeting = (tl.info().get("Meeting") or {}).get("Key") if tl else None
            if meeting is None:
                return {"available": False, "reason": self._missing_reason(key)}
        cache = self.store.root / "tracks" / f"{meeting}.json"
        try:
            return {"available": True, **json.loads(cache.read_text()), "corners": self._corners(key, meeting)}
        except (OSError, ValueError):
            pass
        if time.time() - self._track_miss.get(meeting, -1e18) < 60:
            return {"available": False, "reason": "no car positions for this weekend yet"}
        # This session first, then the rest of the weekend (archives first, newest first).
        siblings = [k for k in self.store.keys() if k != key and self._meeting(k) == meeting]
        siblings.sort(key=lambda k: ((self.store.meta(k) or {}).get("source") != "archive", -k))
        timelines = [tl for k in [key, *siblings] if (tl := self.timeline(k)) is not None]
        found = track_outline(timelines) if timelines else None
        if not found:
            self._track_miss[meeting] = time.time()
            for s in sorted(self.schedule.values(), key=lambda s: -s.start):  # fetch the weekend's archives
                if s.meeting == meeting and s.end < time.time() and not self.store.meta(s.key):
                    self._kick_fetch(s.key)
                    break
            return {"available": False, "reason": "no car positions for this weekend yet"}
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(found))
        os.replace(tmp, cache)
        return {"available": True, **found, "corners": self._corners(key, meeting)}

    # ---- corner numbers for the map
    def _corners(self, key: int, meeting: int) -> list[dict]:
        """The circuit's numbered corners: [{"n", "l", "x", "y", "a"}] (number, letter, position
        in the car positions' X/Y units, and the direction its label sits in, degrees, y up).
        Cached per meeting; the first ask fetches them in the background and gets []."""
        cache = self.store.root / "tracks" / f"{meeting}.corners.json"
        try:
            return json.loads(cache.read_text())
        except (OSError, ValueError):
            pass
        loop = self._loop
        if loop is None or meeting in self._corner_tasks or time.time() - self._corner_miss.get(meeting, -1e18) < 600:
            return []
        tl = self.timeline(key)
        info = tl.info() if tl else {}
        circuit = ((info.get("Meeting") or {}).get("Circuit") or {}).get("Key")
        year = str(info.get("StartDate") or "")[:4]
        if circuit is None or not year.isdigit():
            self._corner_miss[meeting] = time.time()
            return []
        try:
            loop.call_soon_threadsafe(self._spawn_corners, meeting, int(circuit), int(year), cache)
        except RuntimeError:
            pass  # loop closed: shutting down
        return []

    def _spawn_corners(self, meeting: int, circuit: int, year: int, cache: Path) -> None:
        if meeting not in self._corner_tasks:
            self._corner_tasks[meeting] = asyncio.create_task(self._fetch_corners(meeting, circuit, year, cache))

    async def _fetch_corners(self, meeting: int, circuit: int, year: int, cache: Path) -> None:
        try:
            r = await self._client().get(MV_CIRCUIT.format(circuit=circuit, year=year))
            r.raise_for_status()
            corners = [{"n": int(c["number"]), "l": str(c.get("letter") or ""),
                        "x": round(float(c["trackPosition"]["x"]), 1), "y": round(float(c["trackPosition"]["y"]), 1),
                        "a": round(float(c["angle"]), 1)} for c in r.json().get("corners") or []]
            if not corners:
                raise ValueError("no corners in the circuit data")
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(corners))
            os.replace(tmp, cache)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError) as e:
            log.warning("corners for meeting %s (circuit %s, %s): %s", meeting, circuit, year, e)
            self._corner_miss[meeting] = time.time()
        finally:
            self._corner_tasks.pop(meeting, None)

    def ingest_positions(self, key: int, messages: list[dict]) -> dict:
        """Decoded Position.z batches from the browser -> the session's Position.z stream,
        re-encoded exactly like the archive so both load the same way."""
        meta = self.store.meta(key) or {}
        if meta.get("source") == "archive":
            return {"ok": True, "stored": 0, "reason": "archived"}
        clean = []
        for m in messages[:MAX_UPLOAD_MESSAGES]:
            try:
                ts = iso(m["Timestamp"])
                entries = {
                    str(num): {"X": int(e["X"]), "Y": int(e["Y"]), "Z": int(e.get("Z") or 0),
                               "Status": str(e.get("Status") or "")}
                    for num, e in m["Entries"].items()
                }
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            clean.append((ts, m["Timestamp"], entries))
        clean.sort(key=lambda c: c[0])
        with self._key_lock(key):
            last = self._last_upload.get(key, -1e18)
            fresh = [c for c in clean if c[0] > last]  # several tabs may upload the same samples
            if not fresh:
                return {"ok": True, "stored": 0}
            payload = deflate({"Position": [{"Timestamp": raw, "Entries": e} for _, raw, e in fresh]})
            self._writer(key).write(POSITION_TOPIC, payload, fresh[-1][0])
            self._last_upload[key] = fresh[-1][0]
        return {"ok": True, "stored": len(fresh)}

    def status(self) -> dict:
        now = time.time()
        upcoming = [s for s in sorted(self.schedule.values(), key=lambda s: s.start) if s.end > now - 86400][:6]
        return {
            "now": now,
            "capturing": sorted(self._captures),
            "fetching": sorted(self._fetching),
            "loaded": list(self._loaded),
            "sessions": {k: {x: v for x, v in (self.store.meta(k) or {}).items() if x != "key"} for k in self.store.keys()},
            "schedule": [{"key": s.key, "name": s.name, "meeting": s.meeting, "start": s.start, "end": s.end}
                         for s in upcoming],
            "attempts": {k: {"at": a.at, "result": a.result} for k, a in self._attempts.items()},
        }


def _keyframe(path: Path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8-sig"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _archive_t0(d: Path) -> float:
    """Archive start: AudioStreams / ContentStreams keyframe "Utc" (exact); else SessionData,
    whose every Utc equals t0 + its line's offset."""
    for name in ("AudioStreams.json", "ContentStreams.json"):
        for s in _keyframe(d / name).get("Streams") or []:
            if str(s.get("Utc", "")).startswith("20"):
                return iso(s["Utc"])
    for off, raw in read_stream(d / "SessionData.jsonStream"):
        m = json.loads(raw)
        for k in ("Series", "StatusSeries"):
            v = m.get(k)
            for e in (v.values() if isinstance(v, dict) else v if isinstance(v, list) else []):
                if isinstance(e, dict) and e.get("Utc"):
                    return iso(e["Utc"]) - off
    raise ValueError("no t0 in archive")


# ---- routes ----------------------------------------------------------------------


class PositionsBody(BaseModel):
    messages: list[dict] = Field(default_factory=list, max_length=MAX_UPLOAD_MESSAGES)


SessionKey = Annotated[int, PathParam(gt=0, lt=10_000_000)]


def make_router(svc: TimingService) -> APIRouter:
    """Sync handlers on purpose: loading a session takes ~1 s, so they run in the threadpool."""
    r = APIRouter(prefix="/api/timing")

    @r.get("/session/{key}/state")
    def state(key: SessionKey, t: float):
        return svc.state(key, t)

    @r.get("/session/{key}/positions")
    def positions(key: SessionKey, t_from: Annotated[float, Query(alias="from")],
                  t_to: Annotated[float, Query(alias="to")]):
        return svc.positions(key, t_from, t_to)

    @r.get("/session/{key}/track")
    def track(key: SessionKey):
        return svc.track(key)

    @r.post("/session/{key}/positions")
    def ingest(key: SessionKey, body: PositionsBody):
        return svc.ingest_positions(key, body.messages)

    @r.get("/content/{cid}")
    async def content(cid: str):
        if not cid.isdigit():
            raise HTTPException(400, "content id must be numeric")
        if svc.mapper is None:
            raise HTTPException(503, "no F1 TV mapper")
        return await svc.mapper.content(cid)

    @r.get("/status")
    def status():
        return svc.status()

    return r

