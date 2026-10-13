"""Bilibili's live danmaku feed (the web player's websocket), listened to anonymously.

  getDanmuInfo (WBI-signed, with a buvid3 cookie) -> token + hosts
  wss://<host>/sub: auth packet (op 7), heartbeat (op 2) every 30 s, messages (op 5) arrive
  as JSON, or bundles of packets compressed with zlib (protover 2) or brotli (protover 3)

Every packet: 16-byte header (total length, header length 16, protover, op, sequence) + body.
Anonymous listeners get nicknames masked and uids as 0 (LOG_IN_NOTICE: 为保护用户隐私，未登录
无法查看他人昵称); everything else in a DANMU_MSG is real (user hash, levels, medal, colour).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
import time
import urllib.parse
import zlib
from collections.abc import Awaitable, Callable

import httpx

try:
    import brotli  # protover 3; without it we ask for zlib (protover 2)
except ImportError:  # pragma: no cover
    brotli = None

log = logging.getLogger("fone.danmaku")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
# WBI signing: the mixin key is img_key + sub_key (from /nav) permuted by this table
WBI_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14,
           39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6,
           63, 57, 62, 11, 36, 20, 34, 44, 52]
OP_HEARTBEAT, OP_MESSAGE, OP_AUTH = 2, 5, 7
HEARTBEAT_EVERY = 30


def wbi_sign(params: dict, img_key: str, sub_key: str, now: float | None = None) -> dict:
    key = "".join((img_key + sub_key)[i] for i in WBI_TAB)[:32]
    p = dict(sorted({**params, "wts": int(now if now is not None else time.time())}.items()))
    p = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in p.items()}
    return {**p, "w_rid": hashlib.md5((urllib.parse.urlencode(p) + key).encode()).hexdigest()}


def packet(op: int, body: bytes | dict, ver: int = 1) -> bytes:
    b = body if isinstance(body, bytes) else json.dumps(body).encode()
    return struct.pack(">IHHII", 16 + len(b), 16, ver, op, 1) + b


def decode(data: bytes) -> list[dict]:
    """Every JSON message in a frame (bundles unpacked); anything unreadable is skipped."""
    out: list[dict] = []
    i = 0
    while i + 16 <= len(data):
        n, hl, ver, op, _ = struct.unpack(">IHHII", data[i:i + 16])
        if n < 16:
            break
        body = data[i + hl:i + n]
        i += n
        if op != OP_MESSAGE:
            continue
        try:
            if ver == 2:
                out.extend(decode(zlib.decompress(body)))
            elif ver == 3:
                if brotli is not None:
                    out.extend(decode(brotli.decompress(body)))
            else:
                m = json.loads(body)
                if isinstance(m, dict):
                    out.append(m)
        except Exception as e:  # zlib.error, brotli.error, bad JSON: skip the packet
            log.debug("undecodable danmaku packet: %s", e)
    return out


class Feed:
    """One room's feed: `run` connects, hands every message batch to `on_messages(rx, msgs)`
    and calls `on_beat(event)` with "open" once connected and "beat" at every heartbeat;
    it returns (or raises) when the connection ends."""

    def __init__(self, room: int, http: httpx.AsyncClient | None = None):
        self.room = room
        self.http = http or httpx.AsyncClient(timeout=20, headers={"User-Agent": UA})
        self.connected = False
        self.last_rx = 0.0

    async def close(self) -> None:
        await self.http.aclose()

    async def _token(self) -> tuple[str, str, dict]:
        h = self.http
        ref = {"Referer": f"https://live.bilibili.com/{self.room}"}
        b3 = (await h.get("https://api.bilibili.com/x/frontend/finger/spi", headers=ref)).json()["data"]["b_3"]
        h.cookies.set("buvid3", b3, domain=".bilibili.com")
        wbi = (await h.get("https://api.bilibili.com/x/web-interface/nav", headers=ref)).json()["data"]["wbi_img"]
        img, sub = (wbi[k].rsplit("/", 1)[1].split(".")[0] for k in ("img_url", "sub_url"))
        r = (await h.get("https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo", headers=ref,
                         params=wbi_sign({"id": self.room, "type": 0, "web_location": "444.8"}, img, sub))).json()
        if r.get("code") != 0:
            raise RuntimeError(f"getDanmuInfo: code {r.get('code')} {r.get('message')}")
        return b3, r["data"]["token"], r["data"]["host_list"][0]

    async def run(self, on_messages: Callable[[float, list[dict]], Awaitable[None] | None],
                  on_beat: Callable[[str], None]) -> None:
        import websockets  # only the capture needs it

        b3, token, host = await self._token()
        url = f"wss://{host['host']}:{host['wss_port']}/sub"
        # ping_interval=None: the server answers its own heartbeat packets, not websocket pings
        async with websockets.connect(url, ping_interval=None, max_size=2 ** 24, open_timeout=20,
                                      additional_headers={"User-Agent": UA, "Origin": "https://live.bilibili.com"}) as ws:
            await ws.send(packet(OP_AUTH, {"uid": 0, "roomid": self.room, "protover": 3 if brotli else 2,
                                           "buvid": b3, "platform": "web", "type": 2, "key": token}))
            self.connected = True
            on_beat("open")

            async def beat() -> None:
                while True:
                    await ws.send(packet(OP_HEARTBEAT, b"[object Object]"))
                    on_beat("beat")
                    await asyncio.sleep(HEARTBEAT_EVERY)

            hb = asyncio.create_task(beat())
            try:
                async for raw in ws:
                    if not isinstance(raw, bytes):
                        continue
                    msgs = decode(raw)
                    if msgs:
                        self.last_rx = time.time()
                        r = on_messages(self.last_rx, msgs)
                        if asyncio.iscoroutine(r):
                            await r
            finally:
                self.connected = False
                hb.cancel()
