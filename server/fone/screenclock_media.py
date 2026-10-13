"""Reading MultiViewer's "Track time" clock out of a recording's video (FLV).

Frames come from our own incremental FLV tag index rather than ffmpeg seeking: the
recorder writes no keyframe index (so `ffmpeg -ss` demuxes from the start of the
file) and a file that is still being written can't be seeked at all. Instead we
pipe ffmpeg a tiny FLV made of the header, the codec sequence header and only the
tags we want: one keyframe per sample (a 3 h recording at one sample per 30 s is
~70 MB read, ~1 s of decoding), or ~1.3 s of consecutive video for tick timing.

OCR is the tesseract CLI (`apk add tesseract-ocr tesseract-ocr-data-eng`). The
clock is found by full-frame OCR of the words "Track time"; the value box sits
under the label, so layout, window position and scale may change freely. Once a
layout is known, a frame only costs a label-template compare + one small OCR.

Timebase: everything here is FLV presentation time in seconds (`pts`), the same
clock as the audio packets. fone's audio HLS media time is `m = pts - a0`, with
`a0` the first coded audio frame (`FlvIndex.a0`).
"""

from __future__ import annotations

import bisect
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import numpy as np

log = logging.getLogger("fone.screenclock")

TAG_AUDIO, TAG_VIDEO = 8, 9
# Video-only FLV header + PreviousTagSize0: our synthetic streams carry no audio.
FLV_VIDEO_HEADER = b"FLV\x01\x01\x00\x00\x00\x09\x00\x00\x00\x00"
SHOWINFO_PTS = re.compile(rb"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:([-0-9.e]+)")
# Background work: never compete with the live audio extraction.
NICE = ["nice", "-n", "10"] if shutil.which("nice") else []
TESS_ENV = dict(os.environ, OMP_THREAD_LIMIT="1")
MS_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
# Label crop vs its template (mean abs difference, 0-255): above this the clock moved
# or is covered, and the frame goes back to full-frame search.
LABEL_MATCH_MAD = 28


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


# ---- FLV index ---------------------------------------------------------------


@dataclass
class FlvIndex:
    """Keyframe index of an FLV that may still be growing.

    Reads only tag headers (plus 2 payload bytes); `update()` resumes where it
    stopped and never consumes a tag that isn't completely on disk yet.
    """

    path: Path
    pos: int = 0  # end of the last complete tag
    size: int = 0  # file size at the last update
    vheaders: list[tuple[int, bytes]] = field(default_factory=list)  # (pos, raw tag) video sequence headers
    kf_ms: list[int] = field(default_factory=list)  # keyframe DTS, ms
    kf_pos: list[int] = field(default_factory=list)
    kf_len: list[int] = field(default_factory=list)  # whole tag incl. trailing PreviousTagSize
    kf_hdr: list[int] = field(default_factory=list)  # index into vheaders in force
    a0_ms: int | None = None  # first coded audio frame
    last_ms: int = 0  # largest timestamp seen

    @property
    def a0(self) -> float | None:
        return None if self.a0_ms is None else self.a0_ms / 1000

    @property
    def end(self) -> float:
        """Media time (s) up to which complete tags are on disk."""
        return self.last_ms / 1000

    def reset(self) -> None:
        self.__init__(self.path)  # type: ignore[misc]

    def update(self) -> int:
        """Index tags appended since the last call; returns how many were read."""
        n = 0
        with open(self.path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size < self.size:  # truncated / replaced: start over
                self.reset()
            self.size = size
            if self.pos == 0:
                head = f.read(13)
                if len(head) < 13:
                    return 0
                if head[:3] != b"FLV":
                    raise ValueError("not an FLV file")
                self.pos = int.from_bytes(head[5:9], "big") + 4
            pos = self.pos
            while pos + 13 <= size:
                f.seek(pos)
                th = f.read(13)  # tag header + first 2 payload bytes
                dsize = int.from_bytes(th[1:4], "big")
                end = pos + 11 + dsize + 4
                if end > size:
                    break  # still being written
                ttype = th[0] & 0x1F
                ts = int.from_bytes(th[4:7], "big") | (th[7] << 24)
                if ttype == TAG_VIDEO and dsize >= 2:
                    self._video(f, pos, end, th, ts)
                elif ttype == TAG_AUDIO and dsize >= 2 and self.a0_ms is None \
                        and not ((th[11] >> 4) == 10 and th[12] == 0):  # not the AAC sequence header
                    self.a0_ms = ts
                self.last_ms = max(self.last_ms, ts)
                pos = end
                n += 1
            self.pos = pos
        return n

    def _video(self, f: BinaryIO, pos: int, end: int, th: bytes, ts: int) -> None:
        b0 = th[11]
        if b0 & 0x80:  # enhanced FLV: IsExHeader | FrameType(3) | PacketType(4) | FourCC
            frame_type, seq, coded = (b0 >> 4) & 7, (b0 & 0x0F) == 0, (b0 & 0x0F) in (1, 3)
        elif (b0 & 0x0F) in (7, 12):  # legacy AVC / HEVC: FrameType(4) | CodecID(4), PacketType
            frame_type, seq, coded = b0 >> 4, th[12] == 0, th[12] == 1
        else:
            return
        if seq:
            f.seek(pos)
            self.vheaders.append((pos, f.read(end - pos)))
        elif coded and frame_type == 1 and self.vheaders:
            self.kf_ms.append(ts)
            self.kf_pos.append(pos)
            self.kf_len.append(end - pos)
            self.kf_hdr.append(len(self.vheaders) - 1)

    # ---- picking frames -------------------------------------------------

    def pick(self, after: float | None, step: float, until: float | None = None) -> list[int]:
        """Keyframe indices one per `step` seconds, strictly after `after` (s, DTS)."""
        out: list[int] = []
        nxt = -1e18 if after is None else after + step - 0.01
        first = bisect.bisect_left(self.kf_ms, int(nxt * 1000)) if after is not None else 0
        for i in range(first, len(self.kf_ms)):
            t = self.kf_ms[i] / 1000
            if until is not None and t > until:
                break
            if t >= nxt:
                out.append(i)
                nxt = t + step - 0.01
        return out

    def keyframe_stream(self, idxs: list[int]) -> Callable[[BinaryIO], None]:
        """Writer of a video-only FLV holding just these keyframes (each one an IDR)."""

        def write(out: BinaryIO) -> None:
            out.write(FLV_VIDEO_HEADER)
            hdr = -1
            with open(self.path, "rb") as f:
                for i in idxs:
                    if self.kf_hdr[i] != hdr:  # resolution / codec change mid-file
                        hdr = self.kf_hdr[i]
                        out.write(self.vheaders[hdr][1])
                    f.seek(self.kf_pos[i])
                    out.write(f.read(self.kf_len[i]))

        return write

    def window_stream(self, i: int, seconds: float) -> Callable[[BinaryIO], None]:
        """Writer of a video-only FLV: every video tag from keyframe i for `seconds`."""

        def write(out: BinaryIO) -> None:
            out.write(FLV_VIDEO_HEADER)
            out.write(self.vheaders[self.kf_hdr[i]][1])
            stop_ms = self.kf_ms[i] + seconds * 1000
            with open(self.path, "rb") as f:
                pos = self.kf_pos[i]
                while pos + 11 <= self.pos:
                    f.seek(pos)
                    th = f.read(11)
                    dsize = int.from_bytes(th[1:4], "big")
                    ts = int.from_bytes(th[4:7], "big") | (th[7] << 24)
                    end = pos + 11 + dsize + 4
                    if th[0] & 0x1F == TAG_VIDEO:
                        if ts > stop_ms:
                            break
                        f.seek(pos)
                        out.write(f.read(end - pos))
                    pos = end

        return write


# ---- decoding ------------------------------------------------------------------


@dataclass
class Frame:
    pts: float
    img: np.ndarray  # uint8 gray, shape (h, w)

    @property
    def w(self) -> int:
        return self.img.shape[1]

    @property
    def h(self) -> int:
        return self.img.shape[0]


def _read_pgm(f: BinaryIO) -> np.ndarray | None:
    """One binary PGM from a stream ("P5\\nW H\\n255\\n" + pixels); None at EOF."""
    tokens: list[bytes] = []
    tok = b""
    while len(tokens) < 4:
        c = f.read(1)
        if not c:
            return None
        if c.isspace():
            if tok:
                tokens.append(tok)
                tok = b""
        else:
            tok += c
    if tokens[0] != b"P5":
        raise ValueError(f"unexpected image header {tokens!r}")
    w, h = int(tokens[1]), int(tokens[2])
    buf = f.read(w * h)
    if len(buf) < w * h:
        return None
    return np.frombuffer(buf, np.uint8).reshape(h, w)


def decode(feed: Callable[[BinaryIO], None], vf: str = "") -> Iterator[Frame]:
    """Decode an FLV produced by `feed` into gray frames with their pts.

    PGM output is self-describing, so a resolution change mid-stream is harmless.
    `vf` is prepended to the filter chain (e.g. a crop)."""
    cmd = NICE + [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-threads", "2", "-copyts",
        "-f", "flv", "-i", "pipe:0", "-an", "-sn", "-dn",
        "-vf", f"{vf + ',' if vf else ''}showinfo,format=gray", "-fps_mode", "passthrough",
        "-f", "image2pipe", "-c:v", "pgm", "-",
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pts_q: queue.Queue[float | None] = queue.Queue()

    def pump_in() -> None:
        try:
            feed(proc.stdin)
        except (BrokenPipeError, OSError) as e:  # ffmpeg died; its stderr says why
            log.debug("feeding ffmpeg stopped: %s", e)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def pump_err() -> None:
        tail = []
        for line in proc.stderr:
            if m := SHOWINFO_PTS.search(line):
                pts_q.put(float(m.group(1)))
            elif b"rror" in line:
                tail = (tail + [line.strip()])[-3:]
        if tail:
            log.debug("ffmpeg: %s", b" | ".join(tail).decode(errors="replace"))
        pts_q.put(None)

    threads = [threading.Thread(target=pump_in, daemon=True), threading.Thread(target=pump_err, daemon=True)]
    for t in threads:
        t.start()
    try:
        while (img := _read_pgm(proc.stdout)) is not None:
            pts = pts_q.get(timeout=60)
            if pts is None:
                break
            yield Frame(pts, img)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for t in threads:
            t.join(timeout=5)


# ---- OCR primitives ----------------------------------------------------------------


Box = tuple[int, int, int, int]  # x0, y0, x1, y1


def crop(img: np.ndarray, box: Box) -> np.ndarray:
    x0, y0, x1, y1 = box
    return img[y0:y1, x0:x1]


def mad(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape or a.size == 0:
        return 255.0
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def upscale(img: np.ndarray, s: int) -> np.ndarray:
    """Bilinear upscale by an integer factor, contrast-stretched, dark text on light.

    Tesseract reads ~10 px digits far better after this; the exact interpolation
    (pixel-centre aligned, edge clamped) is the one the prototype was tuned with."""
    h, w = img.shape
    lo, hi = int(img.min()), int(img.max())
    lut = np.clip((np.arange(256) - lo) * 255 // max(hi - lo, 1), 0, 255)
    if np.concatenate([img[0], img[-1]]).mean() < 128:  # light text on a dark header
        lut = 255 - lut

    def axis(n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        f = (np.arange(n * s) + 0.5) / s - 0.5
        i0 = np.floor(f)
        return np.clip(i0, 0, n - 1).astype(int), np.clip(i0 + 1, 0, n - 1).astype(int), f - i0

    xa, xb, ax = axis(w)
    ya, yb, ay = axis(h)
    v = img.astype(np.float64)
    top = v[ya][:, xa] + (v[ya][:, xb] - v[ya][:, xa]) * ax
    bot = v[yb][:, xa] + (v[yb][:, xb] - v[yb][:, xa]) * ax
    out = top + (bot - top) * ay[:, None]
    return lut[np.floor(out + 0.5).astype(int)].astype(np.uint8)


def pgm(img: np.ndarray) -> bytes:
    return b"P5\n%d %d\n255\n" % (img.shape[1], img.shape[0]) + np.ascontiguousarray(img).tobytes()


def tesseract(image: bytes, *args: str) -> str:
    r = subprocess.run(NICE + ["tesseract", "stdin", "stdout", *args], input=image, capture_output=True,
                       env=TESS_ENV, check=False)
    return r.stdout.decode("utf-8", "replace")


def parse_hms(text: str) -> int | None:
    """"HH:MM:SS" (or the colons lost: "HHMMSS") -> seconds of day."""
    t = text.replace(" ", "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", t) or re.fullmatch(r"(\d{2})(\d{2})(\d{2})", t)
    if not m:
        return None
    hh, mm, ss = map(int, m.groups())
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return hh * 3600 + mm * 60 + ss


def read_hms(img: np.ndarray, box: Box, scale: int) -> tuple[int | None, str]:
    """OCR an HH:MM:SS box; on a parse failure retry at neighbouring upscale factors
    (cheap insurance for small text, e.g. 720p streams)."""
    txt = ""
    for sc in dict.fromkeys((scale, scale - 1, scale + 1, scale - 2)):
        if sc < 2:
            continue
        big = upscale(crop(img, box), sc)
        txt = tesseract(pgm(big), "--psm", "7", "-c", "tessedit_char_whitelist=0123456789:").strip()
        if (v := parse_hms(txt)) is not None:
            return v, txt
    return None, txt


# ---- finding the clock -------------------------------------------------------------


@dataclass
class Layout:
    label: Box  # the words "Track time"
    value: Box  # HH:MM:SS under it
    scale: int  # upscale factor that makes the digits ~40 px tall
    template: np.ndarray  # label pixels when found
    hits: int = 0
    id: int = 0


def _words(tsv: str) -> list[dict]:
    words = []
    for line in tsv.splitlines()[1:]:
        p = line.split("\t")
        if len(p) < 12 or p[0] != "5" or not p[11].strip():
            continue
        words.append({"line": (p[2], p[3], p[4]), "n": int(p[5]), "x": int(p[6]), "y": int(p[7]),
                      "w": int(p[8]), "h": int(p[9]), "text": p[11].strip()})
    return words


def frame_words(img: np.ndarray) -> list[dict]:
    """Word boxes of a whole frame. Frames under ~1000 lines are upscaled 2x first:
    MultiViewer's header text is ~10 px tall at 1080p and tesseract misses it at 720p."""
    h, w = img.shape
    if h >= 1000:
        return _words(tesseract(pgm(img), "--psm", "11", "tsv"))
    big = subprocess.run(
        NICE + ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-i", "-",
                "-vf", f"scale={w * 2}:{h * 2}:flags=bicubic", "-f", "image2pipe", "-c:v", "pgm", "-"],
        input=np.ascontiguousarray(img).tobytes(), capture_output=True, check=False).stdout
    words = _words(tesseract(big, "--psm", "11", "tsv"))
    for wd in words:
        for k in ("x", "y", "w", "h"):
            wd[k] //= 2
    return words


def tighten(img: np.ndarray, box: Box, contrast: int = 60) -> Box:
    """Shrink a box to the pixels that differ clearly from its background. Tesseract
    now and then reports a word box several times taller than the text, and the
    value box is derived from the label's height."""
    x0, y0, x1, y1 = box
    sub = img[y0:y1, x0:x1].astype(np.int16)
    if sub.size == 0:
        return box
    ink = np.abs(sub - int(np.median(sub))) > contrast
    rows, cols = np.flatnonzero(ink.any(axis=1)), np.flatnonzero(ink.any(axis=0))
    if rows.size == 0:
        return box
    return x0 + int(cols[0]), y0 + int(rows[0]), x0 + int(cols[-1]) + 1, y0 + int(rows[-1]) + 1


def find_layouts(img: np.ndarray) -> list[Layout]:
    """Every "Track time" label on the frame, with the value box derived from it."""
    h, w = img.shape
    words = frame_words(img)
    found = []
    for wd in words:
        t = wd["text"].lower()
        if t.startswith("tracktime"):
            lab = (wd["x"], wd["y"], wd["x"] + wd["w"], wd["y"] + wd["h"])
        elif t == "track":
            nxt = next((v for v in words if v["line"] == wd["line"] and v["n"] == wd["n"] + 1), None)
            if not nxt or not nxt["text"].lower().startswith("tim") or nxt["x"] - (wd["x"] + wd["w"]) > wd["h"] * 1.5:
                continue
            lab = (wd["x"], min(wd["y"], nxt["y"]), nxt["x"] + nxt["w"], max(wd["y"] + wd["h"], nxt["y"] + nxt["h"]))
        else:
            continue
        lab = tighten(img, lab)
        lx0, ly0, lx1, ly1 = lab
        lh, lw = max(ly1 - ly0, 6), lx1 - lx0
        # The value sits right under the label; "HH:MM:SS" is ~0.9x the label width.
        right = lx0 + int(1.15 * lw)
        for v in words:  # don't run into the next header column (e.g. "Wind")
            if abs(v["y"] - ly0) < lh and lx1 < v["x"] < right + 2:
                right = min(right, v["x"] - 2)
        value = (max(lx0 - int(0.4 * lh), 0), min(ly0 + int(1.2 * lh), h - 1),
                 min(right, w), min(ly0 + int(3.5 * lh), h))
        scale = max(2, min(6, round(40 / lh)))
        found.append(Layout(label=lab, value=value, scale=scale, template=crop(img, lab).copy()))
    return found


@dataclass
class Reading:
    pts: float
    tod: int | None = None  # Track time shown, seconds of day
    visible: bool = False  # the label was on screen
    searched: bool = False  # full-frame search ran (or a known layout matched)
    layout: int | None = None


class Reader:
    """Reads the clock off frames, remembering where it was found. Thread-safe."""

    def __init__(self) -> None:
        self.layouts: list[Layout] = []
        self._lock = threading.Lock()
        self.full_searches = 0

    def read(self, fr: Frame, may_search: bool) -> Reading:
        r = Reading(fr.pts)
        with self._lock:
            known = sorted(self.layouts, key=lambda L: -L.hits)
        for L in known:
            if mad(crop(fr.img, L.label), L.template) > LABEL_MATCH_MAD:
                continue
            r.visible = r.searched = True
            tod, _ = read_hms(fr.img, L.value, L.scale)
            if tod is not None:
                return self._hit(r, L, tod)
        if not may_search:  # full-frame OCR is ~1 CPU-s; it is throttled while nothing matches
            return r
        r.searched = True
        with self._lock:
            self.full_searches += 1
        for L in find_layouts(fr.img):
            r.visible = True
            tod, _ = read_hms(fr.img, L.value, L.scale)
            if tod is None:
                continue
            with self._lock:  # the same clock found again (label edges vary by a pixel)
                same = next((K for K in self.layouts
                             if all(abs(a - b) <= 3 for a, b in zip(K.label, L.label, strict=True))), None)
                if same is None:
                    L.id = len(self.layouts)
                    self.layouts.append(L)
                    same = L
            return self._hit(r, same, tod)
        return r

    def _hit(self, r: Reading, L: Layout, tod: int) -> Reading:
        with self._lock:
            L.hits += 1
        r.tod, r.layout = tod, L.id
        return r


# ---- sub-second: when does the seconds digit change? -------------------------------


@dataclass
class Tick:
    pts: float  # midway between the last frame showing `value - 1` and the first showing `value`
    value: int  # seconds of day shown from here on


def find_ticks(index: FlvIndex, kf: int, layout: Layout, seconds: float = 1.3) -> list[Tick]:
    """Decode `seconds` of video from keyframe `kf` (value box only), find the frames
    where the seconds digit changes and confirm each change by OCR: the value two
    frames before must be exactly one second before the value one frame after."""
    x0, y0, x1, y1 = layout.value
    frames = list(decode(index.window_stream(kf, seconds), vf=f"crop={x1 - x0}:{y1 - y0}:{x0}:{y0}"))
    n = len(frames)
    if n < 4:
        return []
    bw, bh = x1 - x0, y1 - y0
    digit = (int(bw * 0.70), 0, bw, bh)  # the last two digits
    rel = (0, 0, bw, bh)
    seq = [crop(f.img, digit) for f in frames]
    diffs = [mad(seq[i], seq[i - 1]) for i in range(1, n)]
    thr = max(1.5, 5 * float(np.median(diffs)) + 0.5)
    out = []
    j = 1
    while j < n:
        if diffs[j - 1] <= thr:
            j += 1
            continue
        before, _ = read_hms(frames[max(j - 2, 0)].img, rel, layout.scale)
        after, _ = read_hms(frames[min(j + 1, n - 1)].img, rel, layout.scale)
        if before is not None and after is not None and (after - before - 1) % 86400 == 0:
            out.append(Tick((frames[j - 1].pts + frames[j].pts) / 2, after))
        j += 3
    return out
