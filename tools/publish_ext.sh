#!/usr/bin/env bash
# Publish the extension where Chrome loads it from (unpacked):
#  - a network share (Windows, at home): DEST, loaded straight from the share
#  - the update server (fone-ext, port 8789, serving DIST): build.json, the build as
#    fone-extension.tar.gz, and the Mac scripts (tools/mac: a launchd job on the Mac keeps
#    ~/fone/extension in step with it, over Tailscale).
# Where that is, and which fone servers the build points at, is this install's own business:
# tools/publish.env (gitignored; copy tools/publish.env.example) sets it, and the same
# variables in the environment win over the file:
#   FONE_EXT_DEST  the folder Chrome loads unpacked   FONE_EXT_DIST  the folder fone-ext serves
#   FONE_SERVERS   the build's default servers, comma-separated, written into local.js
#   FONE_DIST_URL  the update server as a Mac reaches it, written into the Mac scripts
# Installed copies see the new build.json on the update server and reload themselves once it
# has been up for ~75 s and nothing is playing (background.js). The build id is also stamped
# into build.js, which the running code reads once at load.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f tools/publish.env ]; then
  for v in FONE_EXT_DEST FONE_EXT_DIST FONE_SERVERS FONE_DIST_URL; do
    [ -n "${!v:-}" ] || declare "$v=$(. tools/publish.env; printf '%s' "${!v:-}")"
  done
fi
if [ -z "${FONE_EXT_DEST:-}" ] || [ -z "${FONE_EXT_DIST:-}" ]; then
  echo "publish_ext.sh: FONE_EXT_DEST (the folder Chrome loads unpacked) and FONE_EXT_DIST (the folder" >&2
  echo "the fone-ext update server serves) are not set: copy tools/publish.env.example to tools/publish.env" >&2
  exit 1
fi
DEST=$FONE_EXT_DEST
DIST=$FONE_EXT_DIST
[ -n "${FONE_SERVERS:-}" ] || echo "publish_ext.sh: FONE_SERVERS is empty: installed copies will ask for the server in fone's settings" >&2
[ -n "${FONE_DIST_URL:-}" ] || echo "publish_ext.sh: FONE_DIST_URL is empty: the Mac scripts will only run with FONE_DIST set" >&2
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir "$STAGE/ext"
cp -r extension/. "$STAGE/ext/"
rm -f "$STAGE/ext"/*.test.js
# The build's default servers go into local.js (a placeholder in the repo), as JSON so any URL survives.
python3 - "$STAGE/ext/local.js" "${FONE_SERVERS:-}" <<'PY'
import json, sys
servers = [s.strip() for s in sys.argv[2].split(",") if s.strip()]
open(sys.argv[1], "w").write("self.FONE_LOCAL = %s;\n" % json.dumps({"servers": servers}))
PY
# The Mac scripts learn the update server's address the same way (their __FONE_DIST__).
for f in install-mac.sh fone-sync.sh; do
  sed "s|__FONE_DIST__|${FONE_DIST_URL:-__FONE_DIST__}|g" "tools/mac/$f" > "$STAGE/$f"
done
VERSION=$(python3 -c "import json; print(json.load(open('extension/manifest.json'))['version'])")
HASH=$(cd "$STAGE/ext" && find . -type f ! -name build.json -print0 | sort -z | xargs -0 sha1sum | sha1sum | cut -c1-10)
BUILD="$VERSION+$(date -u +%Y%m%d%H%M%S).$HASH"
printf "self.FONE_BUILD = '%s';\n" "$BUILD" > "$STAGE/ext/build.js"
printf '{"build": "%s", "version": "%s"}\n' "$BUILD" "$VERSION" > "$STAGE/ext/build.json"
chmod -R a+rX "$STAGE/ext"
tar -czf "$STAGE/fone-extension.tar.gz" -C "$STAGE/ext" .

# Code first, build.json last, everywhere: an installed copy only reloads once build.json says
# so. (rsync and the renames give new files, never rewriting one a client may have cached.)
mkdir -p "$DEST" "$DIST"
rsync -rt --delete --exclude build.json "$STAGE/ext/" "$DEST/"
install -m 664 "$STAGE/ext/build.json" "$DEST/.build.json.tmp"
mv -f "$DEST/.build.json.tmp" "$DEST/build.json"
for f in "$STAGE/fone-extension.tar.gz" "$STAGE/install-mac.sh" "$STAGE/fone-sync.sh"; do
  install -m 664 "$f" "$DIST/.$(basename "$f").tmp"
  mv -f "$DIST/.$(basename "$f").tmp" "$DIST/$(basename "$f")"
done
install -m 664 "$STAGE/ext/build.json" "$DIST/.build.json.tmp"
mv -f "$DIST/.build.json.tmp" "$DIST/build.json"
echo "published $BUILD -> $DEST, $DIST"
