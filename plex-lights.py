#!/usr/bin/env python3
"""
Plex Lights — dims your smart lights when a movie starts playing.

Runs as a webhook server that receives play/pause/resume/stop events from
Tautulli and adjusts Philips Hue and/or Govee lights automatically.

Play/resume → dim to candlelight
Pause       → brighten slightly
Stop        → back to normal

Configuration via config.json or environment variables.
"""

import json
import logging
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

# --- Config ---

DEFAULT_CONFIG = {
    "port": 32500,
    "log_dir": "",
    "tv_player_name": "",
    "hue": {
        "enabled": False,
        "bridge_ip": "",
        "api_user": "",
        "lights": [],
    },
    "govee": {
        "enabled": False,
        "api_key": "",
        "device": "",
        "model": "",
    },
    "modes": {
        "movie": {
            "hue_brightness": 13,
            "hue_color_temp": 500,
            "govee_brightness": 5,
            "govee_color": {"r": 255, "g": 120, "b": 20},
        },
        "pause": {
            "hue_brightness": 77,
            "hue_color_temp": 400,
            "govee_brightness": 25,
            "govee_color": {"r": 255, "g": 160, "b": 60},
        },
        "normal": {
            "hue_brightness": 254,
            "hue_color_temp": 366,
            "govee_brightness": 100,
            "govee_color": {"r": 255, "g": 200, "b": 120},
        },
    },
}


def load_config():
    """Load config from config.json, falling back to env vars."""
    config = dict(DEFAULT_CONFIG)
    config_path = Path(__file__).parent / "config.json"

    if config_path.exists():
        with open(config_path) as f:
            user_config = json.load(f)
        config = deep_merge(config, user_config)
    else:
        # Fall back to env vars for simple setups
        config["port"] = int(os.environ.get("PLEX_LIGHTS_PORT", 32500))
        config["tv_player_name"] = os.environ.get("TV_PLAYER_NAME", "")
        config["log_dir"] = os.environ.get("PLEX_LIGHTS_LOG_DIR", "")

        if os.environ.get("HUE_BRIDGE_IP"):
            config["hue"]["enabled"] = True
            config["hue"]["bridge_ip"] = os.environ["HUE_BRIDGE_IP"]
            config["hue"]["api_user"] = os.environ.get("HUE_API_USER", "")
            lights_str = os.environ.get("HUE_LIGHTS", "")
            if lights_str:
                config["hue"]["lights"] = [int(x.strip()) for x in lights_str.split(",")]

        if os.environ.get("GOVEE_API_KEY"):
            config["govee"]["enabled"] = True
            config["govee"]["api_key"] = os.environ["GOVEE_API_KEY"]
            config["govee"]["device"] = os.environ.get("GOVEE_DEVICE", "")
            config["govee"]["model"] = os.environ.get("GOVEE_MODEL", "")

    return config


def deep_merge(base, override):
    """Recursively merge override into base dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# --- Logging ---

def setup_logging(config):
    log_dir = config.get("log_dir", "")
    handlers = [logging.StreamHandler()]
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(f"{log_dir}/plex-lights.log"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("plex-lights")


# --- Light Control ---

def set_hue_lights(config, bri, ct, log):
    """Set all configured Hue lights to given brightness and color temp."""
    hue = config["hue"]
    if not hue["enabled"]:
        return

    for light_id in hue["lights"]:
        url = f"http://{hue['bridge_ip']}/api/{hue['api_user']}/lights/{light_id}/state"
        payload = {"on": True, "bri": bri, "ct": ct}
        try:
            r = requests.put(url, json=payload, timeout=5)
            if r.ok:
                log.info(f"Hue light {light_id}: bri={bri}, ct={ct}")
            else:
                log.error(f"Hue light {light_id} failed: {r.text}")
        except Exception as e:
            log.error(f"Hue light {light_id} error: {e}")


def set_govee_light(config, brightness, color, log):
    """Set Govee light brightness and color via Cloud API v1."""
    govee = config["govee"]
    if not govee["enabled"]:
        return

    headers = {"Govee-API-Key": govee["api_key"], "Content-Type": "application/json"}
    url = "https://openapi.api.govee.com/router/api/v1/device/control"

    color_value = ((color["r"] & 0xFF) << 16) | ((color["g"] & 0xFF) << 8) | (color["b"] & 0xFF)

    # Set color
    try:
        r = requests.post(url, headers=headers, json={
            "requestId": str(int(time.time())),
            "payload": {
                "sku": govee["model"],
                "device": govee["device"],
                "capability": {
                    "type": "devices.capabilities.color_setting",
                    "instance": "colorRgb",
                    "value": color_value,
                },
            },
        }, timeout=10)
        if r.ok:
            log.info(f"Govee color: {color}")
        else:
            log.error(f"Govee color failed: {r.text}")
    except Exception as e:
        log.error(f"Govee color error: {e}")

    # Set brightness
    try:
        r = requests.post(url, headers=headers, json={
            "requestId": str(int(time.time())),
            "payload": {
                "sku": govee["model"],
                "device": govee["device"],
                "capability": {
                    "type": "devices.capabilities.range",
                    "instance": "brightness",
                    "value": brightness,
                },
            },
        }, timeout=10)
        if r.ok:
            log.info(f"Govee brightness: {brightness}")
        else:
            log.error(f"Govee brightness failed: {r.text}")
    except Exception as e:
        log.error(f"Govee brightness error: {e}")


def apply_mode(config, mode_name, log):
    """Apply a light mode to all configured lights."""
    mode = config["modes"][mode_name]
    log.info(f"Applying mode: {mode_name}")
    set_hue_lights(config, mode["hue_brightness"], mode["hue_color_temp"], log)
    set_govee_light(config, mode["govee_brightness"], mode["govee_color"], log)


# --- Webhook Handler ---

def make_handler(config, log):
    class WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                try:
                    from urllib.parse import parse_qs
                    parsed = parse_qs(body.decode())
                    if "body" in parsed:
                        data = json.loads(parsed["body"][0])
                    else:
                        data = {k: v[0] for k, v in parsed.items()}
                except Exception:
                    log.warning(f"Could not parse body: {body[:200]}")
                    self.send_response(400)
                    self.end_headers()
                    return

            event = data.get("event", "")
            player = data.get("player", "")
            title = data.get("title", "unknown")
            media_type = data.get("media_type", "")

            log.info(f"Event: {event} | Player: {player} | Title: {title} | Type: {media_type}")

            tv_player = config.get("tv_player_name", "")
            if tv_player and player != tv_player:
                log.info(f"Ignoring event from player '{player}' (not '{tv_player}')")
                self.send_response(200)
                self.end_headers()
                return

            if media_type not in ("movie", "episode", ""):
                log.info(f"Ignoring media type: {media_type}")
                self.send_response(200)
                self.end_headers()
                return

            if event in ("play", "resume"):
                apply_mode(config, "movie", log)
            elif event == "pause":
                apply_mode(config, "pause", log)
            elif event == "stop":
                apply_mode(config, "normal", log)
            else:
                log.info(f"Unhandled event: {event}")

            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    return WebhookHandler


# --- Main ---

def main():
    config = load_config()
    log = setup_logging(config)

    hue_enabled = config["hue"]["enabled"]
    govee_enabled = config["govee"]["enabled"]

    if not hue_enabled and not govee_enabled:
        log.error("No lights configured. Set up Hue and/or Govee in config.json or env vars.")
        log.error("See README.md for setup instructions.")
        sys.exit(1)

    port = config["port"]
    log.info(f"Plex Lights starting on port {port}")

    if hue_enabled:
        log.info(f"Hue: bridge={config['hue']['bridge_ip']}, lights={config['hue']['lights']}")
    if govee_enabled:
        log.info(f"Govee: device={config['govee']['device']}, model={config['govee']['model']}")

    tv_player = config.get("tv_player_name", "")
    if tv_player:
        log.info(f"Filtering for TV player: {tv_player}")
    else:
        log.info("No player filter. Will trigger on ALL players.")
        log.info("Set tv_player_name in config.json after discovering your player name.")

    handler = make_handler(config, log)
    server = HTTPServer(("0.0.0.0", port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
