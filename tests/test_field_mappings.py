"""Validate the metadata this integration ships.

``field_mappings.json`` and the suffix heuristics decide what Home Assistant
is told about every field, and a value Home Assistant does not recognise is
only discovered at runtime — as a warning per field per startup, with the
entity quietly losing the attribute. These checks make a bad value fail here
instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.thethingsnetwork_alt.mappings import _SUFFIX_HEURISTICS
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory

MAPPINGS_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "thethingsnetwork_alt"
    / "field_mappings.json"
)

SENSOR_DEVICE_CLASSES = {item.value for item in SensorDeviceClass}
BINARY_DEVICE_CLASSES = {item.value for item in BinarySensorDeviceClass}
STATE_CLASSES = {item.value for item in SensorStateClass}
ENTITY_CATEGORIES = {item.value for item in EntityCategory}


def shipped_mappings() -> list[dict[str, Any]]:
    """Return the mapping entries as the integration reads them."""
    return json.loads(MAPPINGS_PATH.read_text(encoding="utf-8"))


def describe(entry: dict[str, Any]) -> str:
    """Return something that identifies an entry in a failure message."""
    return str(entry.get("keys") or entry.get("friendly_name") or entry)


@pytest.mark.parametrize("entry", shipped_mappings(), ids=describe)
def test_mapping_device_class_exists(entry: dict[str, Any]) -> None:
    """Every device class must exist for the platform it is used on.

    ``acceleration`` shipped for four fields and is not a Home Assistant
    sensor device class at all, so those sensors dropped their device class
    and logged a warning on every startup.
    """
    device_class = entry.get("device_class")
    if not device_class:
        return

    if entry.get("platform") == "binary_sensor":
        assert device_class in BINARY_DEVICE_CLASSES
    else:
        assert device_class in SENSOR_DEVICE_CLASSES


@pytest.mark.parametrize("entry", shipped_mappings(), ids=describe)
def test_mapping_state_and_category_exist(entry: dict[str, Any]) -> None:
    """State class and entity category must also be values HA knows."""
    if state_class := entry.get("state_class"):
        assert state_class in STATE_CLASSES
    if entity_category := entry.get("entity_category"):
        assert entity_category in ENTITY_CATEGORIES


def test_no_field_is_mapped_twice() -> None:
    """A key in two entries means one of them silently never applies."""
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for entry in shipped_mappings():
        for key in entry.get("keys", []):
            if key in seen:
                duplicates.append(f"{key} (in {seen[key]} and {describe(entry)})")
            seen[key] = describe(entry)

    assert not duplicates


def test_binary_sensor_mappings_are_declared_as_such() -> None:
    """A binary device class on the sensor platform would be dropped.

    The platform is what decides which enum the device class is read
    against, so a door or occupancy class needs the explicit platform.
    """
    for entry in shipped_mappings():
        device_class = entry.get("device_class")
        if device_class and device_class not in SENSOR_DEVICE_CLASSES:
            assert entry.get("platform") == "binary_sensor", describe(entry)


@pytest.mark.parametrize(
    ("suffix", "attr"), _SUFFIX_HEURISTICS, ids=[s for s, _ in _SUFFIX_HEURISTICS]
)
def test_suffix_heuristic_metadata_exists(suffix: str, attr: dict[str, str]) -> None:
    """The suffix heuristics apply to any field name, so they must be valid.

    These are guesses made from a field's name with no mapping behind them,
    which makes an invalid value here reachable by a field nobody configured.
    """
    if device_class := attr.get("device_class"):
        assert device_class in SENSOR_DEVICE_CLASSES, suffix
    if state_class := attr.get("state_class"):
        assert state_class in STATE_CLASSES, suffix
