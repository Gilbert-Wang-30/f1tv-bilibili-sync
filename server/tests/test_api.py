"""End-to-end: real ffmpeg extraction of finished and still-growing recordings."""

import importlib
import os
import subprocess
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

SH = ZoneInfo("Asia/Shanghai")
CHANNEL = "胡闹P房"


def make_flv(path, seconds, realtime=False):
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
    if realtime:
        cmd += ["-re"]
    cmd += [
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-f", "lavfi", "-i", f"color=c=black:s=64x36:r=10:d={seconds}",
        "-c:a", "aac", "-b:a", "128k", "-c:v", "libx264", "-tune", "zerolatency", "-g", "20", "-shortest",
        "-flush_packets", "1", "-f", "flv", str(path),
    ]
    return subprocess.Popen(cmd) if realtime else subprocess.run(cmd, check=True)


def name_for(t, title="【胡闹P房】15点 巴林 正赛"):
    return datetime.fromtimestamp(t, SH).strftime("%Y%m%d-%H%M") + f"-{title}.flv"


@pytest.fixture
def server(tmp_path, monkeypatch):
    rec, data = tmp_path / "rec", tmp_path / "data"
    (rec / CHANNEL).mkdir(parents=True)
    for k, v in {
        "FONE_REC_DIR": rec, "FONE_DATA_DIR": data, "FONE_SCAN_INTERVAL": "0.3",
        "FONE_GROWING_IDLE_SECONDS": "4", "FONE_RECONNECT_IDLE_SECONDS": "2",
        # services that call out to F1 / OpenF1 stay off; test_integration covers the wiring
        "FONE_TIMING": "0", "FONE_SCREENCLOCK": "0",
    }.items():
        monkeypatch.setenv(k, str(v))
    import fone.app

    app_mod = importlib.reload(fone.app)
    with TestClient(app_mod.app) as client:
        yield client, rec / CHANNEL


def wait_for(fn, timeout=40, every=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (v := fn()) is not None:
            return v
        time.sleep(every)
    raise AssertionError("timed out")


def timeline(client, lo, hi):
    r = client.get("/api/timeline", params={"channel": CHANNEL, "from": lo, "to": hi})
    assert r.status_code == 200, r.text
    return r.json()


def test_finished_recording(server):
    client, chan = server
    end = time.time() - 3600
    tmp = chan.parent / "tmp.flv"
    make_flv(tmp, 30)
    path = chan / name_for(end - 30)
    os.rename(tmp, path)
    os.utime(path, (end, end))

    tl = wait_for(lambda: (t := timeline(client, end - 7200, end + 60))["files"]
                  and t["files"][0]["status"] == "done" and t or None)
    f = tl["files"][0]
    assert f["growing"] is False and f["provisional"] is False and tl["complete"] and not tl["live"]
    assert abs(f["duration"] - 30) < 0.2
    # Finished file seen after the fact: wall start = last write - duration.
    assert abs(tl["pieces"][0]["w"] - (end - f["duration"])) < 0.05
    assert tl["pieces"][0]["m"] == 0

    pl = client.get(tl["playlist"]).text
    assert pl.rstrip().endswith("#EXT-X-ENDLIST")
    seg_urls = [line for line in pl.splitlines() if line.startswith("/hls/")]
    assert len(seg_urls) >= 14
    seg = client.get(seg_urls[0])
    assert seg.status_code == 200 and seg.content[:1] == b"\x47"  # MPEG-TS sync byte

    assert client.get("/hls/nope/seg_00000.ts").status_code == 404
    assert client.get(f"/hls/{f['id']}/../done.json").status_code == 404


def test_growing_recording_is_live_then_finalised_on_reconnect(server):
    client, chan = server
    started = time.time()
    path = chan / name_for(started)
    first_bytes = {}

    def watch():  # when the "recorder" actually started putting audio on disk
        while "t" not in first_bytes and time.time() - started < 10:
            if path.exists() and path.stat().st_size > 0:
                first_bytes["t"] = time.time()
            time.sleep(0.01)

    threading.Thread(target=watch, daemon=True).start()
    writer = make_flv(path, 14, realtime=True)
    try:
        tl = wait_for(lambda: (t := timeline(client, started - 600, started + 600))["live"]
                      and t["files"] and not t["files"][0]["provisional"] and t or None, timeout=25)
        f = tl["files"][0]
        assert f["growing"] and f["status"] == "running" and not tl["complete"]
        # Live lag: audio at media 0 arrived when the recorder started writing.
        err = tl["pieces"][0]["w"] - first_bytes["t"]
        assert abs(err) < 0.75, err
        edge1 = tl["edge"]
        assert abs(edge1 - time.time()) < 4.0  # edge tracks real time (segment length + polling)
        time.sleep(3)
        edge2 = timeline(client, started - 600, started + 600)["edge"]
        assert 1.0 < edge2 - edge1 < 5.0
        assert not client.get(tl["playlist"]).text.rstrip().endswith("#EXT-X-ENDLIST")
    finally:
        writer.wait(timeout=30)

    # Stream reconnect: recorder opens a new file; the old one is finalised quickly.
    second = chan / name_for(time.time() + 60, title="reconnect")
    make_flv(second, 6)
    tl = wait_for(lambda: (t := timeline(client, started - 600, started + 900))["files"]
                  and all(x["status"] == "done" for x in t["files"]) and len(t["files"]) == 2 and t or None)
    assert abs(tl["files"][0]["duration"] - 14) < 0.3
    assert [x["inPlaylist"] for x in tl["files"]] == [True, True]
    # Stitched playlist: both files, one discontinuity, closed.
    pl = client.get(tl["playlist"]).text
    assert pl.count("#EXT-X-DISCONTINUITY") == 1 and pl.rstrip().endswith("#EXT-X-ENDLIST")
    # Second file's audio starts after the first in the stitched media timeline.
    f1, f2 = tl["files"]
    assert abs(f2["offset"] - f1["duration"]) < 1e-6
    # Where the two overlap in wall time (this "reconnect" wrote 6 s of audio instantly, so
    # it maps onto the first file's last seconds) the first plays on: no two pieces overlap.
    p2 = [p for p in tl["pieces"] if p["f"] == 1][0]
    assert p2["w"] >= f1["wallEnd"] - 1e-6
    assert abs((p2["m"] - f2["offset"]) - (p2["w"] - f2["wallStart"])) < 1e-6


def test_sync_defaults_and_calibration(server):
    client, _ = server
    q = {"channel": CHANNEL, "clock": "multiviewer"}
    assert client.get("/api/sync/123", params=q).json() == {"delay": 40.0, "source": "default"}
    client.put("/api/sync/123", json={**q, "delay": 47.25})
    assert client.get("/api/sync/123", params=q).json() == {"delay": 47.25, "source": "content"}
    # A calibration becomes the starting point for other sessions on comparable clocks...
    assert client.get("/api/sync/456", params=q).json() == {"delay": 47.25, "source": "channel"}
    # ...but a guess-based live clock doesn't overwrite it.
    client.put("/api/sync/789", json={"channel": CHANNEL, "clock": "edge", "delay": 12})
    assert client.get("/api/sync/456", params=q).json()["delay"] == 47.25


def test_sync_channel_default_only_from_comparable_clocks(server):
    """F1 TV's live player clock ('epoch') reads ~45 s late: a live session calibrated
    -0.2 on it, and the next replay (true UTC) started 45 s early from that."""
    client, _ = server

    def put(cid, clock, delay):
        r = client.put(f"/api/sync/{cid}", json={"channel": CHANNEL, "clock": clock, "delay": delay})
        assert r.status_code == 200

    def get(cid, clock, channel=CHANNEL):
        return client.get(f"/api/sync/{cid}", params={"channel": channel, "clock": clock}).json()

    assert get("1", "epoch") == {"delay": 40.0, "source": "default"}
    put("1000011024", "multiviewer", 40.8)
    put("1000010389", "multiviewer", 44.94)
    put("1000011022", "epoch", -0.2)  # the most recent calibration of all
    assert get("2", "multiviewer") == {"delay": 44.94, "source": "channel"}
    assert get("2", "preroll") == {"delay": 44.94, "source": "channel"}  # an estimated anchor on the same UTC
    assert get("3", "epoch") == {"delay": -0.2, "source": "channel"}  # from the player clock's own
    assert get("4", "edge") == {"delay": 40.0, "source": "default"}  # the extension starts it at its live default
    assert get("2", "multiviewer", channel="someone else") == {"delay": 40.0, "source": "default"}
    put("1000011100", "preroll", 43.5)
    assert get("2", "multiviewer") == {"delay": 43.5, "source": "channel"}
    put("1000011101", "epoch", 0.4)
    assert get("2", "multiviewer") == {"delay": 43.5, "source": "channel"}
    assert get("3", "epoch") == {"delay": 0.4, "source": "channel"}
