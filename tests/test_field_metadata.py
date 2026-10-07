"""Tests for the one helper every entity type derives its metadata attributes through.

Before it existed the mapping was implemented four times and the copies disagreed:
the GPS and meta sensors only ever assigned truthy values, an invalid precision
warned on one entity type and was silently dropped on another, and a meta sensor
ignored ``suggested_display_precision`` altogether
(thethingsnetwork-ha-alt#8).
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from custom_components.thethingsnetwork_alt import mappings
from custom_components.thethingsnetwork_alt.helpers import apply_field_metadata
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from .conftest import TTNHarness, make_uplink

FULL = {
    "friendly_name": "Humidity",
    "unit": "%",
    "device_class": "humidity",
    "state_class": "measurement",
    "entity_category": "diagnostic",
    "suggested_display_precision": 1,
}
RX = [{"gateway_ids": {"gateway_id": "gw-1"}, "rssi": -70, "snr": 8.2}]


def applied(attr: dict, **kwargs: object) -> SensorEntity:
    """Return a bare sensor entity with ``attr`` applied to it."""
    entity = SensorEntity()
    apply_field_metadata(
        entity, attr, field_id="humidity", default_name="humidity", **kwargs
    )
    return entity


def test_every_attribute_is_set_from_metadata() -> None:
    """The baseline the other tests vary."""
    entity = applied(FULL)

    assert entity.name == "Humidity"
    assert entity.native_unit_of_measurement == "%"
    assert entity.device_class is SensorDeviceClass.HUMIDITY
    assert entity.state_class is SensorStateClass.MEASUREMENT
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert entity.suggested_display_precision == 1


def test_re_applied_metadata_leaves_nothing_stale_behind() -> None:
    """Runtime re-application must clear what the new metadata no longer says."""
    entity = applied(FULL)

    apply_field_metadata(entity, {}, field_id="humidity", default_name="humidity")

    assert entity.name == "humidity"
    assert entity.native_unit_of_measurement is None
    assert entity.device_class is None
    assert entity.state_class is None
    assert entity.entity_category is None
    assert entity.suggested_display_precision is None


def test_precision_zero_is_a_precision_not_unset() -> None:
    """``0`` means "whole numbers"; a falsy check would read it as absent."""
    assert applied({"suggested_display_precision": 0}).suggested_display_precision == 0


@pytest.mark.parametrize("precision", ["two", True, [1]])
def test_an_invalid_precision_is_reported_and_dropped(
    caplog: pytest.LogCaptureFixture, precision: object
) -> None:
    """The same bad input now behaves the same on every entity type."""
    with caplog.at_level(logging.WARNING):
        entity = applied({"suggested_display_precision": precision})

    assert entity.suggested_display_precision is None
    assert "invalid suggested_display_precision" in caplog.text


def test_an_unsupported_enum_value_is_reported_and_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sensor used to drop these silently; a binary sensor warned."""
    with caplog.at_level(logging.WARNING):
        entity = applied({"device_class": "sogginess", "state_class": "lumpy"})

    assert entity.device_class is None
    assert entity.state_class is None
    assert "unsupported device_class='sogginess'" in caplog.text
    assert "unsupported state_class='lumpy'" in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, EntityCategory.DIAGNOSTIC),
        ("config", EntityCategory.CONFIG),
        ("nonsense", EntityCategory.DIAGNOSTIC),
    ],
    ids=["absent", "explicit", "unsupported"],
)
def test_the_default_entity_category_applies_only_without_a_valid_one(
    raw: str | None, expected: EntityCategory
) -> None:
    """Meta sensors default to diagnostic; an explicit category still wins."""
    entity = applied(
        {"entity_category": raw}, default_entity_category=EntityCategory.DIAGNOSTIC
    )

    assert entity.entity_category is expected


def test_an_empty_friendly_name_falls_back_to_the_default() -> None:
    """A GPS axis with ``friendly_name: ""`` used to end up with no name."""
    assert applied({"friendly_name": ""}).name == "humidity"


def test_a_binary_sensor_gets_its_own_device_classes_and_no_sensor_attributes() -> None:
    """Units, state classes and precision mean nothing to a binary sensor."""
    entity = BinarySensorEntity()

    apply_field_metadata(
        entity, {**FULL, "device_class": "door"}, field_id="door", default_name="door"
    )

    assert entity.device_class is BinarySensorDeviceClass.DOOR
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert entity.name == "Humidity"
    assert not hasattr(entity, "_attr_native_unit_of_measurement")


async def test_a_meta_sensor_honours_display_precision(ttn: TTNHarness) -> None:
    """Consequence 1 of #8: the meta copy did not handle precision at all."""
    patched = dict(mappings._load_field_mappings())
    patched["_meta_snr"] = {**patched["_meta_snr"], "suggested_display_precision": 0}

    with patch.object(mappings, "_load_field_mappings", return_value=patched):
        await ttn.start(
            make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}, rx_metadata=RX)
        )

        entry = er.async_get(ttn.hass).async_get("sensor.dev_1_snr")
        assert entry is not None
        assert entry.options["sensor"]["suggested_display_precision"] == 0
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
