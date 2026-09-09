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
        try:
            return datetime.fromtimestamp(ts, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            # fromisoformat has read a trailing "Z" since 3.11. Substituting
            # it by hand rewrote every "Z" in the string, not just the offset.
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    return None
