"""Support for The Things Network HA-Alt entities."""

from __future__ import annotations

import logging
from typing import Final

from ttn_client import TTNBaseValue, TTNBinarySensorValue, TTNSensorValue

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TTNCoordinator
from .helpers import raw_received_at, received_at_utc

_LOGGER = logging.getLogger(__name__)

# Value classes whose ``.value`` is a plain scalar. ttn_client picks the class
# from the Python type of the decoded JSON, so a decoder that reports 0/1 on
# some uplinks and false/true on others alternates between these two for a
# single field. Both carry a usable reading, so swapping between them must
# never cost a measurement.
_SCALAR_VALUE_TYPES: Final = (TTNSensorValue, TTNBinarySensorValue)


class TTNCachedEntity(CoordinatorEntity[TTNCoordinator]):
    """Coordinator entity whose state is the last uplink, not the last fetch."""

    @property
    def available(self) -> bool:
        """Return True while this entity holds a reading.

        ``CoordinatorEntity`` reports unavailable whenever the last fetch
        failed. A TTN entity does not show a live poll result — it shows the
        newest uplink TTN has delivered, which a failed fetch does not
        invalidate. Tying availability to the fetch blanked the diagnostic
        sensors and the tracker on every transient TTN error while the data
        sensors, which never rewrite state on a failed poll, kept theirs.
        The ``Last seen`` sensor is what reports a device going quiet.
        """
        return self._attr_available


class TTNEntity(TTNCachedEntity):
    """Representation of a The Things Network sensor entity."""

    _attr_has_entity_name = True
    _ttn_value: TTNBaseValue

    def __init__(
        self,
        coordinator: TTNCoordinator,
        app_id: str,
        ttn_value: TTNBaseValue,
        device_name: str | None = None,
    ) -> None:
        """Initialize a The Things Network entity."""
        super().__init__(coordinator)

        self._ttn_value = ttn_value
        # Resolved once: the properties below read through to the raw uplink,
        # which would otherwise be re-indexed on every coordinator tick.
        self._device_id = str(ttn_value.device_id)
        self._field_id = str(ttn_value.field_id)
        self._warned_transitions: set[tuple[str, str]] = set()

        self._attr_unique_id = f"{self.device_id}_{self.field_id}"
        self._attr_name = self.field_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{app_id}_{self.device_id}")},
            name=device_name or self.device_id,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if not self.coordinator.data:
            return

        update = self.coordinator.data.get(self.device_id, {}).get(self.field_id)
        if update is None or update is self._ttn_value:
            return

        if not self._accepts(update) or not self._supersedes(update):
            return

        _LOGGER.debug("Received update for %s: %s", self.unique_id, update)
        self._adopt(update)
        self.async_write_ha_state()

    def _adopt(self, update: TTNBaseValue) -> None:
        """Make ``update`` the current reading.

        Subclasses override this to refresh anything derived from the reading
        itself — metadata that depends on whether the field is reporting a
        number, for instance — before the state write goes out.
        """
        self._ttn_value = update

    def _accepts(self, update: TTNBaseValue) -> bool:
        """Return True when this entity can represent ``update``.

        Only genuinely incompatible shapes are refused — a scalar entity
        cannot render a GPS fix, and vice versa. Swapping between the two
        scalar classes is normal decoder behaviour and is allowed through.
        """
        if isinstance(update, type(self._ttn_value)):
            return True
        if isinstance(self._ttn_value, _SCALAR_VALUE_TYPES) and isinstance(
            update, _SCALAR_VALUE_TYPES
        ):
            return True

        self._warn_incompatible(update)
        return False

    def _warn_incompatible(self, update: TTNBaseValue) -> None:
        """Warn once per transition, not once per uplink."""
        transition = (type(self._ttn_value).__name__, type(update).__name__)
        if transition in self._warned_transitions:
            return
        self._warned_transitions.add(transition)

        _LOGGER.warning(
            "Ignoring update for %s: the decoder changed field %s from %s to %s, "
            "which this entity cannot represent. Delete the entity in Home "
            "Assistant to have it recreated for the new type",
            self.unique_id,
            self.field_id,
            transition[0],
            transition[1],
        )

    def _supersedes(self, update: TTNBaseValue) -> bool:
        """Return True when ``update`` carries a reading we have not applied.

        Fetch windows overlap, so the coordinator legitimately re-delivers an
        uplink that is already on the entity; that case must not rewrite state.
        Everything else must get through, including an update whose timestamp
        is unreadable — a value we cannot date is not a reason to freeze the
        entity for the rest of the run.
        """
        new_at = received_at_utc(update)
        current_at = received_at_utc(self._ttn_value)

        if new_at is not None and current_at is not None and new_at != current_at:
            return new_at > current_at

        # Equal to microsecond precision, or undateable on either side. The
        # full-precision stamp is the only thing that still distinguishes a
        # redelivery from a new reading.
        new_stamp = raw_received_at(update)
        return new_stamp is None or new_stamp != raw_received_at(self._ttn_value)

    @property
    def device_id(self) -> str:
        """Return device_id."""
        return self._device_id

    @property
    def field_id(self) -> str:
        """Return field_id."""
        return self._field_id
