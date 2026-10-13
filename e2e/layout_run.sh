#!/usr/bin/env bash
# Split view (extension/layout.js) end to end: headless Chromium with the extension on a mock of
# F1 TV's real player DOM (e2e/layout_mock.html). No fone server; the test container runs with
# --network none.
#   e2e/layout_run.sh                  both fullscreen variants: with layout_page.js (main world)
#                                      in the manifest, and without it
#   FSWRAP=1 e2e/layout_run.sh         only one of them
#   SHOTS=dir e2e/layout_run.sh        keep the screenshots there (default: a temp dir, printed)
#   DASH=real e2e/layout_run.sh        keep extension/dashboard.html instead of the stub (skips the
#                                      checks that read the stub's records)
# The extension under test is a temp copy whose manifest is wired the way the integration should
# be (layout.js after content.js; layout_page.js in the MAIN world; dashboard.html web-accessible),
# with e2e/layout_dashboard_stub.* as dashboard.html.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=${PW_IMAGE:-mcr.microsoft.com/playwright/python:v1.63.0-noble}
IMAGE=${LAYOUT_IMAGE:-fone-dev-layout-pw:latest} # $BASE + playwright + pillow, built once
NAME=${LAYOUT_NAME:-fone-dev-layout-test}
FFMPEG_IMAGE=${FFMPEG_IMAGE:-fone:latest} # any image with ffmpeg/libx264; only runs ffmpeg
DASH=${DASH:-stub}
WORK=$(mktemp -d)
SHOTS=${SHOTS:-$(mktemp -d)}
mkdir -p "$SHOTS"
cleanup() {
  docker rm -f "$NAME-1" "$NAME-0" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "== building $IMAGE"
  docker build -q -t "$IMAGE" - >/dev/null <<EOF
FROM $BASE
RUN pip install -q --break-system-packages playwright==1.63.0 pillow
EOF
fi

echo "== test pattern video (16:9, yellow border)"
mkdir -p "$WORK/media" && chmod 777 "$WORK/media"
docker run --rm --cpus 2 --cpu-shares 256 -v "$WORK/media":/m "$FFMPEG_IMAGE" ffmpeg -v error -y -f lavfi \
  -i "testsrc2=s=1280x720:r=25:d=30,drawbox=x=0:y=0:w=iw:h=ih:color=yellow:t=12" \
  -c:v libx264 -preset ultrafast -pix_fmt yuv420p -g 25 -movflags +faststart /m/test.mp4

# extension copy wired for the split view; $1 = 1: with layout_page.js
prepare() {
  local dir="$WORK/ext$1"
  cp -r extension "$dir"
  python3 - "$dir/manifest.json" "$1" <<'EOF'
import json, sys
path, fswrap = sys.argv[1], sys.argv[2] == "1"
m = json.load(open(path))
iso = next(c for c in m["content_scripts"] if c.get("world") != "MAIN")
main = next(c for c in m["content_scripts"] if c.get("world") == "MAIN")
if "layout.js" not in iso["js"]:
    iso["js"].append("layout.js")
if fswrap and "layout_page.js" not in main["js"]:
    main["js"].append("layout_page.js")
if not fswrap and "layout_page.js" in main["js"]:
    main["js"].remove("layout_page.js")
war = m.setdefault("web_accessible_resources", [])
if not any("dashboard.html" in w.get("resources", []) for w in war):
    war.append({"resources": ["dashboard.html"], "matches": ["https://f1tv.formula1.com/*"]})
json.dump(m, open(path, "w"), indent=2, ensure_ascii=False)
EOF
  if [ "$DASH" = stub ] || [ ! -f "$dir/dashboard.html" ]; then
    cp e2e/layout_dashboard_stub.html "$dir/dashboard.html"
    cp e2e/layout_dashboard_stub.js "$dir/dashboard_stub.js"
  fi
}

run() {
  prepare "$1"
  echo "== headless Chromium, layout_page.js $([ "$1" = 1 ] && echo loaded || echo not loaded)"
  docker run --rm --name "$NAME-$1" --network none --ipc=host --cpus 4 --cpu-shares 256 \
    --user "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e FSWRAP="$1" -e DASH="$DASH" -e SHOTS=/shots \
    -v "$WORK/ext$1":/ext:ro -v "$PWD/e2e":/e2e:ro -v "$WORK/media":/media:ro -v "$SHOTS":/shots \
    "$IMAGE" python3 /e2e/layout_test.py
}

if [ -n "${FSWRAP:-}" ]; then run "$FSWRAP"; else run 1; run 0; fi
echo "screenshots: $SHOTS"
