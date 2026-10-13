"""The fone extension itself, once livepos is integrated (offscreen.html loads livepos.js):
the real background.js (token from the login cookie via f1token.js) and the real offscreen
engine, driven like a content script on a LIVE F1 TV video. livetiming.formula1.com is mapped
to the mock (--host-resolver-rules; the test network has no way out anyway), and every
request the extension makes to fone goes through the mock's logging proxy, so the test sees
that the token reaches F1 (the mock) and nothing of it reaches fone.
Run by run.sh when the extension under test is integrated; env MOCK, FONE, CID, SESSION.
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

MOCK = os.environ["MOCK"]  # http://fone-dev-livepos-mock:8080 (control)
FONE_VIA_PROXY = MOCK.rsplit(":", 1)[0] + ":8081/flaky"  # the engine's "fone server": logs, then forwards
FONE = os.environ["FONE"]
CID = os.environ["CID"]  # seeded as LIVE content with sessionKey SESSION
SESSION = int(os.environ["SESSION"])
MOCK_HOST = urllib.parse.urlparse(MOCK).hostname


def b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


EXP = int(time.time()) + 3600
TOKEN = ".".join([b64url({"alg": "RS256", "typ": "JWT"}), b64url({"exp": EXP, "note": "fake, test only"}), b64url("x")])
COOKIE = {"name": "login-session", "value": urllib.parse.quote(json.dumps({"data": {"subscriptionToken": TOKEN}}), safe=""),
          "domain": ".formula1.com", "path": "/", "expires": EXP, "httpOnly": False, "secure": True, "sameSite": "Lax"}

VIDEO = """(servers) => chrome.runtime.sendMessage({
  target: 'engine', type: 'video', cid: '%s', channel: '',
  video: { p: 1000, paused: false, seeking: false, waiting: false, rate: 1, ready: true, live: true, seekEnd: 1030,
           src: 'tiledmedia', at: Date.now() / 1000 },
  settings: { servers, volume: 1, muted: true } })""" % CID


def mock(path: str, body: dict | None = None):
    req = urllib.request.Request(MOCK + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


async def main() -> int:
    mock("/_mock/reset", {"session_key": SESSION, "tokens": [TOKEN], "sticky": True, "end": False, "rate": 20})
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(), channel="chromium", headless=True,
            args=["--load-extension=/ext", "--disable-extensions-except=/ext", "--ignore-certificate-errors",
                  f"--host-resolver-rules=MAP livetiming.formula1.com {MOCK_HOST}", "--autoplay-policy=no-user-gesture-required"])
        sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
        ext = sw.url.split("/")[2]
        await ctx.add_cookies([COOKIE])
        # An extension page in a tab stands in for the content script (the background relays
        # 'engine' messages from tabs) and receives the engine's status reports.
        page = await ctx.new_page()
        await page.goto(f"chrome-extension://{ext}/options.html")
        await page.evaluate("""() => { window.__st = []; chrome.runtime.onMessage.addListener((m) => {
            if (m?.type === 'fone-status') window.__st.push(m.status); }); }""")

        async def feed_video(seconds: float, cond=None):
            t = time.time()
            while time.time() - t < seconds:
                await page.evaluate(VIDEO, [FONE_VIA_PROXY])
                last = await page.evaluate("() => window.__st[window.__st.length - 1] || null")
                if cond and last and cond(last):
                    return last
                await asyncio.sleep(0.5)
            return await page.evaluate("() => window.__st[window.__st.length - 1] || null")

        failed = 0

        def check(name: str, ok: bool, detail=""):
            nonlocal failed
            print(("ok - " if ok else "FAIL - ") + name + ("" if ok else f"\n   {detail}"), flush=True)
            failed += 0 if ok else 1

        last = await feed_video(40, lambda s: (s.get("livepos") or {}).get("relayed", 0) >= 200)
        lp = (last or {}).get("livepos") or {}
        check("LIVE content starts the relay; positions reach fone", lp.get("relayed", 0) >= 200 and lp.get("state") == "live", last)
        log = mock("/_mock/log")
        c = (log["conns"] or [{}])[0]
        check("the real F1 URL, token as Authorization header, from the cookie via the background",
              (c.get("host"), c.get("transport"), c.get("presented"), c.get("token") == TOKEN, c.get("authorized"))
              == ("livetiming.formula1.com", "sse", "header", True, True), c)
        check("the ALB cookie round-trips (sticky routing)", bool(c.get("cookie_ok") and c.get("post_cookie_ok")), c)
        fone = log["fone"]
        paths = {f["path"].split("?")[0] for f in fone}
        check("every request to fone is free of the token and of credentials",
              fone and not any(f["leak"] or f["authorization"] or f["cookie"] for f in fone),
              [f for f in fone if f["leak"] or f["authorization"] or f["cookie"]][:3])
        check("the engine and the relay share the fone server",
              {"/flaky/api/health", "/flaky/api/channels", f"/flaky/api/f1tv/{CID}", f"/flaky/api/timing/session/{SESSION}/positions"} <= paths,
              sorted(paths))
        statuses = await page.evaluate("() => JSON.stringify(window.__st)")
        check("status reports to the tab never carry the token", TOKEN not in statuses)

        await page.evaluate("() => chrome.runtime.sendMessage({ target: 'engine', type: 'detach' })")
        await asyncio.sleep(2)
        stopped_at = time.time()
        log = mock("/_mock/log")
        check("detach stops the relay: F1 connection closed", log["conns"][0]["closed_by"] == "client", log["conns"][0])
        await asyncio.sleep(2)
        later = [f for f in mock("/_mock/log")["fone"] if f["at"] > stopped_at and "/positions" in f["path"]]
        check("detach stops the relay: no more positions posted", not later, later[:2])
        cars = json.load(urllib.request.urlopen(f"{FONE}/api/timing/session/{SESSION}/positions?from=1791017843&to=1791017960"))["cars"]
        check("fone serves the relayed positions back", len(cars) >= 20, {k: len(v) for k, v in cars.items()})
        await ctx.close()
    total = 9
    print(f"{total - failed}/{total} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if asyncio.run(main()) else 0)
