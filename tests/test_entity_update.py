"""Regression tests for TTNEntity's coordinator update path.

Every test here maps to a way a live sensor stopped updating while TTN was
still delivering uplinks for it.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from ttn_client import TTNBaseValue
from ttn_client.parsers.default import default_parser

from custom_components.thethingsnetwork_alt.entity import TTNEntity

from .conftest import (
    APP_ID,
    DEVICE_ID,
    coordinator_data,
    make_coordinator,
    make_uplink,
    parse_uplink,
)


class RecordingEntity(TTNEntity):
    """TTNEntity that counts state writes instead of touching the HA core."""

    def __init__(self, ttn_value: TTNBaseValue, data: Any = None) -> None:
        """Initialize around one held value and an optional first payload."""
        super().__init__(make_coordinator(data), APP_ID, ttn_value)
        self.state_writes = 0

    def async_write_ha_state(self) -> None:
        """Count the write instead of reaching into the state machine."""
        self.state_writes += 1

    def push(self, data: Any) -> None:
        """Deliver a new coordinator payload the way the coordinator would."""
        self.coordinator.data = data
        self._handle_coordinator_update()

    @property
    def value(self) -> Any:
        """Return the value the entity is currently holding."""
        return self._ttn_value.value


def held(received_at: str, decoded: dict[str, Any]) -> TTNBaseValue:
    """Return the single parsed value an entity is constructed around."""
    parsed = parse_uplink(received_at, decoded)
    return parsed[next(iter(parsed))]


# --- Defect A: naive/aware timestamp mix ------------------------------------


def test_naive_timestamp_update_is_applied() -> None:
    """A received_at without an offset must not wedge the entity.

    TTNBaseValue.received_at returns a naive datetime when the uplink stamp
    carries no offset. Comparing that against an aware one raises TypeError,
    which HA swallows in async_update_listeners, so the entity silently stops
    updating for the rest of the run.
    """
    entity = RecordingEntity(held("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    entity.push(coordinator_data("2026-09-01T01:00:00", {"temperature": 21.5}))

    assert entity.value == 21.5
    assert entity.state_writes == 1


def test_entity_holding_naive_timestamp_still_accepts_aware_update() -> None:
    """The reverse pairing must recover too, not just the forward one."""
    entity = RecordingEntity(held("2026-09-01T00:00:00", {"temperature": 20.5}))

    entity.push(coordinator_data("2026-09-01T01:00:00Z", {"temperature": 21.5}))

    assert entity.value == 21.5
    assert entity.state_writes == 1


# --- Defect B: unreadable timestamp -----------------------------------------


def test_update_without_received_at_is_applied() -> None:
    """A missing received_at raises KeyError out of the unguarded comparison."""
    entity = RecordingEntity(held("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    uplink = make_uplink("2026-09-01T01:00:00Z", {"temperature": 21.5})
    del uplink["received_at"]

    entity.push({DEVICE_ID: default_parser(uplink)})

    assert entity.value == 21.5
    assert entity.state_writes == 1


def test_entity_holding_unreadable_timestamp_recovers() -> None:
    """An entity created from a bad uplink must not be frozen forever."""
    uplink = make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5})
    uplink["received_at"] = "not-a-timestamp"

    entity = RecordingEntity(default_parser(uplink)["temperature"])

    entity.push(coordinator_data("2026-09-01T01:00:00Z", {"temperature": 21.5}))

    assert entity.value == 21.5
    assert entity.state_writes == 1


# --- Defect C: scalar type flip ---------------------------------------------


def test_bool_reading_after_numeric_is_not_discarded() -> None:
    """0/1 and false/true from one decoder must not cost a reading.

    default_parser types purely on the Python type of the decoded value, so a
    field that reports 1 on one uplink and true on the next flips between
    TTNSensorValue and TTNBinarySensorValue.
    """
    entity = RecordingEntity(held("2026-09-01T00:00:00Z", {"leak_alarm": 0}))

    entity.push(coordinator_data("2026-09-01T00:10:00Z", {"leak_alarm": True}))

    assert entity.value is True
    assert entity.state_writes == 1


def test_numeric_reading_after_bool_is_not_discarded() -> None:
    """The reverse flip must also be accepted."""
    entity = RecordingEntity(held("2026-09-01T00:00:00Z", {"leak_alarm": True}))

    entity.push(coordinator_data("2026-09-01T00:10:00Z", {"leak_alarm": 0}))

    assert entity.value == 0
    assert entity.state_writes == 1


def test_zero_reading_is_never_dropped() -> None:
    """A sensor reporting 0 is real data, not a missing value."""
    entity = RecordingEntity(held("2026-09-01T00:00:00Z", {"rainfall_mm": 4.2}))

    entity.push(coordinator_data("2026-09-01T00:10:00Z", {"rainfall_mm": 0}))

    assert entity.value == 0
    assert entity.state_writes == 1


def test_gps_value_is_rejected_by_a_scalar_entity() -> None:
    """A genuinely incompatible transition is still refused."""
    entity = RecordingEntity(held("2026-09-01T00:00:00Z", {"probe": 1.0}))

    entity.push(
        coordinator_data(
            "2026-09-01T00:10:00Z", {"probe": {"latitude": 1.0, "longitude": 2.0}}
        )
    )

    assert entity.value == 1.0
    assert entity.state_writes == 0


def test_incompatible_transition_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The refusal must not log once per uplink forever."""
    entity = RecordingEntity(held("2026-09-01T00:00:00Z", {"probe": 1.0}))

    with caplog.at_level(logging.WARNING):
        for minute in range(10, 60, 10):
            entity.push(
                coordinator_data(
                    f"2026-09-01T00:{minute}:00Z",
                    {"probe": {"latitude": 1.0, "longitude": 2.0}},
                )
            )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


# --- Defect D: sub-microsecond timestamps -----------------------------------


def test_uplinks_within_one_microsecond_are_both_applied() -> None:
    """TTN sends nanoseconds; fromisoformat truncates to microseconds.

    Two distinct uplinks can therefore parse to the same datetime, and a strict
    '>' comparison throws the second one away.
    """
    entity = RecordingEntity(held("2026-09-01T00:00:00.123456111Z", {"counter": 1}))

    entity.push(coordinator_data("2026-09-01T00:00:00.123456999Z", {"counter": 2}))

    assert entity.value == 2
    assert entity.state_writes == 1


# --- Guards: behaviour that must NOT regress --------------------------------


def test_redelivered_uplink_does_not_rewrite_state() -> None:
    """Fetch windows overlap, so the same uplink legitimately arrives twice."""
    entity = RecordingEntity(held("2026-09-01T00:00:00.500000000Z", {"counter": 7}))

    entity.push(coordinator_data("2026-09-01T00:00:00.500000000Z", {"counter": 7}))

    assert entity.value == 7
    assert entity.state_writes == 0


def test_older_uplink_does_not_overwrite_newer() -> None:
    """Out-of-order delivery must not roll the state backwards."""
    entity = RecordingEntity(held("2026-09-01T01:00:00Z", {"counter": 9}))

    entity.push(coordinator_data("2026-09-01T00:00:00Z", {"counter": 4}))

    assert entity.value == 9
    assert entity.state_writes == 0


def test_device_absent_from_window_retains_value() -> None:
    """coordinator.data is a delta; a quiet device is simply not in it."""
    entity = RecordingEntity(held("2026-09-01T00:00:00Z", {"counter": 3}))

    entity.push({"some-other-device": {}})

    assert entity.value == 3
    assert entity.state_writes == 0


def test_empty_coordinator_data_retains_value() -> None:
    """An empty poll must not clear the entity."""
    entity = RecordingEntity(held("2026-09-01T00:00:00Z", {"counter": 3}))

    entity.push({})

    assert entity.value == 3
    assert entity.state_writes == 0
