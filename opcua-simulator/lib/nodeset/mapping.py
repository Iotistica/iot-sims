"""
NodeSet2 DataType -> simulator data_type coercion, and the default behavior
assigned to freshly imported variables.

Scope note: this first pass intentionally skips the confidence-scored
behavior-suggestion engine described for a later phase (name/unit-pattern
heuristics like "contains 'temperature' -> suggest sine"). Every imported
variable gets `manual` behavior seeded with its parsed source value (or a
type-appropriate default) — the existing tag-edit UI already lets a user
change that to any of the simulator's behaviors afterward, so nothing is
locked in by this choice, it just doesn't try to guess.
"""
import json
from typing import Optional

from .models import ParseReport

# Simulator's own VALID_DATA_TYPES (lib/db.py) — kept as a local constant
# rather than importing lib.db here, so this module has no dependency on the
# SQLite layer and stays independently testable.
SIM_DATA_TYPES = {"Boolean", "Double", "Int32", "String"}

# NodeSet2 DataType tokens (well-known ns=0 numeric ids, and the friendly
# literal names some hand-authored files use directly in place of a proper
# alias) mapped onto what this simulator can actually represent.
_DATA_TYPE_MAP: dict[str, str] = {
    "i=1": "Boolean", "Boolean": "Boolean",
    "i=2": "Int32", "SByte": "Int32",
    "i=3": "Int32", "Byte": "Int32",
    "i=4": "Int32", "Int16": "Int32",
    "i=5": "Int32", "UInt16": "Int32",
    "i=6": "Int32", "Int32": "Int32",
    "i=7": "Int32", "UInt32": "Int32",
    "i=8": "Int32", "Int64": "Int32",
    "i=9": "Int32", "UInt64": "Int32",
    "i=10": "Double", "Float": "Double",
    "i=11": "Double", "Double": "Double",
    "i=12": "String", "String": "String",
    "i=13": "String", "DateTime": "String",
    "i=20": "String", "QualifiedName": "String",
    "i=21": "String", "LocalizedText": "String",
}
_LOSSY_TYPES = {"i=7", "UInt32", "i=8", "Int64", "i=9", "UInt64"}


def coerce_data_type(raw_data_type: Optional[str], node_id: str, report: ParseReport) -> str:
    """Map a parsed NodeSet2 DataType token onto one of SIM_DATA_TYPES,
    never raising — anything unrecognized (Guid, ByteString, a custom
    ns>0 UADataType, an enum, a structure) becomes String so the variable
    still gets imported, with a warning explaining the coercion."""
    if raw_data_type is None:
        report.warnings.append(f"{node_id}: no DataType specified, defaulting to Double")
        return "Double"

    token = raw_data_type.replace("ns=0;", "")
    sim_type = _DATA_TYPE_MAP.get(token)
    if sim_type is None:
        report.warnings.append(
            f"{node_id}: DataType '{raw_data_type}' is not natively supported by this simulator "
            f"(only Boolean/Double/Int32/String) — imported as String"
        )
        report.unsupported_features.append(f"{node_id}: unsupported DataType {raw_data_type}")
        return "String"

    if token in _LOSSY_TYPES:
        report.warnings.append(
            f"{node_id}: DataType '{raw_data_type}' narrowed to {sim_type} — values outside its range will clip"
        )
    return sim_type


def coerce_initial_value(value: object, sim_type: str):
    """Best-effort cast of whatever the parser extracted from <Value> (which
    may be None if the source had no value, an array, or an unsupported
    complex type) into something valid for `sim_type`."""
    if sim_type == "Boolean":
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in ("true", "1")
    if sim_type == "Double":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if sim_type == "Int32":
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return 0
    # String
    if value is None:
        return ""
    return str(value)


def default_behavior_for(sim_type: str, initial_value: object) -> tuple[str, str]:
    """Every imported variable starts as `manual`, seeded with its source
    value — deterministic, and it's the one behavior that already handles
    all four sim data types without extra params (see lib/behaviors.py's
    ManualBehavior). Returns (behavior_name, behavior_params_json)."""
    return "manual", json.dumps({"value": initial_value})
