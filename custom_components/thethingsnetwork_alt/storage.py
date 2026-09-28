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

A third lives in the API itself: one request returns at most
``PAGE_LIMIT`` records, oldest first, and says nothing when it stops early.
A window holding more than that was read only up to its first thousand
records, the watermark then moved to "now", and everything after the cut was
never read. On a restart that made every field show the value it had hours
earlier, as if it were current.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Final, NamedTuple

from aiohttp import ClientError, ClientResponse, ClientTimeout
from aiohttp.hdrs import ACCEPT, AUTHORIZATION
from ttn_client import TTNAuthError, TTNBaseValue
from ttn_client.parsers import ttn_parse

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .timestamp import parse_ttn_timestamp

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

# How far back the very first fetch reaches.
FIRST_FETCH: Final = timedelta(hours=24)

# The ceiling on any single window. Without it, Home Assistant having been
# off for a week would ask TTN for a week in one request.
MAX_WINDOW: Final = timedelta(hours=24)

# The storage API's own ceiling on records per request: it answers a larger
# ``limit`` with HTTP 400, and without one it silently stops at this many.
PAGE_LIMIT: Final = 1000

# Each follow-up page starts this far before the last record already read.
# ``after`` is exclusive, so without overlap a record sharing the boundary
# stamp at a precision we truncate away could be skipped. Re-reading is free
# for the same reason _FETCH_MARGIN is.
_PAGE_OVERLAP: Final = timedelta(seconds=1)

# A backstop against a response that never shrinks below a full page. At
# PAGE_LIMIT per page this is far beyond one MAX_WINDOW of any real app.
MAX_PAGES: Final = 100

# Truncation for anything quoted back from the API into a log line.
_MAX_QUOTED = 200


class TTNStorageError(RuntimeError):
    """The storage API could not be read."""


class _Page(NamedTuple):
    """What one response contained, beyond the values folded out of it."""

    records: int
    last_received_at: datetime | None


class _UnreadableRecord(Exception):
    """One streamed record could not be read.

    Raised per record and handled per record: the point of this type is that
    the other devices in the same window are unaffected.
    """

    def __init__(self, message: str, received_at: datetime | None = None) -> None:
        """Keep the receipt time, which paging needs even from a bad record."""
        super().__init__(message)
        self.received_at = received_at


async def async_validate_credentials(
    hass: HomeAssistant, hostname: str, app_id: str, api_key: str
) -> None:
    """Check a host, application and key without pulling any uplinks.

    Raises ``TTNAuthError`` if the credentials are refused and
    ``TTNStorageError`` if the application's stored uplinks cannot be read.
    """
    client = TTNStorageClient(hass, hostname, app_id, api_key, first_fetch=timedelta(0))
    await client.fetch_data()


class TTNStorageClient:
    """Fetch the uplinks stored since the last successful read."""

    def __init__(
        self,
        hass: HomeAssistant,
        hostname: str,
        app_id: str,
        api_key: str,
        *,
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

        values: DATA_TYPE = {}
        after: datetime | None = None
        for _ in range(MAX_PAGES):
            page = await self._fetch_page(self._url(window, after), values)
            if page.records < PAGE_LIMIT:
                break
            after = self._next_page_after(page, after)
        else:
            raise TTNStorageError(
                f"the storage API was still returning full pages after "
                f"{MAX_PAGES} requests for one window"
            )

        # Only here, with every page read, is this window accounted for.
        # Anything raised above leaves the watermark where it was.
        self._read_through = issued_at
        return values

    async def _fetch_page(self, url: str, values: DATA_TYPE) -> _Page:
        """Fold one request's records into ``values``."""
        async with async_get_clientsession(self._hass).get(
            url,
            headers={
                ACCEPT: "text/event-stream",
                AUTHORIZATION: f"Bearer {self._api_key}",
            },
            allow_redirects=False,
            timeout=_TIMEOUT,
        ) as response:
            await self._raise_for_status(response)
            return await self._consume(response, values)

    @staticmethod
    def _next_page_after(page: _Page, previous: datetime | None) -> datetime:
        """Return where the page after a full one must start.

        Raises ``TTNStorageError`` when paging could not make progress, since
        returning what was read so far would silently drop the rest.
        """
        if page.last_received_at is None:
            raise TTNStorageError(
                "a full page from the storage API had no readable received_at "
                "to continue from"
            )
        after = page.last_received_at - _PAGE_OVERLAP
        if previous is not None and after <= previous:
            raise TTNStorageError(
                f"more than {PAGE_LIMIT} records share one second at "
                f"{page.last_received_at.isoformat()}; cannot page past them"
            )
        return after

    def _window(self, now: datetime) -> timedelta:
        """Return how far back to ask for, given the current watermark."""
        if self._read_through is None:
            _LOGGER.info("First fetch of TTN data: %s", self._first_fetch)
            return self._first_fetch

        window = min(now - self._read_through + _FETCH_MARGIN, MAX_WINDOW)
        _LOGGER.debug("Fetching TTN data for the last %s", window)
        return window

    def _url(self, window: timedelta, after: datetime | None = None) -> str:
        """Return the storage API URL for one page of a window."""
        if after is None:
            start = f"last={int(window.total_seconds())}s"
        else:
            # A literal "Z", because "+00:00" arrives as " 00:00" in a query.
            start = f"after={after.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"
        options = f"?{start}&order=received_at&limit={PAGE_LIMIT}"
        return _URL.format(
            hostname=self._hostname, app_id=self._app_id, options=options
        )

    @classmethod
    async def _raise_for_status(cls, response: ClientResponse) -> None:
        """Reject a response that cannot be read, naming which kind it is."""
        if response.status in (401, 403):
            raise TTNAuthError
        if not 200 <= response.status < 300:
            raise TTNStorageError(
                f"HTTP {response.status} from the storage API"
                f"{await cls._error_detail(response)}"
            )

    @classmethod
    async def _consume(cls, response: ClientResponse, values: DATA_TYPE) -> _Page:
        """Fold a streamed response into ``values``, newest value per field.

        Records stream oldest-first, so a later record's value for a field
        replaces an earlier one while a field only an earlier record carried
        survives.

        A record with nothing decoded — what every device without a payload
        formatter sends — contributes no fields and no device entry. It must
        not register the device either: the platforms treat a device present
        here as one worth adding entities for, and the diagnostics they would
        add can never hold a value, since each is read off a parsed value's
        uplink and there are none.
        """
        records = 0
        last_received_at: datetime | None = None
        skipped: list[str] = []

        async for raw_line in response.content:
            if not (line := raw_line.strip()):
                continue
            # Unreadable records still count: the API's page limit does.
            records += 1
            try:
                device_id, received_at, parsed = cls._parse_record(line)
            except _UnreadableRecord as err:
                skipped.append(str(err))
                last_received_at = err.received_at or last_received_at
                continue
            last_received_at = received_at or last_received_at
            if parsed:
                values.setdefault(device_id, {}).update(parsed)

        if skipped:
            _LOGGER.warning(
                "Skipped %d unreadable record(s) in this TTN window (first: "
                "%s); the readable records in the same window were kept",
                len(skipped),
                skipped[0],
            )
        return _Page(records, last_received_at)

    @staticmethod
    def _parse_record(
        line: bytes,
    ) -> tuple[str, datetime | None, dict[str, TTNBaseValue]]:
        """Parse one streamed record into its device id, receipt time and values.

        Raises ``_UnreadableRecord``, carrying a description for the log and
        the receipt time when the record had one, for anything that cannot be
        read.
        """
        try:
            payload = json.loads(line)
        except (UnicodeDecodeError, ValueError) as err:
            raise _UnreadableRecord(f"a record that is not valid JSON: {err}") from err

        if not isinstance(payload, dict) or "result" not in payload:
            # TTN reports mid-stream problems as a record without a result.
            raise _UnreadableRecord(
                f"a record without a result: {payload!r:.{_MAX_QUOTED}}"
            )

        result = payload["result"]
        received_at = (
            parse_ttn_timestamp(result.get("received_at"))
            if isinstance(result, dict)
            else None
        )
        try:
            device_id = str(result["end_device_ids"]["device_id"])
        except (KeyError, IndexError, TypeError) as err:
            raise _UnreadableRecord(
                f"a record with no device_id: {err!r}", received_at
            ) from err

        try:
            return device_id, received_at, ttn_parse(result)
        except Exception as err:
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
            raise _UnreadableRecord(
                f"device {device_id}: {err!r}", received_at
            ) from err

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
            payload = json.loads(body)
        except ValueError:
            payload = None

        message = payload.get("message") if isinstance(payload, dict) else None
        detail = message if isinstance(message, str) else body.strip()
        return f": {detail[:_MAX_QUOTED]}" if detail else ""
