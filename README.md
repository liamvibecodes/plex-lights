<div align="center">
  <br>
  <img src="https://img.shields.io/badge/PLEX_LIGHTS-EBAF00?style=for-the-badge&logo=plex&logoColor=white" alt="Plex Lights" height="40" />
  <br><br>
  <strong>Auto-dims your lights when a movie starts playing on Plex</strong>
  <br>
  <sub>Webhook server for Tautulli. Supports Philips Hue, Govee, and Home Assistant lights. Runs as a background service on macOS.</sub>
  <br><br>
  <img src="https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/Plex-EBAF00?style=flat-square&logo=plex&logoColor=white" />
  <img src="https://img.shields.io/badge/Tautulli-E5A00D?style=flat-square&logoColor=white" />
  <img src="https://img.shields.io/badge/Philips_Hue-4DB8FF?style=flat-square&logoColor=white" />
  <img src="https://img.shields.io/badge/Govee-00C853?style=flat-square&logoColor=white" />
  <img src="https://img.shields.io/badge/Home_Assistant-18BCF2?style=flat-square&logo=homeassistant&logoColor=white" />
  <img src="https://img.shields.io/badge/macOS-000000?style=flat-square&logo=apple&logoColor=white" />
  <br><br>
  <img src="https://img.shields.io/github/stars/liamvibecodes/plex-lights?style=flat-square&color=yellow" />
  <img src="https://img.shields.io/github/license/liamvibecodes/plex-lights?style=flat-square" />
  <br><br>
</div>

## What It Does

| Event | Lights |
|-------|--------|
| **Play / Resume** | Dim to candlelight (~5%, warm amber) |
| **Pause** | Brighten slightly (~30%, soft amber) |
| **Stop / End** | Back to normal (100%, warm white) |

Works with movies and TV episodes. Ignores music, photos, and other media types.

## How It Works

```
Plex -> Tautulli (webhook) -> plex-lights.py (port 32500) -> Hue bridge / Govee API / Home Assistant API
```

Tautulli sends play/pause/stop webhooks to plex-lights. The script adjusts your lights based on the event. Brightness levels, color temperatures, and RGB values are configurable per mode.

## Requirements

- [Plex Media Server](https://www.plex.tv/)
- [Tautulli](https://tautulli.com/) (for webhooks)
- Python 3.8+ (`pip install -r requirements.txt`)
- Philips Hue bridge and/or Govee smart lights with a cloud API key
- Optional: Home Assistant with a long-lived access token

## Quick Start

```bash
git clone https://github.com/liamvibecodes/plex-lights.git
cd plex-lights
pip install -r requirements.txt

# Configure your lights
cp config.json.example config.json
# Edit config.json with your bridge IP, API key, light IDs, etc.

# Test it
python3 plex-lights.py

# Or test webhook flow without changing real lights
python3 plex-lights.py --dry-run

# Install as background service (auto-starts on boot)
bash install.sh
```

## Configuration

Copy `config.json.example` to `config.json` and edit it:

Set `"dry_run": true` to simulate all light actions (no provider API calls).

### Philips Hue

```json
{
  "hue": {
    "enabled": true,
    "bridge_ip": "192.168.1.xxx",
    "api_user": "your-hue-api-username",
    "lights": [1, 2, 3]
  }
}
```

**Finding your Hue API user:** Follow the [Hue API getting started guide](https://developers.meethue.com/develop/get-started-2/) to create an authorized username. Or if you already use Home Assistant, check your Hue integration for the bridge IP.

**Finding light IDs:** Open `http://<bridge-ip>/api/<username>/lights` in a browser. Each light has a numeric ID.

### Govee

```json
{
  "govee": {
    "enabled": true,
    "api_key": "your-govee-api-key",
    "device": "AA:BB:CC:DD:EE:FF:00:11",
    "model": "H6076"
  }
}
```

**Getting a Govee API key:** Open the Govee Home app > Profile > About Us > Apply for API Key.

**Finding device ID and model:** Use the [Govee API](https://developer.govee.com/reference/get-you-devices) to list your devices, or check the Govee Home app under device settings.

### Home Assistant

```json
{
  "home_assistant": {
    "enabled": true,
    "url": "http://homeassistant.local:8123",
    "token": "your-long-lived-access-token",
    "verify_ssl": true,
    "transition_seconds": 1,
    "entity_ids": ["light.living_room_lamp"],
    "mode_scenes": {
      "movie": "",
      "pause": "",
      "normal": ""
    }
  }
}
```

Use either approach:

- `entity_ids`: plex-lights calls `light.turn_on` with per-mode values.
- `mode_scenes`: set `movie`, `pause`, and/or `normal` scene entities and scenes are used for those modes.

Long-lived access token in Home Assistant:

1. Profile (bottom-left) -> Security
2. Long-Lived Access Tokens -> Create Token

### Player Filtering

By default, plex-lights triggers on ALL players. To limit it to your TV:

1. Start a movie and check the log for the player name
2. Add it to config.json:

```json
{
  "tv_player_name": "Living Room TV"
}
```

### Webhook Authentication (Optional but Recommended)

Set a shared token and require it on webhook requests:

```json
{
  "webhook_token": "change-this-to-a-random-secret"
}
```

When set, plex-lights accepts either:

- Header: `X-Plex-Lights-Token: <your-token>`
- Query param: `?token=<your-token>`

For Tautulli, easiest is adding the query parameter to the webhook URL.

### Light Modes

Customize brightness and color for each state:

```json
{
  "modes": {
    "movie": {
      "hue_brightness": 13,
      "hue_color_temp": 500,
      "govee_brightness": 5,
      "govee_color": {"r": 255, "g": 120, "b": 20}
    }
  }
}
```

| Setting | Range | Notes |
|---------|-------|-------|
| `hue_brightness` | 1-254 | 1 = dimmest, 254 = brightest |
| `hue_color_temp` | 153-500 | 153 = cool daylight, 500 = warm candlelight |
| `govee_brightness` | 0-100 | Percentage |
| `govee_color` | RGB 0-255 | Only applies to color-capable Govee lights |
| `ha_brightness_pct` | 0-100 | Home Assistant `light.turn_on` brightness |
| `ha_color_temp_kelvin` | 1500-9000 | Used when `ha_rgb_color` is `[]` |
| `ha_rgb_color` | `[]` or RGB 0-255 | `[]` disables RGB for that mode |

### Environment Variables

For simple setups without a config file:

```bash
export HUE_BRIDGE_IP=192.168.1.xxx
export HUE_API_USER=your-username
export HUE_LIGHTS=1,2,3
export GOVEE_API_KEY=your-key
export GOVEE_DEVICE=AA:BB:CC:DD:EE:FF:00:11
export GOVEE_MODEL=H6076
export HOME_ASSISTANT_URL=http://homeassistant.local:8123
export HOME_ASSISTANT_TOKEN=your-long-lived-access-token
export HOME_ASSISTANT_ENTITY_IDS=light.living_room_lamp,light.tv_bias
export HOME_ASSISTANT_MODE_SCENES=movie:scene.movie_mode,pause:scene.pause_mode
export HOME_ASSISTANT_VERIFY_SSL=true
export TV_PLAYER_NAME="Living Room TV"
export PLEX_LIGHTS_WEBHOOK_TOKEN="change-this-to-a-random-secret"
export PLEX_LIGHTS_DRY_RUN=false
python3 plex-lights.py
```

### Dry-Run Mode

Use dry-run when validating Tautulli webhook payloads and mode mapping:

- CLI override: `python3 plex-lights.py --dry-run`
- Config: `"dry_run": true`
- Env: `PLEX_LIGHTS_DRY_RUN=true`

In dry-run mode, webhook handling and logs run normally, but no Hue, Govee, or Home Assistant API requests are sent.

## Tautulli Webhook Setup

1. Open Tautulli > Settings > Notification Agents > Add a new notification agent
2. Select **Webhook**
3. Set the webhook URL to `http://localhost:32500?token=change-this-to-a-random-secret`
4. Under **Triggers**, enable:
   - Playback Start
   - Playback Stop
   - Playback Pause
   - Playback Resume
5. Under **Data**, set the JSON body for each trigger:

```json
{
  "event": "{action}",
  "player": "{player}",
  "title": "{title}",
  "media_type": "{media_type}"
}
```

6. Save and test with "Test Notification"

Health endpoint:

```bash
curl http://localhost:32500/health
```

## Running as a Service

The install script creates a launchd job that starts on boot and auto-restarts if it crashes:

```bash
# Install
bash install.sh

# Restart
launchctl kickstart -k gui/$(id -u)/com.plex-lights

# Stop
launchctl bootout gui/$(id -u)/com.plex-lights

# Uninstall
bash install.sh --uninstall
```

## Works With

- [mac-media-stack](https://github.com/liamvibecodes/mac-media-stack) - Docker-based media server for macOS
- [mac-media-stack-advanced](https://github.com/liamvibecodes/mac-media-stack-advanced) - Full automated media server with Tautulli included

## Author

Built by [@liamvibecodes](https://github.com/liamvibecodes)

## License

[MIT](LICENSE)
