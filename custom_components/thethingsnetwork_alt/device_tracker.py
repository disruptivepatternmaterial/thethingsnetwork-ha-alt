"""The Things Network HA-Alt device tracker platform.

Creates one GPS device_tracker entity per TTN end device so devices show up
on the Home Assistant map and in map cards. Location source priority:

1. GPS decoded from the payload itself (``TTNDeviceTrackerValue``, e.g. a
   RAK10701 field tester) — newest fix wins.
2. The registry location set on the end device in the TTN console
   (``uplink_message.locations.user``, ``SOURCE_REGISTRY``).

``locations["frm-payload"]`` is deliberately ignored: TTN persists it from
old uplinks and it has been observed holding a stale, bogus coordinate on a
device whose decoder never emitted GPS.
"""

from __future__ import annotations

from typing import Any, Final

from ttn_client import TTNDeviceTrackerValue

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant, callback
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

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state on every coordinator refresh."""
        self.async_write_ha_state()

    @property
    def latitude(self) -> float | None:
        """Return the latitude of the device."""
        location = self._location()
        return location["latitude"] if location else None

    @property
    def longitude(self) -> float | None:
        """Return the longitude of the device."""
        location = self._location()
        return location["longitude"] if location else None

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

        gps_value = self._newest_gps_value(device_data.values())
        if gps_value is not None:
            return {
                "latitude": gps_value.latitude,
                "longitude": gps_value.longitude,
                "altitude": gps_value.altitude,
                "source": "gps",
            }

        carrier = newest_uplink_carrier(device_data.values())
        if carrier is None:
            return None
        uplink = carrier.uplink or {}
        locations = uplink.get("uplink_message", {}).get("locations") or {}
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
    def _newest_gps_value(values) -> TTNDeviceTrackerValue | None:
        """Return the newest decoded-payload GPS value, if any."""
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
        return newest
