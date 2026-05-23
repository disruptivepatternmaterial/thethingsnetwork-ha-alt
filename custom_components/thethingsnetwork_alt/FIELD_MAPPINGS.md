# Field mappings guide

Edit **`field_mappings.json`**. Each entry is one HA entity profile; list every TTN `decoded_payload` field name that should use it in **`keys`**.

Pair with **`device_names.json`** for friendly device names.

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
