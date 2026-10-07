"""Tests for the storage API client.

These drive the real streaming code against a mocked HTTP layer, because the
two defects they cover live in the seam this integration used to delegate:
what happens to the *rest* of a window when one record is unreadable, and
what happens to the window a failed request never read.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from ttn_client import TTNAuthError
from ttn_client.parsers import ttn_parse

from custom_components.thethingsnetwork_alt import storage
from custom_components.thethingsnetwork_alt.storage import (
    FIRST_FETCH,
    MAX_RATE_LIMIT_WAIT,
    MAX_RATE_LIMITED_RETRIES,
    MAX_WINDOW,
    PAGE_LIMIT,
    RATE_LIMIT_RESERVE,
    TTNStorageClient,
    TTNStorageError,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import APP_ID, DEVICE_ID, make_uplink

HOST = "eu1.cloud.thethings.network"
URL = f"https://{HOST}/api/v3/as/applications/{APP_ID}/packages/storage/uplink_message"

# Edits one raw record in place to make it unreadable a particular way.
type Mutation = Callable[[dict[str, Any]], None]


def stream(*records: dict[str, Any]) -> str:
    """Render the newline-delimited record stream the storage API returns."""
    return "".join(json.dumps({"result": record}) + "\n" for record in records)


def raw_stream(*lines: str) -> str:
    """Render a stream of literal lines, for records that are not records."""
    return "".join(line + "\n" for line in lines)


def client(hass: HomeAssistant, **kwargs: Any) -> TTNStorageClient:
    """Return a client pointed at the mocked host."""
    return TTNStorageClient(hass, HOST, APP_ID, "not-a-real-key", **kwargs)


def requested_window(mock: AiohttpClientMocker, index: int = -1) -> int:
    """Return the ``last=Ns`` seconds one request asked for."""
    _method, url, _data, _headers = mock.mock_calls[index]
    return int(str(url.query["last"]).removesuffix("s"))


async def test_good_stream_is_parsed_per_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The happy path: two devices, folded into one dict each."""
    aioclient_mock.get(
        URL,
        text=stream(
            make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}),
            make_uplink("2026-09-01T00:00:30Z", {"humidity": 44}, "dev-2"),
        ),
    )

    data = await client(hass).fetch_data()

    assert data[DEVICE_ID]["temperature"].value == 20.5
    assert data["dev-2"]["humidity"].value == 44


async def test_newest_value_wins_within_one_window(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Records stream oldest-first, so the last one read is the newest."""
    aioclient_mock.get(
        URL,
        text=stream(
            make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}),
            make_uplink("2026-09-01T00:00:30Z", {"temperature": 21.5}),
        ),
    )

    data = await client(hass).fetch_data()

    assert data[DEVICE_ID]["temperature"].value == 21.5


async def test_a_field_seen_only_in_an_older_record_survives(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Folding must not drop a field the newest record happens to omit."""
    aioclient_mock.get(
        URL,
        text=stream(
            make_uplink("2026-09-01T00:00:00Z", {"battery": 3.9}),
            make_uplink("2026-09-01T00:00:30Z", {"temperature": 21.5}),
        ),
    )

    data = await client(hass).fetch_data()

    assert data[DEVICE_ID]["battery"].value == 3.9
    assert data[DEVICE_ID]["temperature"].value == 21.5


# --- One bad record must not cost the window -------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("{not json at all", id="malformed-json"),
        pytest.param(json.dumps({"error": "stream aborted"}), id="no-result"),
        pytest.param(json.dumps({"result": {"uplink_message": {}}}), id="no-device"),
    ],
)
async def test_bad_record_does_not_discard_its_neighbours(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    bad: str,
) -> None:
    """Every readable record in the same response must still land.

    The whole response used to be parsed before any of it was returned, so a
    single record TTN could not describe cost every device in that window.
    """
    aioclient_mock.get(
        URL,
        text=(
            stream(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))
            + raw_stream(bad)
            + stream(make_uplink("2026-09-01T00:00:30Z", {"humidity": 44}, "dev-2"))
        ),
    )

    with caplog.at_level(logging.WARNING):
        data = await client(hass).fetch_data()

    assert data[DEVICE_ID]["temperature"].value == 20.5
    assert data["dev-2"]["humidity"].value == 44
    assert "Skipped 1 unreadable record" in caplog.text


def drop(key: str) -> Mutation:
    """Return a mutation removing a top-level key."""
    return lambda record: record.pop(key)


def replace(key: str, value: Any) -> Mutation:
    """Return a mutation replacing a top-level key."""

    def mutate(record: dict[str, Any]) -> None:
        record[key] = value

    return mutate


def set_decoded(value: Any) -> Mutation:
    """Return a mutation replacing decoded_payload wholesale."""

    def mutate(record: dict[str, Any]) -> None:
        record["uplink_message"]["decoded_payload"] = value

    return mutate


def sensecap(decoded: dict[str, Any]) -> Mutation:
    """Return a mutation making a record parse down the Sensecap path."""

    def mutate(record: dict[str, Any]) -> None:
        record["uplink_message"]["version_ids"] = {"brand_id": "sensecap"}
        record["uplink_message"]["decoded_payload"] = decoded

    return mutate


# Records that make ttn_parse raise, each by a route a real deployment can
# produce, and each raising a different exception type. That spread is the
# reason the client's per-record guard cannot be a curated exception list.
HOSTILE: dict[str, Mutation] = {
    "no-uplink-message": drop("uplink_message"),  # KeyError
    "null-uplink-message": replace("uplink_message", None),  # AttributeError
    "decoded-payload-is-a-list": set_decoded([]),  # AttributeError
    "decoded-payload-is-a-string": set_decoded("T=21"),  # AttributeError
    "sensecap-without-err": sensecap({"valid": True}),  # KeyError
    "sensecap-messages-not-a-list": sensecap(
        {"valid": True, "err": 0, "payload": "x", "messages": 5}  # TypeError
    ),
}


@pytest.mark.parametrize("mutate", HOSTILE.values(), ids=HOSTILE.keys())
async def test_record_the_parser_rejects_does_not_abort_the_window(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mutate: Mutation
) -> None:
    """A decoder one device disagrees with must not silence the others."""
    hostile = make_uplink("2026-09-01T00:00:10Z", {"temperature": 21.0})
    mutate(hostile)

    aioclient_mock.get(
        URL,
        text=stream(
            make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}),
            hostile,
            make_uplink("2026-09-01T00:00:30Z", {"humidity": 44}, "dev-2"),
        ),
    )

    data = await client(hass).fetch_data()

    assert data[DEVICE_ID]["temperature"].value == 20.5
    assert data["dev-2"]["humidity"].value == 44


@pytest.mark.parametrize("mutate", HOSTILE.values(), ids=HOSTILE.keys())
def test_hostile_record_really_does_break_the_parser(mutate: Mutation) -> None:
    """Guard the guard: a mutation that stopped raising proves nothing.

    These records are only worth streaming if ``ttn_parse`` still rejects
    them, and a ttn_client upgrade could quietly change that — leaving the
    tests above passing for the wrong reason.
    """
    record = make_uplink("2026-09-01T00:00:10Z", {"temperature": 21.0})
    mutate(record)

    with pytest.raises(Exception):  # noqa: B017 - any type counts, that is the point
        ttn_parse(record)


async def test_uplink_with_nothing_decoded_creates_no_device_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A device with no payload formatter must not become a device here.

    ``ttn_parse`` returns no values for an uplink with no ``decoded_payload``,
    which is what every device without a payload formatter sends. Recording
    the device anyway gave it a device registry entry, four diagnostic
    sensors and a device tracker — all permanently unknown, because each of
    those reads its value off a parsed value's uplink and there are none.
    """
    bare = make_uplink("2026-09-01T00:00:00Z", {})
    del bare["uplink_message"]["decoded_payload"]

    aioclient_mock.get(
        URL,
        text=stream(
            bare,
            make_uplink("2026-09-01T00:00:30Z", {"humidity": 44}, "dev-2"),
        ),
    )

    data = await client(hass).fetch_data()

    assert DEVICE_ID not in data
    assert data["dev-2"]["humidity"].value == 44


async def test_a_window_of_only_bad_records_is_not_an_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Nothing readable is an empty window, not a failed fetch."""
    aioclient_mock.get(URL, text=raw_stream("{not json", "{also not json"))

    assert await client(hass).fetch_data() == {}


async def test_blank_lines_are_not_counted_as_records(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The stream is padded with keep-alive newlines."""
    aioclient_mock.get(
        URL,
        text="\n\n"
        + stream(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))
        + "\n\n",
    )

    data = await client(hass).fetch_data()

    assert data[DEVICE_ID]["temperature"].value == 20.5


# --- The watermark must only move on a fetch that was actually read --------


async def test_first_fetch_reaches_back_a_day(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With no watermark there is no lower bound but the configured one."""
    aioclient_mock.get(URL, text="")

    await client(hass).fetch_data()

    assert requested_window(aioclient_mock) == int(FIRST_FETCH.total_seconds())


async def test_second_fetch_asks_only_for_the_time_since_the_first(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: Any
) -> None:
    """A successful fetch narrows the next window to the elapsed time."""
    aioclient_mock.get(URL, text="")
    ttn = client(hass)

    await ttn.fetch_data()
    freezer.tick(timedelta(seconds=60))
    await ttn.fetch_data()

    # 60s elapsed plus the overlap margin.
    assert requested_window(aioclient_mock) == 120


async def test_failed_fetch_leaves_the_window_to_be_re_read(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: Any
) -> None:
    """This is the defect: a failed poll used to skip past what it never read.

    The watermark advanced before the request went out, so two consecutive
    failures dropped every uplink in between for good. It must now only move
    once a response has been read to the end.
    """
    aioclient_mock.get(URL, text="")
    ttn = client(hass)
    await ttn.fetch_data()

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, status=503)
    for _ in range(3):
        freezer.tick(timedelta(seconds=60))
        with pytest.raises(TTNStorageError):
            await ttn.fetch_data()

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, text="")
    freezer.tick(timedelta(seconds=60))
    await ttn.fetch_data()

    # All four minutes of failure are re-requested, not skipped.
    assert requested_window(aioclient_mock) == 240 + 60


async def test_auth_failure_also_leaves_the_watermark_alone(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: Any
) -> None:
    """A key that is fixed later must not have a hole behind it."""
    aioclient_mock.get(URL, text="")
    ttn = client(hass)
    await ttn.fetch_data()
    read_through = ttn.read_through

    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, status=403)
    freezer.tick(timedelta(seconds=60))
    with pytest.raises(TTNAuthError):
        await ttn.fetch_data()

    assert ttn.read_through == read_through


async def test_a_long_outage_does_not_ask_for_an_unbounded_window(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: Any
) -> None:
    """Home Assistant off for a week must not request a week in one call."""
    aioclient_mock.get(URL, text="")
    ttn = client(hass)
    await ttn.fetch_data()

    freezer.tick(timedelta(days=7))
    await ttn.fetch_data()

    assert requested_window(aioclient_mock) == int(MAX_WINDOW.total_seconds())


# --- A window larger than one page must be read to its end -----------------

T0 = datetime(2026, 9, 27, 13, 30, tzinfo=UTC)
FIRST_PAGE = {"last": f"{int(FIRST_FETCH.total_seconds())}s"}


def stamp(offset_s: float) -> str:
    """Return a TTN-style receipt stamp ``offset_s`` after T0."""
    return (T0 + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def full_page(temperature: float, *, start_s: int = 0, same_stamp: bool = False) -> str:
    """Return exactly PAGE_LIMIT records, one second apart unless told not to."""
    return stream(
        *(
            make_uplink(
                stamp(start_s if same_stamp else start_s + i),
                {"temperature": temperature},
            )
            for i in range(PAGE_LIMIT)
        )
    )


async def test_every_request_asks_for_the_api_page_limit(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Paging is keyed on the page size, so it must be the one we asked for."""
    aioclient_mock.get(URL, text="")

    await client(hass).fetch_data()

    _method, url, _data, _headers = aioclient_mock.mock_calls[-1]
    assert url.query["limit"] == str(PAGE_LIMIT)


async def test_a_full_page_is_followed_by_the_rest_of_the_window(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """This is the defect: a restart showed values from hours earlier.

    The API returns at most PAGE_LIMIT records oldest-first and does not say
    it stopped. A first fetch reaching back a day read only its oldest
    thousand, so every field came up with a reading from that cut-off, and
    the watermark moved to "now" past everything after it.
    """
    last_on_first_page = T0 + timedelta(seconds=PAGE_LIMIT - 1)
    after = (last_on_first_page - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    aioclient_mock.get(URL, params=FIRST_PAGE, text=full_page(20.5))
    aioclient_mock.get(
        URL,
        params={"after": after},
        text=stream(
            make_uplink(stamp(PAGE_LIMIT - 1), {"temperature": 20.5}),
            make_uplink(stamp(PAGE_LIMIT + 600), {"temperature": 3.7}),
        ),
    )

    data = await client(hass).fetch_data()

    assert aioclient_mock.call_count == 2
    assert data[DEVICE_ID]["temperature"].value == 3.7


async def test_a_short_page_ends_the_window(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """One record short of a page is the whole window; asking again is waste."""
    aioclient_mock.get(
        URL,
        text=stream(
            *(
                make_uplink(stamp(i), {"temperature": 20.5})
                for i in range(PAGE_LIMIT - 1)
            )
        ),
    )

    await client(hass).fetch_data()

    assert aioclient_mock.call_count == 1


async def test_unreadable_records_count_toward_a_full_page(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The API's limit counts records, not the ones this client could parse."""
    hostile = make_uplink(stamp(PAGE_LIMIT - 1), {"temperature": 0.0})
    hostile["uplink_message"]["decoded_payload"] = []
    page = [make_uplink(stamp(i), {"temperature": 20.5}) for i in range(PAGE_LIMIT - 1)]
    after = (T0 + timedelta(seconds=PAGE_LIMIT - 2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    aioclient_mock.get(URL, params=FIRST_PAGE, text=stream(*page, hostile))
    aioclient_mock.get(
        URL,
        params={"after": after},
        text=stream(make_uplink(stamp(PAGE_LIMIT + 5), {"temperature": 3.7})),
    )

    data = await client(hass).fetch_data()

    assert aioclient_mock.call_count == 2
    assert data[DEVICE_ID]["temperature"].value == 3.7


async def test_paging_that_cannot_advance_fails_the_fetch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A page that ends where it started would loop forever or drop data.

    Either outcome is worse than a failed poll, which leaves the watermark
    alone so the window is re-read.
    """
    aioclient_mock.get(URL, text=full_page(20.5, same_stamp=True))
    ttn = client(hass)

    with pytest.raises(TTNStorageError, match="cannot page past"):
        await ttn.fetch_data()

    assert ttn.read_through is None


async def test_a_full_page_without_receipt_times_fails_the_fetch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With nothing to continue from, the rest of the window is unreachable."""
    aioclient_mock.get(URL, text=raw_stream(*["{not json"] * PAGE_LIMIT))
    ttn = client(hass)

    with pytest.raises(TTNStorageError, match="no readable received_at"):
        await ttn.fetch_data()

    assert ttn.read_through is None


async def test_a_window_that_never_ends_fails_the_fetch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, monkeypatch: Any
) -> None:
    """A backstop, not an expected path: the watermark must not move."""
    monkeypatch.setattr(storage, "MAX_PAGES", 3)

    async def advancing_page(method: str, url: Any, data: Any) -> Any:
        start = len(aioclient_mock.mock_calls) * PAGE_LIMIT
        return AiohttpClientMockResponse(
            method, url, text=full_page(20.5, start_s=start)
        )

    aioclient_mock.get(URL, side_effect=advancing_page)
    ttn = client(hass)

    with pytest.raises(TTNStorageError, match="still returning full pages"):
        await ttn.fetch_data()

    assert aioclient_mock.call_count == 3
    assert ttn.read_through is None


# --- Response status handling ----------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_refused_credentials_raise_auth_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """Only a credential refusal may trigger the reauth flow."""
    aioclient_mock.get(URL, status=status)

    with pytest.raises(TTNAuthError):
        await client(hass).fetch_data()


@pytest.mark.parametrize("status", [404, 429, 500, 503])
async def test_other_failures_raise_storage_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    """A wrong app id or a bad gateway is not a wrong API key.

    Upstream mapped every 4xx to an auth error, so a typo in the application
    id sent the user to re-enter a key that was never the problem.
    """
    aioclient_mock.get(URL, status=status)

    with pytest.raises(TTNStorageError):
        await client(hass).fetch_data()


async def test_records_arriving_during_the_request_are_re_read(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: Any
) -> None:
    """The watermark is the instant the request was issued, not finished.

    A record stored while the response was still streaming would otherwise
    fall between the two windows and never be read.
    """

    async def slow_response(method: str, url: Any, data: Any) -> Any:
        freezer.tick(timedelta(seconds=30))
        return mock

    mock = aioclient_mock.request("get", URL, text="", side_effect=slow_response)
    ttn = client(hass)

    issued_at = dt_util.utcnow()
    await ttn.fetch_data()

    assert dt_util.utcnow() - issued_at == timedelta(seconds=30)
    assert ttn.read_through == issued_at


# --- Paging must stay inside the API's rate limit --------------------------


def allowance(available: int | str, reset_s: float = 30) -> dict[str, str]:
    """Return the rate-limit headers TTN sends with a successful response."""
    return {
        "X-Rate-Limit-Limit": "10",
        "X-Rate-Limit-Available": str(available),
        "X-Rate-Limit-Reset": f"{reset_s:g}",
        "X-Rate-Limit-Retry": "0",
    }


def rate_limited(**headers: str) -> dict[str, Any]:
    """Return the keyword arguments for one HTTP 429 response."""
    return {
        "status": 429,
        "text": json.dumps({"code": 8, "message": "rate limit exceeded"}),
        "headers": headers,
    }


SECOND_PAGE = {
    "after": (T0 + timedelta(seconds=PAGE_LIMIT - 2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
}


def two_pages(mock: AiohttpClientMocker, first_page_headers: dict[str, str]) -> None:
    """Register a full first page with the given headers, then a short second."""
    mock.get(URL, params=FIRST_PAGE, text=full_page(20.5), headers=first_page_headers)
    mock.get(
        URL,
        params=SECOND_PAGE,
        text=stream(make_uplink(stamp(PAGE_LIMIT + 5), {"temperature": 3.7})),
    )


def in_sequence(mock: AiohttpClientMocker, *responses: dict[str, Any]) -> None:
    """Answer successive requests to URL with ``responses``, in order."""
    remaining = iter(responses)

    async def next_response(method: str, url: Any, data: Any) -> Any:
        return AiohttpClientMockResponse(method, url, **next(remaining))

    mock.get(URL, side_effect=next_response)


async def test_paging_pauses_when_the_allowance_is_nearly_spent(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, waits: list[float]
) -> None:
    """This is the 0.8.3 regression: a busy first fetch ended in HTTP 429.

    The endpoint allows ten requests a minute and a busy application's first
    fetch needs about that many pages. Spent at once, the allowance ran out
    part-way, the poll failed, and every retry started the window again, so
    the entities never came up.
    """
    two_pages(aioclient_mock, allowance(RATE_LIMIT_RESERVE, reset_s=42))

    data = await client(hass).fetch_data()

    assert waits == [42.0]
    assert aioclient_mock.call_count == 2
    assert data[DEVICE_ID]["temperature"].value == 3.7


@pytest.mark.parametrize(
    "headers",
    [allowance(RATE_LIMIT_RESERVE + 1), {}, allowance("lots")],
    ids=["allowance-left", "no-headers", "garbled"],
)
async def test_paging_does_not_pause_without_cause(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    waits: list[float],
    headers: dict[str, str],
) -> None:
    """Allowance to spare, or no rate limiting advertised, means no waiting."""
    two_pages(aioclient_mock, headers)

    await client(hass).fetch_data()

    assert waits == []
    assert aioclient_mock.call_count == 2


async def test_a_rate_limited_page_is_retried_after_the_advised_wait(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, waits: list[float]
) -> None:
    """Another reader of the same application can spend the allowance first."""
    in_sequence(
        aioclient_mock,
        rate_limited(**{"X-Rate-Limit-Retry": "12", "X-Rate-Limit-Reset": "48"}),
        {
            "text": stream(make_uplink(stamp(0), {"temperature": 3.7})),
            "headers": allowance(8),
        },
    )
    ttn = client(hass)

    data = await ttn.fetch_data()

    assert waits == [12.0]
    assert aioclient_mock.call_count == 2
    assert data[DEVICE_ID]["temperature"].value == 3.7
    assert ttn.read_through is not None


@pytest.mark.parametrize(
    ("headers", "expected_wait"),
    [
        ({"X-Rate-Limit-Retry": "0", "X-Rate-Limit-Reset": "9"}, 9.0),
        ({}, 6.0),
        ({"x-rate-limit-retry": "15"}, 15.0),
    ],
    ids=["reset-when-retry-is-zero", "one-refill-without-headers", "any-case"],
)
async def test_the_wait_after_a_429_falls_back_sensibly(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    waits: list[float],
    headers: dict[str, str],
    expected_wait: float,
) -> None:
    """The advised retry first, then the limiter's reset, then one refill."""
    in_sequence(aioclient_mock, rate_limited(**headers), {"text": ""})

    await client(hass).fetch_data()

    assert waits == [expected_wait]


async def test_persistent_rate_limiting_fails_the_fetch_and_keeps_the_watermark(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, waits: list[float]
) -> None:
    """Retries are bounded; the window is re-read on the next poll instead."""
    aioclient_mock.get(URL, **rate_limited(**{"X-Rate-Limit-Retry": "6"}))
    ttn = client(hass)

    with pytest.raises(TTNStorageError, match="still rate limiting"):
        await ttn.fetch_data()

    assert waits == [6.0] * MAX_RATE_LIMITED_RETRIES
    assert aioclient_mock.call_count == MAX_RATE_LIMITED_RETRIES + 1
    assert ttn.read_through is None


async def test_an_implausible_wait_after_a_429_fails_instead_of_stalling(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, waits: list[float]
) -> None:
    """The limiter's window is a minute; an hour's wait is not believed."""
    too_long = MAX_RATE_LIMIT_WAIT.total_seconds() * 60
    aioclient_mock.get(URL, **rate_limited(**{"X-Rate-Limit-Retry": f"{too_long:g}"}))
    ttn = client(hass)

    with pytest.raises(TTNStorageError, match="longer than"):
        await ttn.fetch_data()

    assert waits == []
    assert aioclient_mock.call_count == 1
    assert ttn.read_through is None


async def test_an_implausible_wait_between_pages_fails_instead_of_stalling(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, waits: list[float]
) -> None:
    """The same ceiling applies to the pause before a follow-up page."""
    too_long = MAX_RATE_LIMIT_WAIT.total_seconds() * 60
    two_pages(aioclient_mock, allowance(0, reset_s=too_long))
    ttn = client(hass)

    with pytest.raises(TTNStorageError, match="between pages"):
        await ttn.fetch_data()

    assert waits == []
    assert aioclient_mock.call_count == 1
    assert ttn.read_through is None
