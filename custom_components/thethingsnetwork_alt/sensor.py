"""The Things Network HA-Alt sensor platform."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import logging
from typing import Final

from ttn_client import (
    TTNBinarySensorValue,
    TTNDeviceTrackerValue,
    TTNSensorAttribute,
    TTNSensorValue,
)

from homeassistant.components.sensor import (
    NON_NUMERIC_DEVICE_CLASSES,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import MAX_LENGTH_STATE_STATE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import CONF_APP_ID, DOMAIN
from .coordinator import TTNConfigEntry, TTNCoordinator
from .entity import TTNCachedEntity, TTNEntity
from .exclusions import is_excluded
from .field_defaults import (
    FieldMappingDict,
    SensorAttrDict,
    default_field_attr,
    merge_field_attr,
)
from .helpers import (
    extract_sensor_attr,
    newest_uplink_carrier,
    parse_enum,
    platform_for_value,
)
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
_META_GATEWAY: Final = "_meta_gateway"
_META_KINDS: Final[tuple[str, ...]] = (
    _META_RSSI,
    _META_SNR,
    _META_LAST_SEEN,
    _META_GATEWAY,
)

# GPS sub-component suffixes for TTNDeviceTrackerValue expansion.
_GPS_COMPONENTS: Final[tuple[str, ...]] = ("latitude", "longitude", "altitude")

# Metadata keys that promise Home Assistant a numeric state.
_NUMERIC_ATTR_KEYS: Final[frozenset[str]] = frozenset(
    {"unit", "device_class", "state_class", "suggested_display_precision"}
)


def _implies_numeric(attr: Mapping[str, object]) -> bool:
    """Return True when this metadata makes Home Assistant expect a number.

    Mirrors ``homeassistant.components.sensor._numeric_state_expected``: any
    unit, state class or display precision means numeric, and so does a
    device class other than the handful that describe non-numeric states.
    """
    if attr.get("unit") or attr.get("state_class"):
        return True
    if attr.get("suggested_display_precision") is not None:
        return True

    device_class = parse_enum(SensorDeviceClass, attr.get("device_class"))
    return device_class is not None and device_class not in NON_NUMERIC_DEVICE_CLASSES


def _is_numeric_reading(value: object) -> bool:
    """Return True when ``value`` can be stored on a numeric sensor."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    try:
        float(value)
    except ValueError:
        return False
    return True


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
    data_sensors: dict[tuple[str, str], TtnDataSensor] = {}
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

                key = (device_id, field_id)
                if key in sensors:
                    if existing := data_sensors.get(key):
                        existing.apply_decoder_attr(sensor_attr.get(field_id, {}))
                    continue

                if platform_for_value(field_id, ttn_value) != "sensor":
                    continue

                if not coordinator.claim_field(device_id, field_id, "sensor"):
                    continue

                decoder_attr = sensor_attr.get(field_id, {})
                attr = merge_field_attr(decoder_attr, field_id)
                _validate_sensor_attr(attr, field_id, device_id=device_id)

                sensor = TtnDataSensor(
                    coordinator=coordinator,
                    app_id=app_id,
                    ttn_value=ttn_value,
                    attr=attr,
                    device_name=device_name,
                )
                sensor.remember_decoder_attr(decoder_attr)
                new_entities.append(sensor)
                data_sensors[key] = sensor
                sensors.add(key)

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

        # Skip WITHOUT marking as seen: a later uplink may include altitude,
        # and the entity should then be created.
        if component == "altitude" and ttn_value.altitude is None:
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

    _ttn_value: TTNSensorValue | TTNBinarySensorValue

    def __init__(
        self,
        coordinator: TTNCoordinator,
        app_id: str,
        ttn_value: TTNSensorValue | TTNBinarySensorValue,
        attr: SensorAttrDict,
        device_name: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, app_id, ttn_value, device_name=device_name)
        self._ttn_value = ttn_value
        self._warned_truncation = False
        self._warned_non_numeric = False
        self._decoder_attr: SensorAttrDict = {}
        self._apply_attr(attr)

    def remember_decoder_attr(self, decoder_attr: SensorAttrDict) -> None:
        """Record the ``_sensor_attr`` this entity was built from."""
        self._decoder_attr = dict(decoder_attr)

    def apply_decoder_attr(self, decoder_attr: SensorAttrDict) -> None:
        """Apply ``_sensor_attr`` metadata that arrived after entity creation.

        A decoder is not obliged to put ``_sensor_attr`` on every uplink, and
        the entity is created from whichever uplink first carried the field.
        Without this the unit and device class stay missing until Home
        Assistant is restarted.
        """
        if not decoder_attr or decoder_attr == self._decoder_attr:
            return

        self._decoder_attr = dict(decoder_attr)
        self._apply_attr(merge_field_attr(decoder_attr, self.field_id))
        if self.hass is not None:
            self.async_write_ha_state()

    def _apply_attr(self, attr: SensorAttrDict | FieldMappingDict) -> None:
        """Set every Home Assistant attribute this sensor derives from metadata.

        Assigns all of them, including to None, so re-applying later metadata
        cannot leave a stale value behind from the previous set.
        """
        attr = self._drop_numeric_metadata_for_text(attr)

        self._attr_native_unit_of_measurement = attr.get("unit")
        self._attr_device_class = parse_enum(
            SensorDeviceClass, attr.get("device_class")
        )
        self._attr_state_class = parse_enum(SensorStateClass, attr.get("state_class"))
        self._attr_entity_category = parse_enum(
            EntityCategory, attr.get("entity_category")
        )
        self._attr_suggested_display_precision = self._parse_precision(
            attr.get("suggested_display_precision")
        )
        self._attr_name = attr.get("friendly_name") or self.field_id

        self._parse_timestamp = (
            self._attr_device_class == SensorDeviceClass.TIMESTAMP
            or is_timestamp_field(self.field_id)
        )
        if self._parse_timestamp:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    def _parse_precision(self, precision: object) -> int | None:
        """Return the configured display precision, or None when unusable."""
        if not precision:
            return None
        try:
            return int(precision)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Invalid suggested_display_precision for %s (unique_id=%s): %r",
                self.field_id,
                self.unique_id,
                precision,
            )
            return None

    def _drop_numeric_metadata_for_text(
        self, attr: SensorAttrDict | FieldMappingDict
    ) -> SensorAttrDict | FieldMappingDict:
        """Strip numeric metadata from a field that is reporting text.

        Home Assistant refuses a non-numeric state on a sensor whose unit,
        device class or state class promises a number, and that refusal
        aborts the state write — the entity is never added, or stops moving.
        The suffix heuristics make this easy to hit by accident: a status
        field named ``foo_c`` is read as degrees Celsius.

        The reading is the fact; the metadata is the guess, so the metadata
        is what gives way.
        """
        if not _implies_numeric(attr) or _is_numeric_reading(self._ttn_value.value):
            return attr

        _LOGGER.warning(
            "Field %s on %s reports text (%r) but is described as a numeric "
            "measurement (unit=%r device_class=%r state_class=%r). Keeping it "
            "as a plain text sensor; correct the mapping in "
            "field_mappings.json or the decoder's _sensor_attr to silence this",
            self.field_id,
            self.device_id,
            self._ttn_value.value,
            attr.get("unit"),
            attr.get("device_class"),
            attr.get("state_class"),
        )
        return {
            key: value
            for key, value in attr.items()
            if key not in _NUMERIC_ATTR_KEYS
        }  # type: ignore[return-value]

    @property
    def native_value(self) -> StateType:
        """Return the current sensor value."""
        value = self._ttn_value.value
        if self._parse_timestamp:
            return parse_ttn_timestamp(value)
        if isinstance(value, bool):
            # ttn_client types the value from the decoded JSON, so a field
            # that normally reports 0/1 arrives as a bool on any uplink where
            # the decoder emitted false/true. Home Assistant would record the
            # string "True", which no numeric sensor can chart or keep
            # statistics for. 0/1 carries the identical reading.
            return int(value)
        if isinstance(value, str):
            if self._is_numeric_sensor and not _is_numeric_reading(value):
                return self._refuse_text_reading(value)
            if len(value) > MAX_LENGTH_STATE_STATE:
                return self._truncated(value)
        return value

    @property
    def _is_numeric_sensor(self) -> bool:
        """Return True when Home Assistant will demand a number from us."""
        return _implies_numeric(
            {
                "unit": self._attr_native_unit_of_measurement,
                "device_class": self._attr_device_class,
                "state_class": self._attr_state_class,
                "suggested_display_precision": self._attr_suggested_display_precision,
            }
        )

    def _refuse_text_reading(self, value: str) -> None:
        """Report unknown for text a numeric sensor cannot hold.

        Only reachable when a field that was numeric at creation later sends
        text, e.g. an error string in place of a measurement. Returning None
        keeps the entity alive; raising out of ``native_value`` would freeze
        it at its last reading with only a stack trace in the log.
        """
        if not self._warned_non_numeric:
            self._warned_non_numeric = True
            _LOGGER.warning(
                "Ignoring %r from %s: the sensor is configured as a numeric "
                "measurement and cannot hold text",
                value,
                self.unique_id,
            )
        return None

    def _truncated(self, value: str) -> str:
        """Shorten a reading Home Assistant would otherwise refuse outright.

        A state over ``MAX_LENGTH_STATE_STATE`` raises out of the state
        machine, which leaves the sensor sitting at ``unknown`` — the whole
        reading is lost. Keeping the leading characters loses less. Decoders
        that emit replay buffers (Dragino ``datalog*``) belong in
        ``field_exclusions.json`` instead.
        """
        if not self._warned_truncation:
            self._warned_truncation = True
            _LOGGER.warning(
                "Truncating %s: field %s reported %d characters and Home "
                "Assistant caps a state at %d. Add it to "
                "field_exclusions.json if it is not a reading",
                self.unique_id,
                self.field_id,
                len(value),
                MAX_LENGTH_STATE_STATE,
            )
        return value[:MAX_LENGTH_STATE_STATE]


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


class TtnMetaSensor(TTNCachedEntity, SensorEntity):
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
        # Retain the last computed reading. Polls after the first fetch only
        # cover the seconds since the previous poll, so a device that did not
        # uplink in that window is absent from coordinator.data. Without this
        # cache the sensor would flip to unknown on every such poll (only a
        # constantly-transmitting device would ever show a value).
        self._cached_value: StateType | datetime = None

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
        """Read latest RSSI / SNR / last_seen, retaining the last known value.

        When the device is absent from the current poll (no uplink in the
        window), keep the previously computed reading instead of dropping to
        ``None`` so the sensor matches the persistence of the regular TTN
        sensors.
        """
        computed = self._compute_value()
        if computed is not None:
            self._cached_value = computed
        return self._cached_value

    def _compute_value(self) -> StateType | datetime:
        """Compute the metric from the latest uplink in coordinator data."""
        data = self.coordinator.data or {}
        device_data = data.get(self._device_id_value)
        if not device_data:
            return None

        sample = newest_uplink_carrier(device_data.values())
        if sample is None:
            return None

        uplink = sample.uplink or {}

        if self._kind == _META_LAST_SEEN:
            # parse_ttn_timestamp forces an offset-less stamp to UTC. Home
            # Assistant rejects a naive datetime on a timestamp sensor, and
            # that ValueError aborts the entity's state write — on the first
            # such uplink the sensor fails to be added at all.
            return parse_ttn_timestamp(uplink.get("received_at"))

        rx_metadata = (uplink.get("uplink_message") or {}).get("rx_metadata") or []
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
        if self._kind == _META_GATEWAY:
            gateway_id = (best.get("gateway_ids") or {}).get("gateway_id")
            return str(gateway_id) if gateway_id else None

        return None
