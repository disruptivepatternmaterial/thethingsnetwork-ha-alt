"""Tests for the config and reauth flows.

The flow validates credentials by actually reading the storage API, so these
run against a mocked HTTP layer rather than a mocked client — the mapping
from what TTN answers to what the user is told is the part worth pinning.
"""

from __future__ import annotations

from typing import Any

from aiohttp import ClientConnectionError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.thethingsnetwork_alt.const import CONF_APP_ID, DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_API_KEY, CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import APP_ID

HOST = "eu1.cloud.thethings.network"
URL = f"https://{HOST}/api/v3/as/applications/{APP_ID}/packages/storage/uplink_message"
INPUT = {CONF_HOST: HOST, CONF_APP_ID: APP_ID, CONF_API_KEY: "a-key"}


async def submit(hass: HomeAssistant, **overrides: Any) -> dict[str, Any]:
    """Run the user step with the given form values."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**INPUT, **overrides}
    )
    # Creating the entry sets it up, which polls; let that finish so it is
    # not still in flight at teardown.
    await hass.async_block_till_done()
    return result


async def test_valid_credentials_create_the_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The happy path."""
    aioclient_mock.get(URL, text="")

    result = await submit(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == APP_ID
    assert result["data"] == INPUT


async def test_validation_does_not_pull_a_day_of_uplinks(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Validating a form is not a reason to download 24h of history.

    The first call is the flow's check; the entry being set up afterwards is
    what legitimately reaches back a day.
    """
    aioclient_mock.get(URL, text="")

    await submit(hass)

    assert str(aioclient_mock.mock_calls[0][1].query["last"]) == "0s"
    assert str(aioclient_mock.mock_calls[1][1].query["last"]) == "86400s"


@pytest.mark.parametrize("status", [401, 403])
async def test_refused_key_reports_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """Only a refused credential may say the authentication is wrong."""
    aioclient_mock.get(URL, status=status)

    result = await submit(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.parametrize("status", [404, 429, 500, 503])
async def test_unreadable_application_reports_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """A mistyped application id must not be reported as a bad key.

    TTN answers an application it cannot find with 404. Mapping every 4xx to
    invalid authentication sent the user to reissue a working API key.
    """
    aioclient_mock.get(URL, status=status)

    result = await submit(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_unreachable_host_reports_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A network failure is not an unexpected error either."""
    aioclient_mock.get(URL, exc=ClientConnectionError("nope"))

    result = await submit(hass)

    assert result["errors"] == {"base": "cannot_connect"}


async def test_a_pasted_url_is_accepted_as_a_host(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Users paste the console URL; the API needs a bare hostname."""
    aioclient_mock.get(URL, text="")

    result = await submit(hass, **{CONF_HOST: f"  https://{HOST}/  "})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST


async def test_second_entry_for_one_application_aborts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """One config entry per application."""
    aioclient_mock.get(URL, text="")
    MockConfigEntry(domain=DOMAIN, unique_id=APP_ID, data=INPUT).add_to_hass(hass)

    result = await submit(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_the_key_in_place(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Reauth must rewrite the existing entry, not create a second one."""
    aioclient_mock.get(URL, text="")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=APP_ID,
        data={**INPUT, CONF_API_KEY: "the-expired-key"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**INPUT, CONF_API_KEY: "a-fresh-key"}
    )
    # Reauth reloads the entry; let the reload settle before teardown.
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "a-fresh-key"
