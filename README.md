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

## Changes in 0.8.1

- **Four shipped field mappings claimed a device class Home Assistant does
  not have.** `pga_m_s2`, `accel_x_g`, `accel_y_g` and `accel_z_g` were
  mapped to `device_class: acceleration`, which is not a Home Assistant
  sensor device class. Home Assistant discarded it and the integration
  logged a warning for each field on every startup. The readings themselves
  were never affected. The device class is gone; the unit (`m/s²`, `g`) and
  `state_class: measurement` stay, which is what those sensors were
  actually getting.

- **The shipped metadata is now validated by the test suite.** Every device
  class, state class and entity category in `field_mappings.json`, and every
  one produced by the field-name suffix heuristics, is checked against Home
  Assistant's own enums — along with a check that no field is mapped twice.
  A value Home Assistant does not recognise was previously only discoverable
  by reading the log of a running instance.

## Changes in 0.8.0

- **A failed poll no longer skips past the uplinks it never read.** The
  library this integration used advanced its "read up to here" watermark
  *before* issuing the request, so a fetch that failed still counted as
  having covered that window. With a 60-second polling period and a
  60-second overlap margin, one failure healed itself and two consecutive
  failures dropped every uplink in between — permanently, because the
  storage API is only ever asked for the time since the watermark. TTN had
  the data and Home Assistant never asked for it again. The watermark now
  moves only after a response has been read to the end, so a failed or
  refused poll leaves its window to be re-read by the next one.

- **One unreadable record no longer costs the whole window.** Every record
  in a response used to be parsed before any of it was returned, so a single
  record that could not be read discarded the readings for *every* device in
  that window, not just the device that sent it. Records are now parsed one
  at a time: the unreadable ones are counted and logged, and everything else
  in the same response still lands.

  This is reachable from an ordinary decoder mistake. A decoder returning an
  array instead of an object, an uplink with no `uplink_message`, or a
  Sensecap payload marked `valid` but missing `err` each raise a different
  exception out of the parser, which is why the guard around it is
  deliberately broad rather than a list of expected failures.

- **A mistyped application ID is no longer reported as a bad API key.** Any
  4xx was previously reported as invalid authentication, so a typo in the
  application ID sent you off to reissue a working key. A refused credential
  (401/403) is now the only thing reported as an authentication problem;
  anything else — a 404 for an application that does not exist, storage not
  enabled for the application, a 5xx, an unreachable host — is reported as a
  connection problem, and the log carries the API's own explanation.

- **Polling uses Home Assistant's shared HTTP session** instead of opening
  and discarding a new one every minute.

- **Lint is now enforced.** `pyproject.toml` carries Home Assistant core's
  own ruff rule selection, taken verbatim, and CI fails on `ruff check` or
  `ruff format --check`. Fixing the findings turned up one real bug: TTN
  timestamps were normalised with `text.replace("Z", "+00:00")`, which
  rewrites every `Z` in the string rather than just a trailing offset.

- The config and reauth flows have tests for the first time.

## Changes in 0.7.4

- **A decoded field is no longer swallowed by a GPS object of the same name.**
  A `gps` object is expanded into one sensor per axis, and those were named
  `gps_latitude`, `gps_longitude` and `gps_altitude` — which are also
  perfectly ordinary field names, and ones this integration maps by default.
  A decoder sending both a GPS object and a flat `gps_latitude` reading had
  the flat one silently discarded: the axis reserved the name first, so
  discovery skipped the real field. No entity, no warning, and nothing in the
  log to say a reading was being dropped every uplink. The axes now live in
  the reserved synthetic namespace (`_gps_<parent>_<component>`) that decoded
  fields cannot reach, because discovery ignores any field starting with `_`.

  Existing axis entities are renamed in the entity registry during setup, so
  their entity id, customisations and recorder history carry over. An
  exclusion written against the old name still works. A device that sends a
  flat `gps_latitude` and no GPS object is left alone.

## Changes in 0.7.3

- **Sensors no longer silently stop updating.** Four separate defects in the
  coordinator update path each caused a field to stop tracking new uplinks
  while TTN kept delivering them:
  - An uplink timestamp without a UTC offset produced a naive `datetime`,
    and comparing it against an aware one raised `TypeError`. Home Assistant
    catches listener exceptions and only logs them, so the affected entity
    quietly stopped updating for the rest of the run. Receipt times are now
    normalised to aware UTC before comparison.
  - A missing or malformed `received_at` raised `KeyError` / `ValueError`
    from the same unguarded comparison, with the same permanent effect. Both
    are now handled, and an update that cannot be dated is applied rather
    than discarded — an unreadable timestamp never freezes an entity.
  - A field that reports `0`/`1` on some uplinks and `false`/`true` on others
    alternates between `TTNSensorValue` and `TTNBinarySensorValue`, and the
    exact-type guard threw those readings away. Both classes carry a usable
    scalar, so swapping between them is now accepted. Genuinely incompatible
    changes (a scalar becoming a GPS fix) are still refused, and the warning
    is logged once per transition instead of once per uplink.
  - TTN sends nanosecond timestamps that `fromisoformat` truncates to
    microseconds, so two distinct uplinks could compare equal and the second
    was dropped. Ordering now falls back to the full-precision stamp, which
    still makes a re-delivered uplink idempotent across overlapping fetch
    windows.
- **A sensor could be frozen by an unhandled `AttributeError`.** When a field
  that normally reports numbers sent text instead, `native_value` called a
  method that did not exist. That raised inside Home Assistant's state write,
  so the sensor stopped at its last reading and every later uplink for it was
  lost — with only a traceback in the log to say so.

- **Metadata now follows what a field actually reports.** Numeric metadata
  (`unit`, `device_class`, `state_class`, `suggested_display_precision`) is
  suppressed while a field has only ever reported text, because Home
  Assistant refuses a text state on a sensor that promises a number. That
  decision used to be taken once, at entity creation, so a mapped
  measurement whose *first* uplink happened to carry an error string spent
  the rest of the run with no unit, no device class and no statistics. The
  first numeric reading now restores the mapping, once, and a later text
  reading reports `unknown` rather than tearing the unit back off a sensor
  that has history behind it.

- **Decoder `_sensor_attr` is remembered between fetches.** It was read from
  the current fetch window only, so a decoder that sends its metadata on a
  different cadence than the measurement — or in the same uplink as a
  *different* field — left the entity permanently without a unit. Metadata
  seen in any window now reaches the entity whenever it is created.

- **Binary sensors accept late decoder metadata at all.** They had no
  equivalent path: a `device_class` that did not ride the very first uplink
  never arrived.

- **`device_names.json` is re-read when the config entry is set up.** It was
  cached for the life of the Home Assistant process, unlike every other JSON
  file the integration reads, so renaming a device appeared to do nothing
  until Home Assistant itself was restarted.

- **The startup migration no longer overwrites your customisations.** It
  wrote the mapped name and device class into the entity registry's `name`
  and `device_class` columns — which are the slots a *user's* rename and
  override live in, and which Home Assistant deliberately never touches. Any
  rename you made was reverted on every restart. Those columns are now left
  alone, and an override this integration wrote in an earlier version is
  cleared so the mapping shows through again. Nothing is lost by this: Home
  Assistant already refreshes `original_name`, `original_device_class`, the
  unit, the entity category and the capabilities from the entity itself every
  time it loads, so editing `field_mappings.json` reaches existing entities
  without any registry write.

- **A field mapped from `binary_sensor` back to `sensor` no longer strands
  the old entity.** The opposite direction was already cleaned up; this one
  left a binary sensor in the registry sitting at whatever reading it held
  when the mapping changed.

- **`suggested_display_precision: 0` is honoured.** Zero — "show this as a
  whole number" — was read as "unset".

- **A record TTN sends that `ttn_client` cannot parse is reported as an
  update failure**, naming the cause, instead of an unexpected-error
  traceback every polling period. The window is still lost (the library
  parses the whole response before returning any of it), but the readings
  already held survive and the next poll runs normally.

- **The repository has a test suite.** `pytest` with
  `pytest-homeassistant-custom-component`, run in CI on every push and pull
  request. Each fix above has a regression test, verified to fail when the
  defect is put back.

## Changes in 0.7.2

- **Device names apply to existing HA devices.** `device_names.json` is the
  display-name map (`muon-air-sensor-004` → `Olivine-Bowman`). A trailing
  comma no longer empties the map (JSON parse used to fail closed and reset
  every device to its raw TTN id). Existing registry names are updated on
  setup.
- **`device_tracker` Location uses Home Assistant's GPS attributes.** Coords
  are written to `_attr_latitude` / `_attr_longitude` so map cards and zone
  state work. Flat decoder fields (`latitude` / `longitude`) are used when
  there is no nested GPS object, then the TTN console registry location.

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

**These files live inside the integration folder, so a HACS update overwrites them.** Make edits in your fork/repo (so they ship with the next HACS update), not just on the HA host. After editing, update via HACS and reload the integration. Moving a field between `sensor` and `binary_sensor` needs no manual cleanup — the entity on the old platform is removed and recreated on the next uplink.

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

Device friendly names come from `device_names.json`. Edit that file for your fleet, update via HACS, then reload the integration — all three JSON files are re-read whenever the config entry is set up, and existing devices are renamed at that point.

Edits to `field_mappings.json` reach entities that already exist without any special handling: Home Assistant re-reads each entity's name, device class, unit, entity category and capabilities from the integration every time it loads. A migration pass on setup covers only what an entity cannot do for itself — renaming devices, and removing an entity whose field has been mapped to the other platform (it is recreated on the next uplink). It deliberately does **not** write the entity registry's `name` or `device_class` columns, so a rename or device-class override you set in Settings → Entities is yours and survives restarts.

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

Devices are named from `device_names.json` (TTN end-device `device_id` → friendly name). Devices without an entry fall back to the raw `device_id`. The file must be a JSON object; a trailing comma after the last entry is tolerated but still invalid JSON.

## Upstream

- [home-assistant/core `thethingsnetwork`](https://github.com/home-assistant/core/tree/dev/homeassistant/components/thethingsnetwork)
- [angelnu/thethingsnetwork_python_client](https://github.com/angelnu/thethingsnetwork_python_client) (`ttn_client==1.3.0`)

  Used for its decoder parsing and value types. Since 0.8.0 the storage API
  call itself lives in `storage.py` rather than `ttn_client.TTNClient`, for
  the window-accounting and per-record reasons in the 0.8.0 notes above.

## License

Derived from Home Assistant Core (Apache 2.0).
