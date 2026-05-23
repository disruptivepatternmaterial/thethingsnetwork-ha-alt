"""Default HA sensor metadata for known TTN decoded_payload field names."""

from __future__ import annotations

from typing import TypedDict


class SensorAttrDict(TypedDict, total=False):
    """Home Assistant sensor metadata for a TTN field."""

    unit: str
    device_class: str
    state_class: str
    entity_category: str
    suggested_display_precision: str
    friendly_name: str


# Keys are lowercase TTN field names. Decoder _sensor_attr values override these.
FIELD_DEFAULTS: dict[str, SensorAttrDict] = {
    "tempc_sht31": {
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
        "friendly_name": "Temperature",
    },
    "tempc_sht": {
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
        "friendly_name": "Temperature",
    },
    "tempc_ds18b20": {
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
        "friendly_name": "Probe temperature",
    },
    "tempc_ds": {
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
        "friendly_name": "Probe temperature",
    },
    "hum_sht31": {
        "unit": "%",
        "device_class": "humidity",
        "state_class": "measurement",
        "friendly_name": "Relative humidity",
    },
    "hum_sht": {
        "unit": "%",
        "device_class": "humidity",
        "state_class": "measurement",
        "friendly_name": "Relative humidity",
    },
    "batv": {
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "entity_category": "diagnostic",
        "friendly_name": "Battery voltage",
    },
    "bat": {
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "entity_category": "diagnostic",
        "friendly_name": "Battery voltage",
    },
    "distance": {
        "unit": "m",
        "device_class": "distance",
        "state_class": "measurement",
        "friendly_name": "Distance",
    },
    "distance_mm": {
        "unit": "mm",
        "device_class": "distance",
        "state_class": "measurement",
        "friendly_name": "Distance",
    },
    "wx_temperature": {
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
        "friendly_name": "Temperature",
    },
    "wx_humidity": {
        "unit": "%",
        "device_class": "humidity",
        "state_class": "measurement",
        "friendly_name": "Relative humidity",
    },
    "wx_barometer": {
        "unit": "hPa",
        "device_class": "pressure",
        "state_class": "measurement",
        "friendly_name": "Barometric pressure",
    },
    "barometer_5": {
        "unit": "hPa",
        "device_class": "pressure",
        "state_class": "measurement",
        "friendly_name": "Barometric pressure",
    },
    "barometer": {
        "unit": "hPa",
        "device_class": "pressure",
        "state_class": "measurement",
        "friendly_name": "Barometric pressure",
    },
    "wx_wind_speed": {
        "unit": "m/s",
        "device_class": "wind_speed",
        "state_class": "measurement",
        "friendly_name": "Wind speed",
    },
    "wx_wind_direction": {
        "unit": "°",
        "device_class": "wind_direction",
        "state_class": "measurement",
        "friendly_name": "Wind direction",
    },
    "hub_voltage": {
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "entity_category": "diagnostic",
        "friendly_name": "Hub voltage",
    },
    "data_time": {
        "device_class": "timestamp",
        "entity_category": "diagnostic",
        "friendly_name": "Device timestamp",
    },
    "timestamp": {
        "device_class": "timestamp",
        "entity_category": "diagnostic",
        "friendly_name": "Device timestamp",
    },
    "systimestamp": {
        "device_class": "timestamp",
        "entity_category": "diagnostic",
        "friendly_name": "Device timestamp",
    },
}


def default_field_attr(field_id: str) -> SensorAttrDict:
    """Return built-in metadata for a TTN field name, if known."""
    return dict(FIELD_DEFAULTS.get(field_id.lower(), {}))


def merge_field_attr(
    decoder_attr: SensorAttrDict, field_id: str
) -> SensorAttrDict:
    """Merge built-in defaults with decoder-provided _sensor_attr (decoder wins)."""
    merged = default_field_attr(field_id)
    merged.update(decoder_attr)
    return merged
