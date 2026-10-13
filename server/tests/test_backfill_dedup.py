import os
import subprocess
from pathlib import Path

import pytest

from fone.backfill_dedup import JoinFinder, Timeline, find_joins, load_part, video_packets, video_plan
from test_backfill_synth import LAG, synth  # noqa: F401  (fixture)

REPLAY = Path(os.environ.get("FONE_TEST_KL_REPLAY", ""))  # the KL replay download (p1/p2_audio.m4a); unset: skipped


def parts_of(synth, tmp):
    return [load_part(p, tmp / f"p{k}.aac") for k, p in enumerate(synth.audio)]


def test_finds_fragment_join_and_page_boundary_exactly(synth, tmp_path):
    parts = parts_of(synth, tmp_path)
    jf = JoinFinder(parts, fragment_s=(25, 50))  # synthetic fragments are 30-35 s, not ~1805 s
    joins, warnings = jf.select(jf.candidates())
    assert [(j.cut, j.lag) for j in joins] == synth.cuts
    assert [j.kind for j in joins] == ["fragment-join", "part-boundary"]
    assert all(j.tier == "strong" for j in joins) and warnings == []


def test_timeline_maps_every_packet_to_its_stream_position(synth, tmp_path):
    parts = parts_of(synth, tmp_path)
    jf = JoinFinder(parts, fragment_s=(25, 50))
    tl = Timeline(parts, jf.select(jf.candidates())[0])
    for k, idx in enumerate(synth.index):
        for i in range(0, len(idx), 7):
            assert tl.content_packets(tl.starts[k] + i) == idx[i]
    # The kept packets are the whole stream, once, in order.
    kept = [synth.index[r.part][i] for r in tl.keep() for i in range(r.i0, r.i1)]
    assert kept == list(range(synth.stream.n))
    assert tl.fragments == 3
    assert [tl.fragment(g) for g in (0, synth.cuts[0][0] - 1, synth.cuts[0][0] + LAG, tl.starts[1] + LAG)] == [0, 0, 1, 2]


def test_output_starting_at_a_later_page_keeps_its_leading_repeat(synth, tmp_path):
    parts = parts_of(synth, tmp_path)
    jf = JoinFinder(parts, fragment_s=(25, 50))
    tl = Timeline(parts, jf.select(jf.candidates())[0])
    keep = tl.keep(1, 1)
    assert [(r.part, r.i0, r.i1) for r in keep] == [(1, 0, parts[1].n)]
    assert tl.content_packets(tl.starts[1]) == synth.index[1][0]  # placed where its twin is
    # Page 1 alone: its internal repeat goes, the part-boundary repeat has no later copy inside.
    assert [(r.i0, r.i1) for r in tl.keep(0, 0)] == [(0, synth.cuts[0][0]), (synth.cuts[0][0] + LAG, parts[0].n)]
    assert tl.ambiguous(synth.cuts[0][0] - 10, synth.cuts[0][0] + 10)
    assert not tl.ambiguous(10, 500)


def test_video_plan_cuts_whole_gops_at_content_equivalent_points(synth, tmp_path):
    parts = parts_of(synth, tmp_path)
    jf = JoinFinder(parts, fragment_s=(25, 50))
    tl = Timeline(parts, jf.select(jf.candidates())[0])
    pk = {k: video_packets(v) for k, v in enumerate(synth.video)}
    plan, problems = video_plan(tl, pk, 0, 1)
    assert problems == []
    assert [(v.part, v.start, v.end, v.frames) for v in plan] == [
        (0, 0.0, 30.0, 750), (0, 35.0, 60.0, 625), (1, 0.0, 45.0, 1125)]
    plan2, problems2 = video_plan(tl, pk, 1, 1)
    assert problems2 == [] and [(v.part, v.start, v.frames) for v in plan2] == [(1, 0.0, 1125)]


def test_video_plan_reports_missing_keyframes(synth, tmp_path):
    parts = parts_of(synth, tmp_path)
    jf = JoinFinder(parts, fragment_s=(25, 50))
    tl = Timeline(parts, jf.select(jf.candidates())[0])
    pk = {k: [(t, t == 0.0) for t, _ in video_packets(v)] for k, v in enumerate(synth.video)}  # one GOP per page
    _, problems = video_plan(tl, pk, 0, 1)
    assert problems and "keyframe" in problems[0]


def test_pages_without_any_repeat_are_simply_concatenated(synth, tmp_path):
    parts = parts_of(synth, tmp_path)
    tl = Timeline(parts, [])
    assert [(r.part, r.i0, r.i1) for r in tl.keep()] == [(0, 0, parts[0].n), (1, 0, parts[1].n)]
    pk = {k: video_packets(v) for k, v in enumerate(synth.video)}
    plan, _ = video_plan(tl, pk, 0, 1)
    assert [(v.part, v.start) for v in plan] == [(0, 0.0), (1, 0.0)]


def container_pts(path: Path) -> list[float]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "packet=pts_time",
                          "-of", "csv=p=0", str(path)], check=True, capture_output=True, text=True).stdout
    return [float(x) for x in out.split()]


@pytest.mark.skipif(not (REPLAY / "p2_audio.m4a").exists(), reason="KL replay download not present")
def test_real_kl_replay(tmp_path):
    """BV1ePHq6DE8B (KL qualifying): 2 pages, 3 + 1 fragment joins and a page boundary. Hand-cut
    reference (container time): joins 1805.12 / 3606.60 / 5416.60, page 1 repeated tail from
    7216.613, page 2 join 1805.46; 10378.3 s of unique audio."""
    srcs = [REPLAY / "p1_audio.m4a", REPLAY / "p2_audio.m4a"]
    parts = [load_part(s, tmp_path / f"p{k}.aac") for k, s in enumerate(srcs)]
    joins, warnings = find_joins(parts)
    assert warnings == []
    tl = Timeline(parts, joins)
    pts = [container_pts(s) for s in srcs]

    def at(g):  # container time of global packet g
        k = tl.part_of(g)
        return k, pts[k][g - tl.starts[k]]

    got = []
    for j in joins:
        if j.kind == "part-boundary":  # the earlier page's copy of the later page's start
            got.append(at(tl.starts[1] - j.lag))
        else:
            got.append(at(j.cut))
    want = [(0, 1805.12), (0, 3606.60), (0, 5416.60), (0, 7216.613), (1, 1805.46)]
    assert [k for k, _ in got] == [k for k, _ in want]
    for (_, t), (_, w) in zip(got, want):
        assert abs(t - w) < 0.03, (t, w)
    assert [j.kind for j in joins] == ["fragment-join"] * 3 + ["part-boundary", "fragment-join"]
    assert abs(tl.keep_seconds(tl.keep()) - 10378.3) < 0.1
    assert tl.fragments == 6
