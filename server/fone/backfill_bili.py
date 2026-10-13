"""Bilibili access for backfill: live status, the '直播回放' series, DASH downloads.

Everything here works without a login:
  * room/v1/Room/get_info              live_status (1 = live; 2 = rerun loop, NOT live) + live_time (开播)
  * x/polymer/web-space/seasons_series_list -> the uploader's auto-created '直播回放' series id
  * x/series/archives                  the series, newest first
  * x/player/pagelist, x/player/playurl (non-WBI, try_look=1) -> DASH m4s up to 1080p + AAC

Replay titles are '【直播回放】<room title at 开播> YYYY年MM月DD日HH点场' (HH = Beijing hour of 开播),
so a replay's start is only known to an hour slot until something pins it (our own live_time log).
A stream stopped and restarted yields one replay per restart; replays over ~2 h are split into
pages, each a whole number of ~30-min fragments, every page after the first starting with a ~5 s
repeat of the previous one.

Signed CDN URLs embed the client IP and die ~120 min after playurl, and mirror speed depends on
the (mirror, file) pair (an edge that hasn't cached the file can crawl at 0.5 MB/s while another
serves 30 MB/s), hence the ranking, refreshing and per-chunk mirror rotation in `Downloader`.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger("fone.backfill")

SH = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
SERIES_NAME = "直播回放"
TITLE_RE = re.compile(r"^【直播回放】(?P<title>.*?)\s*(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日(?P<h>\d{1,2})点场$")
AUDIO_PREF = (30280, 30232, 30216)  # 192k/132k/64k nominal; actual ~130/74/67 kbps
AVC = 7
API_MIN_INTERVAL = 2.0  # this IP also runs the recorders: be gentle with the API (CDN is not throttled)
RISK_CODES = (-352, -412, -799)
# Each page after the first starts by repeating the previous page's last ~5 s; fragments
# inside a page repeat ~5 s every ~1805 s. Good enough for page selection, not for timing.
PART_OVERLAP = 5.1
FRAGMENT_REPEAT = 5.0
FRAGMENT_SECONDS = 1800


class RiskControl(RuntimeError):
    """Bilibili answered 412 / -352 / -412 / -799: back off for minutes, don't retry quickly."""


class BiliError(RuntimeError):
    pass


def parse_live_time(s: str | int | None) -> float | None:
    """'2026-10-03 15:03:11' (Beijing) -> unix; '0000-00-00 00:00:00' / 0 -> None."""
    if not s or (isinstance(s, str) and s.startswith("0000")):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH).timestamp()


@dataclass
class RoomStatus:
    room: int
    uid: int
    live_status: int
    live_time: float | None
    title: str

    @property
    def live(self) -> bool:
        return self.live_status == 1


def room_status(room: int, data: dict) -> RoomStatus:
    return RoomStatus(room=room, uid=int(data.get("uid") or 0), live_status=int(data.get("live_status") or 0),
                      live_time=parse_live_time(data.get("live_time")), title=data.get("title") or "")


# ---- the API ----------------------------------------------------------------------------------


class BiliClient:
    """Rate-limited JSON API client (one request at a time, >= 2 s apart)."""

    def __init__(self, http: httpx.AsyncClient | None = None, min_interval: float = API_MIN_INTERVAL):
        self.http = http or httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}, follow_redirects=True)
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.calls = 0

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, url: str, params: dict | None, referer: str) -> httpx.Response:
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            origin = "/".join(referer.split("/", 3)[:3])  # https://host
            try:
                return await self.http.get(url, params=params,
                                           headers={"User-Agent": UA, "Referer": referer, "Origin": origin})
            finally:
                self._last = time.monotonic()
                self.calls += 1

    async def api(self, url: str, params: dict | None = None, referer: str = "https://www.bilibili.com/") -> dict:
        r = await self._get(url, params, referer)
        short = url.split("?")[0]
        if r.status_code == 412:
            raise RiskControl(f"HTTP 412 from {short}")
        if r.status_code != 200:
            raise BiliError(f"HTTP {r.status_code} from {short}")
        j = r.json()
        if j.get("code") in RISK_CODES:
            raise RiskControl(f"code {j.get('code')} from {short}")
        if j.get("code") != 0:
            raise BiliError(f"{short}: code {j.get('code')} {j.get('message') or j.get('msg')}")
        return j["data"]

    async def room_info(self, room: int) -> RoomStatus:
        d = await self.api("https://api.live.bilibili.com/room/v1/Room/get_info", {"room_id": room},
                           referer=f"https://live.bilibili.com/{room}")
        return room_status(room, d)

    async def series_id(self, mid: int) -> int:
        d = await self.api("https://api.bilibili.com/x/polymer/web-space/seasons_series_list",
                           {"mid": mid, "page_num": 1, "page_size": 20}, referer=f"https://space.bilibili.com/{mid}/")
        return find_series_id(d)

    async def series_archives(self, mid: int, sid: int, since: float, max_pages: int = 3) -> list[dict]:
        """Archives of the series published at/after `since`, newest first (usually one request)."""
        out: list[dict] = []
        for pn in range(1, max_pages + 1):
            d = await self.api("https://api.bilibili.com/x/series/archives",
                               {"mid": mid, "series_id": sid, "only_normal": "true", "sort": "desc", "pn": pn, "ps": 50},
                               referer=f"https://space.bilibili.com/{mid}/")
            arcs = d.get("archives") or []
            out += arcs
            if not arcs or arcs[-1]["pubdate"] < since or pn * 50 >= (d.get("page") or {}).get("total", 0):
                break
        return out

    async def pagelist(self, bvid: str) -> list[dict]:
        pages = await self.api("https://api.bilibili.com/x/player/pagelist", {"bvid": bvid})
        return sorted(pages, key=lambda p: p["page"])

    async def playurl(self, bvid: str, cid: int) -> dict:
        d = await self.api("https://api.bilibili.com/x/player/playurl",
                           {"bvid": bvid, "cid": cid, "fnval": 4048, "fnver": 0, "fourk": 1, "qn": 0, "try_look": 1},
                           referer=f"https://www.bilibili.com/video/{bvid}/")
        if not d.get("dash"):
            raise BiliError(f"{bvid}/{cid}: playurl has no DASH streams")
        return d["dash"]

    async def dm_xml(self, cid: int) -> bytes:
        """The replay's danmaku XML (raw-deflate encoded; httpx inflates it)."""
        r = await self._get("https://api.bilibili.com/x/v1/dm/list.so", {"oid": cid}, "https://www.bilibili.com/")
        if r.status_code != 200:
            raise BiliError(f"HTTP {r.status_code} from dm/list.so")
        return r.content


def find_series_id(seasons_series_list: dict) -> int:
    for s in (seasons_series_list.get("items_lists") or {}).get("series_list") or []:
        if s["meta"]["name"] == SERIES_NAME:
            return int(s["meta"]["series_id"])
    raise BiliError("no 直播回放 series on this space (replays disabled?)")


# ---- listing & matching -----------------------------------------------------------------------


@dataclass
class Replay:
    bvid: str
    title: str
    live_title: str
    slot: float  # unix time of the Beijing hour in the title ('15点场' -> 15:00 CST)
    duration: int  # seconds, sum of pages (includes the ~5 s repeats)
    pubdate: int
    # Feasible start (开播) intervals, narrowed by `match`.
    iv: list[tuple[float, float]] = field(default_factory=list)

    @property
    def content(self) -> float:
        """Rough de-duplicated length: ~5 s repeated per ~1800 s fragment."""
        return self.duration - FRAGMENT_REPEAT * int(self.duration // FRAGMENT_SECONDS)

    @property
    def exact_start(self) -> float | None:
        return self.iv[0][0] if self.iv and self.iv[0][0] == self.iv[-1][1] else None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_replay(a: dict) -> Replay | None:
    m = TITLE_RE.match((a.get("title") or "").strip())
    if not m:
        return None
    slot = datetime(int(m["y"]), int(m["m"]), int(m["d"]), int(m["h"]), tzinfo=SH).timestamp()
    return Replay(a["bvid"], a["title"], m["title"].strip(), slot, int(a["duration"]), int(a["pubdate"]))


def parse_replays(archives: list[dict]) -> list[Replay]:
    return [r for a in archives if (r := parse_replay(a))]


def hour_of(t: float) -> float:
    return datetime.fromtimestamp(t, SH).replace(minute=0, second=0, microsecond=0).timestamp()


def _overlap(x: float, r: Replay, t0: float, t1: float) -> float:
    return min(t1, x + r.content) - max(t0, x)


@dataclass
class Hit:
    replay: Replay
    kind: str  # sure | maybe
    start: float  # best estimate of the replay's start (exact when pinned)
    min_overlap: float


def match(replays: list[Replay], t0: float, t1: float, title: str | None = None, live_start: float | None = None,
          sessions: list[tuple[float, float]] | None = None, tol: float = 120.0) -> list[Hit]:
    """Replays overlapping [t0, t1], each with how sure we are and where it starts.

    A replay's start is only known to lie in its title's hour slot. Narrowed by:
      * chain: one room, so replay B starts after replay A ends (A in an earlier slot);
      * sessions: known [开播, 下播] spans (our live log) - a replay lies inside one session,
        and the first replay of a session starts exactly at its 开播;
      * live_start: the exact 开播 of the stream in question pins the replay of that hour.
    """
    reps = sorted((Replay(**{**asdict(r), "iv": []}) for r in replays), key=lambda r: (r.slot, r.pubdate))
    for r in reps:
        r.iv = [(r.slot, r.slot + 3599.0)]
        if sessions:
            fits = [(max(r.slot, s), min(r.slot + 3599.0, e - r.content + tol)) for s, e in sessions]
            r.iv = [x for x in fits if x[0] <= x[1]] or r.iv  # no session fits: the log is incomplete
    pins = [(s, False) for s, _ in (sessions or [])] + ([(live_start, True)] if live_start is not None else [])
    for _ in range(4):
        lo = {id(r): r.iv[0][0] for r in reps}
        hi = {id(r): r.iv[-1][1] for r in reps}
        for a, c in zip(reps, reps[1:]):  # forward then backward propagation along the chain
            lo[id(c)] = max(lo[id(c)], lo[id(a)] + a.content - tol)
        for a, c in reversed(list(zip(reps, reps[1:]))):
            hi[id(a)] = min(hi[id(a)], hi[id(c)] - a.content + tol)
        for r in reps:
            iv = [(max(x, lo[id(r)]), min(y, hi[id(r)])) for x, y in r.iv]
            r.iv = [x for x in iv if x[0] <= x[1]] or [(lo[id(r)], max(lo[id(r)], hi[id(r)]))]
        for s, is_query in pins:
            cands = [r for r in reps if r.slot == hour_of(s) and any(x - tol <= s <= y + tol for x, y in r.iv)]
            if is_query and title:
                cands = [r for r in cands if r.live_title == title] or cands
            if cands:
                cands[0].iv = [(s, s)]
    out = []
    for r in reps:
        xs = [p for x, y in r.iv for p in (x, y, min(max(t0, x), y), min(max(t1 - r.content, x), y))]
        best = max(_overlap(x, r, t0, t1) for x in xs)
        worst = min(_overlap(x, r, t0, t1) for iv in r.iv for x in iv)
        if best <= 0:
            continue
        kind = "sure" if worst > 0 or (title and r.live_title == title) else "maybe"
        est = r.exact_start if r.exact_start is not None else max(xs, key=lambda x: _overlap(x, r, t0, t1))
        out.append(Hit(r, kind, est, max(0.0, worst)))
    return out


def pages_for_window(start_lo: float, start_hi: float, pages: list[dict], t0: float, t1: float,
                     margin: float = 60.0) -> list[dict]:
    """Contiguous run of pages whose wall span can overlap [t0, t1]."""
    keep, off = [], 0.0
    for p in pages:
        if start_lo + off - margin <= t1 and start_hi + off + p["duration"] + margin >= t0:
            keep.append(p)
        off += p["duration"] - PART_OVERLAP
    if not keep:
        return []
    nums = [p["page"] for p in keep]
    return [p for p in pages if min(nums) <= p["page"] <= max(nums)]


def pick_stream(dash: dict, kind: str, max_height: int | None = None) -> dict:
    """Audio: best AAC we know of. Video: AVC (ffmpeg- and FLV-friendly), tallest <= max_height."""
    if kind == "audio":
        by = {a["id"]: a for a in dash.get("audio") or []}
        for i in AUDIO_PREF:
            if i in by:
                return by[i]
        raise BiliError("no AAC audio stream offered")
    vids = [v for v in dash.get("video") or [] if v.get("codecid") == AVC and v["height"] <= (max_height or 10**9)]
    if not vids:
        raise BiliError(f"no AVC video stream <= {max_height}p offered")
    return max(vids, key=lambda v: (v["height"], v["bandwidth"]))


def stream_urls(s: dict) -> list[str]:
    return [s.get("baseUrl") or s["base_url"]] + list(s.get("backupUrl") or s.get("backup_url") or [])


# ---- danmakus.com (third-party archive of every 开播/下播 and live danmaku) -----------------------


DMKS = "https://api.ukamnads.icu/api"
DMKS_HEADERS = {"User-Agent": UA, "Origin": "https://danmakus.com", "Referer": "https://danmakus.com/"}


async def dmks_lives(http: httpx.AsyncClient, uid: int) -> list[dict]:
    """[{id, start, stop, title, danmaku}] - startDate equals Bilibili's live_time. Restarts
    minutes apart get merged into one entry, so prefer our own live log when we have it."""
    r = await http.get(f"{DMKS}/v2/channel", params={"uId": uid}, headers=DMKS_HEADERS, timeout=30)
    r.raise_for_status()
    return [{"id": x["liveId"], "start": x["startDate"] / 1000, "stop": (x.get("stopDate") or 0) / 1000 or None,
             "title": x.get("title") or "", "danmaku": x.get("danmakusCount") or 0} for x in r.json()["data"]["lives"]]


async def dmks_live_full(http: httpx.AsyncClient, live_id: str) -> bytes:
    r = await http.get(f"{DMKS}/v3/lives/{live_id}/full", params={"include": "extra", "includeEnter": "false"},
                       headers={**DMKS_HEADERS, "Accept": "application/x-msgpack"}, timeout=60)
    r.raise_for_status()
    return r.content


# ---- download ---------------------------------------------------------------------------------


class DownloadBusy(RuntimeError):
    """Another process holds the lock on this file."""


class DownloadError(RuntimeError):
    pass


class Downloader:
    """Chunked, resumable, mirror-rotating, lock-guarded download of one DASH stream.

    Range chunks with a read timeout (a stalled CDN connection moves to the next mirror instead
    of hanging), resumed from <dest>.part, guarded by an flock so two servers can't interleave
    writes into one file, size-checked against Content-Range, then renamed into place.
    """

    def __init__(self, http: httpx.AsyncClient | None = None, chunk: int = 8 << 20, min_rate: float = 2e6,
                 url_ttl: float = 100 * 60, read_timeout: float = 25.0, max_failures: int = 8,
                 backoff: Callable[[int], float] = lambda n: min(30.0, 2.0 ** n)):
        self.http = http or httpx.AsyncClient(follow_redirects=True)
        self.chunk, self.min_rate, self.url_ttl = chunk, min_rate, url_ttl
        self.timeout = httpx.Timeout(read_timeout, connect=10.0)
        self.max_failures, self.backoff = max_failures, backoff

    async def close(self) -> None:
        await self.http.aclose()

    async def _range(self, url: str, start: int, end: int, sink) -> tuple[int, int, float]:
        """Append bytes [start, end] of url to sink. Returns (total size, bytes written, seconds)."""
        t, n = time.monotonic(), 0
        headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/", "Range": f"bytes={start}-{end}"}
        async with self.http.stream("GET", url, headers=headers, timeout=self.timeout) as r:
            if r.status_code != 206:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            total = int(r.headers["Content-Range"].rsplit("/", 1)[1])
            async for buf in r.aiter_bytes(1 << 20):
                sink.write(buf)
                n += len(buf)
        return total, n, time.monotonic() - t

    async def rank(self, urls: list[str], probe: int = 1 << 20) -> list[str]:
        """Time a 1 MiB range on each mirror; fastest first."""
        speed = []
        for u in urls:
            sink = _Null()
            try:
                _, got, dt = await self._range(u, 0, probe - 1, sink)
                speed.append((got / max(dt, 1e-3), u))
            except (httpx.HTTPError, OSError, KeyError, ValueError):
                speed.append((0.0, u))
        speed.sort(key=lambda x: -x[0])
        log.info("mirrors: %s", ", ".join(f"{httpx.URL(u).host.split('.')[0]} {s / 1e6:.1f}MB/s" for s, u in speed))
        return [u for _, u in speed]

    async def fetch(self, urls_fn: Callable[[], Awaitable[list[str]]], dest: Path,
                    progress: Callable[[int, int | None], None] | None = None) -> int:
        if dest.exists():
            return dest.stat().st_size
        part, lock_path = dest.with_name(dest.name + ".part"), dest.with_name(dest.name + ".lock")
        lockf = open(lock_path, "w")
        try:
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DownloadBusy(f"{dest.name} is being downloaded by another process") from None
            return await self._fetch_locked(urls_fn, dest, part, progress)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
            lockf.close()
            try:
                lock_path.unlink()
            except OSError:
                pass

    async def _fetch_locked(self, urls_fn, dest: Path, part: Path, progress) -> int:
        urls, fetched_at = await self.rank(await urls_fn()), time.monotonic()
        total: int | None = None
        failures, i, best = 0, 0, 0.0
        with open(part, "ab") as f:
            while total is None or f.tell() < total:
                if time.monotonic() - fetched_at > self.url_ttl:  # signed URLs die 120 min after playurl
                    urls, fetched_at = await self.rank(await urls_fn()), time.monotonic()
                url, have = urls[i % len(urls)], f.tell()
                end = have + self.chunk - 1 if total is None else min(total - 1, have + self.chunk - 1)
                try:
                    total, n, dt = await self._range(url, have, end, f)
                    rate = n / max(dt, 1e-3)
                    best = max(best, rate)
                    failures = 0 if n else failures + 1
                    if n < end - have + 1 or rate < max(self.min_rate, best / 4):  # cut short or slow
                        i += 1
                except httpx.HTTPStatusError as e:
                    failures, i = failures + 1, i + 1
                    code = e.response.status_code
                    log.warning("HTTP %s on %s", code, httpx.URL(url).host)
                    if code in (403, 404, 410):  # expired or IP-bound signature: ask for fresh URLs
                        urls, fetched_at = await self.rank(await urls_fn()), time.monotonic()
                except (httpx.TransportError, OSError, KeyError, ValueError) as e:  # stall/reset: keep what arrived
                    failures, i = failures + 1, i + 1
                    log.warning("%s on %s at %d", type(e).__name__, httpx.URL(url).host, f.tell())
                f.flush()
                if progress:
                    progress(f.tell(), total)
                if failures >= self.max_failures:
                    raise DownloadError(f"giving up on {dest.name} at {f.tell()}/{total}")
                if failures:
                    await asyncio.sleep(self.backoff(failures))
        size = part.stat().st_size
        if size != total:
            raise DownloadError(f"{dest.name}: size mismatch {size} != {total}")
        os.replace(part, dest)
        return size


class _Null:
    def write(self, b: bytes) -> None:
        pass
