# Field mappings, exclusions & device names

Three JSON files in this folder control entity metadata:

| File | Purpose |
|---|---|
| `field_mappings.json` | TTN field → HA entity profile (unit, device_class, friendly name, platform). |
| `field_exclusions.json` | TTN fields to **hide** from HA entirely (global or per device). |
| `device_names.json` | TTN device id → friendly device name. |

After edits: HACS update → **restart Home Assistant**.

---

## Format

`field_mappings.json` is a **JSON array**. One object per profile:

```json
[
  {
    "device_class": "temperature",
    "state_class": "measurement",
    "friendly_name": "Temperature",
    "unit": "°C",
    "keys": ["tempc_sht41", "tempc_sht", "tempc_sht31", "wx_temperature"]
  },
  {
    "platform": "binary_sensor",
    "device_class": "occupancy",
    "friendly_name": "Occupancy",
    "state_on": ["occupied"],
    "state_off": ["vacant"],
    "keys": ["occupancy"]
  }
]
```

Field names in `keys` are matched **case-insensitively** against TTN `decoded_payload` keys.

---

## Sensor (default)

Omit `platform`. Set `unit`, `device_class`, `state_class`, `friendly_name` as needed.

---

## Binary sensor

```json
{
  "platform": "binary_sensor",
  "device_class": "occupancy",
  "friendly_name": "Occupancy",
  "state_on": ["occupied"],
  "state_off": ["vacant"],
  "keys": ["occupancy"]
}
```

---

## Add a new field

1. Find the TTN field name in live data or the entity id suffix.
2. Add it to an existing entry’s `keys`, or add a new array object.
3. Restart HA.

If a field **changes platform** (sensor → binary_sensor), delete the old entity in Settings → Entities.

---

## Legacy format

The old `{ "field_name": { ... } }` object format still loads, but new edits should use the array + `keys` form above.

---

## `field_exclusions.json`

Hide fields from Home Assistant entirely (no sensor / no binary sensor is created). Matched case-insensitively against the TTN `decoded_payload` field id. Trailing `*` matches by prefix.

```json
{
  "global": ["raw_payload", "debug_*"],
  "devices": {
    "muon-air-sensor-001": ["wx_wind_direction"],
    "la666150458": ["adc_v"]
  }
}
```

Special synthetic diagnostics that can be excluded:

- `_meta_rssi` — best gateway RSSI per uplink
- `_meta_snr` — best gateway SNR per uplink
- `_meta_last_seen` — timestamp of most recent uplink

Put them in `global` to turn the diagnostics off everywhere, or in a `devices` entry to turn them off only for specific devices.

If an excluded field already has an entity in HA, remove it from Settings → Devices & services → Entities after restarting.

---

## Built-in defaults (v0.5.0)

The following are recognised automatically — you do not need to map them yourself:

- `latitude` / `lat` / `gps_latitude` → Latitude (°)
- `longitude` / `lon` / `lng` / `gps_longitude` → Longitude (°)
- `altitude` / `alt` / `gps_altitude` → Altitude (m, device_class=distance)
- A decoder that emits a nested GPS object (e.g. `{"gps": {"latitude": ..., "longitude": ..., "altitude": ...}}`) is expanded into three sensors named `gps_latitude`, `gps_longitude`, `gps_altitude`.

Fields not explicitly mapped get an **auto-generated friendly name** (`battery_voltage_mv` → "Battery Voltage Mv") and inherit unit/device_class from common suffixes:

| Suffix | Unit | Device class |
|---|---|---|
| `_mv` | mV | voltage |
| `_v` | V | voltage |
| `_ma` | mA | current |
| `_a` | A | current |
| `_lux` / `_lx` | lx | illuminance |
| `_pct` / `_percent` | % | — |
| `_hpa` | hPa | pressure |
| `_kpa` | kPa | pressure |
| `_pa` | Pa | pressure |
| `_c` | °C | temperature |
| `_f` | °F | temperature |
| `_k` | K | temperature |
| `_mm` / `_cm` / `_m` / `_km` | mm/cm/m/km | distance |
| `_g` / `_kg` | g/kg | weight |
| `_dbm` | dBm | signal_strength |
| `_db` | dB | — |

Anything in `field_mappings.json` overrides the heuristic.

---

## How to see what fields TTN is sending

On every startup the integration logs one INFO line per device:

```
TTN HA-Alt device=<device_id> fields=[a, b, c, ...] excluded=[...] unmapped=[...]
```

Search Home Assistant logs for `TTN HA-Alt device=` to see what's available and what's not yet mapped.
