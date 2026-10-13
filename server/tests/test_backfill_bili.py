import asyncio
import fcntl
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from fone import backfill_bili as bb

FIX = Path(__file__).parent / "fixtures" / "backfill"
KL_LIVE = 1791010991.0  # 2026-10-03 15:03:11 CST, the KL qualifying stream's 开播


def fixture(name):
    return json.loads((FIX / name).read_text())


def bj(*a):
    return datetime(*a, tzinfo=bb.SH).timestamp()


def test_series_listing_parses_replay_titles():
    reps = bb.parse_replays(fixture("series_archives.json")["data"]["archives"])
    assert len(reps) == 30
    kl = next(r for r in reps if r.bvid == "BV1ePHq6DE8B")
    assert kl.live_title == "【胡闹P房】16点 巴林 排位赛"
    assert kl.slot == bj(2026, 10, 3, 15) and kl.duration == 10404
    assert kl.content == 10404 - 5 * 5  # ~5 s repeat per 1800 s
    assert bb.parse_replay({"title": "普通视频", "bvid": "x", "duration": 1, "pubdate": 1}) is None
    assert bb.find_series_id(fixture("seasons_series_list.json")["data"]) == 4169347
    with pytest.raises(bb.BiliError):
        bb.find_series_id({"items_lists": {"series_list": []}})


def test_room_status():
    off = bb.room_status(23954427, fixture("room_get_info.json")["data"])
    assert off.live_status == 2 and not off.live and off.live_time is None and off.uid == 1313650809
    on = bb.room_status(23954427, {"uid": 1, "live_status": 1, "live_time": "2026-10-03 15:03:11", "title": "t"})
    assert on.live and on.live_time == KL_LIVE


def test_match_pins_the_replay_with_our_live_time():
    reps = bb.parse_replays(fixture("series_archives.json")["data"]["archives"])
    gap = (KL_LIVE + 1200, KL_LIVE + 2400)
    hits = bb.match(reps, *gap, title="【胡闹P房】16点 巴林 排位赛", live_start=KL_LIVE)
    sure = [h for h in hits if h.kind == "sure"]
    assert [h.replay.bvid for h in sure] == ["BV1ePHq6DE8B"]
    assert sure[0].start == KL_LIVE and sure[0].replay.exact_start == KL_LIVE
    # Without the live_time the same replay is still found (its hour slot overlaps the gap)...
    loose = bb.match(reps, *gap)
    assert "BV1ePHq6DE8B" in [h.replay.bvid for h in loose]
    # ...and a gap in a stream-less night matches nothing.
    assert bb.match(reps, bj(2026, 10, 3, 3), bj(2026, 10, 3, 4)) == []


def test_match_restart_within_the_hour_uses_session_lengths():
    """Stream stopped 9 min in and restarted at 15:20 with the same title: two replays share the
    15点 slot; our logged sessions tell which is which."""
    s1, s2 = bj(2026, 10, 3, 15, 2), bj(2026, 10, 3, 15, 20)
    mk = lambda bvid, dur, pub: {"title": "【直播回放】T 2026年10月03日15点场", "bvid": bvid, "duration": dur, "pubdate": pub}
    reps = bb.parse_replays([mk("BVshort", 540, 10), mk("BVlong", 7200, 11)])
    sessions = [(s1, s1 + 540), (s2, s2 + 7200)]
    hits = {h.replay.bvid: h for h in bb.match(reps, s2 + 100, s2 + 900, live_start=s2, sessions=sessions)}
    assert set(hits) == {"BVlong"} and hits["BVlong"].start == s2 and hits["BVlong"].kind == "sure"
    hits = {h.replay.bvid: h for h in bb.match(reps, s1 + 60, s1 + 400, live_start=s1, sessions=sessions)}
    assert hits["BVshort"].start == s1 and "BVlong" not in hits


def test_pages_for_window():
    pages = fixture("pagelist.json")["data"]
    assert [p["duration"] for p in pages] == [7222, 3182]
    sel = lambda a, b: [p["page"] for p in bb.pages_for_window(KL_LIVE, KL_LIVE, pages, KL_LIVE + a, KL_LIVE + b)]
    assert sel(100, 200) == [1]
    assert sel(8000, 9000) == [2]
    assert sel(7000, 7300) == [1, 2]
    assert sel(-9000, -8000) == []


def test_pick_stream():
    dash = fixture("playurl.json")["data"]["dash"]
    assert bb.pick_stream(dash, "audio")["id"] == 30280
    v = bb.pick_stream(dash, "video", 1080)
    assert (v["id"], v["codecid"], v["height"]) == (80, 7, 1080)
    assert bb.pick_stream(dash, "video", 720)["height"] == 720
    urls = bb.stream_urls(v)
    assert len(urls) == 2 and urls[0].startswith("https://upos-") and "30080.m4s" in urls[0]
    with pytest.raises(bb.BiliError):
        bb.pick_stream(dash, "video", 100)


# ---- API client -----------------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def client(handler, min_interval=0.0):
    return bb.BiliClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_interval=min_interval)


def test_client_parses_and_spaces_requests():
    seen = []

    def handler(req: httpx.Request):
        seen.append((time.monotonic(), req.url.path, req.headers.get("referer"), req.headers.get("origin")))
        name = {"/room/v1/Room/get_info": "room_get_info.json", "/x/player/pagelist": "pagelist.json",
                "/x/series/archives": "series_archives.json", "/x/player/playurl": "playurl.json",
                "/x/polymer/web-space/seasons_series_list": "seasons_series_list.json"}[req.url.path]
        return httpx.Response(200, json=fixture(name))

    async def go():
        c = client(handler, min_interval=0.2)
        st = await c.room_info(23954427)
        sid = await c.series_id(1313650809)
        arcs = await c.series_archives(1313650809, sid, since=1791000000)  # page 1 reaches past it
        pages = await c.pagelist("BV1ePHq6DE8B")
        dash = await c.playurl("BV1ePHq6DE8B", pages[0]["cid"])
        return st, sid, arcs, pages, dash, c.calls

    st, sid, arcs, pages, dash, calls = run(go())
    assert st.uid == 1313650809 and sid == 4169347 and len(arcs) == 30 and pages[0]["page"] == 1
    assert "audio" in dash and calls == 5
    gaps = [b[0] - a[0] for a, b in zip(seen, seen[1:])]
    assert min(gaps) >= 0.19  # >= min_interval between API calls
    assert seen[0][2] == "https://live.bilibili.com/23954427" and seen[0][3] == "https://live.bilibili.com"


@pytest.mark.parametrize("response,exc", [
    (httpx.Response(412, text="blocked"), bb.RiskControl),
    (httpx.Response(200, json={"code": -352, "message": "risk"}), bb.RiskControl),
    (httpx.Response(200, json={"code": -404, "message": "nothing"}), bb.BiliError),
    (httpx.Response(500, text="oops"), bb.BiliError),
])
def test_client_errors(response, exc):
    with pytest.raises(exc):
        run(client(lambda req: response).pagelist("BV1xx"))


# ---- downloads ------------------------------------------------------------------------------------

BLOB = bytes(range(256)) * (11 * 4096 + 7)  # ~11 MiB, not a multiple of the chunk


def serve(blob=BLOB, broken=(), forbidden=(), log=None, short=False):
    """Mirrors as hosts: 'broken' answer 404, 'forbidden' 403; ranges served from blob."""
    def handler(req: httpx.Request):
        host = req.url.host
        rng = req.headers.get("range", "")
        if log is not None:
            log.append((host, rng))
        if host in broken:
            return httpx.Response(404)
        if host in forbidden:
            return httpx.Response(403)
        a, b = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        b = min(b, len(blob) - 1)
        if short and b - a > 1 << 20:  # a CDN that cuts responses short
            b = a + (1 << 20) - 1
        return httpx.Response(206, content=blob[a:b + 1], headers={"Content-Range": f"bytes {a}-{b}/{len(blob)}"})
    return handler


def downloader(handler, **kw):
    return bb.Downloader(httpx.AsyncClient(transport=httpx.MockTransport(handler)), chunk=4 << 20, min_rate=0,
                         backoff=lambda n: 0, **kw)


def urls_fn(*hosts, calls=None):
    async def f():
        if calls is not None:
            calls.append(1)
        return [f"https://{h}/x.m4s" for h in hosts]
    return f


def test_download_falls_back_to_a_working_mirror(tmp_path):
    dest = tmp_path / "a.m4s"
    seen = []
    size = run(downloader(serve(broken={"bad"})).fetch(urls_fn("bad", "good"), dest, lambda h, t: seen.append(h)))
    assert size == len(BLOB) and dest.read_bytes() == BLOB
    assert not (tmp_path / "a.m4s.part").exists() and not (tmp_path / "a.m4s.lock").exists()
    assert seen[-1] == len(BLOB)
    assert run(downloader(serve(broken={"good", "bad"})).fetch(urls_fn("bad"), dest)) == len(BLOB)  # already there


def test_download_resumes_a_partial_file(tmp_path):
    dest = tmp_path / "a.m4s"
    (tmp_path / "a.m4s.part").write_bytes(BLOB[: 3 << 20])
    log = []
    run(downloader(serve(log=log, short=True)).fetch(urls_fn("m1", "m2"), dest))
    assert dest.read_bytes() == BLOB
    data_ranges = [r for _, r in log if r != f"bytes=0-{(1 << 20) - 1}"]  # minus the mirror-ranking probes
    assert data_ranges[0] == f"bytes={3 << 20}-{(3 << 20) + (4 << 20) - 1}"


def test_download_refreshes_urls_on_403(tmp_path):
    calls = []
    state = {"n": 0}

    async def fresh():
        calls.append(1)
        state["n"] += 1
        return ["https://old/x.m4s"] if state["n"] == 1 else ["https://new/x.m4s"]

    dest = tmp_path / "a.m4s"
    run(downloader(serve(forbidden={"old"})).fetch(fresh, dest))
    assert dest.read_bytes() == BLOB and len(calls) == 2


def test_download_gives_up_and_keeps_partial(tmp_path):
    dest = tmp_path / "a.m4s"
    log = []

    def flaky(req):
        log.append(req.headers.get("range"))
        if len(log) > 3:
            raise httpx.ReadTimeout("stalled", request=req)
        return serve()(req)

    with pytest.raises(bb.DownloadError):
        run(downloader(flaky, max_failures=3).fetch(urls_fn("m1", "m2"), dest))
    assert not dest.exists() and (tmp_path / "a.m4s.part").stat().st_size == 4 << 20


def test_download_lock_keeps_two_downloaders_apart(tmp_path):
    dest = tmp_path / "a.m4s"
    with open(tmp_path / "a.m4s.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(bb.DownloadBusy):
            run(downloader(serve()).fetch(urls_fn("m1"), dest))
    assert not dest.exists()


def test_danmakus_lives():
    def handler(req: httpx.Request):
        assert req.url.path == "/api/v2/channel" and req.url.params["uId"] == "1313650809"
        assert req.headers["origin"] == "https://danmakus.com"
        return httpx.Response(200, json=fixture("dmks_channel.json"))

    lives = run(bb.dmks_lives(httpx.AsyncClient(transport=httpx.MockTransport(handler)), 1313650809))
    assert lives[0] == {"id": "17e996c0-4618-4720-8a92-c0ea27d5f029", "start": KL_LIVE, "stop": 1791021391.871,
                        "title": "【胡闹P房】16点 巴林 排位赛", "danmaku": 10527}
    assert all(x["start"] < (x["stop"] or 2e9) for x in lives)
