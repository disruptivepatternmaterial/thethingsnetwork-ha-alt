# The Things Network HA-Alt

HACS custom integration fork of Home Assistant's official [The Things Network](https://www.home-assistant.io/integrations/thethingsnetwork/) integration.

Adds decoder-driven sensor metadata (`unit`, `device_class`, `state_class`, `entity_category`, `friendly_name`, `suggested_display_precision`) via `_sensor_attr` in TTN payload formatters — based on [home-assistant/core#166565](https://github.com/home-assistant/core/pull/166565).

Uses domain `thethingsnetwork_alt` so it can coexist with the official integration during testing. **Do not run both against the same TTN application** or you will get duplicate entities.

## Prerequisites

Same as the official integration:

1. TTN Storage integration enabled on your application
2. Uplink payload formatter that produces a `decoded_payload`
3. API key with `Read Application Traffic (uplink and downlink)`

## HACS install

1. HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/disruptivepatternmaterial/thethingsnetwork-ha-alt`, category **Integration**
3. Search **The Things Network HA-Alt** → Download
4. Restart Home Assistant
5. Settings → Devices & services → Add integration → **The Things Network HA-Alt**

## What you get out of the box (v0.5.0)

- Sensors for every numeric / string / boolean field in `decoded_payload`.
- Binary sensors for fields configured in `field_mappings.json` with `"platform": "binary_sensor"` (doors, occupancy, alarms, etc.).
- **Lat / lon / altitude** sensors — automatically recognised from common field names (`latitude` / `longitude` / `lat` / `lon` / `lng` / `altitude` / `alt` / `gps_*`), and **also surfaced** when a decoder emits a nested GPS object (e.g. `{ "gps": { "latitude": 47.6, "longitude": -122.3 } }`) — these previously vanished silently.
- **Per-device diagnostic sensors** — `RSSI` (dBm, signal_strength), `SNR` (dB), and `Last seen` (timestamp) — extracted from each uplink's `rx_metadata` / `received_at`. Useful for monitoring sensor health and gateway coverage.
- **Auto-generated friendly names** for fields not explicitly mapped (snake_case → Title Case, with common suffix heuristics like `_mv`, `_ma`, `_c`, `_lux`, `_pct`, `_hpa`, `_dbm`, etc.).
- **One-time INFO log** on startup listing every field per device, plus which are excluded and which lack an explicit mapping. Search Home Assistant logs for `TTN HA-Alt device=` after a restart.

## Field mappings, exclusions, and device names

Three JSON files next to the integration code:

- `field_mappings.json` — TTN field name → HA entity type, unit, device_class, friendly name
- `field_exclusions.json` — TTN field names to **hide** from Home Assistant
- `device_names.json` — TTN device ID → friendly device name

Example `field_mappings.json` entry for Milesight VS370 occupancy:

```json
"occupancy": {
  "platform": "binary_sensor",
  "friendly_name": "Occupancy",
  "device_class": "occupancy",
  "state_on": ["occupied"],
  "state_off": ["vacant"]
}
```

Use `"platform": "binary_sensor"` for on/off fields that arrive as strings or numbers. Sensor fields omit `platform` (default).

After editing, update via HACS and restart. Delete stale entities if a field moved from sensor to binary_sensor.

## Field exclusions

Drop a `field_exclusions.json` next to `field_mappings.json` to hide fields you don't want as Home Assistant entities. Matched **case-insensitively** against TTN `decoded_payload` field names. Trailing `*` matches by prefix.

```json
{
  "global": ["raw_payload", "debug_*"],
  "devices": {
    "muon-air-sensor-001": ["wx_wind_direction"],
    "la666150458": ["adc_v"]
  }
}
```

You can also disable the synthetic diagnostic sensors per-device or globally by adding `_meta_rssi`, `_meta_snr`, or `_meta_last_seen` to the exclusion list.

After editing, update via HACS and restart. To delete entities that are already in Home Assistant after excluding them, remove them from Settings → Devices & services → Entities.

## Field defaults (legacy note)

Without `_sensor_attr` in your TTN decoder, built-in defaults in `field_mappings.json` apply for common Dragino/RAK/VS370 field names. Edit that file to add more.

Device friendly names come from `device_names.json`. Edit that file for your fleet, update via HACS, and restart — names are applied to existing devices on startup.

**Existing entities keep old names/units.** Remove the integration, delete its devices from Settings → Devices, update via HACS, restart, then add the integration again — or delete individual stale entities when platform changes (e.g. occupancy sensor → binary_sensor).

## Decoder metadata (optional override)

Add a `_sensor_attr` object to your TTN payload formatter output:

```javascript
var HA_ATTR = {
  TempC_SHT: {
    unit: "°C",
    device_class: "temperature",
    state_class: "measurement",
    friendly_name: "Air temperature",
  },
  Hum_SHT: {
    unit: "%",
    device_class: "humidity",
    state_class: "measurement",
    friendly_name: "Humidity",
  },
  BatV: {
    unit: "V",
    device_class: "voltage",
    state_class: "measurement",
    entity_category: "diagnostic",
    friendly_name: "Battery",
  },
};

function decodeUplink(input) {
  // decode bytes...
  return {
    data: {
      TempC_SHT: temp,
      Hum_SHT: hum,
      BatV: bat,
      _sensor_attr: HA_ATTR,
    },
  };
}
```

`_sensor_attr` fields do not become entities.

## Device names

Device name in Home Assistant is the TTN end-device `device_id`. Rename the device in TTN Console for a friendlier label.

## Upstream

- [home-assistant/core `thethingsnetwork`](https://github.com/home-assistant/core/tree/dev/homeassistant/components/thethingsnetwork)
- [angelnu/thethingsnetwork_python_client](https://github.com/angelnu/thethingsnetwork_python_client) (`ttn_client==1.3.0`)

## License

Derived from Home Assistant Core (Apache 2.0).
