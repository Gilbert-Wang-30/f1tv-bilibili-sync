#!/usr/bin/env bash
# End-to-end test: fone server + headless Chrome with the extension, all in Docker.
# Uses real F1 TV / MultiViewer / live-timing lookups, synthetic commentary audio.
set -euo pipefail
cd "$(dirname "$0")/.."
PW_IMAGE=${PW_IMAGE:-mcr.microsoft.com/playwright/python:v1.63.0-noble}
# Test image: built from server/ as fone:dev (never fone:latest, which production runs).
# E2E_IMAGE=<tag> E2E_BUILD=0 reuses an existing image instead of building.
# E2E_ONLY=onboards runs Phase A3 alone (the dashboard's onboard views; a dev shortcut).
IMAGE=${E2E_IMAGE:-fone:dev}
# Container/network names: fone-dev-* per docs/architecture.md; override to run in parallel.
NAME=${E2E_NAME:-fone-dev-e2e}
WORK=$(mktemp -d)
NET=$NAME
# Keeps CPU low on a box that also runs the recorders (and the production server).
CAP="--cpus 2 --cpu-shares 256"
CAP_CHROME="--cpus 4 --cpu-shares 256"
cleanup() {
  local rc=$?
  if [ $rc != 0 ]; then echo "== server log (tail)"; docker logs --tail 40 $NAME-server 2>&1 || true; fi
  docker rm -f $NAME-server $NAME-writer >/dev/null 2>&1 || true
  docker network rm $NET >/dev/null 2>&1 || true
  docker run --rm -v "$WORK":/w alpine rm -rf /w/rec /w/data /w/media >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

if [ "${E2E_BUILD:-1}" != 0 ]; then docker build -q -t "$IMAGE" server >/dev/null; fi
mkdir -p "$WORK/rec/胡闹P房" "$WORK/data" "$WORK/media"
chmod -R 777 "$WORK"

echo "== generating synthetic commentary (30 min) and mock F1 TV video"
# Commentary recording that covers the KL qualifying replay: starts 5 min before
# replay position 0 (+40 s default delay) and runs 30 min. Name/mtime mimic the recorder.
docker run --rm -i $CAP -v "$WORK":/w -w /w "$IMAGE" python3 - <<'EOF'
import json, os, subprocess, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
p0 = None
# Same anchor the server will compute (re-derived here independently).
get = lambda u: urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "fone-e2e/0.1"})).read()
mv = json.loads(get("https://api.multiviewer.app/api/v1/meetings/1308/sessions/11730"))
sd = json.loads(get("https://livetiming.formula1.com/static/" + mv["session_info"]["Path"] + "SessionData.json").decode("utf-8-sig"))
started = [e["Utc"] for e in sd["StatusSeries"] if e.get("SessionStatus") == "Started"][0]
p0 = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() - float(mv["session_start"])
wall_start = p0 + 40 - 300
dur = 1800
name = datetime.fromtimestamp(wall_start, ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M") + "-【胡闹P房】16点 巴林 排位赛.flv"
path = f"rec/胡闹P房/{name}"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=48000:duration={dur}",
                "-f", "lavfi", "-i", f"color=c=black:s=64x36:r=5:d={dur}", "-c:a", "aac", "-b:a", "64k",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-shortest", "-f", "flv", path], check=True)
os.utime(path, (wall_start + dur, wall_start + dur))
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x36:r=2:d=1800",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1800", "-c:v", "libx264", "-preset", "ultrafast",
                "-g", "4", "-c:a", "aac", "-b:a", "16k", "-movflags", "+faststart", "media/video.mp4"], check=True)
json.dump({"wall_start": wall_start, "duration": dur, "p0": p0}, open("media/meta.json", "w"))
print("commentary file:", name)
# The room's danmaku, as fone's capture writes them (danmaku.py): one every 2 s over the
# recording, "弹0000".. with levels, medals, colours and top ones mixed in; also every 3 s over
# the next 20 min, to arrive "live" as the clock reaches them.
os.makedirs("data/danmaku/capture", exist_ok=True)
STICKER = {"url": "http://i0.hdslb.com/bfs/live/0b297cec439f88ce5a675af952d6ccdbf304eb98.png", "width": 162, "height": 162}
DOG = "http://i0.hdslb.com/bfs/live/4428c84e694fbf4e0ef6c06e958d9352c3582740.png"
def dm(t, text, i):
    sticker = i % 6 == 1  # a sticker (表情) danmaku; every 6th from 4 has an inline [dog] emoji
    emots = json.dumps({"emots": {"[dog]": {"url": DOG, "width": 20, "height": 20}} if "[dog]" in text else None})
    i0 = [0, 5 if i % 25 == 7 else 1, 25, 0xFF0000 if i % 10 == 3 else 0xFFFFFF, int(t * 1000), 0, 0, format(i, "08x"),
          0, 0, 0, "", 1 if sticker else 0, STICKER if sticker else "{}", "{}", {"extra": emots, "mode": 0}]
    medal = [] if i % 13 == 0 else [i % 11, "胡闹PU", "马莎拉车", 23954427, 6067854, "", 0, 0, 0, 0, 0, 1, 1313650809]
    info = [i0, text, [0, "e2e***", 0, 0, 0, 10000, 1, ""], medal, [i % 20, 0, 0, ">50000", 0], ["", ""], 0, 0, None,
            {"ts": int(t)}, 0, 0, None, None, 0, 65, [(i * 7) % 30]]
    return {"rx": round(t + 0.5, 3), "m": {"cmd": "DANMU_MSG", "info": info}}
import time as _t
lines = [{"rx": wall_start - 10, "ev": "open"}]
lines += [dm(wall_start + 2 * i, f"弹{i:04d}" + ("好快" if i % 3 == 0 else "") + ("[dog]" if i % 6 == 4 else ""), i)
          for i in range(dur // 2)]
now = _t.time()
lines += [dm(now + 3 * i, f"直播{i:03d}", i) for i in range(400)]
day = datetime.fromtimestamp(now, ZoneInfo("UTC")).strftime("%Y%m%d")
with open(f"data/danmaku/capture/23954427-{day}.jsonl", "w") as f:
    f.writelines(json.dumps(x, ensure_ascii=False) + "\n" for x in lines)
EOF

# The live fixture is the 2026-10-04 race (content 1000011022), which F1 TV now lists as a
# REPLAY: keep its real data (anchor, p0) but seed it as LIVE in the server's content cache.
echo "== seeding the race fixture as LIVE"
docker run --rm $CAP -v "$WORK/data":/data "$IMAGE" python3 -c '
import asyncio
from pathlib import Path
from fone.f1 import F1Mapper
from fone.store import Store
async def main():
    s = Store(Path("/data/fone.sqlite3")); m = F1Mapper(s)
    d = await m.content("1000011022"); await m.close()
    assert d.get("anchor") == "multiviewer", d
    d["subtype"] = "LIVE"; s.put_content("1000011022", d)
asyncio.run(main())'

docker network create $NET >/dev/null
docker run -d $CAP --name $NAME-server --network $NET \
  -v "$WORK/rec":/rec:ro -v "$WORK/data":/data \
  -e FONE_GROWING_IDLE_SECONDS=20 -e FONE_ROOMS=23954427=胡闹P房 -e FONE_BACKFILL=0 \
  -e FONE_DANMAKU_CAPTURE=0 -e FONE_DANMAKU_ARCHIVE=0 "$IMAGE" >/dev/null

echo "== starting a live (growing) commentary recording"
docker run -d --cpus 1 --cpu-shares 256 --name $NAME-writer -v "$WORK/rec":/rec "$IMAGE" sh -c '
  f="/rec/胡闹P房/$(TZ=Asia/Shanghai date +%Y%m%d-%H%M)-【胡闹P房】live.flv"
  ffmpeg -v error -re -f lavfi -i sine=frequency=550:sample_rate=48000:duration=900 \
    -f lavfi -i color=c=black:s=64x36:r=5:d=900 -c:a aac -b:a 64k -c:v libx264 -preset ultrafast -tune zerolatency \
    -g 10 -shortest -flush_packets 1 -f flv "$f"' >/dev/null

echo "== waiting for the server to extract the finished recording"
# Until the finished fixture is extracted and in the playlist (a replay in Chrome refreshes its
# timeline only every 60 s: starting before this is a 'No recording covers this session' timeout
# on a loaded box) and the live one has a few segments.
for i in $(seq 1 120); do
  if docker exec $NAME-server python -c "
import json,urllib.request,sys,time
t=time.time()
tl=json.load(urllib.request.urlopen('http://127.0.0.1:8788/api/timeline?channel=%%E8%%83%%A1%%E9%%97%%B9P%%E6%%88%%BF&from=%d&to=%d'%(t-30*86400,t+3600)))
fs=tl['files']
done=[f for f in fs if f['status']=='done' and f['inPlaylist'] and not f['growing']]
live=[f for f in fs if f['growing'] and f['inPlaylist'] and f['duration']>=6]
sys.exit(0 if done and live else 1)" 2>/dev/null; then break; fi; sleep 1
done
sleep 3
# What the extension will get for the replay (its window is around the content's p0)
docker exec $NAME-server python -c "
import json,urllib.request,time
p0=$(python3 -c "import json; print(json.load(open('$WORK/media/meta.json'))['p0'])")
for label,a,b in (('replay window',p0+40-7200,p0+33609),('wide window',time.time()-30*86400,time.time()+3600)):
    tl=json.load(urllib.request.urlopen('http://127.0.0.1:8788/api/timeline?channel=%%E8%%83%%A1%%E9%%97%%B9P%%E6%%88%%BF&from=%f&to=%f'%(a,b)))
    print('  timeline', label, ': pieces', len(tl['pieces']), 'files', [(f['name'][:13], f['status'], f['inPlaylist'], round(f['duration'])) for f in tl['files']])
" 2>&1 | head -4

echo "== running headless Chrome"
docker run --rm $CAP_CHROME --ipc=host --network $NET -e FONE_SERVER=http://$NAME-server:8788 \
  -v "$PWD/extension":/ext:ro -v "$PWD/e2e":/e2e:ro -v "$WORK/media":/media:ro \
  -e E2E_DIAG_PROBE -e E2E_ONLY "$PW_IMAGE" sh -c 'pip install -q --break-system-packages playwright==1.63.0 >/dev/null 2>&1; python3 /e2e/test_e2e.py'
