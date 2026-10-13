"""Mock of F1's live timing SignalR Core endpoint, for the livepos.js integration tests.

Speaks what the real one does (checked against livetiming.formula1.com/signalrcore on
2026-10-09, anonymously): POST /signalrcore/negotiate (negotiateVersion 1, AWSALB/AWSALBCORS
cookies), Server-Sent Events (GET ?id=, client frames POSTed to ?id=) or WebSockets (with or
without negotiate), JSON frames ending in \\x1e, Subscribe -> completion with each topic's
current state, then "feed" invocations [topic, payload, utc] and pings. Unauthorised
subscribers get no Position.z at all, exactly like the real server.

What the scenario controls (POST /_mock/reset): which tokens are accepted or refused (401),
how the token must be presented per transport (Authorization header / access_token query),
cookie stickiness, the session the feed is on, connection drops, position stalls, the
session's end. The Position.z messages are a recording from the static archive.

Also a flaky proxy in front of the fone server (/flaky/api/...): fails the first N POSTs
with 503 and logs what the browser sends there, so the tests can check that no token ever
reaches fone. GET /_mock/log returns everything observed.

    python mock_f1.py POSITION_Z_JSONSTREAM[.gz]   (env FONE_URL, MOCK_TLS=1 for :8443)
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import secrets
import subprocess
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession, WSMsgType, web

RS = "\x1e"
FONE_URL = os.environ.get("FONE_URL", "http://fone-dev-livepos-server:8788")
SESSION_INFO = {"Meeting": {"Key": 1308, "Name": "Bahrain Grand Prix"}, "Type": "Qualifying", "Name": "Qualifying",
                "Path": "2026/2026-10-04_Bahrain_Grand_Prix/2026-10-03_Qualifying/"}
DEFAULT = {
    "session_key": 11730, "status": "Started",
    "tokens": [],  # accepted (Position.z sent)
    "refuse": [],  # answered 401 (negotiate, SSE GET, WS upgrade)
    "auth_sse": "header",  # how SSE accepts a token: header | query | none
    "auth_ws": "query",  # how WebSockets accept a token: query | none
    "sticky": False,  # SSE requests must carry negotiate's AWSALBCORS cookie (else 404, like a mis-routed ALB)
    "rate": 40.0,  # Position.z messages per second (the real feed: ~1)
    "ping": 1.0,
    "overlap": 5,  # a reconnect resumes this many messages back (the keyframe repeats the last one too)
    "drop_after": 0,  # the first authorised connection is cut after this many feed messages
    "stall_after": 0,  # ... or its positions stop (pings go on)
    "end": True,  # after the last message: SessionStatus Finalised + Ends
    "fail_fone": 0,  # the flaky proxy answers 503 to this many POSTs first
    "fone_archived": False,  # ... or answers like a fone server whose session archive is installed
}


def load_messages(path: str) -> tuple[list[str], list[str]]:
    raw = open(path, "rb").read()
    if path.endswith(".gz"):
        raw = gzip.decompress(raw)
    lines = [ln for ln in raw.decode("utf-8-sig").splitlines() if len(ln) > 12]
    t0 = datetime(2026, 10, 3, 7, 47, 14, 597000, tzinfo=timezone.utc)  # the archive's t0 (AudioStreams Utc)
    msgs, utcs = [], []
    for ln in lines:
        h, m, s = ln[:12].split(":")
        off = timedelta(hours=int(h), minutes=int(m), seconds=float(s))
        msgs.append(json.loads(ln[12:]))
        utcs.append((t0 + off).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
    return msgs, utcs


class Conn:
    """One SignalR connection, whichever transport carries it."""

    def __init__(self, mock: Mock, transport: str, cid: str):
        self.mock = mock
        self.out: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = asyncio.Event()
        self.bg: list[asyncio.Task] = []  # feed + pinger: cancelled on close (the writer drains to the None)
        self.authorized = False
        self.log = {"id": cid, "transport": transport, "host": None, "negotiated": False, "presented": None, "token": None,
                    "authorized": False, "cookie_ok": None, "post_cookie_ok": True, "post_auth": [], "handshake": False,
                    "subscribe": None, "pings": 0, "positions_sent": 0, "closed_by": None, "origin": None}

    def send(self, obj) -> None:
        self.out.put_nowait(json.dumps(obj, separators=(",", ":")) + RS)

    def close(self, by: str) -> None:
        if not self.closed.is_set():
            self.log["closed_by"] = self.log["closed_by"] or by
            self.closed.set()
            self.out.put_nowait(None)
            for t in self.bg:
                t.cancel()

    def on_client(self, text: str) -> None:
        for part in text.split(RS):
            if not part.strip():
                continue
            m = json.loads(part)
            typ = m.get("type")
            if typ is None and m.get("protocol") == "json":
                self.log["handshake"] = True
                self.send({})
            elif typ == 1 and m.get("target") == "Subscribe":
                self.log["subscribe"] = m["arguments"][0]
                self.subscribed(m.get("invocationId"))
            elif typ == 6:
                self.log["pings"] += 1
            elif typ == 7:
                self.close("client")

    def subscribed(self, inv) -> None:
        mock, sc = self.mock, self.mock.sc
        result = {"SessionInfo": {**SESSION_INFO, "Key": sc["session_key"], "_kf": True},
                  "SessionStatus": {"Status": sc["status"], "_kf": True}}
        if self.authorized:
            mock.authorized_conns += 1
            self.nth = mock.authorized_conns
            result["Position.z"] = mock.msgs[max(mock.cursor - 1, 0)]  # the topic's current state
            mock.cursor = max(mock.cursor, 1)
        self.send({"type": 3, "invocationId": inv, "result": result})
        self.bg.append(asyncio.create_task(self.feed()))

    async def feed(self) -> None:
        mock, sc = self.mock, self.mock.sc
        if not self.authorized:
            return  # no Position.z for you; pings only
        sent = 0
        start = max(1, mock.cursor - sc["overlap"]) if mock.cursor > 1 else 1
        for i in range(start, len(mock.msgs)):
            await asyncio.sleep(1 / sc["rate"])
            self.send({"type": 1, "target": "feed", "arguments": ["Position.z", mock.msgs[i], mock.utcs[i]]})
            self.log["positions_sent"] += 1
            mock.cursor = max(mock.cursor, i + 1)
            sent += 1
            if self.nth == 1 and sc["drop_after"] and sent >= sc["drop_after"]:
                await asyncio.sleep(0.05)
                return self.close("server")
            if self.nth == 1 and sc["stall_after"] and sent >= sc["stall_after"]:
                return
        if sc["end"]:
            for st in ("Finished", "Finalised", "Ends"):
                await asyncio.sleep(0.05)
                self.send({"type": 1, "target": "feed", "arguments": ["SessionStatus", {"Status": st}, mock.utcs[-1]]})

    async def pinger(self) -> None:
        while True:
            await asyncio.sleep(self.mock.sc["ping"])
            self.send({"type": 6})


class Mock:
    def __init__(self, msgs, utcs):
        self.msgs, self.utcs = msgs, utcs
        self.reset({})

    def reset(self, sc: dict) -> None:
        for c in getattr(self, "conns", {}).values():
            c.close("reset")
        self.sc = {**DEFAULT, **sc}
        self.conns: dict[str, Conn] = {}
        self.all: list[Conn] = []
        self.negotiates: list[dict] = []
        self.fone: list[dict] = []
        self.cursor = 0
        self.authorized_conns = 0
        self.fone_failed = 0

    def token_state(self, token: str | None) -> str:
        if token and token in self.sc["refuse"]:
            return "refused"
        return "ok" if token and token in self.sc["tokens"] else "anonymous"

    def new_conn(self, transport: str, cid: str | None = None) -> Conn:
        c = Conn(self, transport, cid or secrets.token_urlsafe(16)[:22])
        self.conns[c.log["id"]] = c
        self.all.append(c)
        return c

    def tokens_in(self, text: str) -> bool:
        return any(t and t in text for t in [*self.sc["tokens"], *self.sc["refuse"]])


def bearer(req: web.Request) -> str | None:
    h = req.headers.get("Authorization", "")
    return h[7:] if h.startswith("Bearer ") else None


def make_app(mock: Mock) -> web.Application:
    routes = web.RouteTableDef()

    @routes.post("/signalrcore/negotiate")
    async def negotiate(req: web.Request):
        token = bearer(req)
        state = mock.token_state(token)
        mock.negotiates.append({"token": token, "state": state, "origin": req.headers.get("Origin")})
        if state == "refused":
            return web.Response(status=401)
        c = mock.new_conn("sse")
        c.log["negotiated"] = True
        c.cookie = secrets.token_hex(16)
        resp = web.json_response({"negotiateVersion": 1, "connectionId": c.log["id"], "connectionToken": c.log["id"],
                                  "availableTransports": [{"transport": "WebSockets", "transferFormats": ["Text", "Binary"]},
                                                          {"transport": "ServerSentEvents", "transferFormats": ["Text"]}]})
        secure = req.secure
        resp.set_cookie("AWSALB", secrets.token_hex(16), path="/")
        resp.set_cookie("AWSALBCORS", c.cookie, path="/", secure=secure, samesite="None" if secure else None)
        return resp

    @routes.get("/signalrcore")
    async def connect(req: web.Request):
        if req.headers.get("Upgrade", "").lower() == "websocket":
            return await websocket(req)
        return await sse(req)

    async def sse(req: web.Request):
        c = mock.conns.get(req.query.get("id", ""))
        if c is None or c.log["transport"] != "sse":
            return web.Response(status=404, text="No Connection with that ID")
        c.log["cookie_ok"] = req.cookies.get("AWSALBCORS") == c.cookie
        if mock.sc["sticky"] and not c.log["cookie_ok"]:
            return web.Response(status=404, text="No Connection with that ID")  # another server behind the ALB
        how = mock.sc["auth_sse"]
        token = bearer(req) if how == "header" else req.query.get("access_token") if how == "query" else None
        c.log.update(presented=how if token else None, token=token, origin=req.headers.get("Origin"), host=req.host)
        if mock.token_state(token) == "refused":
            return web.Response(status=401)
        c.authorized = c.log["authorized"] = mock.token_state(token) == "ok"
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(req)
        await resp.write(b":\r\n")
        c.bg.append(asyncio.create_task(c.pinger()))
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(c.out.get(), 0.2)
                except asyncio.TimeoutError:
                    if req.transport is None or req.transport.is_closing():
                        break  # the client went away (noticed within 0.2 s, not at the next write)
                    continue
                if frame is None:
                    break
                await resp.write(b"data: " + frame.encode() + b"\r\n\r\n")
        except ConnectionResetError:
            pass  # the client went away
        finally:
            c.close("client")
        return resp

    @routes.post("/signalrcore")
    async def client_frames(req: web.Request):
        c = mock.conns.get(req.query.get("id", ""))
        if c is None or c.closed.is_set():
            return web.Response(status=404, text="No Connection with that ID")
        if req.cookies.get("AWSALBCORS") != getattr(c, "cookie", None):
            c.log["post_cookie_ok"] = False
            if mock.sc["sticky"]:
                return web.Response(status=404, text="No Connection with that ID")
        c.log["post_auth"].append(bearer(req) == c.log["token"] if c.log["token"] else bearer(req) is not None)
        c.on_client(await req.text())
        return web.Response(status=200)

    async def websocket(req: web.Request):
        token = req.query.get("access_token") if mock.sc["auth_ws"] == "query" else None
        state = mock.token_state(req.query.get("access_token"))
        if state == "refused":
            return web.Response(status=401)
        c = mock.conns.get(req.query.get("id", "")) or mock.new_conn("ws")
        c.log.update(transport="ws", presented="query" if token else None, token=req.query.get("access_token"),
                     origin=req.headers.get("Origin"), host=req.host)
        c.authorized = c.log["authorized"] = mock.token_state(token) == "ok"
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(req)

        async def writer():
            while True:
                frame = await c.out.get()
                if frame is None:
                    break
                await ws.send_str(frame)
            await ws.close()

        c.bg.append(asyncio.create_task(c.pinger()))
        write_task = asyncio.create_task(writer())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    c.on_client(msg.data)
        finally:
            c.close("client")
            await asyncio.wait([write_task], timeout=2)
        return ws

    @routes.route("*", "/flaky/{tail:.*}")
    async def flaky(req: web.Request):
        body = await req.read()
        text = body.decode("utf-8", "replace")
        entry = {"method": req.method, "path": req.path, "at": time.time(), "origin": req.headers.get("Origin"),
                 "authorization": "Authorization" in req.headers, "cookie": "Cookie" in req.headers,
                 "leak": mock.tokens_in(str(req.url)) or mock.tokens_in(json.dumps(dict(req.headers))) or mock.tokens_in(text),
                 "messages": len(json.loads(text).get("messages", [])) if text.startswith("{") else None}
        mock.fone.append(entry)
        if req.method == "POST" and mock.sc["fone_archived"]:
            entry["status"] = 200
            return web.json_response({"ok": True, "stored": 0, "reason": "archived"})
        if req.method == "POST" and mock.fone_failed < mock.sc["fail_fone"]:
            mock.fone_failed += 1
            entry["status"] = 503
            return web.Response(status=503, text="flaky")
        async with ClientSession() as s:
            async with s.request(req.method, FONE_URL + "/" + req.match_info["tail"], params=req.query, data=body,
                                 headers={"Content-Type": req.headers.get("Content-Type", "application/json")}) as r:
                entry["status"] = r.status
                return web.Response(status=r.status, body=await r.read(), content_type=r.content_type)

    @routes.post("/_mock/reset")
    async def reset(req: web.Request):
        mock.reset(await req.json())
        return web.json_response({"ok": True, "messages": len(mock.msgs)})

    @routes.get("/_mock/log")
    async def log(req: web.Request):
        return web.json_response({"negotiates": mock.negotiates, "conns": [c.log for c in mock.all], "fone": mock.fone,
                                  "cursor": mock.cursor, "messages": len(mock.msgs)})

    @routes.get("/_mock/health")
    async def health(req: web.Request):
        return web.json_response({"ok": True})

    app = web.Application()
    app.add_routes(routes)
    return app


def tls_context() -> ssl.SSLContext:
    """Self-signed certificate for :8443 (the browser test runs with --ignore-certificate-errors):
    AWS's AWSALBCORS cookie is SameSite=None; Secure, which only exists over https."""
    d = "/tmp/mock-tls"
    os.makedirs(d, exist_ok=True)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj",
                    "/CN=fone-dev-livepos-mock", "-addext", "subjectAltName=DNS:fone-dev-livepos-mock",
                    "-keyout", f"{d}/key.pem", "-out", f"{d}/cert.pem"], check=True, capture_output=True)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(f"{d}/cert.pem", f"{d}/key.pem")
    return ctx


async def main(path: str) -> None:
    msgs, utcs = load_messages(path)
    runner = web.AppRunner(make_app(Mock(msgs, utcs)), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()
    await web.TCPSite(runner, "0.0.0.0", 8081).start()  # a second origin, for the /flaky proxy to fone
    if os.environ.get("MOCK_TLS") == "1":
        ctx = tls_context()
        await web.TCPSite(runner, "0.0.0.0", 8443, ssl_context=ctx).start()
        # 443 too: the integrated-extension test maps livetiming.formula1.com here (--host-resolver-rules).
        await web.TCPSite(runner, "0.0.0.0", 443, ssl_context=ctx).start()
    print(f"mock F1 live timing: {len(msgs)} Position.z messages", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
