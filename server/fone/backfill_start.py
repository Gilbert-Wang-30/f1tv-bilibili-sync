"""When was a downloaded replay's first sample recorded? Three independent answers, best first.

  align      cross-correlate the replay against any partial recording of the same stream the
             recorder did catch (fone already maps those to wall time): a few ms per fragment
             measured, and it lands directly in the recorder's frame. Coarse: 10 ms log-band-
             energy features, FFT cross-correlation of each 30 s recording window against every
             page; fine: 8 kHz waveform cross-correlation around the coarse peak, parabolic
             interpolation. Fragments no window fell into are interpolated (see Placement).
  live_time  Bilibili's 开播 second, logged by our own poll (or danmakus.com's startDate):
             live_time - t0 in [-0.05, +0.84] s (an integer truncation of t0 + ~0.84).
  S0         the origin Bilibili's danmaku importer used for the replay's chunk 0: send time of the
             live danmaku re-posted into the replay minus its progress (matched by crc32(uid) +
             text): S0 - t0 in [5.32, 6.35] s. Computed by Bilibili for *this* replay, so it
             survives merged/restarted lives where a live_time belongs to another broadcast.

t0 is the capture time on the streamer's PC (KL ground truth frame). A live recorder sees each
sample ARRIVAL_LATENCY later, and fone's wall map of recordings is in that frame, so live_time /
S0 estimates are shifted by it to make imports interchangeable with recordings.
"""

from __future__ import annotations

import collections
import html
import re
import statistics
import struct
import subprocess
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .backfill_dedup import Timeline

LT_IV = (-0.05, 0.84)  # live_time - t0, measured on 9 sessions
S0_IV = (5.32, 6.35)  # S0 - t0
LIVE_EVENT_LAG = (21.0, 7.0)  # danmakus LIVE events arrive 15-27 s after t0 (2 restarts measured)
# KL qualifying: fone's live map put recording media 0 at 1791010992.737; the same audio was
# captured at 1791010990.65 (ground truth) -> the recorder frame runs 2.09 s behind capture.
ARRIVAL_LATENCY = 2.09
JOIN_GAP_S = 0.030  # real audio lost at each fragment join (see Placement)
JOIN_TOL = 0.02  # how far one join's loss strays from that (observed <= 13 ms)

SR = 8000
HOP = 80  # 10 ms feature frames
NFFT = 256
EDGES = np.geomspace(150, 3800, 9)  # 8 log-spaced bands
WIN_S = 30.0
MIN_NCC, MIN_RATIO = 0.2, 1.5
AGREE_S = 0.05


@dataclass
class Placement:
    """Where a replay's content sits on the recorder-frame wall clock, fragment by fragment.

    The replay loses ~30 ms of real audio at every fragment join even after the repeat is cut
    bit-exactly (KL, against the live recording: +29.0, +29.4, +43.0, +21.5, +30.0 ms; no drift
    inside a fragment). So content in fragment f maps as wall = offsets[f] + no-gap content
    seconds (`Timeline.content`), and the writer turns the steps into timestamp gaps.
    """

    offsets: list[float]
    pm: float  # +- seconds
    method: str
    notes: list[str] = field(default_factory=list)
    measured: list[int] = field(default_factory=list)  # fragments measured directly (align)

    @property
    def t0(self) -> float:
        return self.offsets[0]

    def wall(self, tl: Timeline, g: float) -> float:
        return self.offsets[min(tl.fragment(g), len(self.offsets) - 1)] + tl.content(g)

    def to_dict(self) -> dict:
        return {"t0": round(self.t0, 3), "pm": round(self.pm, 3), "method": self.method, "notes": self.notes,
                "offsets": [round(x, 3) for x in self.offsets], "measured": self.measured}


def from_t0(t0: float, fragments: int, pm: float, method: str, notes: list[str] | None = None) -> Placement:
    return Placement([t0 + JOIN_GAP_S * f for f in range(fragments)], pm + JOIN_TOL * (fragments - 1) / 2,
                     method, notes or [])


def estimate_start(fragments: int, aligned: Placement | None = None, live_time: float | None = None,
                   s0: float | None = None, live_events: list[float] | None = None,
                   arrival: float = ARRIVAL_LATENCY) -> Placement | None:
    """The cascade: alignment against a recording if we have one, else live_time / S0 (both when
    available, S0 winning a disagreement), else the coarse danmakus LIVE event."""
    notes: list[str] = []
    capture: tuple[float, float, str] | None = None
    iv = []
    if live_time is not None:
        iv.append(("live_time", live_time - LT_IV[1], live_time - LT_IV[0]))
    if s0 is not None:
        iv.append(("S0", s0 - S0_IV[1], s0 - S0_IV[0]))
    if iv:
        lo, hi = max(x[1] for x in iv), min(x[2] for x in iv)
        method = "+".join(x[0] for x in iv)
        if lo > hi:  # disagree: S0 belongs to this replay, live_time may not
            notes.append(f"live_time is {lo - hi:.1f}s inconsistent with S0; using S0")
            lo, hi, method = s0 - S0_IV[1], s0 - S0_IV[0], "S0"  # type: ignore[operator]
        capture = ((lo + hi) / 2, (hi - lo) / 2, method)
    elif live_events:
        capture = (live_events[0] - LIVE_EVENT_LAG[0], LIVE_EVENT_LAG[1], "live-event")
    if aligned is not None:
        if capture and abs(aligned.t0 - arrival - capture[0]) > capture[1] + 1.0:
            aligned.notes.append(f"alignment differs from {capture[2]} by {aligned.t0 - arrival - capture[0]:+.2f}s")
        return aligned
    if capture:
        return from_t0(capture[0] + arrival, fragments, capture[1], capture[2], notes)
    return None


# ---- alignment against a partial recording ---------------------------------------------------------


def decode_8k(inputs: list[Path], dest: Path | None = None) -> np.ndarray:
    """Decode to 8 kHz mono float32. Several inputs are byte-concatenated (HLS .ts segments)."""
    src = str(inputs[0]) if len(inputs) == 1 else "concat:" + "|".join(str(p) for p in inputs)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", src, "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "f32le"]
    if dest is None:
        out = subprocess.run(cmd + ["-"], check=True, capture_output=True).stdout
        return np.frombuffer(out, dtype="<f4")
    subprocess.run(cmd + ["-y", str(dest)], check=True, capture_output=True)
    return np.memmap(dest, dtype="<f4", mode="r")


def features(x: np.ndarray) -> np.ndarray:
    """(n_frames, 8) log band energies, high-passed in time (minus 1 s moving average), z-scored."""
    n = 1 + (len(x) - NFFT) // HOP
    if n <= 0:
        return np.zeros((0, len(EDGES) - 1), np.float32)
    win = np.hanning(NFFT).astype(np.float32)
    freqs = np.fft.rfftfreq(NFFT, 1 / SR)
    bands = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(EDGES[:-1], EDGES[1:])]
    out = np.empty((n, len(bands)), np.float32)
    step = 100_000
    for s in range(0, n, step):
        e = min(n, s + step)
        idx = np.arange(s, e)[:, None] * HOP + np.arange(NFFT)[None, :]
        spec = np.abs(np.fft.rfft(np.asarray(x[idx], np.float32) * win, axis=1)) ** 2
        for b, m in enumerate(bands):
            out[s:e, b] = np.log(spec[:, m].sum(axis=1) + 1e-7)
    k = 100
    c = np.cumsum(np.vstack([np.zeros((1, out.shape[1]), np.float32), out]), axis=0)
    lo = np.clip(np.arange(n) - k // 2, 0, n)
    hi = np.clip(np.arange(n) + k // 2, 0, n)
    out -= (c[hi] - c[lo]) / np.maximum(hi - lo, 1)[:, None]
    out /= out.std(axis=0, keepdims=True) + 1e-6
    return out


class AlignIndex:
    """Features of every page of a replay, ready for FFT cross-correlation."""

    def __init__(self, pages: list[np.ndarray]):
        self.pages = []
        for x in pages:
            f = features(x)
            size = 1 << int(np.ceil(np.log2(len(f) + int(WIN_S * 100) + 1000)))
            e = np.concatenate([[0], np.cumsum((f.astype(np.float64) ** 2).sum(axis=1))])
            self.pages.append(dict(x=x, f=f, L=size, F=np.fft.rfft(f, n=size, axis=0), e=e))

    def coarse(self, qf: np.ndarray) -> tuple[int, int, float, float]:
        """Best (page, frame offset, ncc) and the best rival (> 1 s away, or on another page)."""
        m = len(qf)
        qn = np.sqrt((qf.astype(np.float64) ** 2).sum())
        cands = []
        for pi, p in enumerate(self.pages):
            if m > len(p["f"]):
                continue
            Q = np.fft.rfft(qf[::-1], n=p["L"], axis=0)
            corr = np.fft.irfft((p["F"] * Q).sum(axis=1), n=p["L"])[m - 1: len(p["f"])]
            loc = p["e"][m:] - p["e"][:-m]
            ncc = corr / (qn * np.sqrt(np.maximum(loc, 1e-9)) + 1e-12)
            k = int(np.argmax(ncc))
            cands.append((float(ncc[k]), pi, k, ncc))
        if not cands:
            return -1, 0, 0.0, 1.0
        cands.sort(key=lambda c: -c[0])
        best, pi, k, ncc = cands[0]
        mask = np.ones(len(ncc), bool)
        mask[max(0, k - 100): k + 100] = False
        second = float(ncc[mask].max()) if mask.any() else -1.0
        for c in cands[1:]:
            second = max(second, c[0])
        return pi, k, best, second

    def fine(self, pi: int, k: int, q: np.ndarray, search: int = 480) -> tuple[float, float]:
        """Waveform xcorr around coarse frame k: (sample position of q[0] in the page, peak corr)."""
        x = self.pages[pi]["x"]
        lo = max(0, k * HOP - search)
        hi = min(len(x), k * HOP + len(q) + search)
        r = np.asarray(x[lo:hi], np.float64)
        n = 1 << int(np.ceil(np.log2(len(r) + len(q))))
        c = np.fft.irfft(np.fft.rfft(r, n) * np.conj(np.fft.rfft(q, n)), n)[: len(r) - len(q) + 1]
        j = int(np.argmax(c))
        frac = 0.0
        if 0 < j < len(c) - 1:
            a, b, d = c[j - 1], c[j], c[j + 1]
            den = a - 2 * b + d
            frac = float(0.5 * (a - d) / den) if den != 0 else 0.0
        norm = np.sqrt((q.astype(np.float64) ** 2).sum() * (r[j:j + len(q)] ** 2).sum()) + 1e-12
        return lo + j + frac, float(c[j] / norm)


def align(index: AlignIndex, tl: Timeline, windows: list[tuple[np.ndarray, float]]) -> tuple[
        Placement | None, list[dict]]:
    """windows: (8 kHz samples, recorder-frame wall time of sample 0) from a partial recording.
    Each window that matches unambiguously measures its fragment's offset; fragments without a
    window are interpolated (or extrapolated at JOIN_GAP_S per join)."""
    rows = []
    for q, wall0 in windows:
        q = np.asarray(q, np.float32)
        if len(q) < SR * 5 or q.std() < 1e-4:
            continue
        pi, k, ncc, second = index.coarse(features(q))
        if pi < 0:
            continue
        pos_samples, fine = index.fine(pi, k, q)
        g = tl.global_pos(pi, pos_samples / SR)
        c = tl.content(g)
        ok = ncc >= MIN_NCC and ncc / max(second, 1e-3) >= MIN_RATIO
        # A window that touches a repeated stretch matches either copy: ambiguous by the repeat.
        if tl.ambiguous(g, g + len(q) / SR / tl.pkt_s):
            ok = False
        rows.append(dict(wall=round(wall0, 3), page=pi + 1, pos=round(pos_samples / SR, 4), frag=tl.fragment(g),
                         content=round(c, 4), start=wall0 - c, ncc=round(ncc, 3), second=round(second, 3),
                         fine=round(fine, 3), ok=ok, agree=False))
    good = [r for r in rows if r["ok"]]
    if not good:
        return None, rows
    # Reference: the fragment with the most matches; others must agree with it up to the joins between.
    by_frag = collections.Counter(r["frag"] for r in good)
    f_ref = min(by_frag, key=lambda f: (-by_frag[f], f))
    s_ref = float(np.median([r["start"] for r in good if r["frag"] == f_ref]))
    for r in good:
        n = abs(r["frag"] - f_ref)
        r["agree"] = abs(r["start"] - (s_ref + JOIN_GAP_S * (r["frag"] - f_ref))) <= AGREE_S + JOIN_TOL * n
    agree = [r for r in good if r["agree"]]
    if len(agree) < min(2, len(good)) or len(agree) * 2 < len(good):
        return None, rows
    measured = {f: float(np.median([r["start"] for r in agree if r["frag"] == f])) for f in {r["frag"] for r in agree}}
    spread = float(np.median([abs(r["start"] - measured[r["frag"]]) for r in agree]))
    known = sorted(measured)
    offsets, far = [], 0
    for f in range(tl.fragments):
        lo = max((x for x in known if x <= f), default=None)
        hi = min((x for x in known if x >= f), default=None)
        if lo is not None and hi is not None and lo != hi:
            offsets.append(measured[lo] + (measured[hi] - measured[lo]) * (f - lo) / (hi - lo))
        elif lo is not None or hi is not None:
            ref = lo if lo is not None else hi
            offsets.append(measured[ref] + JOIN_GAP_S * (f - ref))
        far = max(far, min(abs(f - x) for x in known))
    place = Placement(offsets, max(spread, 0.02) + JOIN_TOL * far, "align", measured=known)
    return place, rows


# ---- S0 from the replay's re-posted live danmaku ---------------------------------------------------


def unpackb(b: bytes):
    """Minimal msgpack decoder (what danmakus.com's archive uses); ext types decode to None."""
    def rd(i: int):
        t = b[i]
        i += 1
        if t <= 0x7F:
            return t, i
        if t >= 0xE0:
            return t - 0x100, i
        if 0x80 <= t <= 0x8F:
            return rmap(i, t & 0x0F)
        if 0x90 <= t <= 0x9F:
            return rarr(i, t & 0x0F)
        if 0xA0 <= t <= 0xBF:
            n = t & 0x1F
            return b[i:i + n].decode("utf-8", "replace"), i + n
        if t == 0xC0:
            return None, i
        if t in (0xC2, 0xC3):
            return t == 0xC3, i
        if t in (0xC4, 0xC5, 0xC6, 0xD9, 0xDA, 0xDB):  # bin / str 8,16,32
            w = {0xC4: 1, 0xC5: 2, 0xC6: 4, 0xD9: 1, 0xDA: 2, 0xDB: 4}[t]
            n = int.from_bytes(b[i:i + w], "big")
            i += w
            v = b[i:i + n]
            return (v.decode("utf-8", "replace") if t >= 0xD9 else bytes(v)), i + n
        if t in (0xC7, 0xC8, 0xC9):  # ext 8,16,32
            w = {0xC7: 1, 0xC8: 2, 0xC9: 4}[t]
            n = int.from_bytes(b[i:i + w], "big")
            return None, i + w + 1 + n
        if t in (0xD4, 0xD5, 0xD6, 0xD7, 0xD8):  # fixext
            return None, i + 1 + (1 << (t - 0xD4))
        if t == 0xCA:
            return struct.unpack(">f", b[i:i + 4])[0], i + 4
        if t == 0xCB:
            return struct.unpack(">d", b[i:i + 8])[0], i + 8
        if 0xCC <= t <= 0xCF:
            w = 1 << (t - 0xCC)
            return int.from_bytes(b[i:i + w], "big"), i + w
        if 0xD0 <= t <= 0xD3:
            w = 1 << (t - 0xD0)
            return int.from_bytes(b[i:i + w], "big", signed=True), i + w
        if t in (0xDC, 0xDD):
            w = 2 if t == 0xDC else 4
            return rarr(i + w, int.from_bytes(b[i:i + w], "big"))
        if t in (0xDE, 0xDF):
            w = 2 if t == 0xDE else 4
            return rmap(i + w, int.from_bytes(b[i:i + w], "big"))
        raise ValueError(f"msgpack: bad type byte 0x{t:02x} at {i - 1}")

    def rarr(i: int, n: int):
        out = []
        for _ in range(n):
            v, i = rd(i)
            out.append(v)
        return out, i

    def rmap(i: int, n: int):
        out = {}
        for _ in range(n):
            k, i = rd(i)
            v, i = rd(i)
            out[k if isinstance(k, (str, int, float, bool, type(None))) else str(k)] = v
        return out, i

    return rd(0)[0]


@dataclass
class LiveDanmaku:
    start: float | None  # startDate (= live_time) of the archived live
    messages: list[tuple[str, str, float]]  # (crc32(uid) hex, text, send time)
    live_events: list[float]  # LIVE (开播) notices the archive saw


def parse_dmks_live(raw: bytes) -> LiveDanmaku:
    """danmakus.com /v3/lives/<id>/full (msgpack): [channel, live info, [base ms, actors, _, records]];
    each record is [delta ms, kind, actor (1-based), _, type, [text, ...]]."""
    d = unpackb(raw)
    info, (base, actors, _, recs) = d[1], d[2]
    start = info[3] / 1000 if len(info) > 3 and isinstance(info[3], (int, float)) else None
    msgs, events, s = [], [], base
    for r in recs:
        s += r[0] or 0
        if r[1] == 8:
            events.append(s / 1000)
        if len(r) > 5 and r[4] == 1 and r[2] and r[2] > 0 and r[5]:
            uid = actors[r[2] - 1][0]
            msgs.append((format(zlib.crc32(str(uid).encode()), "x"), str(r[5][0]).strip(), s / 1000))
    return LiveDanmaku(start, msgs, events)


def danmaku_origin(xml: bytes, live: list[tuple[str, str, float]]) -> tuple[float | None, int]:
    """S0 = send time - progress of the live danmaku re-posted into chunk 0 (first 1800 s) of the
    replay. Importer offsets are ms-truncated copies of one integer: take the densest 0.1 s bin."""
    idx: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for crc, text, t in live:
        idx[(crc, text)].append(t)
    offs = []
    for m in re.finditer(r'<d p="([^"]+)">(.*?)</d>', xml.decode("utf-8", "replace"), re.S):
        p = m.group(1).split(",")
        if len(p) < 7:
            continue
        t = float(p[0])
        if t >= 1800:  # chunk 0 only; later chunks use S0 + k
            continue
        for send in idx.get((p[6], html.unescape(m.group(2)).strip()), []):
            offs.append(send - t)
    if len(offs) < 10:
        return None, len(offs)
    mode = collections.Counter(round(o, 1) for o in offs).most_common(1)[0][0]
    near = [o for o in offs if abs(o - mode) <= 0.05]
    return float(round(statistics.median(near))), len(near)
