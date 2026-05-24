"""The Things Network HA-Alt integration constants."""

import json
from pathlib import Path

from homeassistant.const import Platform

DOMAIN = "thethingsnetwork_alt"
TTN_API_HOST = "eu1.cloud.thethings.network"

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

CONF_APP_ID = "app_id"

POLLING_PERIOD_S = 60

_INTEGRATION_VERSION = json.loads(
    (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
)["version"]
