"""Read uplinks from the TTN Storage Integration.

This replaces ``ttn_client.TTNClient``'s HTTP layer. Individual records are
still parsed by ``ttn_client`` (``ttn_parse``), so decoder handling is
unchanged; what changes is everything around that call.

Two upstream behaviours are the reason:

1. ``fetch_data`` advanced its "read up to here" watermark *before* issuing
   the request, so a failed poll narrowed the next window instead of
   re-covering ground it never actually read. With a 60 s period and a 60 s
   margin one failure self-heals, but two consecutive failures dropped every
   uplink in between permanently.
2. It parsed every record before returning any of them, so one record it
   could not read discarded the whole window — every device, not just the one
   that sent the bad record.

Both produced the same user-visible symptom: a field with new data in TTN
that never reaches Home Assistant.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
from typing import Final

from aiohttp import ClientError, ClientResponse, ClientTimeout
from aiohttp.hdrs import ACCEPT, AUTHORIZATION
from ttn_client import TTNAuthError, TTNBaseValue
from ttn_client.parsers import ttn_parse

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

DATA_TYPE = dict[str, dict[str, TTNBaseValue]]

_URL: Final = (
    "https://{hostname}/api/v3/as/applications/"
    "{app_id}/packages/storage/uplink_message{options}"
)

# The storage stream stays open for the whole window, so this is deliberately
# generous. It is a ceiling on a stuck connection, not an expected duration.
_TIMEOUT: Final = ClientTimeout(total=10 * 60)

# Re-request this much either side of the watermark. Uplinks are timestamped
# by the network, not by us, so a record can be stored a little after the
# instant it reports. Overlap is free: the entity layer discards a
# re-delivered uplink by comparing receipt stamps.
_FETCH_MARGIN: Final = timedelta(seconds=60)

# How far back the very first fetch reaches, and the ceiling on any single
# window. Without a ceiling, Home Assistant being off for a week would ask
# TTN for a week in one request.
FIRST_FETCH: Final = timedelta(hours=24)


class TTNStorageError(RuntimeError):
    """The storage API could not be read."""


class TTNStorageClient:
    """Fetch the uplinks stored since the last successful read."""

    def __init__(
        self,
        hass: HomeAssistant,
        hostname: str,
        app_id: str,
        api_key: str,
        first_fetch: timedelta = FIRST_FETCH,
    ) -> None:
        """Initialize the client."""
        self._hass = hass
        self._hostname = hostname
        self._app_id = app_id
        self._api_key = api_key
        self._first_fetch = first_fetch

        # Only ever moved forward by a request that was read to completion.
        self._read_through: datetime | None = None

    @property
    def read_through(self) -> datetime | None:
        """Return the instant the last successful fetch was issued at."""
        return self._read_through

    def _window(self, now: datetime) -> timedelta:
        """Return how far back to ask for, given the current watermark."""
        if self._read_through is None:
            return self._first_fetch
        return min(now - self._read_through + _FETCH_MARGIN, FIRST_FETCH)

    async def fetch_data(self) -> DATA_TYPE:
        """Return uplinks stored since the last successful fetch, by device.

        Raises ``TTNAuthError`` when the credentials are refused and
        ``TTNStorageError`` for any other unusable response. On either, the
        watermark is left alone so the next poll re-covers this window.
        """
        # Captured before the request so records stored while it is in flight
        # are re-covered next time rather than skipped.
        issued_at = dt_util.utcnow()
        window = self._window(issued_at)

        if self._read_through is None:
            _LOGGER.info("First fetch of TTN data: %s", window)
        else:
            _LOGGER.debug("Fetching TTN data for the last %s", window)

        options = f"?last={int(window.total_seconds())}s&order=received_at"
        url = _URL.format(hostname=self._hostname, app_id=self._app_id, options=options)
        session = async_get_clientsession(self._hass)

        values: DATA_TYPE = {}
        skipped = 0
        first_error: str | None = None

        async with session.get(
            url,
            headers={
                ACCEPT: "text/event-stream",
                AUTHORIZATION: f"Bearer {self._api_key}",
            },
            allow_redirects=False,
            timeout=_TIMEOUT,
        ) as response:
            if response.status in (401, 403):
                raise TTNAuthError
            if response.status < 200 or response.status >= 300:
                raise TTNStorageError(
                    f"HTTP {response.status} from the storage API"
                    f"{await self._error_detail(response)}"
                )

            async for raw_line in response.content:
                if (record := self._parse_line(raw_line)) is None:
                    continue
                device_id, parsed = record
                if parsed is None:
                    skipped += 1
                    if first_error is None:
                        first_error = device_id
                    continue
                values.setdefault(device_id, {}).update(parsed)

        if skipped:
            _LOGGER.warning(
                "Skipped %d unreadable record(s) in this TTN window (first: "
                "%s); the readable records in the same window were kept",
                skipped,
                first_error,
            )

        # Only now, with the whole response read, is this window accounted for.
        self._read_through = issued_at
        return values

    @staticmethod
    async def _error_detail(response: ClientResponse) -> str:
        """Return the API's own description of a failure, if it gave one.

        TTN answers a wrong application id with a 404 whose body says so.
        Reporting the status alone would send the user looking at their key.
        """
        try:
            body = await response.text()
        except (ClientError, TimeoutError, UnicodeDecodeError):
            return ""

        try:
            message = json.loads(body).get("message")
        except (AttributeError, ValueError):
            message = None

        detail = message if isinstance(message, str) else body.strip()
        return f": {detail[:200]}" if detail else ""

    @staticmethod
    def _parse_line(
        raw_line: bytes,
    ) -> tuple[str, dict[str, TTNBaseValue] | None] | None:
        """Parse one streamed record.

        Returns ``None`` for a line that carries no record, and
        ``(description, None)`` for one that cannot be read — which is
        counted and skipped so the rest of the window still lands.
        """
        line = raw_line.strip()
        if not line:
            return None

        try:
            payload = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            return ("a record that is not valid JSON", None)

        if not isinstance(payload, dict) or "result" not in payload:
            # TTN reports mid-stream problems as a record without a result.
            return (f"a record without a result: {payload!r:.200}", None)

        result = payload["result"]
        try:
            device_id = str(result["end_device_ids"]["device_id"])
        except (KeyError, IndexError, TypeError):
            return ("a record with no device_id", None)

        try:
            parsed = ttn_parse(result)
        except Exception as err:  # noqa: BLE001 - see below
            # Deliberately broad. This is the boundary around a third-party
            # parser being fed arbitrary decoder output, and the whole point
            # of the boundary is that nothing crossing it can cost the other
            # devices in this window. A decoder returning a list where
            # ttn_client expects an object raises AttributeError, an absent
            # uplink_message raises KeyError, a Sensecap payload missing
            # "err" raises KeyError, non-iterable "messages" raises
            # TypeError; enumerating that set means the next one not on it
            # reintroduces the bug. BaseException is still allowed through,
            # so cancellation and interrupts behave normally.
            return (f"device {device_id}: {err!r}", None)

        return (device_id, parsed or {})
