"""Find and cut the duplicated audio in a Bilibili 直播回放, with no prior knowledge of the joins.

Bilibili builds a replay from ~30-min fragments; each fragment starts by repeating the last ~5 s
of the previous one, and long replays are split into pages (each a whole number of fragments, so
a page boundary is just another fragment join). A page's audio is one continuous AAC encode, so
the repeated 5 s are encoded twice from the same PCM: after a few warm-up packets the two encodes
converge and the packets are byte-identical (with sporadic one-packet differences). So:

  1. hash every AAC packet of every page (demuxed to ADTS: one ffmpeg -c copy pass, ~1 s/hour)
  2. concatenate the pages into one global packet sequence
  3. seeds: hashes that occur exactly twice, on packets that carry sound (digital silence is a
     tiny packet repeated thousands of times; quiet packets repeat by chance too)
  4. group seeds by lag (index distance) and time, extend each group while H[i] == H[i+lag]
  5. walk the replay fragment by fragment (~1805 s each). A group that is >= 80% matching sound
     is accepted anywhere (real joins: 93-97%). Where the cadence says a join is due, less will
     do: >= 40% sound = strong, >= 3 same-lag sound packets over half the lag = weak (flagged).
     If a due window has nothing audible, the repeat is assumed to sit in digital silence there.
  6. each repeat drops exactly `lag` packets right after the last matching packet of the earlier
     copy, so the packet before the seam is byte-identical to the one the decoder would have seen
     in the later copy: the splice is bit-exact.

Validated on the KL qualifying replay (2 pages, 5 joins, all within 22 ms of hand-made cuts).
"""

from __future__ import annotations

import bisect
import collections
import hashlib
import mmap
import statistics
import subprocess
from array import array
from dataclasses import dataclass, field
from pathlib import Path

SAMPLES_PER_PACKET = 1024  # AAC-LC
ADTS_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)

SILENT_BYTES = 16          # digital silence is ~8 B/packet at any bitrate
SEED_FRAC = 0.15           # seed packets must be >= this x median packet size (quiet = small)
MIN_SEED_BYTES = 24
SURE_SOUND_FRAC = 0.8      # accepted anywhere: >= 80% of the lag is matching sound (real: 93-97%)
STRONG_SOUND_FRAC = 0.4    # on cadence: >= 40% of the lag is matching sound
MERGE_LAG_PKTS = 8         # runs whose lags differ this little are one repeat that lost a packet
WEAK_MIN_SEEDS = 3         # weak: a few same-lag sound packets, on cadence ...
WEAK_MIN_COVERAGE = 0.5    # ... spanning at least half the lag
MAX_LAG_S = 120.0          # Bilibili repeats ~5 s; up to 2 min is still looked at
CLUSTER_GAP_S = 30.0       # seeds of one lag further apart than this are separate groups
FRAGMENT_S = (1750.0, 1870.0)  # observed fragment lengths 1801.5-1810 s
DEFAULT_LAG_S = 5.0
VIDEO_GOP_S = 5.0          # 5-s GOP restarted at each fragment start; video repeats are 125 frames
KEYFRAME_TOL = 0.05


# ---- packets -------------------------------------------------------------------------------------


@dataclass
class AudioPart:
    """One page's AAC packets, as ADTS frames in `path`."""

    path: Path
    rate: int
    offsets: array  # byte offset of each frame (header included)
    lengths: array  # frame length (header included)
    header: array  # header length (7, or 9 with CRC)
    hashes: list[bytes]  # md5 of each raw AAC payload

    @property
    def n(self) -> int:
        return len(self.offsets)

    @property
    def duration(self) -> float:
        return self.n * SAMPLES_PER_PACKET / self.rate

    def size(self, i: int) -> int:
        return self.lengths[i] - self.header[i]


def demux_adts(src: Path, dest: Path) -> None:
    """Stream-copy the first audio track to ADTS (one frame per AAC packet, no re-encode)."""
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:a:0", "-c:a", "copy",
                    "-f", "adts", str(dest)], check=True, capture_output=True)


def read_adts(path: Path) -> AudioPart:
    offsets, lengths, header = array("Q"), array("I"), array("B")
    hashes: list[bytes] = []
    rate = None
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        pos, end = 0, len(m)
        while pos + 7 <= end:
            b = m[pos:pos + 7]
            if b[0] != 0xFF or (b[1] & 0xF0) != 0xF0:
                raise ValueError(f"{path}: lost ADTS sync at byte {pos}")
            hlen = 7 if b[1] & 1 else 9
            flen = ((b[3] & 0x03) << 11) | (b[4] << 3) | (b[5] >> 5)
            if (b[6] & 0x03) != 0:
                raise ValueError(f"{path}: multi-block ADTS frames are not supported")
            if rate is None:
                rate = ADTS_RATES[(b[2] >> 2) & 0x0F]
            if flen < hlen or pos + flen > end:
                break  # truncated tail
            offsets.append(pos)
            lengths.append(flen)
            header.append(hlen)
            hashes.append(hashlib.md5(m[pos + hlen:pos + flen]).digest())
            pos += flen
    if not offsets or rate is None:
        raise ValueError(f"{path}: no AAC frames")
    return AudioPart(path, rate, offsets, lengths, header, hashes)


def load_part(src: Path, adts: Path) -> AudioPart:
    if not adts.exists():
        tmp = adts.with_name(adts.name + ".tmp")
        demux_adts(src, tmp)
        tmp.replace(adts)
    return read_adts(adts)


# ---- join detection ------------------------------------------------------------------------------


@dataclass
class Join:
    cut: int  # global packet index where the later copy begins: drop [cut, cut + lag)
    lag: int
    tier: str = ""  # strong | weak | assumed-silent
    kind: str = ""  # fragment-join | part-boundary | spans-part-boundary
    s: int = 0  # matching run [s, e) (earlier copy side)
    e: int = 0
    seeds: int = 0
    coverage: float = 0.0
    sound_frac: float = 0.0
    unsure: int = 0  # packets of silence the true join could be anywhere in
    sound_hits: int = 0
    runs: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("cut", "lag", "tier", "kind", "seeds", "coverage", "unsure")}


class JoinFinder:
    def __init__(self, parts: list[AudioPart], fragment_s: tuple[float, float] | None = None):
        if len({p.rate for p in parts}) != 1:
            raise ValueError("pages have different audio sample rates")
        self.parts = parts
        self.rate = parts[0].rate
        self.pkt_s = SAMPLES_PER_PACKET / self.rate
        self.fragment_s = fragment_s or FRAGMENT_S
        self.starts: list[int] = []
        self.H: list[bytes] = []
        self.S: list[int] = []
        for p in parts:
            self.starts.append(len(self.H))
            self.H += p.hashes
            self.S += [p.size(i) for i in range(p.n)]
        self.N = len(self.H)
        sizes = [s for s in self.S if s >= SILENT_BYTES]
        self.seed_bytes = max(MIN_SEED_BYTES, int(SEED_FRAC * statistics.median(sizes))) if sizes else MIN_SEED_BYTES

    def t(self, g: int) -> float:
        return g * self.pkt_s

    def where(self, g: int) -> dict:
        k = max(0, bisect.bisect_right(self.starts, g) - 1)
        i = g - self.starts[k]
        return {"part": k + 1, "pkt": i, "t": round(i * self.pkt_s, 3)}

    def run_at(self, lag: int, s: int, e: int) -> tuple[int, int, int]:
        """Extend [s, e) while H[i] == H[i+lag]; count mismatches inside."""
        H = self.H
        while s > 0 and H[s - 1] == H[s - 1 + lag]:
            s -= 1
        while e + lag < self.N and H[e] == H[e + lag]:
            e += 1
        return s, e, sum(1 for i in range(s, e) if H[i] != H[i + lag])

    def candidates(self) -> list[Join]:
        H, S = self.H, self.S
        count = collections.Counter(H)
        max_lag = int(MAX_LAG_S / self.pkt_s)
        first: dict[bytes, int] = {}
        seeds: dict[int, list[int]] = collections.defaultdict(list)  # lag -> earlier indices
        for g in range(self.N):
            if S[g] < self.seed_bytes or count[H[g]] != 2:
                continue
            a = first.setdefault(H[g], g)
            if a != g and 0 < g - a <= max_lag:
                seeds[g - a].append(a)
        gap = int(CLUSTER_GAP_S / self.pkt_s)
        runs = []
        for lag, idx in seeds.items():
            idx.sort()
            groups, cur = [], [idx[0]]
            for a in idx[1:]:
                if a - cur[-1] > gap:
                    groups.append(cur)
                    cur = []
                cur.append(a)
            groups.append(cur)
            for grp in groups:
                s, e, _ = self.run_at(lag, grp[0], grp[-1] + 1)
                runs.append(dict(lag=lag, s=s, e=e, seeds=len(grp)))
        return [self.describe(o) for o in self._merge(runs)]

    def _merge(self, runs: list[dict]) -> list[dict]:
        """One repeat can show up as several runs: a packet lost in one copy changes the lag by one
        from there on. Merge runs that touch and have nearly the same lag; the run that reaches the
        join (the latest one) decides lag and cut: dropping it removes the whole later copy."""
        runs.sort(key=lambda o: o["s"])
        merged: list[dict] = []
        for o in runs:
            m = merged[-1] if merged else None
            if m and abs(o["lag"] - m["lag"]) <= MERGE_LAG_PKTS and o["s"] <= m["e"] + MERGE_LAG_PKTS + 1:
                parts = m.setdefault("runs", [dict(lag=m["lag"], s=m["s"], e=m["e"], seeds=m["seeds"])])
                parts.append(dict(o))
                tail = max(parts, key=lambda r: r["e"])
                m.update(lag=tail["lag"], e=tail["e"], seeds=m["seeds"] + o["seeds"])
            else:
                merged.append(dict(o))
        return merged

    def describe(self, o: dict) -> Join:
        s, e, lag = o["s"], o["e"], o["lag"]
        j = Join(cut=e, lag=lag, s=s, e=e, seeds=o["seeds"], runs=o.get("runs") or [],
                 coverage=round(min(1.0, (e - s) / lag), 3), sound_frac=round(o["seeds"] / lag, 3))
        # Where the later copy begins. The earlier copy's tail almost always matches (the encodes
        # have converged) while the later copy's head differs for a few warm-up packets, so
        # normally the join is at e. A run longer than the lag means digital silence let it spill
        # past one end: a matched packet that carries sound is inside the true repeat, so trust
        # that end; if both ends are silent the join is somewhere in [s+lag, e] (all silence: the
        # audio is the same wherever we cut).
        if e <= s + lag or self.S[e - 1] >= SILENT_BYTES:
            j.cut = e
        elif self.S[s] >= SILENT_BYTES:
            j.cut = s + lag
        else:
            j.cut, j.unsure = (s + lag + e) // 2, e - (s + lag)
        return j

    def select(self, cands: list[Join]) -> tuple[list[Join], list[str]]:
        """Walk the replay fragment by fragment. Unmistakable repeats are accepted anywhere;
        weaker ones only where the fragment cadence says a join is due; if nothing is there
        either, the join is assumed to sit in a silent stretch of that window."""
        warn: list[str] = []
        sure = [c for c in cands if c.sound_frac >= SURE_SOUND_FRAC and c.coverage >= 0.8]
        # On cadence a few same-lag sound packets are enough (random pairs never share a lag); the
        # run may be short: two independent encodes take up to ~60 packets to converge.
        ok = [c for c in cands if c.seeds >= WEAK_MIN_SEEDS and c.coverage >= WEAK_MIN_COVERAGE]
        lags = [c.lag for c in sure] or [round(DEFAULT_LAG_S / self.pkt_s)]
        lo_f, hi_f = (int(x / self.pkt_s) for x in self.fragment_s)
        nominal = (lo_f + hi_f) // 2
        chosen: list[Join] = []
        frag_start = 0
        while True:
            window = (frag_start + lo_f, frag_start + hi_f)
            early = [c for c in sure if frag_start + lo_f // 4 < c.cut < window[0] and c not in chosen]
            if early:
                c = min(early, key=lambda c: c.cut)
                c.tier = "strong"
                chosen.append(c)
                warn.append(f"join at {self.where(c.cut)} only {self.t(c.cut - frag_start):.0f}s after the previous one")
                frag_start = c.cut
                continue
            if window[0] >= self.N:
                break
            pool = [c for c in ok if window[0] <= c.cut <= window[1]]
            if pool:
                c = max(pool, key=lambda c: (c.seeds, c.coverage))
                c.tier = "strong" if c.sound_frac >= STRONG_SOUND_FRAC and c.coverage >= 0.8 else "weak"
                if c.tier == "weak":
                    warn.append(f"weak join (only {c.seeds} sound packets match) at {self.where(c.cut)}")
                chosen.append(c)
                frag_start = c.cut
                continue
            if window[1] >= self.N:
                break  # the replay ends before this fragment would
            fb = self._silent_join(window, lags)
            if fb:
                chosen.append(fb)
                warn.append(f"no audible repeat near {self.t(window[0]):.0f}-{self.t(window[1]):.0f}s; assumed one "
                            f"inside the silence at {self.where(fb.cut)}")
                frag_start = fb.cut
                continue
            warn.append(f"no repeat where a fragment join is due ({self.t(window[0]):.0f}-{self.t(window[1]):.0f}s);"
                        f" everything after it may be ~{self.t(lags[0]):.0f}s late")
            frag_start += nominal
        for c in ok:  # off-cadence candidates that were not unmistakable: report, don't cut
            if c not in chosen and not any(c.s < k.e + k.lag and k.s < c.e + c.lag for k in chosen):
                warn.append(f"ignored an off-cadence repeat of {c.seeds} sound packets at lag {c.lag} at {self.where(c.s)}")
        chosen.sort(key=lambda c: c.cut)
        for c in chosen:
            near = any(abs(c.cut - st) <= 16 + c.unsure // 2 for st in self.starts[1:])
            c.kind = "part-boundary" if near else "fragment-join"
            if not near and any(c.cut < st < c.cut + c.lag for st in self.starts[1:]):
                c.kind = "spans-part-boundary"
        for k, st in enumerate(self.starts[1:], 1):
            if not any(c.kind == "part-boundary" and abs(c.cut - st) <= 16 + c.unsure // 2 for c in chosen):
                warn.append(f"no repeat where page {k + 1} begins: pages simply concatenated")
        return chosen, warn

    def _silent_join(self, window: tuple[int, int], lags: list[int]) -> Join | None:
        """A repeat (almost) entirely in silence: try the lags seen elsewhere and take the longest
        stretch of lag-periodic packets (<= 3 differing in a row, <= 5% overall)."""
        best = None
        for lag in sorted(set(lags), key=lambda x: -lags.count(x)):
            o = self._quiet_stretch(window, lag)
            if o and (best is None or (o.sound_hits, o.e - o.s) > (best.sound_hits, best.e - best.s)):
                best = o
        return best

    def _quiet_stretch(self, window: tuple[int, int], lag: int) -> Join | None:
        H, S = self.H, self.S
        lo, hi = max(0, window[0] - lag), min(self.N - lag, window[1] + lag)
        best, i = None, lo
        while i < hi:
            if H[i] != H[i + lag]:
                i += 1
                continue
            j, miss, streak, hits = i, 0, 0, 0
            while j < hi:
                if H[j] == H[j + lag]:
                    streak = 0
                    hits += S[j] >= SILENT_BYTES
                elif streak < 3:
                    streak += 1
                    miss += 1
                else:
                    break
                j += 1
            j -= streak
            if j - i >= lag and miss <= 0.05 * (j - i) and (best is None or j - i > best[1] - best[0]):
                best = (i, j, hits)
            i = j + 1
        if not best:
            return None
        s0, e0, hits = best
        o = self.describe(dict(lag=lag, s=s0, e=e0, seeds=0))
        c = o.cut
        for d in range(lag):  # cut where the packet before the seam matches its twin (bit-exact)
            hit = next((cc for cc in (c - d, c + d) if s0 < cc <= e0 and H[cc - 1] == H[cc - 1 + lag]), None)
            if hit is not None:
                o.cut = hit
                break
        o.tier, o.sound_hits = "assumed-silent", hits
        return o


def find_joins(parts: list[AudioPart]) -> tuple[list[Join], list[str]]:
    jf = JoinFinder(parts)
    return jf.select(jf.candidates())


# ---- the continuous timeline -----------------------------------------------------------------------


@dataclass
class Range:
    part: int  # 0-based page index
    i0: int  # packet range [i0, i1) within that page
    i1: int


class Timeline:
    """All pages' raw packets -> one continuous content timeline (repeats removed)."""

    def __init__(self, parts: list[AudioPart], joins: list[Join]):
        self.parts = parts
        self.rate = parts[0].rate
        self.pkt_s = SAMPLES_PER_PACKET / self.rate
        self.starts = [0]
        for p in parts:
            self.starts.append(self.starts[-1] + p.n)
        self.N = self.starts[-1]
        self.joins = sorted(joins, key=lambda j: j.cut)

    def part_of(self, g: int) -> int:
        return min(len(self.parts) - 1, bisect.bisect_right(self.starts, g) - 1)

    @property
    def fragments(self) -> int:
        return len(self.joins) + 1

    def twin(self, g: float) -> float:
        """Inside a dropped later copy: the same content's position in the earlier copy."""
        for j in self.joins:
            if j.cut <= g < j.cut + j.lag:
                return g - j.lag
        return g

    def fragment(self, g: float) -> int:
        """Which fragment (0-based, counting every join) the content at g belongs to."""
        g = self.twin(g)
        return sum(1 for j in self.joins if j.cut + j.lag <= g)

    def content_packets(self, g: float) -> float:
        """Content position (in packets) of global packet position g (fractional ok), with every
        repeat removed and nothing else: the 'no-gap' content timeline."""
        g = self.twin(g)
        return g - sum(j.lag for j in self.joins if j.cut + j.lag <= g)

    def content(self, g: float) -> float:
        return self.content_packets(g) * self.pkt_s

    def global_pos(self, part: int, seconds: float) -> float:
        return self.starts[part] + seconds / self.pkt_s

    def ambiguous(self, g0: float, g1: float) -> bool:
        """[g0, g1) touches a repeated stretch, so it matches either copy: position unreliable."""
        return any(g0 < j.cut + j.lag and j.cut - j.lag < g1 for j in self.joins)

    def applicable(self, first: int, last: int) -> list[Join]:
        """Joins whose both copies lie inside pages first..last. When the output starts at a later
        page, that page's leading repeat has no earlier copy in the output, so it stays."""
        lo, hi = self.starts[first], self.starts[last + 1]
        return [j for j in self.joins if j.cut - j.lag >= lo and j.cut < hi]

    def keep(self, first: int = 0, last: int | None = None) -> list[Range]:
        last = len(self.parts) - 1 if last is None else last
        lo, hi = self.starts[first], self.starts[last + 1]
        spans, pos = [], lo
        for j in self.applicable(first, last):
            if j.cut > pos:
                spans.append((pos, j.cut))
            pos = max(pos, min(hi, j.cut + j.lag))
        if pos < hi:
            spans.append((pos, hi))
        out = []
        for a, b in spans:  # split at page boundaries
            for k in range(first, last + 1):
                x, y = max(a, self.starts[k]), min(b, self.starts[k + 1])
                if x < y:
                    out.append(Range(k, x - self.starts[k], y - self.starts[k]))
        return out

    def keep_seconds(self, keep: list[Range]) -> float:
        return sum(r.i1 - r.i0 for r in keep) * self.pkt_s


# ---- video ---------------------------------------------------------------------------------------


@dataclass
class VideoRange:
    part: int
    start: float  # keyframe pts (s) in that page's video
    end: float
    frames: int


def video_packets(path: Path) -> list[tuple[float, bool]]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        f = line.split(",")
        if len(f) >= 2 and f[0] not in ("", "N/A"):
            rows.append((float(f[0]), "K" in f[1]))
    rows.sort()
    return rows


def video_plan(tl: Timeline, packets: dict[int, list[tuple[float, bool]]], first: int, last: int,
               frame_s: float = 0.04) -> tuple[list[VideoRange], list[str]]:
    """Keyframe-aligned video ranges for a stream copy (no re-encode).

    Video drops its own duplicate (exactly 5.000 s, whole GOPs) at the same content moment as
    the audio cut (within a frame), so A/V stay aligned to ~20 ms. At a page boundary the later
    page is kept whole and the earlier page's repeated tail dropped. Returns (ranges, problems);
    any problem means keyframes don't sit where they must and the video needs a re-encode.
    """
    keys = {k: [t for t, key in packets[k] if key] for k in range(first, last + 1)}
    ends = {k: max(t for t, _ in packets[k]) + frame_s for k in range(first, last + 1)}
    problems: list[str] = []

    def near(k: int, t: float) -> float:
        return min(keys[k], key=lambda x: abs(x - t)) if keys[k] else 0.0

    segs: list[tuple[int, float, float]] = []
    cur = (first, 0.0)
    pending = list(range(first + 1, last + 1))  # page starts not yet switched to

    def switch_before(k: int) -> None:
        """Pages joined without a repeat (none found): play the earlier one to its end."""
        nonlocal cur
        while pending and pending[0] < k:
            segs.append((cur[0], cur[1], ends[cur[0]]))
            cur = (pending.pop(0), 0.0)

    for j in tl.applicable(first, last):
        b = next((k for k in range(first + 1, last + 1) if j.cut <= tl.starts[k] <= j.cut + j.lag), None)
        if b is not None:  # page boundary: keep the later page whole, drop the earlier page's tail
            switch_before(b)
            want = (tl.starts[b] - j.lag - tl.starts[b - 1]) * tl.pkt_s
            kcut = near(b - 1, want)
            if abs(kcut - want) > KEYFRAME_TOL:
                problems.append(f"no keyframe where page {b}'s repeated tail starts ({want:.3f}s)")
            segs.append((b - 1, cur[1], kcut))
            cur = (b, 0.0)
            pending.remove(b)
        else:
            k = tl.part_of(j.cut)
            switch_before(k + 1)
            tj = (j.cut - tl.starts[k]) * tl.pkt_s
            kcut = near(k, tj)
            kres = near(k, kcut + VIDEO_GOP_S)
            if abs(kcut - tj) > KEYFRAME_TOL or abs(kres - kcut - VIDEO_GOP_S) > KEYFRAME_TOL:
                problems.append(f"keyframes {kcut:.3f}/{kres:.3f} don't bracket the repeat at page {k + 1} {tj:.3f}s")
            segs.append((k, cur[1], kcut))
            cur = (k, kres)
    switch_before(last + 1)
    segs.append((cur[0], cur[1], ends[cur[0]]))
    out = []
    for k, a, b in segs:
        n = sum(1 for t, _ in packets[k] if a - 1e-3 <= t < b - 1e-3)
        if n:
            out.append(VideoRange(k, round(a, 6), round(b, 6), n))
    return out, problems
