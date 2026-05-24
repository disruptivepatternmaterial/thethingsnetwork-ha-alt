"""Device display names and field metadata helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_DEVICE_NAMES: dict[str, str] | None = None


def _load_device_names() -> dict[str, str]:
    global _DEVICE_NAMES  # noqa: PLW0603
    if _DEVICE_NAMES is not None:
        return _DEVICE_NAMES

    path = Path(__file__).with_name("device_names.json")
    if not path.is_file():
        _DEVICE_NAMES = {}
        return _DEVICE_NAMES

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LOGGER.exception("Failed to load device names from %s", path)
        _DEVICE_NAMES = {}
        return _DEVICE_NAMES

    if not isinstance(raw, dict):
        _LOGGER.warning("device_names.json must be a JSON object, got %r", type(raw))
        _DEVICE_NAMES = {}
        return _DEVICE_NAMES

    _DEVICE_NAMES = {str(key): str(value) for key, value in raw.items()}
    return _DEVICE_NAMES


def get_device_name(device_id: str) -> str | None:
    """Return a friendly device name for a TTN end-device ID, if configured."""
    return _load_device_names().get(device_id)


def load_device_names() -> dict[str, str]:
    """Return all configured TTN device ID → friendly name mappings."""
    return dict(_load_device_names())
