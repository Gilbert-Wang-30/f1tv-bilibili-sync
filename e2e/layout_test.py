"""Split view (extension/layout.js): headless Chromium + the extension on a mock of F1 TV's player.

Run by e2e/layout_run.sh (no server, no network). The mock (layout_mock.html) reproduces the real
player DOM: a position:fixed div.video-grid (z 100) holding tiledmedia-view > <video> (a test
pattern with a yellow border), and Tiledmedia's control bar in a second fixed layer
(div.tmw-player-ui-container, z 999) with a clickable seek bar and F1 TV's fullscreen logic
(requestFullscreen on div.video-grid, from the button and from a double-click on the video).

Checks: box sizes at several viewports; the player content inside the box (rects, Tiledmedia's
own resize report, pixels); the control bar working inside the box; the dashboard iframe getting
the right fone-layout messages and every click outside the box; fone's panel moved into the box;
disable() restoring the computed styles exactly; fullscreen (button, double-click, split turned on
while F1 TV is fullscreen, the fallback when no activation is left, the dashboard's button);
feed switch and player rebuild; back to the catalogue and back; the choice kept across reloads;
multi-view; the control bar layer outside the grid; pop-out.
Env: FSWRAP=1|0 (layout_page.js loaded or not), SHOTS (screenshot dir), DASH=stub|real.
"""

import asyncio
import json
import math
import os
import sys
import tempfile
import time

from PIL import Image
from playwright.async_api import async_playwright

EXT = "/ext"
SHOTS = os.environ.get("SHOTS", "/shots")
FSWRAP = os.environ.get("FSWRAP", "1") == "1"
STUB = os.environ.get("DASH", "stub") == "stub"
TAG = "fswrap" if FSWRAP else "nofswrap"
MOCK = open("/e2e/layout_mock.html", "rb").read()
VIDEO = open("/media/test.mp4", "rb").read()
URL = "https://f1tv.formula1.com/detail/1000011024/2026-bahrain-gp-in-malaysia-qualifying?action=play"
SIZES = [(1920, 1080), (2560, 1440), (2560, 1080), (1280, 800), (3840, 1080), (1366, 768)]
NAVY, PURPLE = (24, 40, 72), (64, 24, 72)  # the stub dashboard's right column / bottom strip

passed = 0


def check(cond, msg):
    global passed
    if not cond:
        print(f"FAIL [{TAG}]:", msg, flush=True)
        sys.exit(1)
    passed += 1
    print(f"ok [{TAG}] -", msg, flush=True)


def box(vw, vh):  # layout.js boxFor(): ~70 % of the width in whole 16x9 px steps, at most the height
    n = math.floor(vw * 0.77 / 16 + 0.5)  # JS Math.round (layout.js RATIO)
    if n * 9 > vh:
        n = vh // 9
    return [0, 0, n * 16, n * 9]


def same(a, b, tol=0.6):
    return a is not None and b is not None and len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


GEOM = """() => {
  const q = (s) => document.querySelector(s);
  const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); return [b.left, b.top, b.width, b.height].map((v) => Math.round(v * 10) / 10); };
  const wrap = document.getElementById('fone-overlay')?.shadowRoot?.querySelector('.wrap');
  const f = q('#fone-dash');
  const v = q('tiledmedia-view video');
  const fs = document.fullscreenElement;
  return {
    vw: document.documentElement.clientWidth, vh: document.documentElement.clientHeight,
    grid: r(q('div.video-grid')), ui: r(q('div.tmw-player-ui-container')), view: r(q('tiledmedia-view')), video: r(v),
    views: [...document.querySelectorAll('tiledmedia-view')].map(r),
    bar: r(q('.tm-ui-controlbar')), seek: r(q('.tm-ui-seekbar')), fsbtn: r(q('.tm-ui-fullscreentogglebutton')), play: r(q('.tm-ui-playbacktogglebutton')),
    frame: r(f), frameZ: f ? getComputedStyle(f).zIndex : null, frameBorder: f ? getComputedStyle(f).borderTopWidth : null,
    frameBg: f ? getComputedStyle(f).backgroundColor : null, frameSrc: f ? f.src : null, frameAllow: f ? f.getAttribute('allow') : null,
    wrap: r(wrap), panelHidden: document.getElementById('fone-overlay')?.hidden ?? null,
    layout: document.documentElement.dataset.foneLayout ?? null, styles: document.querySelectorAll('#fone-layout-style').length,
    styleMedia: q('#fone-layout-style')?.media ?? null, frames: document.querySelectorAll('#fone-dash').length,
    fs: fs ? (fs === document.documentElement ? 'html' : fs.className || fs.tagName) : null,
    t: v ? v.currentTime : null, paused: v ? v.paused : null, active: navigator.userActivation.isActive,
    m: window.__m,
  };
}"""

HIT = """([x, y]) => { const e = document.elementFromPoint(x, y); if (!e) return null;
  return e.tagName + (e.id ? '#' + e.id : '') + (typeof e.className === 'string' && e.className.trim() ? '.' + e.className.trim().split(/\\s+/).join('.') : ''); }"""

# Everything layout.js could touch, for "disable() restores exactly".
SNAP = """() => {
  const sels = ['html', 'body', '#app', '#app header', '#video-grid-root', 'div.video-grid', '.video-grid-wrapper', '.video-wrapper',
    '.embedded-player-container', '.tmw-container', '.tmw-player-container', '.tmw-player-views-container', 'tiledmedia-view',
    'tiledmedia-view video', 'div.tmw-player-ui-container', 'tiledmedia-ui-view', '#tm-uicontainer', '.tm-ui-controlbar',
    '.tm-ui-seekbar', '.tm-ui-fullscreentogglebutton'];
  const out = {};
  for (const s of sels) {
    const el = document.querySelector(s);
    if (!el) { out[s] = null; continue; }
    const cs = getComputedStyle(el); const o = {};
    for (let i = 0; i < cs.length; i++) o[cs[i]] = cs.getPropertyValue(cs[i]);
    const b = el.getBoundingClientRect(); o['@rect'] = [b.left, b.top, b.width, b.height].join(',');
    o['@attrs'] = [...el.attributes].map((a) => a.name + '=' + a.value).sort().join(' ');
    out[s] = o;
  }
  const w = document.getElementById('fone-overlay')?.shadowRoot?.querySelector('.wrap');
  if (w) { const cs = getComputedStyle(w); out['.wrap'] = { top: cs.top, right: cs.right, bottom: cs.bottom, left: cs.left, position: cs.position, style: w.getAttribute('style') }; }
  out['@doc'] = { html: document.documentElement.children.length, head: document.head.children.length, sheets: document.styleSheets.length,
    frames: document.querySelectorAll('iframe').length };
  return out;
}"""


class Iso:
    """FoneLayout lives in the extension's isolated world; Playwright's evaluate runs in the
    page's. Reach it over CDP (no user gesture unless asked)."""

    def __init__(self, page):
        self.page = page
        self.ctxs = {}

    async def start(self):
        self.cdp = await self.page.context.new_cdp_session(self.page)
        self.cdp.on("Runtime.executionContextCreated", self._created)
        self.cdp.on("Runtime.executionContextDestroyed", lambda e: self.ctxs.pop(e["executionContextId"], None))
        self.cdp.on("Runtime.executionContextsCleared", lambda e: self.ctxs.clear())
        await self.cdp.send("Runtime.enable")

    def _created(self, e):
        c = e["context"]
        if c.get("auxData", {}).get("type") == "isolated" and (c.get("origin", "").startswith("chrome-extension://") or "fone" in c.get("name", "")):
            self.ctxs[c["id"]] = c

    async def eval(self, expr, gesture=False):
        deadline = time.time() + 10
        while True:
            for cid in sorted(self.ctxs, reverse=True):
                r = await self.cdp.send("Runtime.evaluate", {"expression": expr, "contextId": cid, "returnByValue": True,
                                                            "awaitPromise": True, "userGesture": gesture})
                if "exceptionDetails" not in r:
                    return r["result"].get("value")
            if time.time() > deadline:
                raise RuntimeError(f"no isolated world answered {expr!r}: {r.get('exceptionDetails')}")
            await asyncio.sleep(0.2)


async def wait_for(fn, pred, timeout, what):
    deadline = time.time() + timeout
    v = None
    while time.time() < deadline:
        v = await fn()
        if pred(v):
            return v
        await asyncio.sleep(0.1)
    print("last value:", json.dumps(v, default=str)[:1500])
    raise AssertionError(f"[{TAG}] timed out waiting for {what}")


async def dash_frame(page, timeout=10):
    """The frame of the current #fone-dash (page.frames can still list one just removed),
    once loaded and, for the stub, laid out by a first fone-layout message."""
    async def find():
        el = await page.query_selector("#fone-dash")
        f = el and await el.content_frame()
        if not f or f.is_detached():
            return None
        try:
            ok = await f.evaluate("() => document.readyState === 'complete' && (!window.__stub || __stub.msgs.length > 0)")
        except Exception:
            return None
        return f if ok else None
    return await wait_for(find, bool, timeout, "the dashboard iframe")


async def last_msg(frame):
    return await frame.evaluate("() => window.__stub ? __stub.msgs.at(-1) : null")


def color(img, x, y, r=2):
    px = [img.getpixel((i, j)) for i in range(x - r, x + r + 1) for j in range(y - r, y + r + 1)]
    return tuple(round(sum(p[k] for p in px) / len(px)) for k in range(3))


def near(c, ref, tol=18):
    return all(abs(a - b) <= tol for a, b in zip(c, ref))


def yellow(c):
    return c[0] > 190 and c[1] > 190 and c[2] < 90


async def shot(page, name):
    path = f"{SHOTS}/{TAG}-{name}.png"
    await page.screenshot(path=path)
    return Image.open(path).convert("RGB")


async def main():
    os.makedirs(SHOTS, exist_ok=True)
    errors = []
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(), channel="chromium", headless=True, viewport={"width": 1920, "height": 1080},
            args=[f"--load-extension={EXT}", f"--disable-extensions-except={EXT}", "--autoplay-policy=no-user-gesture-required"],
        )
        sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker")
        # No fone server in this test (the container has no network); keep the engine off real addresses anyway.
        await sw.evaluate("chrome.storage.sync.set({servers: ['http://127.0.0.1:9']})")

        async def f1tv(route):
            url = route.request.url
            if not url.split("?")[0].endswith("/media/test.mp4"):
                return await route.fulfill(body=MOCK, content_type="text/html")
            rng = route.request.headers.get("range", "bytes=0-")
            a, _, b = rng.split("=", 1)[1].partition("-")
            a, b = int(a), int(b) if b else len(VIDEO) - 1
            await route.fulfill(status=206, body=VIDEO[a:b + 1], headers={
                "content-type": "video/mp4", "accept-ranges": "bytes", "content-range": f"bytes {a}-{b}/{len(VIDEO)}"})

        await ctx.route("https://f1tv.formula1.com/**", f1tv)
        page = await ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        iso = Iso(page)
        await iso.start()
        geom = lambda: page.evaluate(GEOM)  # noqa: E731
        hit = lambda x, y: page.evaluate(HIT, [x, y])  # noqa: E731

        async def stored():
            for w in reversed(ctx.service_workers):
                try:
                    return (await w.evaluate("chrome.storage.local.get('layout')")).get("layout")
                except Exception:
                    pass
            return None

        async def ready():
            await page.wait_for_function("() => { const v = document.querySelector('tiledmedia-view video'); return v && v.readyState >= 3 && v.currentTime > 0.3; }", timeout=20000)
            await page.wait_for_function("() => { const h = document.getElementById('fone-overlay'); return h && !h.hidden; }", timeout=10000)

        async def focus_page():  # a click in the dashboard leaves keyboard focus in its frame
            await page.evaluate("() => { document.activeElement?.blur?.(); window.focus(); }")

        async def split_ok(vw, vh, what, msg_check=True):
            b = box(vw, vh)
            g = await wait_for(geom, lambda g: same(g["grid"], b) and g["frames"] == 1, 5, f"split box at {vw}x{vh}")
            check(g["vw"] == vw and g["vh"] == vh and same(g["grid"], b) and same(g["ui"], b) and same(g["view"], b) and same(g["video"], b),
                  f"{what}: grid, control bar layer, tiledmedia-view and <video> all at {b[2]}x{b[3]} top-left of {vw}x{vh}")
            bar = g["bar"]
            check(same([bar[0], bar[1] + bar[3], bar[2]], [0, b[3], b[2]]), f"{what}: control bar spans the box's bottom edge ({bar})")
            wr = g["wrap"]
            check(wr is not None and abs(wr[0] + wr[2] - (b[2] - 14)) < 1 and abs(wr[1] - 14) < 1,
                  f"{what}: fone panel in the box's top-right corner (right edge {wr[0] + wr[2]:.0f} = {b[2]} - 14)")
            check(same(g["frame"], [0, 0, vw, vh]) and g["frameZ"] == "99" and g["frameBorder"] == "0px" and g["frameBg"] == "rgba(0, 0, 0, 0)",
                  f"{what}: dashboard iframe fixed over the viewport below the player (z {g['frameZ']}, border 0, transparent)")
            spots = [(b[2] + (vw - b[2]) // 2, vh // 2), (b[2] // 2, b[3] + (vh - b[3]) // 2) if vh - b[3] > 4 else None, (vw - 3, 3), (b[2] + 2, b[3] // 2)]
            hits = [await hit(*s) for s in spots if s]
            check(all(h == "IFRAME#fone-dash" for h in hits), f"{what}: points outside the box hit the dashboard ({len(hits)} spots)")
            inside = [await hit(*s) for s in [(b[2] // 2, b[3] // 3), (5, 5), (b[2] - 3, b[3] // 2)]]
            check(all(h and "fone-dash" not in h for h in inside), f"{what}: points inside the box hit the player ({sorted(set(inside))})")
            if msg_check and STUB:
                f = await dash_frame(page)
                m = await wait_for(lambda: last_msg(f), lambda m: m and m["data"]["vw"] == vw and m["data"]["vh"] == vh, 5, "fone-layout message")
                d = m["data"]
                check(d["type"] == "fone-layout" and d["rect"] == dict(zip("xywh", b)) and m["fromParent"] and m["origin"] == "https://f1tv.formula1.com",
                      f"{what}: dashboard got {{type: 'fone-layout', rect: {d['rect']}, vw: {d['vw']}, vh: {d['vh']}}} from the page")
            elif msg_check:  # the real dashboard.js scales the box it was told about in playerRect()
                # (an out-of-process iframe can get its load event, and our message, before it knows
                # its own size: the dashboard must not scale the box by a 0x0 innerWidth/innerHeight)
                f = await dash_frame(page)
                want = dict(zip("xywh", b))
                r = await wait_for(lambda: f.evaluate("() => (typeof playerRect === 'function' && playerRect()) || null"), lambda r: r == want, 5, "the real dashboard's playerRect()")
                check(r == want, f"{what}: the real dashboard placed itself around the box {r}")
            return g

        # ---------------- off: F1 TV as it is ----------------
        await page.goto(URL)
        await ready()
        g = await geom()
        check(same(g["grid"], [0, 0, 1920, 1080]) and same(g["video"], [0, 0, 1920, 1080]) and same(g["ui"], [0, 0, 1920, 1080]) and g["frames"] == 0,
              "mock: the player covers the 1920x1080 viewport, control bar layer too, no dashboard")
        check(await iso.eval("typeof FoneLayout === 'object' && FoneLayout.isOn() === false") is True, "FoneLayout in the isolated world, off by default")
        before = await page.evaluate(SNAP)
        img = await shot(page, "01-off-1920x1080")
        check(yellow(color(img, 5, 5)) and yellow(color(img, 1914, 5)), "off: the test pattern's yellow border at the viewport corners")

        # ---------------- on (hotkey): the box at several viewport sizes ----------------
        await page.keyboard.press("KeyS")
        g = await split_ok(1920, 1080, "S key at 1920x1080")
        check(await iso.eval("FoneLayout.isOn()") is True and await stored() == "split", "on: isOn() true, chrome.storage.local layout = 'split'")
        check(g["layout"] == "split" and g["styles"] == 1 and g["frameSrc"].endswith("/dashboard.html?mode=iframe") and g["frameAllow"] is None,
              "on: <html data-fone-layout=split>, one injected stylesheet, iframe dashboard.html?mode=iframe with no allow=")
        dims = await wait_for(lambda: page.evaluate("() => __m.tmDims.at(-1)"), lambda d: d and d["w"] == 1472, 3, "Tiledmedia view resize report")
        check(dims["w"] == 1472 and dims["h"] == 828, f"tiledmedia-view's own ResizeObserver reports the new size to the SDK ({dims['w']}x{dims['h']})")
        t0 = g["t"]
        await asyncio.sleep(1)
        g = await geom()
        check(not g["paused"] and g["t"] > t0 + 0.5, f"video keeps playing in the box ({t0:.1f}s -> {g['t']:.1f}s)")
        img = await shot(page, "02-split-1920x1080")
        check(yellow(color(img, 5, 5)) and yellow(color(img, 1338, 5)) and yellow(color(img, 5, 378)),
              "pixels: the whole video frame is scaled into the box (yellow border at its top-left, top-right, left edge)")
        if STUB:
            check(near(color(img, 1700, 540), NAVY) and near(color(img, 600, 800), PURPLE) and near(color(img, 1900, 1060), NAVY),
                  "pixels: the dashboard's right column and bottom strip fill the L around the box")
        # clicks outside the box reach the dashboard
        if STUB:
            f = await dash_frame(page)
            n0 = await f.evaluate("() => __stub.clicks.length")
            await page.mouse.click(1600, 400)
            await page.mouse.click(500, 950)
            clicks = await wait_for(lambda: f.evaluate("() => __stub.clicks"), lambda c: len(c) >= n0 + 2, 3, "dashboard clicks")
            check(clicks[-2][:2] == [1600, 400] and clicks[-1][:2] == [500, 950], f"real clicks outside the box land in the dashboard ({clicks[-2:]})")
            await focus_page()
        # the control bar works inside the box
        g = await geom()
        s = g["seek"]
        dur = await page.evaluate("() => document.querySelector('tiledmedia-view video').duration")
        await page.mouse.click(s[0] + s[2] * 0.25, s[1] + s[3] / 2)
        await asyncio.sleep(0.3)
        g = await geom()
        check(abs(g["m"]["seeks"][-1] - 0.25) < 0.01 and abs(g["t"] - 0.25 * dur) < 1.5,
              f"seek bar click at 25% inside the box seeks to {g['t']:.1f}s of {dur:.0f}s (fraction {g['m']['seeks'][-1]:.3f})")
        p0 = g["m"]["plays"]
        pb = g["play"]
        await page.mouse.click(pb[0] + pb[2] / 2, pb[1] + pb[3] / 2)
        g = await wait_for(geom, lambda g: g["paused"], 2, "pause from the control bar")
        check(g["m"]["plays"] == p0 + 1, "play/pause button inside the box pauses")
        await page.mouse.click(pb[0] + pb[2] / 2, pb[1] + pb[3] / 2)
        await wait_for(geom, lambda g: not g["paused"], 2, "resume from the control bar")
        check(True, "...and resumes")

        for i, (vw, vh) in enumerate(SIZES[1:], 3):
            await page.set_viewport_size({"width": vw, "height": vh})
            await split_ok(vw, vh, f"{vw}x{vh}")
            img = await shot(page, f"{i:02d}-split-{vw}x{vh}")
            b = box(vw, vh)
            check(yellow(color(img, 5, 5)) and yellow(color(img, b[2] - 6, 5)) and (not STUB or near(color(img, b[2] + 20, vh // 2), NAVY)),
                  f"pixels at {vw}x{vh}: video border at the box's top corners, dashboard right of it")
        await page.set_viewport_size({"width": 1920, "height": 1080})
        await split_ok(1920, 1080, "back at 1920x1080")

        # ---------------- disable() restores everything exactly ----------------
        await page.keyboard.press("KeyS")
        await wait_for(geom, lambda g: g["frames"] == 0, 3, "split off")
        await asyncio.sleep(0.7)  # Tiledmedia's resize debounce
        after = await page.evaluate(SNAP)
        diffs = [(s, k, (before.get(s) or {}).get(k), (after.get(s) or {}).get(k)) for s in set(before) | set(after)
                 for k in set(before.get(s) or {}) | set(after.get(s) or {}) if (before.get(s) or {}).get(k) != (after.get(s) or {}).get(k)]
        if diffs:
            print("differences:", json.dumps(diffs[:12]))
        check(not diffs, f"off again: computed styles, rects and attributes of {len(before) - 2} elements identical to before (fone panel included)")
        check(await iso.eval("FoneLayout.isOn()") is False and await stored() == "off", "off: isOn() false, stored 'off'")
        img = await shot(page, "08-restored-1920x1080")
        check(yellow(color(img, 5, 5)) and yellow(color(img, 1914, 5)), "pixels: the video fills the viewport again")
        check(not await page.evaluate("() => document.documentElement.hasAttribute('data-fone-layout') || !!document.querySelector('#fone-dash, #fone-layout-style')"),
              "off: no iframe, no stylesheet, no html attribute left")

        # ---------------- fullscreen ----------------
        # split off: F1 TV's own fullscreen is untouched (the player alone)
        g = await geom()
        fb = g["fsbtn"]
        await page.mouse.click(fb[0] + fb[2] / 2, fb[1] + fb[3] / 2)
        g = await wait_for(geom, lambda g: g["fs"] is not None, 3, "F1 TV fullscreen")
        check(g["fs"] == "video-grid single" and g["m"]["isFs"], "split off: F1 TV's button makes div.video-grid fullscreen, as on F1 TV")
        await shot(page, "09-fullscreen-split-off")
        # turning the split on while F1 TV is fullscreen swaps to page fullscreen (the key press is the activation)
        await page.keyboard.press("KeyS")
        g = await wait_for(geom, lambda g: g["fs"] == "html" and g["frames"] == 1, 4, "page fullscreen after S key")
        await split_ok(1920, 1080, "split on during F1 TV fullscreen -> page fullscreen")
        await shot(page, "10-enabled-during-fullscreen")
        await page.evaluate("() => document.exitFullscreen()")
        # (fullscreenchange fires a rendering step after exitFullscreen() resolves: wait for it)
        g = await wait_for(geom, lambda g: g["fs"] is None and not g["m"]["isFs"], 3, "exit fullscreen, F1 TV told")
        check(True, "after leaving page fullscreen, F1 TV's own fullscreen state is off too (its button shows 'enter')")
        await split_ok(1920, 1080, "after leaving fullscreen")

        # F1 TV's button with the split on
        g = await geom()
        fb = g["fsbtn"]
        n_req = len(g["m"]["fsLog"])
        await page.mouse.click(fb[0] + fb[2] / 2, fb[1] + fb[3] / 2)
        g = await wait_for(geom, lambda g: g["fs"] == "html", 3, "page fullscreen from F1 TV's button")
        log = g["m"]["fsLog"][n_req:]
        if FSWRAP:
            check(log == ["request"] and g["m"]["isFs"], f"button: F1 TV's own requestFullscreen(div.video-grid) redirected to <html> by layout_page.js ({log})")
        else:
            check(log == [], "button: click caught before F1 TV's handler, the page made fullscreen instead")
        b = await split_ok(1920, 1080, "page fullscreen (button)")
        if STUB:
            f = await dash_frame(page)
            m = await wait_for(lambda: last_msg(f), lambda m: m and m["data"]["fullscreen"] is True, 3, "fullscreen: true message")
            check(m["data"]["rect"] == {"x": 0, "y": 0, "w": 1472, "h": 828}, "dashboard told {fullscreen: true} with the box")
        img = await shot(page, "11-split-fullscreen-button")
        check(yellow(color(img, 5, 5)) and (not STUB or near(color(img, 1700, 540), NAVY)), "pixels: player box and dashboard both on screen in fullscreen")
        g = await geom()
        fb = g["fsbtn"]
        await page.mouse.click(fb[0] + fb[2] / 2, fb[1] + fb[3] / 2)
        g = await wait_for(geom, lambda g: g["fs"] is None and not g["m"]["isFs"], 3, "exit via F1 TV's button, F1 TV told")
        check(True, "button again: leaves fullscreen; F1 TV's fullscreen state is off again")
        await split_ok(1920, 1080, "after the button's exit")

        # double-click on the video (F1 TV's own click timing calls requestFullscreen on the grid)
        g = await geom()
        p0, t0 = g["m"]["plays"], g["t"]
        await page.mouse.dblclick(400, 300)
        g = await wait_for(geom, lambda g: g["fs"] == "html", 3, "page fullscreen from a double-click")
        await asyncio.sleep(0.5)
        g = await geom()
        check(not g["paused"] and g["m"]["plays"] == p0,
              f"double-click on the video: page fullscreen ({'redirected by layout_page.js' if FSWRAP else 'player fullscreen swapped by layout.js'}), playback untouched")
        await split_ok(1920, 1080, "page fullscreen (double-click)")
        await shot(page, "12-split-fullscreen-dblclick")
        await page.evaluate("() => document.exitFullscreen()")  # like Esc
        g = await wait_for(geom, lambda g: g["fs"] is None and not g["m"]["isFs"], 3, "exit fullscreen, F1 TV told")
        check(True, "after Esc: F1 TV's fullscreen state is off again (its button shows 'enter')")
        await split_ok(1920, 1080, "after Esc")

        # fallback: the player alone fullscreen with no activation left to swap it -> stand aside, come back after
        await iso.eval("FoneLayout.disable()")
        g = await geom()
        fb = g["fsbtn"]
        await page.mouse.click(fb[0] + fb[2] / 2, fb[1] + fb[3] / 2)
        await wait_for(geom, lambda g: g["fs"] == "video-grid single", 3, "F1 TV fullscreen")
        # In real Chrome a click's activation lapses after ~5 s; headless Chromium here keeps it and
        # Playwright's --disable-popup-blocking means nothing cheap consumes it. So make layout.js
        # (only its isolated world) see none left.
        await iso.eval("""(() => { window.__ua = Object.getOwnPropertyDescriptor(Navigator.prototype, 'userActivation');
          Object.defineProperty(Navigator.prototype, 'userActivation', { configurable: true, get: () => ({ isActive: false, hasBeenActive: true }) });
          return navigator.userActivation.isActive; })()""")
        await iso.eval("FoneLayout.enable()")  # over CDP: no user gesture
        g = await wait_for(geom, lambda g: g["frames"] == 1, 3, "split on")
        await asyncio.sleep(0.3)
        g = await geom()
        check(g["fs"] == "video-grid single" and g["styleMedia"] == "not all" and same(g["grid"], [0, 0, 1920, 1080])
              and same(g["bar"][1:2], [1080 - g["bar"][3]]) and abs(g["wrap"][0] + g["wrap"][2] - 1906) < 1,
              "no activation: the player stays fullscreen on its own, layout.js stands aside (control bar and panel where F1 TV has them)")
        await shot(page, "13-fallback-player-alone-fullscreen")
        await iso.eval("(() => { Object.defineProperty(Navigator.prototype, 'userActivation', window.__ua); return true; })()")
        await page.evaluate("() => document.exitFullscreen()")
        await wait_for(geom, lambda g: g["fs"] is None, 3, "exit")
        await split_ok(1920, 1080, "after the player's own fullscreen ends, the split is back")

        # the dashboard's own fullscreen button (a click in the iframe activates the page too)
        if STUB:
            f = await dash_frame(page)
            await f.click("#fs")
            await wait_for(geom, lambda g: g["fs"] == "html", 3, "page fullscreen from the dashboard")
            await split_ok(1920, 1080, "page fullscreen from the dashboard's button")
            await f.click("#fs")
            await wait_for(geom, lambda g: g["fs"] is None, 3, "exit from the dashboard")
            check(True, "dashboard's button leaves fullscreen too")
            await focus_page()

        # ---------------- feed switch, player rebuild ----------------
        f = await dash_frame(page)
        born = await f.evaluate("() => window.__stub?.born ?? performance.timeOrigin")
        await page.evaluate("() => mockFeedSwitch()")
        await page.wait_for_function("() => document.querySelector('tiledmedia-view video').readyState >= 3")
        g = await split_ok(1920, 1080, "after a feed switch (new tiledmedia-view and <video>)")
        dims = await wait_for(lambda: page.evaluate("() => __m.tmDims.at(-1)"), lambda d: d and d["view"] > 1, 3, "new view's size report")
        check(dims["w"] == 1472 and dims["h"] == 828, f"the new view reports {dims['w']}x{dims['h']} to the SDK")
        await shot(page, "14-feed-switch")
        await page.evaluate("() => mockRemount(300)")
        await page.wait_for_function("() => __m.opened >= 2 && document.querySelector('tiledmedia-view video')?.readyState >= 3")
        g = await split_ok(1920, 1080, "after F1 TV rebuilt the whole player (new div.video-grid)")
        f2 = await dash_frame(page)
        check(await f2.evaluate("() => window.__stub?.born ?? performance.timeOrigin") == born, "the dashboard was not reloaded by either")

        # ---------------- back to the catalogue and back ----------------
        await page.evaluate("() => mockClosePlayer()")
        g = await wait_for(geom, lambda g: g["frames"] == 0, 4, "teardown after the player left")
        check(g["layout"] is None and g["styles"] == 0 and g["grid"] is None, "player gone (catalogue): iframe, stylesheet and html attribute removed")
        check(await iso.eval("FoneLayout.isOn()") is True, "...the choice stays on")
        await shot(page, "15-catalogue")
        await page.evaluate("() => { history.pushState(null, '', '/detail/1000011024/x?action=play'); mockOpenPlayer(); }")
        await page.wait_for_function("() => document.querySelector('tiledmedia-view video')?.readyState >= 3")
        await split_ok(1920, 1080, "player opened again: split re-applied by itself")

        # ---------------- kept across reloads ----------------
        await page.reload()
        await ready()
        await split_ok(1920, 1080, "after a reload (stored 'split', applied when the player appeared)")
        await shot(page, "16-after-reload")
        await page.click("#fone-overlay #stats")  # content.js panel button -> FoneLayout.toggle()
        await wait_for(geom, lambda g: g["frames"] == 0, 3, "off from the panel")
        check(await stored() == "off", "panel's Stats button turns it off (stored 'off')")
        await page.reload()
        await ready()
        await asyncio.sleep(1)
        g = await geom()
        check(g["frames"] == 0 and g["styles"] == 0 and same(g["grid"], [0, 0, 1920, 1080]), "after a reload: stays off")
        await page.click("#fone-overlay #stats")
        await split_ok(1920, 1080, "panel's Stats button turns it on")
        check(await iso.eval("FoneLayout.toggle()") is False and await iso.eval("FoneLayout.isOn()") is False, "toggle() returns the new state")
        await iso.eval("FoneLayout.enable()")
        await split_ok(1920, 1080, "enable()")

        # ---------------- dashboard commands, pop-out ----------------
        if STUB:
            f = await dash_frame(page)
            await f.click("#off")
            await wait_for(geom, lambda g: g["frames"] == 0, 3, "off from the dashboard")
            check(await stored() == "off", "dashboard's {type: 'fone-layout-cmd', cmd: 'off'} turns the split off")
            await focus_page()
            await iso.eval("FoneLayout.enable()")
            await split_ok(1920, 1080, "on again")
        pages0 = len(ctx.pages)
        await page.click("#fone-overlay #statsPop")  # -> FoneLayout.popOut()
        g = await wait_for(geom, lambda g: g["frames"] == 0, 3, "pop-out turns the split off")
        pop = await wait_for(lambda: asyncio.sleep(0, [x.url for x in ctx.pages[pages0:]]), lambda u: any("/dashboard.html" in x for x in u), 5, "pop-out window")
        check(await stored() == "off", f"pop-out: split off, background opened {pop[-1].split('/')[-1]} in a window of its own")
        for x in ctx.pages[pages0:]:
            await x.close()

        # ---------------- multi-view, control bar layer outside the grid ----------------
        await iso.eval("FoneLayout.enable()")
        await page.goto(URL + "&grid=two-by-two")
        await ready()
        b = box(1920, 1080)
        g = await wait_for(geom, lambda g: same(g["grid"], b) and g["frames"] == 1, 5, "multi-view split")
        inside = all(v[0] >= -0.5 and v[1] >= -0.5 and v[0] + v[2] <= b[2] + 0.5 and v[1] + v[3] <= b[3] + 0.5 for v in g["views"])
        check(len(g["views"]) == 4 and inside and same(g["ui"], b), f"multi-view (2x2): the whole grid in the box, all 4 views inside it ({g['views']})")
        await shot(page, "17-multiview-2x2")
        await page.goto(URL + "&ui=outside")
        await ready()
        g = await split_ok(1920, 1080, "control bar layer outside div.video-grid")
        await shot(page, "18-ui-layer-outside-grid")

        # ---------------- the dashboard's onboards: checked, passed on to page.js ----------------
        # {type: 'fone-layout-cmd', cmd: 'onboards', slots: [{x, y, w, h, channelId}]} from the iframe ->
        # a 'fone-cmd' event {cmd: 'onboards', slots} for page.js (main world), recorded here. Every
        # test command has its own y values, so commands the dashboard might send by itself don't count.
        await page.evaluate("""() => { window.__obCmds = []; document.addEventListener('fone-cmd', (e) => {
          try { const m = JSON.parse(e.detail); if (m.cmd === 'onboards') __obCmds.push(m); } catch {} }); }""")
        cmds = lambda: page.evaluate("() => __obCmds")  # noqa: E731
        f = await dash_frame(page)

        async def dpost(slots, src=None):
            await (src or f).evaluate("(s) => parent.postMessage({type: 'fone-layout-cmd', cmd: 'onboards', slots: s}, '*')", slots)

        A = {"x": 1490.5, "y": 21, "w": 400, "h": 225, "channelId": 1016}
        B = {"x": 1490.5, "y": 262, "w": 400, "h": 225, "channelId": None}
        await dpost([A, B])
        got = await wait_for(cmds, lambda c: any(m["slots"] == [A, B] for m in c), 3, "the onboards command passed on")
        check([m for m in got if m["slots"] == [A, B]] == [{"cmd": "onboards", "slots": [A, B]}],
              "dashboard's onboards command (a driver, an empty slot) passed on to page.js once, as a 'fone-cmd' event")
        bad = [[{**A, "y": 31}, {**A, "y": 32}, {**A, "y": 33}, {**A, "y": 30}, {**A, "y": 29}], [{**A, "y": 34, "x": float("nan")}], [{**A, "y": 35, "x": "1490"}],
               [{**A, "y": 36, "x": 1700}], [{**A, "y": -20}], [{**A, "y": 37, "h": 1100}], [{**A, "y": 38, "channelId": 1016.5}],
               [{**A, "y": 39, "channelId": "1016"}], [{**A, "y": 40, "channelId": -3}], [{**A, "y": 41, "w": 0}],
               [{**A, "y": 42}, None], [{"y": 43}], "slots"]
        for slots in bad:
            await dpost(slots)
        await page.evaluate("(s) => window.postMessage({type: 'fone-layout-cmd', cmd: 'onboards', slots: s}, '*')", [{**A, "y": 44}])
        await asyncio.sleep(0.6)
        ys = {s.get("y") for m in await cmds() for s in m["slots"] if isinstance(s, dict)}
        check(not ys & {29, 30, 31, 32, 33, 34, 35, 36, -20, 37, 38, 39, 40, 41, 42, 43, 44},
              f"{len(bad)} malformed onboards commands refused (5 slots, NaN, a string, outside the viewport, bad channel ids, "
              "zero size, a null slot, no rect, no list), and one from the page itself rather than the dashboard")
        # the dashboard's 'onboards' mode: its own box for the player ({cmd: 'player', box})
        async def ppost(box):
            await f.evaluate("(b) => parent.postMessage({type: 'fone-layout-cmd', cmd: 'player', box: b}, '*')", box)

        await ppost({"w": 1328, "h": 747})
        await wait_for(geom, lambda g: same(g["grid"], [0, 0, 1328, 747]) and same(g["ui"], [0, 0, 1328, 747]), 3, "the dashboard's box")
        check(True, "'player': the player and its controls take the dashboard's box (1328x747 in 1920x1080)")
        for bad in [{"w": 1330, "h": 747}, {"w": 640, "h": 360}, {"w": 1600, "h": 900}, {"w": "1328", "h": 747}]:
            await ppost({"w": 1328, "h": 747})
            await wait_for(geom, lambda g: same(g["grid"], [0, 0, 1328, 747]), 3, "the dashboard's box again")
            await ppost(bad)
            await wait_for(geom, lambda g: same(g["grid"], [0, 0, 1472, 828]), 3, f"ours after a bad box {bad}")
        check(True, "'player': a box that isn't 16:9 steps, under 40% or over 80% of the width, or not numbers: back to our own")
        await ppost({"w": 1328, "h": 747})
        await wait_for(geom, lambda g: same(g["grid"], [0, 0, 1328, 747]), 3, "the dashboard's box, once more")
        await ppost(None)
        await wait_for(geom, lambda g: same(g["grid"], [0, 0, 1472, 828]), 3, "box: null")
        check(True, "'player' with box null: back to our own 77%")
        await ppost({"w": 1328, "h": 747})
        await wait_for(geom, lambda g: same(g["grid"], [0, 0, 1328, 747]), 3, "the dashboard's box before the split goes off")

        n = len(await cmds())
        await page.keyboard.press("KeyS")
        got = await wait_for(cmds, lambda c: len(c) > n and c[-1]["slots"] == [], 3, "slots: [] when the split goes off")
        check(got[-1] == {"cmd": "onboards", "slots": []}, "split off: slots: [] passed on (page.js removes its onboard views)")
        await page.keyboard.press("KeyS")
        # turned off and on again: the dashboard's box was dropped with the split (it asks anew)
        await split_ok(1920, 1080, "split on again for the onboards (our own box: the old 'player' box went with the split)", msg_check=False)
        f = await dash_frame(page)
        C = {"x": 0, "y": 829, "w": 1472, "h": 251, "channelId": 1020}  # the whole bottom strip, edge to edge
        n = len(await cmds())
        await dpost([C])
        await wait_for(cmds, lambda c: any(m["slots"] == [C] for m in c[n:]), 3, "an edge-to-edge slot passed on")
        n = len(await cmds())
        await page.evaluate("() => mockClosePlayer()")
        got = await wait_for(cmds, lambda c: len(c) > n and c[-1]["slots"] == [], 4, "slots: [] once the player left")
        await page.evaluate("() => document.dispatchEvent(new Event('fone-layout-debug'))")
        dbg = json.loads(await page.evaluate("() => document.documentElement.dataset.foneLayoutDebug"))
        check(dbg["onboards"] == [] and not dbg["active"], "player gone (catalogue): slots: [] passed on with the teardown (fone-layout-debug shows it)")
        check(await page.evaluate("() => !document.querySelector('[data-fone-ob-box], tiledmedia-view[data-fone-ob]')"),
              "page.js left this mock alone (its player can't take views): no onboard boxes")

        await iso.eval("FoneLayout.disable()")
        await ctx.close()

    noise = [e for e in errors if "127.0.0.1:9" not in e and "ERR_" not in e and "Failed to load resource" not in e]
    for e in noise[:10]:
        print("page error:", e[:300])
    check(not noise, f"no page errors ({len(errors)} network errors from the dead fone server ignored)")
    print(f"\n[{TAG}] {passed} checks passed")


asyncio.run(main())
