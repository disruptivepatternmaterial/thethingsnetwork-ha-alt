"""The Things Network HA-Alt DataUpdateCoordinator."""

from datetime import timedelta
import logging

from ttn_client import TTNAuthError, TTNClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_APP_ID, POLLING_PERIOD_S

_LOGGER = logging.getLogger(__name__)

type TTNConfigEntry = ConfigEntry[TTNCoordinator]


class TTNCoordinator(DataUpdateCoordinator[TTNClient.DATA_TYPE]):
    """TTN coordinator."""

    config_entry: TTNConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TTNConfigEntry) -> None:
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"TheThingsNetworkAlt_{entry.data[CONF_APP_ID]}",
            update_interval=timedelta(
                seconds=POLLING_PERIOD_S,
            ),
        )

        self._client = TTNClient(
            entry.data[CONF_HOST],
            entry.data[CONF_APP_ID],
            entry.data[CONF_API_KEY],
            push_callback=self._push_callback,
        )

    async def _async_update_data(self) -> TTNClient.DATA_TYPE:
        """Fetch data from API endpoint."""
        try:
            measurements = await self._client.fetch_data()
        except TTNAuthError as err:
            _LOGGER.error("TTNAuthError")
            raise ConfigEntryAuthFailed from err
        else:
            _LOGGER.debug("fetched data: %s", measurements)
            return measurements

    async def _push_callback(self, data: TTNClient.DATA_TYPE) -> None:
        _LOGGER.debug("pushed data: %s", data)
        self.async_set_updated_data(data)
