#!/usr/bin/env python3
"""
Plex Lights — dims your smart lights when a movie starts playing.

Runs as a webhook server that receives play/pause/resume/stop events from
Tautulli and adjusts Philips Hue, Govee, and/or Home Assistant lights
automatically.

Play/resume -> dim to candlelight
Pause       -> brighten slightly
Stop/end    -> restore pre-playback light state (fallback to configured mode)

Configuration via config.json or environment variables.
"""

import argparse
import copy
import json
import logging
import os
import sys
import threading
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
    "dry_run": False,
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
    "home_assistant": {
        "enabled": False,
        "url": "",
        "token": "",
        "verify_ssl": True,
        "transition_seconds": 1,
        "entity_ids": [],
        "mode_scenes": {
            "movie": "",
            "pause": "",
            "normal": "",
        },
    },
    "state_restore": {
        "enabled": True,
        "fallback_mode": "normal",
        "home_assistant_scene_id": "plex_lights_preplay",
        "capture_govee_state": True,
    },
    "modes": {
        "movie": {
            "hue_brightness": 13,
            "hue_color_temp": 500,
            "govee_brightness": 5,
            "govee_color": {"r": 255, "g": 120, "b": 20},
            "ha_brightness_pct": 5,
            "ha_color_temp_kelvin": 2200,
            "ha_rgb_color": [255, 120, 20],
        },
        "pause": {
            "hue_brightness": 77,
            "hue_color_temp": 400,
            "govee_brightness": 25,
            "govee_color": {"r": 255, "g": 160, "b": 60},
            "ha_brightness_pct": 25,
            "ha_color_temp_kelvin": 2600,
            "ha_rgb_color": [255, 160, 60],
        },
        "normal": {
            "hue_brightness": 254,
            "hue_color_temp": 366,
            "govee_brightness": 100,
            "govee_color": {"r": 255, "g": 200, "b": 120},
            "ha_brightness_pct": 100,
            "ha_color_temp_kelvin": 3000,
            "ha_rgb_color": [],
        },
    },
}

RUNTIME_LOCK = threading.Lock()
RUNTIME_STATE = {
    "playback_active": False,
    "snapshot": None,
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


def parse_string_list(values_str):
    """Parse comma-separated values into a trimmed list."""
    if not values_str.strip():
        return []
    return [value.strip() for value in values_str.split(",") if value.strip()]


def parse_mode_scenes(mapping_str):
    """Parse mode scene mappings from env var, e.g. movie:scene.movie_mode,pause:scene.pause_mode."""
    mode_scenes = {}
    if not mapping_str.strip():
        return mode_scenes

    for pair in mapping_str.split(","):
        if ":" not in pair:
            continue
        mode_name, scene_entity = pair.split(":", 1)
        mode_name = mode_name.strip().lower()
        scene_entity = scene_entity.strip()
        if mode_name in ("movie", "pause", "normal") and scene_entity:
            mode_scenes[mode_name] = scene_entity
    return mode_scenes


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


def validate_rgb_list(mode, errors, mode_name, field_name):
    """Validate RGB list. Empty list is treated as disabled."""
    rgb = mode.get(field_name)
    if rgb == []:
        return

    if not isinstance(rgb, list) or len(rgb) != 3:
        errors.append(f"modes.{mode_name}.{field_name} must be a list of 3 integers or []")
        return

    normalized = []
    for idx, value in enumerate(rgb):
        try:
            value = int(value)
        except (TypeError, ValueError):
            errors.append(f"modes.{mode_name}.{field_name}[{idx}] must be an integer")
            continue
        if value < 0 or value > 255:
            errors.append(f"modes.{mode_name}.{field_name}[{idx}] must be between 0 and 255")
            continue
        normalized.append(value)

    if len(normalized) == 3:
        mode[field_name] = normalized


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
        validate_int_range(mode, errors, "ha_brightness_pct", 0, 100)
        validate_int_range(mode, errors, "ha_color_temp_kelvin", 1500, 9000)
        validate_rgb_list(mode, errors, mode_name, "ha_rgb_color")

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
    config["dry_run"] = as_bool(config.get("dry_run", False))

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

    home_assistant = config.get("home_assistant")
    if not isinstance(home_assistant, dict):
        errors.append("home_assistant must be an object")
        home_assistant = {}
        config["home_assistant"] = home_assistant
    home_assistant["enabled"] = as_bool(home_assistant.get("enabled"))
    home_assistant["verify_ssl"] = as_bool(home_assistant.get("verify_ssl", True))
    validate_int_range(home_assistant, errors, "transition_seconds", 0, 30)

    mode_scenes = home_assistant.get("mode_scenes")
    if not isinstance(mode_scenes, dict):
        errors.append("home_assistant.mode_scenes must be an object")
        mode_scenes = {}
        home_assistant["mode_scenes"] = mode_scenes
    for mode_name in ("movie", "pause", "normal"):
        mode_scenes[mode_name] = str(mode_scenes.get(mode_name, "")).strip()

    entity_ids = home_assistant.get("entity_ids")
    if not isinstance(entity_ids, list):
        errors.append("home_assistant.entity_ids must be a list")
        entity_ids = []
        home_assistant["entity_ids"] = entity_ids
    else:
        home_assistant["entity_ids"] = [str(entity).strip() for entity in entity_ids if str(entity).strip()]

    if home_assistant["enabled"]:
        home_assistant["url"] = str(home_assistant.get("url", "")).strip().rstrip("/")
        home_assistant["token"] = str(home_assistant.get("token", "")).strip()

        if not home_assistant["url"]:
            errors.append("home_assistant.url is required when home_assistant.enabled=true")
        if not home_assistant["token"]:
            errors.append("home_assistant.token is required when home_assistant.enabled=true")

        has_scene = any(home_assistant["mode_scenes"].values())
        has_entities = bool(home_assistant["entity_ids"])
        if not has_scene and not has_entities:
            errors.append(
                "home_assistant requires entity_ids and/or mode_scenes when home_assistant.enabled=true"
            )

    state_restore = config.get("state_restore")
    if not isinstance(state_restore, dict):
        errors.append("state_restore must be an object")
        state_restore = {}
        config["state_restore"] = state_restore

    state_restore["enabled"] = as_bool(state_restore.get("enabled", True))
    state_restore["capture_govee_state"] = as_bool(state_restore.get("capture_govee_state", True))
    state_restore["home_assistant_scene_id"] = str(
        state_restore.get("home_assistant_scene_id", "plex_lights_preplay")
    ).strip()
    if state_restore["home_assistant_scene_id"].startswith("scene."):
        state_restore["home_assistant_scene_id"] = state_restore["home_assistant_scene_id"][6:]
    if not state_restore["home_assistant_scene_id"]:
        state_restore["home_assistant_scene_id"] = "plex_lights_preplay"

    fallback_mode = str(state_restore.get("fallback_mode", "normal")).strip().lower()
    if not fallback_mode:
        fallback_mode = "normal"
    state_restore["fallback_mode"] = fallback_mode

    validate_modes(config, errors)

    if state_restore["fallback_mode"] not in config["modes"]:
        errors.append(
            f"state_restore.fallback_mode must be one of: {', '.join(sorted(config['modes'].keys()))}"
        )

    if not hue["enabled"] and not govee["enabled"] and not home_assistant["enabled"]:
        errors.append("Enable at least one provider (hue.enabled, govee.enabled, or home_assistant.enabled)")

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
        config["dry_run"] = os.environ.get("PLEX_LIGHTS_DRY_RUN", "false")
        config["state_restore"]["enabled"] = os.environ.get("PLEX_LIGHTS_RESTORE_STATE_ENABLED", "true")
        config["state_restore"]["fallback_mode"] = os.environ.get(
            "PLEX_LIGHTS_RESTORE_FALLBACK_MODE",
            "normal",
        )
        config["state_restore"]["home_assistant_scene_id"] = os.environ.get(
            "PLEX_LIGHTS_HA_SCENE_ID",
            "plex_lights_preplay",
        )
        config["state_restore"]["capture_govee_state"] = os.environ.get(
            "PLEX_LIGHTS_CAPTURE_GOVEE_STATE",
            "true",
        )

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

        if os.environ.get("HOME_ASSISTANT_URL") or os.environ.get("HOME_ASSISTANT_TOKEN"):
            config["home_assistant"]["enabled"] = True
            config["home_assistant"]["url"] = os.environ.get("HOME_ASSISTANT_URL", "")
            config["home_assistant"]["token"] = os.environ.get("HOME_ASSISTANT_TOKEN", "")
            config["home_assistant"]["verify_ssl"] = os.environ.get("HOME_ASSISTANT_VERIFY_SSL", "true")
            config["home_assistant"]["entity_ids"] = parse_string_list(
                os.environ.get("HOME_ASSISTANT_ENTITY_IDS", "")
            )
            config["home_assistant"]["mode_scenes"] = deep_merge(
                config["home_assistant"]["mode_scenes"],
                parse_mode_scenes(os.environ.get("HOME_ASSISTANT_MODE_SCENES", "")),
            )

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


def normalize_scene_entity_id(scene_id):
    """Normalize scene IDs to both raw ID and entity_id format."""
    normalized = str(scene_id or "").strip()
    if not normalized:
        return "", ""
    if normalized.startswith("scene."):
        return normalized[6:], normalized
    return normalized, f"scene.{normalized}"


def parse_bool_like(value):
    """Parse booleans from mixed payloads."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        return lowered in {"on", "true", "1", "yes"}
    return bool(value)


def parse_govee_rgb(value):
    """Normalize Govee RGB state value to dict form."""
    if isinstance(value, int):
        return {
            "r": (value >> 16) & 0xFF,
            "g": (value >> 8) & 0xFF,
            "b": value & 0xFF,
        }
    if isinstance(value, dict):
        try:
            return {
                "r": int(value.get("r", 0)),
                "g": int(value.get("g", 0)),
                "b": int(value.get("b", 0)),
            }
        except (TypeError, ValueError):
            return None
    return None


def iter_capabilities(payload):
    """Recursively find capability arrays in mixed API payloads."""
    if isinstance(payload, dict):
        capabilities = payload.get("capabilities")
        if isinstance(capabilities, list):
            yield capabilities
        for value in payload.values():
            yield from iter_capabilities(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from iter_capabilities(item)


def get_hue_light_state(config, light_id, log):
    """Fetch one Hue light state for snapshot/restore."""
    hue = config["hue"]
    url = f"http://{hue['bridge_ip']}/api/{hue['api_user']}/lights/{light_id}"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        log.error("Hue snapshot read failed for light %s: %s", light_id, exc)
        return None

    if not response.ok:
        log.error(
            "Hue snapshot read failed for light %s: HTTP %s %s",
            light_id,
            response.status_code,
            response.text,
        )
        return None

    try:
        data = response.json()
    except ValueError:
        log.error("Hue snapshot read returned invalid JSON for light %s", light_id)
        return None

    state = data.get("state")
    if not isinstance(state, dict):
        log.error("Hue snapshot read returned invalid state for light %s", light_id)
        return None

    snapshot = {
        "on": bool(state.get("on", True)),
        "bri": state.get("bri"),
        "ct": state.get("ct"),
        "hue": state.get("hue"),
        "sat": state.get("sat"),
        "xy": state.get("xy"),
        "colormode": state.get("colormode"),
    }
    return snapshot


def capture_hue_snapshot(config, log):
    """Capture current Hue state for configured lights."""
    hue = config["hue"]
    if not hue["enabled"] or not hue["lights"]:
        return None

    if config.get("dry_run", False):
        log.info("[DRY RUN] Hue snapshot capture skipped")
        return {}

    snapshot = {}
    for light_id in hue["lights"]:
        state = get_hue_light_state(config, light_id, log)
        if state is not None:
            snapshot[str(light_id)] = state

    if not snapshot:
        log.warning("Hue snapshot capture found no restorable light states")
        return None
    return snapshot


def restore_hue_snapshot(config, snapshot, log):
    """Restore previously captured Hue state."""
    hue = config["hue"]
    if not hue["enabled"] or not isinstance(snapshot, dict):
        return False

    if config.get("dry_run", False):
        log.info("[DRY RUN] Hue snapshot restore: %s lights", len(snapshot))
        return True

    restored_any = False
    for light_id, state in snapshot.items():
        if not isinstance(state, dict):
            continue

        payload = {"on": bool(state.get("on", True))}
        if payload["on"]:
            bri = state.get("bri")
            if isinstance(bri, int):
                payload["bri"] = max(1, min(254, bri))

            colormode = str(state.get("colormode", "")).lower()
            if colormode == "ct" and isinstance(state.get("ct"), int):
                payload["ct"] = max(153, min(500, state["ct"]))
            elif colormode == "hs" and isinstance(state.get("hue"), int) and isinstance(state.get("sat"), int):
                payload["hue"] = max(0, min(65535, state["hue"]))
                payload["sat"] = max(0, min(254, state["sat"]))
            elif colormode == "xy" and isinstance(state.get("xy"), list) and len(state["xy"]) == 2:
                payload["xy"] = state["xy"]
            elif isinstance(state.get("ct"), int):
                payload["ct"] = max(153, min(500, state["ct"]))

        url = f"http://{hue['bridge_ip']}/api/{hue['api_user']}/lights/{light_id}/state"
        try:
            response = requests.put(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            log.error("Hue restore failed for light %s: %s", light_id, exc)
            continue

        if not response.ok:
            log.error("Hue restore failed for light %s: HTTP %s %s", light_id, response.status_code, response.text)
            continue

        if "error" in response.text.lower():
            log.error("Hue restore API error for light %s: %s", light_id, response.text)
            continue

        restored_any = True

    return restored_any


def govee_state_request(config, log):
    """Fetch current Govee device state from API."""
    govee = config["govee"]
    headers = {
        "Govee-API-Key": govee["api_key"],
        "Content-Type": "application/json",
    }
    params = {
        "sku": govee["model"],
        "device": govee["device"],
    }

    try:
        response = requests.get(
            "https://openapi.api.govee.com/router/api/v1/device/state",
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.error("Govee state request failed: %s", exc)
        return None

    if not response.ok:
        log.error("Govee state request failed: HTTP %s %s", response.status_code, response.text)
        return None

    try:
        parsed = response.json()
    except ValueError:
        log.error("Govee state request returned invalid JSON")
        return None

    if isinstance(parsed, dict) and parsed.get("code") not in (None, 0, 200):
        log.error("Govee state API error response: %s", response.text)
        return None

    return parsed


def capture_govee_snapshot(config, log):
    """Capture Govee power/brightness/color state."""
    govee = config["govee"]
    if not govee["enabled"]:
        return None
    if not config["state_restore"]["capture_govee_state"]:
        return None

    if config.get("dry_run", False):
        log.info("[DRY RUN] Govee snapshot capture skipped")
        return {}

    payload = govee_state_request(config, log)
    if payload is None:
        return None

    snapshot = {}
    for capabilities in iter_capabilities(payload):
        for capability in capabilities:
            if not isinstance(capability, dict):
                continue
            instance = str(capability.get("instance", ""))
            state = capability.get("state", {})
            if isinstance(state, dict):
                value = state.get("value")
            else:
                value = capability.get("value")

            if instance == "powerSwitch":
                snapshot["on"] = parse_bool_like(value)
            elif instance == "brightness":
                try:
                    snapshot["brightness"] = int(value)
                except (TypeError, ValueError):
                    pass
            elif instance in {"colorRgb", "color"}:
                rgb = parse_govee_rgb(value)
                if rgb:
                    snapshot["color"] = rgb

    if not snapshot:
        log.warning("Govee snapshot capture found no restorable state values")
        return None
    return snapshot


def govee_set_power(config, on, log):
    """Set Govee power switch state."""
    value = "on" if on else "off"
    return govee_control_request(
        config,
        {
            "type": "devices.capabilities.on_off",
            "instance": "powerSwitch",
            "value": value,
        },
        log,
    )


def restore_govee_snapshot(config, snapshot, log):
    """Restore captured Govee state."""
    govee = config["govee"]
    if not govee["enabled"] or not isinstance(snapshot, dict):
        return False

    if config.get("dry_run", False):
        log.info("[DRY RUN] Govee snapshot restore: %s", snapshot)
        return True

    restored_any = False
    power_state = snapshot.get("on")
    if power_state is not None:
        if govee_set_power(config, bool(power_state), log):
            restored_any = True
            log.info("Govee power restored to %s", "on" if power_state else "off")
        if not power_state:
            return restored_any

    brightness = snapshot.get("brightness")
    color = snapshot.get("color")
    if isinstance(brightness, int) and isinstance(color, dict):
        set_govee_light(config, max(0, min(100, brightness)), color, log)
        return True

    if isinstance(brightness, int):
        if govee_control_request(
            config,
            {
                "type": "devices.capabilities.range",
                "instance": "brightness",
                "value": max(0, min(100, brightness)),
            },
            log,
        ):
            restored_any = True

    if isinstance(color, dict):
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
            restored_any = True

    return restored_any


def capture_home_assistant_snapshot(config, log):
    """Capture Home Assistant light states using scene.create snapshots."""
    home_assistant = config["home_assistant"]
    if not home_assistant["enabled"]:
        return None

    entity_ids = home_assistant.get("entity_ids", [])
    if not entity_ids:
        log.info("Home Assistant snapshot skipped: no entity_ids configured")
        return None

    raw_scene_id, scene_entity_id = normalize_scene_entity_id(config["state_restore"]["home_assistant_scene_id"])
    if not raw_scene_id:
        log.warning("Home Assistant snapshot skipped: invalid state_restore.home_assistant_scene_id")
        return None

    payload = {
        "scene_id": raw_scene_id,
        "snapshot_entities": entity_ids,
    }

    if home_assistant_service_request(config, "scene", "create", payload, log):
        return {"scene_entity_id": scene_entity_id}
    return None


def restore_home_assistant_snapshot(config, snapshot, log):
    """Restore Home Assistant snapshot scene created at playback start."""
    home_assistant = config["home_assistant"]
    if not home_assistant["enabled"] or not isinstance(snapshot, dict):
        return False

    scene_entity_id = str(snapshot.get("scene_entity_id", "")).strip()
    if not scene_entity_id:
        return False

    if home_assistant_service_request(
        config,
        "scene",
        "turn_on",
        {"entity_id": scene_entity_id},
        log,
    ):
        log.info("Home Assistant snapshot restored: %s", scene_entity_id)
        return True

    return False


def capture_pre_playback_snapshot(config, log):
    """Capture provider state at playback start."""
    snapshot = {
        "captured_at": int(time.time()),
    }

    hue_snapshot = capture_hue_snapshot(config, log)
    if hue_snapshot is not None:
        snapshot["hue"] = hue_snapshot

    govee_snapshot = capture_govee_snapshot(config, log)
    if govee_snapshot is not None:
        snapshot["govee"] = govee_snapshot

    home_assistant_snapshot = capture_home_assistant_snapshot(config, log)
    if home_assistant_snapshot is not None:
        snapshot["home_assistant"] = home_assistant_snapshot

    if len(snapshot) == 1:
        return None
    return snapshot


def restore_pre_playback_snapshot(config, snapshot, log):
    """Restore captured provider states."""
    restored_any = False
    if isinstance(snapshot.get("hue"), dict):
        restored_any = restore_hue_snapshot(config, snapshot["hue"], log) or restored_any
    if isinstance(snapshot.get("govee"), dict):
        restored_any = restore_govee_snapshot(config, snapshot["govee"], log) or restored_any
    if isinstance(snapshot.get("home_assistant"), dict):
        restored_any = restore_home_assistant_snapshot(config, snapshot["home_assistant"], log) or restored_any
    return restored_any


def apply_stop_behavior(config, log):
    """Restore pre-playback state on stop/end, with fallback mode."""
    snapshot = None
    with RUNTIME_LOCK:
        snapshot = RUNTIME_STATE.get("snapshot")
        RUNTIME_STATE["snapshot"] = None
        RUNTIME_STATE["playback_active"] = False

    if config["state_restore"]["enabled"] and isinstance(snapshot, dict):
        if restore_pre_playback_snapshot(config, snapshot, log):
            log.info("Restored pre-playback light state")
            return
        log.warning(
            "Pre-playback restore attempted but no provider restored successfully; applying fallback mode '%s'",
            config["state_restore"]["fallback_mode"],
        )

    apply_mode(config, config["state_restore"]["fallback_mode"], log)


def apply_event_mode(config, event, mode_name, log):
    """Apply event behavior with state snapshot/restore logic."""
    if event == "play":
        if config["state_restore"]["enabled"]:
            should_capture = False
            with RUNTIME_LOCK:
                if not RUNTIME_STATE["playback_active"] and RUNTIME_STATE["snapshot"] is None:
                    should_capture = True

            if should_capture:
                snapshot = capture_pre_playback_snapshot(config, log)
                with RUNTIME_LOCK:
                    RUNTIME_STATE["snapshot"] = snapshot
                if snapshot is not None:
                    log.info("Captured pre-playback light snapshot")
                else:
                    log.warning("State restore is enabled but no provider snapshot could be captured")

        with RUNTIME_LOCK:
            RUNTIME_STATE["playback_active"] = True
        apply_mode(config, mode_name, log)
        return

    if event == "resume":
        with RUNTIME_LOCK:
            RUNTIME_STATE["playback_active"] = True
        apply_mode(config, mode_name, log)
        return

    if event == "pause":
        apply_mode(config, mode_name, log)
        return

    if event == "stop":
        apply_stop_behavior(config, log)
        return

    apply_mode(config, mode_name, log)


def set_hue_lights(config, bri, ct, log):
    """Set all configured Hue lights to given brightness and color temp."""
    hue = config["hue"]
    if not hue["enabled"]:
        return

    if not hue["lights"]:
        log.warning("Hue enabled but no lights configured")
        return

    if config.get("dry_run", False):
        for light_id in hue["lights"]:
            log.info("[DRY RUN] Hue light %s -> on=true bri=%s ct=%s", light_id, bri, ct)
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

    if config.get("dry_run", False):
        log.info("[DRY RUN] Govee -> brightness=%s rgb=(%s,%s,%s)", brightness, color["r"], color["g"], color["b"])
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


def home_assistant_service_request(config, domain, service, data, log):
    """Send one Home Assistant service call."""
    home_assistant = config["home_assistant"]
    endpoint = f"{home_assistant['url']}/api/services/{domain}/{service}"
    headers = {
        "Authorization": f"Bearer {home_assistant['token']}",
        "Content-Type": "application/json",
    }

    if config.get("dry_run", False):
        log.info("[DRY RUN] Home Assistant %s.%s -> %s", domain, service, data)
        return True

    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json=data,
            timeout=REQUEST_TIMEOUT_SECONDS,
            verify=home_assistant["verify_ssl"],
        )
    except requests.RequestException as exc:
        log.error("Home Assistant request failed: %s", exc)
        return False

    if not response.ok:
        log.error(
            "Home Assistant service %s.%s failed: HTTP %s %s",
            domain,
            service,
            response.status_code,
            response.text,
        )
        return False

    return True


def set_home_assistant_mode(config, mode_name, mode, log):
    """Apply mode via Home Assistant scene or light entities."""
    home_assistant = config["home_assistant"]
    if not home_assistant["enabled"]:
        return

    scene_entity = home_assistant["mode_scenes"].get(mode_name, "")
    if scene_entity:
        if home_assistant_service_request(
            config,
            "scene",
            "turn_on",
            {"entity_id": scene_entity},
            log,
        ):
            log.info("Home Assistant scene applied for mode '%s': %s", mode_name, scene_entity)
        return

    entity_ids = home_assistant["entity_ids"]
    if not entity_ids:
        log.warning("Home Assistant enabled but no entity_ids configured for mode '%s'", mode_name)
        return

    payload = {
        "entity_id": entity_ids,
        "brightness_pct": mode["ha_brightness_pct"],
    }

    transition_seconds = home_assistant["transition_seconds"]
    if transition_seconds > 0:
        payload["transition"] = transition_seconds

    if mode["ha_rgb_color"]:
        payload["rgb_color"] = mode["ha_rgb_color"]
    else:
        payload["color_temp_kelvin"] = mode["ha_color_temp_kelvin"]

    if home_assistant_service_request(config, "light", "turn_on", payload, log):
        log.info("Home Assistant lights updated for mode '%s'", mode_name)


def apply_mode(config, mode_name, log):
    """Apply a light mode to all configured lights."""
    mode = config["modes"].get(mode_name)
    if mode is None:
        log.error("Unknown mode '%s'", mode_name)
        return

    log.info("Applying mode: %s", mode_name)
    set_hue_lights(config, mode["hue_brightness"], mode["hue_color_temp"], log)
    set_govee_light(config, mode["govee_brightness"], mode["govee_color"], log)
    set_home_assistant_mode(config, mode_name, mode, log)


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
                apply_event_mode(config, event, mode_name, log)
            else:
                log.info("Unhandled event: %s", event)

            self.respond(200, {"status": "ok"})

        def log_message(self, format, *args):
            pass

    return WebhookHandler


# --- Main ---


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Plex Lights webhook listener")
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate config and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate light actions without sending requests to providers.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        config = load_config()
    except ValueError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.validate_config:
        print("Config is valid.")
        return

    if args.dry_run:
        config["dry_run"] = True

    log = setup_logging(config)

    hue_enabled = config["hue"]["enabled"]
    govee_enabled = config["govee"]["enabled"]
    home_assistant_enabled = config["home_assistant"]["enabled"]

    port = config["port"]
    log.info("Plex Lights starting on port %s", port)

    if hue_enabled:
        log.info("Hue: bridge=%s, lights=%s", config["hue"]["bridge_ip"], config["hue"]["lights"])
    if govee_enabled:
        log.info("Govee: device=%s, model=%s", config["govee"]["device"], config["govee"]["model"])
    if home_assistant_enabled:
        has_scene = any(config["home_assistant"]["mode_scenes"].values())
        log.info(
            "Home Assistant: url=%s, entities=%s, mode_scenes=%s",
            config["home_assistant"]["url"],
            len(config["home_assistant"]["entity_ids"]),
            "enabled" if has_scene else "disabled",
        )

    if config["webhook_token"]:
        log.info("Webhook token auth is enabled")
    if config["dry_run"]:
        log.info("Dry-run mode enabled: provider calls will be simulated")
    if config["state_restore"]["enabled"]:
        log.info(
            "State restore enabled: stop/end restores pre-playback state (fallback mode: %s)",
            config["state_restore"]["fallback_mode"],
        )
    else:
        log.info(
            "State restore disabled: stop/end uses fallback mode '%s'",
            config["state_restore"]["fallback_mode"],
        )

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
