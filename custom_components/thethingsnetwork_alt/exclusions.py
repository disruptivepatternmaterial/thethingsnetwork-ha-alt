"""Load editable field exclusions for The Things Network HA-Alt.

Reads `field_exclusions.json` next to this module. Schema:

    {
      "global": ["field_a", "debug_*"],
      "devices": {
        "device_id": ["field_b"]
      }
    }

All matching is case-insensitive. A name ending in `*` is treated as a
prefix wildcard. Keys starting with `_` (e.g. `_comment`) are ignored.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_EXCLUSIONS_CACHE: tuple[frozenset[str], tuple[str, ...], dict[
    str, tuple[frozenset[str], tuple[str, ...]]
]] | None = None


def _split_patterns(raw: object) -> tuple[frozenset[str], tuple[str, ...]]:
    """Split a list of raw patterns into exact lowercase set + prefix tuple."""
    if not isinstance(raw, list):
        return frozenset(), ()

    exact: set[str] = set()
    prefixes: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        text = entry.strip().lower()
        if not text:
            continue
        if text.endswith("*"):
            prefix = text[:-1]
            if prefix:
                prefixes.append(prefix)
        else:
            exact.add(text)
    return frozenset(exact), tuple(prefixes)


def _load_exclusions() -> tuple[
    frozenset[str], tuple[str, ...], dict[str, tuple[frozenset[str], tuple[str, ...]]]
]:
    """Load and cache field exclusions from disk."""
    global _EXCLUSIONS_CACHE  # noqa: PLW0603
    if _EXCLUSIONS_CACHE is not None:
        return _EXCLUSIONS_CACHE

    path = Path(__file__).with_name("field_exclusions.json")
    if not path.is_file():
        _EXCLUSIONS_CACHE = (frozenset(), (), {})
        return _EXCLUSIONS_CACHE

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LOGGER.exception("Failed to load field exclusions from %s", path)
        _EXCLUSIONS_CACHE = (frozenset(), (), {})
        return _EXCLUSIONS_CACHE

    if not isinstance(raw, dict):
        _LOGGER.warning(
            "field_exclusions.json must be a JSON object, got %r", type(raw)
        )
        _EXCLUSIONS_CACHE = (frozenset(), (), {})
        return _EXCLUSIONS_CACHE

    global_exact, global_prefix = _split_patterns(raw.get("global", []))

    devices_raw = raw.get("devices", {})
    per_device: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {}
    if isinstance(devices_raw, dict):
        for device_id, patterns in devices_raw.items():
            if not isinstance(device_id, str) or device_id.startswith("_"):
                continue
            per_device[device_id.lower()] = _split_patterns(patterns)

    _EXCLUSIONS_CACHE = (global_exact, global_prefix, per_device)
    return _EXCLUSIONS_CACHE


def reload_exclusions() -> None:
    """Clear cached exclusions (call on integration setup)."""
    global _EXCLUSIONS_CACHE  # noqa: PLW0603
    _EXCLUSIONS_CACHE = None


def _matches(field_lc: str, exact: frozenset[str], prefixes: tuple[str, ...]) -> bool:
    if field_lc in exact:
        return True
    return any(field_lc.startswith(prefix) for prefix in prefixes)


def is_excluded(device_id: str, field_id: str) -> bool:
    """Return True when a TTN field should be hidden from Home Assistant."""
    global_exact, global_prefix, per_device = _load_exclusions()
    field_lc = field_id.lower()

    if _matches(field_lc, global_exact, global_prefix):
        return True

    device_rules = per_device.get(device_id.lower())
    if device_rules and _matches(field_lc, *device_rules):
        return True

    return False


def load_exclusions_summary() -> dict[str, object]:
    """Return a human-readable summary, used by startup logs."""
    global_exact, global_prefix, per_device = _load_exclusions()
    return {
        "global_exact": sorted(global_exact),
        "global_prefix": list(global_prefix),
        "devices": {k: sorted(v[0]) + list(v[1]) for k, v in per_device.items()},
    }
