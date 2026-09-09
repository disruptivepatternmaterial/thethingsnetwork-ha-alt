"""Shared fixtures and builders for the TTN HA-Alt test suite.

Tests build real ``ttn_client`` value objects by running the library's own
parser over a synthetic uplink, rather than mocking them. The bugs these tests
cover are all consequences of the real class hierarchy and of
``TTNBaseValue.received_at`` re-parsing the raw uplink string on every access,
so mocks would hide exactly what we are trying to pin down.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from ttn_client import TTNBaseValue
from ttn_client.parsers.default import default_parser

APP_ID = "test-app"
DEVICE_ID = "dev-1"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: Any,
) -> None:
    """Let Home Assistant load this repo's custom_components/ folder."""
    return


def make_uplink(
    received_at: str,
    decoded: dict[str, Any],
    device_id: str = DEVICE_ID,
) -> dict[str, Any]:
    """Build the ``result`` object shape the TTN Storage API streams back."""
    return {
        "end_device_ids": {"device_id": device_id},
        "received_at": received_at,
        "uplink_message": {"decoded_payload": decoded},
    }


def parse_uplink(
    received_at: str,
    decoded: dict[str, Any],
    device_id: str = DEVICE_ID,
) -> dict[str, TTNBaseValue]:
    """Return one device's parsed fields, as they appear in coordinator data."""
    return default_parser(make_uplink(received_at, decoded, device_id))


def coordinator_data(
    received_at: str,
    decoded: dict[str, Any],
    device_id: str = DEVICE_ID,
) -> dict[str, dict[str, TTNBaseValue]]:
    """Return a full ``TTNCoordinator.data`` payload for a single device."""
    return {device_id: parse_uplink(received_at, decoded, device_id)}


def make_coordinator(data: Any = None) -> MagicMock:
    """Return a stand-in coordinator carrying a fixed data payload."""
    coordinator = MagicMock()
    coordinator.data = data if data is not None else {}
    coordinator.last_update_success = True
    return coordinator
