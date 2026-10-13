"""Find known pre-recorded clips (the streamer's sponsor reel) in commentary audio.

Landmark hashing (Shazam / audfprint style), numpy only so it runs in the alpine image:

  8 kHz mono -> |STFT| (512-pt, 16 ms hop) -> log -> whiten (minus 31x31 box mean)
  -> local maxima above +1 nat -> pairs (anchor, target) within 0.64 s / +-63 bins
  -> 21-bit hash (f1, df, dt) + anchor frame.

Every stream hash equal to a reference hash votes for an alignment offset (stream frame
minus reference frame). A real play gives hundreds of votes at one offset (+-1 frame);
chance collisions give a handful (never more than 22 in 17 h of this streamer's talk,
music and race audio), so 40 votes recognise a clip. Which reference seconds got votes
at that offset gives the start and end of partial plays.

The detector is streaming: feed audio as it arrives (any chunk size) and it reports a
play as soon as it is recognised, then settles where it ended. Finished recordings go
through the same path, in bounded memory.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

SR = 8000
NFFT = 512
HOP = 128
FPS = SR / HOP  # 62.5 frames/s (16 ms)
FMIN, FMAX = 3, 250  # STFT bins kept: ~47 Hz .. 3.9 kHz
ENV_T, ENV_F = 31, 31  # whitening box (frames, bins)
DF_MAX = 63


@dataclass(frozen=True)
class Params:
    max_t: int = 13  # local-max neighbourhood, frames
    max_f: int = 17  # local-max neighbourhood, bins
    thr: float = 1.0  # a peak must stand this far (nat) above its local mean
    fanout: int = 10  # pairs per anchor peak
    dt_max: int = 40  # pair window, frames (0.64 s)
    min_votes: int = 40  # votes (+-1 frame) that recognise a play
    min_seconds: int = 1  # ... spread over this many reference seconds
    sec_votes: int = 3  # votes that make a reference second "present"

    @property
    def ctx(self) -> int:
        """Frames of right context a peak needs before it is final."""
        return max(ENV_T, self.max_t) // 2 + 1


# Dense peaks (~40/s) keep recall under talk-over down to -18 dB; evaluated on 17 h of
# this streamer (36/36 plays, 0 false positives, edges within 20 ms).
DEFAULT = Params()

_WIN = np.hanning(NFFT).astype(np.float32)


# ---- features ----------------------------------------------------------------


def frames_of(n_samples: int) -> int:
    return 0 if n_samples < NFFT else 1 + (n_samples - NFFT) // HOP


def logspec(x: np.ndarray) -> np.ndarray:
    n = frames_of(len(x))
    out = np.empty((n, FMAX - FMIN), np.float32)
    for s in range(0, n, 4096):  # bounded temporaries
        m = min(4096, n - s)
        idx = np.arange(NFFT)[None, :] + HOP * (s + np.arange(m))[:, None]
        mag = np.abs(np.fft.rfft(x[idx] * _WIN, axis=1))[:, FMIN:FMAX]
        out[s:s + m] = np.log(mag + 1e-4)
    return out


def _box_mean(a: np.ndarray, kt: int, kf: int) -> np.ndarray:
    pt, pf = kt // 2, kf // 2
    p = np.pad(a, ((pt, pt), (pf, pf)), mode="edge").astype(np.float64)  # float32 cumsums drift
    c = np.zeros((p.shape[0] + 1, p.shape[1] + 1))
    c[1:, 1:] = p.cumsum(0).cumsum(1)
    t, f = a.shape
    s = c[kt:kt + t, kf:kf + f] - c[:t, kf:kf + f] - c[kt:kt + t, :f] + c[:t, :f]
    return (s / (kt * kf)).astype(np.float32)


def _box_max(a: np.ndarray, kt: int, kf: int) -> np.ndarray:
    p = np.pad(a, ((kt // 2, kt // 2), (0, 0)), constant_values=-np.inf)
    m = sliding_window_view(p, kt, axis=0).max(-1)
    p = np.pad(m, ((0, 0), (kf // 2, kf // 2)), constant_values=-np.inf)
    return sliding_window_view(p, kf, axis=1).max(-1)


def peaks_from(spec: np.ndarray, f0: int, lo: int, hi: int, p: Params) -> tuple[np.ndarray, np.ndarray]:
    """Peaks (frame, bin) with absolute frame in [lo, hi); spec row 0 is absolute frame f0.
    Callers pass p.ctx frames of context on both sides where they exist."""
    if len(spec) == 0 or hi <= lo:
        z = np.zeros(0, np.int64)
        return z, z
    w = spec - _box_mean(spec, ENV_T, ENV_F)
    # The floor drops peaks in digital silence (gated mic), which are pure numerical noise.
    cand = (w == _box_max(w, p.max_t, p.max_f)) & (w > p.thr) & (spec > np.log(1e-2))
    ft, fb = np.nonzero(cand)
    ft = ft + f0
    k = (ft >= lo) & (ft < hi)
    ft, fb = ft[k].astype(np.int64), fb[k].astype(np.int64) + FMIN
    o = np.lexsort((fb, ft))
    return ft[o], fb[o]


def hashes_from(ft: np.ndarray, fb: np.ndarray, p: Params) -> tuple[np.ndarray, np.ndarray]:
    """Pair each peak with up to p.fanout later peaks -> (hash, anchor frame), by frame."""
    n = len(ft)
    if n < 2:
        return np.zeros(0, np.uint32), np.zeros(0, np.int64)
    hs, ts = [], []
    cnt = np.zeros(n, np.int64)
    for k in range(1, 64):
        if k >= n:
            break
        a = np.arange(n - k)
        b = a + k
        dt = ft[b] - ft[a]
        if dt.min() > p.dt_max:
            break
        df = fb[b] - fb[a]
        ok = (dt >= 1) & (dt <= p.dt_max) & (np.abs(df) <= DF_MAX) & (cnt[a] < p.fanout)
        a, dt, df = a[ok], dt[ok], df[ok]
        cnt[a] += 1
        hs.append((fb[a].astype(np.uint32) << 13) | ((df + 64).astype(np.uint32) << 6) | dt.astype(np.uint32))
        ts.append(ft[a])
    if not hs:
        return np.zeros(0, np.uint32), np.zeros(0, np.int64)
    h, t = np.concatenate(hs), np.concatenate(ts)
    o = np.argsort(t, kind="stable")
    return h[o], t[o]


# ---- references ----------------------------------------------------------------


@dataclass
class Ref:
    name: str
    nframes: int
    dur: float  # seconds
    hash_per_s: np.ndarray  # reference hashes per second
    hash_frames: np.ndarray  # sorted anchor frames of the reference hashes

    @property
    def expect(self) -> np.ndarray:
        """Reference seconds that carry enough material to be checked for."""
        return self.hash_per_s >= 8

    def hashes_between(self, f0: int, f1: int) -> int:
        return int(np.searchsorted(self.hash_frames, f1) - np.searchsorted(self.hash_frames, f0))


class RefSet:
    def __init__(self, p: Params = DEFAULT):
        self.p = p
        self.refs: list[Ref] = []
        self._h = np.zeros(0, np.uint32)
        self._t = np.zeros(0, np.int64)
        self._r = np.zeros(0, np.int64)

    def add(self, name: str, x8: np.ndarray) -> None:
        """Fingerprint a clip (8 kHz mono float) and add it."""
        nframes, h, t = fingerprint(x8, self.p)
        self.add_hashes(name, nframes, len(x8) / SR, h, t)

    def add_hashes(self, name: str, nframes: int, dur: float, h: np.ndarray, t: np.ndarray) -> None:
        """Add a clip from its fingerprint (what fingerprint() returned for it)."""
        h, t = np.asarray(h, np.uint32), np.asarray(t, np.int64)
        nsec = int(np.ceil(nframes / FPS))
        self.refs.append(Ref(name, nframes, dur, np.bincount((t / FPS).astype(int), minlength=nsec), np.sort(t)))
        h = np.concatenate([self._h, h])
        t = np.concatenate([self._t, t])
        r = np.concatenate([self._r, np.full(len(h) - len(self._r), len(self.refs) - 1)])
        o = np.argsort(h, kind="stable")
        self._h, self._t, self._r = h[o], t[o], r[o]

    def digest(self) -> str:
        """sha1 of everything a match depends on: the clips' names, lengths and hashes."""
        d = hashlib.sha1()
        for r in self.refs:
            d.update(f"{r.name} {r.nframes} {r.dur!r}\n".encode())
        for a in (self._h, self._t, self._r):
            d.update(a.tobytes())
        return d.hexdigest()

    def lookup(self, qh: np.ndarray, qt: np.ndarray):
        """-> (ref id, ref anchor frame, stream anchor frame, pair dt) for every collision."""
        lo = np.searchsorted(self._h, qh, "left")
        n = np.searchsorted(self._h, qh, "right") - lo
        if not n.any():
            z = np.zeros(0, np.int64)
            return z, z, z, z
        qi = np.repeat(np.arange(len(qh)), n)
        ri = np.repeat(lo - np.r_[0, np.cumsum(n)[:-1]], n) + np.arange(n.sum())
        return self._r[ri], self._t[ri], qt[qi], (qh[qi] & 63).astype(np.int64)


def fingerprint(x8: np.ndarray, p: Params = DEFAULT) -> tuple[int, np.ndarray, np.ndarray]:
    """A clip's fingerprint: (STFT frames, hashes, their anchor frames), what RefSet keeps of it."""
    spec = logspec(x8)
    h, t = hashes_from(*peaks_from(spec, 0, 0, len(spec), p), p)
    return len(spec), h, t


def read_wav8(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if w.getframerate() != SR or w.getsampwidth() != 2:
            raise ValueError(f"{path}: need {SR} Hz 16-bit, got {w.getframerate()} Hz")
        ch = w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    return a.reshape(-1, ch).mean(1) if ch > 1 else a


# ---- reference files -------------------------------------------------------------
#
# refs.npz is a RefSet without the audio it came from: the clips stay with whoever
# recorded them; the repository and the image carry only their landmark hashes, which
# no audio can be rebuilt from. numpy savez_compressed arrays:
#   format   ()    int      REFS_FORMAT
#   params   ()    str      JSON of the Params the peaks and hashes were made with
#   name     (R,)  str      clip names
#   nframes  (R,)  int64    STFT frames in each clip
#   dur      (R,)  float64  seconds
#   count    (R,)  int64    hashes of each clip = its rows of hash / frame
#   hash     (N,)  uint32   landmark hashes (f1 << 13 | (df + 64) << 6 | dt), clip by clip
#   frame    (N,)  int32    anchor frame of each hash within its clip
# A clip's rows come in anchor-frame order, as fingerprint() returns them, so the RefSet
# read back is the one the clips would build. fone/ads/build_refs.py writes it.

REFS_FORMAT = 1
FINGERPRINT_PARAMS = ("max_t", "max_f", "thr", "fanout", "dt_max")  # the Params hashes depend on


def write_refs(path: Path, clips: list[tuple[str, np.ndarray]], p: Params = DEFAULT) -> None:
    """Fingerprint (name, 8 kHz mono float audio) clips into refs.npz."""
    prints = [(name, *fingerprint(x8, p), len(x8) / SR) for name, x8 in clips]
    np.savez_compressed(
        path, format=REFS_FORMAT, params=json.dumps(asdict(p)),
        name=np.array([f[0] for f in prints], str),
        nframes=np.array([f[1] for f in prints], np.int64),
        dur=np.array([f[4] for f in prints], np.float64),
        count=np.array([len(f[2]) for f in prints], np.int64),
        hash=np.concatenate([np.zeros(0, np.uint32)] + [f[2] for f in prints]).astype(np.uint32),
        frame=np.concatenate([np.zeros(0, np.int32)] + [f[3] for f in prints]).astype(np.int32))


def read_refs(path: Path, p: Params = DEFAULT) -> RefSet:
    """refs.npz -> RefSet matched with p. Its hashes must come from the same fingerprint
    settings; the matching ones (min_votes, ...) are free to differ."""
    with np.load(path) as z:
        if int(z["format"]) != REFS_FORMAT:
            raise ValueError(f"{path}: refs format {int(z['format'])}, this detector reads {REFS_FORMAT}: rebuild it")
        made = json.loads(str(z["params"]))
        if any(made.get(k) != getattr(p, k) for k in FINGERPRINT_PARAMS):
            raise ValueError(f"{path}: fingerprinted with {made}, the detector uses {asdict(p)}: rebuild it")
        refs = RefSet(p)
        at = 0
        for name, nframes, dur, n in zip(z["name"], z["nframes"], z["dur"], z["count"]):
            refs.add_hashes(str(name), int(nframes), float(dur), z["hash"][at:at + n], z["frame"][at:at + n])
            at += int(n)
    return refs


# ---- matching ------------------------------------------------------------------

END_SLACK_S = 0.15  # after the last reference peak proven present
STOP_EXPECTED = 25.0  # a clip has stopped once this many expected votes failed to arrive
START_SLACK_S = 4  # first matched second this close to the clip start: it played from the start
SILENT_LOGMAG = float(np.log(0.005))  # max-bin |STFT| of a digitally silent frame (~ -90 dBFS)
SNAP_GAP_S = 0.4  # a digital silence at least this long marks a clip boundary
SNAP_BACK_S = 6.0
SAME_PLAY_S = 5.0  # alignments of one clip this close together can belong to one play


@dataclass
class Hit:
    """One play of a reference clip, in seconds since the first fed sample. A play may
    hold several alignments when the clip was re-edited (e.g. a different gap after its
    first line): each explains a different part of the reference."""

    ref: str
    offset: float  # stream time of reference t=0 (main alignment)
    start: float
    end: float
    votes: int
    covered: float  # fraction of the reference's seconds explained
    partial: bool
    detected_at: float | None = None  # seconds fed when it was recognised
    ended_at: float | None = None  # seconds fed when its end was settled
    aligns: int = 1


def cluster_offsets(rid, rt, qt, dt, p: Params) -> list[tuple]:
    """Group collisions into candidates (ref, offset frame, votes, ref anchor frames,
    ref target frames), strongest first. Votes within +-1 frame are pooled."""
    out: list[tuple] = []
    if len(rid) == 0:
        return out
    off = qt - rt
    for r in np.unique(rid):
        m = rid == r
        o, rr, dd = off[m], rt[m], dt[m]
        u, c = np.unique(o, return_counts=True)
        cs = c.astype(np.int64).copy()
        adj = np.diff(u) == 1
        cs[1:] += np.where(adj, c[:-1], 0)
        cs[:-1] += np.where(adj, c[1:], 0)
        taken: list[int] = []
        for i in np.argsort(-cs):
            if cs[i] < p.min_votes:
                break
            if any(abs(u[i] - t) <= 3 for t in taken):
                continue
            taken.append(int(u[i]))
            k = np.abs(o - u[i]) <= 1
            out.append((int(r), int(u[i]), int(cs[i]), rr[k], rr[k] + dd[k]))
    out.sort(key=lambda x: -x[2])
    return out


def coverage(ref: Ref, rts: np.ndarray, p: Params) -> np.ndarray:
    n = len(ref.hash_per_s)
    sec = np.bincount(np.clip((rts / FPS).astype(int), 0, n - 1), minlength=n)
    return (sec >= p.sec_votes) & ref.expect


def snap_start(spec: np.ndarray, s0: int, sf: int) -> int:
    """For a play whose first matched second is well into the clip (a different take of its
    opening, or we joined mid-clip): if a >= SNAP_GAP_S digital silence ends within
    SNAP_BACK_S before frame sf, the clip started right after it (the streamer's mic is
    gated while clips play). Otherwise keep sf. spec row 0 is absolute frame s0."""
    lo = max(s0, sf - int(SNAP_BACK_S * FPS))
    if sf - lo < 8:
        return sf
    silent = spec[lo - s0: sf - s0].max(1) < SILENT_LOGMAG
    need = int(SNAP_GAP_S * FPS)
    run = 0
    for i in range(len(silent) - 1, -1, -1):
        run = run + 1 if silent[i] else 0
        if run >= need:
            j = i + run
            while j < len(silent) and silent[j]:
                j += 1
            return lo + j
    return sf


_play_ids = itertools.count(1)


class _Play:
    """One play of a clip: its alignments and the reference seconds they explain."""

    def __init__(self, r: int, ref: Ref, off: int, votes: int, rts, rte, p: Params):
        self.id, self.r, self.ref, self.p = next(_play_ids), r, ref, p
        self.al: dict[int, dict] = {}
        self.add(off, votes, rts, rte)

    def add(self, off: int, votes: int, rts, rte) -> None:
        self.al[off] = dict(rts=rts.copy(), rte=rte.copy(), votes=votes)
        self._refresh(off)

    def vote(self, off: int, rts, rte) -> None:
        a = self.al[off]
        a["rts"] = np.concatenate([a["rts"], rts])
        a["rte"] = np.concatenate([a["rte"], rte])
        a["votes"] += len(rts)
        self._refresh(off)

    def _refresh(self, off: int) -> None:
        a = self.al[off]
        a["cov"] = coverage(self.ref, a["rts"], self.p)
        # First/last reference frame proven present, ignoring isolated chance votes.
        o = np.argsort(a["rts"])
        r, e = a["rts"][o], a["rte"][o]
        ok = np.zeros(len(r), bool)
        if len(r) > 1:
            close = np.diff(r) <= FPS
            ok[1:] |= close
            ok[:-1] |= close
        if not ok.any():
            ok[:] = True
        a["first_r"], a["last_t"] = int(r[ok].min()), int(e[ok].max())

    def tail_expected(self, o: int, upto: int) -> float:
        """Votes alignment o should have produced between its last proven frame and
        reference frame `upto`, at the rate it has matched so far."""
        a = self.al[o]
        rho = a["votes"] / max(1, self.ref.hashes_between(a["first_r"], a["last_t"] + 1))
        return rho * self.ref.hashes_between(a["last_t"] + 1, min(upto, self.ref.nframes))

    @property
    def cov(self) -> np.ndarray:
        c = np.zeros(len(self.ref.hash_per_s), bool)
        for a in self.al.values():
            c |= a["cov"]
        return c

    @property
    def votes(self) -> int:
        return sum(a["votes"] for a in self.al.values())

    def density(self, o: int) -> float:
        a = self.al[o]
        return a["votes"] / max(1, a["cov"].sum())

    def strong(self) -> list[int]:
        """Alignments that carry real evidence (drops weak echoes that slipped in)."""
        dmax = max(self.density(o) for o in self.al)
        return [o for o in self.al if self.density(o) >= 0.25 * dmax and self.al[o]["cov"].any()]

    @property
    def main(self) -> int:
        return max(self.al, key=lambda o: (self.al[o]["cov"].sum(), self.al[o]["votes"]))

    def stream_secs(self) -> set[int]:
        out: set[int] = set()
        for o, a in self.al.items():
            out |= {int(o / FPS + s) for s in np.flatnonzero(a["cov"])}
        return out

    def extent(self) -> tuple[float, float]:
        return min(self.al) / FPS, max(self.al) / FPS + self.ref.dur

    def span(self, spec: np.ndarray, s0: int, settled: bool, reach: int) -> tuple[float, float, bool]:
        """(start s, end s, partial). Until settled, assume the clip plays out (the safe
        side for muting). `reach`: stream frame up to which hashes were matched."""
        e0 = int(np.flatnonzero(self.ref.expect)[0])
        full = int(round(self.ref.dur * FPS))
        starts, ends, from_start = [], [], False
        for o in self.strong():
            a = self.al[o]
            if np.flatnonzero(a["cov"])[0] <= e0 + START_SLACK_S:
                from_start = True
                starts.append(o)
            else:
                starts.append(o + a["first_r"])
            if not settled or a["last_t"] >= full - 2 * FPS or \
                    self.tail_expected(o, min(full, reach - o)) < STOP_EXPECTED:
                ends.append(o + full)  # played out (or its tail is masked by talk-over)
            else:
                ends.append(o + min(full, a["last_t"] + int(END_SLACK_S * FPS)))
        sf, ef = min(starts), max(ends)
        if not from_start:
            sf = snap_start(spec, s0, sf)
        partial = (not from_start) or ef < self.main + full - 2 * FPS
        return sf / FPS, ef / FPS, bool(partial)

    def hit(self, spec: np.ndarray, s0: int, settled: bool, reach: int) -> Hit:
        s, e, partial = self.span(spec, s0, settled, reach)
        cov_frac = float(self.cov.sum() / max(1, self.ref.expect.sum()))
        return Hit(self.ref.name, round(self.main / FPS, 3), round(float(s), 3), round(float(e), 3), int(self.votes),
                   round(cov_frac, 3), partial, aligns=len(self.strong()))


def _assign(plays: list[_Play], r: int, ref: Ref, off: int, votes: int, rts, rte, p: Params):
    """Attach a candidate alignment to a play: 'new' play, 'added' alignment, 'echo' or 'weak'.
    A clip can't play twice at once, so a candidate overlapping a play of the same clip
    either explains stream seconds that play doesn't (a re-edited part: 'added') or is an
    echo (rhythmic music aligns weakly at +-0.5/1/2/8 s of the true offset)."""
    cov = coverage(ref, rts, p)
    if cov.sum() < p.min_seconds:
        return "weak", None
    idx = np.flatnonzero(cov)
    c_lo, c_hi = off / FPS + idx[0], off / FPS + idx[-1] + 1
    for pl in plays:
        if pl.r != r:
            continue
        lo, hi = pl.extent()
        if not (c_lo < hi + SAME_PLAY_S and c_hi > lo - SAME_PLAY_S):
            continue
        if off in pl.al:
            return "echo", pl
        expl = pl.stream_secs()
        new_s = [x for x in (int(off / FPS + i) for i in idx) if not ({x - 1, x, x + 1} & expl)]
        new_r = int((cov & ~pl.cov).sum())
        dens = votes / max(1, cov.sum())
        if len(new_s) >= p.min_seconds and new_r >= p.min_seconds and \
                dens >= 0.25 * max(pl.density(o) for o in pl.al):
            pl.add(off, votes, rts, rte)
            return "added", pl
        return "echo", pl
    pl = _Play(r, ref, off, votes, rts, rte, p)
    plays.append(pl)
    return "new", pl


class AdDetector:
    """Streaming detector. feed() 8 kHz mono float audio as it arrives; it returns events
      ("start", hit)   a clip was recognised; hit.start may lie in the past and hit.end is
                       provisional (assumes it plays out)
      ("update", hit)  the play's span changed
      ("end", hit)     it played out or stopped early; hit.end is final
    Hit objects are updated in place; `hits` lists every play seen so far.

    A peak is final p.ctx frames after it and a hash p.dt_max frames after that, so audio
    at time t has been matched once t + (ctx + dt_max) / FPS (~0.9 s) has been fed.
    Recognition then needs p.min_votes votes, i.e. ~1-2.5 s of the clip.

    Early stop: votes arrive at a rate rho per reference hash (measured on this play).
    Once the reference hashes since the last proven frame should have produced
    STOP_EXPECTED votes and none came (and >= STOP_MIN_S passed), the clip has stopped.
    """

    KEEP_S = 12.0  # spectrogram history (snap_start)
    PENDING_S = 12.0  # unexplained collisions kept for clustering
    DONE_S = 30.0  # finished plays that a late re-edited part may still extend
    STOP_MIN_S = 1.5
    CHUNK = 30 * SR  # bigger inputs are processed in pieces: bounded memory

    def __init__(self, refs: RefSet):
        self.refs, self.p = refs, refs.p
        self.audio = np.zeros(0, np.float32)
        self.a0 = 0  # absolute sample index of audio[0]
        self.spec = np.zeros((0, FMAX - FMIN), np.float32)
        self.spec0 = 0  # absolute frame of spec[0]
        self.nframes = 0
        self.peak_done = 0
        self.pk_t = np.zeros(0, np.int64)
        self.pk_f = np.zeros(0, np.int64)
        self.hash_done = 0  # hashes anchored before this frame have been matched
        self.plays: list[_Play] = []  # may still be playing
        self.done: list[_Play] = []  # recently ended (a re-edited part may re-open one)
        self._hits: dict[int, Hit] = {}  # play id -> Hit
        self.pending: list[tuple] = []
        self.cpu = 0.0

    @property
    def fed(self) -> float:
        """Seconds of audio fed so far."""
        return (self.a0 + len(self.audio)) / SR

    @property
    def matched(self) -> float:
        """Seconds of audio whose hashes have been matched."""
        return self.hash_done / FPS

    @property
    def hits(self) -> list[Hit]:
        return sorted(self._hits.values(), key=lambda h: h.start)

    def feed(self, x: np.ndarray, final: bool = False) -> list[tuple[str, Hit]]:
        x = np.asarray(x, np.float32)
        events: list[tuple[str, Hit]] = []
        for i in range(0, max(len(x), 1), self.CHUNK):
            events += self._feed(x[i:i + self.CHUNK], final and i + self.CHUNK >= len(x))
        return events

    def _feed(self, x: np.ndarray, final: bool) -> list[tuple[str, Hit]]:
        c0 = time.process_time()
        p = self.p
        events: list[tuple[str, Hit]] = []
        self.audio = np.concatenate([self.audio, x])
        s = self.nframes * HOP - self.a0
        n_new = frames_of(len(self.audio) - s)
        if n_new:
            self.spec = np.concatenate([self.spec, logspec(self.audio[s: s + (n_new - 1) * HOP + NFFT])])
            self.nframes += n_new
            drop = self.nframes * HOP - self.a0
            self.audio = self.audio[drop:]
            self.a0 += drop
        lim = self.nframes if final else self.nframes - p.ctx
        if lim > self.peak_done:
            c_lo = max(self.spec0, self.peak_done - p.ctx)
            c_hi = min(self.nframes, lim + p.ctx)
            ft, fb = peaks_from(self.spec[c_lo - self.spec0: c_hi - self.spec0], c_lo, self.peak_done, lim, p)
            self.pk_t = np.concatenate([self.pk_t, ft])
            self.pk_f = np.concatenate([self.pk_f, fb])
            self.peak_done = lim
        a_lim = self.peak_done if final else self.peak_done - p.dt_max
        if a_lim > self.hash_done:
            h, t = hashes_from(self.pk_t, self.pk_f, p)
            sel = (t >= self.hash_done) & (t < a_lim)
            self.hash_done = a_lim
            keep = self.pk_t >= a_lim
            self.pk_t, self.pk_f = self.pk_t[keep], self.pk_f[keep]
            events += self._match(h[sel], t[sel])
        events += self._check_ends(final)
        keep_from = max(0, self.nframes - int(self.KEEP_S * FPS))
        if keep_from > self.spec0:
            self.spec = self.spec[keep_from - self.spec0:]
            self.spec0 = keep_from
        self.cpu += time.process_time() - c0
        return events

    def _emit(self, kind: str, pl: _Play, settled: bool = False) -> list[tuple[str, Hit]]:
        h = pl.hit(self.spec, self.spec0, settled, self.hash_done)
        old = self._hits.get(pl.id)
        if old is None:
            h.detected_at = round(self.fed, 3)
            self._hits[pl.id] = h
            return [(kind, h)]
        changed = (old.start, old.end) != (h.start, h.end)
        for k in ("offset", "start", "end", "votes", "covered", "partial", "aligns"):
            setattr(old, k, getattr(h, k))
        if settled:
            old.ended_at = round(self.fed, 3)
        return [(kind, old)] if (changed or kind == "end") else []

    def _match(self, h: np.ndarray, t: np.ndarray) -> list[tuple[str, Hit]]:
        p = self.p
        events: list[tuple[str, Hit]] = []
        rid, rt, qt, dt = self.refs.lookup(h, t)
        if len(rid) == 0:
            return events
        off = qt - rt
        unexplained = np.ones(len(rid), bool)
        for pl in self.plays + self.done:
            for o in list(pl.al):
                m = (rid == pl.r) & (np.abs(off - o) <= 1)
                if m.any():
                    pl.vote(o, rt[m], rt[m] + dt[m])
                    unexplained &= ~m
                    if pl in self.done and m.sum() >= 3:  # its end was called too early: resume
                        self.done.remove(pl)
                        self.plays.append(pl)
                        events += self._emit("update", pl)
        u = unexplained
        self.pending.append((rid[u], rt[u], qt[u], dt[u]))
        horizon = self.hash_done - int(self.PENDING_S * FPS)
        self.pending = [x for x in self.pending if len(x[2]) and x[2].max() >= horizon]
        if not self.pending:
            return events
        rr, rrt, qqt, ddt = (np.concatenate([x[i] for x in self.pending]) for i in range(4))
        for r, o, votes, rts, rte in cluster_offsets(rr, rrt, qqt, ddt, p):
            ref = self.refs.refs[r]
            recent = [d for d in self.done if d.r == r]
            kind, pl = _assign(self.plays + recent, r, ref, o, votes, rts, rte, p)
            if kind == "new":
                self.plays.append(pl)
                events += self._emit("start", pl)
            elif kind == "added":
                if pl in self.done:
                    self.done.remove(pl)
                    self.plays.append(pl)
                events += self._emit("update", pl)
            else:
                continue
            # These collisions are explained now; don't let them seed another candidate.
            self.pending = [(a[~m], b[~m], c[~m], d[~m]) for a, b, c, d in self.pending
                            for m in [(a == r) & (np.abs(c - b - o) <= 1)]]
        return events

    def _stopped(self, pl: _Play) -> bool:
        """Every strong alignment has gone quiet for >= STOP_MIN_S while it should have
        produced >= STOP_EXPECTED votes."""
        for o in pl.strong():
            reach = min(pl.ref.nframes, self.hash_done - o)
            if reach - pl.al[o]["last_t"] < self.STOP_MIN_S * FPS or pl.tail_expected(o, reach) < STOP_EXPECTED:
                return False
        return True

    def _check_ends(self, final: bool) -> list[tuple[str, Hit]]:
        events: list[tuple[str, Hit]] = []
        for pl in list(self.plays):
            played_out = all(self.hash_done - o >= pl.ref.nframes for o in pl.strong())
            if final or played_out or self._stopped(pl):
                self.plays.remove(pl)
                self.done = [d for d in self.done if self.hash_done - max(d.al) < self.DONE_S * FPS] + [pl]
                events += self._emit("end", pl, settled=True)
        return events
