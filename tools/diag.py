#!/usr/bin/env python3
"""Read the extension's playback diagnostics (GET /api/diag) as a timeline.

  tools/diag.py                     every client's last report, then the newest client's timeline
  tools/diag.py --client c1a2b3c4   that client
  tools/diag.py --minutes 30        how far back (default 20)
  tools/diag.py --server http://localhost:8788
"""
import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def fmt_t(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")


def speed_of(snap):
    v = snap.get("video") or {}
    r = ((v.get("dx") or {}).get("r")) or {}
    parts = [f"chosen x{v.get('rate')}"]
    if r:
        parts.append(f"page want {r.get('want')} sdk {r.get('sdk')}{'' if r.get('exact') else '~'} el {r.get('el')}"
                     f" ps {r.get('ps')} fails {r.get('fails')}" + (f" err {r.get('err')!r}" if r.get("err") else ""))
    return ", ".join(parts)


def line(e):
    t, k = fmt_t(e["t"]), e["type"]
    if k == "snap":
        a, v = e.get("audio") or {}, e.get("video") or {}
        drift = e.get("drift")
        dm = ((v.get("dx") or {}).get("dm")) or {}
        dms = (f" | 弹幕 {'on' if dm.get('on') else 'off'} {dm.get('shown')}/{dm.get('n')} lanes {dm.get('lanes')} at {dm.get('rect')}"
               f" video {dm.get('video')} img {dm.get('img')}{' err ' + str(dm.get('err')) if dm.get('err') else ''}") if dm else ""
        return (f"{t} snap  {e.get('state'):9} drift {drift if drift is None else round(drift, 3)}"
                f" | audio x{a.get('rate')} pp {a.get('pp')} rs {a.get('rs')} ahead {a.get('ahead')}s paused {a.get('paused')}"
                f" | video p {v.get('p')} paused {v.get('paused')} waiting {v.get('waiting')} {v.get('src')} | {speed_of(e)}{dms}")
    if k == "seek":
        return f"{t} SEEK  {e.get('from')} -> {e.get('to')} (target {e.get('target')}, x{e.get('rate')}, ahead {e.get('ahead')}s, {e.get('src')})"
    if k == "state":
        return f"{t} state {e.get('from')} -> {e.get('to')}  {e.get('text') or ''}"
    if k == "speed":
        return f"{t} SPEED engine follows x{e.get('to')} ({e.get('src')})"
    if k == "hls":
        return f"{t} HLS   {e.get('details')} {'FATAL ' if e.get('fatal') else ''}at {e.get('at')}"
    if k in ("wake", "gap"):
        return f"{t} {k.upper()}  {e.get('slept', e.get('s'))} s"
    if k == "pstate":
        return f"{t} PLAYER {e.get('from')} -> {e.get('to')} at p {e.get('p')} (want {e.get('want')} sdk {e.get('sdk')}, audio {e.get('audio')}, handle {e.get('h')}, views {e.get('nv')}{', err ' + repr(e.get('err')) if e.get('err') else ''})"
    if k == "audio":
        who = "fone" if e.get("auto") else "user" if e.get("manual") else "?"
        return f"{t} AUDIO {e.get('from')} -> {e.get('to')} ({e.get('kind')}, by {who}, fx {e.get('on')}, best {e.get('fx')}) at p {e.get('p')} ps {e.get('ps')} handle {e.get('h')}{', err ' + repr(e.get('err')) if e.get('err') else ''}"
    if k == "handle":
        return f"{t} HANDLE {e.get('from')} -> {e.get('to')} at p {e.get('p')} ps {e.get('ps')} (audio {e.get('audio')}, want {e.get('want')}, views {e.get('nv')})"
    if k == "sdk":
        how = "ok" if e.get("ok") else f"ERR {e.get('err')!r}"
        gi = " interrupted" if e.get("gi") else ""
        return f"{t} SDK   {e.get('name')}({e.get('args')}) {how}{gi} in {e.get('ms')} ms  [ps {e.get('ps')} handle {e.get('h')} views {e.get('nv')} player {e.get('pid')}]"
    if k == "fatal":
        return (f"{t} FATAL code {e.get('code')} {e.get('msg')!r} at p {e.get('p')} ps {e.get('ps')} handle {e.get('h')} views {e.get('nv')}"
                f" (our call {e.get('busy')}, f1tv apply {e.get('f1tv')}; before: {e.get('last')})")
    if k in ("player", "dead"):
        return f"{t} {k.upper():6}{e.get('from')} -> {e.get('to')} at p {e.get('p')}" if k == "player" else f"{t} DEAD  player {e.get('pid')} at p {e.get('p')} ps {e.get('ps')}"
    if k == "ob":
        return f"{t} OB    {e.get('from') or '-'} -> {e.get('to') or '-'} at p {e.get('p')} ps {e.get('ps')}{', err ' + repr(e.get('err')) if e.get('err') else ''}"
    return f"{t} {k} {json.dumps({x: y for x, y in e.items() if x not in ('t', 'type', 'client', 'build', 'platform', 'rx')})}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:8788")
    ap.add_argument("--client")
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--type")
    a = ap.parse_args()
    q = {"since": time.time() - a.minutes * 60, "limit": 5000}
    if a.client:
        q["client"] = a.client
    if a.type:
        q["type"] = a.type
    d = json.load(urllib.request.urlopen(f"{a.server}/api/diag?{urllib.parse.urlencode(q)}", timeout=20))
    for c, info in sorted(d["clients"].items(), key=lambda x: -(x[1].get("last") or 0)):
        print(f"client {c}: {info.get('platform')} build {info.get('build')} last {fmt_t(info['last']) if info.get('last') else '?'}")
    evs = d["events"]
    if not a.client and evs:
        newest = max(d["clients"].items(), key=lambda x: x[1].get("last") or 0)[0]
        evs = [e for e in evs if e.get("client") == newest]
        print(f"-- timeline of {newest} (newest client), last {a.minutes:g} min --")
    for e in evs:
        print(line(e))


if __name__ == "__main__":
    main()
