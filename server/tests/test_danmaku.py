"""Danmaku: the feed protocol, captured messages -> items, the capture files' indexer and
listening spans, danmakus.com's archive filling in around them, and GET /api/danmaku."""
import asyncio
import json
import shutil
import struct
import zlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fone import danmaku as D
from fone.config import Config
from fone.danmaku_feed import OP_MESSAGE, decode, packet, wbi_sign

FIX = Path(__file__).parent / "fixtures"
CAPTURE = FIX / "danmaku" / "capture.jsonl"
ROOM = 23954427


def lines():
    return [json.loads(x) for x in CAPTURE.read_text().splitlines()]


def service(tmp_path, **settings):
    from zoneinfo import ZoneInfo

    cfg = Config(rec_dir=tmp_path / "rec", data_dir=tmp_path / "data", filename_tz=ZoneInfo("Asia/Shanghai"),
                 segment_seconds=2, growing_idle_seconds=90, reconnect_idle_seconds=8, scan_interval=3, eager_days=10,
                 max_parallel_extractions=1, default_delay=40, channel_aliases={}, rooms={ROOM: "胡闹P房"})
    return D.DanmakuService(cfg, D.Settings(**{"capture": False, "archive": False, **settings}))


def test_feed_frames_decode_plain_and_zlib_bundles():
    a, b = {"cmd": "DANMU_MSG", "info": [1]}, {"cmd": "LIVE"}
    bundle = packet(OP_MESSAGE, zlib.compress(packet(OP_MESSAGE, a, ver=0) + packet(OP_MESSAGE, b, ver=0)), ver=2)
    heartbeat_reply = struct.pack(">IHHII", 20, 16, 1, 3, 1) + b"\x00\x00\x00\x07"
    assert decode(heartbeat_reply + bundle + packet(OP_MESSAGE, b"not json", ver=0)) == [a, b]
    signed = wbi_sign({"id": 1, "type": 0}, "a" * 32, "b" * 32, now=1700000000)
    assert signed["wts"] == "1700000000" and len(signed["w_rid"]) == 32


def test_captured_messages_become_items():
    room_msgs = [x for x in lines() if "m" in x]
    items = [D.message_item(x["m"], x["rx"], ROOM) for x in room_msgs]
    assert all(items) and len(items) == 7
    first = items[0]
    info = room_msgs[0]["m"]["info"]
    assert first["t"] == info[0][4] / 1000 and first["x"] == info[1] and first["u"] == info[0][7]
    assert first["ul"] == info[4][0] and first["ml"] == info[3][0] and first["wl"] == info[16][0]
    assert "m" not in first and "c" not in first and "s" not in first  # the defaults: scroll, white, 25
    assert any(it.get("c") not in (None, 0xFFFFFF) for it in items)
    emo = next(it for it in items if "e" in it)
    assert emo["e"]["u"].startswith("http") and emo["e"]["w"] > 0
    inline = next(it for it in items if "em" in it)
    assert all(code in inline["x"] for code in inline["em"])
    sc = items[-1]
    assert sc["k"] == "sc" and sc["p"] == 30 and sc["x"] == "测试醒目留言" and sc["u"] == format(zlib.crc32(b"12345"), "x")
    # another room's medal isn't this room's fan medal
    other = json.loads(json.dumps(room_msgs[0]["m"]))
    other["info"][3][3] = 1
    assert D.message_item(other, 0, ROOM)["ml"] == 0
    assert D.message_item({"cmd": "DANMU_MSG", "info": [[0] * 5, ""]}, 0, ROOM) is None  # nothing to show
    assert D.message_item({"cmd": "INTERACT_WORD"}, 0, ROOM) is None


def test_indexer_tails_capture_files_and_records_when_it_was_listening(tmp_path):
    svc = service(tmp_path)
    path = svc.capture_dir / f"{ROOM}-20261010.jsonl"
    raw = CAPTURE.read_bytes()
    cut = raw.index(b"\n", len(raw) // 2) + 1
    path.write_bytes(raw[:cut] + raw[cut:cut + 40])  # half the file and a line still being written
    first = svc.index_once()
    assert 0 < first < 7
    with path.open("ab") as f:
        f.write(raw[cut + 40:])
    assert svc.index_once() == 7 - first
    assert svc.index_once() == 0
    got = [json.loads(j) for j in svc.store.range(ROOM, 0, 2e9)]
    assert len(got) == 7 and [g["t"] for g in got] == sorted(g["t"] for g in got)
    recs = lines()
    assert svc.store.spans(ROOM) == [(recs[0]["rx"], recs[-1]["rx"])]  # from "open" to the last beat
    # silence longer than the heartbeats, then a reconnect: two spans
    rx = recs[-1]["rx"]
    with path.open("a") as f:
        f.write(json.dumps({"rx": rx + 200, "ev": "open"}) + "\n" + json.dumps({"rx": rx + 230, "ev": "beat"}) + "\n")
    svc.index_once()
    assert svc.store.spans(ROOM) == [(recs[0]["rx"], rx), (rx + 200, rx + 230)]
    # a restart re-reads nothing (positions are kept) and two writers' duplicate lines count once
    again = service(tmp_path)
    assert again.index_once() == 0
    with path.open("ab") as f:
        f.write(raw)
    assert again.index_once() == 0


def test_archive_fills_only_what_the_capture_did_not_hear(tmp_path, monkeypatch):
    items = D.archive_items((FIX / "backfill" / "dmks_live_7a5add15.msgpack").read_bytes())
    assert len(items) == 219 and all(it["x"] and it["u"] for it in items)
    assert [it["t"] for it in items] == sorted(it["t"] for it in items)
    t0, t1 = items[0]["t"], items[-1]["t"]
    svc = service(tmp_path, archive=True)
    mid = (t0 + t1) / 2
    svc.store.save_file("x.jsonl", ROOM, 0, [mid, t1 + 100], [])  # we listened from mid-live on
    svc.uids[ROOM] = 1313650809
    lives = [{"id": "7a5add15", "start": t0 - 60, "stop": t1 + 60, "title": "巡游", "danmaku": 219},
             {"id": "still-live", "start": t1, "stop": None, "title": "now", "danmaku": 0}]

    async def fake_lives(http, uid):
        assert uid == 1313650809
        return lives

    async def fake_full(http, live):
        return (FIX / "backfill" / "dmks_live_7a5add15.msgpack").read_bytes()

    monkeypatch.setattr("fone.backfill_bili.dmks_lives", fake_lives)
    monkeypatch.setattr("fone.backfill_bili.dmks_live_full", fake_full)
    assert asyncio.run(svc.archive_once(None, now=t1 + 3600)) == 1
    got = [json.loads(j) for j in svc.store.range(ROOM, 0, 2e9)]
    before = [it for it in items if it["t"] < mid - D.SPAN_SLACK[0]]
    assert len(got) == len(before) and got[-1]["t"] < mid
    assert asyncio.run(svc.archive_once(None, now=t1 + 3600)) == 0  # once per live
    # a live that ended minutes ago waits for the archive to complete it
    lives[0]["id"] = "fresh"
    assert asyncio.run(svc.archive_once(None, now=t1 + 120)) == 0


def test_api_serves_a_window_of_items(tmp_path):
    svc = service(tmp_path)
    shutil.copy(CAPTURE, svc.capture_dir / f"{ROOM}-20261010.jsonl")
    svc.index_once()
    app = FastAPI()
    app.include_router(D.make_router(svc))
    c = TestClient(app)
    every = c.get("/api/danmaku", params={"channel": "胡闹P房", "from": 0, "to": 2e9}).json()
    assert len(every["items"]) == 7 and every["live"] is False and every["room"] == ROOM
    ts = [it["t"] for it in every["items"]]
    some = c.get("/api/danmaku", params={"channel": "胡闹P房", "from": ts[2], "to": ts[5]}).json()["items"]
    assert [it["t"] for it in some] == ts[2:5]
    assert len(c.get("/api/danmaku", params={"channel": "胡闹P房", "from": 0, "to": 2e9, "limit": 2}).json()["items"]) == 2
    assert c.get("/api/danmaku", params={"channel": "nobody", "from": 0, "to": 1}).status_code == 404
    assert c.get("/api/danmaku/status").json()["rooms"][str(ROOM)] == {"channel": "胡闹P房", "live": False}


def test_old_items_and_raw_files_are_pruned(tmp_path):
    svc = service(tmp_path)
    old = svc.capture_dir / f"{ROOM}-20200101.jsonl"
    keep = svc.capture_dir / f"{ROOM}-20261010.jsonl"
    old.write_text("")
    keep.write_text("")
    svc.store.add(ROOM, [{"t": 1000.0, "x": "old"}, {"t": 1.791e9, "x": "new"}], "capture")
    svc.prune_once(1.7916e9)
    assert not old.exists() and keep.exists()
    assert [json.loads(j)["x"] for j in svc.store.range(ROOM, 0, 2e9)] == ["new"]


@pytest.mark.parametrize("bad", [[], [[]], [[0, 1, 25, 0xFFFFFF, "x"], "hi"], "nope"])
def test_malformed_danmu_is_skipped(bad):
    assert D.danmu_item(bad, 0, ROOM) is None
