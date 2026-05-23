"""Load editable TTN field → Home Assistant entity mappings."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal, TypedDict

_LOGGER = logging.getLogger(__name__)

PlatformType = Literal["sensor", "binary_sensor"]

_FIELD_MAPPINGS: dict[str, FieldMappingDict] | None = None


class FieldMappingDict(TypedDict, total=False):
    """Mapping from a TTN decoded_payload field to HA entity metadata."""

    platform: PlatformType
    friendly_name: str
    unit: str
    device_class: str
    state_class: str
    entity_category: str
    suggested_display_precision: str
    state_on: list[str | int | bool]
    state_off: list[str | int | bool]


class SensorAttrDict(TypedDict, total=False):
    """Sensor metadata applied to HA entities."""

    unit: str
    device_class: str
    state_class: str
    entity_category: str
    suggested_display_precision: str
    friendly_name: str


def reload_field_mappings() -> None:
    """Clear cached field mappings (call after file edits on restart)."""
    global _FIELD_MAPPINGS  # noqa: PLW0603
    _FIELD_MAPPINGS = None


def _normalize_state_values(raw: object) -> list[str | int | bool]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return [raw]


def _mapping_from_entry(entry: dict) -> FieldMappingDict | None:
    """Build a FieldMappingDict from one JSON entry (without keys)."""
    keys = entry.get("keys")
    if not keys:
        _LOGGER.warning("field_mappings entry missing keys: %s", entry)
        return None

    mapping: FieldMappingDict = {
        k: v for k, v in entry.items() if k != "keys" and not str(k).startswith("_")
    }
    if "state_on" in mapping:
        mapping["state_on"] = _normalize_state_values(mapping["state_on"])
    if "state_off" in mapping:
        mapping["state_off"] = _normalize_state_values(mapping["state_off"])
    return mapping


def _load_field_mappings() -> dict[str, FieldMappingDict]:
    global _FIELD_MAPPINGS  # noqa: PLW0603
    if _FIELD_MAPPINGS is not None:
        return _FIELD_MAPPINGS

    path = Path(__file__).with_name("field_mappings.json")
    if not path.is_file():
        _FIELD_MAPPINGS = {}
        return _FIELD_MAPPINGS

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LOGGER.exception("Failed to load field mappings from %s", path)
        _FIELD_MAPPINGS = {}
        return _FIELD_MAPPINGS

    mappings: dict[str, FieldMappingDict] = {}

    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            mapping = _mapping_from_entry(entry)
            if not mapping:
                continue
            keys = entry.get("keys", [])
            if not isinstance(keys, list):
                continue
            for key in keys:
                mappings[str(key).lower()] = dict(mapping)
    elif isinstance(raw, dict):
        # Legacy: { "field_name": { ...metadata } }
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            if str(key).startswith("_"):
                continue
            mapping = dict(value)
            if "state_on" in mapping:
                mapping["state_on"] = _normalize_state_values(mapping["state_on"])
            if "state_off" in mapping:
                mapping["state_off"] = _normalize_state_values(mapping["state_off"])
            mappings[str(key).lower()] = mapping
    else:
        _LOGGER.warning(
            "field_mappings.json must be a JSON array of mappings or a legacy object"
        )

    _FIELD_MAPPINGS = mappings
    return _FIELD_MAPPINGS


def get_field_mapping(field_id: str) -> FieldMappingDict:
    """Return configured mapping for a TTN field name."""
    return dict(_load_field_mappings().get(field_id.lower(), {}))


def get_field_platform(field_id: str) -> PlatformType:
    """Return HA platform to use for a TTN field."""
    platform = get_field_mapping(field_id).get("platform", "sensor")
    if platform == "binary_sensor":
        return "binary_sensor"
    return "sensor"


def default_field_attr(field_id: str) -> SensorAttrDict:
    """Return built-in metadata for a TTN field name, if configured."""
    mapping = get_field_mapping(field_id)
    attr: SensorAttrDict = {}
    for key in (
        "unit",
        "device_class",
        "state_class",
        "entity_category",
        "suggested_display_precision",
        "friendly_name",
    ):
        if key in mapping:
            attr[key] = str(mapping[key])
    return attr


def merge_field_attr(
    decoder_attr: SensorAttrDict, field_id: str
) -> FieldMappingDict:
    """Merge file mapping with decoder-provided _sensor_attr (decoder wins)."""
    merged: FieldMappingDict = get_field_mapping(field_id)
    merged.update(decoder_attr)
    return merged


def value_is_on(value: object, mapping: FieldMappingDict) -> bool | None:
    """Map a TTN value to binary on/off using optional state_on/state_off lists."""
    if isinstance(value, bool):
        return value

    state_on = mapping.get("state_on", [])
    state_off = mapping.get("state_off", [])

    if value in state_on:
        return True
    if value in state_off:
        return False

    if isinstance(value, str):
        lowered = value.lower()
        if any(isinstance(item, str) and item.lower() == lowered for item in state_on):
            return True
        if any(isinstance(item, str) and item.lower() == lowered for item in state_off):
            return False

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in state_on:
            return True
        if value in state_off:
            return False

    return None
