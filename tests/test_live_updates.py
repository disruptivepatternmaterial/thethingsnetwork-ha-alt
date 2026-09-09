"""End-to-end tests: does a value that changed in TTN change in Home Assistant?

These boot the real integration into a real ``hass`` and read Home Assistant's
own state machine. The unit tests in ``test_entity_update.py`` prove the
coordinator update path in isolation; these prove that nothing between the
coordinator and the state machine throws the reading away.
"""

from __future__ import annotations

import logging

import pytest

from .conftest import DEVICE_ID, TTNHarness, make_uplink

RX = [{"gateway_ids": {"gateway_id": "gw-1"}, "rssi": -70, "snr": 8.2}]


async def test_scalar_sensor_tracks_successive_uplinks(ttn: TTNHarness) -> None:
    """The baseline every other test is measured against."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    assert ttn.state("sensor.dev_1_temperature") == "20.5"

    await ttn.poll(make_uplink("2026-09-01T00:01:00Z", {"temperature": 21.5}))

    assert ttn.state("sensor.dev_1_temperature") == "21.5"


async def test_field_discovered_after_setup_becomes_an_entity(
    ttn: TTNHarness,
) -> None:
    """A field absent from the first window must still get an entity.

    ``humidity`` is mapped to the friendly name "Relative Humidity", which is
    what the entity id is slugged from.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    assert ttn.state("sensor.dev_1_relative_humidity") is None

    await ttn.poll(
        make_uplink("2026-09-01T00:01:00Z", {"temperature": 21.5, "humidity": 44})
    )

    assert ttn.state("sensor.dev_1_relative_humidity") == "44"


async def test_quiet_device_retains_its_readings(ttn: TTNHarness) -> None:
    """coordinator.data is a delta; an empty window must not clear entities."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    await ttn.poll()

    assert ttn.state("sensor.dev_1_temperature") == "20.5"


async def test_non_numeric_value_under_numeric_metadata_keeps_updating(
    ttn: TTNHarness,
) -> None:
    """A decoder that annotates a text field numerically must not freeze it.

    ``SensorEntity.state`` raises ValueError when device_class/state_class/unit
    imply a number and the value is text. The coordinator wraps every listener
    in try/except, so the entity silently stops updating for the rest of the
    run with nothing user-facing to explain it.
    """
    await ttn.start(
        make_uplink(
            "2026-09-01T00:00:00Z",
            {
                "status_c": "OK",
                "_sensor_attr": {
                    "status_c": {"device_class": "temperature", "unit": "°C"}
                },
            },
        )
    )

    await ttn.poll(
        make_uplink(
            "2026-09-01T00:01:00Z",
            {
                "status_c": "FAULT",
                "_sensor_attr": {
                    "status_c": {"device_class": "temperature", "unit": "°C"}
                },
            },
        )
    )

    assert ttn.state("sensor.dev_1_status_c") == "FAULT"


async def test_meta_sensors_track_signal_strength(ttn: TTNHarness) -> None:
    """RSSI/SNR/gateway are synthesized per device from rx_metadata."""
    await ttn.start(
        make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}, rx_metadata=RX)
    )

    assert ttn.state("sensor.dev_1_rssi") == "-70"

    await ttn.poll(
        make_uplink(
            "2026-09-01T00:01:00Z",
            {"temperature": 21.5},
            rx_metadata=[
                {"gateway_ids": {"gateway_id": "gw-2"}, "rssi": -55, "snr": 11.0}
            ],
        )
    )

    assert ttn.state("sensor.dev_1_rssi") == "-55"
    assert ttn.state("sensor.dev_1_gateway") == "gw-2"


async def test_meta_sensor_survives_a_mixed_offset_window(ttn: TTNHarness) -> None:
    """One undateable field must not take the whole device's diagnostics down.

    ``newest_uplink_carrier`` compares ``received_at`` values outside the guard
    that parses them, so a window holding both an offset-bearing and an
    offset-free stamp raises TypeError out of ``native_value``.
    """
    await ttn.start(
        make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}, rx_metadata=RX)
    )

    assert ttn.state("sensor.dev_1_rssi") == "-70"

    await ttn.poll(
        make_uplink("2026-09-01T00:01:00", {"humidity": 44}, rx_metadata=RX),
        make_uplink(
            "2026-09-01T00:02:00Z",
            {"temperature": 21.5},
            rx_metadata=[
                {"gateway_ids": {"gateway_id": "gw-2"}, "rssi": -55, "snr": 11.0}
            ],
        ),
    )

    assert ttn.state("sensor.dev_1_rssi") == "-55"


async def test_device_tracker_survives_a_mixed_offset_window(
    ttn: TTNHarness,
) -> None:
    """The tracker walks the same comparison and fails the same way."""
    await ttn.start(
        make_uplink(
            "2026-09-01T00:00:00Z",
            {"temperature": 20.5},
            rx_metadata=RX,
            locations={"user": {"latitude": 47.6, "longitude": -122.3}},
        )
    )

    assert ttn.attribute("device_tracker.dev_1_location", "latitude") == 47.6

    await ttn.poll(
        make_uplink("2026-09-01T00:01:00", {"humidity": 44}, rx_metadata=RX),
        make_uplink(
            "2026-09-01T00:02:00Z",
            {"temperature": 21.5},
            rx_metadata=RX,
            locations={"user": {"latitude": 48.0, "longitude": -122.0}},
        ),
    )

    assert ttn.attribute("device_tracker.dev_1_location", "latitude") == 48.0


async def test_decoder_metadata_arriving_late_is_applied(ttn: TTNHarness) -> None:
    """_sensor_attr is not guaranteed to ride the uplink that creates the entity."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"tank_level": 42}))

    assert ttn.attribute("sensor.dev_1_tank_level", "unit_of_measurement") is None

    await ttn.poll(
        make_uplink(
            "2026-09-01T00:01:00Z",
            {
                "tank_level": 43,
                "_sensor_attr": {"tank_level": {"unit": "%"}},
            },
        )
    )

    assert ttn.state("sensor.dev_1_tank_level") == "43"
    assert ttn.attribute("sensor.dev_1_tank_level", "unit_of_measurement") == "%"


async def test_type_flipping_field_does_not_get_two_entities(
    ttn: TTNHarness,
) -> None:
    """0/1 then false/true must not leave a duplicate on the other platform.

    The bool is recorded as 1, not "True": the field's other readings are
    numeric, and a text state would drop out of history and statistics.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"leak": 0}))

    assert ttn.state("sensor.dev_1_leak") == "0"

    await ttn.poll(make_uplink("2026-09-01T00:01:00Z", {"leak": True}))

    assert ttn.state("binary_sensor.dev_1_leak") is None
    assert ttn.state("sensor.dev_1_leak") == "1"


async def test_failed_poll_does_not_lose_the_reading(ttn: TTNHarness) -> None:
    """A transient storage-API error must not permanently strand entities."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    ttn.fetch_error = RuntimeError("expected 200 got 503 - Service Unavailable")
    await ttn.poll()
    ttn.fetch_error = None

    await ttn.poll(make_uplink("2026-09-01T00:02:00Z", {"temperature": 22.5}))

    assert ttn.state("sensor.dev_1_temperature") == "22.5"


async def test_gps_components_and_tracker_follow_the_newest_fix(
    ttn: TTNHarness,
) -> None:
    """A decoded GPS field drives both the axis sensors and the tracker."""
    await ttn.start(
        make_uplink(
            "2026-09-01T00:00:00Z",
            {"gps": {"latitude": 47.6, "longitude": -122.3, "altitude": 30.0}},
            rx_metadata=RX,
        )
    )

    assert ttn.state("sensor.dev_1_latitude") == "47.6"
    assert ttn.attribute("device_tracker.dev_1_location", "latitude") == 47.6

    await ttn.poll(
        make_uplink(
            "2026-09-01T00:01:00Z",
            {"gps": {"latitude": 48.1, "longitude": -122.9, "altitude": 35.0}},
            rx_metadata=RX,
        )
    )

    assert ttn.state("sensor.dev_1_latitude") == "48.1"
    assert ttn.state("sensor.dev_1_altitude") == "35.0"
    assert ttn.attribute("device_tracker.dev_1_location", "latitude") == 48.1


async def test_binary_sensor_tracks_successive_uplinks(ttn: TTNHarness) -> None:
    """A mapped binary field must move with the payload."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"door_status": "open"}))

    assert ttn.state("binary_sensor.dev_1_external_input_exti") == "on"

    await ttn.poll(make_uplink("2026-09-01T00:01:00Z", {"door_status": "close"}))

    assert ttn.state("binary_sensor.dev_1_external_input_exti") == "off"


async def test_second_device_appearing_later_is_set_up(ttn: TTNHarness) -> None:
    """A device that first uplinks after setup must get its entities."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    await ttn.poll(
        make_uplink("2026-09-01T00:01:00Z", {"temperature": 21.5}, DEVICE_ID),
        make_uplink("2026-09-01T00:01:30Z", {"temperature": 5.0}, "dev-2"),
    )

    assert ttn.state("sensor.dev_2_temperature") == "5.0"


async def test_platform_ownership_survives_a_restart(ttn: TTNHarness) -> None:
    """The field keeps the platform it already has in the entity registry.

    Claiming a field only lasts as long as the run; after a restart the first
    uplink's value class would decide again, so a field that came back in its
    boolean form would be rebuilt as a binary sensor and strand the sensor
    that holds all of its history.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"leak": 0}))

    assert ttn.state("sensor.dev_1_leak") == "0"

    await ttn.reload(make_uplink("2026-09-01T00:05:00Z", {"leak": True}))

    assert ttn.state("binary_sensor.dev_1_leak") is None
    assert ttn.state("sensor.dev_1_leak") == "1"


async def test_binary_owned_field_accepts_a_numeric_reading(
    ttn: TTNHarness,
) -> None:
    """The flip in the other direction: a bool field that sends 1/0."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"leak": False}))

    assert ttn.state("binary_sensor.dev_1_leak") == "off"

    await ttn.poll(make_uplink("2026-09-01T00:01:00Z", {"leak": 1}))

    assert ttn.state("sensor.dev_1_leak") is None
    assert ttn.state("binary_sensor.dev_1_leak") == "on"


async def test_last_seen_accepts_a_stamp_without_an_offset(ttn: TTNHarness) -> None:
    """Last seen is a timestamp sensor, and HA rejects a naive datetime.

    The rejection raises while the entity is being added, so the sensor never
    reaches the state machine at all.
    """
    await ttn.start(
        make_uplink("2026-09-01T00:00:00", {"temperature": 20.5}, rx_metadata=RX)
    )

    assert ttn.state("sensor.dev_1_last_seen") == "2026-09-01T00:00:00+00:00"

    await ttn.poll(
        make_uplink("2026-09-01T00:01:00", {"temperature": 21.5}, rx_metadata=RX)
    )

    assert ttn.state("sensor.dev_1_last_seen") == "2026-09-01T00:01:00+00:00"


async def test_reading_longer_than_a_state_is_kept(ttn: TTNHarness) -> None:
    """A state over 255 characters is refused outright, blanking the sensor."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"note": "short"}))

    assert ttn.state("sensor.dev_1_note") == "short"

    await ttn.poll(make_uplink("2026-09-01T00:01:00Z", {"note": "x" * 400}))

    state = ttn.state("sensor.dev_1_note")
    assert state == "x" * 255


async def test_text_field_typed_numeric_by_a_suffix_heuristic_still_works(
    ttn: TTNHarness,
) -> None:
    """``_c`` means Celsius to the heuristics, even on a status string.

    The reading is the fact and the heuristic is the guess, so the sensor is
    created as plain text rather than failing HA's numeric validation.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"pump_state_c": "IDLE"}))

    assert ttn.state("sensor.dev_1_pump_state_c") == "IDLE"
    assert ttn.attribute("sensor.dev_1_pump_state_c", "unit_of_measurement") is None

    await ttn.poll(make_uplink("2026-09-01T00:01:00Z", {"pump_state_c": "RUNNING"}))

    assert ttn.state("sensor.dev_1_pump_state_c") == "RUNNING"


async def test_numeric_sensor_reports_unknown_for_a_later_text_reading(
    ttn: TTNHarness,
) -> None:
    """A numeric field that later sends an error string must stay alive."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    await ttn.poll(make_uplink("2026-09-01T00:01:00Z", {"temperature": "ERR"}))

    assert ttn.state("sensor.dev_1_temperature") == "unknown"

    await ttn.poll(make_uplink("2026-09-01T00:02:00Z", {"temperature": 22.5}))

    assert ttn.state("sensor.dev_1_temperature") == "22.5"


async def test_field_resumes_after_several_quiet_windows(ttn: TTNHarness) -> None:
    """A field the device stops sending must pick up again when it returns."""
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    for minute in range(1, 4):
        await ttn.poll(make_uplink(f"2026-09-01T00:0{minute}:00Z", {"humidity": 40}))

    assert ttn.state("sensor.dev_1_temperature") == "20.5"

    await ttn.poll(make_uplink("2026-09-01T00:05:00Z", {"temperature": 25.5}))

    assert ttn.state("sensor.dev_1_temperature") == "25.5"


async def test_altitude_arriving_late_still_gets_a_sensor(ttn: TTNHarness) -> None:
    """A GPS fix without altitude must not permanently suppress the entity."""
    await ttn.start(
        make_uplink(
            "2026-09-01T00:00:00Z", {"gps": {"latitude": 47.6, "longitude": -122.3}}
        )
    )

    assert ttn.state("sensor.dev_1_altitude") is None

    await ttn.poll(
        make_uplink(
            "2026-09-01T00:01:00Z",
            {"gps": {"latitude": 47.6, "longitude": -122.3, "altitude": 30.0}},
        )
    )

    assert ttn.state("sensor.dev_1_altitude") == "30.0"


async def test_stale_gps_fix_loses_to_the_registry_location(
    ttn: TTNHarness,
) -> None:
    """A device whose GPS stopped reporting must not pin an ancient fix."""
    await ttn.start(
        make_uplink(
            "2026-09-01T00:00:00Z",
            {"gps": {"latitude": 47.6, "longitude": -122.3}},
            rx_metadata=RX,
        )
    )

    assert ttn.attribute("device_tracker.dev_1_location", "latitude") == 47.6

    await ttn.poll(
        make_uplink(
            "2026-09-01T00:00:00Z",
            {"gps": {"latitude": 47.6, "longitude": -122.3}},
            rx_metadata=RX,
        ),
        make_uplink(
            "2026-09-03T00:00:00Z",
            {"temperature": 20.5},
            rx_metadata=RX,
            locations={"user": {"latitude": 40.0, "longitude": -100.0}},
        ),
    )

    assert ttn.attribute("device_tracker.dev_1_location", "latitude") == 40.0
    assert ttn.attribute("device_tracker.dev_1_location", "location_source") == (
        "registry"
    )


async def test_unparseable_record_is_reported_as_an_update_failure(
    ttn: TTNHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """ttn_client parses the whole window before returning any of it.

    One record it cannot read raises out of ``fetch_data`` and costs every
    device that window — nothing this integration can recover. What it can do
    is name the cause: unhandled, the same failure arrives as a bare
    "Unexpected error" traceback every polling period, which reads like a bug
    in the integration rather than a malformed record from TTN.
    """
    await ttn.start(make_uplink("2026-09-01T00:00:00Z", {"temperature": 20.5}))

    with caplog.at_level(logging.DEBUG):
        ttn.fetch_error = KeyError("uplink_message")
        await ttn.poll()

    assert "Unexpected error" not in caplog.text
    assert "could not parse" in caplog.text
    assert ttn.state("sensor.dev_1_temperature") == "20.5"

    ttn.fetch_error = None
    await ttn.poll(make_uplink("2026-09-01T00:02:00Z", {"temperature": 22.5}))

    assert ttn.state("sensor.dev_1_temperature") == "22.5"


async def test_failed_poll_does_not_blank_the_diagnostics(ttn: TTNHarness) -> None:
    """One failed fetch must not contradict the readings we still hold.

    The data sensors never rewrite state on a failed poll, so they kept their
    value while the meta sensors and the tracker — which rewrite on every
    tick — dropped to unavailable off the same event.
    """
    await ttn.start(
        make_uplink(
            "2026-09-01T00:00:00Z",
            {"temperature": 20.5},
            rx_metadata=RX,
            locations={"user": {"latitude": 47.6, "longitude": -122.3}},
        )
    )

    ttn.fetch_error = RuntimeError("expected 200 got 503 - Service Unavailable")
    await ttn.poll()

    assert ttn.state("sensor.dev_1_temperature") == "20.5"
    assert ttn.state("sensor.dev_1_rssi") == "-70"
    assert ttn.state("sensor.dev_1_last_seen") == "2026-09-01T00:00:00+00:00"
    assert ttn.attribute("device_tracker.dev_1_location", "latitude") == 47.6


# --- A GPS object must not consume a decoded field's name -------------------


async def test_flat_field_is_not_swallowed_by_a_gps_component(
    ttn: TTNHarness,
) -> None:
    """A decoder can send a GPS object *and* a flat field of the same name.

    The axis sensors were named ``f"{parent}_{component}"``, which is exactly
    what a decoded ``gps_latitude`` field is called. The axis reserved the
    name first, so discovery skipped the real field: no entity, no warning,
    and a reading that never reached Home Assistant at all.
    """
    await ttn.start(
        make_uplink(
            "2026-09-01T00:00:00Z",
            {
                "gps": {"latitude": 47.6, "longitude": -122.3},
                "gps_latitude": 11.11,
            },
            rx_metadata=RX,
        )
    )

    assert "47.6" in ttn.readings()
    assert "11.11" in ttn.readings()


async def test_swallowed_flat_field_keeps_tracking_later_uplinks(
    ttn: TTNHarness,
) -> None:
    """Recovering the entity is only useful if it then follows the payload."""
    await ttn.start(
        make_uplink(
            "2026-09-01T00:00:00Z",
            {
                "gps": {"latitude": 47.6, "longitude": -122.3},
                "gps_latitude": 11.11,
            },
            rx_metadata=RX,
        )
    )

    await ttn.poll(
        make_uplink(
            "2026-09-01T00:01:00Z",
            {
                "gps": {"latitude": 47.7, "longitude": -122.4},
                "gps_latitude": 22.22,
            },
            rx_metadata=RX,
        )
    )

    assert "47.7" in ttn.readings()
    assert "22.22" in ttn.readings()


async def test_gps_axis_sensors_use_the_reserved_namespace(
    ttn: TTNHarness,
) -> None:
    """Axis unique_ids must sit where no decoded field can ever reach.

    Discovery skips any field_id starting with ``_``, so a synthetic id built
    with that prefix cannot be produced by a decoder.
    """
    await ttn.start(
        make_uplink(
            "2026-09-01T00:00:00Z",
            {"gps": {"latitude": 47.6, "longitude": -122.3, "altitude": 30.0}},
            rx_metadata=RX,
        )
    )

    axis_ids = [uid for uid in ttn.unique_ids() if "gps" in uid]

    assert axis_ids == [
        f"{DEVICE_ID}__gps_gps_altitude",
        f"{DEVICE_ID}__gps_gps_latitude",
        f"{DEVICE_ID}__gps_gps_longitude",
    ]
