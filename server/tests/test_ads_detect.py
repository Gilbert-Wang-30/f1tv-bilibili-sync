"""Streaming clip detector on synthetic mixes of synthetic reference clips.

The streamer's real sponsor clips are not in the repository: fone/ads/refs.npz carries
their fingerprints only (ads.json describes them). The clips here are stand-ins of the
same lengths; the shipped refs.npz is checked to load and to match its manifest.
"""

import functools
import json
import subprocess
import sys
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from fone.ads import REFS_DIR, load_refs
from fone.ads_detect import DEFAULT, SR, AdDetector, RefSet, frames_of, read_refs, write_refs

A, B, C = "clip_A", "clip_B", "clip_C"
CLIPS = {A: (47.04, 1), B: (36.62, 2), C: (18.14, 3)}  # seconds, seed: the real reel's lengths


def synth_clip(seconds: float, seed: int) -> np.ndarray:
    """A stand-in for a produced advert: three voices of plucked tones at random pitches
    and onsets (a jingle, speech-like formants) over a quiet noise bed. Dense, distinct
    spectral peaks and no repetition that could align a clip with itself."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    x = 0.01 * np.convolve(rng.standard_normal(n), np.ones(4) / 4, "same")
    for _ in range(3):
        at = rng.uniform(0, 0.3)
        while at < seconds:
            dur, f = rng.uniform(0.12, 0.45), np.exp(rng.uniform(np.log(150), np.log(1900)))
            i0, i1 = int(at * SR), min(n, int((at + dur) * SR))
            u = np.arange(i1 - i0) / SR
            env = (1 - np.exp(-u / 0.004)) * np.exp(-3 * u / dur)
            x[i0:i1] += rng.uniform(0.15, 0.4) * env * (np.sin(2 * np.pi * f * u) + 0.5 * np.sin(4 * np.pi * f * u + 1))
            at += rng.uniform(0.07, 0.3)
    return (0.9 * x / np.abs(x).max()).astype(np.float32)


@functools.cache
def clip(name: str) -> np.ndarray:
    return synth_clip(*CLIPS[name])


def write_wav(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


@pytest.fixture(scope="module")
def refs():
    refs = RefSet(DEFAULT)
    for name in (A, B, C):
        refs.add(name, clip(name))
    return refs


def talk(seconds: float, seed: int = 0, level: float = 0.05) -> np.ndarray:
    """Speech-like filler: low-passed noise with a syllable-rate envelope plus a wandering
    voiced tone, so it has peaks to hash but nothing that lines up with a clip."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    x = np.convolve(rng.standard_normal(n), np.ones(4) / 4, "same")
    pitch = 140 + 40 * np.sin(2 * np.pi * 0.3 * t + rng.uniform(0, 6))
    x += 0.8 * np.sin(2 * np.pi * np.cumsum(pitch) / SR)
    env = (0.55 + 0.45 * np.sin(2 * np.pi * 3.7 * t + rng.uniform(0, 6))) ** 2
    return (level * x * env).astype(np.float32)


def mix(parts: list[np.ndarray], over: float = 0.0, seed: int = 9) -> np.ndarray:
    """Concatenate, then optionally lay quiet talk over everything (the streamer talking)."""
    x = np.concatenate(parts)
    if over:
        x = x + talk(len(x) / SR, seed=seed, level=over)
    return x.astype(np.float32)


def run(refs, x: np.ndarray, chunk_s: float = 2.0):
    """Feed in chunks; events are snapshots (kind, ref, start, end, seconds fed) because
    hits are updated in place."""
    det = AdDetector(refs)
    step = int(chunk_s * SR)
    events = []
    for i in range(0, len(x), step):
        for kind, h in det.feed(x[i:i + step], final=i + step >= len(x)):
            events.append((kind, h.ref, h.start, h.end, det.fed))
    return det, events


def same(a, b, tol=0.02):
    return len(a) == len(b) and all(x.ref == y.ref and abs(x.start - y.start) < tol and abs(x.end - y.end) < tol
                                    for x, y in zip(a, b))


def test_finds_clip_under_talk_with_exact_edges(refs):
    a = clip(A)
    det, events = run(refs, mix([talk(10, 1), a, talk(10, 2)], over=0.01))
    (h,) = det.hits
    assert h.ref == A and not h.partial
    assert abs(h.start - 10.0) < 0.05
    assert abs(h.end - (10.0 + len(a) / SR)) < 0.05
    assert h.detected_at - 10.0 < 3.5  # recognised ~2 s into the clip, fed in 2-s chunks


def test_reel_back_to_back(refs):
    gap = np.zeros(int(0.8 * SR), np.float32)  # the gated mic between clips
    a, b, c = clip(A), clip(B), clip(C)
    x = mix([talk(5, 3), a, gap, b, gap, c, talk(5, 4)])
    det, _ = run(refs, x)
    t = 5.0
    want = []
    for name, y in ((A, a), (B, b), (C, c)):
        want.append((name, t, t + len(y) / SR))
        t += len(y) / SR + 0.8
    got = [(h.ref, h.start, h.end) for h in det.hits]
    assert [g[0] for g in got] == [w[0] for w in want]
    for (_, s, e), (_, ws, we) in zip(got, want):
        assert abs(s - ws) < 0.05 and abs(e - we) < 0.05


def test_clip_cut_short_ends_where_it_stopped(refs):
    b = clip(B)[: 10 * SR]
    det, events = run(refs, mix([talk(6, 5), b, talk(12, 6)], over=0.01))
    (h,) = det.hits
    assert h.ref == B and h.partial
    assert abs(h.start - 6.0) < 0.05
    assert abs(h.end - 16.0) < 0.3
    # Until the stop is evident the span assumes the clip plays out (safe side for muting)...
    (start,) = [e for e in events if e[0] == "start"]
    assert abs(start[3] - (6.0 + len(clip(B)) / SR)) < 0.05
    # ...and the early stop is settled a few seconds after it happened.
    assert h.ended_at - 16.0 <= 4.5  # 1.5 s of missing votes + 0.9 s context + 2-s chunks


def test_no_hits_in_plain_talk(refs):
    det, _ = run(refs, mix([talk(240, 7)], over=0.02, seed=8))
    assert det.hits == []


def test_chunk_size_does_not_change_the_answer(refs):
    x = mix([talk(4, 11), clip(C), talk(4, 12)], over=0.01)
    one, _ = run(refs, x, chunk_s=len(x) / SR + 1)
    small, _ = run(refs, x, chunk_s=0.5)
    assert len(one.hits) == 1 and same(one.hits, small.hits)


# ---- reference files -------------------------------------------------------------------


def test_refs_file_round_trip(refs, tmp_path):
    """refs.npz reads back as the RefSet its clips build, and detects like it."""
    write_refs(tmp_path / "refs.npz", [(n, clip(n)) for n in (A, B, C)])
    back = read_refs(tmp_path / "refs.npz")
    assert [r.name for r in back.refs] == [A, B, C] and back.digest() == refs.digest()
    x = mix([talk(3, 41), clip(B), talk(3, 42)], over=0.01)
    one, _ = run(refs, x)
    two, _ = run(back, x)
    assert len(one.hits) == 1 and same(one.hits, two.hits, tol=1e-9)
    # Hashes depend on the fingerprint settings, not the matching ones.
    with pytest.raises(ValueError, match="rebuild"):
        read_refs(tmp_path / "refs.npz", replace(DEFAULT, fanout=DEFAULT.fanout + 1))
    assert read_refs(tmp_path / "refs.npz", replace(DEFAULT, min_votes=50)).p.min_votes == 50


def test_build_refs_tool_and_wav_fallback_agree(tmp_path):
    """build_refs.py turns a directory of WAVs into refs.npz (+ ads.json); loading that gives
    the refs and version that fingerprinting the WAVs on the fly gives."""
    wavs = tmp_path / "clips"
    wavs.mkdir()
    for name in (A, B, C):
        write_wav(wavs / f"{name}.wav", clip(name))
    out = tmp_path / "refs"
    out.mkdir()
    subprocess.run([sys.executable, str(REFS_DIR / "build_refs.py"), str(wavs), "-o", str(out / "refs.npz")],
                   check=True, capture_output=True)
    from_npz, v_npz = load_refs(out)
    from_wav, v_wav = load_refs(wavs)
    assert v_npz == v_wav and from_npz.digest() == from_wav.digest()
    manifest = json.loads((out / "ads.json").read_text())["refs"]
    assert [(m["name"], m["duration"]) for m in manifest] == [(n, CLIPS[n][0]) for n in (A, B, C)]
    assert all(m["description"] for m in manifest)


def test_shipped_refs_match_their_manifest():
    """fone/ads/refs.npz, what the server runs with, loads and holds what ads.json says;
    the clips' audio is not in the repository."""
    refs, version = load_refs()
    manifest = json.loads((REFS_DIR / "ads.json").read_text())["refs"]
    assert [r.name for r in refs.refs] == [m["name"] for m in manifest]
    for r, m in zip(refs.refs, manifest):
        assert abs(r.dur - m["duration"]) < 0.01 and r.nframes == frames_of(round(r.dur * SR))
        assert r.hash_per_s.sum() > 100 * r.dur  # a produced clip: hundreds of hashes a second
    assert len(version) == 12 and int(version, 16) >= 0
    assert not list(REFS_DIR.glob("*.wav"))
