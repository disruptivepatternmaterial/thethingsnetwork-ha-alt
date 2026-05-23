"""The Things Network HA-Alt integration constants."""

from homeassistant.const import Platform

DOMAIN = "thethingsnetwork_alt"
TTN_API_HOST = "eu1.cloud.thethings.network"

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

CONF_APP_ID = "app_id"

POLLING_PERIOD_S = 60
