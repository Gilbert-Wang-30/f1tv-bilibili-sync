#!/usr/bin/env bash
# Integration test of the live car-position relay (extension/livepos.js + f1token.js):
#   mock F1 SignalR Core server (mock_f1.py, plays a recorded Position.z archive)
#   -> livepos.js in Node 22 (relay_node.js) and in headless Chromium as the real extension
#      setup: background reads a (fake) login cookie, offscreen document relays (relay_chrome.py)
#   -> a dev fone server (fone:dev-livepos), whose GET /positions must serve the samples back.
#   Once the extension is integrated (offscreen.html loads livepos.js), also the extension
#   itself: real background + offscreen engine on a LIVE video (relay_extension.py).
# Everything runs on an --internal Docker network: no traffic to F1, OpenF1 or Bilibili.
#   e2e/livepos/run.sh             all phases     LIVEPOS_ONLY=node|chrome|ext   one of them
#   LIVEPOS_EXT=<dir>              the extension to test in the ext phase (default: extension/)
set -euo pipefail
cd "$(dirname "$0")/../.."
NAME=fone-dev-livepos
NET=$NAME-net
FIXTURE=server/tests/fixtures/timing/static/2026/2026-10-04_Bahrain_Grand_Prix/2026-10-03_Qualifying/Position.z.jsonStream.gz
PW_IMAGE=${PW_IMAGE:-mcr.microsoft.com/playwright/python:v1.63.0-noble}
CAP="--cpus 2 --cpu-shares 256"
EXT=${LIVEPOS_EXT:-$PWD/extension}
CID=1000099901 SESSION=90201  # a LIVE content the dev server is told about (ext phase)
phase() { [ -z "${LIVEPOS_ONLY:-}" ] || [ "$LIVEPOS_ONLY" = "$1" ]; }
WORK=$(mktemp -d)
cleanup() {
  local rc=$?
  if [ $rc != 0 ]; then
    echo "== mock log (tail)"; docker logs --tail 20 $NAME-mock 2>&1 || true
    echo "== server log (tail)"; docker logs --tail 20 $NAME-server 2>&1 || true
  fi
  docker rm -f $NAME-server $NAME-mock $NAME-node $NAME-chrome $NAME-ext >/dev/null 2>&1 || true
  docker network rm $NET >/dev/null 2>&1 || true
  docker run --rm -v "$WORK":/w alpine rm -rf /w/data /w/rec >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "== building images"
docker build -q -t fone:dev-livepos server >/dev/null
docker build -q -t $NAME-mock - >/dev/null <<'EOF'
FROM python:3.12-alpine
RUN apk add --no-cache openssl && pip install --no-cache-dir aiohttp==3.11.18
EOF
if phase chrome || phase ext; then
  docker build -q -t $NAME-pw - >/dev/null <<EOF
FROM $PW_IMAGE
RUN pip install -q --break-system-packages playwright==1.63.0
EOF
fi

mkdir -p "$WORK/data" "$WORK/rec"
chmod -R 777 "$WORK"
# For the engine (ext phase): one short recording, so the channel exists, and the LIVE content
# seeded in the content cache (F1 TV's API is out of reach), answered for the next 10 min.
docker run --rm $CAP -v "$WORK/rec":/rec -v "$WORK/data":/data fone:dev-livepos sh -c "
  mkdir -p /rec/livepos-test && ffmpeg -v error -y -f lavfi -i sine=frequency=440:sample_rate=48000:duration=20 \
    -f lavfi -i color=c=black:s=64x36:r=5:d=20 -c:a aac -b:a 64k -c:v libx264 -preset ultrafast -g 10 -shortest \
    -f flv '/rec/livepos-test/20261010-090000-000-livepos test.flv' && python3 -c '
import time
from pathlib import Path
from fone.store import Store
Store(Path(\"/data/fone.sqlite3\")).put_content(\"$CID\", {\"contentId\": \"$CID\", \"title\": \"livepos test - Race\",
  \"subtype\": \"LIVE\", \"sessionKey\": $SESSION, \"meetingKey\": 1308, \"sessionName\": \"Race\", \"kind\": \"race\",
  \"isSessionFeed\": True, \"anchor\": None, \"p0\": None, \"scheduledStart\": time.time() - 600, \"streams\": []})'"
docker network create --internal $NET >/dev/null

# Dev fone server: timing only, no live capture or archive downloads (nothing to reach anyway).
docker run -d $CAP --name $NAME-server --network $NET -v "$WORK/rec":/rec:ro -v "$WORK/data":/data \
  -e FONE_TIMING_RECORD=0 -e FONE_TIMING_ARCHIVE_DAYS=0 -e FONE_ADS=0 -e FONE_SCREENCLOCK=0 -e FONE_VIDEO=0 \
  -e FONE_BACKFILL=0 fone:dev-livepos >/dev/null
docker run -d $CAP --name $NAME-mock --network $NET -e FONE_URL=http://$NAME-server:8788 -e MOCK_TLS=1 \
  -v "$PWD/e2e/livepos":/t:ro -v "$PWD/$FIXTURE":/fixture/Position.z.jsonStream.gz:ro \
  $NAME-mock python -I /t/mock_f1.py /fixture/Position.z.jsonStream.gz >/dev/null

echo "== waiting for the dev server and the mock"
for i in $(seq 1 60); do
  if docker exec $NAME-mock python -c "
import urllib.request as u
u.urlopen('http://$NAME-server:8788/api/health', timeout=2); u.urlopen('http://127.0.0.1:8080/_mock/health', timeout=2)" 2>/dev/null
  then break; fi
  sleep 1
done

rc=0
if phase node; then
  echo "== livepos.js in Node 22"
  docker run --rm $CAP --name $NAME-node --network $NET \
    -e MOCK=http://$NAME-mock:8080 -e FONE=http://$NAME-server:8788 -e LIVEPOS=/ext/livepos.js \
    -e FIXTURE=/fixture/Position.z.jsonStream.gz \
    -v "$PWD/extension":/ext:ro -v "$PWD/e2e/livepos":/t:ro -v "$PWD/$FIXTURE":/fixture/Position.z.jsonStream.gz:ro \
    node:22-alpine node /t/relay_node.js || rc=1
fi
if phase chrome; then
  echo "== livepos.js + f1token.js in headless Chromium (extension: background + offscreen document)"
  mkdir -p "$WORK/ext"
  cp extension/livepos.js extension/f1token.js e2e/livepos/ext/* "$WORK/ext/"
  docker run --rm --cpus 4 --cpu-shares 256 --ipc=host --name $NAME-chrome --network $NET \
    -e MOCK=http://$NAME-mock:8080 -e MOCK_TLS_BASE=https://$NAME-mock:8443 -e FONE=http://$NAME-server:8788 \
    -v "$WORK/ext":/ext:ro -v "$PWD/e2e/livepos":/t:ro $NAME-pw python3 /t/relay_chrome.py || rc=1
fi
if phase ext; then
  if grep -q 'livepos.js' "$EXT/offscreen.html"; then
    echo "== the integrated extension ($EXT): engine on a LIVE video, livetiming.formula1.com mapped to the mock"
    docker run --rm --cpus 4 --cpu-shares 256 --ipc=host --name $NAME-ext --network $NET \
      -e MOCK=http://$NAME-mock:8080 -e FONE=http://$NAME-server:8788 -e CID=$CID -e SESSION=$SESSION \
      -v "$EXT":/ext:ro -v "$PWD/e2e/livepos":/t:ro $NAME-pw python3 /t/relay_extension.py || rc=1
  else
    echo "== integrated extension: skipped ($EXT/offscreen.html doesn't load livepos.js yet)"
  fi
fi
exit $rc
