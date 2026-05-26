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
from .field_defaults import FieldMappingDict, get_field_platform, merge_field_attr, value_is_on
from .helpers import extract_sensor_attr, parse_enum
from .metadata import get_device_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TTNConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up TTN binary sensors from a config entry."""
    coordinator = entry.runtime_data
    sensors: set[tuple[str, str]] = set()

    def _async_measurement_listener() -> None:
        data = coordinator.data
        if not data:
            return

        new_sensors: dict[tuple[str, str], TtnDataBinarySensor] = {}

        for device_id, device_uplinks in data.items():
            sensor_attr = extract_sensor_attr(device_uplinks)

            for field_id, ttn_value in device_uplinks.items():
                if (device_id, field_id) in sensors:
                    continue

                if field_id.startswith("_"):
                    continue

                if isinstance(ttn_value, TTNSensorAttribute):
                    continue

                if is_excluded(device_id, field_id):
                    continue

                is_binary_value = isinstance(ttn_value, TTNBinarySensorValue)
                is_mapped_binary = (
                    isinstance(ttn_value, TTNSensorValue)
                    and get_field_platform(field_id) == "binary_sensor"
                )
                if not is_binary_value and not is_mapped_binary:
                    continue

                attr = merge_field_attr(sensor_attr.get(field_id, {}), field_id)

                new_sensors[(device_id, field_id)] = TtnDataBinarySensor(
                    coordinator=coordinator,
                    app_id=entry.data[CONF_APP_ID],
                    ttn_value=ttn_value,
                    attr=attr,
                    device_name=get_device_name(device_id),
                )

        if new_sensors:
            async_add_entities(new_sensors.values())

        sensors.update(new_sensors.keys())

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
        device_name: str | None = None,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, app_id, ttn_value, device_name=device_name)
        self._ttn_value = ttn_value
        self._attr = attr

        if device_class := parse_enum(BinarySensorDeviceClass, attr.get("device_class")):
            self._attr_device_class = device_class

        if entity_category := parse_enum(EntityCategory, attr.get("entity_category")):
            self._attr_entity_category = entity_category

        if friendly_name := attr.get("friendly_name"):
            self._attr_name = friendly_name

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        value = self._ttn_value.value
        if isinstance(self._ttn_value, TTNBinarySensorValue):
            return bool(value)
        return value_is_on(value, self._attr)
