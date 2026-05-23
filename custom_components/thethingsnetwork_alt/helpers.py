"""Shared helpers for TTN HA-Alt platforms."""

from __future__ import annotations

from typing import Final, TypeVar, cast

from ttn_client import TTNSensorAttribute

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

from .field_defaults import SensorAttrDict

_SENSOR_ATTR_PREFIX: Final = "_sensor_attr_"
_ATTR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "unit",
        "device_class",
        "state_class",
        "entity_category",
        "suggested_display_precision",
        "friendly_name",
    }
)

EnumT = TypeVar(
    "EnumT",
    SensorDeviceClass,
    SensorStateClass,
    BinarySensorDeviceClass,
    EntityCategory,
)


def parse_enum(enum_cls: type[EnumT], raw: object | None) -> EnumT | None:
    """Parse a raw decoder value into a Home Assistant enum."""
    if raw is None:
        return None

    try:
        return enum_cls(str(raw))
    except (ValueError, TypeError):
        return None


def extract_sensor_attr(fields: dict[str, object]) -> dict[str, SensorAttrDict]:
    """Extract flattened TTN sensor attribute keys into a nested dict."""
    sensor_attr: dict[str, SensorAttrDict] = {}

    for key, value in fields.items():
        if not isinstance(value, TTNSensorAttribute):
            continue

        if not key.startswith(_SENSOR_ATTR_PREFIX):
            continue

        remainder = key[len(_SENSOR_ATTR_PREFIX) :]

        for attr_key in _ATTR_KEYS:
            if not remainder.endswith(f"_{attr_key}"):
                continue

            field_name = remainder[: -(len(attr_key) + 1)]
            cast(dict[str, str], sensor_attr.setdefault(field_name, {}))[attr_key] = (
                str(value.value)
            )
            break

    return sensor_attr
