"""The Things Network HA-Alt integration constants."""

import json
from pathlib import Path
from typing import Final

from homeassistant.const import Platform

DOMAIN = "thethingsnetwork_alt"
TTN_API_HOST = "eu1.cloud.thethings.network"

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

CONF_APP_ID = "app_id"

POLLING_PERIOD_S = 60

# Axes a TTNDeviceTrackerValue is expanded into, one sensor each.
GPS_COMPONENTS: Final[tuple[str, ...]] = ("latitude", "longitude", "altitude")

# Every synthetic field id this integration invents starts with an underscore.
# Discovery skips any decoded field whose id starts with one, so the two
# namespaces cannot overlap and a decoder cannot collide with a synthetic id.
SYNTHETIC_PREFIX: Final = "_"
_GPS_COMPONENT_PREFIX: Final = "_gps_"


def gps_component_field_id(parent_field_id: str, component: str) -> str:
    """Return the synthetic field id for one axis of a decoded GPS object.

    Naming these ``f"{parent}_{component}"`` put them in the decoded-field
    namespace: a ``gps`` object produced ``gps_latitude``, which is also a
    perfectly ordinary field name, and whichever one was seen first silently
    excluded the other.
    """
    return f"{_GPS_COMPONENT_PREFIX}{parent_field_id}_{component}"


def legacy_gps_component_field_id(parent_field_id: str, component: str) -> str:
    """Return the pre-0.7.4 axis field id, for migration and exclusions."""
    return f"{parent_field_id}_{component}"


_INTEGRATION_VERSION = json.loads(
    (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
)["version"]
