"""Pure helpers for /api/timeline: which wall time each file plays, and ad mutes."""

from __future__ import annotations

from .extractor import Piece

# An import fills a hole in the recordings only if the hole is at least this long: a
# shorter one isn't worth seeking into another file and back.
MIN_FILL_SECONDS = 2.0
# Two recordings of the same moment (a reconnect's overlap): the later one takes over
# where the earlier ends, unless what's left of it is only float noise.
MIN_CLIP_SECONDS = 0.1
# The sponsor clips of one reel follow each other with up to ~2.4 s of gated silence
# between them. Mute straight through such a gap rather than un-muting for a moment.
MUTE_MERGE_SECONDS = 3.0


def uncovered(p: Piece, covered: list[tuple[float, float]], min_len: float = MIN_FILL_SECONDS) -> list[Piece]:
    """The parts of p that no covered span (sorted, disjoint) overlaps. A piece that
    overlaps nothing comes back as is; clipped remainders shorter than min_len are dropped."""
    a, b = p.wall, p.wall + p.duration
    holes, at = [], a
    for c0, c1 in covered:
        if c1 <= at or c0 >= b:
            continue
        if c0 > at:
            holes.append((at, c0))
        at = max(at, c1)
    if at == a and not holes:
        return [p]
    if at < b:
        holes.append((at, b))
    return [Piece(wall=h0, media=p.media + (h0 - a), duration=h1 - h0)
            for h0, h1 in holes if h1 - h0 >= min_len]


def union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def mutes(spans: list[dict], pieces: list[Piece], offset: float) -> list[dict]:
    """A file's ad spans ({start, end, name} in file-local media time) -> mutes
    {w, d, m, name} on the wall clock and the stitched playlist (m = offset + media), through
    the pieces of the file that are actually played."""
    merged: list[dict] = []
    for s in sorted(spans, key=lambda s: s["start"]):
        if merged and s["start"] - merged[-1]["end"] <= MUTE_MERGE_SECONDS:
            merged[-1]["end"] = max(merged[-1]["end"], s["end"])
            merged[-1]["name"] += "+" + s["name"]
        else:
            merged.append(dict(s))
    out = []
    for s in merged:
        for p in pieces:
            a, b = max(s["start"], p.media), min(s["end"], p.media + p.duration)
            if b > a:
                out.append({"w": p.wall + (a - p.media), "d": b - a, "m": offset + a, "name": s["name"]})
    return out


def content_span(data: dict) -> tuple[float, float] | None:
    """Real-world span of an F1 TV video (F1Mapper.content): the session if scheduled,
    else the replay's p0 .. p0 + duration."""
    a, b = data.get("scheduledStart"), data.get("scheduledEnd")
    if a and b and b > a:
        return a, b
    p0, dur = data.get("p0"), data.get("duration")
    if p0 and dur:
        return p0, p0 + float(dur)
    return (a, a + 2 * 3600) if a else None
