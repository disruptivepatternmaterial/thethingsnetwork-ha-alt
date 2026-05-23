# Field mappings guide

Edit **`field_mappings.json`** next to this file. Keys must be the TTN **`decoded_payload` field name in lowercase** — same string you see in the entity id after the device id (e.g. `Hum_SHT31` → key `"hum_sht31"`).

Pair with **`device_names.json`** for friendly device names (`9181010k6063240022` → `RAK Weather Sensor 002`).

After edits: HACS update (or copy file to HA) → **restart Home Assistant**.

If a field **changes platform** (sensor → binary_sensor), delete the old entity in Settings → Entities.

---

## Sensor (default)

Omit `platform` (or use `"sensor"`). Set unit + device_class + friendly_name as needed.

```json
"hum_sht31": {
  "friendly_name": "Relative humidity",
  "unit": "%",
  "device_class": "humidity",
  "state_class": "measurement"
}
```

Common `device_class` values: `temperature`, `humidity`, `pressure`, `voltage`, `battery`, `distance`, `wind_speed`, `wind_direction`, `timestamp`, `illuminance`, `acceleration`.

Use `"entity_category": "diagnostic"` for battery, firmware, serial, timestamps.

---

## Binary sensor

Use when the decoder sends strings (`occupied` / `vacant`, `OPEN` / `CLOSE`) or booleans.

```json
"occupancy": {
  "platform": "binary_sensor",
  "friendly_name": "Occupancy",
  "device_class": "occupancy",
  "state_on": ["occupied"],
  "state_off": ["vacant"]
}
```

Common `device_class` values: `occupancy`, `door`, `motion`, `opening`, `problem`, `vibration`, `running`.

`state_on` / `state_off` are optional lists of values that mean on/off. Match your decoder output exactly (case matters unless both sides are strings — matching is case-insensitive for strings).

---

## How to add a new field

1. In HA, open the entity (or TTN live data) and note the **field name** in `decoded_payload`.
2. Add a lowercase key to `field_mappings.json`.
3. Restart HA.
4. For new fields, wait for the next uplink. For relabeling, migration runs on startup; delete stale entities if the platform changed.

---

## Fleet reference (this repo’s defaults)

| Device / decoder | Example fields | Notes |
|------------------|----------------|-------|
| Dragino S31B-LS | `tempc_sht31`, `hum_sht31`, `batv`, `door_status` | Door is binary OPEN/CLOSE |
| Dragino LHT65N | `tempc_sht`, `hum_sht`, `batv` | |
| Dragino DDS75 (snow) | `distance`, `bat`, `tempc_ds18b20` | `distance` is mm |
| Dragino DS03A | `door_open_status`, `door_open_times` | |
| Dragino PB01 | `tempc_sht41`, `hum_sht41`, `button_pressed` | |
| RAK wx station | `wx_temperature`, `wx_barometer`, `wx_wind_speed` | |
| Milesight VS370 | `occupancy`, `battery`, `illuminance` | |
| RAK10703 earthquake | `earthquake_active`, `pga_m_s2`, `temperature_c` | |

Keys starting with `_` (e.g. `_about`) are ignored.
