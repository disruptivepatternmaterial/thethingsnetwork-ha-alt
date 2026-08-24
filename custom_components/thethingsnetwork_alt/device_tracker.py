"""The Things Network HA-Alt device tracker platform.

Creates one GPS device_tracker entity per TTN end device so devices show up
on the Home Assistant map and in map cards. Location source priority:

1. GPS decoded from the payload itself (``TTNDeviceTrackerValue``, e.g. a
   RAK10701 field tester) — newest fix wins, but only while the fix is no
   more than ``_GPS_STALE_AFTER`` older than the device's newest uplink.
   A device whose GPS stopped reporting must not pin an ancient fix over a
   corrected registry location.
2. The registry location set on the end device in the TTN console
   (``uplink_message.locations.user``, ``SOURCE_REGISTRY``).

``locations["frm-payload"]`` is deliberately ignored: TTN persists it from
old uplinks and it has been observed holding a stale, bogus coordinate on a
device whose decoder never emitted GPS.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final

from ttn_client import TTNDeviceTrackerValue

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_APP_ID, DOMAIN
from .coordinator import TTNConfigEntry, TTNCoordinator
from .exclusions import is_excluded
from .helpers import newest_uplink_carrier
from .metadata import get_device_name

# Synthetic field id used for exclusions (exclude it per device in
# field_exclusions.json to suppress the tracker for that device).
_META_LOCATION: Final = "_meta_location"

# A decoded-payload GPS fix older than this relative to the device's newest
# uplink is considered stale and loses to the registry location.
_GPS_STALE_AFTER: Final = timedelta(hours=24)

_LAT_FIELD_IDS: Final[frozenset[str]] = frozenset(
    {"latitude", "lat", "gps_latitude"}
)
_LON_FIELD_IDS: Final[frozenset[str]] = frozenset(
    {"longitude", "lon", "lng", "gps_longitude"}
)
_ALT_FIELD_IDS: Final[frozenset[str]] = frozenset(
    {"altitude", "alt", "gps_altitude"}
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TTNConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up TTN device trackers from a config entry."""
    coordinator = entry.runtime_data
    app_id = entry.data[CONF_APP_ID]
    tracked: set[str] = set()

    def _async_measurement_listener() -> None:
        """Create a tracker for each newly discovered device."""
        data = coordinator.data
        if not data:
            return

        new_entities: list[TrackerEntity] = []
        for device_id in data:
            if device_id in tracked:
                continue
            if is_excluded(device_id, _META_LOCATION):
                tracked.add(device_id)
                continue
            new_entities.append(
                TtnDeviceTracker(
                    coordinator=coordinator,
                    app_id=app_id,
                    device_id=device_id,
                    device_name=get_device_name(device_id),
                )
            )
            tracked.add(device_id)

        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_measurement_listener))
    _async_measurement_listener()


class TtnDeviceTracker(CoordinatorEntity[TTNCoordinator], TrackerEntity):
    """GPS tracker for a TTN end device."""

    _attr_has_entity_name = True
    _attr_name = "Location"
    _attr_source_type = SourceType.GPS

    def __init__(
        self,
        coordinator: TTNCoordinator,
        app_id: str,
        device_id: str,
        device_name: str | None = None,
    ) -> None:
        """Initialize the tracker."""
        super().__init__(coordinator)
        self._device_id_value = device_id
        self._attr_unique_id = f"{device_id}{_META_LOCATION}"
        # Retain the last computed location: polls after the first fetch only
        # cover the seconds since the previous poll, so a device that did not
        # uplink in that window is absent from coordinator.data.
        self._cached_location: dict[str, Any] | None = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{app_id}_{device_id}")},
            name=device_name or device_id,
        )
        self._apply_location_attrs()

    async def async_added_to_hass(self) -> None:
        """Apply cached GPS attrs and the JSON display name on the existing device."""
        await super().async_added_to_hass()
        self._apply_location_attrs()
        friendly = get_device_name(self._device_id_value)
        if friendly and self.device_entry and self.device_entry.name != friendly:
            dr.async_get(self.hass).async_update_device(
                self.device_entry.id, name=friendly
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state on every coordinator refresh."""
        self._apply_location_attrs()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose altitude and the location source."""
        location = self._location()
        if not location:
            return None
        attrs: dict[str, Any] = {"location_source": location["source"]}
        if location["altitude"] is not None:
            attrs["altitude"] = location["altitude"]
        return attrs

    def _apply_location_attrs(self) -> None:
        """Copy computed coords onto TrackerEntity cached attributes.

        HA TrackerEntity reads ``_attr_latitude`` / ``_attr_longitude`` via
        cached properties. Overriding ``latitude`` as a property is ignored,
        and the first ``None`` write is cached forever unless we set these
        attrs and drop the cached values.
        """
        location = self._location()
        if location is None:
            return
        self._attr_latitude = location["latitude"]
        self._attr_longitude = location["longitude"]
        self.__dict__.pop("latitude", None)
        self.__dict__.pop("longitude", None)

    def _location(self) -> dict[str, Any] | None:
        computed = self._compute_location()
        if computed is not None:
            self._cached_location = computed
        return self._cached_location

    def _compute_location(self) -> dict[str, Any] | None:
        """Compute the location from the latest data for this device."""
        data = self.coordinator.data or {}
        device_data = data.get(self._device_id_value)
        if not device_data:
            return None

        carrier = newest_uplink_carrier(device_data.values())

        gps_location = self._gps_location(device_data.values(), carrier)
        if gps_location is not None:
            return gps_location

        field_location = self._field_location(device_data)
        if field_location is not None:
            return field_location

        if carrier is None:
            return None
        uplink = carrier.uplink or {}
        uplink_message = uplink.get("uplink_message") or {}
        locations = uplink_message.get("locations") or {}
        registry = locations.get("user")
        if not isinstance(registry, dict):
            return None
        latitude = registry.get("latitude")
        longitude = registry.get("longitude")
        if not isinstance(latitude, (int, float)) or not isinstance(
            longitude, (int, float)
        ):
            return None
        altitude = registry.get("altitude")
        return {
            "latitude": float(latitude),
            "longitude": float(longitude),
            "altitude": float(altitude)
            if isinstance(altitude, (int, float))
            else None,
            "source": "registry",
        }

    @staticmethod
    def _field_location(device_data: dict[str, Any]) -> dict[str, Any] | None:
        """Use top-level decoded latitude/longitude fields as a GPS fix.

        Decoders that emit flat ``latitude`` / ``longitude`` numbers become
        ordinary sensors, not ``TTNDeviceTrackerValue``. Without this, the
        tracker stays unknown even though HA already has the coordinates.
        """
        latitude = None
        longitude = None
        altitude = None
        for field_id, value in device_data.items():
            if isinstance(value, TTNDeviceTrackerValue):
                continue
            raw = getattr(value, "value", None)
            if not isinstance(raw, (int, float)):
                continue
            key = str(field_id).lower()
            if key in _LAT_FIELD_IDS:
                latitude = float(raw)
            elif key in _LON_FIELD_IDS:
                longitude = float(raw)
            elif key in _ALT_FIELD_IDS:
                altitude = float(raw)
        if latitude is None or longitude is None:
            return None
        return {
            "latitude": latitude,
            "longitude": longitude,
            "altitude": altitude,
            "source": "payload",
        }

    @staticmethod
    def _gps_location(values, carrier) -> dict[str, Any] | None:
        """Return the newest valid, non-stale decoded-payload GPS location.

        Returns None (falling back to the registry location) when there is
        no GPS field, its coordinates are not numeric, or the fix is more
        than ``_GPS_STALE_AFTER`` older than the device's newest uplink.
        """
        newest: TTNDeviceTrackerValue | None = None
        newest_received_at = None
        for value in values:
            if not isinstance(value, TTNDeviceTrackerValue):
                continue
            try:
                received_at = value.received_at
            except (KeyError, ValueError, TypeError):
                continue
            if newest_received_at is None or received_at > newest_received_at:
                newest = value
                newest_received_at = received_at
        if newest is None:
            return None

        if carrier is not None:
            try:
                if carrier.received_at - newest_received_at > _GPS_STALE_AFTER:
                    return None
            except (KeyError, ValueError, TypeError):
                pass

        try:
            latitude = newest.latitude
            longitude = newest.longitude
        except (KeyError, TypeError):
            return None
        if not isinstance(latitude, (int, float)) or not isinstance(
            longitude, (int, float)
        ):
            return None
        try:
            altitude = newest.altitude
        except (KeyError, TypeError):
            altitude = None
        return {
            "latitude": float(latitude),
            "longitude": float(longitude),
            "altitude": float(altitude) if isinstance(altitude, (int, float)) else None,
            "source": "gps",
        }
