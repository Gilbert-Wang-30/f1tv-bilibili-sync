"""HTTP API for the browser extension."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import replace

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from . import config, recordings
from .extractor import Extractor, FileState, Piece
from .f1 import F1Mapper
from .store import CLOCK_FAMILY, Store
from .timeline import MIN_CLIP_SECONDS, MIN_FILL_SECONDS, content_span, mutes as ad_mutes, uncovered, union

log = logging.getLogger("fone")
SEG_RE = re.compile(r"^seg_\d{5}\.ts$")
# Screen-clock results good enough to beat the channel's calibrated default.
MEASURED_CONFIDENCE = ("high", "medium")


cfg = config.load()
store = Store(cfg.data_dir / "fone.sqlite3")
# Everything is built at import so the routers below can be mounted; nothing runs until
# lifespan starts it.
extractor = Extractor(cfg)
mapper = F1Mapper(store)


# Optional services, imported only when enabled (numpy, tesseract): (service, router).
def _timing():
    from .timing import TimingService, make_router

    svc = TimingService(cfg, mapper)
    return svc, make_router(svc)


def _ads():
    from .ads import AdService, make_router

    svc = AdService(cfg, extractor)
    return svc, make_router(svc)


def _screenclock():
    from .screenclock import ScreenClockService, make_router

    svc = ScreenClockService(cfg, extractor)
    return svc, make_router(svc)


def _video():
    from .video import VideoService, make_router

    svc = VideoService(cfg, extractor)
    return svc, make_router(svc)


def _backfill():
    from .backfill import BackfillService, Settings, make_router

    svc = BackfillService(cfg, extractor, store,
                          replace(Settings.from_env(), rooms=cfg.rooms, import_dir=cfg.import_dir))
    return svc, make_router(svc)


def _danmaku():
    from .danmaku import DanmakuService, make_router

    svc = DanmakuService(cfg)
    return svc, make_router(svc)


def _build(name, build):
    """An enabled service, or None. One that can't even be built (missing numpy, bad
    settings) is left out rather than taking the commentary sync down with it."""
    if name not in cfg.features:
        return None
    try:
        svc, router = build()
    except Exception:
        log.exception("%s disabled: failed to initialise", name)
        return None
    routers.append(router)
    return svc


routers: list = []
timing = _build("timing", _timing)
ads = _build("ads", _ads)
screenclock = _build("screenclock", _screenclock)
video = _build("video", _video)
backfill = _build("backfill", _backfill)
danmaku = _build("danmaku", _danmaku)
SERVICES = {name: svc for name, svc in
            (("timing", timing), ("ads", ads), ("screenclock", screenclock), ("video", video),
             ("backfill", backfill), ("danmaku", danmaku)) if svc is not None}


async def _scanner() -> None:
    while True:
        try:
            recs = await asyncio.to_thread(recordings.scan, cfg.rec_dir, cfg.filename_tz, cfg.channel_aliases,
                                           cfg.import_dir)
            extractor.update(recs)
            extractor.tick()
        except Exception:
            log.exception("scan failed")
        await asyncio.sleep(cfg.scan_interval)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request otherwise
    task = asyncio.create_task(_scanner())
    log.info("watching %s (imports: %s; services: %s)", cfg.rec_dir, cfg.import_dir, ", ".join(SERVICES) or "none")
    started = []
    for name, svc in SERVICES.items():
        try:  # nor one that fails to start
            await svc.start()
            started.append(svc)
        except Exception:
            log.exception("%s failed to start", name)
    yield
    for svc in reversed(started):
        try:
            await svc.stop()
        except Exception:
            log.exception("stopping %s", type(svc).__name__)
    task.cancel()
    await extractor.shutdown()
    await mapper.close()


class RefuseWebPageWrites:
    """Requests that change something come from the extension's own pages (Origin
    chrome-extension://…) or from no browser at all (curl, scripts: no Origin). Any other
    web page is refused: with CORS * and private-network access granted below, a page you
    merely visit could otherwise re-tune your delays, write fake car positions into a live
    session or start replay downloads (GBs, over the recorders' connection, into the NAS).
    Reads stay open to everyone, as before."""

    EXTENSION = re.compile(r"^(chrome|moz)-extension://")
    SAFE = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in self.SAFE:
            origin = next((v for k, v in scope["headers"] if k == b"origin"), None)
            if origin is not None and not self.EXTENSION.match(origin.decode("latin-1")):
                resp = PlainTextResponse("changes are only accepted from the fone extension", status_code=403)
                return await resp(scope, receive, send)
        await self.app(scope, receive, send)


app = FastAPI(title="fone", lifespan=lifespan)
app.add_middleware(RefuseWebPageWrites)  # inside CORS, so its 403 still carries CORS headers
# allow_private_network: Chrome preflights requests from public origins to LAN hosts.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   allow_private_network=True)
for _router in routers:
    app.include_router(_router)

# Playback diagnostics from the extension (diag.py): always on, it's how problems get debugged.
from .diag import make_router as _diag_router, make_store as _diag_store  # noqa: E402

diag_store = _diag_store(cfg)
app.include_router(_diag_router(diag_store))


# ---- helpers ---------------------------------------------------------------


def _span_guess(st: FileState) -> tuple[float, float]:
    """Rough wall span for files we haven't extracted yet: [created, last write]."""
    rec = st.rec
    start = rec.name_time if rec.name_time is not None else rec.mtime - 6 * 3600
    return start, rec.mtime


def _channel_files(channel: str) -> list[FileState]:
    return [st for st in extractor.files.values() if st.rec.channel == channel]


async def _measured(cid: str, channel: str) -> dict | None:
    """Streamer delay from the screen clock of the recording covering this content:
    commentary about event E is recorded at E + delayScreen + reaction."""
    if screenclock is None or not cid.isdigit():
        return None
    cached = store.get_content(cid)
    try:
        data = cached[0] if cached else await mapper.content(cid)
    except Exception:
        log.exception("content %s", cid)
        return None
    span = content_span(data)
    if span is None:
        return None
    lo, hi = span[0] + cfg.default_delay, span[1] + cfg.default_delay  # where its commentary sits
    found = []
    for st in _channel_files(channel):
        if st.status not in ("running", "done"):
            continue
        pieces, _, _ = extractor.pieces(st)
        overlap = sum(max(0.0, min(hi, p.wall + p.duration) - max(lo, p.wall)) for p in pieces)
        if overlap > 0:
            found.append((st.rec.source == "recorder", overlap, st))
    for _, _, st in sorted(found, key=lambda f: f[:2], reverse=True):
        res = screenclock.result(st.rec.id)
        # zonesChecked False: OpenF1 was down, so the clock was read modulo 15 min and a
        # MultiViewer replay of another session could pass for a delay. Wait for the re-check.
        if res and res.get("delayScreen") is not None and res.get("confidence") in MEASURED_CONFIDENCE \
                and res.get("zonesChecked") is not False:
            screen = res["delayScreen"]
            return {"delay": round(screen + cfg.reaction_seconds, 3), "source": "measured", "screen": screen,
                    "reaction": cfg.reaction_seconds, "confidence": res["confidence"], "file": st.rec.id}
    return None


# ---- routes ----------------------------------------------------------------


@app.get("/api/health")
def health():
    return {"ok": True, "recDir": str(cfg.rec_dir), "files": len(extractor.files), "now": time.time(),
            "services": sorted(SERVICES)}


@app.get("/api/channels")
def channels():
    out: dict[str, dict] = {}
    now = time.time()
    for st in extractor.files.values():
        c = out.setdefault(st.rec.channel, {"name": st.rec.channel, "files": 0, "latest": 0.0, "live": False})
        c["files"] += 1
        c["latest"] = max(c["latest"], st.rec.mtime)
        c["live"] = c["live"] or extractor.is_growing(st.rec, now)
    return sorted(out.values(), key=lambda c: -c["latest"])


@app.get("/api/f1tv/{cid}")
async def f1tv(cid: str, refresh: bool = False):
    if not cid.isdigit():
        raise HTTPException(400, "content id must be numeric")
    return await mapper.content(cid, refresh=refresh)


class SyncBody(BaseModel):
    channel: str
    clock: str
    delay: float


@app.get("/api/sync/{cid}")
async def get_sync(cid: str, channel: str, clock: str):
    if (d := store.content_delay(cid, channel, clock)) is not None:
        return {"delay": d, "source": "content"}
    # The screen clock measures against true UTC, so only a true-UTC clock can use it.
    if CLOCK_FAMILY.get(clock) == "utc" and (measured := await _measured(cid, channel)) is not None:
        return measured
    # Only from a comparable clock: a live session's -0.2 on F1 TV's player clock (epoch)
    # once started a replay's commentary 45 s early.
    if (d := store.family_delay(channel, clock)) is not None:
        return {"delay": d, "source": "channel"}
    return {"delay": cfg.default_delay, "source": "default"}


@app.put("/api/sync/{cid}")
def put_sync(cid: str, body: SyncBody):
    store.set_delay(cid, body.channel, body.clock, round(body.delay, 3))
    return {"ok": True}


@app.get("/api/timeline")
def timeline(channel: str, start: float = Query(alias="from"), end: float = Query(alias="to")):
    now = time.time()
    candidates = []
    for st in _channel_files(channel):
        lo, hi = _span_guess(st)
        if hi >= start and lo <= end:
            extractor.ensure(st)  # old recordings are extracted on demand
            candidates.append(st)

    entries = []
    for st in candidates:
        pieces, segs, provisional = extractor.pieces(st)
        wall = pieces[0].wall if pieces else _span_guess(st)[0]
        entries.append((wall, st, pieces, segs, provisional))
    entries.sort(key=lambda e: e[0])

    # Files that may be in the playlist: those up to the first one still growing or being
    # extracted (keeps the playlist append-only). Imports join once fully extracted, so
    # they never hold the others back.
    eligible, blocked = [], False
    for i, (_, st, _, segs, _) in enumerate(entries):
        if segs and not blocked and (st.rec.source == "recorder" or st.status == "done"):
            eligible.append(i)
            blocked = st.status != "done"
    # Wall time each file is played for: recordings in wall order, then imports only where
    # nothing before them covers, so no two pieces overlap.
    played: dict[int, list[Piece]] = {}
    covered: list[tuple[float, float]] = []
    for i in sorted(eligible, key=lambda i: entries[i][1].rec.source != "recorder"):
        min_len = MIN_CLIP_SECONDS if entries[i][1].rec.source == "recorder" else MIN_FILL_SECONDS
        played[i] = [q for p in entries[i][2] for q in uncovered(p, covered, min_len)]
        covered = union(covered + [(p.wall, p.wall + p.duration) for p in played[i]])

    files, out_pieces, mutes, playlist_ids = [], [], [], []
    offset = 0.0
    for i, (wall, st, pieces, segs, provisional) in enumerate(entries):
        in_playlist = bool(played.get(i))
        duration = segs[-1].end if segs else 0.0
        ad_state = ads.state(st.rec.id) if ads is not None and in_playlist else None
        files.append({
            "id": st.rec.id, "name": st.rec.name, "title": st.rec.title, "source": st.rec.source,
            "status": st.status, "growing": extractor.is_growing(st.rec, now), "provisional": provisional,
            "inPlaylist": in_playlist, "wallStart": wall,
            "wallEnd": (pieces[-1].wall + pieces[-1].duration) if pieces else None,
            "duration": duration, "offset": offset if in_playlist else None, "error": st.error,
            "adsAnalyzedTo": ad_state["analyzedTo"] if ad_state else None,
        })
        if in_playlist:
            playlist_ids.append(st.rec.id)
            out_pieces += [{"w": p.wall, "m": offset + p.media, "d": p.duration, "f": i} for p in played[i]]
            if ad_state:
                mutes += ad_mutes(ad_state["spans"], played[i], offset)
            offset += duration
    out_pieces.sort(key=lambda p: p["w"])  # an import can fill a hole between two recordings
    mutes.sort(key=lambda m: m["w"])

    live_file = next((f for f in reversed(files) if f["inPlaylist"] and f["growing"]), None)
    return {
        "channel": channel,
        "now": now,
        "files": files,
        "pieces": out_pieces,
        "mutes": mutes,
        "playlist": f"/api/playlist.m3u8?files={','.join(playlist_ids)}" if playlist_ids else None,
        "live": live_file is not None,
        "edge": live_file["wallEnd"] if live_file else None,
        # Over the eligible files, not just those with pieces left: a reconnect's new file
        # can be wholly overlapped by the previous one for a moment, and "complete" would
        # then send the extension to its slow (60 s) refresh right as the stream resumes.
        "complete": all(entries[i][1].status == "done" for i in eligible),
    }


@app.get("/api/playlist.m3u8")
def playlist(files: str):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:EVENT"]
    body: list[str] = []
    target = 1.0
    complete = True
    for n, fid in enumerate(f for f in files.split(",") if f):
        st = extractor.files.get(fid)
        if st is None:
            raise HTTPException(404, f"unknown file {fid}")
        segs, _ = st.segments()
        if n:
            body.append("#EXT-X-DISCONTINUITY")
        for s in segs:
            target = max(target, s.duration)
            body += [f"#EXTINF:{s.duration:.6f},", f"/hls/{fid}/{s.name}"]
        if st.status != "done":
            complete = False
            break  # later files would shift once this one grows
    lines.insert(2, f"#EXT-X-TARGETDURATION:{int(target + 0.999)}")
    lines += body
    if complete:
        lines.append("#EXT-X-ENDLIST")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl",
                             headers={"Cache-Control": "no-store"})


@app.get("/hls/{fid}/{name}")
def segment(fid: str, name: str):
    st = extractor.files.get(fid)
    if st is None or not SEG_RE.match(name):
        raise HTTPException(404)
    path = st.out_dir / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "public, max-age=3600"})


def _extras(st: FileState) -> str:
    """Status-page notes from the optional services."""
    out = []
    if ads is not None and (spans := ads.spans(st.rec.id)):
        out.append(f"{len(spans)} ad clip{'s' * (len(spans) > 1)}")
    if screenclock is not None and st.status in ("running", "done"):
        res = screenclock.result(st.rec.id) or {}
        if res.get("delayScreen") is not None:
            out.append(f"screen delay {res['delayScreen']:.2f} s ({res['confidence']})")
    return ", ".join(out)


@app.get("/", response_class=HTMLResponse)
def index():
    now = time.time()
    rows = []
    for st in sorted(extractor.files.values(), key=lambda s: -(s.rec.name_time or s.rec.mtime)):
        segs, _ = st.segments()
        dur = segs[-1].end if segs else 0
        growing = "🔴 live" if extractor.is_growing(st.rec, now) else ""
        source = " (import)" if st.rec.source == "import" else ""
        rows.append(
            f"<tr><td>{html.escape(st.rec.channel)}</td><td>{html.escape(st.rec.name)}{source}</td>"
            f"<td>{st.status} {growing}</td><td>{dur / 60:.1f} min</td>"
            f"<td>{html.escape(_extras(st))}</td><td>{html.escape(st.error or '')}</td></tr>"
        )
    return (
        "<!doctype html><meta charset=utf-8><title>fone</title>"
        "<style>body{font:14px system-ui;margin:2em}td{padding:4px 10px;border-bottom:1px solid #ddd}</style>"
        f"<h2>fone — {len(rows)} recordings in {html.escape(str(cfg.rec_dir))}</h2>"
        "<table><tr><th>channel</th><th>file</th><th>status</th><th>audio</th><th>analysis</th><th>error</th></tr>"
        + "".join(rows) + "</table>"
    )
