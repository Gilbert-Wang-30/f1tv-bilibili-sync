"""FLV keyframe index: what video.py needs to cut a recording into HLS segments.

One pass over the tags records every keyframe's DTS and byte offset plus the codec
sequence headers. A segment is then just the bytes between two keyframes, prefixed
with the FLV header and the sequence headers in force there, which ffmpeg remuxes to
MPEG-TS with -c copy: no re-encode, no per-viewer process, seek anywhere.

Tail-friendly: update() resumes at the end of the last complete tag and never consumes
a tag whose payload (and trailing PreviousTagSize) isn't fully on disk, so it works on
a file the recorder is still writing. Big sequential reads, parsed in memory: on the NAS
that costs one streaming read of the file (~6 s for a 3 h, 1.6 GB 1080p recording).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path

TAG_AUDIO, TAG_VIDEO, TAG_SCRIPT = 8, 9, 18
READ_CHUNK = 4 << 20
MAX_TAG = 16 << 20  # anything bigger is corruption, not a frame
# Legacy FLV video codec ids we can cut: AVC, and HEVC under the id Chinese CDNs use.
AVC_LIKE = {7, 12}
EX_HEADER = 0x80  # enhanced FLV (E-RTMP): FourCC codecs, packet type in the low nibble
EX_SEQUENCE_START, EX_CODED_FRAMES, EX_CODED_FRAMES_X = 0, 1, 3
FRAME_KEY, FRAME_COMMAND = 1, 5
SOUND_AAC, SOUND_EX_HEADER = 10, 9


@dataclass(frozen=True)
class Segment:
    a: int  # keyframe index where it starts
    b: int | None  # keyframe index where the next one starts; None = to the end of the file
    start: float  # playlist time (seconds from the first keyframe)
    duration: float

    @property
    def name(self) -> str:
        return f"{self.a}_{'end' if self.b is None else self.b}.ts"


@dataclass(frozen=True)
class Snapshot:
    """Immutable view of an index, safe to read while the next update() runs in a thread."""

    header: bytes
    video_headers: tuple[tuple[int, bytes], ...]
    audio_headers: tuple[tuple[int, bytes], ...]
    kf_ts: tuple[int, ...]
    kf_pos: tuple[int, ...]
    first_audio_ts: int | None
    first_video_ts: int | None
    last_video_ts: int | None
    video_frames: int
    last_ts: int
    pos: int

    def headers_at(self, pos: int) -> bytes:
        """The video + audio sequence-header tags in force at byte `pos`."""
        out = b""
        for found in (self.video_headers, self.audio_headers):
            raw = next((r for p, r in reversed(found) if p < pos), found[0][1] if found else b"")
            out += raw
        return out

    def frame_ms(self) -> float:
        if self.video_frames > 1 and self.last_video_ts is not None and self.first_video_ts is not None:
            return max(1.0, (self.last_video_ts - self.first_video_ts) / (self.video_frames - 1))
        return 40.0

    def segments(self, target_ms: int, finished: bool) -> list[Segment]:
        """Cut at the first keyframe at least target_ms after the segment start.

        Greedy from the first keyframe, so the cut points depend only on earlier
        keyframes: a growing file's playlist only ever gains segments (EVENT playlists
        must be append-only). A growing file lists only segments whose closing keyframe
        is on disk; a finished one adds the tail up to the last frame.
        """
        kf = self.kf_ts
        if not kf:
            return []
        out: list[Segment] = []
        a = 0
        for i in range(1, len(kf)):
            if kf[i] - kf[a] >= target_ms:
                out.append(Segment(a, i, (kf[a] - kf[0]) / 1000, (kf[i] - kf[a]) / 1000))
                a = i
        if finished:
            last = max(self.last_video_ts if self.last_video_ts is not None else kf[a], kf[a])
            out.append(Segment(a, None, (kf[a] - kf[0]) / 1000, (last - kf[a] + self.frame_ms()) / 1000))
        return out


@dataclass
class FlvIndex:
    path: Path
    header: bytes = b""  # FLV header + PreviousTagSize0
    video_headers: list[tuple[int, bytes]] = field(default_factory=list)  # (byte pos, raw tag)
    audio_headers: list[tuple[int, bytes]] = field(default_factory=list)
    kf_ts: list[int] = field(default_factory=list)  # keyframe DTS, ms
    kf_pos: list[int] = field(default_factory=list)  # byte offset of the keyframe's tag
    first_audio_ts: int | None = None  # first coded audio frame: the audio HLS's m = 0
    first_video_ts: int | None = None
    last_video_ts: int | None = None
    video_frames: int = 0
    last_ts: int = 0
    pos: int = 0  # end of the last complete tag
    corrupt_at: int | None = None  # a broken tag stops the index here (never past it)
    scanned: int = 0  # read position during update(), for progress reports only

    def update(self) -> int:
        """Index the complete tags appended since the last call. Returns how many."""
        if self.corrupt_at is not None:
            return 0
        with open(self.path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size < self.pos:
                raise ValueError("recording shrank: not the file we indexed")
            if self.pos == 0:
                head = f.read(9)
                if len(head) < 9:
                    return 0
                if head[:3] != b"FLV":
                    raise ValueError("not an FLV file")
                hlen = int.from_bytes(head[5:9], "big")
                if size < hlen + 4:
                    return 0
                f.seek(0)
                self.header = f.read(hlen + 4)
                self.pos = hlen + 4
            f.seek(self.pos)
            buf = b""
            base = self.pos  # file offset of buf[0]
            off = 0
            new = 0
            while True:
                if len(buf) - off < 11:
                    more = f.read(READ_CHUNK)
                    if not more:
                        break
                    buf, base, off = buf[off:] + more, base + off, 0
                    self.scanned = base
                    continue
                ttype = buf[off] & 0x1F
                dsize = int.from_bytes(buf[off + 1: off + 4], "big")
                end = off + 11 + dsize + 4
                if ttype not in (TAG_AUDIO, TAG_VIDEO, TAG_SCRIPT) or dsize > MAX_TAG:
                    self.corrupt_at = base + off
                    break
                if end > len(buf):
                    if base + end > size:
                        break  # the recorder hasn't finished writing this tag
                    more = f.read(max(READ_CHUNK, end - len(buf)))
                    if not more:
                        break
                    buf, base, off = buf[off:] + more, base + off, 0
                    continue
                if int.from_bytes(buf[end - 4: end], "big") != 11 + dsize:
                    self.corrupt_at = base + off
                    break
                ts = int.from_bytes(buf[off + 4: off + 7], "big") | (buf[off + 7] << 24)
                self._tag(ttype, ts, buf, off, dsize, base + off, end)
                off = end
                new += 1
            self.pos = self.scanned = base + off
        return new

    def _tag(self, ttype: int, ts: int, buf: bytes, off: int, dsize: int, pos: int, end: int) -> None:
        p = off + 11
        if ttype == TAG_VIDEO and dsize >= 2:
            b0 = buf[p]
            if b0 & EX_HEADER:
                ftype, pkt = (b0 >> 4) & 7, b0 & 0x0F
                header, frame = pkt == EX_SEQUENCE_START, pkt in (EX_CODED_FRAMES, EX_CODED_FRAMES_X)
            else:
                ftype, codec = b0 >> 4, b0 & 0x0F
                if codec in AVC_LIKE:
                    header, frame = buf[p + 1] == 0, buf[p + 1] == 1
                else:
                    header, frame = False, True  # older codecs: every tag is a frame
            if ftype == FRAME_COMMAND:
                return
            if header:
                self.video_headers.append((pos, bytes(buf[off:end])))
            elif frame:
                if self.first_video_ts is None:
                    self.first_video_ts = ts
                self.last_video_ts = ts
                self.video_frames += 1
                if ftype == FRAME_KEY:
                    self.kf_ts.append(ts)
                    self.kf_pos.append(pos)
            self.last_ts = max(self.last_ts, ts)
        elif ttype == TAG_AUDIO and dsize >= 1:
            fmt = buf[p] >> 4
            if fmt == SOUND_EX_HEADER:
                header = (buf[p] & 0x0F) == EX_SEQUENCE_START
            else:
                header = fmt == SOUND_AAC and dsize >= 2 and buf[p + 1] == 0
            if header:
                self.audio_headers.append((pos, bytes(buf[off:end])))
                return
            if self.first_audio_ts is None:
                self.first_audio_ts = ts
            self.last_ts = max(self.last_ts, ts)

    def snapshot(self) -> Snapshot:
        return Snapshot(
            header=self.header,
            video_headers=tuple(self.video_headers),
            audio_headers=tuple(self.audio_headers),
            kf_ts=tuple(self.kf_ts),
            kf_pos=tuple(self.kf_pos),
            first_audio_ts=self.first_audio_ts,
            first_video_ts=self.first_video_ts,
            last_video_ts=self.last_video_ts,
            video_frames=self.video_frames,
            last_ts=self.last_ts,
            pos=self.pos,
        )

    # ---- persistence: a finished 3 h file shouldn't be re-read after every restart ----

    def to_json(self) -> dict:
        b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
        return {
            "header": b64(self.header),
            "video_headers": [[p, b64(r)] for p, r in self.video_headers],
            "audio_headers": [[p, b64(r)] for p, r in self.audio_headers],
            "kf_ts": self.kf_ts, "kf_pos": self.kf_pos,
            "first_audio_ts": self.first_audio_ts, "first_video_ts": self.first_video_ts,
            "last_video_ts": self.last_video_ts, "video_frames": self.video_frames,
            "last_ts": self.last_ts, "pos": self.pos, "corrupt_at": self.corrupt_at,
        }

    @classmethod
    def from_json(cls, path: Path, d: dict) -> FlvIndex:
        unb64 = base64.b64decode
        return cls(
            path=path,
            header=unb64(d["header"]),
            video_headers=[(p, unb64(r)) for p, r in d["video_headers"]],
            audio_headers=[(p, unb64(r)) for p, r in d["audio_headers"]],
            kf_ts=list(d["kf_ts"]), kf_pos=list(d["kf_pos"]),
            first_audio_ts=d["first_audio_ts"], first_video_ts=d["first_video_ts"],
            last_video_ts=d["last_video_ts"], video_frames=d["video_frames"],
            last_ts=d["last_ts"], pos=d["pos"], corrupt_at=d.get("corrupt_at"),
        )

    def still_matches(self) -> bool:
        """Is the file on disk still the one this (restored) index describes? Same header
        bytes, and the newest keyframe still sits where we saw it."""
        try:
            with open(self.path, "rb") as f:
                if os.fstat(f.fileno()).st_size < self.pos or f.read(len(self.header)) != self.header:
                    return False
                if self.kf_pos:
                    f.seek(self.kf_pos[-1])
                    th = f.read(11)
                    ts = int.from_bytes(th[4:7], "big") | (th[7] << 24) if len(th) == 11 else -1
                    return (th[0] & 0x1F) == TAG_VIDEO and ts == self.kf_ts[-1]
        except OSError:
            return False
        return True
