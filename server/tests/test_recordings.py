import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fone.recordings import parse_name, scan

SH = ZoneInfo("Asia/Shanghai")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def test_current_recorder_template_minute_precision():
    t, prec, title = parse_name("20261004-1430-【胡闹P房】15点 巴林 正赛", SH)
    assert t == utc(2026, 10, 4, 6, 30)
    assert prec == 60
    assert title == "【胡闹P房】15点 巴林 正赛"


def test_bililiverecorder_default_template_millis():
    t, prec, title = parse_name("录制-23954427-20261004-143012-345-【胡闹P房】15点 巴林 正赛", SH)
    assert abs(t - (utc(2026, 10, 4, 6, 30, 12) + 0.345)) < 1e-6
    assert prec == 0.001
    assert title == "【胡闹P房】15点 巴林 正赛"


def test_blrec_style():
    t, prec, _ = parse_name("23954427_2026-10-04_14-30-12", SH)
    assert t == utc(2026, 10, 4, 6, 30, 12)
    assert prec == 1


def test_digits_in_title_do_not_confuse_parser():
    t, _, title = parse_name("20261004-1430-2026赛季 第16站 120分钟", SH)
    assert t == utc(2026, 10, 4, 6, 30)
    assert title == "2026赛季 第16站 120分钟"


def test_no_timestamp():
    assert parse_name("random name", SH) == (None, None, "random name")


def test_channel_is_parent_folder_under_any_recorder_root(tmp_path):
    for root in ("bilibili", "fone"):
        (tmp_path / root / "胡闹P房").mkdir(parents=True)
        (tmp_path / root / "胡闹P房" / f"20261004-1430-{root}.flv").write_bytes(b"x")
    assert {r.channel for r in scan(tmp_path, SH)} == {"胡闹P房"}


def test_folder_alias_merges_account_name_into_show_channel(tmp_path):
    for folder in ("马莎拉车", "胡闹P房"):
        (tmp_path / "fone" / folder).mkdir(parents=True)
        (tmp_path / "fone" / folder / "20261004-1430-x.flv").write_bytes(b"x")
    recs = scan(tmp_path, SH, {"马莎拉车": "胡闹P房"})
    assert {r.channel for r in recs} == {"胡闹P房"} and len(recs) == 2


def test_parse_aliases():
    from fone.config import parse_aliases
    assert parse_aliases(" 马莎拉车=胡闹P房 , a=b,bad,=x") == {"马莎拉车": "胡闹P房", "a": "b"}


def test_scan_groups_by_channel_and_skips_noise(tmp_path):
    chan = tmp_path / "胡闹P房"
    chan.mkdir()
    (chan / "20261004-1430-a.flv").write_bytes(b"x")
    (chan / "20261004-1430-a.xml").write_text("danmaku")
    (tmp_path / "log").mkdir()
    (tmp_path / "log" / "20261004-1430-b.flv").write_bytes(b"x")
    (tmp_path / "config.json").write_text("{}")
    os.utime(chan / "20261004-1430-a.flv", (1, 1))

    recs = scan(tmp_path, SH)
    assert [r.relpath for r in recs] == ["胡闹P房/20261004-1430-a.flv"]
    assert recs[0].channel == "胡闹P房"
    assert recs[0].mtime == 1


def test_imports_are_tagged_and_keep_their_ids_wherever_the_import_dir_is(tmp_path):
    rec = tmp_path / "rec"
    for d in (rec / "fone" / "胡闹P房", rec / "fone-import" / "胡闹P房"):
        d.mkdir(parents=True)
    (rec / "fone" / "胡闹P房" / "20261004-143012-345-live.flv").write_bytes(b"x")
    (rec / "fone-import" / "胡闹P房" / "20261004-130000-000-replay.flv").write_bytes(b"x")
    nested = scan(rec, SH, None, rec / "fone-import")
    assert [(r.relpath, r.source, r.channel) for r in nested] == [
        ("fone-import/胡闹P房/20261004-130000-000-replay.flv", "import", "胡闹P房"),
        ("fone/胡闹P房/20261004-143012-345-live.flv", "recorder", "胡闹P房"),
    ]
    assert {r.source for r in scan(rec, SH)} == {"recorder"}  # no import dir configured

    # import dir outside the recordings mount: scanned as a second root, same ids
    (tmp_path / "elsewhere").mkdir()
    os.rename(rec / "fone-import", tmp_path / "elsewhere" / "fone-import")
    apart = scan(rec, SH, None, tmp_path / "elsewhere" / "fone-import")
    assert [(r.id, r.relpath, r.source) for r in apart] == [(r.id, r.relpath, r.source) for r in nested]
