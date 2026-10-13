"""Playback diagnostics from the extension: what fone and F1 TV's player were doing, so a
problem on a machine nobody is watching over (a Mac after it slept, glitches at 2x) can be
read afterwards instead of reproduced.

POST /api/diag   {"client", "build", "platform", "events": [{"t", "type", ...}, ...]}
                 (from the extension's audio engine every few seconds; refused from web
                 pages like every other write, see app.RefuseWebPageWrites)
GET  /api/diag?client=&type=&since=&limit=   newest last; plus every client's last report

Playback state only: positions, speeds, sync drift, seeks, buffering, player errors. Kept
in memory (the last 20000 events) and in <data_dir>/diag/<day>.jsonl for 7 days.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from .config import Config

log = logging.getLogger("fone.diag")

KEEP_DAYS = 7
MAX_EVENTS = 200  # per request
MAX_EVENT_BYTES = 4000


class DiagBody(BaseModel):
    client: str = Field(max_length=64)
    build: str = Field(default="", max_length=80)
    platform: str = Field(default="", max_length=80)
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_EVENTS)


class DiagStore:
    def __init__(self, root: Path | None, size: int = 20000):
        self.root = root
        self.events: deque[dict] = deque(maxlen=size)
        self.clients: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._pruned_day = ""
        if root:
            root.mkdir(parents=True, exist_ok=True)
            self._load()

    def _load(self) -> None:
        """The last days' events back into memory after a restart."""
        for f in sorted(self.root.glob("*.jsonl"))[-KEEP_DAYS:]:
            try:
                for line in f.read_text().splitlines():
                    ev = json.loads(line)
                    self.events.append(ev)
                    c = self.clients.setdefault(ev.get("client", "?"), {})
                    c.update(last=ev.get("rx"), build=ev.get("build"), platform=ev.get("platform"))
            except (OSError, ValueError):
                log.warning("unreadable diagnostics file %s", f.name)

    def add(self, body: DiagBody) -> int:
        rx = time.time()
        kept = []
        for ev in body.events:
            line = json.dumps(ev, separators=(",", ":"), default=str)
            if len(line) > MAX_EVENT_BYTES:
                ev = {"t": ev.get("t"), "type": ev.get("type"), "truncated": len(line)}
            kept.append({**ev, "client": body.client, "build": body.build, "platform": body.platform, "rx": rx})
        with self._lock:
            self.events.extend(kept)
            self.clients[body.client] = {"last": rx, "build": body.build, "platform": body.platform}
            self._persist(kept, rx)
        return len(kept)

    def _persist(self, kept: list[dict], rx: float) -> None:
        if not self.root or not kept:
            return
        day = datetime.fromtimestamp(rx, timezone.utc).strftime("%Y-%m-%d")
        try:
            with (self.root / f"{day}.jsonl").open("a") as f:
                f.writelines(json.dumps(ev, separators=(",", ":"), default=str) + "\n" for ev in kept)
            if day != self._pruned_day:
                self._pruned_day = day
                for old in sorted(self.root.glob("*.jsonl"))[:-KEEP_DAYS]:
                    old.unlink(missing_ok=True)
        except OSError as e:
            log.warning("diagnostics not saved: %s", e)

    def query(self, client: str | None, types: set[str] | None, since: float | None, limit: int) -> list[dict]:
        with self._lock:
            out = [ev for ev in self.events
                   if (client is None or ev.get("client") == client)
                   and (types is None or ev.get("type") in types)
                   and (since is None or (ev.get("t") or ev.get("rx") or 0) >= since)]
        return out[-limit:]


def make_router(store: DiagStore) -> APIRouter:
    r = APIRouter(prefix="/api/diag")

    @r.post("")
    def post(body: DiagBody):
        return {"ok": True, "stored": store.add(body)}

    @r.get("")
    def get(client: str | None = None, type: str | None = None, since: float | None = None,
            limit: Annotated[int, Query(ge=1, le=5000)] = 500):
        types = set(type.split(",")) if type else None
        return {"clients": store.clients, "events": store.query(client, types, since, limit)}

    return r


def make_store(cfg: Config) -> DiagStore:
    return DiagStore(cfg.data_dir / "diag")
