"""Read end-device display names from The Things Stack Identity Server."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
import json
import math
import re
from typing import Any, Final
from urllib.parse import quote

from aiohttp import ClientError, ClientResponse, ClientTimeout
from aiohttp.hdrs import ACCEPT, AUTHORIZATION

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_URL: Final = (
    "https://{hostname}/api/v3/applications/"
    "{app_id}/devices?field_mask=name&limit={limit}&page={page}"
)

_TIMEOUT: Final = ClientTimeout(total=30)

# The Identity Server's own ceiling on end devices returned per page.
PAGE_LIMIT: Final = 1000

# A backstop against a server that never returns a short page.
MAX_PAGES: Final = 100

MAX_RATE_LIMITED_RETRIES: Final = 2
_DEFAULT_RATE_LIMIT_WAIT: Final = timedelta(seconds=6)
MAX_RATE_LIMIT_WAIT: Final = timedelta(seconds=60)
_RESET_HEADER: Final = "x-rate-limit-reset"
_RETRY_HEADER: Final = "x-rate-limit-retry"

_MAX_QUOTED: Final = 200

_TTN_SANDBOX_HOST_RE: Final = re.compile(
    r"^[a-z0-9-]+\.cloud\.thethings\.network(?::\d+)?$",
    re.IGNORECASE,
)
_TTS_CLOUD_HOST_RE: Final = re.compile(
    r"^([a-z0-9_-]+)\.[a-z0-9-]+\.cloud\.thethings\.industries(?::\d+)?$",
    re.IGNORECASE,
)


class TTNDeviceRegistryError(RuntimeError):
    """The end-device registry could not be read."""


class TTNDeviceRegistryForbidden(TTNDeviceRegistryError):
    """The API key lacks permission to read the application's end devices."""


class _RateLimited(Exception):
    """The Identity Server answered HTTP 429 with an advised backoff."""

    def __init__(self, wait: timedelta) -> None:
        """Store the advised wait duration."""
        super().__init__(f"rate limited for {wait.total_seconds():g} s")
        self.wait = wait


async def _wait(duration: timedelta) -> None:
    """Pause for the rate limiter; exposed as a seam so tests do not sleep."""
    await asyncio.sleep(duration.total_seconds())


def identity_server_host(host: str) -> str:
    """Return the Identity Server hostname corresponding to a cluster host.

    On The Things Stack Sandbox and Cloud, Application Server storage lives on
    the regional cluster while the Identity Server lives only in ``eu1``. Self-
    hosted deployments serve both from the configured host.
    """
    cleaned = host.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    if _TTN_SANDBOX_HOST_RE.match(cleaned):
        return "eu1.cloud.thethings.network"
    if match := _TTS_CLOUD_HOST_RE.match(cleaned):
        return f"{match.group(1).lower()}.eu1.cloud.thethings.industries"
    return cleaned


class TTNDeviceRegistryClient:
    """Fetch end-device friendly names for an application."""

    def __init__(
        self,
        hass: HomeAssistant,
        hostname: str,
        app_id: str,
        api_key: str,
    ) -> None:
        """Initialize the registry client."""
        self._hass = hass
        self._hostname = identity_server_host(hostname)
        self._app_id = app_id
        self._api_key = api_key

    @property
    def hostname(self) -> str:
        """Return the Identity Server hostname in use."""
        return self._hostname

    async def fetch_names(self) -> dict[str, str]:
        """Return ``{device_id: name}`` for every named device in the app."""
        names: dict[str, str] = {}
        for page in range(1, MAX_PAGES + 1):
            count = await self._fetch_page_within_limit(page, names)
            if count < PAGE_LIMIT:
                return names

        raise TTNDeviceRegistryError(
            f"the device registry was still returning full pages after "
            f"{MAX_PAGES} requests"
        )

    async def _fetch_page_within_limit(self, page: int, names: dict[str, str]) -> int:
        """Fetch one page, retrying bounded HTTP 429 responses."""
        for _ in range(MAX_RATE_LIMITED_RETRIES):
            try:
                return await self._fetch_page(page, names)
            except _RateLimited as limited:
                await _wait(limited.wait)
        try:
            return await self._fetch_page(page, names)
        except _RateLimited as limited:
            raise TTNDeviceRegistryError(
                f"the device registry was still rate limiting after "
                f"{MAX_RATE_LIMITED_RETRIES} retries"
            ) from limited

    async def _fetch_page(self, page: int, names: dict[str, str]) -> int:
        """Fold one page of end devices into ``names`` and return record count."""
        url = _URL.format(
            hostname=self._hostname,
            app_id=quote(self._app_id, safe=""),
            limit=PAGE_LIMIT,
            page=page,
        )
        try:
            async with async_get_clientsession(self._hass).get(
                url,
                headers={
                    ACCEPT: "application/json",
                    AUTHORIZATION: f"Bearer {self._api_key}",
                },
                allow_redirects=False,
                timeout=_TIMEOUT,
            ) as response:
                await self._raise_for_status(response)
                body = await response.text()
        except (TTNDeviceRegistryError, _RateLimited):
            raise
        except (ClientError, TimeoutError, UnicodeDecodeError) as err:
            raise TTNDeviceRegistryError(
                f"could not read the device registry at {self._hostname}: "
                f"{type(err).__name__}"
            ) from err

        return self._parse_page(body, names)

    @classmethod
    async def _raise_for_status(cls, response: ClientResponse) -> None:
        """Reject a non-2xx response with a typed registry exception."""
        if response.status == 403:
            raise TTNDeviceRegistryForbidden(
                "HTTP 403 from the device registry: API key needs "
                "'View devices in application'"
            )
        if response.status == 429:
            raise cls._rate_limited(response)
        if not 200 <= response.status < 300:
            raise TTNDeviceRegistryError(
                f"HTTP {response.status} from the device registry"
                f"{await cls._error_detail(response)}"
            )

    @staticmethod
    def _rate_limited(response: ClientResponse) -> Exception:
        """Translate HTTP 429 headers into a bounded retry wait or an error."""
        headers = {str(name).lower(): value for name, value in response.headers.items()}
        wait = next(
            (
                seconds
                for seconds in (
                    _header_seconds(headers, _RETRY_HEADER),
                    _header_seconds(headers, _RESET_HEADER),
                )
                if seconds
            ),
            _DEFAULT_RATE_LIMIT_WAIT,
        )
        if wait > MAX_RATE_LIMIT_WAIT:
            return TTNDeviceRegistryError(
                f"HTTP 429 from the device registry asking for a "
                f"{wait.total_seconds():g} s wait, longer than "
                f"{MAX_RATE_LIMIT_WAIT.total_seconds():g} s"
            )
        return _RateLimited(wait)

    @staticmethod
    def _parse_page(body: str, names: dict[str, str]) -> int:
        """Parse one JSON page, fold named devices into ``names``, return count."""
        try:
            payload = json.loads(body)
        except ValueError as err:
            raise TTNDeviceRegistryError(
                f"device registry returned invalid JSON: {err}"
            ) from err

        if not isinstance(payload, dict):
            raise TTNDeviceRegistryError(
                f"device registry returned {type(payload).__name__}, expected object"
            )

        # Proto3 JSON omits empty repeated fields, so an app with no devices is {}.
        raw_devices = payload.get("end_devices")
        if raw_devices is None:
            return 0
        if not isinstance(raw_devices, list):
            raise TTNDeviceRegistryError(
                f"device registry end_devices was {type(raw_devices).__name__}, "
                f"expected list"
            )

        for entry in raw_devices:
            device_id, name = TTNDeviceRegistryClient._parse_device(entry)
            if name:
                names[device_id] = name

        return len(raw_devices)

    @staticmethod
    def _parse_device(entry: Any) -> tuple[str, str | None]:
        """Extract ``(device_id, stripped_name_or_none)`` from one device object."""
        if not isinstance(entry, dict):
            raise TTNDeviceRegistryError(
                f"device registry entry was {type(entry).__name__}, expected object"
            )
        ids = entry.get("ids")
        device_id = ids.get("device_id") if isinstance(ids, dict) else None
        if not isinstance(device_id, str) or not device_id:
            raise TTNDeviceRegistryError("device registry entry had no ids.device_id")

        raw_name = entry.get("name")
        if raw_name is None:
            return device_id, None
        if not isinstance(raw_name, str):
            raise TTNDeviceRegistryError(
                f"device {device_id} name was {type(raw_name).__name__}, "
                f"expected string"
            )
        stripped = raw_name.strip()
        return device_id, stripped or None

    @staticmethod
    async def _error_detail(response: ClientResponse) -> str:
        """Return a short API error message from ``response``, if readable."""
        try:
            body = await response.text()
        except (ClientError, TimeoutError, UnicodeDecodeError):
            return ""

        try:
            payload = json.loads(body)
        except ValueError:
            payload = None

        message = payload.get("message") if isinstance(payload, dict) else None
        detail = message if isinstance(message, str) else body.strip()
        return f": {detail[:_MAX_QUOTED]}" if detail else ""


def _header_seconds(headers: Mapping[str, str], name: str) -> timedelta | None:
    """Return a non-negative finite duration from ``headers[name]``, if present."""
    try:
        seconds = float(headers[name])
    except (KeyError, TypeError, ValueError):
        return None
    return (
        timedelta(seconds=seconds) if math.isfinite(seconds) and seconds >= 0 else None
    )
