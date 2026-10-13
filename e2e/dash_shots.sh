#!/usr/bin/env bash
# Stats dashboard (extension/dashboard.html) in headless Chromium, in Docker: screenshots of
# the scenarios in dash_shots.py plus behaviour checks, against a fone server's timing routes
# (GET requests only). Default server: the production one on this box.
#   e2e/dash_shots.sh [out_dir]            (default /tmp/dash-shots)
#   DASH_SERVER=http://host:port e2e/dash_shots.sh
#   DASH_ONLY=iframe1080_sq1 e2e/dash_shots.sh   (scenarios whose name contains this)
#   DASH_NOPROXY=1: leave dashproxy.js out of the service worker (the iframe's fallback path)
#   DASH_FAKE_PORT=8798: the harness's GET-only forwarder to the server (adds an onboard
#     stream list while the server has none); a free port on this box, dev range 8795-8799
set -euo pipefail
cd "$(dirname "$0")/.."
PW_IMAGE=${PW_IMAGE:-mcr.microsoft.com/playwright/python:v1.63.0-noble}
SERVER=${DASH_SERVER:-http://localhost:8788}
OUT=$(realpath -m "${1:-/tmp/dash-shots}")
NAME=${DASH_NAME:-fone-dev-dash-shots} # dev containers: fone-dev-dash-*
WORK=$(mktemp -d)
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$OUT"

# The extension as it ships with the dashboard wired in: dashboard.html web-accessible on
# F1 TV, dashproxy.js loaded by the service worker. A no-op once manifest.json and
# background.js have them.
cp -r extension "$WORK/ext"
python3 - "$WORK/ext" "${DASH_NOPROXY:-0}" <<'EOF'
import json, sys
from pathlib import Path
ext = Path(sys.argv[1])
m = json.loads((ext / "manifest.json").read_text())
war = m.setdefault("web_accessible_resources", [])
if not any("dashboard.html" in e.get("resources", []) for e in war):
    war.append({"resources": ["dashboard.html"], "matches": ["https://f1tv.formula1.com/*"]})
(ext / "manifest.json").write_text(json.dumps(m, indent=2))
bg = ext / "background.js"
src = bg.read_text()
if sys.argv[2] == "1":  # the iframe's fallback: no proxy, it fetches directly
    src = "\n".join(l for l in src.split("\n") if not l.startswith("importScripts('dashproxy.js')"))
elif "dashproxy.js" not in src:
    src += "\nimportScripts('dashproxy.js');\n"
bg.write_text(src)
EOF

# --network host: the server is on this box's localhost. The screenshots are handed back to
# the calling user.
docker run --rm --name "$NAME" --cpus 4 --cpu-shares 256 --network host --ipc=host \
  -e DASH_SERVER="$SERVER" -e DASH_ONLY="${DASH_ONLY:-}" -e DASH_NOPROXY="${DASH_NOPROXY:-0}" \
  -e DASH_FAKE_PORT="${DASH_FAKE_PORT:-8798}" -e OWNER="$(id -u):$(id -g)" \
  -v "$WORK/ext":/ext:ro -v "$PWD/e2e":/e2e:ro -v "$OUT":/out \
  "$PW_IMAGE" sh -c 'pip install -q --break-system-packages playwright==1.63.0 >/dev/null 2>&1
    python3 /e2e/dash_shots.py; rc=$?; chown -R "$OWNER" /out; exit $rc'
echo "screenshots: $OUT"
