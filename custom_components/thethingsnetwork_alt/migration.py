"""Update existing entity registry entries when field defaults change."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CONF_APP_ID, DOMAIN
from .field_defaults import merge_field_attr
from .metadata import get_device_name

_LOGGER = logging.getLogger(__name__)


def _ttn_device_id(
    device_registry: dr.DeviceRegistry,
    device_id: str | None,
    app_id: str,
) -> str | None:
    if not device_id or not (device := device_registry.async_get(device_id)):
        return None

    for item in device.identifiers:
        if item[0] == DOMAIN and item[1].startswith(f"{app_id}_"):
            return item[1][len(f"{app_id}_") :]
    return None


def _field_id_from_unique_id(unique_id: str | None, device_id: str) -> str | None:
    if not unique_id:
        return None
    prefix = f"{device_id}_"
    if unique_id.startswith(prefix):
        return unique_id[len(prefix) :]
    return None


async def update_registered_entity_metadata(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Apply field defaults and device names to entities already in the registry."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    app_id = entry.data[CONF_APP_ID]

    for entity_entry in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity_entry.domain != "sensor":
            continue

        device_id = _ttn_device_id(device_registry, entity_entry.device_id, app_id)
        if not device_id:
            continue

        field_id = _field_id_from_unique_id(entity_entry.unique_id, device_id)
        if not field_id:
            continue

        attr = merge_field_attr({}, field_id)
        updates: dict[str, str] = {}

        if device_class := attr.get("device_class"):
            if entity_entry.device_class != device_class:
                updates["device_class"] = device_class

        if unit := attr.get("unit"):
            if entity_entry.unit_of_measurement != unit:
                updates["unit_of_measurement"] = unit

        if state_class := attr.get("state_class"):
            if entity_entry.state_class != state_class:
                updates["state_class"] = state_class

        if entity_category := attr.get("entity_category"):
            if entity_entry.entity_category != entity_category:
                updates["entity_category"] = entity_category

        if friendly_name := attr.get("friendly_name"):
            if entity_entry.name != friendly_name:
                updates["name"] = friendly_name

        if updates:
            _LOGGER.debug(
                "Updating entity %s metadata: %s",
                entity_entry.entity_id,
                updates,
            )
            entity_registry.async_update_entity(entity_entry.entity_id, **updates)

        if device_name := get_device_name(device_id):
            if entity_entry.device_id and (
                device := device_registry.async_get(entity_entry.device_id)
            ):
                if device.name != device_name:
                    device_registry.async_update_device(
                        entity_entry.device_id, name=device_name
                    )
