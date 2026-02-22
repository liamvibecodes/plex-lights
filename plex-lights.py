#!/usr/bin/env python3
"""
Plex Lights — dims your smart lights when a movie starts playing.

Runs as a webhook server that receives play/pause/resume/stop events from
Tautulli and adjusts Philips Hue and/or Govee lights automatically.

Play/resume -> dim to candlelight
Pause       -> brighten slightly
Stop/end    -> back to normal

Configuration via config.json or environment variables.
"""

import copy
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

REQUEST_TIMEOUT_SECONDS = 10
SUPPORTED_MEDIA_TYPES = {"movie", "episode", ""}
EVENT_TO_MODE = {
    "play": "movie",
    "resume": "movie",
    "pause": "pause",
    "stop": "normal",
}

# --- Config ---

DEFAULT_CONFIG = {
    "port": 32500,
    "log_dir": "",
    "tv_player_name": "",
    "webhook_token": "",
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


def deep_merge(base, override):
    """Recursively merge override into base dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def parse_hue_lights(lights_str):
    """Parse comma-separated light IDs from env var."""
    if not lights_str.strip():
        return []

    parsed = []
    for raw in lights_str.split(","):
        raw = raw.strip()
        if not raw:
            continue
        parsed.append(int(raw))
    return parsed


def as_bool(value):
    """Parse booleans from native bools or common string representations."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def validate_int_range(config, errors, field_name, min_value, max_value):
    """Validate integer range, storing normalized int back in config."""
    value = config.get(field_name)
    try:
        value = int(value)
    except (TypeError, ValueError):
        errors.append(f"{field_name} must be an integer")
        return

    if value < min_value or value > max_value:
        errors.append(f"{field_name} must be between {min_value} and {max_value}")
        return

    config[field_name] = value


def validate_modes(config, errors):
    """Validate required mode keys and numeric ranges."""
    modes = config.get("modes")
    if not isinstance(modes, dict):
        errors.append("modes must be an object")
        return

    for mode_name in ("movie", "pause", "normal"):
        mode = modes.get(mode_name)
        if not isinstance(mode, dict):
            errors.append(f"modes.{mode_name} is missing or invalid")
            continue

        validate_int_range(mode, errors, "hue_brightness", 1, 254)
        validate_int_range(mode, errors, "hue_color_temp", 153, 500)
        validate_int_range(mode, errors, "govee_brightness", 0, 100)

        color = mode.get("govee_color")
        if not isinstance(color, dict):
            errors.append(f"modes.{mode_name}.govee_color must be an object")
            continue

        for channel in ("r", "g", "b"):
            try:
                color_value = int(color.get(channel))
            except (TypeError, ValueError):
                errors.append(f"modes.{mode_name}.govee_color.{channel} must be an integer")
                continue

            if color_value < 0 or color_value > 255:
                errors.append(f"modes.{mode_name}.govee_color.{channel} must be between 0 and 255")
                continue

            color[channel] = color_value


def validate_config(config):
    """Validate and normalize config values."""
    errors = []

    validate_int_range(config, errors, "port", 1, 65535)

    config["tv_player_name"] = str(config.get("tv_player_name", "")).strip()
    config["webhook_token"] = str(config.get("webhook_token", "")).strip()

    hue = config.get("hue")
    if not isinstance(hue, dict):
        errors.append("hue must be an object")
        hue = {}
        config["hue"] = hue
    hue["enabled"] = as_bool(hue.get("enabled"))
    if hue["enabled"]:
        if not str(hue.get("bridge_ip", "")).strip():
            errors.append("hue.bridge_ip is required when hue.enabled=true")
        if not str(hue.get("api_user", "")).strip():
            errors.append("hue.api_user is required when hue.enabled=true")

        lights = hue.get("lights")
        if not isinstance(lights, list) or not lights:
            errors.append("hue.lights must be a non-empty list when hue.enabled=true")
        else:
            normalized = []
            for idx, light_id in enumerate(lights):
                try:
                    parsed = int(light_id)
                except (TypeError, ValueError):
                    errors.append(f"hue.lights[{idx}] must be an integer")
                    continue
                if parsed <= 0:
                    errors.append(f"hue.lights[{idx}] must be > 0")
                    continue
                normalized.append(parsed)
            hue["lights"] = normalized

    govee = config.get("govee")
    if not isinstance(govee, dict):
        errors.append("govee must be an object")
        govee = {}
        config["govee"] = govee
    govee["enabled"] = as_bool(govee.get("enabled"))
    if govee["enabled"]:
        if not str(govee.get("api_key", "")).strip():
            errors.append("govee.api_key is required when govee.enabled=true")
        if not str(govee.get("device", "")).strip():
            errors.append("govee.device is required when govee.enabled=true")
        if not str(govee.get("model", "")).strip():
            errors.append("govee.model is required when govee.enabled=true")

    validate_modes(config, errors)

    if not hue["enabled"] and not govee["enabled"]:
        errors.append("Enable at least one provider (hue.enabled or govee.enabled)")

    if errors:
        raise ValueError("Invalid config:\n- " + "\n- ".join(errors))

    return config


def load_config():
    """Load config from config.json, falling back to env vars."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config_path = Path(__file__).parent / "config.json"

    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                user_config = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"config.json is invalid JSON: {exc}") from exc

        if not isinstance(user_config, dict):
            raise ValueError("config.json must contain a JSON object at the top level")

        config = deep_merge(config, user_config)
    else:
        config["port"] = os.environ.get("PLEX_LIGHTS_PORT", 32500)
        config["tv_player_name"] = os.environ.get("TV_PLAYER_NAME", "")
        config["log_dir"] = os.environ.get("PLEX_LIGHTS_LOG_DIR", "")
        config["webhook_token"] = os.environ.get("PLEX_LIGHTS_WEBHOOK_TOKEN", "")

        if os.environ.get("HUE_BRIDGE_IP"):
            config["hue"]["enabled"] = True
            config["hue"]["bridge_ip"] = os.environ["HUE_BRIDGE_IP"]
            config["hue"]["api_user"] = os.environ.get("HUE_API_USER", "")
            config["hue"]["lights"] = parse_hue_lights(os.environ.get("HUE_LIGHTS", ""))

        if os.environ.get("GOVEE_API_KEY"):
            config["govee"]["enabled"] = True
            config["govee"]["api_key"] = os.environ["GOVEE_API_KEY"]
            config["govee"]["device"] = os.environ.get("GOVEE_DEVICE", "")
            config["govee"]["model"] = os.environ.get("GOVEE_MODEL", "")

    return validate_config(config)


# --- Logging ---


def setup_logging(config):
    log_dir = config.get("log_dir", "")
    handlers = [logging.StreamHandler()]
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(f"{log_dir}/plex-lights.log", encoding="utf-8"))

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

    if not hue["lights"]:
        log.warning("Hue enabled but no lights configured")
        return

    for light_id in hue["lights"]:
        url = f"http://{hue['bridge_ip']}/api/{hue['api_user']}/lights/{light_id}/state"
        payload = {"on": True, "bri": bri, "ct": ct}
        try:
            response = requests.put(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            log.error("Hue light %s request failed: %s", light_id, exc)
            continue

        if not response.ok:
            log.error("Hue light %s failed: HTTP %s %s", light_id, response.status_code, response.text)
            continue

        response_text = response.text.lower()
        if "error" in response_text:
            log.error("Hue light %s API error: %s", light_id, response.text)
            continue

        log.info("Hue light %s updated (bri=%s, ct=%s)", light_id, bri, ct)


def govee_control_request(config, capability_payload, log):
    """Send one Govee control request and validate the response."""
    govee = config["govee"]
    headers = {
        "Govee-API-Key": govee["api_key"],
        "Content-Type": "application/json",
    }
    request_body = {
        "requestId": str(int(time.time() * 1000)),
        "payload": {
            "sku": govee["model"],
            "device": govee["device"],
            "capability": capability_payload,
        },
    }

    try:
        response = requests.post(
            "https://openapi.api.govee.com/router/api/v1/device/control",
            headers=headers,
            json=request_body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.error("Govee request failed: %s", exc)
        return False

    if not response.ok:
        log.error("Govee request failed: HTTP %s %s", response.status_code, response.text)
        return False

    try:
        parsed = response.json()
    except ValueError:
        parsed = {}

    if isinstance(parsed, dict) and parsed.get("code") not in (None, 0, 200):
        log.error("Govee API error response: %s", response.text)
        return False

    return True


def set_govee_light(config, brightness, color, log):
    """Set Govee light brightness and color via Cloud API v1."""
    govee = config["govee"]
    if not govee["enabled"]:
        return

    color_value = ((color["r"] & 0xFF) << 16) | ((color["g"] & 0xFF) << 8) | (color["b"] & 0xFF)

    if govee_control_request(
        config,
        {
            "type": "devices.capabilities.color_setting",
            "instance": "colorRgb",
            "value": color_value,
        },
        log,
    ):
        log.info("Govee color updated to rgb(%s, %s, %s)", color["r"], color["g"], color["b"])

    if govee_control_request(
        config,
        {
            "type": "devices.capabilities.range",
            "instance": "brightness",
            "value": brightness,
        },
        log,
    ):
        log.info("Govee brightness updated to %s", brightness)


def apply_mode(config, mode_name, log):
    """Apply a light mode to all configured lights."""
    mode = config["modes"].get(mode_name)
    if mode is None:
        log.error("Unknown mode '%s'", mode_name)
        return

    log.info("Applying mode: %s", mode_name)
    set_hue_lights(config, mode["hue_brightness"], mode["hue_color_temp"], log)
    set_govee_light(config, mode["govee_brightness"], mode["govee_color"], log)


# --- Webhook Handler ---


def normalize_event(event):
    """Normalize multiple event naming styles to play/pause/resume/stop."""
    event = event.strip().lower()
    if event in ("play", "playback start", "playback.start", "played"):
        return "play"
    if event in ("resume", "playback resume", "playback.resume", "resumed"):
        return "resume"
    if event in ("pause", "playback pause", "playback.pause", "paused"):
        return "pause"
    if event in (
        "stop",
        "ended",
        "playback stop",
        "playback.stop",
        "playback ended",
        "stopped",
    ):
        return "stop"
    return event


def extract_player_name(player_value):
    """Extract player name from either a string or nested object."""
    if isinstance(player_value, dict):
        return str(player_value.get("title") or player_value.get("name") or "").strip()
    return str(player_value or "").strip()


def parse_payload(body):
    """Parse JSON or form-encoded webhook payload."""
    text_body = body.decode("utf-8", errors="replace")

    try:
        parsed = json.loads(text_body)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    form_data = parse_qs(text_body)
    if "body" in form_data and form_data["body"]:
        try:
            nested = json.loads(form_data["body"][0])
            if isinstance(nested, dict):
                return nested
        except json.JSONDecodeError:
            return {}

    if form_data:
        return {key: values[0] for key, values in form_data.items() if values}

    return {}


def make_handler(config, log):
    class WebhookHandler(BaseHTTPRequestHandler):
        def respond(self, status_code, payload):
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                self.respond(200, {"status": "ok"})
                return
            self.respond(404, {"error": "not found"})

        def do_POST(self):
            token = config.get("webhook_token", "")
            if token:
                header_token = self.headers.get("X-Plex-Lights-Token", "").strip()
                query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0].strip()
                if header_token != token and query_token != token:
                    log.warning("Rejected webhook with invalid token")
                    self.respond(403, {"error": "invalid token"})
                    return

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = parse_payload(body)

            if not data:
                log.warning("Could not parse webhook body: %r", body[:200])
                self.respond(400, {"error": "invalid payload"})
                return

            event = normalize_event(str(data.get("event", "")))
            player = extract_player_name(data.get("player", ""))
            if not player:
                player = str(data.get("player_title", "")).strip()

            title = str(data.get("title") or data.get("full_title") or "unknown").strip()
            media_type = str(data.get("media_type") or data.get("mediaType") or "").strip().lower()

            log.info(
                "Event: %s | Player: %s | Title: %s | Type: %s",
                event,
                player,
                title,
                media_type,
            )

            tv_player = config.get("tv_player_name", "")
            if tv_player and player != tv_player:
                log.info("Ignoring event from player '%s' (not '%s')", player, tv_player)
                self.respond(200, {"status": "ignored_player"})
                return

            if media_type not in SUPPORTED_MEDIA_TYPES:
                log.info("Ignoring media type: %s", media_type)
                self.respond(200, {"status": "ignored_media_type"})
                return

            mode_name = EVENT_TO_MODE.get(event)
            if mode_name:
                apply_mode(config, mode_name, log)
            else:
                log.info("Unhandled event: %s", event)

            self.respond(200, {"status": "ok"})

        def log_message(self, format, *args):
            pass

    return WebhookHandler


# --- Main ---


def main():
    try:
        config = load_config()
    except ValueError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    log = setup_logging(config)

    hue_enabled = config["hue"]["enabled"]
    govee_enabled = config["govee"]["enabled"]

    port = config["port"]
    log.info("Plex Lights starting on port %s", port)

    if hue_enabled:
        log.info("Hue: bridge=%s, lights=%s", config["hue"]["bridge_ip"], config["hue"]["lights"])
    if govee_enabled:
        log.info("Govee: device=%s, model=%s", config["govee"]["device"], config["govee"]["model"])

    if config["webhook_token"]:
        log.info("Webhook token auth is enabled")

    tv_player = config.get("tv_player_name", "")
    if tv_player:
        log.info("Filtering for TV player: %s", tv_player)
    else:
        log.info("No player filter. Will trigger on ALL players.")
        log.info("Set tv_player_name in config.json after discovering your player name.")

    handler = make_handler(config, log)
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    except OSError as exc:
        log.error("Failed to bind to port %s: %s", port, exc)
        sys.exit(1)

    server.daemon_threads = True

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
