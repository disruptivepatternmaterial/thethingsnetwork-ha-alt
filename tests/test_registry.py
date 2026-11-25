"""Tests for the TTN Identity Server end-device registry client."""

from __future__ import annotations

import json
from typing import Any

from aiohttp import ClientConnectionError
from aiohttp.hdrs import ACCEPT, AUTHORIZATION
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.thethingsnetwork_alt import registry
from custom_components.thethingsnetwork_alt.registry import (
    PAGE_LIMIT,
    TTNDeviceRegistryClient,
    TTNDeviceRegistryError,
    TTNDeviceRegistryForbidden,
    identity_server_host,
)
from homeassistant.core import HomeAssistant

from .conftest import APP_ID

API_KEY = "NNSXS.TEST_SECRET_DO_NOT_LOG"
IS_HOST = "eu1.cloud.thethings.network"
BASE_URL = f"https://{IS_HOST}/api/v3/applications/{APP_ID}/devices"


def device(device_id: str, name: str | None = None) -> dict[str, Any]:
    """Build one ``EndDevice`` entry in the shape Identity Server returns."""
    entry: dict[str, Any] = {
        "ids": {
            "device_id": device_id,
            "application_ids": {"application_id": APP_ID},
        }
    }
    if name is not None:
        entry["name"] = name
    return entry


def page_body(*devices: dict[str, Any]) -> str:
    """Serialize a list response the way Proto3 JSON does."""
    if not devices:
        return "{}"
    return json.dumps({"end_devices": list(devices)})


def make_client(
    hass: HomeAssistant, host: str = "nam1.cloud.thethings.network"
) -> TTNDeviceRegistryClient:
    """Return a registry client configured against ``host``."""
    return TTNDeviceRegistryClient(hass, host, APP_ID, API_KEY)


@pytest.mark.parametrize(
    ("configured_host", "expected_is_host"),
    [
        ("nam1.cloud.thethings.network", "eu1.cloud.thethings.network"),
        ("au1.cloud.thethings.network", "eu1.cloud.thethings.network"),
        ("eu1.cloud.thethings.network", "eu1.cloud.thethings.network"),
        ("NAM1.Cloud.TheThings.Network", "eu1.cloud.thethings.network"),
        ("https://nam1.cloud.thethings.network/", "eu1.cloud.thethings.network"),
        ("nam1.cloud.thethings.network:443", "eu1.cloud.thethings.network"),
        (
            "acme.nam1.cloud.thethings.industries",
            "acme.eu1.cloud.thethings.industries",
        ),
        (
            "my-tenant_1.au1.cloud.thethings.industries",
            "my-tenant_1.eu1.cloud.thethings.industries",
        ),
        ("ttn.example.com", "ttn.example.com"),
        ("ttn.internal:8443", "ttn.internal:8443"),
    ],
)
def test_identity_server_host_derivation(
    configured_host: str, expected_is_host: str
) -> None:
    """Sandbox and Cloud route to eu1; self-hosted hosts stay untouched."""
    assert identity_server_host(configured_host) == expected_is_host


async def test_fetch_names_parses_named_unnamed_and_blank_devices(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Named devices are trimmed; missing and whitespace-only names are skipped."""
    aioclient_mock.get(
        BASE_URL,
        text=page_body(
            device("dev-named", "  East Gate Sensor  "),
            device("dev-unnamed"),
            device("dev-blank", "   "),
            device("dev-second", "Sauna Sensor"),
        ),
    )

    names = await make_client(hass, "nam1.cloud.thethings.network").fetch_names()

    assert names == {
        "dev-named": "East Gate Sensor",
        "dev-second": "Sauna Sensor",
    }

    assert aioclient_mock.call_count == 1
    _method, url, _data, headers = aioclient_mock.mock_calls[0]
    assert url.host == IS_HOST
    assert url.path == f"/api/v3/applications/{APP_ID}/devices"
    assert dict(url.query) == {
        "field_mask": "name",
        "limit": str(PAGE_LIMIT),
        "page": "1",
    }
    assert headers[AUTHORIZATION] == f"Bearer {API_KEY}"
    assert headers[ACCEPT] == "application/json"


async def test_empty_application_returns_empty_dict(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Proto3 JSON omits end_devices altogether when an application is empty."""
    aioclient_mock.get(BASE_URL, text="{}")

    assert await make_client(hass).fetch_names() == {}


async def test_multiple_pages_are_combined_until_short_page(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A full first page triggers page 2; a short second page ends pagination."""
    first = [device(f"dev-{i}", f"Sensor {i}") for i in range(PAGE_LIMIT)]
    second = [device("dev-last", "Final Sensor")]

    aioclient_mock.get(
        BASE_URL,
        params={"field_mask": "name", "limit": str(PAGE_LIMIT), "page": "1"},
        text=page_body(*first),
    )
    aioclient_mock.get(
        BASE_URL,
        params={"field_mask": "name", "limit": str(PAGE_LIMIT), "page": "2"},
        text=page_body(*second),
    )

    names = await make_client(hass).fetch_names()

    assert aioclient_mock.call_count == 2
    assert len(names) == PAGE_LIMIT + 1
    assert names["dev-0"] == "Sensor 0"
    assert names["dev-last"] == "Final Sensor"


async def test_unbounded_pages_abort_with_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, monkeypatch: Any
) -> None:
    """A server returning full pages forever stops at MAX_PAGES."""
    monkeypatch.setattr(registry, "MAX_PAGES", 2)
    full = page_body(*[device(f"dev-{i}", f"Sensor {i}") for i in range(PAGE_LIMIT)])

    async def always_full(method: str, url: Any, data: Any) -> Any:
        return AiohttpClientMockResponse(method, url, text=full)

    aioclient_mock.get(BASE_URL, side_effect=always_full)

    with pytest.raises(TTNDeviceRegistryError, match="still returning full pages"):
        await make_client(hass).fetch_names()

    assert aioclient_mock.call_count == 2


async def test_403_raises_forbidden_without_leaking_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """HTTP 403 raises the dedicated permission exception."""
    aioclient_mock.get(
        BASE_URL,
        status=403,
        text=json.dumps({"message": "no_application_rights"}),
    )

    with pytest.raises(TTNDeviceRegistryForbidden) as exc_info:
        await make_client(hass).fetch_names()

    assert API_KEY not in str(exc_info.value)


@pytest.mark.parametrize("status", [401, 404, 500])
async def test_other_http_errors_raise_general_registry_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """401, 404 and 500 raise TTNDeviceRegistryError, not Forbidden."""
    aioclient_mock.get(
        BASE_URL,
        status=status,
        text=json.dumps({"message": "boom"}),
    )

    with pytest.raises(TTNDeviceRegistryError) as exc_info:
        await make_client(hass).fetch_names()

    assert not isinstance(exc_info.value, TTNDeviceRegistryForbidden)
    assert API_KEY not in str(exc_info.value)


@pytest.mark.parametrize(
    "bad_body",
    [
        "{not valid json",
        "[]",
        json.dumps({"end_devices": "not-a-list"}),
        json.dumps({"end_devices": [{"ids": {}}]}),
        json.dumps({"end_devices": [{"ids": {"device_id": "dev-1"}, "name": 123}]}),
    ],
)
async def test_malformed_payload_raises_general_registry_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, bad_body: str
) -> None:
    """Unusable JSON shapes raise TTNDeviceRegistryError."""
    aioclient_mock.get(BASE_URL, text=bad_body)

    with pytest.raises(TTNDeviceRegistryError):
        await make_client(hass).fetch_names()


async def test_connection_failure_raises_general_registry_error_without_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Transport errors wrap into TTNDeviceRegistryError and never expose the key."""
    aioclient_mock.get(BASE_URL, exc=ClientConnectionError(API_KEY))

    with pytest.raises(TTNDeviceRegistryError) as exc_info:
        await make_client(hass).fetch_names()

    assert API_KEY not in str(exc_info.value)


async def test_app_id_with_reserved_url_chars_is_percent_encoded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Special characters in app_id cannot inject path or query segments."""
    encoded_url = f"https://{IS_HOST}/api/v3/applications/app%2F1%3Fx%3D2/devices"
    aioclient_mock.get(encoded_url, text="{}")

    client = TTNDeviceRegistryClient(
        hass, "nam1.cloud.thethings.network", "app/1?x=2", API_KEY
    )
    assert await client.fetch_names() == {}
    assert aioclient_mock.call_count == 1


async def test_429_retries_after_advised_wait_and_succeeds(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, waits: list[float]
) -> None:
    """A transient HTTP 429 backs off for X-Rate-Limit-Retry and retries."""
    responses = [
        AiohttpClientMockResponse(
            "GET",
            BASE_URL,
            status=429,
            text='{"message":"rate_limited"}',
            headers={"X-Rate-Limit-Retry": "4"},
        ),
        AiohttpClientMockResponse(
            "GET",
            BASE_URL,
            status=200,
            text=page_body(device("dev-1", "Recovered Sensor")),
        ),
    ]

    async def sequence(method: str, url: Any, data: Any) -> Any:
        return responses.pop(0)

    aioclient_mock.get(BASE_URL, side_effect=sequence)

    assert await make_client(hass).fetch_names() == {"dev-1": "Recovered Sensor"}
    assert waits == [4.0]


async def test_429_exceeding_max_wait_fails_without_sleeping(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, waits: list[float]
) -> None:
    """An unreasonable Retry header fails immediately rather than stalling."""
    aioclient_mock.get(
        BASE_URL,
        status=429,
        headers={"X-Rate-Limit-Retry": "120"},
    )

    with pytest.raises(TTNDeviceRegistryError, match="longer than 60 s"):
        await make_client(hass).fetch_names()

    assert waits == []
