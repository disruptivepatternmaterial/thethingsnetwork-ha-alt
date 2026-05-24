"""Support for The Things Network HA-Alt."""

import logging

from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant

from .const import PLATFORMS, TTN_API_HOST, _INTEGRATION_VERSION
from .coordinator import TTNConfigEntry, TTNCoordinator
from .field_defaults import reload_field_mappings
from .mappings import _load_field_mappings
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

    coordinator = TTNCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    reload_field_mappings()
    _LOGGER.info(
        "The Things Network HA-Alt v%s loaded (%s field mappings, %s device names)",
        _INTEGRATION_VERSION,
        len(_load_field_mappings()),
        len(load_device_names()),
    )

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
