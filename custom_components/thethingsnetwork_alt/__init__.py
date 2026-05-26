"""Support for The Things Network HA-Alt."""

from __future__ import annotations

import logging

from ttn_client import TTNSensorAttribute

from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant

from .const import PLATFORMS, TTN_API_HOST, _INTEGRATION_VERSION
from .coordinator import TTNConfigEntry, TTNCoordinator
from .exclusions import (
    is_excluded,
    load_exclusions_summary,
    reload_exclusions,
)
from .field_defaults import reload_field_mappings
from .mappings import _load_field_mappings, get_field_mapping
from .metadata import load_device_names
from .migration import update_registered_entity_metadata

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: TTNConfigEntry) -> bool:
    """Establish connection with The Things Network."""

    _LOGGER.debug(
        "Set up %s at %s",
        entry.data[CONF_API_KEY],
        entry.data.get(CONF_HOST, TTN_API_HOST),
    )

    reload_field_mappings()
    reload_exclusions()

    coordinator = TTNCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    _LOGGER.info(
        "The Things Network HA-Alt v%s loaded "
        "(%d field mappings, %d device names, %s)",
        _INTEGRATION_VERSION,
        len(_load_field_mappings()),
        len(load_device_names()),
        _exclusions_log_line(),
    )

    _log_field_discovery(coordinator.data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    try:
        await update_registered_entity_metadata(hass, entry)
    except Exception:
        _LOGGER.exception("TTN HA-Alt metadata migration failed")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: TTNConfigEntry) -> bool:
    """Unload a config entry."""

    _LOGGER.debug(
        "Remove %s at %s",
        entry.data[CONF_API_KEY],
        entry.data.get(CONF_HOST, TTN_API_HOST),
    )

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _exclusions_log_line() -> str:
    summary = load_exclusions_summary()
    parts: list[str] = []
    if summary["global_exact"] or summary["global_prefix"]:
        parts.append(
            f"global_exclusions={len(summary['global_exact']) + len(summary['global_prefix'])}"
        )
    if summary["devices"]:
        parts.append(f"per_device_exclusions={len(summary['devices'])}")
    if not parts:
        return "no exclusions"
    return ", ".join(parts)


def _log_field_discovery(data: dict | None) -> None:
    """Log one INFO line per device describing what TTN is sending."""
    if not data:
        _LOGGER.info(
            "TTN HA-Alt: no uplinks yet — nothing to enumerate "
            "(check Storage Integration + API key scope)"
        )
        return

    for device_id, fields in data.items():
        all_fields: list[str] = []
        for field_id, value in fields.items():
            if field_id.startswith("_") or isinstance(value, TTNSensorAttribute):
                continue
            all_fields.append(field_id)
        all_fields.sort()

        excluded = [f for f in all_fields if is_excluded(device_id, f)]
        included = [f for f in all_fields if f not in excluded]
        unmapped = [f for f in included if not get_field_mapping(f)]

        _LOGGER.info(
            "TTN HA-Alt device=%s fields=%s excluded=%s unmapped=%s",
            device_id,
            included or "[]",
            excluded or "[]",
            unmapped or "[]",
        )
