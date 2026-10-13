"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


FEATURES = ("timing", "ads", "screenclock", "video", "backfill", "danmaku")
OFF = ("0", "false", "no", "off")


@dataclass(frozen=True)
class Config:
    # Recorder output root (mounted read-only), scanned recursively. A recording's
    # channel is its parent folder: BililiveRecorder's `{{ name }}/` template.
    rec_dir: Path
    # Local scratch: sqlite DB + extracted audio HLS. Should be on local disk.
    data_dir: Path
    # Timezone the recorder uses when it renders timestamps into filenames.
    filename_tz: ZoneInfo
    # HLS segment length. Shorter = fresher live edge, more files.
    segment_seconds: float
    # A recording whose mtime is older than this is considered finished.
    growing_idle_seconds: float
    # When a newer file appears in the same channel, the previous file is
    # finalised once it has been idle this long (stream reconnect handling).
    reconnect_idle_seconds: float
    scan_interval: float
    # Finished recordings newer than this are extracted eagerly; older ones on demand.
    eager_days: float
    max_parallel_extractions: int
    # Streamer delay (seconds behind real time) used before you've calibrated.
    default_delay: float
    # Recorder folder name -> channel name. 录播姬 names folders after the account
    # (马莎拉车) while you know the show as 胡闹P房; aliases also survive renames.
    channel_aliases: dict[str, str]
    # Where backfill writes replays it imports (`<import_dir>/<channel>/...flv`). Files there
    # are recordings with source "import": they only fill what the recorder missed.
    # Usually inside rec_dir (a nested rw mount); scanned as a second root otherwise.
    import_dir: Path | None = None
    # Bilibili room id -> channel, for the live-status log that drives backfill.
    rooms: dict[int, str] = field(default_factory=dict)
    # Streamer's reaction time on top of the measured screen delay: commentary about an
    # event is heard this long after the streamer's screen showed it.
    reaction_seconds: float = 0.9
    # Optional services, each switchable off (FONE_<NAME>=0).
    features: frozenset[str] = frozenset(FEATURES)


def load() -> Config:
    env = os.environ.get
    return Config(
        rec_dir=Path(env("FONE_REC_DIR", "/rec")),
        data_dir=Path(env("FONE_DATA_DIR", "/data")),
        filename_tz=ZoneInfo(env("FONE_FILENAME_TZ", "Asia/Shanghai")),
        segment_seconds=float(env("FONE_SEGMENT_SECONDS", "2")),
        growing_idle_seconds=float(env("FONE_GROWING_IDLE_SECONDS", "90")),
        reconnect_idle_seconds=float(env("FONE_RECONNECT_IDLE_SECONDS", "8")),
        scan_interval=float(env("FONE_SCAN_INTERVAL", "3")),
        eager_days=float(env("FONE_EAGER_DAYS", "10")),
        max_parallel_extractions=int(env("FONE_MAX_PARALLEL", "2")),
        default_delay=float(env("FONE_DEFAULT_DELAY", "40")),
        channel_aliases=parse_aliases(env("FONE_CHANNEL_ALIASES", "")),
        import_dir=Path(env("FONE_IMPORT_DIR")) if env("FONE_IMPORT_DIR") else None,
        rooms=parse_rooms(env("FONE_ROOMS", "")),
        reaction_seconds=float(env("FONE_REACTION_SECONDS", "0.9")),
        features=frozenset(f for f in FEATURES if env(f"FONE_{f.upper()}", "1").strip().lower() not in OFF),
    )


def parse_aliases(spec: str) -> dict[str, str]:
    """"folder=channel,folder2=channel" -> dict."""
    out = {}
    for pair in filter(None, (x.strip() for x in spec.split(","))):
        folder, _, channel = pair.partition("=")
        if folder.strip() and channel.strip():
            out[folder.strip()] = channel.strip()
    return out


def parse_rooms(spec: str) -> dict[int, str]:
    """"23954427=胡闹P房,123=other" -> {23954427: "胡闹P房", 123: "other"}."""
    out = {}
    for pair in filter(None, (x.strip() for x in spec.split(","))):
        room, _, channel = pair.partition("=")
        if room.strip().isdigit() and channel.strip():
            out[int(room.strip())] = channel.strip()
    return out
