"""F1 live-timing state at any UTC moment, rebuilt from recorded topic streams.

A session directory holds one `<Topic>.jsonStream` per topic in the static archive's
format: each line is `HH:MM:SS.mmm<json>`, the offset from the session's t0 at which the
message reached the timing screens. Archive downloads and our own live captures share it,
so one loader serves both, and a capture that is still being written is tailed on read.

State at UTC t = every message with offset <= t - t0 merged in arrival order (SignalR merge
rules). Nothing later is ever looked at: that is what keeps the dashboard spoiler-free.
Position.z samples carry their own sensor Timestamp (0..1.7 s before they arrive), the
moment the car was physically there; the map uses those.
"""

from __future__ import annotations

import base64
import bisect
import json
import math
import pickle
import re
import threading
import time
import zlib
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OFFSET_LEN = 12  # "HH:MM:SS.mmm"
BOM = b"\xef\xbb\xbf"
SNAP_EVERY = 150  # messages between pickled snapshots of a topic's merged state
POSITION_TOPIC = "Position.z"
# What the state needs. Other topics in a directory (Heartbeat, TimingStats, ...) are skipped.
STATE_TOPICS = {
    "SessionInfo", "SessionStatus", "DriverList", "TimingData", "TimingAppData", "TrackStatus",
    "RaceControlMessages", "WeatherData", "ExtrapolatedClock", "LapCount",
}
TRACK_FLAG = {"1": "GREEN", "2": "YELLOW", "4": "SC", "5": "RED", "6": "VSC", "7": "VSC"}
COMPOUNDS = {"SOFT", "MEDIUM", "HARD", "INTERMEDIATE", "WET"}
ENDED = {"Finished", "Finalised", "Ends"}
RCM_SHOW_SECONDS = 60  # a race-control message stays on the conditions panel this long

_FRACTION = re.compile(r"\.(\d+)")


# ---- parsing helpers ---------------------------------------------------------


def iso(s: str) -> float:
    """'2026-10-03T08:00:00.172Z' (any number of fraction digits; no zone = UTC) -> unix seconds."""
    s = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], s.strip().replace("Z", "+00:00"), count=1)
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def iso_ms(t: float) -> str:
    """Unix seconds -> '2026-10-03T08:00:00.172Z' (exact to the millisecond, no float drift)."""
    ms = round(t * 1000)
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def format_offset(o: float) -> str:
    ms = max(0, round(o * 1000))  # integer maths: never renders 59.9996 s as "60.000"
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def parse_offset(s: str) -> float:
    h, m, rest = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def inflate(b64: str) -> Any:
    """Position.z / CarData.z payloads: base64 of raw-deflated JSON."""
    return json.loads(zlib.decompress(base64.b64decode(b64), -zlib.MAX_WBITS))


def deflate(obj: Any) -> str:
    c = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = c.compress(json.dumps(obj, separators=(",", ":")).encode()) + c.flush()
    return base64.b64encode(raw).decode()


def _copy(v):
    if isinstance(v, dict):
        return {k: _copy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_copy(x) for x in v]
    return v


def merge(base, delta):
    """SignalR live-timing merge: dicts merge recursively, a dict applied to a list updates it by
    index ({"3": {...}}), `_deleted` removes keys, anything else replaces. Never aliases delta."""
    if not isinstance(delta, dict):
        return _copy(delta)
    if isinstance(base, list):
        for k, v in delta.items():
            if k == "_deleted":
                for d in sorted((int(i) for i in v), reverse=True):
                    if d < len(base):
                        base.pop(d)
                continue
            i = int(k)
            while len(base) <= i:
                base.append(None)
            base[i] = merge(base[i], v)
        return base
    if not isinstance(base, dict):
        base = {}
    for k, v in delta.items():
        if k == "_deleted":
            for d in v:
                base.pop(str(d), None)
            continue
        base[k] = merge(base.get(k), v)
    return base


def seq(x) -> list:
    """Lists arrive either as JSON lists or as {"0": .., "3": ..} index dicts."""
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        try:
            n = max(int(k) for k in x) + 1 if x else 0
        except ValueError:
            return list(x.values())
        out = [None] * n
        for k, v in x.items():
            out[int(k)] = v
        return out
    return []


def secs(s) -> float | None:
    """'1:35.130' / '35.130' / '+0.298' / '00:17:59' -> seconds."""
    if not s or not isinstance(s, str):
        return None
    try:
        v = 0.0
        for part in s.strip().lstrip("+").split(":"):
            v = v * 60 + float(part)
        return v
    except ValueError:
        return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _text(v) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


# ---- stream files ------------------------------------------------------------


class StreamFile:
    """Incremental reader of one .jsonStream. Each read() returns the complete lines appended
    since the previous call, so a live capture can be read while it is being written."""

    def __init__(self, path: Path):
        self.path = path
        self.pos = 0

    def read(self) -> list[tuple[float, str]]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size <= self.pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read(size - self.pos)
        end = data.rfind(b"\n")
        if end < 0:
            return []  # half-written line: wait for its newline
        chunk = data[: end + 1]
        if self.pos == 0 and chunk.startswith(BOM):
            chunk = chunk[len(BOM):]
        self.pos += end + 1
        out = []
        for ln in chunk.decode("utf-8", errors="replace").split("\n"):
            ln = ln.rstrip("\r")
            if len(ln) > OFFSET_LEN:
                try:
                    out.append((parse_offset(ln[:OFFSET_LEN]), ln[OFFSET_LEN:]))
                except ValueError:
                    continue
        return out


def read_stream(path: Path) -> list[tuple[float, str]]:
    return StreamFile(path).read()


# ---- per-topic timeline --------------------------------------------------------


class Topic:
    """Arrival-ordered messages of one topic plus periodic snapshots of the merged state.
    Messages stay JSON text (a sixth of the memory of parsed dicts) and are parsed on replay."""

    __slots__ = ("name", "offs", "msgs", "snap_n", "snap_blob", "_head")

    def __init__(self, name: str):
        self.name = name
        self.offs: list[float] = []
        self.msgs: list[str] = []
        self.snap_n: list[int] = [0]
        self.snap_blob: list[bytes] = [pickle.dumps(None)]
        self._head = None

    def append(self, off: float, raw: str) -> None:
        # Arrival order wins: a timestamp a few ms behind its predecessor is clamped, not reordered.
        if self.offs and off < self.offs[-1]:
            off = self.offs[-1]
        self.offs.append(off)
        self.msgs.append(raw)
        self._head = merge(self._head, json.loads(raw))
        if len(self.msgs) % SNAP_EVERY == 0:
            self.snap_n.append(len(self.msgs))
            self.snap_blob.append(pickle.dumps(self._head, protocol=5))

    def at(self, off: float):
        """Merged state of every message with offset <= off."""
        n = bisect.bisect_right(self.offs, off)
        k = bisect.bisect_right(self.snap_n, n) - 1
        state = pickle.loads(self.snap_blob[k])
        for m in self.msgs[self.snap_n[k]:n]:
            state = merge(state, json.loads(m))
        return state


class Positions:
    """Position.z decoded into per-car sample arrays keyed by sensor Timestamp (UTC)."""

    def __init__(self):
        self.t: dict[str, array] = {}
        self.x: dict[str, array] = {}
        self.y: dict[str, array] = {}
        self.on: dict[str, array] = {}

    def add(self, raw: str) -> None:
        """One Position.z line: base64+deflate text (archive, live feed) or a decoded object."""
        p = json.loads(raw)
        if isinstance(p, str):
            p = inflate(p)
        for batch in p.get("Position") or []:
            ts = iso(batch["Timestamp"])
            for num, e in (batch.get("Entries") or {}).items():
                ta = self.t.get(num)
                if ta is None:
                    ta = self.t[num] = array("d")
                    self.x[num], self.y[num], self.on[num] = array("i"), array("i"), array("b")
                if ta and ts <= ta[-1]:
                    continue  # duplicate (re-sent upload) or out of order
                x, y = int(e.get("X", 0)), int(e.get("Y", 0))
                ta.append(ts)
                self.x[num].append(x)
                self.y[num].append(y)
                self.on[num].append(1 if e.get("Status") == "OnTrack" and (x or y) else 0)

    def window(self, t_from: float, t_to: float) -> dict[str, list[list]]:
        """On-track samples with sensor time in [t_from, t_to], plus the one just before t_from
        so the client can interpolate from the first frame. Never anything after t_to."""
        out = {}
        for num, ta in self.t.items():
            i = max(0, bisect.bisect_left(ta, t_from) - 1)
            j = bisect.bisect_right(ta, t_to)
            xs, ys, on = self.x[num], self.y[num], self.on[num]
            pts = [[round(ta[k], 3), xs[k], ys[k]] for k in range(i, j) if on[k]]
            if pts:
                out[num] = pts
        return out


# ---- the session -------------------------------------------------------------


def session_kind(info: dict) -> str:
    typ, name = (info.get("Type") or "").lower(), (info.get("Name") or "").lower()
    if typ == "qualifying" or "qualifying" in name or "shootout" in name:
        return "quali"
    if typ == "race" or name in ("race", "sprint"):
        return "race"
    return "practice"


class SessionTimeline:
    """Every topic of one session directory, queryable at any UTC (thread-safe)."""

    def __init__(self, directory: Path, t0: float, refresh_interval: float = 0.5):
        self.dir = directory
        self.t0 = t0
        self.refresh_interval = refresh_interval
        self.topics: dict[str, Topic] = {}
        self.pos = Positions()
        self._files: dict[str, StreamFile] = {}
        self._refreshed = -math.inf
        self._lock = threading.Lock()
        with self._lock:
            self._refresh(force=True)

    # -- loading
    def _refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._refreshed < self.refresh_interval:
            return
        self._refreshed = now
        for p in sorted(self.dir.glob("*.jsonStream")):
            name = p.name[: -len(".jsonStream")]
            if name != POSITION_TOPIC and name not in STATE_TOPICS:
                continue
            sf = self._files.get(name)
            if sf is None:
                sf = self._files[name] = StreamFile(p)
            for off, raw in sf.read():
                if name == POSITION_TOPIC:
                    self.pos.add(raw)
                else:
                    topic = self.topics.get(name)
                    if topic is None:
                        topic = self.topics[name] = Topic(name)
                    topic.append(off, raw)

    def _at(self, name: str, off: float) -> dict:
        t = self.topics.get(name)
        v = t.at(off) if t else None
        return v if isinstance(v, dict) else {}

    def info(self) -> dict:
        """SessionInfo (static metadata: safe to show at any t)."""
        with self._lock:
            self._refresh()
            t = self.topics.get("SessionInfo")
            return t.at(math.inf) if t and t.offs else {}

    def end(self) -> float | None:
        """UTC of the newest message (live: the capture's edge)."""
        with self._lock:
            self._refresh()
            offs = [t.offs[-1] for t in self.topics.values() if t.offs]
            return self.t0 + max(offs) if offs else None

    # -- queries
    def state(self, t: float) -> dict:
        with self._lock:
            self._refresh()
            return self._state(t)

    def positions(self, t_from: float, t_to: float) -> dict[str, list[list]]:
        with self._lock:
            self._refresh()
            return self.pos.window(t_from, t_to)

    def _state(self, t: float) -> dict:
        off = t - self.t0
        if off < 0 or not self.topics:
            return {"t": t, "available": False, "reason": "no timing data before this moment"}
        info = self._at("SessionInfo", off) or self._at("SessionInfo", math.inf)
        kind = session_kind(info)
        td = self._at("TimingData", off)
        app = self._at("TimingAppData", off)
        drivers = self._at("DriverList", off)
        status = self._at("SessionStatus", off).get("Status")
        part = td.get("SessionPart") if kind == "quali" else None
        part = part if isinstance(part, int) and part > 0 else None

        rows = [
            _row(str(num), ln, drivers.get(num) or {}, (app.get("Lines") or {}).get(num) or {}, kind, part)
            for num, ln in (td.get("Lines") or {}).items() if isinstance(ln, dict)
        ]
        rows.sort(key=lambda r: (r["pos"] is None, r["pos"] or 0, r["num"]))

        rcm = [m for m in seq(self._at("RaceControlMessages", off).get("Messages")) if isinstance(m, dict)]
        lap = self._at("LapCount", off)
        return {
            "t": t,
            "available": True,
            "session": {"key": info.get("Key"), "name": info.get("Name"), "type": info.get("Type"),
                        "meeting": (info.get("Meeting") or {}).get("Name")},
            "status": status,
            "clock": {"remaining": _remaining(self._at("ExtrapolatedClock", off), t),
                      "lap": lap.get("CurrentLap"), "laps": lap.get("TotalLaps")},
            "part": part,
            "track": _track(self._at("TrackStatus", off), rcm, status, t),
            "weather": _weather(self._at("WeatherData", off)),
            "tower": rows,
        }

    # -- geometry
    def laps(self) -> list[tuple[float, str, float]]:
        """(lap seconds, driver, UTC the lap time appeared) for every completed lap."""
        with self._lock:
            self._refresh()
            td = self.topics.get("TimingData")
            if not td:
                return []
            pairs = list(zip(td.offs, td.msgs))
        out = []
        for off, raw in pairs:
            if '"LastLapTime"' not in raw:
                continue
            for num, ln in (json.loads(raw).get("Lines") or {}).items():
                if isinstance(ln, dict):
                    v = secs((ln.get("LastLapTime") or {}).get("Value"))
                    if v:
                        out.append((v, num, self.t0 + off))
        return out

    def lap_path(self, num: str, lap: float, end: float) -> list[tuple[int, int]] | None:
        """The car's sensor path over one lap that ended around `end`, or None if the data is
        patchy (dropouts, off track, pit lane) or doesn't close into a loop."""
        with self._lock:
            ta = self.pos.t.get(num)
            if not ta:
                return None
            i0 = bisect.bisect_left(ta, end - lap - 3.0)
            i1 = bisect.bisect_right(ta, end + 1.0)
            times = ta[i0:i1]
            if len(times) < 2.5 * lap:  # Position.z runs at ~3.7 Hz
                return None
            if any(b - a > 2.0 for a, b in zip(times, times[1:])):
                return None
            if not all(self.pos.on[num][i0:i1]):
                return None
            pts = list(zip(self.pos.x[num][i0:i1], self.pos.y[num][i0:i1]))
        # One loop: cut where the path, past half way, comes closest to where it started.
        sx, sy = pts[0]
        k = min(range(len(pts) // 2, len(pts)), key=lambda i: (pts[i][0] - sx) ** 2 + (pts[i][1] - sy) ** 2)
        loop = pts[: k + 1]
        length = sum(math.dist(a, b) for a, b in zip(loop, loop[1:]))
        if length <= 0 or math.dist(loop[0], loop[-1]) > 0.02 * length:
            return None
        return loop


def _row(num: str, ln: dict, drv: dict, app_ln: dict, kind: str, part: int | None) -> dict:
    stints = [s for s in seq(app_ln.get("Stints")) if isinstance(s, dict)]
    cur = stints[-1] if stints else {}
    last = ln.get("LastLapTime") or {}
    if kind == "quali":
        bests = [(b or {}).get("Value") for b in seq(ln.get("BestLapTimes"))]
        stats = seq(ln.get("Stats"))
        p = (part or 1) - 1
        if ln.get("KnockedOut"):  # show the time of the part they went out in, like the TV tower
            p = max((i for i in range(min(p + 1, len(bests))) if _text(bests[i])), default=p)
        st = (stats[p] if p < len(stats) else None) or {}
        best = bests[p] if p < len(bests) else None
        gap, interval = st.get("TimeDiffToFastest"), st.get("TimeDifftoPositionAhead")
    elif kind == "race":
        best = (ln.get("BestLapTime") or {}).get("Value")
        gap = ln.get("GapToLeader")
        itv = ln.get("IntervalToPositionAhead")
        interval = itv.get("Value") if isinstance(itv, dict) else itv
    else:
        best = (ln.get("BestLapTime") or {}).get("Value")
        gap, interval = ln.get("TimeDiffToFastest"), ln.get("TimeDiffToPositionAhead")

    pos = ln.get("Position")
    pos = int(pos) if isinstance(pos, str) and pos.isdigit() else ln.get("Line")
    compound = str(cur.get("Compound") or "").upper()
    pits = ln.get("NumberOfPitStops")
    colour = drv.get("TeamColour")
    first, last_name = drv.get("FirstName"), drv.get("LastName")
    return {
        "pos": pos if isinstance(pos, int) else None,
        "num": num,
        "code": drv.get("Tla"),
        "name": f"{first} {last_name}" if first and last_name else drv.get("FullName") or drv.get("BroadcastName"),
        "team": drv.get("TeamName"),
        "color": f"#{colour}" if colour else None,
        "gap": _text(gap),
        "interval": _text(interval),
        "last": _text(last.get("Value")),
        "lastFlag": ("overall" if last.get("OverallFastest") else "personal" if last.get("PersonalFastest") else "none")
        if _text(last.get("Value")) else "none",
        "best": _text(best),
        "tyre": compound if compound in COMPOUNDS else "UNKNOWN",
        "tyreAge": int(_num(cur.get("TotalLaps")) or 0),
        "newTyre": str(cur.get("New")).lower() == "true",
        "pits": int(pits) if isinstance(pits, int) else max(0, len(stints) - 1),
        "inPit": bool(ln.get("InPit")),
        "pitOut": bool(ln.get("PitOut")),
        "retired": bool(ln.get("Retired")),
        "knockedOut": bool(ln.get("KnockedOut")),
    }


def _remaining(clk: dict, t: float) -> str | None:
    rem = secs(clk.get("Remaining"))
    if rem is None:
        return None
    if clk.get("Extrapolating") and clk.get("Utc"):
        rem = max(0.0, rem - max(0.0, t - iso(clk["Utc"])))
    # A countdown shows whole seconds rounded up: 18:00 until a full second has gone.
    m, s = divmod(math.ceil(rem - 1e-6), 60)
    return f"{m:02d}:{s:02d}"


def _track(ts: dict, rcm: list[dict], status: str | None, t: float) -> dict:
    sectors: dict = {}  # sector -> YELLOW / DOUBLE YELLOW, from race control
    for m in rcm:
        if m.get("Category") != "Flag":
            continue
        flag, scope = m.get("Flag"), m.get("Scope")
        if scope == "Track":
            sectors.clear()
        elif scope == "Sector":
            if flag in ("YELLOW", "DOUBLE YELLOW"):
                sectors[m.get("Sector")] = flag
            elif flag == "CLEAR":
                sectors.pop(m.get("Sector"), None)
    if status in ENDED:
        flag = "CHEQUERED"
    else:
        flag = TRACK_FLAG.get(str(ts.get("Status") or ""), "GREEN")
        if flag == "YELLOW" and "DOUBLE YELLOW" in sectors.values():
            flag = "DOUBLE YELLOW"
    message = None
    if rcm and rcm[-1].get("Utc"):
        try:
            if iso(rcm[-1]["Utc"]) >= t - RCM_SHOW_SECONDS:
                message = rcm[-1].get("Message")
        except ValueError:
            pass
    return {"status": ts.get("Message"), "flag": flag, "message": message}


def _weather(wx: dict) -> dict:
    wind_dir = _num(wx.get("WindDirection"))
    return {
        "air": _num(wx.get("AirTemp")),
        "track": _num(wx.get("TrackTemp")),
        "humidity": _num(wx.get("Humidity")),
        "rain": (_num(wx.get("Rainfall")) or 0) > 0,
        "windSpeed": _num(wx.get("WindSpeed")),
        "windDir": int(wind_dir) if wind_dir is not None else None,
    }


# ---- track map -----------------------------------------------------------------


def display_rotation(points: list[tuple[int, int]]) -> float:
    """Degrees to turn the outline counter-clockwise (F1 axes, y up) so its long axis lies
    horizontal: x' = x cos r - y sin r, y' = x sin r + y cos r."""
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points) / n
    syy = sum((p[1] - my) ** 2 for p in points) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points) / n
    return round(-math.degrees(0.5 * math.atan2(2 * sxy, sxx - syy)), 1)


def track_outline(timelines: list[SessionTimeline], candidates: int = 12) -> dict | None:
    """Outline from the fastest lap (across the given sessions) whose positions form a clean loop."""
    laps = sorted((lap, num, end, i) for i, tl in enumerate(timelines) for lap, num, end in tl.laps() if lap > 30)
    for lap, num, end, i in laps[:candidates]:
        loop = timelines[i].lap_path(num, lap, end)
        if loop:
            xs, ys = [p[0] for p in loop], [p[1] for p in loop]
            return {
                "outline": [list(p) for p in loop],
                "rotation": display_rotation(loop),
                "bounds": [min(xs), min(ys), max(xs), max(ys)],
                "source": {"session": (timelines[i].info().get("Key")), "driver": num, "lap": lap},
            }
    return None
