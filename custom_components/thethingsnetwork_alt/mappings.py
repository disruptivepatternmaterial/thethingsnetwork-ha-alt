"""Load editable TTN field → Home Assistant entity mappings."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal, TypedDict

_LOGGER = logging.getLogger(__name__)

PlatformType = Literal["sensor", "binary_sensor"]

_FIELD_MAPPINGS: dict[str, FieldMappingDict] | None = None

# Heuristic suffix → (unit, device_class, state_class) for unmapped fields.
# Keep conservative: only suffixes that strongly imply a single physical
# meaning. Applied case-insensitively after lowercasing the field_id.
_SUFFIX_HEURISTICS: tuple[tuple[str, dict[str, str]], ...] = (
    ("_mv", {"unit": "mV", "device_class": "voltage", "state_class": "measurement"}),
    ("_uv", {"unit": "µV", "device_class": "voltage", "state_class": "measurement"}),
    ("_v", {"unit": "V", "device_class": "voltage", "state_class": "measurement"}),
    ("_ma", {"unit": "mA", "device_class": "current", "state_class": "measurement"}),
    ("_a", {"unit": "A", "device_class": "current", "state_class": "measurement"}),
    ("_lux", {"unit": "lx", "device_class": "illuminance", "state_class": "measurement"}),
    ("_lx", {"unit": "lx", "device_class": "illuminance", "state_class": "measurement"}),
    ("_pct", {"unit": "%", "state_class": "measurement"}),
    ("_percent", {"unit": "%", "state_class": "measurement"}),
    ("_hpa", {"unit": "hPa", "device_class": "pressure", "state_class": "measurement"}),
    ("_pa", {"unit": "Pa", "device_class": "pressure", "state_class": "measurement"}),
    ("_kpa", {"unit": "kPa", "device_class": "pressure", "state_class": "measurement"}),
    ("_c", {"unit": "°C", "device_class": "temperature", "state_class": "measurement"}),
    ("_f", {"unit": "°F", "device_class": "temperature", "state_class": "measurement"}),
    ("_k", {"unit": "K", "device_class": "temperature", "state_class": "measurement"}),
    ("_mm", {"unit": "mm", "device_class": "distance", "state_class": "measurement"}),
    ("_cm", {"unit": "cm", "device_class": "distance", "state_class": "measurement"}),
    ("_m", {"unit": "m", "device_class": "distance", "state_class": "measurement"}),
    ("_km", {"unit": "km", "device_class": "distance", "state_class": "measurement"}),
    ("_g", {"unit": "g", "device_class": "weight", "state_class": "measurement"}),
    ("_kg", {"unit": "kg", "device_class": "weight", "state_class": "measurement"}),
    ("_dbm", {"unit": "dBm", "device_class": "signal_strength", "state_class": "measurement"}),
    ("_db", {"unit": "dB", "state_class": "measurement"}),
)


def _friendly_name_from_field_id(field_id: str) -> str:
    """Convert a snake_case TTN field name into a Title Case friendly name."""
    cleaned = field_id.replace("_", " ").strip()
    if not cleaned:
        return field_id
    # Preserve common all-caps tokens.
    parts = cleaned.split()
    capitalised: list[str] = []
    for part in parts:
        upper = part.upper()
        if upper in {"RSSI", "SNR", "GPS", "ID", "UV", "IR", "PM", "CO", "CO2", "VOC"}:
            capitalised.append(upper)
        else:
            capitalised.append(part.capitalize())
    return " ".join(capitalised)


def _heuristic_for_field(field_id: str) -> dict[str, str]:
    """Return inferred attrs based on a field_id suffix."""
    lower = field_id.lower()
    for suffix, attrs in _SUFFIX_HEURISTICS:
        if lower.endswith(suffix) and len(lower) > len(suffix):
            return dict(attrs)
    return {}


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
    """Return built-in metadata for a TTN field, falling back to heuristics."""
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

    if not attr:
        # No explicit mapping → infer from suffix and synthesize a friendly name.
        heuristic = _heuristic_for_field(field_id)
        for key, value in heuristic.items():
            attr[key] = value
        attr.setdefault("friendly_name", _friendly_name_from_field_id(field_id))
    elif "friendly_name" not in attr:
        attr["friendly_name"] = _friendly_name_from_field_id(field_id)

    return attr


def merge_field_attr(
    decoder_attr: SensorAttrDict, field_id: str
) -> FieldMappingDict:
    """Merge file mapping with decoder-provided _sensor_attr (decoder wins).

    When the field is not in `field_mappings.json` and the decoder did not
    provide a `_sensor_attr.<field>`, fall back to heuristics + an
    auto-generated friendly name so unmapped fields still look reasonable.
    """
    file_mapping: FieldMappingDict = get_field_mapping(field_id)

    if not file_mapping:
        heuristic = _heuristic_for_field(field_id)
        merged: FieldMappingDict = dict(heuristic)  # type: ignore[assignment]
        merged.setdefault("friendly_name", _friendly_name_from_field_id(field_id))
    else:
        merged = dict(file_mapping)
        merged.setdefault("friendly_name", _friendly_name_from_field_id(field_id))

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
