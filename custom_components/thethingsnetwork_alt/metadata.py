"""Device display names and field metadata helpers."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_DEVICE_NAMES: dict[str, str] | None = None


def _agent_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    # #region agent log
    try:
        import time

        payload = {
            "sessionId": "b0653e",
            "runId": "post-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        line = json.dumps(payload, default=str) + "\n"
        for dest in (
            Path(__file__).with_name("_debug_b0653e.ndjson"),
            Path("/config/custom_components/thethingsnetwork_alt/_debug_b0653e.ndjson"),
        ):
            try:
                dest.open("a", encoding="utf-8").write(line)
            except OSError:
                pass
    except Exception:
        pass
    # #endregion


def _load_device_names() -> dict[str, str]:
    global _DEVICE_NAMES  # noqa: PLW0603
    if _DEVICE_NAMES is not None:
        return _DEVICE_NAMES

    path = Path(__file__).with_name("device_names.json")
    if not path.is_file():
        _DEVICE_NAMES = {}
        # #region agent log
        _agent_log("A", "metadata.py:_load_device_names", "names file missing", {"path": str(path)})
        # #endregion
        return _DEVICE_NAMES

    try:
        text = path.read_text(encoding="utf-8")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as err:
            # Trailing commas (illegal JSON) have emptied this map in production.
            raw = json.loads(re.sub(r",(\s*[}\]])", r"\1", text))
            _LOGGER.warning(
                "device_names.json had invalid JSON (%s); loaded after trailing-comma strip",
                err,
            )
            # #region agent log
            _agent_log(
                "A",
                "metadata.py:_load_device_names",
                "names parse recovered",
                {"path": str(path), "err": str(err)},
            )
            # #endregion
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.exception("Failed to load device names from %s", path)
        _DEVICE_NAMES = {}
        # #region agent log
        _agent_log(
            "A",
            "metadata.py:_load_device_names",
            "names parse failed",
            {"path": str(path), "err_type": type(err).__name__, "err": str(err)},
        )
        # #endregion
        return _DEVICE_NAMES

    if not isinstance(raw, dict):
        _LOGGER.warning("device_names.json must be a JSON object, got %r", type(raw))
        _DEVICE_NAMES = {}
        return _DEVICE_NAMES

    _DEVICE_NAMES = {str(key): str(value) for key, value in raw.items()}
    # #region agent log
    _agent_log(
        "A",
        "metadata.py:_load_device_names",
        "names loaded",
        {"count": len(_DEVICE_NAMES), "sample_keys": list(_DEVICE_NAMES)[:5]},
    )
    # #endregion
    return _DEVICE_NAMES


def get_device_name(device_id: str) -> str | None:
    """Return a friendly device name for a TTN end-device ID, if configured."""
    name = _load_device_names().get(device_id)
    # #region agent log
    if name is None:
        _agent_log(
            "C",
            "metadata.py:get_device_name",
            "name lookup miss",
            {"device_id": device_id, "mapped": None, "fallback_would_be_device_id": True},
        )
    # #endregion
    return name


def load_device_names() -> dict[str, str]:
    """Return all configured TTN device ID → friendly name mappings."""
    return dict(_load_device_names())
