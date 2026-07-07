# The Things Network HA-Alt

HACS custom integration fork of Home Assistant's official [The Things Network](https://www.home-assistant.io/integrations/thethingsnetwork/) integration.

Adds decoder-driven sensor metadata (`unit`, `device_class`, `state_class`, `entity_category`, `friendly_name`, `suggested_display_precision`) via `_sensor_attr` in TTN payload formatters — based on [home-assistant/core#166565](https://github.com/home-assistant/core/pull/166565).

Uses domain `thethingsnetwork_alt` so it can coexist with the official integration during testing. **Do not run both against the same TTN application** or you will get duplicate entities.

## Prerequisites

- **Home Assistant 2025.4.0 or newer** (uses the `wind_direction` device class / `measurement_angle` state class and current entity-platform APIs)

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

## Changes in 0.7.1

- **Hardened the `device_tracker` location logic** (multi-agent code review
  findings):
  - Decoded-payload GPS coordinates are now validated (must be numeric)
    before use; an invalid fix falls back to the registry location instead
    of masking it or latching into the cached location.
  - A payload GPS fix more than 24 h older than the device's newest uplink
    is considered stale and loses to the registry location, so a device
    whose GPS stopped reporting can't pin an ancient fix forever.
- Guarded against an explicit `null` `uplink_message` in tracker and
  diagnostic-sensor metadata parsing.

## Changes in 0.7.0

- **New `device_tracker` platform.** Every TTN end device now gets a GPS
  `device_tracker` entity so it shows on the Home Assistant map and in map
  cards. Location priority: GPS decoded from the payload itself
  (`TTNDeviceTrackerValue`, e.g. a RAK10701 field tester), otherwise the
  registry location set on the end device in the TTN console
  (`uplink_message.locations.user`). `locations["frm-payload"]` is
  deliberately ignored — TTN persists it from old uplinks and it can hold a
  stale, bogus coordinate. Altitude and the location source (`gps` /
  `registry`) are exposed as attributes. Exclude `_meta_location` per device
  in `field_exclusions.json` to suppress the tracker.
- **New `Gateway` diagnostic sensor** (`_meta_gateway`) alongside RSSI / SNR /
  Last seen: the `gateway_id` of the best-RSSI gateway from the latest
  uplink's `rx_metadata`.
- **Field mappings** for the RAK2560 sensor-hub decoder's per-probe fields:
  `env_temperature` / `env_humidity` (atmospheric temp/humidity probe),
  per-probe serials (`wx_serial`, `env_serial`), and `hub_voltage` split out
  from battery voltage.

## Changes in 0.6.0

Release-readiness pass driven by a multi-model code review.

- **Security: the TTN API key is no longer written to debug logs.** Setup and
  unload logged the raw API key at DEBUG level (inherited from the upstream
  core integration); they now log the application ID.
- **Correct minimum Home Assistant version.** `hacs.json` claimed `2024.6.0`,
  but the code uses APIs introduced in 2025.3/2025.4
  (`AddConfigEntryEntitiesCallback`, `measurement_angle`). Minimum is now
  `2025.4.0`.
- **GPS altitude sensor no longer permanently suppressed** when the first
  uplink lacks an altitude value — it is now created as soon as an uplink
  includes one.
- **RSSI / SNR / Last-seen read the newest uplink** for the device instead of
  the first field in iteration order, which could lag behind the most recent
  packet.
- **Robust timestamp parsing.** ISO strings with sub-second precision or
  timezone offsets now parse (previously returned unknown), and malformed
  numeric timestamps can no longer raise out of the sensor.
- **Type-change safety.** A field whose value type changes (decoder update)
  now logs a warning instead of raising `AssertionError` inside the
  coordinator callback.
- **Config-flow host normalization.** Pasting `https://eu1.cloud.thethings.network/`
  now works; the scheme and trailing slash are stripped before validation.
- Added `LICENSE` (Apache-2.0), GitHub Actions validation (hassfest + HACS),
  `loggers` in the manifest, duplicate-key warnings for `field_mappings.json`,
  binary-sensor metadata validation warnings, and documentation corrections
  (array mapping example, `device_names.json` naming, migration behavior,
  HACS-update-overwrites-JSON warning).

## Changes in 0.5.3

- **RSSI / SNR / Last-seen no longer go blank for infrequent senders.**
  After the first fetch, each poll only covers the seconds since the
  previous poll, so a device that did not uplink in that window is absent
  from the coordinator data. The diagnostic meta-sensors read that data
  live and dropped to `unknown`/`unavailable` on every such poll — only a
  constantly-transmitting device ever showed a value. They now retain the
  last computed reading, matching how the regular sensors persist.
- **Moved JSON config reads off the event loop.** `field_mappings.json`,
  `field_exclusions.json`, and `device_names.json` were read with blocking
  I/O during setup, which Home Assistant flags as a blocking call in the
  event loop. The caches are now primed in the executor before any
  in-loop accessor runs.
- **Much more complete field mappings.** Standardized PM/AQI naming and
  device classes (`PM1`/`PM2.5`/`PM4`/`PM10`, `AQI`, `AQI (PM2.5)`,
  `AQI (PM10)`), corrected Dragino S31-LB/LSN50 interrupt fields
  (`Door_status`, `EXTI_Trigger`, pin level are the external-interrupt
  input — diagnostic, not a real door; the real door uses
  `door_open_status`), `Battery status`/`Battery OK`/`Data confidence`/
  `Fog suspect` typing, and various status/diagnostic fields. The Dragino
  `datalog*` replay buffers (lists of past readings, not live values) are
  now excluded.

## Changes in 0.5.2

- **Fixed startup crash in the entity-metadata migration.** It tried to
  write `state_class` through `entity_registry.async_update_entity()`,
  which rejects that argument (`state_class` is not a registry column —
  it lives under read-only `capabilities`). Every reload logged
  `Failed to migrate entity metadata for …` and aborted that entity's
  migration. `state_class` is already applied natively by the sensor on
  each load, so the redundant registry write was removed.
- **Fixed `wind_direction` state class.** The `wx_wind_direction`
  mapping used `state_class: measurement` which Home Assistant rejects
  for the `wind_direction` device class (it requires `measurement_angle`
  or none). Updated the default mapping to `measurement_angle`.

### Known upstream log noise

The bundled `ttn_client==1.3.0` parser logs a `WARNING` for every
`decoded_payload` field that arrives as `null`
(`Ignoring entry <field> with value=None - check your application
decoder`). This comes from the library, not this integration, and can be
high-volume for sensors that report sparse fields. Quiet it from
`configuration.yaml`:

```yaml
logger:
  logs:
    ttn_client.parsers.default: error
```

## What you get out of the box

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

`field_mappings.json` is a JSON **array**; each entry maps one profile to a list of TTN field names (`keys`). Example for Milesight VS370 occupancy:

```json
[
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

Use `"platform": "binary_sensor"` for on/off fields that arrive as strings or numbers. Sensor fields omit `platform` (default). See `custom_components/thethingsnetwork_alt/FIELD_MAPPINGS.md` for the full schema.

**These files live inside the integration folder, so a HACS update overwrites them.** Make edits in your fork/repo (so they ship with the next HACS update), not just on the HA host. After editing, update via HACS and restart. Delete stale entities if a field moved from sensor to binary_sensor.

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

You can also disable the synthetic diagnostic sensors per-device or globally by adding `_meta_rssi`, `_meta_snr`, `_meta_last_seen`, or `_meta_gateway` to the exclusion list, and the per-device location tracker by adding `_meta_location`.

After editing, update via HACS and restart. To delete entities that are already in Home Assistant after excluding them, remove them from Settings → Devices & services → Entities.

## Field defaults (legacy note)

Without `_sensor_attr` in your TTN decoder, built-in defaults in `field_mappings.json` apply for common Dragino/RAK/VS370 field names. Edit that file to add more.

Device friendly names come from `device_names.json`. Edit that file for your fleet, update via HACS, and restart — names are applied to existing devices on startup.

On every startup a migration pass applies the current `field_mappings.json` names, units, device classes, and entity categories to **existing** registry entries, and removes stale `sensor` entities whose field moved to `binary_sensor` (they are recreated on the next uplink). The one case that still needs manual cleanup is a field moving from `binary_sensor` back to `sensor` — delete that entity in Settings → Entities.

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

Devices are named from `device_names.json` (TTN end-device `device_id` → friendly name). Devices without an entry fall back to the raw `device_id`.

## Upstream

- [home-assistant/core `thethingsnetwork`](https://github.com/home-assistant/core/tree/dev/homeassistant/components/thethingsnetwork)
- [angelnu/thethingsnetwork_python_client](https://github.com/angelnu/thethingsnetwork_python_client) (`ttn_client==1.3.0`)

## License

Derived from Home Assistant Core (Apache 2.0).
