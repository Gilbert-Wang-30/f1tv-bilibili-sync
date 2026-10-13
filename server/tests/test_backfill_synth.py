"""A synthetic Bilibili-style replay for the backfill tests (real ffmpeg, a few seconds).

One continuous 100 s encode ("the stream"), cut the way Bilibili cuts replays: fragment 2
starts by repeating the last 5 s of fragment 1, and page 2 starts by repeating page 1's last
5 s. Every page packet is a byte copy of a known stream packet, so the right answer is known
exactly. Video: 25 fps, a keyframe every 5 s, repeated as whole GOPs at the same moments.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from fone.backfill_dedup import AudioPart, read_adts

RATE = 48000
PKT = 1024 / RATE
FRAG = 1406  # packets in fragment 1 (~30 s)
LAG = 234  # repeated packets (~5 s), like Bilibili's
SECONDS = 100


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


@dataclass
class Synth:
    dir: Path
    stream: AudioPart  # the continuous encode
    audio: list[Path]  # page audio (m4a, like a DASH download)
    video: list[Path] | None
    index: list[list[int]]  # stream packet index of every page packet
    cuts: list[tuple[int, int]]  # (global cut, lag) a correct dedup finds


def write_frames(stream: AudioPart, idx: list[int], dest: Path) -> None:
    with open(stream.path, "rb") as src, open(dest, "wb") as out:
        for i in idx:
            src.seek(stream.offsets[i])
            out.write(src.read(stream.lengths[i]))


def speechlike(seconds: float, rate: int, seed: int = 7) -> np.ndarray:
    """Noise and tones under a random syllable-rate envelope: distinctive at every point (no
    repeats for the dedup to trip on, unique band-energy patterns for the alignment)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    out = np.zeros(n, np.float32)
    t = 0
    while t < n:
        k = int(rng.uniform(0.05, 0.3) * rate)
        seg = rng.normal(0, 1, k) * rng.uniform(0.02, 0.4)
        f = rng.uniform(150, 2500)
        seg += np.sin(2 * np.pi * f * np.arange(k) / rate) * rng.uniform(0, 0.3)
        out[t:t + k] = seg[: n - t]
        t += k
    return np.clip(out, -1, 1)


def make_synth(d: Path, video: bool = True) -> Synth:
    d.mkdir(parents=True, exist_ok=True)
    speechlike(SECONDS, RATE).tofile(d / "stream.f32")
    ff("-f", "f32le", "-ar", str(RATE), "-ac", "1", "-i", str(d / "stream.f32"), "-ac", "2", "-c:a", "aac",
       "-b:a", "128k", "-f", "adts", str(d / "stream.aac"))
    stream = read_adts(d / "stream.aac")
    index = [list(range(0, FRAG)) + list(range(FRAG - LAG, 2 * FRAG)), list(range(2 * FRAG - LAG, stream.n))]
    audio = []
    for k, idx in enumerate(index, 1):
        write_frames(stream, idx, d / f"page{k}.aac")
        ff("-f", "aac", "-i", str(d / f"page{k}.aac"), "-c", "copy", "-f", "mp4", str(d / f"p{k}_audio.m4a"))
        audio.append(d / f"p{k}_audio.m4a")
    n1 = len(index[0])
    cuts = [(FRAG, LAG), (n1, LAG)]
    vids = None
    if video:
        ff("-f", "lavfi", "-i", f"testsrc2=s=64x36:r=25:d={SECONDS}", "-c:v", "libx264", "-g", "125",
           "-keyint_min", "125", "-sc_threshold", "0", "-bf", "0", "-pix_fmt", "yuv420p", str(d / "stream.mp4"))
        vids = []
        for k, ranges in enumerate([[(0, 30), (25, 60)], [(55, SECONDS)]], 1):
            pieces = []
            for n, (a, b) in enumerate(ranges):
                p = d / f"v{k}_{n}.mp4"
                seek = ["-ss", f"{a + 1.0}"] if a else []
                ff(*seek, "-i", str(d / "stream.mp4"), "-frames:v", str((b - a) * 25), "-c", "copy",
                   "-avoid_negative_ts", "make_zero", str(p))
                pieces.append(p)
            lst = d / f"v{k}.txt"
            lst.write_text("".join(f"file '{p}'\n" for p in pieces))
            ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(d / f"p{k}_video.mp4"))
            vids.append(d / f"p{k}_video.mp4")
    return Synth(d, stream, audio, vids, index, cuts)


@pytest.fixture(scope="session")
def synth(tmp_path_factory) -> Synth:
    return make_synth(tmp_path_factory.mktemp("synth"))


def test_synthetic_pages_are_byte_copies_of_the_stream(synth):
    from fone.backfill_dedup import load_part
    for k, src in enumerate(synth.audio):
        part = load_part(src, synth.dir / f"check{k}.aac")
        assert part.n == len(synth.index[k])
        assert all(part.hashes[i] == synth.stream.hashes[j] for i, j in enumerate(synth.index[k]))
