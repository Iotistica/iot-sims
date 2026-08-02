"""CSV import/export for BACnet EDE (Engineering Data Exchange) point lists.

Scope is intentionally narrow (see GH issue #11 for what's deferred):
  - Only the object types the simulator supports (analog-*/binary-*/multi-state-*).
  - Only the engineering units already offered in the simulator UI, with
    numeric codes taken from bacpypes3's own EngineeringUnits enum instead of
    a hand-kept table.
  - Imported points always land as "constant" behavior seeded from the
    row's Present-Value-Default column; reconstructing sine/ramp/etc.
    behavior_params from an EDE row isn't attempted. Multi-state points
    import with the default number_of_states (2) — EDE has no column for
    it, so bump it in the object editor afterwards if needed.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Optional

from bacpypes3.basetypes import EngineeringUnits

OBJECT_TYPE_CODE = {
    "analog-input": 0, "analog-output": 1, "analog-value": 2,
    "binary-input": 3, "binary-output": 4, "binary-value": 5,
    "multi-state-input": 13, "multi-state-output": 14, "multi-state-value": 19,
}
OBJECT_TYPE_ABBR = {
    "analog-input": "AI", "analog-output": "AO", "analog-value": "AV",
    "binary-input": "BI", "binary-output": "BO", "binary-value": "BV",
    "multi-state-input": "MSI", "multi-state-output": "MSO", "multi-state-value": "MSV",
}
CODE_TO_OBJECT_TYPE = {v: k for k, v in OBJECT_TYPE_CODE.items()}
ABBR_TO_OBJECT_TYPE = {v: k for k, v in OBJECT_TYPE_ABBR.items()}

# Same set the admin UI offers (BACNET_UNITS in bacnet_simulator.py). Codes
# come from bacpypes3 so they match the real BACnet standard values.
UNIT_NAMES = [
    "no-units", "degrees-celsius", "degrees-fahrenheit", "degrees-kelvin",
    "percent", "parts-per-million", "kilowatts", "watts", "kilowatt-hours",
    "amperes", "volts", "cubic-feet-per-minute", "liters-per-second",
    "pascals", "kilopascals", "bars", "cubic-meters-per-hour",
    "revolutions-per-minute", "meters-per-second", "luxes",
]
UNIT_CODE = {name: int(EngineeringUnits(name)) for name in UNIT_NAMES}
CODE_TO_UNIT = {v: k for k, v in UNIT_CODE.items()}

EDE_HEADER = [
    "Keyname", "Device-Instance", "Object-Type", "Object-Instance",
    "Object-Name", "Description", "Present-Value-Default",
    "Min-Present-Value", "Max-Present-Value", "Unit-Code", "Notes",
]


def _present_value_default(behavior: str, params: dict) -> Optional[float]:
    for key in ("value", "start", "center"):
        if key in params:
            try:
                return float(params[key])
            except (TypeError, ValueError):
                return None
    return None


def devices_to_ede(devices: list[dict], project_name: str = "") -> str:
    """Serialize devices (each with an "objects" list, same shape as a saved
    profile's data) into an EDE CSV. Works for a single device or a whole
    project — every row carries its own Device-Instance."""
    buf = io.StringIO()
    buf.write(f"#Project;{project_name}\n#EdeVersion;iotistica-1\n\n")
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(EDE_HEADER)
    for dev in devices:
        instance = dev["device_instance"]
        for obj in dev.get("objects", []):
            otype = obj["object_type"]
            abbr = OBJECT_TYPE_ABBR.get(otype, otype)
            try:
                params = json.loads(obj.get("behavior_params") or "{}")
            except (TypeError, ValueError):
                params = {}
            default = _present_value_default(obj.get("behavior", ""), params)
            writer.writerow([
                f"{instance}.{abbr}{obj['object_instance']}",
                instance,
                OBJECT_TYPE_CODE.get(otype, ""),
                obj["object_instance"],
                obj["name"],
                dev.get("description", ""),
                default if default is not None else "",
                "",
                "",
                UNIT_CODE.get(obj.get("units", "no-units"), ""),
                "",
            ])
    return buf.getvalue()


def _resolve_object_type(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return CODE_TO_OBJECT_TYPE.get(int(raw))
    if raw.upper() in ABBR_TO_OBJECT_TYPE:
        return ABBR_TO_OBJECT_TYPE[raw.upper()]
    normalized = raw.lower().replace("_", "-")
    return normalized if normalized in OBJECT_TYPE_CODE else None


def parse_ede_rows(text: str) -> list[dict]:
    """Parse an EDE CSV into a flat list of
    {device_instance, object_type, object_instance, name, units, behavior_params}.
    Rows with an unrecognized object type/instance/device-instance are
    skipped rather than aborting the whole import."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []

    reader = csv.reader(lines, delimiter=";")
    header = [h.strip().lower() for h in next(reader)]

    def col(row: list[str], name: str) -> str:
        try:
            return row[header.index(name)].strip()
        except (ValueError, IndexError):
            return ""

    results: list[dict] = []
    for row in reader:
        if not row or not any(row):
            continue

        object_type = _resolve_object_type(col(row, "object-type"))
        if object_type is None:
            continue
        try:
            device_instance = int(col(row, "device-instance"))
            object_instance = int(col(row, "object-instance"))
        except ValueError:
            continue

        name = col(row, "object-name") or f"{object_type}-{object_instance}"

        unit_col = col(row, "unit-code")
        units = CODE_TO_UNIT.get(int(unit_col), "no-units") if unit_col.isdigit() else "no-units"

        default_col = col(row, "present-value-default")
        try:
            default_value: Any = float(default_col) if default_col else 0
        except ValueError:
            default_value = 0
        if object_type.startswith("binary"):
            default_value = bool(default_value)
        elif object_type.startswith("multi-state"):
            default_value = max(1, round(default_value)) if default_value else 1

        results.append({
            "device_instance": device_instance,
            "object_type": object_type,
            "object_instance": object_instance,
            "name": name,
            "units": units,
            "behavior": "constant",
            "behavior_params": json.dumps({"value": default_value}),
            "enabled": 1,
        })
    return results


def rows_to_devices(rows: list[dict], device_name: str = "") -> dict:
    """Group parsed EDE rows by device-instance into the {"devices":[...]}
    shape the profile import/export pipeline already uses. Devices that
    don't otherwise have a name use device_name (falling back to
    "Device-<instance>")."""
    devices_by_instance: dict[int, dict] = {}
    for row in rows:
        instance = row["device_instance"]
        dev = devices_by_instance.setdefault(instance, {
            "device_instance": instance,
            "name": device_name or f"Device-{instance}",
            "description": "",
            "vendor_name": "Iotistica",
            "model_name": "BACnet Simulator",
            "enabled": 1,
            "objects": [],
        })
        dev["objects"].append({
            "object_type": row["object_type"],
            "object_instance": row["object_instance"],
            "name": row["name"],
            "units": row["units"],
            "behavior": row["behavior"],
            "behavior_params": row["behavior_params"],
            "enabled": row["enabled"],
        })
    return {"devices": list(devices_by_instance.values())}
