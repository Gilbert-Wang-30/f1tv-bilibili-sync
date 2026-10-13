import json
import os
import struct
import subprocess
from pathlib import Path

import numpy as np
import pytest

from fone.backfill_dedup import JoinFinder, Timeline, load_part, find_joins
from fone.backfill_start import (ARRIVAL_LATENCY, JOIN_GAP_S, SR, AlignIndex, align, danmaku_origin, decode_8k,
                                 estimate_start, from_t0, parse_dmks_live, unpackb)
from test_backfill_synth import synth  # noqa: F401  (fixture)

FIX = Path(__file__).parent / "fixtures" / "backfill"
REPLAY = Path(os.environ.get("FONE_TEST_KL_REPLAY", ""))  # the KL replay download (p1/p2_audio.m4a); unset: skipped
KL_FLV = Path("/mnt/nas/media/library/recordings/fone/胡闹P房/20261003-150312-650-【胡闹P房】16点 巴林 排位赛.flv")
KL_W0 = 1791010992.737  # fone's wall clock of that recording's media 0


def test_cascade_on_recorded_sessions():
    """Ten 2026 replays whose capture start is known from the MultiViewer clock on screen."""
    rows = json.loads((FIX / "start_anchors.json").read_text())["rows"]
    methods = {}
    for r in rows:
        est = estimate_start(1, live_time=r["live_time"], s0=r["s0"])
        if r["live_time"] is None and r["s0"] is None:
            assert est is None
            continue
        err = est.t0 - ARRIVAL_LATENCY - r["t0"]
        assert abs(err) <= est.pm + 0.05, (r["tag"], err, est)
        methods[r["tag"]] = est.method
    # ES FP2: danmakus' startDate was 3.5 s off (merged lives); S0 overrides it.
    assert methods["ESFP2_1500"] == "S0" and methods["KL_3700"] == "live_time+S0"
    assert methods["FP2_2400"] == "live_time" and methods["AZQ_1200"] == "S0"


def test_cascade_prefers_alignment_and_spreads_join_losses():
    live = estimate_start(4, live_time=1791010991)
    assert live.method == "live_time"
    assert abs(live.t0 - (1791010991 - 0.395 + ARRIVAL_LATENCY)) < 1e-6
    assert [round(b - a, 6) for a, b in zip(live.offsets, live.offsets[1:])] == [JOIN_GAP_S] * 3
    aligned = from_t0(1791010992.7, 4, 0.02, "align")
    out = estimate_start(4, aligned, live_time=1791010991)
    assert out is aligned and out.notes == []
    far = estimate_start(4, from_t0(1791010999.0, 4, 0.02, "align"), live_time=1791010991)
    assert far.method == "align" and "differs from live_time" in far.notes[0]
    coarse = estimate_start(1, live_events=[1791011012.0])
    assert coarse.method == "live-event" and coarse.pm == 7.0
    assert estimate_start(3) is None


def test_s0_from_the_replays_reposted_live_danmaku():
    """BV1P1as6hE9y (巡游, 2026-09-26 16:56:01 开播): its danmaku importer used S0 = live_time + 6."""
    live = parse_dmks_live((FIX / "dmks_live_7a5add15.msgpack").read_bytes())
    assert live.start == 1790412961.0
    assert len(live.messages) == 219 and all(len(c) <= 8 for c, _, _ in live.messages[:20])
    s0, n = danmaku_origin((FIX / "dm_42232054404.xml").read_bytes(), live.messages)
    assert s0 == 1790412967.0 and n >= 50
    est = estimate_start(1, live_time=live.start, s0=s0)
    assert est.method == "live_time+S0" and est.pm < 0.25
    assert danmaku_origin(b"<i></i>", live.messages) == (None, 0)


def test_minimal_msgpack_decoder():
    def s(x: str) -> bytes:
        b = x.encode()
        return bytes([0xA0 | len(b)]) + b
    raw = (bytes([0x93])  # [ {..}, -3, [..] ]
           + bytes([0x82]) + s("a") + bytes([0x01]) + s("b") + bytes([0xCB]) + struct.pack(">d", 1.5)
           + bytes([0xFD])
           + bytes([0xDC, 0x00, 0x05]) + bytes([0xC0, 0xC3, 0xCD, 0x01, 0x00]) + bytes([0xD2]) + struct.pack(">i", -70000)
           + bytes([0xD9, 0x03]) + "胡".encode())
    assert unpackb(raw) == [{"a": 1, "b": 1.5}, -3, [None, True, 256, -70000, "胡"]]


def test_alignment_measures_each_fragment(synth):
    """A 'recording' of the stream that has 30 ms more audio at the fragment join and 45 ms more
    at the page boundary than the replay (what Bilibili's joins lose): each fragment's offset is
    measured; windows across a repeat are refused; an unrelated window doesn't vote."""
    d = synth.dir
    jf = JoinFinder([load_part(p, d / f"al{k}.aac") for k, p in enumerate(synth.audio)], fragment_s=(25, 50))
    tl = Timeline(jf.parts, jf.select(jf.candidates())[0])
    x = decode_8k([synth.stream.path])
    cut1, cut2 = int(synth.cuts[0][0] * 1024 / 6), int(tl.content_packets(tl.starts[1] + 234) * 1024 / 6)
    rng = np.random.default_rng(1)
    rec = np.concatenate([x[:cut1], rng.normal(0, 0.05, 240), x[cut1:cut2], rng.normal(0, 0.05, 360), x[cut2:]])
    rec = (0.7 * rec + rng.normal(0, 0.01, len(rec))).astype(np.float32)
    W = 1_800_000_000.0
    index = AlignIndex([decode_8k([p.path], d / f"al{k}.f32") for k, p in enumerate(jf.parts)])

    def win(t, n=10):
        return rec[int(t * SR): int((t + n) * SR)], W + t

    noise = (rng.normal(0, 0.3, 10 * SR).astype(np.float32), W + 50)
    place, rows = align(index, tl, [win(2), win(12), win(25), win(38), win(70), win(85), noise])
    assert place is not None and place.measured == [0, 1, 2]
    for f, want in enumerate((0.0, 0.030, 0.075)):
        assert abs(place.offsets[f] - (W + want)) < 0.003, (f, place.offsets[f] - W)
    assert [r["ok"] for r in rows] == [True, True, False, True, True, True, False]
    # Only fragment 0 seen: the rest extrapolate at JOIN_GAP_S per join.
    place0, _ = align(index, tl, [win(2), win(12)])
    assert place0.measured == [0]
    assert abs(place0.offsets[2] - place0.offsets[0] - 2 * JOIN_GAP_S) < 1e-6
    assert align(index, tl, [noise])[0] is None


@pytest.mark.skipif(not (KL_FLV.exists() and (REPLAY / "p2_audio.m4a").exists()), reason="KL material not present")
def test_real_kl_alignment(tmp_path):
    """The KL recording against its replay: the step at every join is measured (+29/+29/+43/+22/+30 ms),
    and extrapolating from a single fragment stays within ~15 ms of that everywhere."""
    parts = [load_part(REPLAY / f"p{k}_audio.m4a", tmp_path / f"p{k}.aac") for k in (1, 2)]
    tl = Timeline(parts, find_joins(parts)[0])
    index = AlignIndex([decode_8k([p.path], tmp_path / f"p{k}.f32") for k, p in enumerate(parts)])

    def win(t):
        out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(t), "-i", str(KL_FLV), "-t", "30",
                              "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"], check=True,
                             capture_output=True).stdout
        return np.frombuffer(out, "<f4"), KL_W0 + t

    full, _ = align(index, tl, [win(t) for t in (300, 1000, 3000, 5000, 7000, 9500)])
    assert full.measured == [0, 1, 2, 3, 5]
    steps = [round((b - a) * 1000) for a, b in zip(full.offsets, full.offsets[1:])]
    assert steps[0] in range(26, 33) and steps[1] in range(26, 33) and steps[2] in range(39, 47)
    assert abs(full.t0 - KL_W0) < 0.005
    one, _ = align(index, tl, [win(4000), win(4500)])
    assert one.measured == [2]
    assert max(abs(a - b) for a, b in zip(one.offsets, full.offsets)) < 0.015
