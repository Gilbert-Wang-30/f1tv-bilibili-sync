"""Stats dashboard (extension/dashboard.html) in headless Chromium against a fone server.

Renders the scenarios below, standalone and as the iframe behind F1 TV's player at a few
screen sizes, and saves a screenshot of each; checks along the way that the page never shows
data from after its clock, follows only its own tab's player, stays out of the player's box,
and keeps up with seeks and playback. In the iframe the tower ends on the player's bottom edge,
and two onboard slots with driver pickers tell the page where to show F1 TV's onboard views
('onboards' command); the host page draws a translucent stand-in there, as page.js would show
the video. Run through e2e/dash_shots.sh (Docker, --network host).
Uses real timing: 2026 Singapore Sprint Qualifying (session 11379) and FP1 (11378). The pages
read the server through a GET-only forwarder on this box (FAKE_PORT) that adds an onboard
stream list to the SQ content while the server has none, answers a content id of its own with
no list at all (a server from before them), and makes up a race (RACE) from SQ's data.
"""

import asyncio
import copy
import http.server
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from playwright.async_api import async_playwright

SERVER = os.environ.get("DASH_SERVER", "http://localhost:8788")
ONLY = os.environ.get("DASH_ONLY", "")
PROXY = os.environ.get("DASH_NOPROXY", "0") != "1"
FAKE_PORT = int(os.environ.get("DASH_FAKE_PORT", "8798"))  # dev ports: 8795-8799
FWD = f"http://localhost:{FAKE_PORT}"
OUT = "/out"
SQ, FP1 = 11379, 11378
RACE = 99999  # no race on the server yet: a race-shaped state made from SQ's (race_state)
CID = "1000010389"  # F1 TV: 2026 Singapore GP - Sprint Qualifying
PLAIN_CID = "1000000001"  # the forwarder's: SQ's content without a stream list
T_SQ1 = 1791550150

# name, session, E, (viewport w, h), player rect (None: standalone window), options:
#   picks: the onboard choices in storage ([num|None, ...] up to 4; default none)
#   cid: the F1 TV content the clock reports (default CID; None: no content)
# (layout.js: 77% of the width in whole 16x9 px steps, full height on very wide screens)
P1080 = {"x": 0, "y": 0, "w": 1472, "h": 828}
P1147 = {"x": 0, "y": 0, "w": 1504, "h": 846}  # 1948x1147: the user's PC
P1440 = {"x": 0, "y": 0, "w": 1968, "h": 1107}
P2560x1080 = {"x": 0, "y": 0, "w": 1920, "h": 1080}
P768 = {"x": 0, "y": 0, "w": 1056, "h": 594}
PMAC = {"x": 0, "y": 0, "w": 960, "h": 540}  # the user's Mac window, 1248x739
PMBP = {"x": 0, "y": 0, "w": 1168, "h": 657}  # MacBook Pro 14 full screen, 1512x857
SCENARIOS = [
    ("standalone_sq1_running", SQ, T_SQ1, (1280, 900), None, {}),
    ("iframe1080_sq1_running", SQ, T_SQ1, (1920, 1080), P1080, {}),
    ("iframe1080_sq1_onboards", SQ, T_SQ1, (1920, 1080), P1080, {"picks": [1, 44, 16, 81]}),
    ("iframe1080_sq1_redflag", SQ, 1791549400, (1920, 1080), P1080, {"picks": [16, 81]}),
    ("iframe1080_sq1_restart_wait", SQ, 1791549902, (1920, 1080), P1080, {"picks": [63, None]}),
    ("iframe1147_sq1_onboards", SQ, T_SQ1, (1948, 1147), P1147, {"picks": [16, 44, 1]}),
    ("iframe1440_sq2_running", SQ, 1791551700, (2560, 1440), P1440, {"picks": [3, 12]}),
    ("iframe2560x1080_sq3_end", SQ, 1791552955, (2560, 1080), P2560x1080, {"picks": [1, 44]}),  # a column: no slots
    ("iframe1080_sq3_chequered", SQ, 1791553000, (1920, 1080), P1080, {"picks": [16, 1]}),
    ("iframe1440_session_over", SQ, 1791560000, (2560, 1440), P1440, {}),
    ("standalone_session_over", SQ, 1791560000, (1280, 900), None, {}),
    ("iframe1080_fp1_vsc", FP1, 1791537000, (1920, 1080), P1080, {"cid": PLAIN_CID, "picks": [1, 44]}),  # no list: "unavailable"
    ("standalone_narrow_fp1", FP1, 1791535800, (620, 1000), None, {}),
    ("iframe1080_sq1_double_yellow", SQ, 1791549260, (1920, 1080), P1080, {"picks": [None, 30]}),
    ("standalone_sq1_double_yellow", SQ, 1791549260, (1280, 900), None, {}),
    ("standalone_race_sc", RACE, T_SQ1, (1280, 900), None, {}),
    ("iframe1080_race_sc", RACE, T_SQ1, (1920, 1080), P1080, {"picks": [44, 16]}),
    ("iframe1200_sq1_double_yellow", SQ, 1791549260, (1920, 1200), P1080, {"picks": [44, 10]}),
    ("iframe768_sq2_running", SQ, 1791551700, (1366, 768), P768, {"picks": [1, None]}),
    ("iframe_mac1248_sq1_running", SQ, T_SQ1, (1248, 739), PMAC, {"picks": [44, 87]}),  # Bearman: no onboard here
    ("iframe_mbp1512_sq1_running", SQ, T_SQ1, (1512, 857), PMBP, {"picks": [63, 1, 44, 16]}),
    # a short wide window (1440p with the browser's bars): 'stats' mode has no room for onboards,
    # 'onboards' mode shrinks the picture to the dashboard's box (FoneDash.onboardBox: 1792x1008)
    ("iframe2560x1270_stats_mode", SQ, T_SQ1, (2560, 1270), {"x": 0, "y": 0, "w": 1968, "h": 1107}, {"picks": [1, 44, 16, 81]}),
    ("iframe2560x1270_onboards_mode", SQ, T_SQ1, (2560, 1270), {"x": 0, "y": 0, "w": 1968, "h": 1107},
     {"picks": [1, 44, 16, 81], "mode": "onboards", "box": {"w": 1792, "h": 1008}}),
    ("popout560_sq1_running", SQ, T_SQ1, (560, 1000), None, {}),  # background.js's pop-out window
]


def get(path):
    return json.load(urllib.request.urlopen(SERVER + path, timeout=20))


# What a race looks like on the tower: lap counter, gaps to the leader with lapped cars, pit
# stops, a retirement, the safety car. Built from SQ's state at the same t (same cars, same
# track, so the map still has real positions).
def race_state(t):
    st = copy.deepcopy(get(f"/api/timing/session/{SQ}/state?t={t}"))
    st["session"] = {"key": RACE, "name": "Race", "type": "Race", "meeting": "Singapore Grand Prix"}
    st["status"], st["part"] = "Started", None
    st["clock"] = {"remaining": "84:12", "lap": 23, "laps": 62}
    st["track"] = {"status": "SCDeployed", "flag": "SC", "message": None}
    gap = 0.0
    for i, r in enumerate(st["tower"]):
        step = 0.4 + (i * 0.37) % 1.9
        gap += step
        r.update(pos=i + 1, knockedOut=False, inPit=False, pitOut=False, retired=False,
                 gap="LAP 23" if i == 0 else f"+{gap:.3f}", interval="LAP 23" if i == 0 else f"+{step:.3f}",
                 last=f"1:{40 + i % 7}.{(i * 137) % 1000:03d}", lastFlag="personal" if i % 5 == 2 else "none",
                 best=f"1:{38 + i % 5}.{(i * 251) % 1000:03d}", pits=1 + i % 2, tyre=["HARD", "MEDIUM", "HARD", "SOFT"][i % 4],
                 tyreAge=3 + (i * 7) % 20, newTyre=i % 6 == 1)
    st["tower"][0]["lastFlag"] = "overall"
    st["tower"][17].update(gap="1L", interval="+1 LAP")
    st["tower"][18].update(gap="1L", interval="+12.402")
    st["tower"][19].update(inPit=True)
    st["tower"][20].update(pitOut=True, tyre="INTERMEDIATE", tyreAge=0, newTyre=True)
    st["tower"][21].update(retired=True, gap="", interval="", last="", tyre="WET", pits=3)
    return st


# The SQ content's feeds as the server will list them (/api/f1tv/{cid} "streams"): the channel
# ids its channelOffsets name, four other feeds and one onboard per driver, except Bearman's
# (a driver F1 TV carries no onboard of: the slot says so).
NO_ONBOARD = 87


def fake_streams():
    ids = sorted(int(c) for c in get(f"/api/f1tv/{CID}").get("channelOffsets", {})) or list(range(1003, 1029))
    other = {1003: "F1 LIVE", 1033: "INTERNATIONAL", 1004: "TRACKER", 1025: "DATA"}
    drivers = sorted(get(f"/api/timing/session/{SQ}/state?t={T_SQ1}")["tower"], key=lambda r: int(r["num"]))
    out = [{"channelId": c, "type": "additional", "title": t, "number": None, "name": None, "team": None, "color": None}
           for c, t in other.items()]
    for c, r in zip([c for c in ids if c not in other], drivers):
        if int(r["num"]) != NO_ONBOARD:
            out.append({"channelId": c, "type": "obc", "title": r["code"], "number": int(r["num"]), "name": r.get("name"),
                        "team": r.get("team"), "color": r.get("color")})
    return out


STREAMS = fake_streams()
CHANNEL = {s["number"]: s["channelId"] for s in STREAMS if s["type"] == "obc"}  # racing number -> channel
CODE = {s["channelId"]: s["title"] for s in STREAMS}


# GET only, like the dashboard: everything goes to SERVER but for the made-up bits above.
class Forward(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        content = re.fullmatch(r"/api/f1tv/(\d+)", path)
        race = path.startswith(f"/api/timing/session/{RACE}/")
        try:
            if race and path.endswith("/state"):
                t = float(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)["t"][0])
                status, body = 200, json.dumps(race_state(t)).encode()
            else:
                upstream = self.path.replace(f"/{RACE}/", f"/{SQ}/") if race else self.path
                if content and content.group(1) == PLAIN_CID:
                    upstream = f"/api/f1tv/{CID}"
                with urllib.request.urlopen(SERVER + upstream, timeout=20) as r:
                    status, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
        except Exception as e:  # noqa: BLE001 - the page sees a 502, as from a proxy
            status, body = 502, json.dumps({"detail": str(e)}).encode()
        if content and status == 200:
            c = json.loads(body)
            if content.group(1) == PLAIN_CID:
                c.pop("streams", None)
                c["contentId"] = PLAIN_CID
            elif content.group(1) == CID:  # always ours: the checks know its channels
                c["streams"] = STREAMS
            body = json.dumps(c).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


failures = []


def check(ok, msg):
    print(("ok   " if ok else "FAIL ") + msg, flush=True)
    if not ok:
        failures.append(msg)


def gap(vw, vh):  # FoneDash.layout's gap between panels (JS Math.round)
    return math.floor(min(max(min(vw, vh) * 0.008, 6), 12) + 0.5)


def overlaps(a, b):
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]


# F1 TV stand-in: the dashboard iframe over the whole viewport, the "player" box above it,
# layout.js's side of the contract (post the box on load, again when the iframe asks) and
# page.js's: an onboard view over each slot's video area the dashboard names, above the iframe
# (here a translucent stand-in labelled with the driver and channel; none for an empty slot).
def host_page(src, rect):
    return f"""<!doctype html><title>F1 TV</title>
<body style="margin:0;background:#000;overflow:hidden">
<style>
  .ob {{ position: fixed; z-index: 3; box-sizing: border-box; border-radius: 10px; pointer-events: none;
    display: grid; place-content: center; gap: 4px; text-align: center; font: 700 15px Arial, sans-serif; letter-spacing: .12em;
    color: rgba(226, 234, 255, .95); border: 1px dashed rgba(150, 185, 255, .8);
    background: repeating-linear-gradient(135deg, rgba(80, 130, 240, .34) 0 16px, rgba(80, 130, 240, .2) 16px 32px); }}
  .ob small {{ font: 600 11px Arial, sans-serif; letter-spacing: .08em; color: rgba(200, 214, 245, .8); }}
</style>
<iframe id="dash" src="{src}" style="position:fixed;inset:0;width:100%;height:100%;border:0;z-index:1"></iframe>
<div id="player" style="position:fixed;left:{rect['x']}px;top:{rect['y']}px;width:{rect['w']}px;height:{rect['h']}px;z-index:2;
  background:radial-gradient(ellipse at 50% 40%,#1f2329,#08090b 75%);display:grid;place-items:center;
  font:600 28px Arial,sans-serif;color:#3c424c;letter-spacing:.2em">F1 TV PLAYER</div>
<script>
  window.__keys = []; // what reaches the page's own key handlers (F1 TV's, fone's hotkeys)
  addEventListener('keydown', (e) => window.__keys.push(e.key), true);
  const rect = {json.dumps(rect)}, rect0 = {{...rect}};
  const CODE = {json.dumps(CODE)};
  const dash = document.getElementById('dash');
  const post = () => dash.contentWindow.postMessage({{type: 'fone-layout', rect, vw: innerWidth, vh: innerHeight}}, '*');
  window.__onboards = []; // every 'onboards' command: {{t, slots}}
  window.__players = []; // every 'player' command's box (null: ours), applied as layout.js does
  function player(box) {{
    window.__players.push(box);
    Object.assign(rect, box ? {{x: 0, y: 0, w: box.w, h: box.h}} : rect0);
    Object.assign(document.getElementById('player').style, {{width: rect.w + 'px', height: rect.h + 'px'}});
    post();
  }}
  let views = [];
  function onboards(slots) {{
    window.__onboards.push({{t: performance.now(), slots}});
    views.forEach((v) => v.remove());
    views = slots.filter((s) => s.channelId != null).map((s) => {{
      const v = document.createElement('div');
      v.className = 'ob';
      Object.assign(v.style, {{left: s.x + 'px', top: s.y + 'px', width: s.w + 'px', height: s.h + 'px'}});
      v.innerHTML = `<span>ONBOARD ${{CODE[s.channelId] || ''}}</span><small>channel ${{s.channelId}}</small>`;
      return document.body.appendChild(v);
    }});
  }}
  addEventListener('message', (e) => {{
    const d = e.data;
    if (e.source !== dash.contentWindow || !d || d.type !== 'fone-layout-cmd') return;
    if (d.cmd === 'rect') post();
    else if (d.cmd === 'onboards') onboards(d.slots);
    else if (d.cmd === 'player') player(d.box);
  }});
  dash.addEventListener('load', post);
</script></body>"""


async def dash_frame(page):
    for _ in range(100):
        fr = next((f for f in page.frames if f.url.startswith("chrome-extension://")), None)
        if fr:
            return fr
        await asyncio.sleep(0.1)
    raise RuntimeError("no dashboard frame")


async def snap(fr):
    return await fr.evaluate("__dash.snapshot()")


async def wait_snap(fr, pred, timeout, what):
    t0 = time.time()
    s = None
    while time.time() - t0 < timeout:
        try:
            s = await snap(fr)
            if pred(s):
                check(True, f"{what} ({time.time() - t0:.1f} s)")
                return s
        except Exception:
            pass
        await asyncio.sleep(0.1)
    check(False, f"{what} (timed out; last {s})")
    return s


async def center(fr, js):
    """The middle of the element a JS expression finds in the dashboard (= page px: the iframe covers the viewport)."""
    return await fr.evaluate(f"(() => {{ const r = ({js}).getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }})()")


async def main():
    os.makedirs(OUT, exist_ok=True)
    fwd = http.server.ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), Forward)
    threading.Thread(target=fwd.serve_forever, daemon=True).start()
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(), channel="chromium", headless=True,  # full Chromium: the headless shell has no extensions
            args=["--load-extension=/ext", "--disable-extensions-except=/ext"],
        )
        sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
        ext = sw.url.split("/")[2]
        base = f"chrome-extension://{ext}/dashboard.html"
        hosts = {}

        async def f1tv(route):
            await route.fulfill(body=hosts.get(route.request.url.split("?")[0], "<title>F1 TV</title>"), content_type="text/html")

        await ctx.route("https://f1tv.formula1.com/**", f1tv)
        errors = []

        async def set_picks(picks):
            await sw.evaluate("(v) => v ? chrome.storage.local.set({onboards: v}) : chrome.storage.local.remove('onboards')", picks)

        async def stored_picks():
            return await sw.evaluate("() => chrome.storage.local.get('onboards').then((x) => x.onboards ?? null)")

        async def open_dash(name, session, E, size, rect, extra="", opts=None):
            opts = opts or {}
            await set_picks(opts.get("picks"))
            await sw.evaluate("(m) => chrome.storage.local.set({statsMode: m})", opts.get("mode", "stats"))
            page = await ctx.new_page()
            await page.set_viewport_size({"width": size[0], "height": size[1]})
            # the network-trouble tests break requests on purpose
            expected = ("Failed to load resource",) if name in ("stale", "down") else ()
            page.on("console", lambda m: errors.append(f"{name}: {m.text}")
                    if m.type == "error" and not m.text.startswith(expected or ("\0",)) else None)
            page.on("pageerror", lambda e: errors.append(f"{name}: {e}"))
            cid = opts.get("cid", CID)
            q = f"server={FWD}&session={session}&E={E}{extra}{f'&cid={cid}' if cid else ''}" if E else extra.lstrip("&")
            if rect is None:
                await page.goto(f"{base}?{q}")
                return page, page.main_frame
            url = f"https://f1tv.formula1.com/detail/{CID}/{name}"
            hosts[url] = host_page(f"{base}?mode=iframe&{q}", rect)
            await page.goto(url)
            return page, await dash_frame(page)

        def want_channels(opts, n):
            if opts.get("cid", CID) != CID:
                return [None] * n
            picks = list(opts.get("picks") or []) + [None] * n
            return [CHANNEL.get(p) if p is not None else None for p in picks[:n]]

        def slots_of(s):
            return [s["panels"][k] for k in sorted(s["panels"]) if k.startswith("slot") and s["panels"][k]]

        async def last_posted(page):
            return await page.evaluate("window.__onboards.length ? window.__onboards[window.__onboards.length - 1].slots : null")

        # ---------------- screenshots ----------------
        for name, session, E, size, rect, opts in SCENARIOS:
            if ONLY and ONLY not in name:
                continue
            page, fr = await open_dash(name, session, E, size, rect, opts=opts)
            picks = (list(opts.get("picks") or []) + [None] * 4)[:4]  # stored as 4 (dashcore OB_SLOTS)
            s = await wait_snap(fr, lambda s: s["stateT"] is not None and s["track"] and s["cars"] > 0
                                and (rect is None or (s["onboards"]["status"] not in ("loading", "none") and s["onboards"]["picks"] == picks)),
                                30, f"{name}: state, track and cars load" + (", onboard list and choices" if rect else ""))
            if rect is not None:
                # the box the dashboard asks layout.js for: stats mode none (null: layout.js's
                # own), onboards mode a smaller one where the strip would otherwise hold none
                want_box = opts.get("box")
                t0 = time.time()
                while True:
                    players = await page.evaluate("window.__players")
                    if (players and players[-1] == want_box) or time.time() - t0 > 10:
                        break
                    await asyncio.sleep(0.1)
                check(players and players[-1] == want_box and all(b is None or b == want_box for b in players),
                      f"{name}: asks layout.js for {want_box or 'its own box'} ({players})")
                if want_box:
                    rect = {"x": 0, "y": 0, **want_box}
                    s = await wait_snap(fr, lambda s: len(slots_of(s)) >= 2, 10, f"{name}: onboards beside the map in the smaller box")
            await asyncio.sleep(0.6)  # a few frames: cars drawn, fonts settled
            s = await snap(fr)
            await page.screenshot(path=f"{OUT}/{name}.png")
            ob = s["onboards"]
            print(f"     {name}: layout={s['layout']} flag={s['flag']} part={s['part']} clock={s['clock']} rows={s['rows']} "
                  f"dots={len(s['dots'])} slots={ob['heads']} channels={ob['channels']}")
            check(s["stateT"] <= s["E"] + 1e-6, f"{name}: state from t={s['stateT']:.1f} <= E={s['E']:.1f} (never after the picture)")
            if rect is None:
                check(not s["panels"]["slot0"] and ob["posted"] is None, f"{name}: no onboard slots in a window of its own")
                await page.close()
                continue
            check(s["proxy"] == PROXY, f"{name}: iframe fetches {'through the service worker' if PROXY else 'directly (no proxy)'}")
            bad = [k for k, b in s["panels"].items() if b and overlaps(b, rect)]
            check(not bad, f"{name}: no panel under the player ({bad or 'none'})")
            boxes = [b for b in s["panels"].values() if b]
            check(all(a is b or not overlaps(a, b) for a in boxes for b in boxes), f"{name}: no two panels overlap")
            slots = slots_of(s)
            posted = await last_posted(page)
            if s["layout"] == "L":
                t, bottom = s["panels"]["tower"], rect["y"] + rect["h"]
                check(t["y"] + t["h"] == bottom, f"{name}: the tower's bottom on the player's ({t['y'] + t['h']} = {bottom})")
                c, m = s["panels"]["cond"], s["panels"]["map"]
                check(200 <= c["w"] <= 300 and m["x"] == c["x"] + c["w"] + gap(*size),
                      f"{name}: compact conditions ({c['w']}x{c['h']}), the map right beside them")
                check(1 <= len(slots) <= 4 and all(v["video"]["w"] * 9 == v["video"]["h"] * 16 and v["video"]["y"] > v["y"] for v in slots),
                      f"{name}: {len(slots)} 16:9 onboard slots under their headers ({[(v['video']['w'], v['video']['h']) for v in slots]})")
                check(m["x"] + m["w"] + gap(*size) == slots[0]["x"], f"{name}: the map up to the onboards ({m['w']} px)")
                check(all(v["x"] + v["w"] <= size[0] and v["y"] + v["h"] <= size[1] for v in slots), f"{name}: slots on screen")
            else:
                check(not slots, f"{name}: no onboard slots in the '{s['layout']}' layout")
            want = [{**v["video"], "channelId": ch} for v, ch in zip(slots, want_channels(opts, len(slots)))]
            check(posted == want, f"{name}: the page was told {posted} (want {want})")
            if slots:
                for i, n in enumerate(picks[:len(slots)]):  # the slots this layout has
                    head = ob["heads"][i]
                    if opts.get("cid", CID) != CID:
                        check(ob["disabled"][i] and "unavailable" in head, f"{name}: slot {i} without a stream list: disabled, says so ({head!r})")
                    elif n is None:
                        check(not ob["disabled"][i] and head.startswith("Onboard"), f"{name}: slot {i} empty: 'Onboard ▾' ({head!r})")
                    elif n == NO_ONBOARD:
                        check("no onboard" in head and ob["channels"][i] is None, f"{name}: slot {i}: #{n} has no onboard here ({head!r})")
                    else:
                        check(head.startswith(CODE[CHANNEL[n]] + str(n)), f"{name}: slot {i} shows #{n} ({head!r})")
            await page.close()

        if ONLY:
            return

        # ---------------- onboards: the picker, the page told on every change and every 2 s ----------------
        page, fr = await open_dash("picker", SQ, T_SQ1, (1920, 1080), P1080, opts={"picks": [1, 44]})
        s = await wait_snap(fr, lambda s: s["stateT"] is not None and s["onboards"]["status"] == "ok" and s["onboards"]["picks"] == [1, 44, None, None],
                            30, "picker: state, stream list and stored choices load")
        n0 = await page.evaluate("window.__onboards.length")
        await asyncio.sleep(4.3)
        n1 = await page.evaluate("window.__onboards.length")
        check(n1 - n0 >= 2, f"onboards: re-posted while nothing changes ({n1 - n0} in 4.3 s)")
        await page.mouse.click(*await center(fr, "document.querySelector('#slot0 .pick')"))
        s = await snap(fr)
        menu = s["onboards"]["menu"]
        tower = [r["code"] for r in get(f"/api/timing/session/{SQ}/state?t={T_SQ1}")["tower"] if int(r["num"]) != NO_ONBOARD]
        check(menu is not None and menu["slot"] == 0, "picker: a click on the slot's button opens it")
        if menu:
            check(menu["items"] == tower, f"picker: the drivers with an onboard, in the tower's order ({' '.join(menu['items'])})")
            b, sl = menu["box"], s["panels"]["slot0"]
            check(not overlaps(b, P1080) and b["x"] + b["w"] <= 1920 and b["y"] >= 0 and b["y"] + b["h"] <= sl["y"],
                  f"picker: above the slots, over the tower, clear of the player ({b})")
        await page.screenshot(path=f"{OUT}/iframe1080_picker_open.png")
        await page.mouse.click(*await center(fr, "[...document.querySelectorAll('#menuOpts .opt')].find((b) => b.textContent.startsWith('VER'))"))
        s = await snap(fr)
        check(s["onboards"]["menu"] is None and s["onboards"]["picks"] == [3, 44, None, None], f"picker: VER into the left slot ({s['onboards']['picks']})")
        check(await stored_picks() == [3, 44, None, None], "picker: the choice is stored by racing number")
        told = [x["channelId"] for x in await last_posted(page)]
        check(told[:2] == [CHANNEL[3], CHANNEL[44]] and all(c is None for c in told[2:]), f"picker: the page is told the new channel at once ({told})")
        # a driver from the other slot swaps the two
        await page.mouse.click(*await center(fr, "document.querySelector('#slot1 .pick')"))
        await page.mouse.click(*await center(fr, "[...document.querySelectorAll('#menuOpts .opt')].find((b) => b.textContent.startsWith('VER'))"))
        s = await snap(fr)
        check(s["onboards"]["picks"] == [44, 3, None, None], f"picker: VER from the left slot into the right swaps them ({s['onboards']['picks']})")
        # None clears
        await page.mouse.click(*await center(fr, "document.querySelector('#slot0 .pick')"))
        await page.mouse.click(*await center(fr, "document.getElementById('menuNone')"))
        s = await snap(fr)
        check(s["onboards"]["picks"] == [None, 3, None, None] and (await last_posted(page))[0]["channelId"] is None, "picker: None clears the slot")
        check(await stored_picks() == [None, 3, None, None], "picker: ...and that is stored too")
        await page.screenshot(path=f"{OUT}/iframe1080_picker_after.png")
        await page.keyboard.press("k")
        check(await page.evaluate("window.__keys") == ["k"], "picker: the keyboard stays with the page after the picker's clicks")
        # left alone, it closes by itself (no focus here, so no Escape)
        await page.mouse.click(*await center(fr, "document.querySelector('#slot1 .pick')"))
        check((await snap(fr))["onboards"]["menu"] is not None, "picker: open again")
        await page.mouse.move(1700, 150)  # onto the tower: still the stats
        await wait_snap(fr, lambda s: s["onboards"]["menu"] is None, 3, "picker: closes soon after the pointer leaves it for the rest of the stats")
        await page.mouse.click(*await center(fr, "document.querySelector('#slot1 .pick')"))
        await page.mouse.move(700, 400)  # over the player: the frame hears nothing more, not even a pointerleave
        await wait_snap(fr, lambda s: s["onboards"]["menu"] is None, 8, "picker: closes once the pointer has gone quiet (gone to the player)")
        # a resize moves the slots, and the page hears of it
        await page.set_viewport_size({"width": 1948, "height": 1147})
        await page.evaluate("post()")  # layout.js re-posts the box on a resize
        s = await wait_snap(fr, lambda s: slots_of(s) and slots_of(s)[-1]["x"] + slots_of(s)[-1]["w"] == 1948 - gap(1948, 1147), 3,
                            "onboards: the slots follow a resize")
        posted = await last_posted(page)
        last = slots_of(s)[-1]["video"]
        check(posted and len(posted) == len(slots_of(s)) and posted[-1]["x"] == last["x"] and posted[-1]["y"] == last["y"],
              f"onboards: ...and the page is told ({posted})")
        # a page whose px aren't ours (it reports twice our viewport): the boxes go back in its px
        await page.evaluate("dash.contentWindow.postMessage({type: 'fone-layout', rect: {x: 0, y: 0, w: 3008, h: 1692}, vw: 3896, vh: 2294}, '*')")
        await asyncio.sleep(0.3)
        s = await snap(fr)
        posted = await last_posted(page)
        want = [{k: 2 * v["video"][k] for k in "xywh"} for v in slots_of(s)]
        check(s["panels"]["tower"]["y"] + s["panels"]["tower"]["h"] == 846 and [{k: p[k] for k in "xywh"} for p in posted] == want,
              f"onboards: in the page's own px when they differ from ours ({posted})")
        await page.close()

        # the server without a list, then with one: the pickers wake up on the next ask
        page, fr = await open_dash("nolist", SQ, T_SQ1, (1920, 1080), P1080, opts={"picks": [1, None], "cid": None})
        s = await wait_snap(fr, lambda s: s["stateT"] is not None, 30, "no content id: state loads")
        shown = [d for d, h in zip(s["onboards"]["disabled"], s["onboards"]["heads"]) if h is not None]  # this layout's slots
        check(s["onboards"]["status"] == "none" and shown and all(shown), "no content id: pickers off, quietly")
        check(all(x["channelId"] is None for x in await last_posted(page)), "no content id: empty slots for the page")
        await page.close()

        # ---------------- playback: cars move, clock ticks, a seek back hides the future ----------------
        page, fr = await open_dash("play", SQ, T_SQ1, (1920, 1080), P1080, "&play=1")
        a = await wait_snap(fr, lambda s: s["stateT"] is not None and len(s["dots"]) > 5, 30, "playing: cars on the map")
        await asyncio.sleep(2.0)
        b = await snap(fr)
        check(1.7 < b["E"] - a["E"] < 2.6, f"playing: the clock ran {b['E'] - a['E']:.2f} s in 2 s")
        moved = [d for d in b["dots"] for o in a["dots"] if o["id"] == d["id"] and abs(o["x"] - d["x"]) + abs(o["y"] - d["y"]) > 3]
        check(len(moved) >= 5, f"playing: {len(moved)} cars moved on the map")
        check(b["clock"] != a["clock"], f"playing: the session clock ticks ({a['clock']} -> {b['clock']})")
        check(b["stateT"] > a["stateT"], "playing: /state keeps being polled")
        await page.screenshot(path=f"{OUT}/play_iframe1080_sq1.png")
        # seek back 10 minutes: the newer state must vanish at once, the one at the new E follow
        await fr.evaluate("__dash.simSet({E: 1791549200})")
        s = await snap(fr)
        check(s["stateT"] is None or s["stateT"] <= s["E"] + 1, f"seek back: nothing from after E on screen (state t={s['stateT']}, E={s['E']:.1f})")
        s = await wait_snap(fr, lambda s: s["stateT"] is not None and abs(s["stateT"] - s["E"]) < 3, 5, "seek back: state at the new E")
        check(s["flag"] == "GREEN" and s["clock"] in ("08:42", "08:41", "08:40", "08:39"), f"seek back: SQ1 before the red flag ({s['flag']} {s['clock']})")
        # pause: the clock stops, and the state is fetched for the paused frame
        await fr.evaluate("__dash.simSet({playing: false})")
        p1 = await snap(fr)
        await asyncio.sleep(1.0)
        p2 = await snap(fr)
        check(p1["E"] == p2["E"] and not p2["playing"], "pause: E holds still")
        await page.close()

        # ---------------- iframe: a click on the stats leaves the keyboard with the page ----------------
        page, fr = await open_dash("focus", SQ, T_SQ1, (1920, 1080), P1080)
        await wait_snap(fr, lambda s: s["stateT"] is not None, 30, "focus: state loads")
        await page.mouse.click(600, 400)  # the player
        await page.keyboard.press("a")
        await page.mouse.click(1600, 300)  # a tower row
        await page.keyboard.press("b")
        await page.mouse.click(130, 950)  # the conditions panel
        await page.keyboard.press("c")
        await page.mouse.click(600, 950)  # the map
        await page.keyboard.press("d")
        keys = await page.evaluate("window.__keys")
        check(keys == ["a", "b", "c", "d"], f"focus: keys still reach the page after clicks on the stats ({keys})")
        await page.close()

        # ---------------- network trouble: keep the last state, marked stale ----------------
        page, fr = await open_dash("stale", SQ, T_SQ1, (1280, 900), None, "&play=1")
        await wait_snap(fr, lambda s: s["stateT"] is not None and s["rows"] == 22, 30, "stale: state loads")
        await page.route(f"{FWD}/api/timing/session/{SQ}/state*", lambda route: route.abort())
        s = await wait_snap(fr, lambda s: s["staleShown"], 6, "stale: the indicator shows when /state fails")
        check(s["rows"] == 22 and s["stateT"] is not None, "stale: the last good state stays on screen")
        await page.screenshot(path=f"{OUT}/standalone_stale.png")
        await page.unroute(f"{FWD}/api/timing/session/{SQ}/state*")
        s = await wait_snap(fr, lambda s: not s["staleShown"], 12, "stale: the indicator clears once /state answers again")
        await page.close()
        page, fr = await open_dash("down", SQ, T_SQ1, (1280, 900), None, "&play=1")
        await page.route(f"{FWD}/api/timing/**", lambda route: route.abort())
        await page.reload()
        fr = page.main_frame
        s = await wait_snap(fr, lambda s: "reach" in s["note"], 10, "server down from the start: says so")
        await page.close()

        # ---------------- iframe: follows its own tab only ----------------
        page, fr = await open_dash("tabs", None, None, (1920, 1080), P1080, "")
        s = await wait_snap(fr, lambda s: isinstance(s["myTab"], int), 10, "iframe: learns its tab id")
        mine = s["myTab"]

        def report(tab, E, playing=True):
            return sw.evaluate(
                """([tab, E, playing, server, session]) => chrome.runtime.sendMessage({target: 'dashboard', type: 'clock', tab, E,
                     playing, rate: 1, at: Date.now() / 1000, sessionKey: session, cid: null, server, live: false}).catch(() => {})""",
                [tab, E, playing, SERVER, SQ])

        for _ in range(5):
            await report(mine + 1000, 1791552600)
            await asyncio.sleep(0.1)
        s = await snap(fr)
        check(s["E"] is None, "iframe: ignores another tab's player")
        for _ in range(5):
            await report(mine, T_SQ1)
            await asyncio.sleep(0.1)
        s = await wait_snap(fr, lambda s: s["stateT"] is not None, 10, "iframe: follows its own tab's player")
        check(abs(s["E"] - T_SQ1) < 2, f"iframe: E from its own tab ({s['E']})")
        await report(mine + 1000, 1791552600)
        await asyncio.sleep(0.3)
        s = await snap(fr)
        check(abs(s["E"] - T_SQ1) < 2, "iframe: still its own tab after another tab reports")
        await page.close()

        # ---------------- standalone: follows the tab that plays ----------------
        page, fr = await open_dash("follow", None, None, (1280, 900), None, "")
        for _ in range(4):
            await report(101, T_SQ1, True)
            await report(102, 1791551700, False)
            await asyncio.sleep(0.15)
        s = await snap(fr)
        check(s["follow"] == 101 and abs(s["E"] - T_SQ1) < 2, f"standalone: follows the playing tab ({s['follow']})")
        for _ in range(4):
            await report(101, 1791550151, False)
            await report(102, 1791551700, True)
            await asyncio.sleep(0.15)
        s = await snap(fr)
        check(s["follow"] == 102 and abs(s["E"] - 1791551700) < 2, f"standalone: moves to the tab that plays now ({s['follow']})")
        await page.close()

        # ---------------- no data: quiet note ----------------
        page, fr = await open_dash("nodata", SQ, 1791540000, (1280, 900), None)
        s = await wait_snap(fr, lambda s: s["na"] is not None, 10, "before the session: no timing yet")
        check("No timing data yet" in s["note"], f"before the session: quiet note ({s['note']!r})")
        await page.screenshot(path=f"{OUT}/standalone_no_data.png")
        await page.close()

        for e in errors:
            check(False, f"page error: {e}")
        await ctx.close()
    fwd.shutdown()


asyncio.run(main())
print(f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
