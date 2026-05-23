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
2. Add `https://github.com/ntableman/thethingsnetwork-ha-alt`, category **Integration**
3. Search **The Things Network HA-Alt** → Download
4. Restart Home Assistant
5. Settings → Devices & services → Add integration → **The Things Network HA-Alt**

## Decoder metadata

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
