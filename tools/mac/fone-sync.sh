#!/bin/sh
# Keeps ~/fone/extension in step with the build on the update server (tools/publish_ext.sh),
# for a Mac that can't reach the share. launchd runs it every minute (install-mac.sh); Chrome
# loads ~/fone/extension unpacked, and fone's own updater reloads it once a new build is in.
# The server's address is a placeholder in the repo, filled in (FONE_DIST_URL) by
# tools/publish_ext.sh in the copy it publishes; from the repo it needs FONE_DIST set.
set -eu
BASE="${FONE_DIST:-__FONE_DIST__}"
case $BASE in
  http://*|https://*) ;;
  *) echo "fone-sync: no update server: run it with FONE_DIST=http://<host>:8789 (or publish it with FONE_DIST_URL set)" >&2; exit 1 ;;
esac
DIR="$HOME/fone"
EXT="$DIR/extension"
build() { sed -n 's/.*"build": *"\([^"]*\)".*/\1/p' "$1" 2>/dev/null || true; }

mkdir -p "$DIR"
tmp=$(mktemp -d "$DIR/.dl.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
# Away from home or the server down: nothing to do until next time.
curl -fsS --max-time 10 -o "$tmp/build.json" "$BASE/build.json" 2>/dev/null || exit 0
want=$(build "$tmp/build.json")
[ -n "$want" ] || exit 0
[ "$want" = "$(build "$EXT/build.json")" ] && exit 0

curl -fsS --max-time 120 -o "$tmp/ext.tar.gz" "$BASE/fone-extension.tar.gz"
mkdir "$tmp/new"
tar -xzf "$tmp/ext.tar.gz" -C "$tmp/new"
# Published between our two requests: take it next time, whole.
[ "$(build "$tmp/new/build.json")" = "$want" ] || exit 0
[ -f "$tmp/new/manifest.json" ] || { echo "fone-sync: no manifest.json in the download" >&2; exit 1; }
# Swap in one rename, so Chrome never reads a half-written folder.
[ -d "$EXT" ] && mv "$EXT" "$tmp/old"
mv "$tmp/new" "$EXT"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) fone-sync: installed $want"
