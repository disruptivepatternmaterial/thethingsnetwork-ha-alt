"""Periodic TTN end-device name synchronization and live HA device renaming."""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_APP_ID, DEVICE_NAME_REFRESH_INTERVAL, DOMAIN
from .registry import (
    TTNDeviceRegistryClient,
    TTNDeviceRegistryError,
    TTNDeviceRegistryForbidden,
)

_LOGGER = logging.getLogger(__name__)

_FORBIDDEN_TRANSLATION_KEY: Final = "device_names_forbidden"


def device_names_forbidden_issue_id(entry_id: str) -> str:
    """Return the Repairs issue ID for a 403 from the device registry."""
    return f"{_FORBIDDEN_TRANSLATION_KEY}_{entry_id}"


def ttn_device_id_for_device(device: dr.DeviceEntry, app_id: str) -> str | None:
    """Return the TTN end-device ID carried by ``device``, if any."""
    prefix = f"{app_id}_"
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier.startswith(prefix):
            if ttn_device_id := identifier[len(prefix) :]:
                return ttn_device_id
    return None


def _seed_names_from_registry(
    hass: HomeAssistant, entry_id: str, app_id: str
) -> dict[str, str]:
    """Read current integration-supplied names off HA's device registry.

    Seeding before the first TTN fetch ensures a transient Identity Server
    outage or a missing API-key permission on startup leaves existing devices
    with the names they already carry instead of resetting them to raw IDs.
    """
    seeded: dict[str, str] = {}
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry_id):
        ttn_device_id = ttn_device_id_for_device(device, app_id)
        if ttn_device_id and device.name and device.name != ttn_device_id:
            seeded[ttn_device_id] = device.name
    return seeded


class TTNDeviceNameCoordinator(DataUpdateCoordinator[dict[str, str]]):
    """Fetch end-device names from TTN and keep HA device names aligned."""

    config_entry: ConfigEntry[Any]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        """Initialize the device-name coordinator."""
        app_id = str(entry.data[CONF_APP_ID])
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"TheThingsNetworkAltNames_{app_id}",
            update_interval=DEVICE_NAME_REFRESH_INTERVAL,
            always_update=True,
        )
        self._app_id = app_id
        self._issue_id = device_names_forbidden_issue_id(entry.entry_id)
        self._client = TTNDeviceRegistryClient(
            hass,
            entry.data[CONF_HOST],
            app_id,
            entry.data[CONF_API_KEY],
        )
        self.data = _seed_names_from_registry(hass, entry.entry_id, app_id)

    def name_for(self, device_id: str) -> str | None:
        """Return the friendly name known for ``device_id``, if any."""
        return (self.data or {}).get(device_id)

    async def _async_update_data(self) -> dict[str, str]:
        """Fetch the application's current device-name map from TTN."""
        try:
            names = await self._client.fetch_names()
        except TTNDeviceRegistryForbidden as err:
            # A traffic-only key still reads uplinks, so do not trigger reauth;
            # surface a Repairs notice explaining the missing permission.
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=_FORBIDDEN_TRANSLATION_KEY,
                translation_placeholders={"app_id": self._app_id},
            )
            raise UpdateFailed(str(err)) from err
        except TTNDeviceRegistryError as err:
            raise UpdateFailed(str(err)) from err

        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        return names

    @callback
    def apply_device_names(self) -> None:
        """Reconcile HA device names after a successful TTN fetch."""
        if not self.last_update_success:
            return

        device_registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(
            device_registry, self.config_entry.entry_id
        ):
            ttn_device_id = ttn_device_id_for_device(device, self._app_id)
            if not ttn_device_id:
                continue

            desired = self.name_for(ttn_device_id) or ttn_device_id
            if device.name == desired:
                continue

            device_registry.async_update_device(device.id, name=desired)
            _LOGGER.info("Renamed TTN device %s to %s", ttn_device_id, desired)
