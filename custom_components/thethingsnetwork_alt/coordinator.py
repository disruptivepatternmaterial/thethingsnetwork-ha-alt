"""The Things Network HA-Alt DataUpdateCoordinator."""

from datetime import timedelta
import logging

from aiohttp import ClientError
from ttn_client import TTNAuthError, TTNClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_APP_ID, POLLING_PERIOD_S
from .field_defaults import PlatformType, get_field_mapping
from .helpers import platform_for_value

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

        # Which platform owns each (device_id, field_id). See claim_field.
        self._field_platforms: dict[tuple[str, str], PlatformType] = {}

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

    async def _async_update_data(self) -> TTNClient.DATA_TYPE:
        """Fetch data from API endpoint."""
        try:
            measurements = await self._client.fetch_data()
        except TTNAuthError as err:
            _LOGGER.error("TTN authentication error: %s", err)
            raise ConfigEntryAuthFailed from err
        except (ClientError, TimeoutError, RuntimeError) as err:
            # RuntimeError is what ttn_client raises for a non-2xx that is not
            # a 4xx. None of these mean the stored data is gone, so report a
            # plain update failure instead of an unexpected-error traceback.
            raise UpdateFailed(f"Error fetching TTN data: {err}") from err
        except (KeyError, TypeError, ValueError) as err:
            # ttn_client parses the whole response before returning any of it,
            # so one record it cannot read costs the entire window — every
            # device, not just the one that sent it. Nothing here is
            # recoverable from this side; the point is that the log names the
            # cause instead of showing an unexpected-error traceback, and that
            # the next poll still runs.
            raise UpdateFailed(
                f"TTN returned a record ttn_client could not parse, so this "
                f"window was dropped: {err!r}"
            ) from err
        else:
            _LOGGER.debug("fetched data: %s", measurements)
            return measurements

    async def _push_callback(self, data: TTNClient.DATA_TYPE) -> None:
        _LOGGER.debug("pushed data: %s", data)
        self.async_set_updated_data(data)
