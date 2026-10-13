"""Write a de-duplicated replay as a file that looks like the recorder caught it live.

What makes it interchangeable with a 录播姬 recording, as far as fone can tell:
  * layout and name: <import dir>/<channel>/<yyyyMMdd-HHmmss-fff Beijing>-<room title>.flv,
    the name time being the recorder-frame wall time of the first audio sample
  * FLV with the stream's H.264 + AAC (both stream-copied when keyframes allow), timestamps
    from 0, the ~30 ms of audio a replay loses at each fragment join kept as timestamp gaps
    so that media time stays proportional to wall time
  * mtime = wall time of the last sample (fone maps a finished file by mtime - duration)
It is staged under a non-media name (.part) and renamed only after the mtime is set, so fone
never mistakes a half-copied file for a live recording.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .backfill_dedup import AudioPart, Range, Timeline, VideoRange, video_packets, video_plan
from .backfill_start import Placement

SEEK_PAST_KEYFRAME = 1.0  # fragmented-MP4 seeks land one GOP early when aimed just past a keyframe
# A re-encode takes every core for many minutes: below the live commentary path, and
# below everything else on this box.
NICE = ("nice", "-n", "15") if shutil.which("nice") else ()
_UNSAFE = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')


DURATION_TOL = 0.1  # audio in the FLV vs the plan (s)
VIDEO_TOL = 1.0


@dataclass
class Piece:
    path: Path
    offset: float  # where it starts in the output timeline (s)
    length: float  # its own media length (s)


def safe_title(title: str) -> str:
    t = _UNSAFE.sub("_", title).strip(" ._") or "replay"
    # Filenames are capped at 255 bytes, and a CJK character takes 3: keep the name, its
    # 24-byte time prefix and ".flv.part" well inside that.
    return t.encode()[:180].decode(errors="ignore").rstrip(" ._") or "replay"


def import_name(start: float, title: str, tz: ZoneInfo) -> str:
    ms = round(start * 1000)
    dt = datetime.fromtimestamp(ms // 1000, tz)
    return f"{dt:%Y%m%d-%H%M%S}-{ms % 1000:03d}-{safe_title(title)}.flv"


def run(*cmd: str) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        name = cmd[len(NICE)] if NICE and cmd[:len(NICE)] == NICE else cmd[0]
        raise RuntimeError(f"{name} failed: {r.stderr.strip()[-400:]}")
    return r.stdout


# ---- audio ---------------------------------------------------------------------------------------


def audio_piece(part: AudioPart, r: Range, dest: Path) -> int:
    """Frames [i0, i1) of a page, byte-exact (ADTS frames are contiguous in the file)."""
    a = part.offsets[r.i0]
    b = part.offsets[r.i1 - 1] + part.lengths[r.i1 - 1]
    with open(part.path, "rb") as src, open(dest, "wb") as out:
        src.seek(a)
        left = b - a
        while left:
            buf = src.read(min(left, 8 << 20))
            if not buf:
                raise IOError(f"{part.path}: short read")
            out.write(buf)
            left -= len(buf)
    return r.i1 - r.i0


# ---- video ---------------------------------------------------------------------------------------


def _first_hashes(path: Path, interval: str | None = None) -> list[tuple[float, str]]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_data_hash", "MD5", "-show_entries",
           "packet=pts_time,data_hash", "-of", "csv=p=0"]
    if interval:
        cmd += ["-read_intervals", interval]
    out = []
    for line in run(*cmd, str(path)).splitlines():
        f = line.split(",")
        if len(f) == 2 and f[0] not in ("", "N/A"):
            out.append((float(f[0]), f[1]))
    return out


def video_piece_copy(src: Path, vr: VideoRange, dest: Path) -> None:
    """Whole GOPs [start, end) by frame count, stream copy; checks where the seek landed."""
    seek = ["-ss", f"{vr.start + SEEK_PAST_KEYFRAME:.3f}"] if vr.start > 0 else []
    run("ffmpeg", "-nostdin", "-v", "error", "-y", *seek, "-i", str(src), "-frames:v", str(vr.frames),
        "-map", "0:v:0", "-c", "copy", "-avoid_negative_ts", "make_zero", "-f", "mp4", str(dest))
    first = _first_hashes(dest, "%+#1")
    near = _first_hashes(src, f"{max(0.0, vr.start - 7):.3f}%{vr.start + 7:.3f}")
    landed = [t for t, h in near if first and h == first[0][1]]
    if not landed or min(abs(t - vr.start) for t in landed) > 0.02:
        raise RuntimeError(f"video seek in {src.name} landed at {landed} instead of {vr.start:.3f}")


def video_piece_encode(src: Path, start: float, length: float, dest: Path, fps: int = 25) -> None:
    """Frame-accurate fallback when keyframes don't sit on the cuts: decode and re-encode."""
    run(*NICE, "ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{start:.6f}", "-i", str(src), "-t", f"{length:.6f}",
        "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", str(2 * fps), "-r", str(fps),
        "-pix_fmt", "yuv420p", "-an", "-f", "mp4", str(dest))


# ---- container -----------------------------------------------------------------------------------


def concat_list(pieces: list[Piece], dest: Path) -> Path:
    """ffmpeg concat-demuxer list. Each piece's `duration` runs to the next piece's offset, so the
    gaps between pieces become timestamp gaps in the output."""
    lines = ["ffconcat version 1.0"]
    for i, p in enumerate(pieces):
        nxt = pieces[i + 1].offset if i + 1 < len(pieces) else p.offset + p.length
        lines += [f"file '{p.path.as_posix()}'", f"duration {max(p.length, nxt - p.offset):.6f}"]
    dest.write_text("\n".join(lines) + "\n")
    return dest


def mux_flv(audio: list[Piece], video: list[Piece] | None, work: Path, dest: Path) -> None:
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list(audio, work / "audio.ffconcat"))]
    maps = ["-map", "0:a:0"]
    if video:
        args += ["-f", "concat", "-safe", "0", "-i", str(concat_list(video, work / "video.ffconcat"))]
        maps = ["-map", "1:v:0", "-map", "0:a:0"]
    run(*args, *maps, "-c", "copy", "-f", "flv", str(dest))


def probe(path: Path) -> dict[str, dict]:
    """Per stream type: packet count and media span (FLV streams carry no duration of their own)."""
    out = run("ffprobe", "-v", "error", "-show_entries", "packet=codec_type,pts_time,duration_time", "-of", "csv=p=0",
              str(path))
    d: dict[str, dict] = {}
    for line in out.splitlines():
        f = line.split(",")
        if len(f) < 3 or f[1] in ("", "N/A"):
            continue
        t, dur = float(f[1]), float(f[2]) if f[2] not in ("", "N/A") else 0.0
        s = d.setdefault(f[0], {"packets": 0, "start": t, "end": t + dur})
        s["packets"] += 1
        s["start"], s["end"] = min(s["start"], t), max(s["end"], t + dur)
    for s in d.values():
        s["duration"] = s["end"] - s["start"]
    return d


def audio_packets(path: Path) -> list[tuple[float, str]]:
    """(pts, md5) of every audio packet: what the import really contains."""
    out = run("ffprobe", "-v", "error", "-select_streams", "a:0", "-show_data_hash", "MD5", "-show_entries",
              "packet=pts_time,data_hash", "-of", "csv=p=0", str(path))
    return [(float(a), b) for a, b in (line.split(",") for line in out.splitlines() if line.count(",") == 1)]


def publish(src: Path, final: Path, start: float, duration: float) -> Path:
    """Copy into the import dir under a non-media name, set mtime = last sample, rename in place."""
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = final.with_name(final.name + ".part")
    shutil.copyfile(src, staging)
    end = start + duration
    os.utime(staging, (end, end))
    os.replace(staging, final)
    return final


# ---- the whole import ----------------------------------------------------------------------------


@dataclass
class ImportResult:
    path: Path
    start: float  # recorder-frame wall time of the first audio sample (= name time)
    duration: float  # media length incl. join gaps (mtime = start + duration)
    video: str  # copy | reencode | none
    gaps: list[float]  # timestamp gaps inserted between audio pieces (s)
    notes: list[str]

    def to_dict(self) -> dict:
        return {"path": str(self.path), "start": round(self.start, 3), "duration": round(self.duration, 3),
                "video": self.video, "gaps": [round(g, 4) for g in self.gaps], "notes": self.notes}


def _monotonic(pieces: list[Piece], notes: list[str], what: str) -> list[Piece]:
    """No piece may start before the previous one ends (one FLV timeline); butt an overlap."""
    prev_end = None
    for i, p in enumerate(pieces):
        if prev_end is not None and p.offset < prev_end:
            if prev_end - p.offset > 0.025:  # more than a video frame
                notes.append(f"{what} piece {i} overlapped the previous by {(prev_end - p.offset) * 1000:.0f} ms")
            p.offset = prev_end
        prev_end = p.offset + p.length
    return pieces


def range_wall(tl: Timeline, place: Placement, g0: float, g1: float) -> float:
    """Wall time of global position g0 for a run continuing to g1. A run that starts with a kept
    leading repeat (output starting at a later page) is placed by its first packet past that
    repeat, so the one-join uncertainty falls on the 5 s repeat rather than the whole fragment."""
    g = g0
    for j in tl.joins:
        if j.cut <= g < j.cut + j.lag:
            g = float(j.cut + j.lag)
            break
    if g >= g1:
        g = g0
    return place.wall(tl, g) - (g - g0) * tl.pkt_s


def build_import(tl: Timeline, place: Placement, first: int, last: int, videos: dict[int, Path] | None,
                 work: Path, out_dir: Path, title: str, tz: ZoneInfo) -> ImportResult:
    """Pages first..last of a replay -> <out_dir>/<name>.flv, placed on the wall clock by `place`."""
    work.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    keep = tl.keep(first, last)
    walls = [range_wall(tl, place, tl.starts[r.part] + r.i0, tl.starts[r.part] + r.i1) for r in keep]
    out_start = walls[0]
    audio = []
    for n, (r, w) in enumerate(zip(keep, walls)):
        p = work / f"a{n:03d}.aac"
        audio_piece(tl.parts[r.part], r, p)
        audio.append(Piece(p, w - out_start, (r.i1 - r.i0) * tl.pkt_s))
    _monotonic(audio, notes, "audio")
    gaps = [b.offset - (a.offset + a.length) for a, b in zip(audio, audio[1:])]

    vpieces: list[Piece] | None = None
    mode = "none"
    if videos:
        packets = {k: video_packets(videos[k]) for k in range(first, last + 1)}
        plan, problems = video_plan(tl, packets, first, last)
        vpieces, mode = [], "copy"
        if not problems:
            try:
                for n, vr in enumerate(plan):
                    p = work / f"v{n:03d}.mp4"
                    video_piece_copy(videos[vr.part], vr, p)
                    g = tl.starts[vr.part] + vr.start / tl.pkt_s
                    w = range_wall(tl, place, g, tl.starts[vr.part] + vr.end / tl.pkt_s)
                    vpieces.append(Piece(p, w - out_start, vr.end - vr.start))
            except RuntimeError as e:
                problems.append(str(e))
        if problems:  # keyframes don't sit on the cuts: re-encode along the audio's own ranges
            notes += problems
            vpieces, mode = [], "reencode"
            for n, (r, a) in enumerate(zip(keep, audio)):
                p = work / f"r{n:03d}.mp4"
                video_piece_encode(videos[r.part], r.i0 * tl.pkt_s, a.length, p)
                vpieces.append(Piece(p, a.offset, a.length))
        v0 = vpieces[0].offset  # the concat demuxer starts the first piece at 0 (A/V within a frame)
        for p in vpieces:
            p.offset -= v0
        _monotonic(vpieces, notes, "video")

    flv = work / "import.flv"
    mux_flv(audio, vpieces, work, flv)
    expected = audio[-1].offset + audio[-1].length
    info = probe(flv)
    a, v = info.get("audio", {}), info.get("video", {})
    # ffmpeg can finish "successfully" after dropping a bad input; don't ship a short file.
    if a.get("packets") != sum(r.i1 - r.i0 for r in keep) or abs(a["duration"] - expected) > DURATION_TOL:
        raise RuntimeError(f"import audio has {a.get('packets')} packets / {a.get('duration', 0):.2f}s, "
                           f"expected {sum(r.i1 - r.i0 for r in keep)} / {expected:.2f}s")
    if vpieces and abs(v.get("duration", 0) - expected) > VIDEO_TOL:
        raise RuntimeError(f"import video is {v.get('duration', 0):.2f}s, expected {expected:.2f}s")
    final = out_dir / import_name(out_start, title, tz)
    publish(flv, final, out_start, expected)
    return ImportResult(final, out_start, expected, mode, gaps, notes)
