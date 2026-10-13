"""SQLite persistence: F1 content lookups and sync calibrations (backfill adds its own tables)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS f1_content (
    cid TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
-- Streamer delay = how far the commentary recording lags real-world time.
-- Per channel default, for servers before v0.3 (they start every clock from it). This one
-- starts a new session from the channel's latest calibration on a comparable clock instead
-- (Store.family_delay); the table is still kept current for them.
CREATE TABLE IF NOT EXISTS channel_delay (
    channel TEXT PRIMARY KEY,
    delay REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- Per (content, channel, clock) once you've tuned a specific video.
CREATE TABLE IF NOT EXISTS content_delay (
    cid TEXT NOT NULL,
    channel TEXT NOT NULL,
    clock TEXT NOT NULL,
    delay REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (cid, channel, clock)
);
"""

# Clocks whose delays are comparable, so one session's calibration is a fair start for
# another's. multiviewer and preroll both put the video on true UTC (an exact / an
# estimated anchor). epoch is the player's own wall clock, which on F1 TV's live streams
# reads ~40-45 s late, so it only compares with itself. edge has no family (the extension
# starts a live edge at its own default unless that video was tuned), nor has manual.
CLOCK_FAMILY = {"multiviewer": "utc", "preroll": "utc", "epoch": "epoch"}
# Clocks whose event time is exact, so a calibration there measures the streamer's true
# delay: what channel_delay holds for older servers, which apply it to every clock.
TRUSTED_CLOCKS = {"multiviewer"}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock, self._db:
            return self._db.execute(sql, args).fetchall()

    # For services that keep their own tables in this DB (backfill), sharing the lock.
    def ensure_schema(self, script: str) -> None:
        with self._lock, self._db:
            self._db.executescript(script)

    def query(self, sql: str, args: tuple = ()) -> list[tuple]:
        return self._q(sql, args)

    def insert(self, sql: str, args: tuple = ()) -> int:
        """Run an INSERT; returns the new rowid."""
        with self._lock, self._db:
            return self._db.execute(sql, args).lastrowid

    def get_content(self, cid: str) -> tuple[dict, float] | None:
        rows = self._q("SELECT data, fetched_at FROM f1_content WHERE cid = ?", (cid,))
        return (json.loads(rows[0][0]), rows[0][1]) if rows else None

    def put_content(self, cid: str, data: dict) -> None:
        self._q("INSERT OR REPLACE INTO f1_content VALUES (?, ?, ?)", (cid, json.dumps(data), time.time()))

    def family_delay(self, channel: str, clock: str) -> float | None:
        """The channel's latest calibration on a clock comparable with `clock`, if it has
        a family (CLOCK_FAMILY)."""
        family = CLOCK_FAMILY.get(clock)
        clocks = [c for c, f in CLOCK_FAMILY.items() if f == family] if family else []
        if not clocks:
            return None
        rows = self._q(
            f"SELECT delay FROM content_delay WHERE channel = ? AND clock IN ({', '.join('?' * len(clocks))}) "
            "ORDER BY updated_at DESC, rowid DESC LIMIT 1", (channel, *clocks)
        )
        return rows[0][0] if rows else None

    def content_delay(self, cid: str, channel: str, clock: str) -> float | None:
        rows = self._q(
            "SELECT delay FROM content_delay WHERE cid = ? AND channel = ? AND clock = ?", (cid, channel, clock)
        )
        return rows[0][0] if rows else None

    def set_delay(self, cid: str, channel: str, clock: str, delay: float) -> None:
        now = time.time()
        self._q("INSERT OR REPLACE INTO content_delay VALUES (?, ?, ?, ?, ?)", (cid, channel, clock, delay, now))
        if clock in TRUSTED_CLOCKS:
            self._q("INSERT OR REPLACE INTO channel_delay VALUES (?, ?, ?)", (channel, delay, now))
