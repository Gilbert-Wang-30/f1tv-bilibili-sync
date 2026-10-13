import asyncio
import os
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fone import config, recordings
from fone.backfill_dedup import JoinFinder, Timeline, load_part
from fone.backfill_start import JOIN_GAP_S, from_t0
from fone.backfill_writer import audio_packets, build_import, import_name, probe, publish, safe_title
from fone.extractor import Extractor
from test_backfill_synth import ff, synth  # noqa: F401  (fixture)

SH = ZoneInfo("Asia/Shanghai")
T0 = 1791010992.735  # recorder-frame wall time of the synthetic replay's first sample
TITLE = "【胡闹P房】16点 巴林 排位赛"


def test_import_name_is_recorder_style_and_parses_back():
    name = import_name(T0, TITLE, SH)
    assert name == "20261003-150312-735-【胡闹P房】16点 巴林 排位赛.flv"
    t, precision, title = recordings.parse_name(Path(name).stem, SH)
    assert abs(t - T0) < 1e-3 and precision == 0.001 and title == TITLE
    assert import_name(1791010992.9996, "x", SH).startswith("20261003-150313-000-")  # rounds into the next second
    assert safe_title('a/b\\c:d*e?"f"<g>|h\x01') == "a_b_c_d_e_f_g_h"
    assert safe_title(" ... ") == "replay"
    long = safe_title("胡闹P房" * 60)  # 300 characters, 900+ bytes
    assert len(import_name(T0, long, SH).encode() + b".part") < 255 and long.startswith("胡闹P房")


def timeline(synth, tmp):
    parts = [load_part(p, tmp / f"p{k}.aac") for k, p in enumerate(synth.audio)]
    jf = JoinFinder(parts, fragment_s=(25, 50))
    return Timeline(parts, jf.select(jf.candidates())[0])


def extract(path_root: Path, data: Path):
    """Run fone's own extractor over the import dir, like the server's scanner would."""
    cfg = replace(config.load(), rec_dir=path_root, data_dir=data)

    async def run():
        ex = Extractor(cfg)
        ex.update(recordings.scan(path_root, SH))
        (st,) = ex.files.values()
        assert not ex.is_growing(st.rec)
        ex.ensure(st)
        for _ in range(300):
            if st.status in ("done", "error"):
                break
            await asyncio.sleep(0.1)
        return ex, st

    return asyncio.run(run())


def test_full_import_with_video(synth, tmp_path):
    tl = timeline(synth, tmp_path)
    place = from_t0(T0, tl.fragments, 0.05, "live_time")
    root = tmp_path / "import"
    res = build_import(tl, place, 0, 1, dict(enumerate(synth.video)), tmp_path / "work", root / "胡闹P房", TITLE, SH)

    assert res.path == root / "胡闹P房" / import_name(T0, TITLE, SH) and res.path.exists()
    assert not list(root.rglob("*.part"))
    assert res.video == "copy" and res.notes == []
    assert [round(g, 4) for g in res.gaps] == [JOIN_GAP_S, JOIN_GAP_S]
    assert abs(res.path.stat().st_mtime - (T0 + res.duration)) < 1e-3
    kept = [h for r in tl.keep() for h in tl.parts[r.part].hashes[r.i0:r.i1]]
    assert abs(res.duration - (len(kept) * 1024 / 48000 + 2 * JOIN_GAP_S)) < 1e-6

    # The audio is exactly the stream, once, with the join losses as timestamp gaps.
    pk = audio_packets(res.path)
    assert [h for _, h in pk] == [f"MD5:{h.hex()}" for h in kept]
    assert [h for _, h in pk] == [f"MD5:{h.hex()}" for h in synth.stream.hashes]
    jumps = [b[0] - a[0] - 1024 / 48000 for a, b in zip(pk, pk[1:]) if b[0] - a[0] > 0.03]
    assert len(jumps) == 2 and all(abs(j - JOIN_GAP_S) <= 0.0015 for j in jumps)  # FLV stamps are whole ms
    info = probe(res.path)
    assert abs(info["video"]["duration"] - info["audio"]["duration"]) < 0.1

    # fone's extractor maps it like a recording: media 0 at the name time.
    ex, st = extract(root, tmp_path / "data")
    pieces, segs, provisional = ex.pieces(st)
    assert st.status == "done" and not provisional and len(pieces) == 1
    assert abs(pieces[0].wall - T0) < 0.03
    assert abs(segs[-1].end - res.duration) < 0.05


def test_later_page_alone_audio_only(synth, tmp_path):
    tl = timeline(synth, tmp_path)
    place = from_t0(T0, tl.fragments, 0.05, "live_time")
    res = build_import(tl, place, 1, 1, None, tmp_path / "work", tmp_path / "import", TITLE, SH)
    assert res.video == "none" and res.gaps == []
    # Page 2 starts with its 5-s repeat; placed by the new content after it (fragment 2).
    lead = tl.content(tl.starts[1] + 234) - 234 * 1024 / 48000
    assert abs(res.start - (T0 + 2 * JOIN_GAP_S + lead)) < 1e-6
    pk = audio_packets(res.path)
    assert len(pk) == tl.parts[1].n and "video" not in probe(res.path)


def test_reencodes_when_keyframes_do_not_sit_on_the_cuts(synth, tmp_path):
    tl = timeline(synth, tmp_path)
    vids = {}
    for k, v in enumerate(synth.video):  # 10-s GOPs: the 5-s repeat can't be dropped by whole GOPs
        vids[k] = tmp_path / f"gop10_{k}.mp4"
        ff("-i", str(v), "-c:v", "libx264", "-g", "250", "-keyint_min", "250", "-sc_threshold", "0",
           "-pix_fmt", "yuv420p", str(vids[k]))
    place = from_t0(T0, tl.fragments, 0.05, "live_time")
    res = build_import(tl, place, 0, 1, vids, tmp_path / "work", tmp_path / "import", TITLE, SH)
    assert res.video == "reencode" and any("keyframe" in n for n in res.notes)
    info = probe(res.path)
    assert abs(info["video"]["duration"] - res.duration) < 0.2


def test_publish_stages_then_renames(tmp_path):
    src = tmp_path / "x.flv"
    src.write_bytes(b"FLV")
    final = tmp_path / "out" / "c" / "20261003-150312-735-t.flv"
    publish(src, final, 1791010992.735, 100.0)
    assert final.read_bytes() == b"FLV" and not final.with_name(final.name + ".part").exists()
    assert abs(os.stat(final).st_mtime - 1791011092.735) < 1e-3


@pytest.mark.parametrize("bad", ["", "\x00"])
def test_safe_title_never_empty(bad):
    assert safe_title(bad) == "replay"
