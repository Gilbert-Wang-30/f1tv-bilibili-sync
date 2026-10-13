"""Danmaku (弹幕) of the recorded Bilibili rooms, served by wall-clock time, so the extension
can scroll them over F1 TV in step with the commentary.

Sources:
  capture  our own anonymous connection to each room's danmaku feed (danmaku_feed.Feed),
           written as JSON lines to <data>/danmaku/capture/<room>-<UTC yyyymmdd>.jsonl:
           {"rx", "m": message} for each message kept, {"rx", "ev": "open"|"beat"} while
           connected. The indexer tails those files into the database; the open/beat lines
           say when we were listening.
  archive  danmakus.com's record of each finished live (send time, uid, text; no levels,
           colours or emoticons), for what the capture didn't hear: before it existed, or
           while it was disconnected.

GET /api/danmaku?channel=&from=&to=   the items sent in [from, to) (unix s), oldest first:
  {"t" send time, "x" text, "m" mode (1 scroll, 4 bottom, 5 top), "s" size, "c" colour,
   "u" user hash}, and from the capture only: "ul" user level, "ml" fan-medal level (this
  room's medal), "wl" honour level (荣耀等级), "g" guard (3 舰长, 2 提督, 1 总督), "e" {"u", "w",
  "h"} an emoticon danmaku's image, "em" {"[code]": url} inline emoji; a super chat has
  "k": "sc" and "p" its price. Plus "live": whether we're listening to the room right now.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from .config import OFF, Config

log = logging.getLogger("fone.danmaku")

KEEP_DAYS = 30  # items
KEEP_RAW_DAYS = 7  # capture files (indexed by then)
SPAN_GAP = 75.0  # s without a line (heartbeats every 30 s): the capture wasn't listening
SPAN_SLACK = (5.0, 35.0)  # around a span, still covered: archive items there are the capture's
ARCHIVE_EVERY = 600.0
ARCHIVE_SETTLE = 900.0  # import a live this long after it ended (the archive completes it)
ARCHIVE_PER_TICK = 3
MAX_ITEMS = 6000  # per request (the oldest first)
KEEP_CMDS = ("DANMU_MSG", "SUPER_CHAT_MESSAGE", "LIVE", "PREPARING")
MODES = (1, 4, 5)


@dataclass(frozen=True)
class Settings:
    capture: bool = True  # listen to the rooms' feeds (FONE_DANMAKU_CAPTURE)
    archive: bool = True  # fill in from danmakus.com (FONE_DANMAKU_ARCHIVE)

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(capture=env("FONE_DANMAKU_CAPTURE", "1").strip().lower() not in OFF,
                   archive=env("FONE_DANMAKU_ARCHIVE", "1").strip().lower() not in OFF)


# ---- messages -> items -------------------------------------------------------------------------


def _num(x, default=None):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else default


def danmu_item(info: list, rx: float, room: int) -> dict | None:
    """A DANMU_MSG's info array -> item (None if it isn't one we can show)."""
    try:
        i0, text = info[0], info[1]
        if not isinstance(text, str) or not text.strip():
            return None
        sent = _num(i0[4])
        it: dict = {"t": round(sent / 1000, 3) if sent and sent > 1e12 else round(rx, 3), "x": text}
        mode = _num(i0[1], 1)
        if mode in MODES and mode != 1:
            it["m"] = mode
        size = _num(i0[2], 25)
        if size != 25:
            it["s"] = size
        color = _num(i0[3], 0xFFFFFF)
        if color != 0xFFFFFF:
            it["c"] = color
        if isinstance(i0[7], str) and i0[7]:
            it["u"] = i0[7]
        if isinstance(info[4], list) and info[4]:
            it["ul"] = _num(info[4][0], 0)
        medal = info[3]
        it["ml"] = _num(medal[0], 0) if isinstance(medal, list) and len(medal) > 3 and medal[3] == room else 0
        if len(info) > 16 and isinstance(info[16], list) and info[16]:
            it["wl"] = _num(info[16][0], 0)
        if _num(info[7], 0):
            it["g"] = info[7]
        emo = i0[13] if len(i0) > 13 else None
        if _num(i0[12], 0) == 1 and isinstance(emo, dict) and emo.get("url"):
            it["e"] = {"u": emo["url"], "w": _num(emo.get("width"), 0), "h": _num(emo.get("height"), 0)}
        extra = i0[15].get("extra") if len(i0) > 15 and isinstance(i0[15], dict) else None
        if isinstance(extra, str) and "emots" in extra:
            emots = json.loads(extra).get("emots")
            if isinstance(emots, dict):
                em = {k: v["url"] for k, v in emots.items() if isinstance(v, dict) and v.get("url")}
                if em:
                    it["em"] = em
        return it
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def superchat_item(data: dict, rx: float) -> dict | None:
    try:
        text = data.get("message")
        if not isinstance(text, str) or not text.strip():
            return None
        it = {"t": float(_num(data.get("start_time"), rx)), "x": text, "k": "sc", "p": _num(data.get("price"), 0)}
        uid = _num(data.get("uid"))
        if uid:
            it["u"] = format(zlib.crc32(str(uid).encode()), "x")
        return it
    except (TypeError, ValueError):
        return None


def message_item(m: dict, rx: float, room: int) -> dict | None:
    cmd = str(m.get("cmd", "")).split(":")[0]
    if cmd == "DANMU_MSG" and isinstance(m.get("info"), list):
        return danmu_item(m["info"], rx, room)
    if cmd == "SUPER_CHAT_MESSAGE" and isinstance(m.get("data"), dict):
        return superchat_item(m["data"], rx)
    return None


def archive_items(raw: bytes) -> list[dict]:
    """danmakus.com /v3/lives/<id>/full (msgpack, see backfill_start.parse_dmks_live): every
    record [delta ms, kind, actor (1-based), _, type, payload]; type 1 a danmaku ([text]),
    type 5 a super chat ([text, price])."""
    from .backfill_start import unpackb

    d = unpackb(raw)
    _, actors, _, recs = d[2]
    base = d[2][0]
    out, s = [], base
    for r in recs:
        s += r[0] or 0
        if len(r) < 6 or r[4] not in (1, 5) or not r[5] or not isinstance(r[5][0], str) or not r[5][0].strip():
            continue
        it: dict = {"t": round(s / 1000, 3), "x": r[5][0]}
        if r[2] and 0 < r[2] <= len(actors):
            it["u"] = format(zlib.crc32(str(actors[r[2] - 1][0]).encode()), "x")
        if r[4] == 5:
            it.update(k="sc", p=r[5][1] if len(r[5]) > 1 else 0)
        out.append(it)
    return out


# ---- the database ------------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS dm (room INTEGER NOT NULL, t REAL NOT NULL, u TEXT NOT NULL, x TEXT NOT NULL,
  src TEXT NOT NULL, j TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS dm_key ON dm (room, t, u, x);
-- when the capture was listening: closed spans, and each capture file's open one
CREATE TABLE IF NOT EXISTS dm_span (room INTEGER NOT NULL, a REAL NOT NULL, b REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dm_file (name TEXT PRIMARY KEY, room INTEGER NOT NULL, pos INTEGER NOT NULL,
  a REAL, b REAL);
CREATE TABLE IF NOT EXISTS dm_live (live TEXT PRIMARY KEY, room INTEGER NOT NULL, start REAL, stop REAL,
  n INTEGER NOT NULL, at REAL NOT NULL);
"""


class DanmakuStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def add(self, room: int, items: list[dict], src: str) -> int:
        rows = [(room, it["t"], it.get("u", ""), it["x"], src, json.dumps(it, ensure_ascii=False, separators=(",", ":")))
                for it in items]
        with self.lock, self.db:
            before = self.db.total_changes
            self.db.executemany("INSERT OR IGNORE INTO dm VALUES (?, ?, ?, ?, ?, ?)", rows)
            return self.db.total_changes - before

    def range(self, room: int, a: float, b: float, limit: int = MAX_ITEMS) -> list[str]:
        with self.lock:
            return [j for (j,) in self.db.execute(
                "SELECT j FROM dm WHERE room = ? AND t >= ? AND t < ? ORDER BY t LIMIT ?", (room, a, b, limit))]

    def spans(self, room: int) -> list[tuple[float, float]]:
        with self.lock:
            rows = self.db.execute("SELECT a, b FROM dm_span WHERE room = ? UNION ALL "
                                   "SELECT a, b FROM dm_file WHERE room = ? AND a IS NOT NULL", (room, room)).fetchall()
        return sorted(rows)

    def file(self, name: str) -> tuple[int, float | None, float | None] | None:
        with self.lock:
            r = self.db.execute("SELECT pos, a, b FROM dm_file WHERE name = ?", (name,)).fetchone()
        return tuple(r) if r else None

    def save_file(self, name: str, room: int, pos: int, span: list | None, closed: list[tuple[float, float]]) -> None:
        with self.lock, self.db:
            self.db.executemany("INSERT INTO dm_span VALUES (?, ?, ?)", [(room, a, b) for a, b in closed])
            self.db.execute("INSERT OR REPLACE INTO dm_file VALUES (?, ?, ?, ?, ?)",
                            (name, room, pos, *(span or (None, None))))

    def imported(self, live: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM dm_live WHERE live = ?", (live,)).fetchone() is not None

    def mark_imported(self, live: str, room: int, start: float, stop: float | None, n: int) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO dm_live VALUES (?, ?, ?, ?, ?, ?)",
                            (live, room, start, stop, n, time.time()))

    def prune(self, before: float) -> int:
        with self.lock, self.db:
            n = self.db.execute("DELETE FROM dm WHERE t < ?", (before,)).rowcount
            self.db.execute("DELETE FROM dm_span WHERE b < ?", (before,))
        return n


def covered(spans: list[tuple[float, float]], t: float) -> bool:
    return any(a - SPAN_SLACK[0] <= t <= b + SPAN_SLACK[1] for a, b in spans)


# ---- the capture files -> database ---------------------------------------------------------------


def index_file(store: DanmakuStore, path: Path) -> int:
    """New complete lines of one capture file into the database; returns items added."""
    name = path.name
    try:
        room = int(name.split("-", 1)[0])
    except ValueError:
        return 0
    prev = store.file(name)
    pos, a, b = prev if prev else (0, None, None)
    size = path.stat().st_size
    if size <= pos:
        return 0
    with path.open("rb") as f:
        f.seek(pos)
        chunk = f.read(size - pos)
    end = chunk.rfind(b"\n") + 1
    if end <= 0:
        return 0
    span = [a, b] if a is not None else None
    closed: list[tuple[float, float]] = []
    items: list[dict] = []
    for line in chunk[:end].splitlines():
        try:
            rec = json.loads(line)
            rx = float(rec["rx"])
        except (ValueError, KeyError, TypeError):
            continue
        if rec.get("ev") == "open" or (span and rx - span[1] > SPAN_GAP):
            if span:
                closed.append((span[0], span[1]))
            span = [rx, rx]
        elif span is None:
            span = [rx, rx]
        else:
            span[1] = max(span[1], rx)
        m = rec.get("m")
        if isinstance(m, dict):
            it = message_item(m, rx, room)
            if it:
                items.append(it)
    n = store.add(room, items, "capture") if items else 0
    store.save_file(name, room, pos + end, span, closed)
    return n


# ---- the service ---------------------------------------------------------------------------------


class DanmakuService:
    def __init__(self, cfg: Config, settings: Settings | None = None):
        self.cfg = cfg
        self.settings = settings or Settings.from_env()
        self.rooms = dict(cfg.rooms)  # room -> channel
        self.root = cfg.data_dir / "danmaku"
        self.capture_dir = self.root / "capture"
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.store = DanmakuStore(self.root / "danmaku.sqlite3")
        self.feeds: dict = {}
        self.uids: dict[int, int] = {}
        self.tasks: list[asyncio.Task] = []
        self.status = {"indexed": 0, "archive": None, "archived": 0}

    def room_of(self, channel: str) -> int | None:
        return next((r for r, c in self.rooms.items() if c == channel), None)

    def live(self, room: int) -> bool:
        f = self.feeds.get(room)
        return bool(f and f.connected)

    async def start(self) -> None:
        self.tasks.append(asyncio.create_task(self._index_loop()))
        if self.settings.capture:
            for room in self.rooms:
                self.tasks.append(asyncio.create_task(self._capture_loop(room)))
        if self.settings.archive and self.rooms:
            self.tasks.append(asyncio.create_task(self._archive_loop()))
        log.info("danmaku: rooms %s (capture %s, archive %s)", ", ".join(map(str, self.rooms)) or "none",
                 self.settings.capture, self.settings.archive)

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for f in self.feeds.values():
            await f.close()

    # -- index ------------------------------------------------------------------------------------

    def index_once(self) -> int:
        n = 0
        for path in sorted(self.capture_dir.glob("*.jsonl")):
            try:
                n += index_file(self.store, path)
            except OSError as e:
                log.warning("danmaku: can't read %s: %s", path.name, e)
        self.status["indexed"] += n
        return n

    def prune_once(self, now: float) -> None:
        self.store.prune(now - KEEP_DAYS * 86400)
        cutoff = datetime.fromtimestamp(now - KEEP_RAW_DAYS * 86400, timezone.utc).strftime("%Y%m%d")
        for path in self.capture_dir.glob("*.jsonl"):
            day = path.stem.rsplit("-", 1)[-1]
            if day.isdigit() and day < cutoff:
                path.unlink(missing_ok=True)

    async def _index_loop(self) -> None:
        pruned = 0.0
        while True:
            try:
                await asyncio.to_thread(self.index_once)
                if time.time() - pruned > 86400:
                    pruned = time.time()
                    await asyncio.to_thread(self.prune_once, pruned)
            except Exception:
                log.exception("danmaku: indexing failed")
            await asyncio.sleep(1)

    # -- capture ----------------------------------------------------------------------------------

    def _write(self, room: int, rx: float, lines: list[dict]) -> None:
        day = datetime.fromtimestamp(rx, timezone.utc).strftime("%Y%m%d")
        with (self.capture_dir / f"{room}-{day}.jsonl").open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n" for x in lines)

    async def _capture_loop(self, room: int) -> None:
        from .danmaku_feed import Feed

        feed = self.feeds[room] = Feed(room)
        backoff = 5.0
        while True:
            t0 = time.time()
            try:
                def on_messages(rx: float, msgs: list[dict]) -> None:
                    keep = [{"rx": round(rx, 3), "m": m} for m in msgs if str(m.get("cmd", "")).split(":")[0] in KEEP_CMDS]
                    if keep:
                        self._write(room, rx, keep)

                await feed.run(on_messages, lambda ev: self._write(room, time.time(), [{"rx": round(time.time(), 3), "ev": ev}]))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network, Bilibili refusing, connection dropped: start over
                log.warning("danmaku: room %s feed: %s: %s", room, type(e).__name__, e)
            backoff = 5.0 if time.time() - t0 > 300 else min(backoff * 2, 300.0)
            await asyncio.sleep(backoff)

    # -- archive ----------------------------------------------------------------------------------

    async def _uid(self, room: int) -> int | None:
        if room not in self.uids:
            from .backfill_bili import BiliClient

            client = BiliClient()
            try:
                self.uids[room] = (await client.room_info(room)).uid or 0
            finally:
                await client.close()
        return self.uids[room] or None

    async def archive_once(self, http, now: float | None = None) -> int:
        from .backfill_bili import dmks_live_full, dmks_lives

        now = now or time.time()
        done = 0
        for room in self.rooms:
            uid = await self._uid(room)
            if not uid:
                continue
            lives = await dmks_lives(http, uid)
            todo = [x for x in lives if x["stop"] and now - x["stop"] > ARCHIVE_SETTLE
                    and x["stop"] > now - KEEP_DAYS * 86400 and not self.store.imported(x["id"])]
            for x in sorted(todo, key=lambda x: -x["start"])[:ARCHIVE_PER_TICK - done]:
                if not x["danmaku"]:
                    self.store.mark_imported(x["id"], room, x["start"], x["stop"], 0)
                    continue
                items = archive_items(await dmks_live_full(http, x["id"]))
                spans = self.store.spans(room)
                fresh = [it for it in items if not covered(spans, it["t"])]
                n = self.store.add(room, fresh, "archive")
                self.store.mark_imported(x["id"], room, x["start"], x["stop"], n)
                self.status["archived"] += n
                log.info("danmaku: archived live %s (%s): %d of %d items%s", x["id"][:8], x["title"], n, len(items),
                         f" ({len(items) - len(fresh)} heard live)" if len(fresh) < len(items) else "")
                done += 1
            if done >= ARCHIVE_PER_TICK:
                break
        return done

    async def _archive_loop(self) -> None:
        import httpx

        await asyncio.sleep(60)
        async with httpx.AsyncClient(timeout=60) as http:
            while True:
                try:
                    await self.archive_once(http)
                    self.status["archive"] = "ok"
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.status["archive"] = f"{type(e).__name__}: {e}"
                    log.warning("danmaku: archive: %s", self.status["archive"])
                await asyncio.sleep(ARCHIVE_EVERY)


def make_router(svc: DanmakuService) -> APIRouter:
    r = APIRouter(prefix="/api/danmaku")

    @r.get("")
    def get(channel: str, start: Annotated[float, Query(alias="from")], end: Annotated[float, Query(alias="to")],
            limit: Annotated[int, Query(ge=1, le=MAX_ITEMS)] = MAX_ITEMS):
        room = svc.room_of(channel)
        if room is None:
            raise HTTPException(404, f"no Bilibili room for channel {channel!r}")
        items = svc.store.range(room, start, end, limit) if end > start else []
        body = ('{"items":[' + ",".join(items) + f'],"live":{json.dumps(svc.live(room))},"room":{room}}}')
        return Response(body, media_type="application/json")

    @r.get("/status")
    def status():
        return {"rooms": {str(r): {"channel": c, "live": svc.live(r)} for r, c in svc.rooms.items()}, **svc.status}

    return r
