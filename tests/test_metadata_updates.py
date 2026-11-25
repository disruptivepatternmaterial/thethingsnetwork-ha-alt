"""End-to-end tests for metadata that arrives, or changes, after entity creation.

``test_live_updates.py`` covers the reading itself. This file covers everything
attached to it — unit, device class, display precision, name — plus the
registry rows that outlive a restart. Each test here maps to a way a live
entity kept a value it should have replaced, or lost one it should have kept.
"""

from __future__ import annotations

from unittest.mock import patch

from custom_components.thethingsnetwork_alt import mappings
from custom_components.thethingsnetwork_alt.const import DOMAIN
from custom_components.thethingsnetwork_alt.device_names import (
    device_names_forbidden_issue_id,
)
from custom_components.thethingsnetwork_alt.registry import (
    TTNDeviceRegistryError,
    TTNDeviceRegistryForbidden,
)
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.setup import async_setup_component

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
        "door_status",
        {**mappings.get_field_mapping("door_status"), "platform": "sensor"},
    )

    with patch.object(mappings, "_load_field_mappings", return_value=moved):
        await ttn.reload(make_uplink("2026-09-01T00:05:00Z", {"door_status": "close"}))

    assert ttn.state(EXTI) is None
    assert er.async_get(ttn.hass).async_get(EXTI) is None


# --- TTN end-device names and live renames ----------------------------------


async def test_ttn_device_name_applied_on_setup(ttn: TTNHarness) -> None:
    """A name from TTN is applied to a newly discovered device at setup."""
    ttn.names = {"dev-1": "East Gate Sensor"}

    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    device = ttn.device_entry()
    assert device is not None
    assert device.name == "East Gate Sensor"
    entity_id = ttn.entity_id_for("dev-1_temperature")
    assert entity_id == "sensor.east_gate_sensor_temperature"
    assert ttn.attribute(entity_id, "friendly_name") == "East Gate Sensor Temperature"


async def test_ttn_device_rename_applied_on_hourly_refresh(
    ttn: TTNHarness,
) -> None:
    """A rename in TTN reaches an existing HA device on the hourly refresh."""
    ttn.names = {"dev-1": "East Gate Sensor"}
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))
    entity_id = ttn.entity_id_for("dev-1_temperature")
    assert entity_id is not None

    ttn.names = {"dev-1": "West Gate Sensor"}
    await ttn.poll_names()

    device = ttn.device_entry()
    assert device is not None
    assert device.name == "West Gate Sensor"
    assert ttn.entity_id_for("dev-1_temperature") == entity_id
    assert ttn.attribute(entity_id, "friendly_name") == "West Gate Sensor Temperature"


async def test_update_entity_action_refreshes_device_names(
    ttn: TTNHarness,
) -> None:
    """Calling homeassistant.update_entity also refreshes TTN device names."""
    assert await async_setup_component(ttn.hass, "homeassistant", {})
    ttn.names = {"dev-1": "Initial Name"}
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))
    entity_id = ttn.entity_id_for("dev-1_temperature")
    assert entity_id is not None

    ttn.names = {"dev-1": "Updated Via Action"}
    await ttn.hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": entity_id},
        blocking=True,
    )
    await ttn.hass.async_block_till_done()

    device = ttn.device_entry()
    assert device is not None
    assert device.name == "Updated Via Action"


async def test_disabled_polling_suppresses_hourly_name_refresh(
    ttn: TTNHarness,
) -> None:
    """System options 'Enable polling for changes' stops the hourly name poll."""
    ttn.add_entry()
    ttn.hass.config_entries.async_update_entry(ttn.entry, pref_disable_polling=True)
    ttn.names = {"dev-1": "Initial Name"}

    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))
    assert ttn.names_fetch_count == 1

    ttn.names = {"dev-1": "Should Not Poll"}
    await ttn.poll_names()

    assert ttn.names_fetch_count == 1
    device = ttn.device_entry()
    assert device is not None
    assert device.name == "Initial Name"


async def test_cleared_ttn_device_name_reverts_to_device_id(
    ttn: TTNHarness,
) -> None:
    """Clearing a device's name in TTN resets its HA device name to the TTN ID."""
    ttn.names = {"dev-1": "Temporary Name"}
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))
    assert ttn.device_entry().name == "Temporary Name"

    ttn.names = {}
    await ttn.poll_names()

    assert ttn.device_entry().name == "dev-1"


async def test_user_device_name_override_survives_ttn_rename(
    ttn: TTNHarness,
) -> None:
    """name_by_user set in HA always wins over TTN device renames."""
    ttn.names = {"dev-1": "TTN Name"}
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))
    entity_id = ttn.entity_id_for("dev-1_temperature")
    assert entity_id is not None

    device = ttn.device_entry()
    assert device is not None
    dr.async_get(ttn.hass).async_update_device(device.id, name_by_user="My Greenhouse")

    ttn.names = {"dev-1": "Renamed In TTN"}
    await ttn.poll_names()

    updated = ttn.device_entry()
    assert updated is not None
    assert updated.name_by_user == "My Greenhouse"
    assert updated.name == "Renamed In TTN"
    assert ttn.attribute(entity_id, "friendly_name") == "My Greenhouse Temperature"


async def test_existing_device_name_survives_failed_initial_fetch(
    ttn: TTNHarness,
) -> None:
    """Upgrade/restart with an unreachable Identity Server keeps existing names."""
    ttn.preregister("sensor", "dev-1_temperature", device_name="Old Name")
    ttn.names_error = TTNDeviceRegistryError("unreachable")

    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    device = ttn.device_entry()
    assert device is not None
    assert device.name == "Old Name"


async def test_forbidden_creates_repair_issue_and_keeps_names_until_resolved(
    ttn: TTNHarness,
) -> None:
    """HTTP 403 keeps existing names and raises a Repairs issue until fixed."""
    ttn.preregister("sensor", "dev-1_temperature", device_name="Kept Name")
    ttn.names_error = TTNDeviceRegistryForbidden("missing right")

    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    issue_id = device_names_forbidden_issue_id(ttn.entry.entry_id)
    assert ttn.device_entry().name == "Kept Name"
    assert ir.async_get(ttn.hass).async_get_issue(DOMAIN, issue_id) is not None

    ttn.names_error = None
    ttn.names = {"dev-1": "Fresh Name"}
    await ttn.poll_names()

    assert ir.async_get(ttn.hass).async_get_issue(DOMAIN, issue_id) is None
    assert ttn.device_entry().name == "Fresh Name"


async def test_failed_name_fetch_leaves_sensors_available_and_skips_reauth(
    ttn: TTNHarness,
) -> None:
    """A name-sync failure never marks sensors unavailable or triggers reauth."""
    ttn.names_error = TTNDeviceRegistryForbidden("missing right")
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    assert ttn.state("sensor.dev_1_temperature") == "20.5"
    assert not ttn.hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    ttn.names_error = TTNDeviceRegistryError("503")
    await ttn.poll_names()

    assert ttn.state("sensor.dev_1_temperature") == "20.5"
    assert not ttn.hass.config_entries.flow.async_progress_by_handler(DOMAIN)


# --- Upgrading from the pre-0.7.4 GPS axis naming ---------------------------

GPS_UPLINK = make_uplink(
    "2026-09-01T00:00:00Z",
    {"gps": {"latitude": 47.6, "longitude": -122.3, "altitude": 30.0}},
)


async def test_existing_gps_axis_keeps_its_entity_across_the_rename(
    ttn: TTNHarness,
) -> None:
    """Renaming the unique_id must not orphan the entity behind it.

    Installs upgrading from <=0.7.3 already have ``<device>_gps_latitude``
    rows carrying the user's customisations and their recorder history. The
    migration renames those in place; without it the platform would register
    the new unique_id as a second entity and leave the original stranded.
    """
    original = ttn.preregister(
        "sensor", "dev-1_gps_latitude", suggested_object_id="dev_1_latitude"
    )

    await ttn.start(GPS_UPLINK)

    assert ttn.entity_id_for("dev-1__gps_gps_latitude") == original
    assert ttn.entity_id_for("dev-1_gps_latitude") is None
    assert ttn.state(original) == "47.6"


async def test_migration_does_not_touch_a_plain_field_named_like_an_axis(
    ttn: TTNHarness,
) -> None:
    """A device with no GPS object keeps its ordinary gps_latitude sensor."""
    original = ttn.preregister("sensor", "dev-1_gps_latitude")

    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"gps_latitude": 11.11}))

    assert ttn.entity_id_for("dev-1_gps_latitude") == original
    assert ttn.entity_id_for("dev-1__gps_gps_latitude") is None


async def test_migration_leaves_both_entities_when_the_device_sends_both(
    ttn: TTNHarness,
) -> None:
    """The axis takes the migrated row; the flat field gets a fresh one."""
    original = ttn.preregister(
        "sensor", "dev-1_gps_latitude", suggested_object_id="dev_1_latitude"
    )

    await ttn.start(
        make_uplink(
            "2026-09-01T00:00:00Z",
            {
                "gps": {"latitude": 47.6, "longitude": -122.3},
                "gps_latitude": 11.11,
            },
        )
    )

    assert ttn.entity_id_for("dev-1__gps_gps_latitude") == original
    assert ttn.state(original) == "47.6"

    flat = ttn.entity_id_for("dev-1_gps_latitude")
    assert flat is not None and flat != original
    assert ttn.state(flat) == "11.11"
