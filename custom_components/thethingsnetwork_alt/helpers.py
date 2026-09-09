"""Shared helpers for TTN HA-Alt platforms."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Final, TypeVar, cast

from ttn_client import (
    TTNBaseValue,
    TTNBinarySensorValue,
    TTNSensorAttribute,
    TTNSensorValue,
)

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

from .field_defaults import PlatformType, SensorAttrDict, get_field_mapping

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


def received_at_utc(value: TTNBaseValue) -> datetime | None:
    """Return an aware UTC receipt time, or None when it cannot be read.

    ``TTNBaseValue.received_at`` re-parses the raw uplink string on every
    access and is unguarded: a missing key raises ``KeyError``, a malformed
    stamp raises ``ValueError``, and a stamp with no offset yields a naive
    datetime that cannot be compared against an aware one.
    """
    try:
        received_at = value.received_at
    except (KeyError, TypeError, ValueError):
        return None

    if not isinstance(received_at, datetime):
        return None

    # TTN timestamps are UTC; one without an offset is still UTC.
    if received_at.tzinfo is None:
        return received_at.replace(tzinfo=UTC)
    return received_at.astimezone(UTC)


def raw_received_at(value: TTNBaseValue) -> str | None:
    """Return the uplink's unparsed receipt stamp.

    TTN reports nanoseconds but ``datetime.fromisoformat`` truncates to
    microseconds, so two distinct uplinks can share a parsed timestamp. The
    original string keeps the full precision and is what distinguishes a
    re-delivered uplink from a genuinely new one.
    """
    uplink = getattr(value, "uplink", None)
    if not isinstance(uplink, dict):
        return None
    stamp = uplink.get("received_at")
    return None if stamp is None else str(stamp)


def newest_uplink_carrier(values: Iterable[object]) -> TTNBaseValue | None:
    """Return the TTN value carrying the most recent uplink.

    Fields can retain uplinks of different ages (a field absent from the
    latest packet keeps its older uplink), so pick by receipt time rather
    than dict iteration order. Receipt times are normalised to aware UTC
    first: one device's fields can carry stamps with and without an offset,
    and comparing those directly raises ``TypeError`` out of whichever
    entity property called this.
    """
    newest: TTNBaseValue | None = None
    newest_received_at: datetime | None = None
    for value in values:
        if not (isinstance(value, TTNBaseValue) and getattr(value, "uplink", None)):
            continue
        received_at = received_at_utc(value)
        if received_at is None:
            continue
        if newest_received_at is None or received_at > newest_received_at:
            newest = value
            newest_received_at = received_at
    return newest


def platform_for_value(field_id: str, ttn_value: object) -> PlatformType | None:
    """Return the single platform that owns ``field_id``, or None for neither.

    ``ttn_client``'s parser picks the value class from the Python type of the
    decoded JSON, so a decoder that reports ``0``/``1`` on some uplinks and
    ``false``/``true`` on others alternates between ``TTNSensorValue`` and
    ``TTNBinarySensorValue``. Resolving the platform in one place keeps both
    platforms from each claiming the field on the uplink that suits them and
    leaving the user with a duplicate entity.

    An explicit ``platform`` in ``field_mappings.json`` always wins; otherwise
    the value type decides.
    """
    explicit = get_field_mapping(field_id).get("platform")
    if explicit in ("sensor", "binary_sensor"):
        return explicit

    if isinstance(ttn_value, TTNBinarySensorValue):
        return "binary_sensor"
    if isinstance(ttn_value, TTNSensorValue):
        return "sensor"
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
