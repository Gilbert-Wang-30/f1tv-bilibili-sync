"""Synthetic viewers for the two danmaku-origin fixtures next to this file.

dmks_live_7a5add15.msgpack is a danmakus.com live archive (/v3/lives/<id>/full) and
dm_42232054404.xml the danmaku XML of the replay Bilibili made of that live (BV1P1as6hE9y, 巡游,
2026-09-26). The tests compute S0 from them, the origin Bilibili's importer used when it re-posted
the live danmaku into the replay (backfill_start.danmaku_origin matches the XML's (crc32(uid),
text) pairs against the archive's). Both were downloaded with real viewers in them; this script
rewrote them in place so that no real viewer remains, and the committed fixtures are its output:

- every viewer uid becomes 10000 + k (k by first appearance in the archive's actor table; the
  XML's few hashes the table doesn't know continue the sequence) and every nickname
  viewer_<actor index>; the XML's user hashes are crc32 of the new uids, so exactly the pairs
  that matched before still match;
- every message text becomes two words from WORDS, numbered by first appearance (same text ->
  same words, different texts -> different words), in the archive and the XML alike; the
  archive's word cloud is recounted from them;
- the streamer's channel entry becomes [1000, 'streamer', 1] and danmakus.com's uploaders
  uploader_<id> (their 来自 records too);
- the XML's danmaku ids become 2e18 + position;
- everything else stays: the archive's layout, record count, kinds, types, source ids, base time
  and every delta, the live's info (start/stop dates, counts, the viewer-count curve), gifts and
  stickers; the XML's progress, mode, size, colour, timestamp, pool and weight columns.

Run it again on the committed fixtures and nothing changes. Run it on a fresh archive + XML pair
(paths as arguments) to make a new synthetic pair from real downloads.

    python make_synthetic.py [archive.msgpack replay.xml]
"""

from __future__ import annotations

import collections
import html
import re
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # server/
from fone.backfill_start import danmaku_origin, parse_dmks_live, unpackb  # noqa: E402

UID0 = 10000
WORDS = ["box", "pit", "lap", "tyre", "gap", "drs", "flag", "turn", "grid", "pole", "kerb", "push", "slow",
         "fast", "wet", "dry", "soft", "hard", "medium", "undercut", "stint", "radio", "safety", "yellow",
         "green", "blue", "red", "chequered", "sector", "delta", "fuel", "lift"]
D_RE = re.compile(r'<d p="([^"]+)">(.*?)</d>', re.S)


def packb(o) -> bytes:
    """Minimal msgpack encoder, the inverse of backfill_start.unpackb for what the archive holds."""
    if o is None:
        return b"\xc0"
    if isinstance(o, bool):
        return b"\xc3" if o else b"\xc2"
    if isinstance(o, int):
        if 0 <= o <= 0x7F or -32 <= o < 0:
            return struct.pack("b" if o < 0 else "B", o)
        for t, fmt, lo, hi in ((0xCC, ">B", 0, 0xFF), (0xCD, ">H", 0, 0xFFFF), (0xCE, ">I", 0, 0xFFFFFFFF),
                               (0xCF, ">Q", 0, (1 << 64) - 1), (0xD0, ">b", -0x80, 0), (0xD1, ">h", -0x8000, 0),
                               (0xD2, ">i", -(1 << 31), 0), (0xD3, ">q", -(1 << 63), 0)):
            if lo <= o <= hi:
                return bytes([t]) + struct.pack(fmt, o)
        raise ValueError(f"int out of range: {o}")
    if isinstance(o, float):
        return b"\xcb" + struct.pack(">d", o)
    if isinstance(o, (str, bytes)):
        b = o.encode("utf-8") if isinstance(o, str) else o
        if isinstance(o, str) and len(b) < 32:
            return bytes([0xA0 | len(b)]) + b
        return _head(len(b), None, *((0xD9, 0xDA, 0xDB) if isinstance(o, str) else (0xC4, 0xC5, 0xC6))) + b
    if isinstance(o, list):
        return _head(len(o), 0x90, None, 0xDC, 0xDD) + b"".join(packb(v) for v in o)
    if isinstance(o, dict):
        return _head(len(o), 0x80, None, 0xDE, 0xDF) + b"".join(packb(k) + packb(v) for k, v in o.items())
    raise TypeError(f"cannot pack {type(o).__name__}")


def _head(n: int, fix: int | None, t8: int | None, t16: int, t32: int) -> bytes:
    if fix is not None and n < 16:
        return bytes([fix | n])
    if t8 is not None and n < 0x100:
        return bytes([t8, n])
    if n < 0x10000:
        return bytes([t16]) + struct.pack(">H", n)
    return bytes([t32]) + struct.pack(">I", n)


def words(n: int) -> str:
    """Text number n as words: base-len(WORDS) digits, least significant first, two at least."""
    out = []
    while n or len(out) < 2:
        out.append(WORDS[n % len(WORDS)])
        n //= len(WORDS)
    return " ".join(out)


def crc(uid: int) -> str:
    return format(zlib.crc32(str(uid).encode()), "x")


def rewrite(archive: Path, xml_path: Path) -> None:
    d = unpackb(archive.read_bytes())
    info, (_, actors, _, recs) = d[1], d[2]
    uids: dict[int, int] = {}  # real uid -> synthetic, one per distinct uid (a renamed viewer is listed twice)
    for uid, _ in actors:
        uids.setdefault(uid, UID0 + len(uids))
    hashes = {crc(old): crc(new) for old, new in uids.items()}
    texts: dict[str, str] = {}

    def text(s: str) -> str:
        s = s.strip()
        return texts.setdefault(s, words(len(texts))) if s else s

    d[0] = [1000, "streamer", 1]
    for i, a in enumerate(actors):
        a[0], a[1] = uids[a[0]], f"viewer_{i:03d}"
    uploaders = {}
    if len(info) > 18 and isinstance(info[18], list):  # danmakus.com's sources: [.., name, id, ..]
        for src in info[18]:
            if isinstance(src, list) and len(src) > 5 and isinstance(src[4], str) and src[5]:
                uploaders[src[4]] = src[4] = f"uploader_{src[5]}"
    for r in recs:
        if len(r) > 5 and isinstance(r[5], list) and r[5] and isinstance(r[5][0], str):
            if r[4] == 1:
                r[5][0] = text(r[5][0])
            elif r[4] == 9 and r[5][0].startswith("来自 "):
                r[5][0] = "来自 " + uploaders.get(r[5][0][3:], r[5][0][3:])
    if len(info) > 17 and isinstance(info[17], list) and len(info[17]) > 1 and isinstance(info[17][1], dict):
        cloud = collections.Counter(w for r in recs if r[4] == 1 and r[5] for w in str(r[5][0]).split())
        info[17][1] = dict(cloud.most_common(len(info[17][1])))

    extra: dict[str, str] = {}  # the XML's hashes that aren't in the actor table

    def entry(m: re.Match) -> str:
        p = m.group(1).split(",")
        if p[6] not in hashes and p[6] not in extra:
            extra[p[6]] = crc(UID0 + len(uids) + len(extra))
        p[6] = hashes.get(p[6]) or extra[p[6]]
        p[7] = str(2_000_000_000_000_000_000 + len(ids))
        ids.append(p[7])
        return f'<d p="{",".join(p)}">{html.escape(text(html.unescape(m.group(2))), quote=False)}</d>'

    ids: list[str] = []
    xml = D_RE.sub(entry, xml_path.read_bytes().decode("utf-8"))
    assert len(set(hashes.values()) | set(extra.values())) == len(hashes) + len(extra), "crc32 collision"

    packed = packb(d)
    assert unpackb(packed) == d
    archive.write_bytes(packed)
    xml_path.write_bytes(xml.encode("utf-8"))
    live = parse_dmks_live(packed)
    s0, n = danmaku_origin(xml.encode("utf-8"), live.messages)
    print(f"{archive.name}: {len(actors)} actors ({len(uids)} uids), {len(recs)} records, {len(live.messages)} "
          f"messages, {len(texts)} distinct texts; {xml_path.name}: {len(ids)} entries, {len(extra)} unknown "
          f"hashes; S0 = {s0}, n = {n}")


if __name__ == "__main__":
    args = [Path(a) for a in sys.argv[1:]] or [HERE / "dmks_live_7a5add15.msgpack", HERE / "dm_42232054404.xml"]
    if len(args) != 2:
        sys.exit(__doc__)
    rewrite(*args)
