"""Support for The Things Network HA-Alt entities."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Final

from ttn_client import TTNBaseValue, TTNBinarySensorValue, TTNSensorValue

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TTNCoordinator

_LOGGER = logging.getLogger(__name__)

# Value classes whose ``.value`` is a plain scalar. ttn_client picks the class
# from the Python type of the decoded JSON, so a decoder that reports 0/1 on
# some uplinks and false/true on others alternates between these two for a
# single field. Both carry a usable reading, so swapping between them must
# never cost a measurement.
_SCALAR_VALUE_TYPES: Final = (TTNSensorValue, TTNBinarySensorValue)


def received_at_utc(value: TTNBaseValue) -> datetime | None:
    """Return an aware UTC receipt time, or None when it cannot be read.

    ``TTNBaseValue.received_at`` re-parses the raw uplink string on every
    access and is unguarded: a missing key raises ``KeyError``, a malformed
    stamp raises ``ValueError``, and a stamp with no offset yields a naive
    datetime that cannot be compared against an aware one.
    """
    try:
        received_at = value.received_at
    except (KeyError, TypeError, ValueError):
        return None

    if not isinstance(received_at, datetime):
        return None

    # TTN timestamps are UTC; one without an offset is still UTC.
    if received_at.tzinfo is None:
        return received_at.replace(tzinfo=UTC)
    return received_at.astimezone(UTC)


def raw_received_at(value: TTNBaseValue) -> str | None:
    """Return the uplink's unparsed receipt stamp.

    TTN reports nanoseconds but ``datetime.fromisoformat`` truncates to
    microseconds, so two distinct uplinks can share a parsed timestamp. The
    original string keeps the full precision and is what distinguishes a
    re-delivered uplink from a genuinely new one.
    """
    uplink = getattr(value, "uplink", None)
    if not isinstance(uplink, dict):
        return None
    stamp = uplink.get("received_at")
    return None if stamp is None else str(stamp)


class TTNEntity(CoordinatorEntity[TTNCoordinator]):
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
        self._ttn_value = update
        self.async_write_ha_state()

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
