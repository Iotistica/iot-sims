#!/usr/bin/env python3
"""One-time, idempotent backfill: apply the same Brick/Haystack semantic tags
Database.seed_default() assigns on a *fresh* database, but onto an existing
one where seed_default()'s "only seed if devices already exist" guard
correctly skipped them.

Safe to re-run: every UPDATE is scoped to `... IS NULL`, so a tag you (or
anyone) already set manually — through the admin UI or a prior run of this
script — is never overwritten. Matches purely by device/object name, so
devices you've added beyond the original seed_default() building layout
simply won't match anything here and are left untouched.

Usage (inside the sim-bacnet container, where the app's own Python env and
DATA_DIR are already set up correctly):
    docker exec sim-bacnet python3 scripts/backfill_semantic_tags.py

Or against an explicit DB file:
    python3 scripts/backfill_semantic_tags.py --db /path/to/bacnet_sim.db
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import DB_PATH  # noqa: E402

# device name -> equipment_type. Mirrors Database.seed_default() exactly —
# BMS-Gateway and the DALI-GW-* gateways are intentionally left untagged
# there (no single faithful Brick Equipment class for either), so they're
# absent here too.
DEVICE_EQUIPMENT_TYPE = {
    "Chiller-Plant": "Chiller",
    "HW-Plant": "Boiler",
    "AHU-1": "Air_Handling_Unit",
    "AHU-2": "Air_Handling_Unit",
    "VAV-L1-01": "Variable_Air_Volume_Box",
    "VAV-L1-02": "Variable_Air_Volume_Box",
    "VAV-L2-01": "Variable_Air_Volume_Box",
    "VAV-L2-02": "Variable_Air_Volume_Box",
    "VAV-L3-01": "Variable_Air_Volume_Box",
    "VAV-L3-02": "Variable_Air_Volume_Box",
    "VAV-L4-01": "Variable_Air_Volume_Box",
    "VAV-L4-02": "Variable_Air_Volume_Box",
}

# AHU-1/AHU-2 and all 8 VAVs share identical object-name sets — listed once
# per device group here, expanded into concrete (device, object) pairs below.
_AHU_POINTS = {
    "SF-Run": "Run_Status",
    "RF-Run": "Run_Status",
    "SAT": "Supply_Air_Temperature_Sensor",
    "RAT": "Return_Air_Temperature_Sensor",
    "MAT": "Mixed_Air_Temperature_Sensor",
    "OAT": "Outside_Air_Temperature_Sensor",
    "OAD-Position": "Damper_Position_Command",
    "CC-Valve": "Valve_Position_Command",
    "HC-Valve": "Valve_Position_Command",
    "SA-Flow": "Air_Flow_Sensor",
    "SA-Static-Pressure": "Static_Pressure_Sensor",
    "Filter-DP-Alarm": "Change_Filter_Alarm",
    "Freeze-Stat": "Freeze_Status",
}
_VAV_POINTS = {
    "Zone-Temp": "Zone_Air_Temperature_Sensor",
    "Cooling-SP": "Cooling_Temperature_Setpoint",
    "Heating-SP": "Heating_Temperature_Setpoint",
    "Damper-Cmd": "Damper_Position_Command",
    "Zone-Airflow": "Air_Flow_Sensor",
    "Reheat-Valve": "Valve_Position_Command",
    "Occupancy": "Occupancy_Sensor",
    "Zone-CO2": "CO2_Level_Sensor",
}
_LIGHTING_POINTS = {
    "Zone-A-Lights-On": "On_Off_Command",
    "Zone-B-Lights-On": "On_Off_Command",
    "Zone-A-Dim-Level": "Lighting_Level_Command",
    "Zone-B-Dim-Level": "Lighting_Level_Command",
}

# (device_name, object_name) -> point_type
OBJECT_POINT_TYPE: dict[tuple[str, str], str] = {}
for _dev in ("AHU-1", "AHU-2"):
    for _obj, _pt in _AHU_POINTS.items():
        OBJECT_POINT_TYPE[(_dev, _obj)] = _pt
for _dev in ("VAV-L1-01", "VAV-L1-02", "VAV-L2-01", "VAV-L2-02",
             "VAV-L3-01", "VAV-L3-02", "VAV-L4-01", "VAV-L4-02"):
    for _obj, _pt in _VAV_POINTS.items():
        OBJECT_POINT_TYPE[(_dev, _obj)] = _pt
for _dev in ("DALI-GW-L1", "DALI-GW-L2", "DALI-GW-L3", "DALI-GW-L4"):
    for _obj, _pt in _LIGHTING_POINTS.items():
        OBJECT_POINT_TYPE[(_dev, _obj)] = _pt

OBJECT_POINT_TYPE.update({
    ("BMS-Gateway", "Energy-Today-kWh"): "Energy_Sensor",
    ("BMS-Gateway", "Peak-Demand-kW"): "Demand_Sensor",
    ("BMS-Gateway", "Outside-Air-Temp"): "Outside_Air_Temperature_Sensor",
    ("BMS-Gateway", "Outside-Air-Humidity"): "Outside_Air_Humidity_Sensor",
    ("Chiller-Plant", "CH-1-Run"): "Run_Status",
    ("Chiller-Plant", "CH-1-kW"): "Power_Sensor",
    ("Chiller-Plant", "CH-2-Run"): "Run_Status",
    ("Chiller-Plant", "CH-2-kW"): "Power_Sensor",
    ("Chiller-Plant", "CW-Supply-Temp"): "Leaving_Chilled_Water_Temperature_Sensor",
    ("Chiller-Plant", "CW-Return-Temp"): "Entering_Chilled_Water_Temperature_Sensor",
    ("Chiller-Plant", "CW-Flow"): "Water_Flow_Sensor",
    ("Chiller-Plant", "CW-Diff-Pressure"): "Water_Differential_Pressure_Sensor",
    ("Chiller-Plant", "CT-Fan-1-Run"): "Run_Status",
    ("Chiller-Plant", "CT-Fan-2-Run"): "Run_Status",
    ("Chiller-Plant", "CW-Pump-1-Run"): "Run_Status",
    ("Chiller-Plant", "CW-Pump-2-Run"): "Run_Status",
    ("HW-Plant", "BLR-1-Run"): "Run_Status",
    ("HW-Plant", "BLR-2-Run"): "Run_Status",
    ("HW-Plant", "HW-Supply-Temp"): "Leaving_Hot_Water_Temperature_Sensor",
    ("HW-Plant", "HW-Return-Temp"): "Entering_Hot_Water_Temperature_Sensor",
    ("HW-Plant", "HW-Diff-Pressure"): "Water_Differential_Pressure_Sensor",
    ("HW-Plant", "HW-Pump-1-Run"): "Run_Status",
    ("HW-Plant", "HW-Pump-2-Run"): "Run_Status",
})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH), help="Path to bacnet_sim.db")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    tagged_devices = 0
    for name, equip in DEVICE_EQUIPMENT_TYPE.items():
        cur = conn.execute(
            "UPDATE devices SET equipment_type=? WHERE name=? AND equipment_type IS NULL",
            (equip, name),
        )
        tagged_devices += cur.rowcount

    devices = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM devices")}
    tagged_objects = 0
    for (dev_name, obj_name), point_type in OBJECT_POINT_TYPE.items():
        dev_id = devices.get(dev_name)
        if dev_id is None:
            continue
        cur = conn.execute(
            "UPDATE objects SET point_type=? WHERE device_id=? AND name=? AND point_type IS NULL",
            (point_type, dev_id, obj_name),
        )
        tagged_objects += cur.rowcount

    conn.commit()
    conn.close()
    print(f"Backfilled equipment_type on {tagged_devices} device(s), point_type on {tagged_objects} object(s).")


if __name__ == "__main__":
    main()
