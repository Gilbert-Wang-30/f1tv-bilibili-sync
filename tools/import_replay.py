"""Turn a downloaded Bilibili 直播回放 into a file that looks like 录播姬 recorded it live.

Runs inside the fone image (needs ffmpeg):  python import_replay.py spec.json

spec.json:
{
  "out_dir": "/rec/胡闹P房",
  "title": "【胡闹P房】16点 巴林 排位赛",
  "content_start_unix": 1791010990.65,   # real time the first kept second was captured
  "arrival_latency_s": 2.0,              # how far behind that a live recorder receives it
  "keep": [                              # source ranges to keep, in order (seconds)
    {"video": "/w/p1_video.mp4", "audio": "/w/p1_audio.m4a", "in": 0, "out": 1805.12},
    ...
  ]
}

Why "keep": Bilibili builds replays from ~30-min fragments that each start by
repeating the previous fragment's last ~5 s, and splits long replays into parts.
A live recorder sees one continuous stream, so the duplicates are cut out and the
parts joined. Cut points should sit on video keyframes (the fragment joins do).

What makes the result look like a live 录播姬 recording, as far as fone can tell:
  * layout and name: <streamer>/<yyyyMMdd-HHmmss-fff Beijing>-<room title>.flv
  * FLV with the stream's H.264 + AAC, timestamps from 0
  * name time = when the recorder connected; mtime = when it wrote the last byte
It is written under a non-media name and renamed into place only after the mtime is
set, so fone never mistakes the half-copied file for a live recording.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SH = ZoneInfo("Asia/Shanghai")


def run(*cmd):
    subprocess.run(cmd, check=True)


def duration(path, kind):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", kind[0], "-show_entries",
                          "stream=duration:format=duration", "-of", "json", str(path)],
                         check=True, capture_output=True, text=True).stdout
    d = json.loads(out)
    s = d.get("streams") or [{}]
    return float(s[0].get("duration") or d["format"]["duration"])


SEEK_PAST = 0.02  # land on the keyframe at `in`, never the one before it (half a 25 fps frame)


def concat(tmp, keep, kind):
    """Stream-copy the kept ranges of one track into a single continuous file.

    Each range is cut to its own file first (seek lands exactly on the keyframe at
    `in`), then the pieces are joined; the concat demuxer's inpoint/outpoint lets
    B-frames bleed across a cut and breaks timestamp order.
    """
    ext = "mp4" if kind == "video" else "m4a"
    stream = "0:v:0" if kind == "video" else "0:a:0"
    pieces = []
    for i, k in enumerate(keep):
        piece = tmp / f"{kind}_{i}.{ext}"
        run("ffmpeg", "-v", "error", "-xerror", "-y", "-ss", f"{k['in'] + SEEK_PAST:.3f}", "-i", k[kind],
            "-t", f"{k['out'] - k['in']:.3f}", "-map", stream, "-c", "copy", "-avoid_negative_ts", "make_zero", str(piece))
        pieces.append(piece)
    lst = tmp / f"{kind}.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in pieces))
    out = tmp / f"{kind}.{ext}"
    run("ffmpeg", "-v", "error", "-xerror", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out))
    return out


def video_track(tmp, keep):
    """Frame-accurate video join. Bilibili re-encodes often lack keyframes at the
    fragment joins, so stream copy can't start a range exactly; decode each range
    (accurate seek), concatenate, re-encode."""
    args = []
    for k in keep:
        args += ["-ss", f"{k['in']:.3f}", "-t", f"{k['out'] - k['in']:.3f}", "-i", k["video"]]
    graph = "".join(f"[{i}:v:0]setpts=PTS-STARTPTS[v{i}];" for i in range(len(keep)))
    graph += "".join(f"[v{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=1:a=0[v]"
    out = tmp / "video.mp4"
    run("ffmpeg", "-v", "error", "-xerror", "-y", *args, "-filter_complex", graph, "-map", "[v]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "50", "-r", "25", "-pix_fmt", "yuv420p", str(out))
    return out


def main(spec_path):
    spec = json.loads(Path(spec_path).read_text())
    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=Path(spec_path).parent))

    audio = concat(tmp, spec["keep"], "audio")  # exact: every AAC packet is a sync point
    video = video_track(tmp, spec["keep"])
    flv = tmp / "rec.flv"
    run("ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
        "-c", "copy", "-f", "flv", str(flv))
    dur = duration(flv, "audio")
    expected = sum(k["out"] - k["in"] for k in spec["keep"])
    vdur = duration(flv, "video")
    # ffmpeg can finish "successfully" after dropping a bad input; don't ship a short file.
    if abs(dur - expected) > 0.5 or abs(vdur - expected) > 1.0:
        sys.exit(f"duration check failed: audio {dur:.2f}s, video {vdur:.2f}s, expected {expected:.2f}s")

    arrive = spec["content_start_unix"] + spec["arrival_latency_s"]
    stamp = datetime.fromtimestamp(arrive, SH).strftime("%Y%m%d-%H%M%S-") + f"{int(round(arrive * 1000)) % 1000:03d}"
    final = out_dir / f"{stamp}-{spec['title']}.flv"
    staging = final.with_name(final.name + ".part")  # not a media extension: fone ignores it
    run("cp", str(flv), str(staging))
    end = arrive + dur
    os.utime(staging, (end, end))
    os.rename(staging, final)
    print(json.dumps({"file": str(final), "audio_duration": dur, "expected_duration": expected,
                      "wall_start": arrive, "mtime": end, "bytes": final.stat().st_size}, ensure_ascii=False))

    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()


if __name__ == "__main__":
    main(sys.argv[1])
