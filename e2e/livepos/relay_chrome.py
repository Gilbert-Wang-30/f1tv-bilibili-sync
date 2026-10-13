"""livepos.js + f1token.js in headless Chromium, wired like the real extension: the
background reads the login cookie (a fake JWT set on .formula1.com) and hands the token
only to the offscreen document, which relays positions from the mock F1 server (over TLS,
with AWS-style sticky cookies) to the dev fone server through mock_f1.py's /flaky proxy,
which records what reaches fone. Run by run.sh; env MOCK, MOCK_TLS_BASE, FONE.
"""

import asyncio
import base64
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request

from playwright.async_api import async_playwright

MOCK = os.environ["MOCK"]  # plain http: control endpoints, /flaky proxy to fone
F1 = os.environ["MOCK_TLS_BASE"] + "/signalrcore"  # https / wss, like livetiming.formula1.com
FONE = os.environ["FONE"]
FAST = {"tick": 250, "ping": 1000, "silence": 6000, "probation": 2000, "stall": 3000, "otherSession": 1000,
        "noToken": 500, "authRetry": 3000, "backoff": [200, 400, 800, 1600], "postTimeout": 5000}


def b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


EXP = int(time.time()) + 3600
TOKEN = ".".join([b64url({"alg": "RS256", "typ": "JWT"}), b64url({"exp": EXP, "SubscriptionStatus": "active",
                                                                   "note": "fake, test only"}), b64url("no-signature")])
# What formula1.com stores: encodeURIComponent(JSON.stringify({data: {subscriptionToken}})).
COOKIE = {"name": "login-session", "value": urllib.parse.quote(json.dumps({"data": {"subscriptionToken": TOKEN}}), safe=""),
          "domain": ".formula1.com", "path": "/", "expires": EXP, "httpOnly": False, "secure": True, "sameSite": "Lax"}


def mock(path: str, body: dict | None = None):
    req = urllib.request.Request(MOCK + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


def fone_get(path: str):
    return json.load(urllib.request.urlopen(FONE + path, timeout=10))


async def main() -> int:
    failed = 0
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(), channel="chromium", headless=True,  # full Chromium: the headless shell has no extensions
            args=["--load-extension=/ext", "--disable-extensions-except=/ext", "--ignore-certificate-errors"])
        sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
        ext = sw.url.split("/")[2]
        await ctx.add_cookies([COOKIE])
        page = await ctx.new_page()
        await page.goto(f"chrome-extension://{ext}/page.html")
        await page.evaluate("T.ensure()")

        async def call(kind: str, opts: dict | None = None) -> dict:
            return await page.evaluate("([k, o]) => T.call(k, o)", [kind, opts])

        async def until(what: str, cond, timeout: float = 30) -> dict:
            t = time.time()
            while time.time() - t < timeout:
                st = await call("status")
                if cond(st):
                    return st
                await asyncio.sleep(0.2)
            raise AssertionError(f"timed out waiting for {what}: {st}")

        def start(key: int) -> dict:
            return {"sessionKey": key, "server": MOCK + "/flaky", "f1Base": F1, "timing": FAST}

        async def sse_tls_sticky():
            mock("/_mock/reset", {"session_key": 90101, "tokens": [TOKEN], "sticky": True})
            await call("start", start(90101))
            st = await until("the session end", lambda s: s["state"] == "ended" and s["pending"] == 0)
            log = mock("/_mock/log")
            c = log["conns"][0]
            assert (st["relayed"], st["stored"], st["mode"], st["connects"]) == (722, 722, "sse", 1), st
            assert (c["presented"], c["token"] == TOKEN, c["authorized"]) == ("header", True, True), c
            assert c["cookie_ok"] and c["post_cookie_ok"], "the AWSALBCORS cookie didn't make it back: the ALB would mis-route"
            assert c["origin"] is None or c["origin"].startswith("chrome-extension://"), c["origin"]
            assert log["fone"] and all(f["origin"] == f"chrome-extension://{ext}" for f in log["fone"]), log["fone"][:2]
            sent = [{k: f[k] for k in ("leak", "authorization", "cookie")} for f in log["fone"] if f["leak"] or f["authorization"] or f["cookie"]]
            assert not sent, f"credentials sent to fone: {sent[:3]}"
            assert TOKEN not in json.dumps(st)
            assert await page.evaluate("T.tokenFromPage()") is None, "the background gave the token to a page"
            # 100 s of the recording: 22 cars, 8316 on-track samples (+ the one before the window).
            cars = fone_get("/api/timing/session/90101/positions?from=1791017900&to=1791018000")["cars"]
            assert len(cars) == 22 and sum(len(v) for v in cars.values()) in (8316, 8338), {k: len(v) for k, v in cars.items()}

        async def ws_fallback_tls():
            mock("/_mock/reset", {"session_key": 90102, "tokens": [TOKEN], "auth_sse": "none", "auth_ws": "query"})
            await call("start", start(90102))
            st = await until("the session end", lambda s: s["state"] == "ended" and s["pending"] == 0)
            log = mock("/_mock/log")
            assert (st["relayed"], st["mode"], st["verified"]) == (722, "ws", True), st
            ws = log["conns"][1]
            assert [c["transport"] for c in log["conns"]] == ["sse", "ws"]
            assert (ws["presented"], ws["token"] == TOKEN, ws["origin"]) == ("query", True, f"chrome-extension://{ext}"), ws

        async def signed_out_then_in():
            mock("/_mock/reset", {"session_key": 90103, "tokens": [TOKEN]})
            await ctx.clear_cookies()
            await call("start", start(90103))
            await until("no-token", lambda s: s["state"] == "no-token")
            await asyncio.sleep(1)
            assert mock("/_mock/log")["conns"] == [], "connected without a token"
            await ctx.add_cookies([COOKIE])  # signing in on F1 TV writes the cookie
            st = await until("the session end", lambda s: s["state"] == "ended" and s["pending"] == 0)
            assert st["relayed"] == 722, st

        async def stop_closes():
            mock("/_mock/reset", {"session_key": 90104, "tokens": [TOKEN], "rate": 10, "end": False})
            await call("start", start(90104))
            await until("positions relayed", lambda s: s["relayed"] > 0)
            st = await call("stop")
            await asyncio.sleep(1.5)
            log = mock("/_mock/log")
            assert st["state"] == "stopped" and log["conns"][0]["closed_by"] == "client", (st, log["conns"])
            posts = len(log["fone"])
            await asyncio.sleep(1.5)
            assert len(mock("/_mock/log")["fone"]) == posts, "posted after stop()"

        for name, f in [("SSE over TLS: Authorization header, sticky ALB cookie, Origin chrome-extension://", sse_tls_sticky),
                        ("WebSocket over TLS with access_token after SSE brought no positions", ws_fallback_tls),
                        ("signed out: no connection; signing in resumes within a re-check", signed_out_then_in),
                        ("stop() closes the connection and posting", stop_closes)]:
            try:
                await f()
                print("ok -", name, flush=True)
            except Exception as e:  # noqa: BLE001 - report and go on
                failed += 1
                print("FAIL -", name, "\n", repr(e), flush=True)
                await call("stop")
        await ctx.close()
    print(f"{4 - failed}/4 passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if asyncio.run(main()) else 0)
