"""Keeps fone-recorder to F1 streams.

The streamer also streams other things (games: "小播一会粥"). 录播姬 records every live
session (AutoRecord stays on, so if this gate is down everything still records); this
polls its API and, by stream title:
  - stops a recording whose title isn't F1, and deletes the seconds-old file that
    stopping leaves behind (only files the gate itself cut short, opened < 3 min ago);
  - starts recording again if the title switches to F1 mid-stream.
Stdlib only; config via env (see docker-compose.yml).
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

API = os.environ.get("GATE_API", "http://fone-recorder:2356").rstrip("/")
REC = Path(os.environ.get("GATE_REC", "/rec"))
POLL = float(os.environ.get("GATE_POLL", "3"))
# Every F1 stream so far carries the 【胡闹P房】 tag; the keywords catch one that doesn't.
RECORD = re.compile(os.environ.get(
    "GATE_RECORD", r"胡闹P房|F1|F2|F3|一练|二练|三练|练习赛|排位赛|冲刺|正赛|大奖赛|发布会|围场|方程式|巡游"), re.I)
SKIP = re.compile(os.environ["GATE_SKIP"], re.I) if os.environ.get("GATE_SKIP") else None
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Shanghai"))  # filename timestamps (recorder template)
CLEANUP_MAX_AGE = 180  # seconds: only files opened this recently are ours to delete
AUTH = "Basic " + base64.b64encode(
    f"{os.environ['BREC_HTTP_BASIC_USER']}:{os.environ['BREC_HTTP_BASIC_PASS']}".encode()).decode()
NAME = re.compile(r"^(\d{8}-\d{6}-\d{3})-(.*)\.(flv|xml)$")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def api(method, path):
    req = urllib.request.Request(API + path, method=method, headers={"Authorization": AUTH})
    if method == "POST":
        req.data = b""
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
    return json.loads(body) if body else None


def is_f1(title):
    """True/False by title; None when there's no title to judge (then leave it alone)."""
    if not title or not title.strip():
        return None
    return bool(RECORD.search(title)) and not (SKIP and SKIP.search(title))


def cleanup(room, stopped_at):
    """Delete the stub file(s) a just-stopped non-F1 recording left in the room's folder."""
    folder = REC / room["name"]
    if not folder.is_dir():
        return
    for f in folder.iterdir():
        m = NAME.match(f.name)
        if not m or is_f1(m.group(2)) is not False:
            continue
        try:
            opened = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S-%f").replace(tzinfo=TZ).timestamp()
            st = f.stat()
        except (ValueError, OSError):
            continue
        if stopped_at - CLEANUP_MAX_AGE <= opened <= stopped_at + 5 and st.st_mtime >= opened - 5:
            f.unlink()
            log(f"deleted stub {f.name} ({st.st_size} bytes)")


def tick(pending):
    for room in api("GET", "/api/room"):
        oid, title = room["objectId"], room.get("title") or ""
        f1 = is_f1(title)
        if room.get("recording") and f1 is False:
            api("POST", f"/api/room/{oid}/stop")
            log(f"room {room['roomId']}: not F1 ({title!r}) - stopped recording")
            pending.append((room, time.time()))
        elif room.get("streaming") and not room.get("recording") and f1 \
                and not room.get("autoRecordForThisSession", True):
            # Only a session this gate stopped has autoRecordForThisSession off; any other
            # not-recording gap (reconnects, retries) is 录播姬's own business.
            api("POST", f"/api/room/{oid}/start")
            log(f"room {room['roomId']}: F1 ({title!r}) - started recording")
    # Give the recorder a moment to close the file before removing it.
    for item in [p for p in pending if time.time() - p[1] >= 5]:
        pending.remove(item)
        cleanup(*item)


def main():
    log(f"gate up: {API}, record if title matches /{RECORD.pattern}/"
        + (f", skip /{SKIP.pattern}/" if SKIP else ""))
    pending, failing = [], False
    while True:
        try:
            tick(pending)
            if failing:
                log("recorder API reachable again")
            failing = False
        except Exception as e:  # recorder restarting, network blip: keep polling
            if not failing:
                log(f"recorder API error: {e!r}")
            failing = True
        time.sleep(POLL)


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:  # quick title test: python gate.py --check < titles.txt
        for line in sys.stdin:
            print(is_f1(line.strip()), line.strip())
    else:
        main()
