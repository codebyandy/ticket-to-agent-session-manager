#!/bin/bash
# Run the dashboard at login and keep it running (macOS launchd).
# Usage: scripts/install-service.sh            install / reinstall
#        scripts/install-service.sh uninstall  remove
set -euo pipefail

LABEL=com.ticket-to-agent-session-manager
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$DIR/server.log"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = uninstall ]; then rm -f "$PLIST"; echo "Removed $LABEL"; exit 0; fi

PY="$(command -v python3)"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Needs Python 3.10+ ($PY is older)"; exit 1; }
GH_DIR="$(dirname "$(command -v gh)")" || { echo "Install the GitHub CLI (gh) first"; exit 1; }

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/dashboard.py</string><string>--serve</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$GH_DIR:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PLIST

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Running at http://localhost:$(grep -E '^PORT=' "$DIR/.env" 2>/dev/null | cut -d= -f2 || echo 7777)  (log: $LOG)"
