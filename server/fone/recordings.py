"""Discover recorder output files and parse what their names tell us."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MEDIA_EXTS = {".flv", ".mp4", ".ts", ".m4a", ".mkv"}
SKIP_DIRS = {"log", "logs", "@eaDir", "#recycle"}

# Ordered most- to least-precise. Each entry: (regex, precision in seconds).
# Covers BililiveRecorder's default `yyyyMMdd-HHmmss-fff`, the minute-precision
# template this recorder currently uses, and blrec-style `YYYY-MM-DD_HH-MM-SS`.
_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[-_ ](\d{2})(\d{2})(\d{2})[-_.](\d{3})(?!\d)"), 0.001),
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[-_ ](\d{2})(\d{2})(\d{2})(?!\d)"), 1.0),
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[ _T](\d{2})[-:.](\d{2})[-:.](\d{2})(?!\d)"), 1.0),
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[-_ ](\d{2})(\d{2})(?!\d)"), 60.0),
]
_LEADING_JUNK = re.compile(r"^[\s\-_.]+|[\s\-_.]+$")
_BREC_PREFIX = re.compile(r"^录制-\d+-?")


@dataclass
class Recording:
    id: str
    relpath: str
    path: Path
    channel: str
    name: str
    title: str
    size: int
    mtime: float
    name_time: float | None  # unix seconds parsed from the filename
    name_precision: float | None
    # "recorder": captured live. "import": a replay backfill placed on the wall clock; it
    # only fills wall time no recording covers.
    source: str = "recorder"


def file_id(relpath: str) -> str:
    return hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:16]


def parse_name(stem: str, tz: ZoneInfo) -> tuple[float | None, float | None, str]:
    """Return (unix_time, precision_seconds, title) parsed from a filename stem."""
    stem = _BREC_PREFIX.sub("", stem)
    for pattern, precision in _PATTERNS:
        m = pattern.search(stem)
        if not m:
            continue
        parts = [int(g) for g in m.groups()]
        year, month, day, hour, minute = parts[:5]
        second = parts[5] if len(parts) > 5 else 0
        micro = parts[6] * 1000 if len(parts) > 6 else 0
        try:
            dt = datetime(year, month, day, hour, minute, second, micro, tzinfo=tz)
        except ValueError:
            continue
        title = _LEADING_JUNK.sub("", stem[m.end():]) or _LEADING_JUNK.sub("", stem[: m.start()])
        return dt.timestamp(), precision, title
    return None, None, stem


def scan(rec_dir: Path, tz: ZoneInfo, aliases: dict[str, str] | None = None,
         import_dir: Path | None = None) -> list[Recording]:
    """Every media file under rec_dir (and import_dir, when it isn't inside rec_dir).
    Files under import_dir are tagged source="import"."""
    roots = [(rec_dir, "")]
    if import_dir is not None and not import_dir.is_relative_to(rec_dir):
        roots.append((import_dir, f"{import_dir.name}/"))  # same ids as when nested under rec_dir
    found: list[Recording] = []
    for root_dir, prefix in roots:
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
            for fname in files:
                path = Path(root) / fname
                if path.suffix.lower() not in MEDIA_EXTS:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                relpath = prefix + path.relative_to(root_dir).as_posix()
                # The recorder's `{{ name }}/` template puts each streamer in its own folder,
                # wherever that folder sits (one or several recorder roots under rec_dir).
                channel = path.parent.name if path.parent != root_dir else ""
                channel = (aliases or {}).get(channel, channel)
                name_time, precision, title = parse_name(path.stem, tz)
                imported = import_dir is not None and path.is_relative_to(import_dir)
                found.append(
                    Recording(
                        id=file_id(relpath),
                        relpath=relpath,
                        path=path,
                        channel=channel,
                        name=fname,
                        title=title,
                        size=st.st_size,
                        mtime=st.st_mtime,
                        name_time=name_time,
                        name_precision=precision,
                        source="import" if imported else "recorder",
                    )
                )
    found.sort(key=lambda r: (r.channel, r.name_time or r.mtime, r.name))
    return found
