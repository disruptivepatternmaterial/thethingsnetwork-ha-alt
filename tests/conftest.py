"""Shared fixtures and builders for the TTN HA-Alt test suite.

Tests build real ``ttn_client`` value objects by running the library's own
parser over a synthetic uplink, rather than mocking them. The bugs these tests
cover are all consequences of the real class hierarchy and of
``TTNBaseValue.received_at`` re-parsing the raw uplink string on every access,
so mocks would hide exactly what we are trying to pin down.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from ttn_client import TTNBaseValue
from ttn_client.parsers import ttn_parse
from ttn_client.parsers.default import default_parser

from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.thethingsnetwork_alt.const import (
    CONF_APP_ID,
    DOMAIN,
    POLLING_PERIOD_S,
)

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
    *,
    rx_metadata: list[dict[str, Any]] | None = None,
    locations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``result`` object shape the TTN Storage API streams back."""
    uplink_message: dict[str, Any] = {"decoded_payload": decoded}
    if rx_metadata is not None:
        uplink_message["rx_metadata"] = rx_metadata
    if locations is not None:
        uplink_message["locations"] = locations
    return {
        "end_device_ids": {"device_id": device_id},
        "received_at": received_at,
        "uplink_message": uplink_message,
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


def window(*uplinks: dict[str, Any]) -> dict[str, dict[str, TTNBaseValue]]:
    """Merge raw uplinks exactly the way ``TTNClient.fetch_data`` does.

    The storage API streams uplinks oldest-first and the client folds them
    per device with ``|=``, so a field present in several uplinks keeps the
    newest one and a field present in only an older uplink survives.
    """
    merged: dict[str, dict[str, TTNBaseValue]] = {}
    for uplink in uplinks:
        device_id = uplink["end_device_ids"]["device_id"]
        parsed = ttn_parse(uplink)
        if not parsed:
            continue
        merged.setdefault(device_id, {}).update(parsed)
    return merged


class TTNHarness:
    """Drive the real integration through real coordinator polling cycles.

    Nothing about the entity or platform layer is mocked: only the network
    call at the very edge (``TTNClient.fetch_data``) is replaced, so every
    assertion runs against Home Assistant's own state machine.
    """

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.fetch_error: Exception | None = None
        self._payload: dict[str, dict[str, TTNBaseValue]] = {}
        self.fetch_count = 0

    async def fetch_data(self) -> dict[str, dict[str, TTNBaseValue]]:
        """Stand in for the storage API call."""
        self.fetch_count += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return self._payload

    async def start(self, *uplinks: dict[str, Any]) -> None:
        """Set up the config entry with an initial fetch window."""
        self._payload = window(*uplinks)
        self.entry.add_to_hass(self.hass)
        assert await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.hass.async_block_till_done()

    async def poll(self, *uplinks: dict[str, Any]) -> None:
        """Advance time past the polling period with a new fetch window."""
        self._payload = window(*uplinks)
        async_fire_time_changed(
            self.hass, dt_util.utcnow() + timedelta(seconds=POLLING_PERIOD_S + 1)
        )
        await self.hass.async_block_till_done()

    async def reload(self, *uplinks: dict[str, Any]) -> None:
        """Restart the config entry, with ``uplinks`` as its first fetch."""
        self._payload = window(*uplinks)
        await self.hass.config_entries.async_reload(self.entry.entry_id)
        await self.hass.async_block_till_done()

    def state(self, entity_id: str) -> str | None:
        """Return the current state string, or None when the entity is absent."""
        state = self.hass.states.get(entity_id)
        return None if state is None else state.state

    def attribute(self, entity_id: str, key: str) -> Any:
        """Return one state attribute, or None when the entity is absent."""
        state = self.hass.states.get(entity_id)
        return None if state is None else state.attributes.get(key)

    def entity_ids(self, prefix: str = "") -> list[str]:
        """Return every entity id this integration created, sorted."""
        return sorted(
            state.entity_id
            for state in self.hass.states.async_all()
            if state.entity_id.startswith(prefix)
        )


@pytest.fixture
def ttn(hass: HomeAssistant) -> Iterator[TTNHarness]:
    """Yield a harness with ``TTNClient`` replaced at the coordinator seam."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=APP_ID,
        unique_id=APP_ID,
        data={
            CONF_HOST: "eu1.cloud.thethings.network",
            CONF_APP_ID: APP_ID,
            CONF_API_KEY: "not-a-real-key",
        },
    )
    harness = TTNHarness(hass, entry)

    def _client_factory(*args: Any, **kwargs: Any) -> MagicMock:
        client = MagicMock()
        client.fetch_data = harness.fetch_data
        return client

    with patch(
        "custom_components.thethingsnetwork_alt.coordinator.TTNClient",
        side_effect=_client_factory,
    ):
        yield harness
