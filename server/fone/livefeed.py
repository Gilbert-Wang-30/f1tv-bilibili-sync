"""Anonymous F1 live timing over SignalR Core, Server-Sent-Events transport (httpx only).

    POST /signalrcore/negotiate?negotiateVersion=1   -> connectionToken
    GET  /signalrcore?id=<token>  (text/event-stream) server -> client frames
    POST /signalrcore?id=<token>                      client -> server frames

Handshake `{"protocol":"json","version":1}`, then `Subscribe([topics])`: its completion
carries every topic's current state (keyframes); afterwards "feed" invocations
`[topic, payload, utc]` stream the deltas. The utc is the archive's clock (an archived
message at offset o has Utc == t0 + o), so captures and archives line up. Frames end in
\\x1e. The old /signalr endpoint answers 401 now; /signalrcore serves every topic
anonymously except Position.z / CarData.z, which need the F1 TV token only the browser has.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from .timing_state import iso

log = logging.getLogger("fone.livefeed")

BASE = "https://livetiming.formula1.com/signalrcore"
RS = "\x1e"
# The archive's topic set (what the dashboard reads) plus Heartbeat, which guarantees a
# feed message every few seconds even when nothing happens on track.
TOPICS = [
    "Heartbeat", "SessionInfo", "SessionData", "SessionStatus", "ArchiveStatus", "DriverList",
    "TimingData", "TimingAppData", "TimingStats", "TrackStatus", "RaceControlMessages", "WeatherData",
    "ExtrapolatedClock", "LapCount", "PitLaneTimeCollection", "Position.z",
]
PING_SECONDS = 15  # SignalR drops clients it hasn't heard from in 30 s
UA = {"User-Agent": "fone/0.3"}


class FeedError(Exception):
    pass


@dataclass
class Keyframes:
    """Subscribe completion: current state of each topic."""

    topics: dict[str, Any]


@dataclass
class Feed:
    topic: str
    payload: Any
    utc: float


@dataclass
class Ping:
    """Server keep-alive (every ~15 s): lets consumers act while the feed is quiet."""


class SseFrames:
    """Splits an SSE text stream into SignalR JSON frames. Parsed by hand: httpx's
    aiter_lines() uses str.splitlines(), which also splits on \\x1e."""

    def __init__(self):
        self._buf = ""

    def feed(self, text: str) -> list[dict]:
        # Normalise on the joined buffer: a chunk may end between the \r and the \n.
        self._buf = (self._buf + text).replace("\r\n", "\n")
        *events, self._buf = self._buf.split("\n\n")
        frames = []
        for ev in events:
            data = "\n".join(ln[5:].removeprefix(" ") for ln in ev.split("\n") if ln.startswith("data:"))
            for f in data.split(RS):
                if f.strip():
                    try:
                        frames.append(json.loads(f))
                    except ValueError:
                        log.warning("bad frame: %.200s", f)
        return frames


async def _send(client: httpx.AsyncClient, url: httpx.URL, msg: dict) -> None:
    r = await client.post(url, content=json.dumps(msg) + RS, headers={"Content-Type": "text/plain"})
    r.raise_for_status()


async def _pinger(client: httpx.AsyncClient, url: httpx.URL) -> None:
    while True:
        await asyncio.sleep(PING_SECONDS)
        try:
            await _send(client, url, {"type": 6})
        except httpx.HTTPError as e:
            log.debug("ping failed: %s", e)


async def stream(client: httpx.AsyncClient, topics: list[str] = TOPICS,
                 base: str = BASE) -> AsyncIterator[Keyframes | Feed | Ping]:
    """One connection: yields Keyframes once, then Feed (and Ping) events until the server
    closes. Use with contextlib.aclosing() so breaking out closes the connection."""
    r = await client.post(f"{base}/negotiate", params={"negotiateVersion": 1}, headers=UA)
    r.raise_for_status()
    url = httpx.URL(base, params={"id": r.json()["connectionToken"]})
    pinger: asyncio.Task | None = None
    try:
        async with client.stream("GET", url, headers={"Accept": "text/event-stream", **UA}) as sse:
            sse.raise_for_status()
            await _send(client, url, {"protocol": "json", "version": 1})
            parser = SseFrames()
            async for chunk in sse.aiter_text():
                for m in parser.feed(chunk):
                    typ = m.get("type")
                    if typ is None:  # handshake response
                        if m.get("error"):
                            raise FeedError(f"handshake: {m['error']}")
                        if pinger is None:
                            await _send(client, url, {"type": 1, "invocationId": "1", "target": "Subscribe",
                                                      "arguments": [topics]})
                            pinger = asyncio.create_task(_pinger(client, url))
                    elif typ == 3:
                        if m.get("error"):
                            raise FeedError(f"subscribe: {m['error']}")
                        result = m.get("result") or {}
                        for v in result.values():
                            if isinstance(v, dict):
                                v.pop("_kf", None)
                        yield Keyframes({k: v for k, v in result.items() if v is not None})
                    elif typ == 1 and m.get("target") == "feed":
                        args = m.get("arguments") or []
                        if len(args) >= 3:
                            yield Feed(args[0], args[1], iso(args[2]))
                    elif typ == 6:
                        yield Ping()
                    elif typ == 7:
                        raise FeedError(f"server closed: {m.get('error') or 'no reason'}")
    finally:
        if pinger:
            pinger.cancel()
