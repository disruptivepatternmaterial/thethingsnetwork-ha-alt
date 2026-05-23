"""Parse TTN decoder timestamp values for Home Assistant."""

from __future__ import annotations

from datetime import UTC, datetime

TIMESTAMP_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "data_time",
        "timestamp",
        "systimestamp",
    }
)


def is_timestamp_field(field_id: str) -> bool:
    """Return True when a TTN field should be treated as a timestamp sensor."""
    return field_id.lower() in TIMESTAMP_FIELD_NAMES


def parse_ttn_timestamp(value: object) -> datetime | None:
    """Convert a TTN decoded timestamp value to an aware datetime."""
    if value is None:
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 9999999999:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=UTC)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            return parsed.replace(tzinfo=UTC)

    return None
