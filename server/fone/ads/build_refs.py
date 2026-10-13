#!/usr/bin/env python3
"""Build refs.npz, the sponsor clips' fingerprints, from their WAV files.

  python fone/ads/build_refs.py CLIPS_DIR [-o fone/ads/refs.npz]       (from server/)
  docker run --rm -v $PWD/server:/app -w /app -v /path/to/clips:/clips fone:dev \
      python fone/ads/build_refs.py /clips

Every *.wav in CLIPS_DIR becomes one reference clip named after its file: 8 kHz 16-bit
(mono, or stereo mixed down), cut at the clip's audible edges (the streamer's mic is
gated while a clip plays), e.g.  ffmpeg -i clip.m4a -ac 1 -ar 8000 clip_name.wav
The audio never leaves CLIPS_DIR: refs.npz holds landmark hashes only (format in
fone/ads_detect.py). ads.json next to the output is refreshed to describe the clips
(descriptions of clips it already lists are kept). The server reads refs.npz when it
starts; its version changes with the clips, so every recording is analysed again.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # server/, for `fone`

from fone.ads_detect import SR, read_wav8, write_refs  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("clips", type=Path, help="directory of 8 kHz 16-bit WAV files, one per clip")
    ap.add_argument("-o", "--out", type=Path, default=Path(__file__).with_name("refs.npz"))
    args = ap.parse_args()
    wavs = sorted(args.clips.glob("*.wav"))
    if not wavs:
        sys.exit(f"no *.wav in {args.clips}")
    clips = [(w.stem, read_wav8(w)) for w in wavs]
    write_refs(args.out, clips)

    manifest = args.out.with_name("ads.json")
    try:
        doc = json.loads(manifest.read_text())
    except (OSError, ValueError):
        doc = {"note": "What refs.npz holds, for people; the server reads refs.npz (see build_refs.py)."}
    known = {r["name"]: r for r in doc.get("refs", [])}
    doc["refs"] = [{**known.get(name, {"description": f"sponsor clip {name}"}),
                    "name": name, "duration": round(len(x) / SR, 2)} for name, x in clips]
    manifest.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    for name, x in clips:
        print(f"{name}: {len(x) / SR:.2f} s")
    print(f"{len(clips)} clip(s) -> {args.out} ({args.out.stat().st_size} bytes); {manifest} refreshed")


if __name__ == "__main__":
    main()
