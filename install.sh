#!/bin/bash
# Installs plex-lights as a background launchd service on macOS.
# Starts automatically on boot, restarts if it crashes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_LABEL="com.plex-lights"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$PLIST_LABEL.plist"
PYTHON="$(command -v python3)"
LOG_FILE="$SCRIPT_DIR/plex-lights.log"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [[ "${1:-}" == "--uninstall" ]]; then
    echo "Uninstalling plex-lights..."
    launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo -e "${GREEN}Uninstalled.${NC}"
    exit 0
fi

if [[ ! -f "$SCRIPT_DIR/plex-lights.py" ]]; then
    echo -e "${RED}plex-lights.py not found in $SCRIPT_DIR${NC}"
    exit 1
fi

if [[ -z "$PYTHON" ]]; then
    echo -e "${RED}python3 not found in PATH. Install Python 3.8+ and retry.${NC}"
    exit 1
fi

if ! "$PYTHON" -c "import requests" 2>/dev/null; then
    echo -e "${RED}Python requests module not installed. Run: pip install -r requirements.txt${NC}"
    exit 1
fi

if [[ ! -f "$SCRIPT_DIR/config.json" ]]; then
    echo -e "${YELLOW}No config.json found. Copy config.json.example to config.json and edit it.${NC}"
    exit 1
fi

if ! "$PYTHON" -m json.tool "$SCRIPT_DIR/config.json" >/dev/null 2>&1; then
    echo -e "${RED}config.json is not valid JSON. Fix it before installing.${NC}"
    exit 1
fi

mkdir -p "$PLIST_DIR"

cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT_DIR/plex-lights.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_FILE</string>
    <key>StandardErrorPath</key>
    <string>$LOG_FILE</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

echo -e "${GREEN}Installed and started.${NC}"
echo "  Plist: $PLIST_PATH"
echo "  Log:   $LOG_FILE"
echo ""
echo "Manage:"
echo "  launchctl kickstart -k gui/$(id -u)/$PLIST_LABEL   # restart"
echo "  launchctl bootout gui/$(id -u)/$PLIST_LABEL         # stop"
echo "  bash install.sh --uninstall                          # remove"
