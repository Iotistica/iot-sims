#!/usr/bin/env python3
"""
Generates a ~500-device BACnet load-test profile, scaled up from the same
equipment templates used by loadtest-100-devices.json (and the admin UI's own
TemplatePickerModal.vue) — a larger high-rise campus rather than a naive 5x
multiply of the 100-device mix: central plant equipment (BMS/chiller/boiler)
scales with redundancy, not floor count, while VAV/FCU terminal units carry
almost all of the extra device count as floor area grows.

Device mix (~500 devices, 30 floors):
    1  BMS
    4  Chiller Plant
    3  Boiler Plant
   20  AHU        (roughly one per 1.5 floors)
   20  Meter       (per-floor/wing sub-metering)
   30  Lighting    (one per floor)
  300  VAV
  122  FCU
  ---
  500 devices

device_instance range: 5000-5499 (kept clear of loadtest-100's 2000-2099
range so both profiles can be imported side by side without collisions).

Usage:
    python scripts/gen_loadtest_500_devices.py
Writes profiles/loadtest-500-devices.json.
"""
import json
from pathlib import Path

# ─── BACnet templates (object_type-based), copied verbatim from
# bacnet-simulator/admin/src/components/TemplatePickerModal.vue ──────────────

BACNET_TEMPLATES = {
    "ahu": [
        {"object_type": "binary-input",  "object_instance":  1, "name": "SF-Run",              "units": "no-units",              "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "binary-input",  "object_instance":  2, "name": "RF-Run",              "units": "no-units",              "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "analog-input",  "object_instance":  3, "name": "SF-Speed",            "units": "percent",               "behavior": "sine",        "behavior_params": '{"base":75,"amplitude":15,"period_hours":12}'},
        {"object_type": "analog-input",  "object_instance":  4, "name": "RF-Speed",            "units": "percent",               "behavior": "sine",        "behavior_params": '{"base":70,"amplitude":12,"period_hours":12}'},
        {"object_type": "analog-input",  "object_instance":  5, "name": "SAT",                 "units": "degrees-celsius",       "behavior": "noise",       "behavior_params": '{"base":13,"noise":0.4}'},
        {"object_type": "analog-input",  "object_instance":  6, "name": "RAT",                 "units": "degrees-celsius",       "behavior": "sine",        "behavior_params": '{"base":22,"amplitude":2,"period_hours":24}'},
        {"object_type": "analog-input",  "object_instance":  7, "name": "MAT",                 "units": "degrees-celsius",       "behavior": "noise",       "behavior_params": '{"base":16,"noise":0.8}'},
        {"object_type": "analog-input",  "object_instance":  8, "name": "OAT",                 "units": "degrees-celsius",       "behavior": "sine",        "behavior_params": '{"base":12,"amplitude":8,"period_hours":24}'},
        {"object_type": "analog-output", "object_instance":  9, "name": "OAD-Position",        "units": "percent",               "behavior": "sine",        "behavior_params": '{"base":28,"amplitude":18,"period_hours":24}'},
        {"object_type": "analog-output", "object_instance": 10, "name": "CC-Valve",            "units": "percent",               "behavior": "sine",        "behavior_params": '{"base":55,"amplitude":25,"period_hours":12}'},
        {"object_type": "analog-output", "object_instance": 11, "name": "HC-Valve",            "units": "percent",               "behavior": "sine",        "behavior_params": '{"base":10,"amplitude":9,"period_hours":24}'},
        {"object_type": "analog-input",  "object_instance": 12, "name": "SA-Flow",             "units": "cubic-feet-per-minute", "behavior": "noise",       "behavior_params": '{"base":8500,"noise":250}'},
        {"object_type": "analog-input",  "object_instance": 13, "name": "SA-Static-Pressure",  "units": "pascals",               "behavior": "noise",       "behavior_params": '{"base":375,"noise":12}'},
        {"object_type": "binary-input",  "object_instance": 14, "name": "Filter-DP-Alarm",     "units": "no-units",              "behavior": "manual",      "behavior_params": '{"value":false}'},
        {"object_type": "binary-input",  "object_instance": 15, "name": "Freeze-Stat",         "units": "no-units",              "behavior": "manual",      "behavior_params": '{"value":false}'},
    ],
    "vav": [
        {"object_type": "analog-input",  "object_instance": 1, "name": "Zone-Temp",      "units": "degrees-celsius",       "behavior": "noise",       "behavior_params": '{"base":22,"noise":0.3}'},
        {"object_type": "analog-value",  "object_instance": 2, "name": "Zone-Setpoint",  "units": "degrees-celsius",       "behavior": "constant",    "behavior_params": '{"value":22}'},
        {"object_type": "analog-input",  "object_instance": 3, "name": "Damper-Pos",     "units": "percent",               "behavior": "noise",       "behavior_params": '{"base":55,"noise":3}'},
        {"object_type": "analog-output", "object_instance": 4, "name": "Damper-Cmd",     "units": "percent",               "behavior": "sine",        "behavior_params": '{"base":55,"amplitude":14,"period_hours":8}'},
        {"object_type": "analog-input",  "object_instance": 5, "name": "Zone-Airflow",   "units": "cubic-feet-per-minute", "behavior": "noise",       "behavior_params": '{"base":350,"noise":18}'},
        {"object_type": "analog-output", "object_instance": 6, "name": "Reheat-Valve",   "units": "percent",               "behavior": "sine",        "behavior_params": '{"base":0,"amplitude":10,"period_hours":12}'},
        {"object_type": "binary-input",  "object_instance": 7, "name": "Occupancy",      "units": "no-units",              "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "analog-input",  "object_instance": 8, "name": "Zone-CO2",       "units": "parts-per-million",     "behavior": "random_walk", "behavior_params": '{"value":650,"step":30,"min":400,"max":1200}'},
    ],
    "fcu": [
        {"object_type": "analog-input",  "object_instance": 1, "name": "Room-Temp",      "units": "degrees-celsius", "behavior": "sine",     "behavior_params": '{"base":23,"amplitude":1,"period_hours":24}'},
        {"object_type": "analog-value",  "object_instance": 2, "name": "Room-Setpoint",  "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":22}'},
        {"object_type": "analog-input",  "object_instance": 3, "name": "Coil-Temp",      "units": "degrees-celsius", "behavior": "noise",    "behavior_params": '{"base":12,"noise":0.5}'},
        {"object_type": "analog-output", "object_instance": 4, "name": "Cooling-Valve",  "units": "percent",         "behavior": "manual",   "behavior_params": '{"value":0}'},
        {"object_type": "analog-output", "object_instance": 5, "name": "Heating-Valve",  "units": "percent",         "behavior": "manual",   "behavior_params": '{"value":0}'},
        {"object_type": "binary-output", "object_instance": 6, "name": "Fan-Low-Speed",  "units": "no-units",        "behavior": "manual",   "behavior_params": '{"value":true}'},
        {"object_type": "binary-output", "object_instance": 7, "name": "Fan-High-Speed", "units": "no-units",        "behavior": "manual",   "behavior_params": '{"value":false}'},
    ],
    "chiller": [
        {"object_type": "binary-input", "object_instance":  1, "name": "CH-1-Run",              "units": "no-units",          "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "analog-input", "object_instance":  2, "name": "CH-1-kW",               "units": "kilowatts",         "behavior": "random_walk", "behavior_params": '{"value":212,"step":8,"min":80,"max":320}'},
        {"object_type": "analog-input", "object_instance":  3, "name": "CH-1-COP",              "units": "no-units",          "behavior": "noise",       "behavior_params": '{"base":5.8,"noise":0.2}'},
        {"object_type": "binary-input", "object_instance":  4, "name": "CH-2-Run",              "units": "no-units",          "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "analog-input", "object_instance":  5, "name": "CH-2-kW",               "units": "kilowatts",         "behavior": "random_walk", "behavior_params": '{"value":198,"step":8,"min":80,"max":320}'},
        {"object_type": "analog-input", "object_instance":  6, "name": "CH-2-COP",              "units": "no-units",          "behavior": "noise",       "behavior_params": '{"base":5.6,"noise":0.2}'},
        {"object_type": "analog-input", "object_instance":  7, "name": "CW-Supply-Temp",        "units": "degrees-celsius",   "behavior": "noise",       "behavior_params": '{"base":6.5,"noise":0.2}'},
        {"object_type": "analog-input", "object_instance":  8, "name": "CW-Return-Temp",        "units": "degrees-celsius",   "behavior": "noise",       "behavior_params": '{"base":12.2,"noise":0.2}'},
        {"object_type": "analog-input", "object_instance":  9, "name": "CW-Flow",               "units": "liters-per-second", "behavior": "noise",       "behavior_params": '{"base":48,"noise":1.5}'},
        {"object_type": "analog-input", "object_instance": 10, "name": "CW-Diff-Pressure",      "units": "pascals",           "behavior": "noise",       "behavior_params": '{"base":225,"noise":8}'},
        {"object_type": "binary-input", "object_instance": 11, "name": "CT-Fan-1-Run",          "units": "no-units",          "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "binary-input", "object_instance": 12, "name": "CT-Fan-2-Run",          "units": "no-units",          "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "analog-input", "object_instance": 13, "name": "CT-Leaving-Water-Temp", "units": "degrees-celsius",   "behavior": "noise",       "behavior_params": '{"base":29.5,"noise":0.5}'},
        {"object_type": "binary-input", "object_instance": 15, "name": "CW-Pump-1-Run",         "units": "no-units",          "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "binary-input", "object_instance": 16, "name": "CW-Pump-2-Run",         "units": "no-units",          "behavior": "manual",      "behavior_params": '{"value":false}'},
    ],
    "boiler": [
        {"object_type": "binary-input", "object_instance":  1, "name": "BLR-1-Run",        "units": "no-units",              "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "analog-input", "object_instance":  2, "name": "BLR-1-Firing-Rate", "units": "percent",               "behavior": "noise",       "behavior_params": '{"base":62,"noise":5}'},
        {"object_type": "analog-input", "object_instance":  3, "name": "BLR-1-Flue-Temp",  "units": "degrees-celsius",       "behavior": "noise",       "behavior_params": '{"base":88,"noise":3}'},
        {"object_type": "binary-input", "object_instance":  4, "name": "BLR-2-Run",        "units": "no-units",              "behavior": "manual",      "behavior_params": '{"value":false}'},
        {"object_type": "analog-input", "object_instance":  5, "name": "BLR-2-Firing-Rate", "units": "percent",              "behavior": "manual",      "behavior_params": '{"value":0}'},
        {"object_type": "analog-input", "object_instance":  6, "name": "HW-Supply-Temp",   "units": "degrees-celsius",       "behavior": "noise",       "behavior_params": '{"base":71,"noise":0.8}'},
        {"object_type": "analog-input", "object_instance":  7, "name": "HW-Return-Temp",   "units": "degrees-celsius",       "behavior": "noise",       "behavior_params": '{"base":58.5,"noise":0.8}'},
        {"object_type": "analog-input", "object_instance":  8, "name": "HW-Diff-Pressure", "units": "pascals",               "behavior": "noise",       "behavior_params": '{"base":180,"noise":6}'},
        {"object_type": "analog-input", "object_instance":  9, "name": "Gas-Flow",         "units": "cubic-feet-per-minute", "behavior": "random_walk", "behavior_params": '{"value":44,"step":3,"min":10,"max":85}'},
        {"object_type": "binary-input", "object_instance": 10, "name": "HW-Pump-1-Run",    "units": "no-units",              "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "binary-input", "object_instance": 11, "name": "HW-Pump-2-Run",    "units": "no-units",              "behavior": "manual",      "behavior_params": '{"value":false}'},
    ],
    "bms": [
        {"object_type": "binary-value", "object_instance": 1, "name": "Building-Occupied",    "units": "no-units",       "behavior": "manual",      "behavior_params": '{"value":true}'},
        {"object_type": "analog-value", "object_instance": 2, "name": "Active-Alarms",        "units": "no-units",       "behavior": "random_walk", "behavior_params": '{"value":2,"step":1,"min":0,"max":8}'},
        {"object_type": "analog-input", "object_instance": 3, "name": "Energy-Today-kWh",     "units": "kilowatt-hours", "behavior": "random_walk", "behavior_params": '{"value":430,"step":12,"min":0,"max":2000}'},
        {"object_type": "analog-input", "object_instance": 4, "name": "Peak-Demand-kW",       "units": "kilowatts",      "behavior": "random_walk", "behavior_params": '{"value":182,"step":4,"min":50,"max":320}'},
        {"object_type": "analog-input", "object_instance": 5, "name": "Outside-Air-Temp",     "units": "degrees-celsius","behavior": "sine",        "behavior_params": '{"base":12,"amplitude":8,"period_hours":24}'},
        {"object_type": "analog-input", "object_instance": 6, "name": "Outside-Air-Humidity", "units": "percent",        "behavior": "sine",        "behavior_params": '{"base":55,"amplitude":15,"period_hours":24}'},
    ],
    "meter": [
        {"object_type": "analog-input", "object_instance": 1, "name": "Active-Power-kW", "units": "kilowatts",      "behavior": "noise",       "behavior_params": '{"base":45,"noise":3}'},
        {"object_type": "analog-input", "object_instance": 2, "name": "Energy-kWh",      "units": "kilowatt-hours", "behavior": "random_walk", "behavior_params": '{"value":1000,"step":0.05,"min":0,"max":999999}'},
        {"object_type": "analog-input", "object_instance": 3, "name": "Voltage-L1",      "units": "volts",          "behavior": "noise",       "behavior_params": '{"base":230,"noise":2}'},
        {"object_type": "analog-input", "object_instance": 4, "name": "Voltage-L2",      "units": "volts",          "behavior": "noise",       "behavior_params": '{"base":230,"noise":2}'},
        {"object_type": "analog-input", "object_instance": 5, "name": "Current-L1",      "units": "amperes",        "behavior": "noise",       "behavior_params": '{"base":65,"noise":4}'},
        {"object_type": "analog-input", "object_instance": 6, "name": "Power-Factor",    "units": "no-units",       "behavior": "noise",       "behavior_params": '{"base":0.92,"noise":0.03}'},
    ],
    "lighting": [
        {"object_type": "analog-output", "object_instance": 1, "name": "Zone-1-Level",       "units": "percent",  "behavior": "manual",   "behavior_params": '{"value":100}'},
        {"object_type": "analog-output", "object_instance": 2, "name": "Zone-2-Level",       "units": "percent",  "behavior": "manual",   "behavior_params": '{"value":80}'},
        {"object_type": "analog-output", "object_instance": 3, "name": "Zone-3-Level",       "units": "percent",  "behavior": "manual",   "behavior_params": '{"value":60}'},
        {"object_type": "binary-output", "object_instance": 4, "name": "Zone-1-Override",    "units": "no-units", "behavior": "manual",   "behavior_params": '{"value":false}'},
        {"object_type": "binary-output", "object_instance": 5, "name": "Zone-2-Override",    "units": "no-units", "behavior": "manual",   "behavior_params": '{"value":false}'},
        {"object_type": "binary-value",  "object_instance": 6, "name": "Occupancy-Status",   "units": "no-units", "behavior": "manual",   "behavior_params": '{"value":true}'},
        {"object_type": "analog-value",  "object_instance": 7, "name": "Occupancy-Setpoint", "units": "percent",  "behavior": "constant", "behavior_params": '{"value":100}'},
        {"object_type": "analog-value",  "object_instance": 8, "name": "Standby-Setpoint",   "units": "percent",  "behavior": "constant", "behavior_params": '{"value":30}'},
    ],
}

VENDORS = {
    "ahu":      ("Johnson Controls", "FEC26B"),
    "vav":      ("Siemens Building Technologies", "RXB29.1"),
    "fcu":      ("Daikin Applied", "FXFQ-A"),
    "chiller":  ("Trane Technologies", "Tracer SC+"),
    "boiler":   ("Honeywell International", "Excel 500"),
    "bms":      ("Honeywell International", "WEBs-N4"),
    "meter":    ("Schneider Electric", "PowerLogic PM5560"),
    "lighting": ("LOYTEC electronics GmbH", "L-DALI/4"),
}

DESCRIPTIONS = {
    "ahu": "Air handling unit",
    "vav": "Variable air volume terminal unit",
    "fcu": "Fan coil unit",
    "chiller": "Chiller plant",
    "boiler": "Hot water boiler plant",
    "bms": "Building management system supervisor",
    "meter": "Electrical sub-meter",
    "lighting": "DALI-to-BACnet lighting gateway",
}

FLOORS = 30
DEVICE_INSTANCE_BASE = 5000


def build_device_plan():
    """Returns list of (key, name) in a realistic ~500-device high-rise campus layout."""
    plan = []
    plan.append(("bms", "BMS-Gateway"))
    for i in range(1, 5):
        plan.append(("chiller", f"Chiller-Plant-{i}"))
    for i in range(1, 4):
        plan.append(("boiler", f"Boiler-Plant-{i}"))
    for i in range(1, 21):
        plan.append(("ahu", f"AHU-{i}"))
    for i in range(1, 21):
        plan.append(("meter", f"Meter-{i}"))
    for f in range(1, FLOORS + 1):
        plan.append(("lighting", f"Lighting-F{f}"))

    # VAVs and FCUs, round-robin distributed across floors (zone letter
    # increments each time a floor is revisited) so the exact target count is
    # always reached regardless of how it divides against FLOORS.
    def distribute(key: str, prefix: str, count: int):
        for i in range(count):
            floor = 1 + (i % FLOORS)
            zone = chr(ord("A") + (i // FLOORS))
            plan.append((key, f"{prefix}-F{floor}-{zone}"))

    distribute("vav", "VAV", 300)
    distribute("fcu", "FCU", 122)

    return plan


def gen_bacnet_profile():
    plan = build_device_plan()
    devices = []
    device_instance = DEVICE_INSTANCE_BASE
    for key, name in plan:
        vendor, model = VENDORS[key]
        objects = [dict(o) for o in BACNET_TEMPLATES[key]]
        for o in objects:
            o["enabled"] = True
        devices.append({
            "device_instance": device_instance,
            "name": name,
            "description": DESCRIPTIONS[key],
            "vendor_name": vendor,
            "model_name": model,
            "enabled": True,
            "objects": objects,
        })
        device_instance += 1
    return {"devices": devices}


if __name__ == "__main__":
    bacnet_profile = gen_bacnet_profile()
    bacnet_points = sum(len(d["objects"]) for d in bacnet_profile["devices"])
    print(f"BACnet: {len(bacnet_profile['devices'])} devices, {bacnet_points} objects")

    # Sanity: device_instance uniqueness, object_instance uniqueness per device, name uniqueness
    instances = [d["device_instance"] for d in bacnet_profile["devices"]]
    assert len(instances) == len(set(instances)), "duplicate device_instance!"
    for d in bacnet_profile["devices"]:
        oi = [(o["object_type"], o["object_instance"]) for o in d["objects"]]
        assert len(oi) == len(set(oi)), f"duplicate object in {d['name']}"
    names = [d["name"] for d in bacnet_profile["devices"]]
    assert len(names) == len(set(names)), "duplicate device name!"

    out_path = Path(__file__).resolve().parent.parent / "profiles" / "loadtest-500-devices.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bacnet_profile, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
