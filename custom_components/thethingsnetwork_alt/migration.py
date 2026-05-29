"""Update existing entity and device registry entries when defaults change."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CONF_APP_ID, DOMAIN, _INTEGRATION_VERSION
from .field_defaults import (
    get_field_platform,
    merge_field_attr,
    reload_field_mappings,
)
from .mappings import _load_field_mappings
from .metadata import get_device_name, load_device_names

_LOGGER = logging.getLogger(__name__)

_VALID_SENSOR_DEVICE_CLASSES = frozenset(item.value for item in SensorDeviceClass)
_VALID_BINARY_DEVICE_CLASSES = frozenset(
    item.value for item in BinarySensorDeviceClass
)
_VALID_ENTITY_CATEGORIES = frozenset(item.value for item in EntityCategory)


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


def _update_registered_device_names(
    device_registry: dr.DeviceRegistry,
    entry: ConfigEntry,
) -> None:
    """Rename TTN devices using device_names.json."""
    app_id = entry.data[CONF_APP_ID]

    for device in device_registry.devices.values():
        if entry.entry_id not in device.config_entries:
            continue

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


async def update_registered_entity_metadata(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Apply field defaults and device names to existing registry entries."""
    reload_field_mappings()

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
            if not entity_entry.device_id or not (
                device := device_registry.async_get(entity_entry.device_id)
            ):
                continue

            ttn_device_id: str | None = None
            for domain, identifier in device.identifiers:
                if domain != DOMAIN:
                    continue
                ttn_device_id = _ttn_device_id_from_identifier(identifier, app_id)
                if ttn_device_id:
                    break

            if not ttn_device_id:
                continue

            field_id = _field_id_from_unique_id(entity_entry.unique_id, ttn_device_id)
            if not field_id:
                continue

            attr = merge_field_attr({}, field_id)
            mapped_platform = get_field_platform(field_id)

            if entity_entry.domain == "sensor" and mapped_platform == "binary_sensor":
                _LOGGER.warning(
                    "Removing stale sensor %s (%s is mapped to binary_sensor); "
                    "it will be recreated on the next uplink",
                    entity_entry.entity_id,
                    field_id,
                )
                entity_registry.async_remove(entity_entry.entity_id)
                continue
            if entity_entry.domain == "binary_sensor" and mapped_platform != "binary_sensor":
                continue

            updates: dict[str, Any] = {}

            if friendly_name := attr.get("friendly_name"):
                if entity_entry.name != friendly_name:
                    updates["name"] = friendly_name

            if (raw := attr.get("entity_category")) and raw in _VALID_ENTITY_CATEGORIES:
                # HA's async_update_entity requires the EntityCategory enum, not a raw string.
                category = EntityCategory(raw)
                if entity_entry.entity_category != category:
                    updates["entity_category"] = category

            if device_class := attr.get("device_class"):
                if entity_entry.domain == "sensor":
                    if (
                        device_class in _VALID_SENSOR_DEVICE_CLASSES
                        and entity_entry.device_class != device_class
                    ):
                        updates["device_class"] = device_class
                elif (
                    device_class in _VALID_BINARY_DEVICE_CLASSES
                    and entity_entry.device_class != device_class
                ):
                    updates["device_class"] = device_class

            if entity_entry.domain == "sensor":
                if unit := attr.get("unit"):
                    if entity_entry.unit_of_measurement != unit:
                        updates["unit_of_measurement"] = unit

            # NOTE: ``state_class`` is intentionally not migrated here.
            # It is not a column on the entity registry (and is rejected by
            # ``async_update_entity``); it lives under read-only
            # ``capabilities`` populated from the entity's own
            # ``_attr_state_class``. ``TtnDataSensor`` already applies the
            # mapped ``state_class`` natively on every load, so HA refreshes
            # the stored capabilities automatically without a registry write.

            if updates:
                _LOGGER.info(
                    "Updated entity %s: %s",
                    entity_entry.entity_id,
                    updates,
                )
                entity_registry.async_update_entity(
                    entity_entry.entity_id, **updates
                )
                applied += 1
        except Exception:
            _LOGGER.exception(
                "Failed to migrate entity metadata for %s",
                entity_entry.entity_id,
            )

    _LOGGER.info("Migration applied to %d/%d entities", applied, total)
