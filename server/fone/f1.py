"""Map an F1 TV content ID to real-world time.

    F1 TV content API  ->  meeting/session keys, scheduled start, LIVE/REPLAY
    MultiViewer API    ->  `session_start`: replay position (s) of the official
                           `SessionStatus: Started` moment
    F1 live timing     ->  UTC of that `Started` moment

    event_utc(p) = p0 + p,   p0 = started_utc - session_start

Verified across 2026 sessions: the anchor is consistent to <0.5 s. Until
MultiViewer publishes (live, or the first hours after a session) we fall back to
`scheduled_start + PREROLL[type]`, the replay's fixed pre-roll measured from
every 2026 session.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import httpx

from .store import Store

log = logging.getLogger("fone.f1")

F1TV_META = "https://f1tv.formula1.com/2.0/R/ENG/WEB_DASH/ALL/CONTENT/VIDEO/{cid}/F1_TV_Pro_Annual/2"
MV_SESSION = "https://api.multiviewer.app/api/v1/meetings/{mk}/sessions/{sk}"
LT_SESSION_DATA = "https://livetiming.formula1.com/static/{path}SessionData.json"
OPENF1_SESSIONS = "https://api.openf1.org/v1/sessions"
UA = {"User-Agent": "fone-commentary-sync/0.1"}

# Replay position 0 relative to scheduled start (seconds), median of 2026 sessions.
PREROLL = {
    "practice": -1165.0,
    "sprint_qualifying": -1162.3,
    "qualifying": -307.0,
    "sprint": -249.7,
    "race": -307.3,
}


def session_kind(name: str) -> str | None:
    n = name.lower()
    if "sprint" in n and ("qualifying" in n or "shootout" in n):
        return "sprint_qualifying"
    if "sprint" in n:
        return "sprint"
    if "qualifying" in n:
        return "qualifying"
    if "practice" in n or n.strip().startswith("fp"):
        return "practice"
    if "race" in n or "grand prix" in n:
        return "race"
    return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ms(v) -> float | None:
    """F1 TV sends epoch milliseconds, sometimes as a string."""
    try:
        return float(v) / 1000
    except (TypeError, ValueError):
        return None


def _iso(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _streams(raw: list) -> list[dict]:
    """The content's channels (F1 TV's additionalStreams): an onboard camera per driver
    (type "obc") and the other feeds (F1 LIVE, INTERNATIONAL, TRACKER, DATA: "additional"),
    for the dashboard's onboard picker. channelId is what F1 TV's player feeds carry."""
    out = []
    for s in raw:
        ch = _int(s.get("channelId"))
        if ch is None:
            continue
        name = " ".join(x for x in (s.get("driverFirstName"), s.get("driverLastName")) if x)
        out.append({"channelId": ch, "type": s.get("type") or "", "title": s.get("title") or "",
                    "number": _int(s.get("racingNumber")) or None, "name": name or None,
                    "team": s.get("teamName") or None, "color": s.get("hex") or None})
    return out


class F1Mapper:
    def __init__(self, store: Store):
        self.store = store
        self.http = httpx.AsyncClient(timeout=15, headers=UA, follow_redirects=True)

    async def close(self) -> None:
        await self.http.aclose()

    async def _json(self, url: str) -> dict | None:
        try:
            r = await self.http.get(url)
        except httpx.HTTPError as e:
            log.warning("GET %s failed: %s", url, e)
            return None
        if r.status_code != 200:
            return None
        return json.loads(r.content.decode("utf-8-sig"))

    async def content(self, cid: str, refresh: bool = False) -> dict:
        cached = self.store.get_content(cid)
        if cached and not refresh:
            data, fetched_at = cached
            # Anchored replays never change; everything else is re-checked so a live
            # session upgrades to the exact anchor once MultiViewer publishes it. An entry
            # cached before the stream list existed is fetched once more for it.
            if "streams" in data and (data.get("anchor") == "multiviewer" or time.time() - fetched_at < 600):
                return data
        data = await self._resolve(cid)
        self.store.put_content(cid, data)
        return data

    async def _find_nearby_session(self, cid: str) -> tuple[dict, dict] | None:
        """F1 TV hides metadata for pre-live content, but MultiViewer already lists the
        content id against its session. Search this week's sessions for it."""
        now = time.time()
        lo = datetime.fromtimestamp(now - 4 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        hi = datetime.fromtimestamp(now + 4 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        sessions = await self._json(f"{OPENF1_SESSIONS}?date_start>={lo}&date_start<={hi}")
        for s in sessions or []:
            mv = await self._json(MV_SESSION.format(mk=s["meeting_key"], sk=s["session_key"]))
            if mv and str(mv.get("f1tv_content_id")) == str(cid):
                return s, mv
        return None

    async def _resolve(self, cid: str) -> dict:
        out: dict = {"contentId": cid, "anchor": None, "p0": None, "streams": []}
        meta = await self._json(F1TV_META.format(cid=cid))
        try:
            m = meta["resultObj"]["containers"][0]["metadata"]  # type: ignore[index]
        except (TypeError, KeyError, IndexError):
            m = None
        mv = None
        if m:
            emf = m.get("emfAttributes") or {}
            title = m.get("title") or ""
            out.update(
                title=title,
                subtype=m.get("contentSubtype"),  # LIVE | REPLAY | ...
                duration=m.get("duration"),
                meetingKey=_int(emf.get("MeetingKey")),
                sessionKey=_int(emf.get("MeetingSessionKey")),
                meetingName=emf.get("Meeting_Name") or emf.get("Global_Meeting_Name"),
                scheduledStart=_ms(emf.get("sessionStartDate")),
                scheduledEnd=_ms(emf.get("sessionEndDate")),
            )
            session_name = title.rsplit(" - ", 1)[-1] if " - " in title else title
            out["streams"] = _streams(m.get("additionalStreams") or [])
        else:
            found = await self._find_nearby_session(cid)
            if not found:
                out["error"] = "F1 TV does not know this content id"
                return out
            s, mv = found
            session_name = s["session_name"]
            out.update(
                title=f"{s.get('country_name', '')} {session_name}".strip(),
                subtype="LIVE",  # pre-live / live content is what F1 TV hides
                meetingKey=s["meeting_key"],
                sessionKey=s["session_key"],
                meetingName=s.get("location"),
                scheduledStart=_iso(s["date_start"]),
                scheduledEnd=_iso(s["date_end"]) if s.get("date_end") else None,
            )
        mk, sk = out["meetingKey"], out["sessionKey"]
        if mv is None and mk and sk:
            mv = await self._json(MV_SESSION.format(mk=mk, sk=sk))

        is_main = bool(mv) and str(mv.get("f1tv_content_id")) == str(cid)
        if mv and mv.get("session_info"):
            session_name = mv["session_info"].get("Name") or session_name
        out["sessionName"] = session_name
        out["kind"] = session_kind(session_name)
        out["isSessionFeed"] = is_main
        if mv:
            out["channelOffsets"] = {
                str(o["streamData"].get("channelId")): o.get("diff", 0)
                for o in (mv.get("sync_offsets") or {}).get("sync_offsets", [])
                if str(o.get("streamData", {}).get("contentId")) == str(cid)
            }

        if is_main and mv.get("session_start") and mv.get("session_info"):
            sd = await self._json(LT_SESSION_DATA.format(path=mv["session_info"]["Path"]))
            started = [
                e["Utc"] for e in (sd or {}).get("StatusSeries", []) if e.get("SessionStatus") == "Started"
            ]
            if started:
                started_utc = _iso(started[0])
                video_s = float(mv["session_start"])
                out.update(anchor="multiviewer", startedUtc=started_utc, sessionStartVideo=video_s,
                           p0=started_utc - video_s)
                return out

        # No published sync yet: a session feed (or something titled like one) uses the pre-roll.
        if (is_main or mv is None) and out["kind"] and out["scheduledStart"]:
            out.update(anchor="preroll", p0=out["scheduledStart"] + PREROLL[out["kind"]])
        return out
