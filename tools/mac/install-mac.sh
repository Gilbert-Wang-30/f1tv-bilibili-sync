#!/bin/sh
# One-time setup of fone on a Mac (run in Terminal):
#   curl -fsSL __FONE_DIST__/install-mac.sh | sh
# Puts the extension in ~/fone/extension and a launchd job (fone.sync) that keeps it up to
# date from the update server (fone-ext) every minute. Then load ~/fone/extension in
# chrome://extensions (once).
# Undo: launchctl bootout gui/$(id -u)/fone.sync; rm ~/Library/LaunchAgents/fone.sync.plist; rm -r ~/fone
# The update server's address is a placeholder in the repo: tools/publish_ext.sh fills it in
# (FONE_DIST_URL) in the copy it publishes. Run from the repo, it needs FONE_DIST set.
set -eu
BASE="${FONE_DIST:-__FONE_DIST__}"
case $BASE in
  http://*|https://*) ;;
  *) echo "install-mac.sh: no update server: run it with FONE_DIST=http://<host>:8789 (or publish it with FONE_DIST_URL set)" >&2; exit 1 ;;
esac
DIR="$HOME/fone"
PLIST="$HOME/Library/LaunchAgents/fone.sync.plist"

mkdir -p "$DIR" "$HOME/Library/LaunchAgents"
curl -fsS --max-time 20 -o "$DIR/fone-sync.sh" "$BASE/fone-sync.sh"
chmod +x "$DIR/fone-sync.sh"
FONE_DIST="$BASE" /bin/sh "$DIR/fone-sync.sh"
[ -f "$DIR/extension/manifest.json" ] || { echo "Couldn't download fone from $BASE (is Tailscale on?)" >&2; exit 1; }

cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>fone.sync</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>$DIR/fone-sync.sh</string></array>
  <key>EnvironmentVariables</key><dict><key>FONE_DIST</key><string>$BASE</string></dict>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/sync.log</string>
  <key>StandardErrorPath</key><string>$DIR/sync.log</string>
</dict>
</plist>
PL
launchctl bootout "gui/$(id -u)/fone.sync" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "fone is in $DIR/extension and stays up to date by itself (log: $DIR/sync.log)."
echo "Last step, once: chrome://extensions -> Developer mode (top right) -> Load unpacked ->"
echo "press Cmd+Shift+G, type ~/fone/extension, Enter, then Select."
