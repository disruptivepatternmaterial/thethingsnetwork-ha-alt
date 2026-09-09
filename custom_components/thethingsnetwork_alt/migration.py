"""Update existing entity and device registry entries when defaults change."""

from __future__ import annotations

import logging
from typing import Any

from ttn_client import TTNDeviceTrackerValue

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    CONF_APP_ID,
    DOMAIN,
    GPS_COMPONENTS,
    SYNTHETIC_PREFIX,
    gps_component_field_id,
    legacy_gps_component_field_id,
)
from .coordinator import TTNConfigEntry
from .field_defaults import get_field_mapping, get_field_platform, merge_field_attr
from .metadata import get_device_name

_LOGGER = logging.getLogger(__name__)


def _ttn_device_id_from_identifier(identifier: str, app_id: str) -> str | None:
    prefix = f"{app_id}_"
    if identifier.startswith(prefix):
        return identifier[len(prefix) :]
    return None


def _field_id_from_unique_id(unique_id: str | None, device_id: str) -> str | None:
    if not unique_id:
        return None
    prefix = f"{device_id}_"
    if unique_id.startswith(prefix):
        return unique_id[len(prefix) :]
    return None


def _ttn_device_id_for_entity(
    entity_entry: er.RegistryEntry,
    device_registry: dr.DeviceRegistry,
    app_id: str,
) -> str | None:
    """Return the TTN device id behind a registry entry, if it has one."""
    if not entity_entry.device_id or not (
        device := device_registry.async_get(entity_entry.device_id)
    ):
        return None

    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        if ttn_device_id := _ttn_device_id_from_identifier(identifier, app_id):
            return ttn_device_id
    return None


def seed_field_platforms(hass: HomeAssistant, entry: TTNConfigEntry) -> None:
    """Give each already-registered field back to the platform that owns it.

    ``TTNCoordinator.claim_field`` keeps one field from becoming both a sensor
    and a binary sensor, but on its own that only holds within a single run:
    after a restart the first uplink's value type would decide again, and a
    decoder that alternates between ``0``/``1`` and ``false``/``true`` could
    hand the field to the other platform and strand the existing entity.
    Seeding from the registry makes the original choice stick.

    Fields with an explicit ``platform`` in ``field_mappings.json`` are left
    alone: that mapping is authoritative and is allowed to move a field, which
    is what ``update_registered_entity_metadata`` cleans up after.
    """
    coordinator = entry.runtime_data
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    app_id = entry.data[CONF_APP_ID]

    for entity_entry in er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    ):
        if entity_entry.domain not in ("sensor", "binary_sensor"):
            continue

        ttn_device_id = _ttn_device_id_for_entity(entity_entry, device_registry, app_id)
        if not ttn_device_id:
            continue

        field_id = _field_id_from_unique_id(entity_entry.unique_id, ttn_device_id)
        if not field_id or field_id.startswith("_"):
            continue

        if get_field_mapping(field_id).get("platform"):
            continue

        coordinator.seed_field_platform(ttn_device_id, field_id, entity_entry.domain)


def migrate_gps_component_unique_ids(
    hass: HomeAssistant, entry: TTNConfigEntry
) -> None:
    """Move GPS axis sensors into the reserved synthetic namespace.

    Before 0.7.4 an axis of a decoded GPS object was registered as
    ``f"{device}_{parent}_{component}"`` — indistinguishable from an ordinary
    decoded field of the same name. Renaming the unique_id here rather than
    letting the platform register the new one keeps the entity_id, the user's
    customisations and the recorder history attached to the axis.

    Only devices that are *currently* decoding a GPS object are considered, so
    an ordinary ``gps_latitude`` sensor on a device with no GPS object is left
    alone. On a device that sends both, the registered entity can only ever
    have been the axis — that is the bug being fixed, the axis reserved the
    name and the flat field was dropped — so it migrates and the flat field
    gets its own entity on the next poll.
    """
    coordinator = entry.runtime_data
    data = coordinator.data or {}

    gps_parents: dict[str, set[str]] = {}
    for device_id, fields in data.items():
        for field_id, value in fields.items():
            if isinstance(value, TTNDeviceTrackerValue):
                gps_parents.setdefault(str(device_id), set()).add(str(field_id))

    if not gps_parents:
        return

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    app_id = entry.data[CONF_APP_ID]

    for entity_entry in list(
        er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    ):
        if entity_entry.domain != "sensor":
            continue

        ttn_device_id = _ttn_device_id_for_entity(entity_entry, device_registry, app_id)
        if not ttn_device_id or not (parents := gps_parents.get(ttn_device_id)):
            continue

        field_id = _field_id_from_unique_id(entity_entry.unique_id, ttn_device_id)
        if not field_id or field_id.startswith(SYNTHETIC_PREFIX):
            continue

        match = next(
            (
                (parent, component)
                for parent in parents
                for component in GPS_COMPONENTS
                if field_id == legacy_gps_component_field_id(parent, component)
            ),
            None,
        )
        if match is None:
            continue

        new_unique_id = f"{ttn_device_id}_{gps_component_field_id(*match)}"
        if entity_registry.async_get_entity_id("sensor", DOMAIN, new_unique_id):
            _LOGGER.warning(
                "Not migrating %s to unique_id %s: already registered",
                entity_entry.entity_id,
                new_unique_id,
            )
            continue

        entity_registry.async_update_entity(
            entity_entry.entity_id, new_unique_id=new_unique_id
        )
        _LOGGER.info(
            "Migrated GPS axis %s to unique_id %s",
            entity_entry.entity_id,
            new_unique_id,
        )


def _update_registered_device_names(
    device_registry: dr.DeviceRegistry,
    entry: ConfigEntry,
) -> None:
    """Rename TTN devices using device_names.json."""
    app_id = entry.data[CONF_APP_ID]

    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        ttn_device_id: str | None = None
        for domain, identifier in device.identifiers:
            if domain != DOMAIN:
                continue
            ttn_device_id = _ttn_device_id_from_identifier(identifier, app_id)
            if ttn_device_id:
                break

        if not ttn_device_id:
            continue

        friendly_name = get_device_name(ttn_device_id)
        if not friendly_name or device.name == friendly_name:
            continue

        device_registry.async_update_device(device.id, name=friendly_name)
        _LOGGER.info(
            "Renamed TTN device %s to %s",
            ttn_device_id,
            friendly_name,
        )


def _remove_stale_entity(
    entity_registry: er.EntityRegistry,
    entity_entry: er.RegistryEntry,
    field_id: str,
    mapped_platform: str,
) -> None:
    """Drop an entity the mapping has moved to the other platform.

    Left in place it would sit at whatever reading it last took, for as long
    as the field keeps uplinking into its replacement.
    """
    _LOGGER.warning(
        "Removing stale %s %s (%s is mapped to %s); it will be recreated on "
        "the next uplink",
        entity_entry.domain,
        entity_entry.entity_id,
        field_id,
        mapped_platform,
    )
    entity_registry.async_remove(entity_entry.entity_id)


async def update_registered_entity_metadata(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reconcile the registry rows the entities themselves cannot reach.

    Home Assistant re-reads ``original_name``, ``original_device_class``,
    ``entity_category``, ``unit_of_measurement`` and ``capabilities`` off the
    entity every time it is added, so an edit to ``field_mappings.json``
    reaches entities that already exist without any registry write at all.

    ``name`` and ``device_class`` are the exception, and not in a useful way:
    they are the columns a user's own customisation lives in, which is exactly
    why Home Assistant never overwrites them. Earlier versions wrote the
    mapped values there, which shadowed the mapping from that point on and
    overwrote the user's choice on every restart. Undoing that is what is left
    here, alongside the two things no entity can do for itself: renaming
    devices, and removing an entity whose field has been mapped to the other
    platform.

    The mapping/device-name caches are reloaded and primed off-loop by
    ``async_setup_entry`` before this runs, so no file reads happen here.
    """
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    _update_registered_device_names(device_registry, entry)

    app_id = entry.data[CONF_APP_ID]

    total = 0
    applied = 0

    for entity_entry in er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    ):
        if entity_entry.domain not in ("sensor", "binary_sensor"):
            continue

        total += 1

        try:
            ttn_device_id = _ttn_device_id_for_entity(
                entity_entry, device_registry, app_id
            )
            if not ttn_device_id:
                continue

            field_id = _field_id_from_unique_id(entity_entry.unique_id, ttn_device_id)
            if not field_id:
                continue

            attr = merge_field_attr({}, field_id)
            mapped_platform = get_field_platform(field_id)
            explicit_platform = get_field_mapping(field_id).get("platform")

            if entity_entry.domain == "sensor" and mapped_platform == "binary_sensor":
                _remove_stale_entity(
                    entity_registry, entity_entry, field_id, "binary_sensor"
                )
                continue

            if entity_entry.domain == "binary_sensor":
                if explicit_platform == "sensor":
                    # Only an explicit mapping may move a field this way. The
                    # default from ``get_field_platform`` is "sensor", so
                    # reading ``mapped_platform`` here instead would delete
                    # every binary sensor that was assigned by value type
                    # rather than by the mapping file.
                    _remove_stale_entity(
                        entity_registry, entity_entry, field_id, "sensor"
                    )
                    continue
                if mapped_platform != "binary_sensor":
                    # Assigned by value type, so the mapping describes nothing
                    # about it and has no metadata to migrate.
                    continue

            # Clear a customisation only where it still holds exactly what
            # this integration used to write there — never a value the user
            # chose. Once cleared, the mapped value shows through from
            # ``original_name`` / ``original_device_class`` as it should.
            updates: dict[str, Any] = {
                column: None
                for column, mapped in (
                    ("name", attr.get("friendly_name")),
                    ("device_class", attr.get("device_class")),
                )
                if getattr(entity_entry, column) is not None
                and getattr(entity_entry, column) == mapped
            }

            if updates:
                _LOGGER.info(
                    "Released %s back to field_mappings.json: cleared %s",
                    entity_entry.entity_id,
                    ", ".join(sorted(updates)),
                )
                entity_registry.async_update_entity(entity_entry.entity_id, **updates)
                applied += 1
        except Exception:
            _LOGGER.exception(
                "Failed to migrate entity metadata for %s",
                entity_entry.entity_id,
            )

    _LOGGER.info("Migration applied to %d/%d entities", applied, total)
