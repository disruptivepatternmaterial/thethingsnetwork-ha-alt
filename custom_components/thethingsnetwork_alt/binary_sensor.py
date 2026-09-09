"""The Things Network HA-Alt binary sensor platform."""

from __future__ import annotations

import logging

from ttn_client import TTNBinarySensorValue, TTNSensorAttribute, TTNSensorValue

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_APP_ID
from .coordinator import TTNConfigEntry, TTNCoordinator
from .entity import TTNEntity
from .exclusions import is_excluded
from .field_defaults import (
    FieldMappingDict,
    SensorAttrDict,
    merge_field_attr,
    value_is_on,
)
from .helpers import EnumT, extract_sensor_attr, parse_enum
from .metadata import get_device_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TTNConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up TTN binary sensors from a config entry."""
    coordinator = entry.runtime_data
    sensors: dict[tuple[str, str], TtnDataBinarySensor] = {}
    # Latest ``_sensor_attr`` seen for each field, kept across fetch windows.
    # A decoder puts it on the uplinks it chooses, which need not be the ones
    # carrying the field, so metadata read from any window has to survive to
    # whichever window creates the entity.
    decoder_attrs: dict[tuple[str, str], SensorAttrDict] = {}

    def _async_measurement_listener() -> None:
        data = coordinator.data
        if not data:
            return

        new_sensors: dict[tuple[str, str], TtnDataBinarySensor] = {}

        for device_id, device_uplinks in data.items():
            _remember_decoder_attr(device_id, extract_sensor_attr(device_uplinks))

            for field_id, ttn_value in device_uplinks.items():
                key = (device_id, field_id)
                if key in sensors:
                    continue

                if field_id.startswith("_"):
                    continue

                if isinstance(ttn_value, TTNSensorAttribute):
                    continue

                if is_excluded(device_id, field_id):
                    continue

                if coordinator.resolve_platform(device_id, field_id, ttn_value) != (
                    "binary_sensor"
                ):
                    continue

                decoder_attr = decoder_attrs.get(key, {})

                new_sensors[key] = TtnDataBinarySensor(
                    coordinator=coordinator,
                    app_id=entry.data[CONF_APP_ID],
                    ttn_value=ttn_value,
                    attr=merge_field_attr(decoder_attr, field_id),
                    decoder_attr=decoder_attr,
                    device_name=get_device_name(device_id),
                )

        if new_sensors:
            async_add_entities(new_sensors.values())

        sensors.update(new_sensors)

    def _remember_decoder_attr(
        device_id: str, sensor_attr: dict[str, SensorAttrDict]
    ) -> None:
        """Record this window's ``_sensor_attr`` and push it to live entities."""
        for field_id, decoder_attr in sensor_attr.items():
            key = (device_id, field_id)
            if not decoder_attr or decoder_attrs.get(key) == decoder_attr:
                continue

            decoder_attrs[key] = decoder_attr
            if existing := sensors.get(key):
                existing.apply_decoder_attr(decoder_attr)

    entry.async_on_unload(coordinator.async_add_listener(_async_measurement_listener))
    _async_measurement_listener()


class TtnDataBinarySensor(TTNEntity, BinarySensorEntity):
    """Representation of a TTN binary sensor."""

    _ttn_value: TTNBinarySensorValue | TTNSensorValue

    def __init__(
        self,
        coordinator: TTNCoordinator,
        app_id: str,
        ttn_value: TTNBinarySensorValue | TTNSensorValue,
        attr: FieldMappingDict,
        decoder_attr: SensorAttrDict | None = None,
        device_name: str | None = None,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, app_id, ttn_value, device_name=device_name)
        self._ttn_value = ttn_value
        self._decoder_attr: SensorAttrDict = dict(decoder_attr or {})
        self._apply_attr(attr)

    def apply_decoder_attr(self, decoder_attr: SensorAttrDict) -> None:
        """Apply ``_sensor_attr`` metadata that arrived after entity creation.

        A decoder is not obliged to put ``_sensor_attr`` on every uplink, and
        the entity is created from whichever uplink first carried the field.
        Without this the device class stays missing until Home Assistant is
        restarted.
        """
        if not decoder_attr or decoder_attr == self._decoder_attr:
            return

        self._decoder_attr = dict(decoder_attr)
        self._apply_attr(merge_field_attr(decoder_attr, self.field_id))
        if self.hass is not None:
            self.async_write_ha_state()

    def _apply_attr(self, attr: FieldMappingDict) -> None:
        """Set every attribute this sensor derives from metadata.

        Assigns all of them, including to None, so re-applying later metadata
        cannot leave a stale value behind from the previous set.
        """
        self._attr = attr

        self._attr_device_class = self._parse_or_warn(
            BinarySensorDeviceClass, attr.get("device_class"), "device_class"
        )
        self._attr_entity_category = self._parse_or_warn(
            EntityCategory, attr.get("entity_category"), "entity_category"
        )
        self._attr_name = attr.get("friendly_name") or self.field_id

    def _parse_or_warn(
        self, enum_cls: type[EnumT], raw: object | None, key: str
    ) -> EnumT | None:
        """Parse one metadata value, reporting anything Home Assistant refuses."""
        if not raw:
            return None

        if (parsed := parse_enum(enum_cls, raw)) is not None:
            return parsed

        _LOGGER.warning(
            "Field %s has unsupported binary_sensor %s=%r",
            self.field_id,
            key,
            raw,
        )
        return None

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        value = self._ttn_value.value
        if isinstance(self._ttn_value, TTNBinarySensorValue):
            return bool(value)
        return value_is_on(value, self._attr)
