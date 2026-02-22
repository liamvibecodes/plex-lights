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
CONFIG_PATH="$SCRIPT_DIR/config.json"
CONFIG_EXAMPLE_PATH="$SCRIPT_DIR/config.json.example"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SETUP_MODE=false
UNINSTALL_MODE=false
WIZARD_MODE=false

print_usage() {
    cat <<EOF
Usage: bash install.sh [OPTION]

Options:
  --setup      One-shot setup: create .venv, install dependencies, and install service.
  --wizard     Interactive setup wizard to build config.json.
  --uninstall  Stop and remove the launchd service.
  --help       Show this help message.

Examples:
  bash install.sh --wizard
  bash install.sh --wizard --setup
  bash install.sh --setup
  bash install.sh
  bash install.sh --uninstall
EOF
}

prompt_text() {
    local prompt="$1"
    local default_value="$2"
    local out_var="$3"
    local value=""

    if [[ -n "$default_value" ]]; then
        printf "%s [%s]: " "$prompt" "$default_value"
    else
        printf "%s: " "$prompt"
    fi

    if ! read -r value; then
        value=""
    fi

    if [[ -z "$value" ]]; then
        value="$default_value"
    fi

    printf -v "$out_var" "%s" "$value"
}

prompt_required_text() {
    local prompt="$1"
    local out_var="$2"
    local value=""

    while true; do
        printf "%s: " "$prompt"
        if ! read -r value; then
            value=""
        fi

        if [[ -n "$value" ]]; then
            printf -v "$out_var" "%s" "$value"
            return
        fi

        echo -e "${YELLOW}This value is required.${NC}"
    done
}

prompt_yes_no() {
    local prompt="$1"
    local default_value="$2"
    local out_var="$3"
    local value=""
    local normalized=""

    while true; do
        if [[ "$default_value" == "y" ]]; then
            printf "%s [Y/n]: " "$prompt"
        else
            printf "%s [y/N]: " "$prompt"
        fi

        if ! read -r value; then
            value=""
        fi

        if [[ -z "$value" ]]; then
            value="$default_value"
        fi

        normalized="$(printf "%s" "$value" | tr '[:upper:]' '[:lower:]')"
        case "$normalized" in
            y|yes)
                printf -v "$out_var" "%s" "true"
                return
                ;;
            n|no)
                printf -v "$out_var" "%s" "false"
                return
                ;;
            *)
                echo -e "${YELLOW}Please enter y or n.${NC}"
                ;;
        esac
    done
}

run_wizard() {
    local overwrite="false"
    local any_provider_enabled="false"

    if [[ -f "$CONFIG_PATH" ]]; then
        prompt_yes_no "config.json already exists. Overwrite it" "n" overwrite
        if [[ "$overwrite" != "true" ]]; then
            echo -e "${YELLOW}Keeping existing config.json.${NC}"
            return
        fi
    fi

    if [[ ! -f "$CONFIG_EXAMPLE_PATH" ]]; then
        echo -e "${RED}config.json.example not found in $SCRIPT_DIR${NC}"
        exit 1
    fi

    echo ""
    echo "Plex Lights setup wizard"
    echo ""

    prompt_text "Port" "32500" WIZARD_PORT
    prompt_text "Webhook token (optional, recommended)" "" WIZARD_WEBHOOK_TOKEN
    prompt_text "TV player name filter (optional)" "" WIZARD_TV_PLAYER_NAME
    prompt_yes_no "Enable dry-run mode by default" "n" WIZARD_DRY_RUN

    while true; do
        any_provider_enabled="false"

        prompt_yes_no "Enable Philips Hue" "n" WIZARD_HUE_ENABLED
        if [[ "$WIZARD_HUE_ENABLED" == "true" ]]; then
            any_provider_enabled="true"
            prompt_required_text "Hue bridge IP (e.g. 192.168.1.10)" WIZARD_HUE_BRIDGE_IP
            prompt_required_text "Hue API user" WIZARD_HUE_API_USER
            prompt_required_text "Hue light IDs (comma-separated, e.g. 1,2,3)" WIZARD_HUE_LIGHTS
        else
            WIZARD_HUE_BRIDGE_IP=""
            WIZARD_HUE_API_USER=""
            WIZARD_HUE_LIGHTS=""
        fi

        prompt_yes_no "Enable Govee" "n" WIZARD_GOVEE_ENABLED
        if [[ "$WIZARD_GOVEE_ENABLED" == "true" ]]; then
            any_provider_enabled="true"
            prompt_required_text "Govee API key" WIZARD_GOVEE_API_KEY
            prompt_required_text "Govee device ID" WIZARD_GOVEE_DEVICE
            prompt_required_text "Govee model/SKU" WIZARD_GOVEE_MODEL
        else
            WIZARD_GOVEE_API_KEY=""
            WIZARD_GOVEE_DEVICE=""
            WIZARD_GOVEE_MODEL=""
        fi

        prompt_yes_no "Enable Home Assistant" "n" WIZARD_HA_ENABLED
        if [[ "$WIZARD_HA_ENABLED" == "true" ]]; then
            any_provider_enabled="true"
            prompt_required_text "Home Assistant URL (e.g. http://homeassistant.local:8123)" WIZARD_HA_URL
            prompt_required_text "Home Assistant long-lived token" WIZARD_HA_TOKEN
            prompt_yes_no "Verify Home Assistant SSL certificate" "y" WIZARD_HA_VERIFY_SSL
            prompt_text "Home Assistant transition seconds" "1" WIZARD_HA_TRANSITION_SECONDS
            prompt_text "Home Assistant light entity IDs (comma-separated, optional)" "" WIZARD_HA_ENTITY_IDS
            prompt_yes_no "Use Home Assistant scenes for modes" "n" WIZARD_HA_USE_SCENES
            if [[ "$WIZARD_HA_USE_SCENES" == "true" ]]; then
                prompt_text "Scene entity for movie mode (optional)" "" WIZARD_HA_SCENE_MOVIE
                prompt_text "Scene entity for pause mode (optional)" "" WIZARD_HA_SCENE_PAUSE
                prompt_text "Scene entity for normal mode (optional)" "" WIZARD_HA_SCENE_NORMAL
            else
                WIZARD_HA_SCENE_MOVIE=""
                WIZARD_HA_SCENE_PAUSE=""
                WIZARD_HA_SCENE_NORMAL=""
            fi
        else
            WIZARD_HA_URL=""
            WIZARD_HA_TOKEN=""
            WIZARD_HA_VERIFY_SSL="true"
            WIZARD_HA_TRANSITION_SECONDS="1"
            WIZARD_HA_ENTITY_IDS=""
            WIZARD_HA_USE_SCENES="false"
            WIZARD_HA_SCENE_MOVIE=""
            WIZARD_HA_SCENE_PAUSE=""
            WIZARD_HA_SCENE_NORMAL=""
        fi

        if [[ "$any_provider_enabled" == "true" ]]; then
            break
        fi

        echo -e "${YELLOW}Enable at least one provider (Hue, Govee, or Home Assistant).${NC}"
        echo ""
    done

    WIZARD_CONFIG_EXAMPLE_PATH="$CONFIG_EXAMPLE_PATH" \
    WIZARD_CONFIG_PATH="$CONFIG_PATH" \
    WIZARD_PORT="$WIZARD_PORT" \
    WIZARD_WEBHOOK_TOKEN="$WIZARD_WEBHOOK_TOKEN" \
    WIZARD_TV_PLAYER_NAME="$WIZARD_TV_PLAYER_NAME" \
    WIZARD_DRY_RUN="$WIZARD_DRY_RUN" \
    WIZARD_HUE_ENABLED="$WIZARD_HUE_ENABLED" \
    WIZARD_HUE_BRIDGE_IP="$WIZARD_HUE_BRIDGE_IP" \
    WIZARD_HUE_API_USER="$WIZARD_HUE_API_USER" \
    WIZARD_HUE_LIGHTS="$WIZARD_HUE_LIGHTS" \
    WIZARD_GOVEE_ENABLED="$WIZARD_GOVEE_ENABLED" \
    WIZARD_GOVEE_API_KEY="$WIZARD_GOVEE_API_KEY" \
    WIZARD_GOVEE_DEVICE="$WIZARD_GOVEE_DEVICE" \
    WIZARD_GOVEE_MODEL="$WIZARD_GOVEE_MODEL" \
    WIZARD_HA_ENABLED="$WIZARD_HA_ENABLED" \
    WIZARD_HA_URL="$WIZARD_HA_URL" \
    WIZARD_HA_TOKEN="$WIZARD_HA_TOKEN" \
    WIZARD_HA_VERIFY_SSL="$WIZARD_HA_VERIFY_SSL" \
    WIZARD_HA_TRANSITION_SECONDS="$WIZARD_HA_TRANSITION_SECONDS" \
    WIZARD_HA_ENTITY_IDS="$WIZARD_HA_ENTITY_IDS" \
    WIZARD_HA_SCENE_MOVIE="$WIZARD_HA_SCENE_MOVIE" \
    WIZARD_HA_SCENE_PAUSE="$WIZARD_HA_SCENE_PAUSE" \
    WIZARD_HA_SCENE_NORMAL="$WIZARD_HA_SCENE_NORMAL" \
    "$SYSTEM_PYTHON" - <<'PY'
import json
import os
import re
import sys
from pathlib import Path


def as_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def parse_list(values):
    return [item.strip() for item in values.split(",") if item.strip()]


def parse_int_list(values, field_name):
    parsed = []
    for item in parse_list(values):
        if not re.fullmatch(r"\d+", item):
            raise ValueError(f"{field_name} must contain only integers")
        parsed.append(int(item))
    return parsed


example_path = Path(os.environ["WIZARD_CONFIG_EXAMPLE_PATH"])
config_path = Path(os.environ["WIZARD_CONFIG_PATH"])

cfg = json.loads(example_path.read_text(encoding="utf-8"))

port_raw = os.environ.get("WIZARD_PORT", "32500").strip()
if not re.fullmatch(r"\d+", port_raw):
    raise ValueError("Port must be a number")
cfg["port"] = int(port_raw)
cfg["webhook_token"] = os.environ.get("WIZARD_WEBHOOK_TOKEN", "").strip()
cfg["tv_player_name"] = os.environ.get("WIZARD_TV_PLAYER_NAME", "").strip()
cfg["dry_run"] = as_bool(os.environ.get("WIZARD_DRY_RUN", "false"))

hue_enabled = as_bool(os.environ.get("WIZARD_HUE_ENABLED", "false"))
cfg["hue"]["enabled"] = hue_enabled
if hue_enabled:
    cfg["hue"]["bridge_ip"] = os.environ.get("WIZARD_HUE_BRIDGE_IP", "").strip()
    cfg["hue"]["api_user"] = os.environ.get("WIZARD_HUE_API_USER", "").strip()
    cfg["hue"]["lights"] = parse_int_list(os.environ.get("WIZARD_HUE_LIGHTS", ""), "Hue lights")
else:
    cfg["hue"].update({"bridge_ip": "", "api_user": "", "lights": []})

govee_enabled = as_bool(os.environ.get("WIZARD_GOVEE_ENABLED", "false"))
cfg["govee"]["enabled"] = govee_enabled
if govee_enabled:
    cfg["govee"]["api_key"] = os.environ.get("WIZARD_GOVEE_API_KEY", "").strip()
    cfg["govee"]["device"] = os.environ.get("WIZARD_GOVEE_DEVICE", "").strip()
    cfg["govee"]["model"] = os.environ.get("WIZARD_GOVEE_MODEL", "").strip()
else:
    cfg["govee"].update({"api_key": "", "device": "", "model": ""})

ha_enabled = as_bool(os.environ.get("WIZARD_HA_ENABLED", "false"))
cfg["home_assistant"]["enabled"] = ha_enabled
if ha_enabled:
    transition_raw = os.environ.get("WIZARD_HA_TRANSITION_SECONDS", "1").strip()
    if not re.fullmatch(r"\d+", transition_raw):
        raise ValueError("Home Assistant transition_seconds must be a number")

    cfg["home_assistant"]["url"] = os.environ.get("WIZARD_HA_URL", "").strip()
    cfg["home_assistant"]["token"] = os.environ.get("WIZARD_HA_TOKEN", "").strip()
    cfg["home_assistant"]["verify_ssl"] = as_bool(os.environ.get("WIZARD_HA_VERIFY_SSL", "true"))
    cfg["home_assistant"]["transition_seconds"] = int(transition_raw)
    cfg["home_assistant"]["entity_ids"] = parse_list(os.environ.get("WIZARD_HA_ENTITY_IDS", ""))
    cfg["home_assistant"]["mode_scenes"] = {
        "movie": os.environ.get("WIZARD_HA_SCENE_MOVIE", "").strip(),
        "pause": os.environ.get("WIZARD_HA_SCENE_PAUSE", "").strip(),
        "normal": os.environ.get("WIZARD_HA_SCENE_NORMAL", "").strip(),
    }
else:
    cfg["home_assistant"].update(
        {
            "url": "",
            "token": "",
            "verify_ssl": True,
            "transition_seconds": 1,
            "entity_ids": [],
            "mode_scenes": {"movie": "", "pause": "", "normal": ""},
        }
    )

config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {config_path}")
PY

    echo -e "${GREEN}Wizard complete.${NC}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --setup)
            SETUP_MODE=true
            shift
            ;;
        --wizard)
            WIZARD_MODE=true
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

if [[ "$WIZARD_MODE" == true ]]; then
    run_wizard
    if [[ "$SETUP_MODE" != true ]]; then
        echo -e "${GREEN}Next step:${NC} run ${YELLOW}bash install.sh --setup${NC}"
        exit 0
    fi
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
        echo -e "${YELLOW}Edit config.json or run: bash install.sh --wizard${NC}"
        echo -e "${YELLOW}Then rerun: bash install.sh --setup${NC}"
        exit 0
    fi

    echo -e "${YELLOW}No config.json found. Copy config.json.example to config.json, or run bash install.sh --wizard${NC}"
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
echo "  bash install.sh --wizard                             # rebuild config.json"
echo "  bash install.sh --setup                              # refresh venv + deps"
echo "  bash install.sh --uninstall                          # remove"
