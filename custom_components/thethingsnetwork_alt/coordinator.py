"""The Things Network HA-Alt DataUpdateCoordinator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from aiohttp import ClientError
from ttn_client import TTNAuthError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_APP_ID, DOMAIN, POLLING_PERIOD_S
from .device_names import TTNDeviceNameCoordinator
from .field_defaults import PlatformType, get_field_mapping
from .helpers import platform_for_value
from .storage import DATA_TYPE, TTNStorageClient, TTNStorageError

_LOGGER = logging.getLogger(__name__)


@dataclass(kw_only=True, slots=True)
class TTNRuntimeData:
    """Coordinators attached to a loaded config entry."""

    uplinks: TTNCoordinator
    names: TTNDeviceNameCoordinator


type TTNConfigEntry = ConfigEntry[TTNRuntimeData]


class TTNCoordinator(DataUpdateCoordinator[DATA_TYPE]):
    """TTN coordinator."""

    config_entry: TTNConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TTNConfigEntry) -> None:
        """Initialize my coordinator."""
        app_id = str(entry.data[CONF_APP_ID])
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"TheThingsNetworkAlt_{app_id}",
            update_interval=timedelta(
                seconds=POLLING_PERIOD_S,
            ),
        )
        self._app_id = app_id

        self._client = TTNStorageClient(
            hass,
            entry.data[CONF_HOST],
            app_id,
            entry.data[CONF_API_KEY],
        )

        # Which platform owns each (device_id, field_id). See resolve_platform.
        self._field_platforms: dict[tuple[str, str], PlatformType] = {}
        # Devices whose first-seen uplink already triggered an on-demand name check.
        self._probed_unknown_devices: set[str] = set()

    def resolve_platform(
        self, device_id: str, field_id: str, ttn_value: object
    ) -> PlatformType | None:
        """Return the platform that owns a field, or None if neither does.

        The first answer sticks for the lifetime of the config entry. Without
        that, a decoder reporting ``0``/``1`` on some uplinks and
        ``false``/``true`` on others would hand the field to whichever
        platform matches the current value class, and the user would end up
        with both a ``sensor`` and a ``binary_sensor`` for one measurement.

        An explicit ``platform`` in ``field_mappings.json`` is authoritative
        and is deliberately allowed to move a field between platforms; that is
        the edit ``update_registered_entity_metadata`` cleans up after.
        """
        explicit = get_field_mapping(field_id).get("platform")
        if explicit in ("sensor", "binary_sensor"):
            return explicit

        key = (device_id, field_id)
        if (owner := self._field_platforms.get(key)) is not None:
            return owner

        platform = platform_for_value(field_id, ttn_value)
        if platform is not None:
            self._field_platforms[key] = platform
        return platform

    def seed_field_platform(
        self, device_id: str, field_id: str, platform: PlatformType
    ) -> None:
        """Pre-assign a field's owner, e.g. from the entity registry."""
        self._field_platforms.setdefault((device_id, field_id), platform)

    async def _async_update_data(self) -> DATA_TYPE:
        """Fetch data from API endpoint."""
        try:
            measurements = await self._client.fetch_data()
        except TTNAuthError as err:
            _LOGGER.error("TTN authentication error: %s", err)
            raise ConfigEntryAuthFailed from err
        except (ClientError, TimeoutError, TTNStorageError) as err:
            # None of these mean the stored data is gone. The client leaves
            # its watermark alone on failure, so the next poll asks for this
            # window again rather than skipping past it.
            raise UpdateFailed(f"Error fetching TTN data: {err}") from err

        await self._refresh_names_for_unseen_devices(measurements)
        _LOGGER.debug("fetched data: %s", measurements)
        return measurements

    async def _refresh_names_for_unseen_devices(self, measurements: DATA_TYPE) -> None:
        """Fetch names once when a brand-new device uplinks between hourly polls.

        Entity IDs are slugged from the device name at initial registration, so
        refreshing before the platform listeners create entities gives a newly
        provisioned device its TTN friendly name in its entity IDs immediately.
        """
        if not measurements:
            return
        runtime_data = getattr(self.config_entry, "runtime_data", None)
        if runtime_data is None:
            return

        device_registry = dr.async_get(self.hass)
        needs_refresh = False
        for device_id in measurements:
            if device_id in self._probed_unknown_devices:
                continue
            if runtime_data.names.name_for(device_id) is not None:
                self._probed_unknown_devices.add(device_id)
                continue
            existing = device_registry.async_get_device_by_identifier(
                (DOMAIN, f"{self._app_id}_{device_id}"),
                self.config_entry.entry_id,
            )
            self._probed_unknown_devices.add(device_id)
            if existing is None:
                needs_refresh = True

        if needs_refresh:
            await runtime_data.names.async_refresh()
