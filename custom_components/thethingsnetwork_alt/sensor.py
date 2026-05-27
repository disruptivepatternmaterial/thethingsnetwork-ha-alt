"""The Things Network HA-Alt sensor platform."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Final

from ttn_client import (
    TTNBaseValue,
    TTNDeviceTrackerValue,
    TTNSensorAttribute,
    TTNSensorValue,
)

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_APP_ID, DOMAIN
from .coordinator import TTNConfigEntry, TTNCoordinator
from .entity import TTNEntity
from .exclusions import is_excluded
from .field_defaults import (
    FieldMappingDict,
    SensorAttrDict,
    default_field_attr,
    get_field_platform,
    merge_field_attr,
)
from .helpers import extract_sensor_attr, parse_enum
from .metadata import get_device_name
from .timestamp import is_timestamp_field, parse_ttn_timestamp

_LOGGER = logging.getLogger(__name__)

VALID_DEVICE_CLASSES: Final[frozenset[str]] = frozenset(
    item.value for item in SensorDeviceClass
)
VALID_STATE_CLASSES: Final[frozenset[str]] = frozenset(
    item.value for item in SensorStateClass
)
VALID_ENTITY_CATEGORIES: Final[frozenset[str]] = frozenset(
    item.value for item in EntityCategory
)

# Synthetic per-device diagnostic field ids surfaced from rx_metadata.
_META_RSSI: Final = "_meta_rssi"
_META_SNR: Final = "_meta_snr"
_META_LAST_SEEN: Final = "_meta_last_seen"
_META_KINDS: Final[tuple[str, ...]] = (_META_RSSI, _META_SNR, _META_LAST_SEEN)

# GPS sub-component suffixes for TTNDeviceTrackerValue expansion.
_GPS_COMPONENTS: Final[tuple[str, ...]] = ("latitude", "longitude", "altitude")


def _validate_sensor_attr(
    attr: SensorAttrDict, field_name: str, *, device_id: str
) -> None:
    """Log unsupported Home Assistant metadata values from the decoder."""
    if (raw := attr.get("device_class")) and raw not in VALID_DEVICE_CLASSES:
        _LOGGER.warning(
            "Device %s field %s has unsupported device_class=%r",
            device_id,
            field_name,
            raw,
        )

    if (raw := attr.get("state_class")) and raw not in VALID_STATE_CLASSES:
        _LOGGER.warning(
            "Device %s field %s has unsupported state_class=%r",
            device_id,
            field_name,
            raw,
        )

    if (raw := attr.get("entity_category")) and raw not in VALID_ENTITY_CATEGORIES:
        _LOGGER.warning(
            "Device %s field %s has unsupported entity_category=%r",
            device_id,
            field_name,
            raw,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TTNConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up TTN sensors from a config entry."""
    coordinator = entry.runtime_data
    sensors: set[tuple[str, str]] = set()
    app_id = entry.data[CONF_APP_ID]

    def _async_measurement_listener() -> None:
        """Create new entities for newly discovered TTN values."""
        data = coordinator.data
        if not data:
            return

        new_entities: list[SensorEntity] = []

        for device_id, device_uplinks in data.items():
            sensor_attr = extract_sensor_attr(device_uplinks)
            device_name = get_device_name(device_id)

            # Add per-device diagnostic sensors once.
            for kind in _META_KINDS:
                key = (device_id, kind)
                if key in sensors:
                    continue
                if is_excluded(device_id, kind):
                    sensors.add(key)
                    continue
                attr = default_field_attr(kind)
                new_entities.append(
                    TtnMetaSensor(
                        coordinator=coordinator,
                        app_id=app_id,
                        device_id=device_id,
                        kind=kind,
                        attr=attr,
                        device_name=device_name,
                    )
                )
                sensors.add(key)

            for field_id, ttn_value in device_uplinks.items():
                if field_id.startswith("_"):
                    continue

                if isinstance(ttn_value, TTNSensorAttribute):
                    continue

                if is_excluded(device_id, field_id):
                    continue

                if isinstance(ttn_value, TTNDeviceTrackerValue):
                    _add_gps_components(
                        new_entities,
                        sensors,
                        coordinator,
                        app_id,
                        ttn_value,
                        device_name,
                    )
                    continue

                if (device_id, field_id) in sensors:
                    continue

                if not isinstance(ttn_value, TTNSensorValue):
                    continue

                if get_field_platform(field_id) == "binary_sensor":
                    continue

                attr = merge_field_attr(sensor_attr.get(field_id, {}), field_id)
                _validate_sensor_attr(attr, field_id, device_id=device_id)

                new_entities.append(
                    TtnDataSensor(
                        coordinator=coordinator,
                        app_id=app_id,
                        ttn_value=ttn_value,
                        attr=attr,
                        device_name=device_name,
                    )
                )
                sensors.add((device_id, field_id))

        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_measurement_listener))
    _async_measurement_listener()


def _add_gps_components(
    new_entities: list[SensorEntity],
    sensors: set[tuple[str, str]],
    coordinator: TTNCoordinator,
    app_id: str,
    ttn_value: TTNDeviceTrackerValue,
    device_name: str | None,
) -> None:
    """Expand a TTNDeviceTrackerValue into latitude/longitude/altitude sensors."""
    device_id = str(ttn_value.device_id)
    parent_field_id = str(ttn_value.field_id)

    for component in _GPS_COMPONENTS:
        synthetic_field_id = f"{parent_field_id}_{component}"
        key = (device_id, synthetic_field_id)

        if key in sensors:
            continue

        if is_excluded(device_id, synthetic_field_id):
            sensors.add(key)
            continue

        if component == "altitude" and ttn_value.altitude is None:
            sensors.add(key)
            continue

        attr = default_field_attr(component)
        new_entities.append(
            TtnGpsComponentSensor(
                coordinator=coordinator,
                app_id=app_id,
                ttn_value=ttn_value,
                component=component,
                synthetic_field_id=synthetic_field_id,
                attr=attr,
                device_name=device_name,
            )
        )
        sensors.add(key)


class TtnDataSensor(TTNEntity, SensorEntity):
    """Representation of a TTN sensor."""

    _ttn_value: TTNSensorValue

    def __init__(
        self,
        coordinator: TTNCoordinator,
        app_id: str,
        ttn_value: TTNSensorValue,
        attr: SensorAttrDict,
        device_name: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, app_id, ttn_value, device_name=device_name)
        self._ttn_value = ttn_value

        if unit := attr.get("unit"):
            self._attr_native_unit_of_measurement = unit

        if device_class := parse_enum(SensorDeviceClass, attr.get("device_class")):
            self._attr_device_class = device_class

        if state_class := parse_enum(SensorStateClass, attr.get("state_class")):
            self._attr_state_class = state_class

        if entity_category := parse_enum(EntityCategory, attr.get("entity_category")):
            self._attr_entity_category = entity_category

        if precision := attr.get("suggested_display_precision"):
            try:
                self._attr_suggested_display_precision = int(precision)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Invalid suggested_display_precision for %s (unique_id=%s): %r",
                    ttn_value.field_id,
                    self.unique_id,
                    precision,
                )

        if friendly_name := attr.get("friendly_name"):
            self._attr_name = friendly_name

        # Use getattr to avoid AttributeError on HA's CachedProperties descriptor
        # when _attr_device_class hasn't been set yet (mangled __attr_X cache key).
        current_dc = getattr(self, "_attr_device_class", None)
        self._parse_timestamp = (
            current_dc == SensorDeviceClass.TIMESTAMP
            or is_timestamp_field(ttn_value.field_id)
        )
        if self._parse_timestamp and current_dc != SensorDeviceClass.TIMESTAMP:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> StateType:
        """Return the current sensor value."""
        value = self._ttn_value.value
        if self._parse_timestamp:
            return parse_ttn_timestamp(value)
        return value


class TtnGpsComponentSensor(TTNEntity, SensorEntity):
    """One axis (latitude/longitude/altitude) of a TTNDeviceTrackerValue."""

    _ttn_value: TTNDeviceTrackerValue

    def __init__(
        self,
        coordinator: TTNCoordinator,
        app_id: str,
        ttn_value: TTNDeviceTrackerValue,
        component: str,
        synthetic_field_id: str,
        attr: FieldMappingDict,
        device_name: str | None = None,
    ) -> None:
        """Initialize a GPS component sensor."""
        super().__init__(coordinator, app_id, ttn_value, device_name=device_name)
        self._ttn_value = ttn_value
        self._component = component
        # Override unique_id so each component is a separate HA entity.
        self._attr_unique_id = f"{self.device_id}_{synthetic_field_id}"
        self._attr_name = attr.get("friendly_name", component.title())

        if unit := attr.get("unit"):
            self._attr_native_unit_of_measurement = unit
        if device_class := parse_enum(SensorDeviceClass, attr.get("device_class")):
            self._attr_device_class = device_class
        if state_class := parse_enum(SensorStateClass, attr.get("state_class")):
            self._attr_state_class = state_class
        if entity_category := parse_enum(EntityCategory, attr.get("entity_category")):
            self._attr_entity_category = entity_category
        if precision := attr.get("suggested_display_precision"):
            try:
                self._attr_suggested_display_precision = int(precision)
            except (ValueError, TypeError):
                pass

    @property
    def native_value(self) -> StateType:
        """Return latitude / longitude / altitude from the parent device-tracker value."""
        if self._component == "latitude":
            return self._ttn_value.latitude
        if self._component == "longitude":
            return self._ttn_value.longitude
        if self._component == "altitude":
            return self._ttn_value.altitude
        return None


class TtnMetaSensor(CoordinatorEntity[TTNCoordinator], SensorEntity):
    """Per-device diagnostic synthesized from the latest uplink rx_metadata."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TTNCoordinator,
        app_id: str,
        device_id: str,
        kind: str,
        attr: SensorAttrDict,
        device_name: str | None = None,
    ) -> None:
        """Initialize a meta sensor."""
        super().__init__(coordinator)
        self._device_id_value = device_id
        self._kind = kind
        self._attr_unique_id = f"{device_id}_{kind}"

        self._attr_name = attr.get("friendly_name", kind.replace("_meta_", ""))

        if unit := attr.get("unit"):
            self._attr_native_unit_of_measurement = unit
        if device_class := parse_enum(SensorDeviceClass, attr.get("device_class")):
            self._attr_device_class = device_class
        if state_class := parse_enum(SensorStateClass, attr.get("state_class")):
            self._attr_state_class = state_class
        if entity_category := parse_enum(EntityCategory, attr.get("entity_category")):
            self._attr_entity_category = entity_category
        else:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{app_id}_{device_id}")},
            name=device_name or device_id,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state on every coordinator refresh."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> StateType | datetime:
        """Read latest RSSI / SNR / last_seen from any TTN value for this device."""
        data = self.coordinator.data or {}
        device_data = data.get(self._device_id_value)
        if not device_data:
            return None

        sample = _first_uplink_carrier(device_data.values())
        if sample is None:
            return None

        uplink = sample.uplink or {}

        if self._kind == _META_LAST_SEEN:
            received_at = uplink.get("received_at")
            if not received_at:
                return None
            try:
                return datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None

        rx_metadata = uplink.get("uplink_message", {}).get("rx_metadata") or []
        if not rx_metadata:
            return None

        def _rssi_of(entry: dict) -> float:
            for key in ("rssi", "channel_rssi"):
                val = entry.get(key)
                if isinstance(val, (int, float)):
                    return float(val)
            return float("-inf")

        best = max(rx_metadata, key=_rssi_of) if rx_metadata else None
        if not isinstance(best, dict):
            return None

        if self._kind == _META_RSSI:
            for key in ("rssi", "channel_rssi"):
                val = best.get(key)
                if isinstance(val, (int, float)):
                    return val
            return None
        if self._kind == _META_SNR:
            val = best.get("snr")
            if isinstance(val, (int, float)):
                return val
            return None

        return None


def _first_uplink_carrier(values) -> TTNBaseValue | None:
    """Return the first TTN value that exposes a non-empty uplink dict."""
    for v in values:
        if isinstance(v, TTNBaseValue) and getattr(v, "uplink", None):
            return v
    return None
