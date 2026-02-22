#!/bin/bash
# Installs plex-lights as a background launchd service on macOS.
# Starts automatically on boot, restarts if it crashes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_LABEL="com.plex-lights"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$PLIST_LABEL.plist"
LOG_FILE="$SCRIPT_DIR/plex-lights.log"
SYSTEM_PYTHON="$(command -v python3 || true)"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python3"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SETUP_MODE=false
UNINSTALL_MODE=false

print_usage() {
    cat <<EOF
Usage: bash install.sh [OPTION]

Options:
  --setup      One-shot setup: create .venv, install dependencies, and install service.
  --uninstall  Stop and remove the launchd service.
  --help       Show this help message.

Examples:
  bash install.sh --setup
  bash install.sh
  bash install.sh --uninstall
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --setup)
            SETUP_MODE=true
            shift
            ;;
        --uninstall)
            UNINSTALL_MODE=true
            shift
            ;;
        --help|-h)
            print_usage
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            print_usage
            exit 1
            ;;
    esac
done

if [[ "$UNINSTALL_MODE" == true ]]; then
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

if [[ ! -f "$SCRIPT_DIR/requirements.txt" ]]; then
    echo -e "${RED}requirements.txt not found in $SCRIPT_DIR${NC}"
    exit 1
fi

if [[ -z "$SYSTEM_PYTHON" ]]; then
    echo -e "${RED}python3 not found in PATH. Install Python 3.8+ and retry.${NC}"
    exit 1
fi

if [[ "$SETUP_MODE" == true ]]; then
    if [[ ! -d "$VENV_DIR" ]]; then
        echo "Creating virtual environment at $VENV_DIR..."
        "$SYSTEM_PYTHON" -m venv "$VENV_DIR"
    fi

    echo "Installing dependencies into .venv..."
    "$VENV_PYTHON" -m pip install --upgrade pip >/dev/null
    "$VENV_PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt"
fi

SERVICE_PYTHON="$SYSTEM_PYTHON"
if [[ -x "$VENV_PYTHON" ]]; then
    SERVICE_PYTHON="$VENV_PYTHON"
fi

if ! "$SERVICE_PYTHON" -c "import requests" 2>/dev/null; then
    echo -e "${RED}Python requests module not installed for $SERVICE_PYTHON.${NC}"
    echo -e "${YELLOW}Run: bash install.sh --setup${NC}"
    exit 1
fi

if [[ ! -f "$SCRIPT_DIR/config.json" ]]; then
    if [[ "$SETUP_MODE" == true && -f "$SCRIPT_DIR/config.json.example" ]]; then
        cp "$SCRIPT_DIR/config.json.example" "$SCRIPT_DIR/config.json"
        echo -e "${YELLOW}Created config.json from config.json.example.${NC}"
        echo -e "${YELLOW}Edit config.json, then rerun: bash install.sh --setup${NC}"
        exit 0
    fi

    echo -e "${YELLOW}No config.json found. Copy config.json.example to config.json and edit it.${NC}"
    exit 1
fi

if ! "$SERVICE_PYTHON" -m json.tool "$SCRIPT_DIR/config.json" >/dev/null 2>&1; then
    echo -e "${RED}config.json is not valid JSON. Fix it before installing.${NC}"
    exit 1
fi

if ! VALIDATE_OUTPUT="$("$SERVICE_PYTHON" "$SCRIPT_DIR/plex-lights.py" --validate-config 2>&1)"; then
    echo -e "${RED}config validation failed.${NC}"
    echo "$VALIDATE_OUTPUT"
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
        <string>$SERVICE_PYTHON</string>
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
echo "  Python: $SERVICE_PYTHON"
echo "  Plist:  $PLIST_PATH"
echo "  Log:    $LOG_FILE"
echo ""
echo "Manage:"
echo "  launchctl kickstart -k gui/$(id -u)/$PLIST_LABEL   # restart"
echo "  launchctl bootout gui/$(id -u)/$PLIST_LABEL         # stop"
echo "  bash install.sh --setup                              # refresh venv + deps"
echo "  bash install.sh --uninstall                          # remove"
