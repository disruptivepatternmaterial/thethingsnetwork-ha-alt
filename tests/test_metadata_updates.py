"""End-to-end tests for metadata that arrives, or changes, after entity creation.

``test_live_updates.py`` covers the reading itself. This file covers everything
attached to it — unit, device class, display precision, name — plus the
registry rows that outlive a restart. Each test here maps to a way a live
entity kept a value it should have replaced, or lost one it should have kept.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.helpers import entity_registry as er

from custom_components.thethingsnetwork_alt import mappings, metadata

from .conftest import TTNHarness, make_uplink

EXTI = "binary_sensor.dev_1_external_input_exti"


def _mappings_with(field_id: str, mapping: dict) -> dict:
    """Return the real mappings with one entry replaced."""
    patched = dict(mappings._load_field_mappings())
    patched[field_id] = mapping
    return patched


# --- Decoder _sensor_attr that does not ride the uplink carrying the field ---


async def test_decoder_metadata_applies_without_the_field_present(
    ttn: TTNHarness,
) -> None:
    """``_sensor_attr`` is not obliged to share a window with its own field.

    Metadata was read from the current window only, so a decoder that sends
    ``_sensor_attr`` on a different cadence than the measurement left the
    entity permanently without a unit.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"tank_level": 42}))
    assert ttn.attribute("sensor.dev_1_tank_level", "unit_of_measurement") is None

    await ttn.poll(
        make_uplink(
            "2026-09-01T00:01:00Z",
            {"other": 1, "_sensor_attr": {"tank_level": {"unit": "%"}}},
        )
    )

    assert ttn.attribute("sensor.dev_1_tank_level", "unit_of_measurement") == "%"


async def test_decoder_metadata_seen_before_the_field_is_kept(
    ttn: TTNHarness,
) -> None:
    """The reverse order: metadata first, then the field it describes."""
    await ttn.start(
        make_uplink("2026-09-01T00:00:00Z", {"_sensor_attr": {"x": {"unit": "%"}}})
    )

    await ttn.poll(make_uplink("2026-09-01T00:01:00Z", {"x": 5}))

    assert ttn.state("sensor.dev_1_x") == "5"
    assert ttn.attribute("sensor.dev_1_x", "unit_of_measurement") == "%"


async def test_binary_sensor_applies_late_decoder_metadata(
    ttn: TTNHarness,
) -> None:
    """Binary sensors had no path for metadata arriving after creation at all.

    The mapped name must survive the re-apply: the second set of metadata is
    merged over the file mapping, not substituted for it.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"door_status": "open"}))
    assert ttn.attribute(EXTI, "device_class") == "opening"

    await ttn.poll(
        make_uplink(
            "2026-09-01T00:01:00Z",
            {
                "door_status": "close",
                "_sensor_attr": {"door_status": {"device_class": "door"}},
            },
        )
    )

    assert ttn.state(EXTI) == "off"
    assert ttn.attribute(EXTI, "device_class") == "door"
    assert ttn.attribute(EXTI, "friendly_name") == "dev-1 External input (EXTI)"


# --- Numeric metadata vs. what the field actually reports --------------------


async def test_field_that_starts_text_regains_its_metadata(
    ttn: TTNHarness,
) -> None:
    """A mapped measurement whose first reading is an error string.

    Numeric metadata is suppressed while a field has only ever reported text,
    otherwise Home Assistant refuses the state outright. That decision was
    taken once, at creation, so a sensor whose very first uplink happened to
    carry an error string spent the rest of the run with no unit, no device
    class and no statistics.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": "ERR"}))

    assert ttn.state("sensor.dev_1_temperature") == "ERR"
    assert ttn.attribute("sensor.dev_1_temperature", "unit_of_measurement") is None

    await ttn.poll(make_uplink("2026-09-01T00:01:00Z", {"temperature": 22.5}))

    assert ttn.state("sensor.dev_1_temperature") == "22.5"
    assert ttn.attribute("sensor.dev_1_temperature", "unit_of_measurement") == "°C"
    assert ttn.attribute("sensor.dev_1_temperature", "state_class") == "measurement"


async def test_metadata_is_not_torn_off_again_by_a_later_text_reading(
    ttn: TTNHarness,
) -> None:
    """The first number settles it: the field is a measurement from then on.

    Flipping the unit off and back on per reading would keep resetting the
    long-term statistics of a sensor that only occasionally fails to read.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": "ERR"}))
    await ttn.poll(make_uplink("2026-09-01T00:01:00Z", {"temperature": 22.5}))

    await ttn.poll(make_uplink("2026-09-01T00:02:00Z", {"temperature": "ERR"}))

    assert ttn.state("sensor.dev_1_temperature") == "unknown"
    assert ttn.attribute("sensor.dev_1_temperature", "unit_of_measurement") == "°C"

    await ttn.poll(make_uplink("2026-09-01T00:03:00Z", {"temperature": 23.5}))

    assert ttn.state("sensor.dev_1_temperature") == "23.5"


async def test_display_precision_zero_reaches_the_entity(ttn: TTNHarness) -> None:
    """``suggested_display_precision: 0`` means whole numbers, not "unset"."""
    patched = _mappings_with(
        "widgets",
        {
            "friendly_name": "Widgets",
            "state_class": "measurement",
            "suggested_display_precision": 0,
        },
    )

    with patch.object(mappings, "_load_field_mappings", return_value=patched):
        await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"widgets": 12.7}))

        entry = er.async_get(ttn.hass).async_get("sensor.dev_1_widgets")
        assert entry.options["sensor"]["suggested_display_precision"] == 0


# --- Registry rows across a restart -----------------------------------------


async def test_user_rename_survives_a_restart(ttn: TTNHarness) -> None:
    """``name`` is the user's slot; the integration's name is ``original_name``.

    Writing the mapped name into ``name`` overwrote whatever the user had
    renamed the entity to, on every single restart.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    registry = er.async_get(ttn.hass)
    registry.async_update_entity("sensor.dev_1_temperature", name="My Greenhouse")

    await ttn.reload(make_uplink("2026-09-01T00:05:00Z", {"temperature": 21.5}))

    assert registry.async_get("sensor.dev_1_temperature").name == "My Greenhouse"


async def test_user_device_class_override_survives_a_restart(
    ttn: TTNHarness,
) -> None:
    """``device_class`` is the same kind of slot as ``name``, and was clobbered."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    registry = er.async_get(ttn.hass)
    registry.async_update_entity("sensor.dev_1_temperature", device_class="pressure")

    await ttn.reload(make_uplink("2026-09-01T00:05:00Z", {"temperature": 21.5}))

    entry = registry.async_get("sensor.dev_1_temperature")
    assert entry.device_class == "pressure"
    assert ttn.attribute("sensor.dev_1_temperature", "device_class") == "pressure"


async def test_edited_mapping_reaches_an_existing_entity(ttn: TTNHarness) -> None:
    """Editing field_mappings.json must still take effect without a registry write.

    This is what makes writing ``name`` and ``device_class`` unnecessary: the
    platform refreshes ``original_name``, ``original_device_class``, the unit
    and the entity category from the entity on every startup.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    patched = _mappings_with(
        "temperature",
        {
            **mappings.get_field_mapping("temperature"),
            "friendly_name": "Air Temp",
            "device_class": "pressure",
            "unit": "hPa",
            "entity_category": "diagnostic",
        },
    )

    with patch.object(mappings, "_load_field_mappings", return_value=patched):
        await ttn.reload(make_uplink("2026-09-01T00:05:00Z", {"temperature": 21.5}))

    entry = er.async_get(ttn.hass).async_get("sensor.dev_1_temperature")
    assert entry.original_name == "Air Temp"
    assert entry.original_device_class == "pressure"
    assert entry.unit_of_measurement == "hPa"
    assert entry.entity_category.value == "diagnostic"
    assert entry.name is None
    assert entry.device_class is None
    assert ttn.attribute("sensor.dev_1_temperature", "friendly_name") == (
        "dev-1 Air Temp"
    )
    assert ttn.attribute("sensor.dev_1_temperature", "device_class") == "pressure"


async def test_edited_mapping_reaches_an_existing_binary_sensor(
    ttn: TTNHarness,
) -> None:
    """The binary_sensor platform is refreshed the same way."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"door_status": "open"}))

    patched = _mappings_with(
        "door_status",
        {**mappings.get_field_mapping("door_status"), "device_class": "door"},
    )

    with patch.object(mappings, "_load_field_mappings", return_value=patched):
        await ttn.reload(make_uplink("2026-09-01T00:05:00Z", {"door_status": "close"}))

    assert er.async_get(ttn.hass).async_get(EXTI).original_device_class == "door"
    assert ttn.attribute(EXTI, "device_class") == "door"


async def test_entity_moved_to_the_other_platform_is_not_stranded(
    ttn: TTNHarness,
) -> None:
    """Mapping a binary field to ``sensor`` must remove the binary sensor.

    The sensor→binary_sensor direction was cleaned up and the reverse was not,
    so the old entity stayed in the registry showing the reading it happened
    to hold when the mapping changed.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"door_status": "open"}))
    assert ttn.state(EXTI) == "on"

    moved = _mappings_with(
        "door_status", {**mappings.get_field_mapping("door_status"), "platform": "sensor"}
    )

    with patch.object(mappings, "_load_field_mappings", return_value=moved):
        await ttn.reload(make_uplink("2026-09-01T00:05:00Z", {"door_status": "close"}))

    assert ttn.state(EXTI) is None
    assert er.async_get(ttn.hass).async_get(EXTI) is None


async def test_device_names_are_re_read_on_setup(ttn: TTNHarness) -> None:
    """device_names.json was cached for the life of the process.

    Every other JSON file the integration reads is re-read when the config
    entry is set up, so renaming a device appeared to do nothing until Home
    Assistant itself was restarted.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    metadata._DEVICE_NAMES = {"dev-1": "Stale Cached Name"}

    await ttn.reload(make_uplink("2026-09-01T00:05:00Z", {"temperature": 21.5}))

    assert metadata.get_device_name("dev-1") != "Stale Cached Name"
