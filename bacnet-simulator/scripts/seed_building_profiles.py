#!/usr/bin/env python3
"""
Seeds the simulator's Profiles library with a handful of realistic building
configurations (small / medium / large / industrial), each using real BACnet
vendors and plausible equipment lists. Profiles are saved via the existing
Profile feature (a row in the `profiles` table) — this does NOT touch the
currently-running devices/objects, so it's safe to run against a live
instance. Load a profile from the admin UI (or POST /profiles/{id}/load)
when you want to switch the simulator to it.

Usage (inside the simulator container, or anywhere with access to the DB):
    DATA_DIR=/data python scripts/seed_building_profiles.py

Re-running is safe — it only adds new profile rows; it won't touch or
duplicate anything if profiles with the same name already exist (existing
profiles with a matching name are left alone, not overwritten).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bacnet_simulator as sim  # noqa: E402


def obj(object_type: str, object_instance: int, name: str, units: str, behavior: str, params: dict) -> dict:
    return {
        "object_type": object_type,
        "object_instance": object_instance,
        "name": name,
        "units": units,
        "behavior": behavior,
        "behavior_params": json.dumps(params),
        "enabled": 1,
        "manual_value": None,
    }


def device(instance: int, name: str, description: str, vendor: str, model: str, objects: list[dict]) -> dict:
    return {
        "device_instance": instance,
        "name": name,
        "description": description,
        "vendor_name": vendor,
        "model_name": model,
        "enabled": 1,
        "objects": objects,
    }


# ─── Small Office (single storey) ──────────────────────────────────────────
# One packaged rooftop unit and a small BACnet-integrated lighting relay
# panel — the common retrofit pattern for small single-tenant commercial
# space, where a 3rd-party BAS integrator adds a BACnet controller to a
# stock RTU rather than a full central-plant system.

def build_small_office() -> dict:
    devices = [
        device(2001, "RTU-1", "Single packaged rooftop unit serving the whole space",
               "Reliable Controls Corporation", "MACH-ProWeb", [
            obj("binary-input",  1, "SF-Run",              "no-units",        "manual",      {"value": True}),
            obj("binary-input",  2, "Comp-1-Run",           "no-units",        "manual",      {"value": True}),
            obj("binary-input",  3, "Comp-2-Run",           "no-units",        "manual",      {"value": False}),
            obj("analog-input",  4, "Space-Temp",           "degrees-celsius", "noise",       {"base": 22.0, "noise": 0.4}),
            obj("analog-value",  5, "Cooling-SP",           "degrees-celsius", "manual",      {"value": 23.0}),
            obj("analog-value",  6, "Heating-SP",           "degrees-celsius", "manual",      {"value": 20.0}),
            obj("analog-input",  7, "OAT",                  "degrees-celsius", "sine",        {"base": 12.0, "amplitude": 8.0, "period_hours": 24}),
            obj("analog-output", 8, "Economizer-Position",  "percent",         "sine",        {"base": 22.0, "amplitude": 18.0, "period_hours": 24}),
            obj("analog-input",  9, "Space-CO2",            "parts-per-million","random_walk", {"value": 550, "step": 25, "min": 400, "max": 1000}),
            obj("binary-input", 10, "Filter-Alarm",         "no-units",        "manual",      {"value": False}),
        ]),
        device(2002, "Lighting-Panel-1", "BACnet-integrated lighting relay panel — 4 circuits",
               "WAGO GmbH & Co. KG", "750-889 PFC200", [
            obj("binary-value", 1, "Circuit-1-On", "no-units", "schedule", {"default": 0, "blocks": [{"start": "07:00", "value": 1}, {"start": "18:00", "value": 0}]}),
            obj("binary-value", 2, "Circuit-2-On", "no-units", "schedule", {"default": 0, "blocks": [{"start": "07:00", "value": 1}, {"start": "18:00", "value": 0}]}),
            obj("binary-value", 3, "Circuit-3-On", "no-units", "schedule", {"default": 0, "blocks": [{"start": "07:00", "value": 1}, {"start": "18:00", "value": 0}]}),
            obj("binary-value", 4, "Circuit-4-On", "no-units", "schedule", {"default": 0, "blocks": [{"start": "08:00", "value": 1}, {"start": "17:00", "value": 0}]}),
            obj("analog-input", 5, "Lighting-Power", "kilowatts", "random_walk", {"value": 1.1, "step": 0.1, "min": 0.0, "max": 2.2}),
        ]),
    ]
    return {"devices": devices}


# ─── Large Corporate Campus (12-storey, dual chiller + standby) ───────────
# Bigger central plant (N+1 chillers), three AHUs split across low/mid/high
# rise zones, sampled VAVs per zone group, DALI lighting gateways covering
# multiple floors each, plus power metering and VFDs on major plant pumps —
# the kind of extra instrumentation that shows up once a building is large
# enough to justify sub-metering and drive-level monitoring.

def build_large_campus() -> dict:
    devices = [
        device(3001, "Chiller-Plant", "Central plant – 2 duty + 1 standby centrifugal chillers, cooling towers",
               "Johnson Controls", "YK Centrifugal Chiller", [
            obj("binary-input",  1, "CH-1-Run",              "no-units",        "manual",      {"value": True}),
            obj("analog-input",  2, "CH-1-kW",                "kilowatts",       "random_walk", {"value": 380, "step": 12, "min": 150, "max": 550}),
            obj("analog-input",  3, "CH-1-COP",               "no-units",        "noise",       {"base": 6.1, "noise": 0.2}),
            obj("binary-input",  4, "CH-2-Run",               "no-units",        "manual",      {"value": True}),
            obj("analog-input",  5, "CH-2-kW",                "kilowatts",       "random_walk", {"value": 365, "step": 12, "min": 150, "max": 550}),
            obj("analog-input",  6, "CH-2-COP",               "no-units",        "noise",       {"base": 5.9, "noise": 0.2}),
            obj("binary-input",  7, "CH-3-Standby",           "no-units",        "manual",      {"value": False}),
            obj("analog-input",  8, "CW-Supply-Temp",         "degrees-celsius", "noise",       {"base": 6.0, "noise": 0.2}),
            obj("analog-input",  9, "CW-Return-Temp",         "degrees-celsius", "noise",       {"base": 12.5, "noise": 0.2}),
            obj("binary-input", 10, "CT-Fan-1-Run",           "no-units",        "manual",      {"value": True}),
            obj("binary-input", 11, "CT-Fan-2-Run",           "no-units",        "manual",      {"value": True}),
            obj("binary-input", 12, "CT-Fan-3-Run",           "no-units",        "manual",      {"value": False}),
            obj("analog-input", 13, "CT-Leaving-Water-Temp",  "degrees-celsius", "noise",       {"base": 29.0, "noise": 0.5}),
        ]),
        device(3002, "HW-Plant", "Central hot-water plant – 3 condensing boilers",
               "Automated Logic Corporation", "PlantCTRL", [
            obj("binary-input", 1, "BLR-1-Run",         "no-units",        "manual", {"value": True}),
            obj("binary-input", 2, "BLR-2-Run",         "no-units",        "manual", {"value": True}),
            obj("binary-input", 3, "BLR-3-Run",         "no-units",        "manual", {"value": False}),
            obj("analog-input", 4, "HW-Supply-Temp",    "degrees-celsius", "noise",  {"base": 71.0, "noise": 0.8}),
            obj("analog-input", 5, "HW-Return-Temp",    "degrees-celsius", "noise",  {"base": 58.0, "noise": 0.8}),
            obj("analog-input", 6, "Gas-Flow",          "cubic-feet-per-minute", "random_walk", {"value": 60, "step": 4, "min": 15, "max": 120}),
        ]),
    ]

    ahu_cfg = [
        (3003, "AHU-Low",  "Serves floors 1-4 (low rise)",  75.0, 13.0),
        (3004, "AHU-Mid",  "Serves floors 5-8 (mid rise)",  70.0, 13.5),
        (3005, "AHU-High", "Serves floors 9-12 (high rise)",65.0, 14.0),
    ]
    for inst, name, desc, sf_base, sat_base in ahu_cfg:
        devices.append(device(inst, name, desc, "Siemens Building Technologies", "PXC-BACnet", [
            obj("binary-input",  1, "SF-Run",             "no-units",        "manual", {"value": True}),
            obj("binary-input",  2, "RF-Run",              "no-units",        "manual", {"value": True}),
            obj("analog-input",  3, "SF-Speed",            "percent",         "sine",   {"base": sf_base, "amplitude": 15.0, "period_hours": 12}),
            obj("analog-input",  4, "RF-Speed",            "percent",         "sine",   {"base": sf_base - 5, "amplitude": 12.0, "period_hours": 12}),
            obj("analog-input",  5, "SAT",                 "degrees-celsius","noise",  {"base": sat_base, "noise": 0.4}),
            obj("analog-input",  6, "RAT",                 "degrees-celsius","sine",   {"base": 22.0, "amplitude": 2.0, "period_hours": 24}),
            obj("analog-input",  7, "MAT",                 "degrees-celsius","noise",  {"base": 16.0, "noise": 0.8}),
            obj("analog-input",  8, "OAT",                 "degrees-celsius","sine",   {"base": 12.0, "amplitude": 8.0, "period_hours": 24}),
            obj("analog-output", 9, "OAD-Position",         "percent",        "sine",   {"base": 26.0, "amplitude": 16.0, "period_hours": 24}),
            obj("analog-output",10, "CC-Valve",             "percent",        "sine",   {"base": 52.0, "amplitude": 24.0, "period_hours": 12}),
            obj("analog-output",11, "HC-Valve",             "percent",        "sine",   {"base": 11.0, "amplitude": 9.0, "period_hours": 24}),
            obj("analog-input", 12, "SA-Flow",              "cubic-feet-per-minute","noise", {"base": 12500, "noise": 300}),
            obj("analog-input", 13, "SA-Static-Pressure",   "pascals",        "noise",  {"base": 400, "noise": 15}),
            obj("binary-input", 14, "Filter-DP-Alarm",      "no-units",       "manual", {"value": False}),
        ]))

    vav_cfg = [
        (3101, 21.5, 23.0, 20.0, 68,  360, 12.0),   # Low zone A
        (3102, 22.0, 23.0, 20.5, 70,  340, 10.0),   # Low zone B
        (3201, 21.8, 23.0, 20.0, 66,  380, 13.0),   # Mid zone A
        (3202, 22.2, 23.5, 20.5, 71,  355, 11.0),   # Mid zone B
        (3301, 21.0, 22.5, 20.0, 62,  330, 15.0),   # High zone A – exec floors
        (3302, 21.5, 23.0, 20.0, 65,  310, 14.0),   # High zone B
    ]
    for inst, zt, csp, hsp, dmp, flow, rh in vav_cfg:
        devices.append(device(inst, f"VAV-{inst}", f"Zone controller – instance {inst}",
                               "Distech Controls, Inc.", "ECY-VAV", [
            obj("analog-input",  1, "Zone-Temp",     "degrees-celsius", "noise",  {"base": zt, "noise": 0.3}),
            obj("analog-value",  2, "Cooling-SP",    "degrees-celsius", "manual", {"value": csp}),
            obj("analog-value",  3, "Heating-SP",    "degrees-celsius", "manual", {"value": hsp}),
            obj("analog-output", 4, "Damper-Cmd",    "percent",         "sine",   {"base": dmp, "amplitude": 14.0, "period_hours": 8}),
            obj("analog-input",  5, "Zone-Airflow",  "cubic-feet-per-minute", "noise", {"base": flow, "noise": 20}),
            obj("analog-output", 6, "Reheat-Valve",  "percent",         "sine",   {"base": rh, "amplitude": 10.0, "period_hours": 12}),
            obj("binary-input",  7, "Occupancy",     "no-units",        "manual", {"value": True}),
            obj("analog-input",  8, "Zone-CO2",      "parts-per-million","random_walk", {"value": 650, "step": 30, "min": 400, "max": 1200}),
        ]))

    dali_cfg = [
        (3401, "DALI-GW-Low",  "Floors 1-4 lighting",  "07:00", "19:00"),
        (3402, "DALI-GW-Mid",  "Floors 5-8 lighting",  "07:00", "19:00"),
        (3403, "DALI-GW-High", "Floors 9-12 lighting", "06:30", "20:00"),
    ]
    for inst, name, desc, on_time, off_time in dali_cfg:
        objs = [obj("binary-value", 1, "Emergency-Test-OK", "no-units", "fault", {
            "base_behavior": "constant", "base_params": {"value": True},
            "fault_type": "stuck", "fault_value": 0, "mtbf_minutes": 10080, "fault_duration_seconds": 600,
        })]
        next_inst = 2
        for zone in ("A", "B", "C"):
            objs += [
                obj("binary-value", next_inst,     f"Zone-{zone}-Lights-On",  "no-units", "schedule", {
                    "default": 0, "blocks": [{"start": on_time, "value": 1}, {"start": off_time, "value": 0}],
                }),
                obj("analog-value", next_inst + 1, f"Zone-{zone}-Dim-Level",  "percent",  "sine",     {
                    "base": 75.0, "amplitude": 15.0, "period_hours": 24,
                }),
                obj("analog-value", next_inst + 2, f"Zone-{zone}-Scene",      "no-units", "manual",   {"value": 2}),
                obj("analog-input", next_inst + 3, f"Zone-{zone}-Daylight",   "luxes",    "sine",     {
                    "base": 250.0, "amplitude": 240.0, "period_hours": 24,
                }),
                obj("binary-input", next_inst + 4, f"Zone-{zone}-Lamp-Fault", "no-units", "fault",    {
                    "base_behavior": "constant", "base_params": {"value": False},
                    "fault_type": "stuck", "fault_value": 1, "mtbf_minutes": 4320, "fault_duration_seconds": 1800,
                }),
                obj("analog-input", next_inst + 5, f"Zone-{zone}-Power",      "kilowatts","random_walk", {
                    "value": 2.4, "step": 0.15, "min": 0.2, "max": 4.5,
                }),
            ]
            next_inst += 6
        devices.append(device(inst, name, desc, "LOYTEC electronics GmbH", "L-DALI/8", objs))

    meter_cfg = [
        (3501, "Meter-Main",   "Main switchgear power meter"),
        (3502, "Meter-Floor-4","Floor 4 sub-metering panel"),
        (3503, "Meter-Floor-8","Floor 8 sub-metering panel"),
    ]
    for inst, name, desc in meter_cfg:
        devices.append(device(inst, name, desc, "Schneider Electric", "PowerLogic PM8000", [
            obj("analog-input", 1, "kW-Total",     "kilowatts",      "random_walk", {"value": 180, "step": 10, "min": 40, "max": 420}),
            obj("analog-input", 2, "kWh-Total",    "kilowatt-hours", "random_walk", {"value": 12000, "step": 40, "min": 0, "max": 500000}),
            obj("analog-input", 3, "Voltage-L1",   "volts",          "noise",       {"base": 347.0, "noise": 1.5}),
            obj("analog-input", 4, "Voltage-L2",   "volts",          "noise",       {"base": 347.0, "noise": 1.5}),
            obj("analog-input", 5, "Voltage-L3",   "volts",          "noise",       {"base": 347.0, "noise": 1.5}),
            obj("analog-input", 6, "Current-L1",   "amperes",        "noise",       {"base": 210.0, "noise": 8.0}),
            obj("analog-input", 7, "Current-L2",   "amperes",        "noise",       {"base": 205.0, "noise": 8.0}),
            obj("analog-input", 8, "Current-L3",   "amperes",        "noise",       {"base": 215.0, "noise": 8.0}),
            obj("analog-input", 9, "Power-Factor", "no-units",       "noise",       {"base": 0.94, "noise": 0.02}),
        ]))

    vfd_cfg = [
        (3601, "VFD-CWP-1", "Condenser water pump 1 drive"),
        (3602, "VFD-CHWP-1","Chilled water pump 1 drive"),
    ]
    for inst, name, desc in vfd_cfg:
        devices.append(device(inst, name, desc, "ABB, Inc.", "ACH580", [
            obj("analog-output", 1, "Speed-Pct",  "percent",   "sine",  {"base": 65.0, "amplitude": 20.0, "period_hours": 12}),
            obj("analog-input",  2, "Output-kW",  "kilowatts", "noise", {"base": 18.0, "noise": 2.0}),
            obj("binary-input",  3, "Run-Status", "no-units",  "manual",{"value": True}),
            obj("binary-input",  4, "Drive-Fault","no-units",  "fault", {
                "base_behavior": "constant", "base_params": {"value": False},
                "fault_type": "stuck", "fault_value": 1, "mtbf_minutes": 20160, "fault_duration_seconds": 900,
            }),
        ]))

    return {"devices": devices}


# ─── Distribution Warehouse / Industrial ──────────────────────────────────
# Several small RTUs instead of a central plant (typical for large open
# floor areas), dock door heaters, exhaust/makeup-air fan VFDs, and
# wireless (not DALI) high-bay lighting — Lutron Vive-style wireless mesh
# control is the more common real-world fit for big open warehouse spans
# where running a wired DALI bus the length of the building is impractical.

def build_warehouse() -> dict:
    devices = []
    rtu_cfg = [
        (4001, "RTU-1", "Rooftop unit 1 – zone A spot conditioning"),
        (4002, "RTU-2", "Rooftop unit 2 – zone B spot conditioning"),
        (4003, "RTU-3", "Rooftop unit 3 – zone C spot conditioning"),
    ]
    for inst, name, desc in rtu_cfg:
        devices.append(device(inst, name, desc, "Carrier Corporation", "RTU Open", [
            obj("binary-input",  1, "SF-Run",             "no-units",        "manual", {"value": True}),
            obj("binary-input",  2, "Comp-1-Run",          "no-units",        "manual", {"value": True}),
            obj("binary-input",  3, "Comp-2-Run",          "no-units",        "manual", {"value": False}),
            obj("analog-input",  4, "Space-Temp",          "degrees-celsius", "noise",  {"base": 18.0, "noise": 0.6}),
            obj("analog-value",  5, "Cooling-SP",          "degrees-celsius", "manual", {"value": 20.0}),
            obj("analog-value",  6, "Heating-SP",          "degrees-celsius", "manual", {"value": 15.0}),
            obj("analog-input",  7, "OAT",                 "degrees-celsius", "sine",   {"base": 12.0, "amplitude": 8.0, "period_hours": 24}),
            obj("analog-output", 8, "Economizer-Position", "percent",         "sine",   {"base": 30.0, "amplitude": 20.0, "period_hours": 24}),
        ]))

    dock_objs = []
    for bay in range(1, 5):
        dock_objs += [
            obj("binary-input", (bay - 1) * 2 + 1, f"Dock-{bay}-Door-Open",  "no-units", "manual",   {"value": False}),
            obj("binary-value", (bay - 1) * 2 + 2, f"Dock-{bay}-Heater-On", "no-units", "schedule", {
                "default": 0, "blocks": [{"start": "05:00", "value": 1}, {"start": "09:00", "value": 0}],
            }),
        ]
    devices.append(device(4004, "Dock-Systems", "Loading dock – door position sensors and heaters, 4 bays",
                           "Reliable Controls Corporation", "MACH-ProSys", dock_objs))

    devices.append(device(4005, "Exhaust-Fans", "Warehouse exhaust / makeup-air fan drives – 2 units",
                           "Danfoss Drives A/S", "VLT HVAC Drive FC 102", [
        obj("analog-output", 1, "Fan-1-Speed", "percent",   "sine",  {"base": 60.0, "amplitude": 25.0, "period_hours": 12}),
        obj("binary-input",  2, "Fan-1-Run",   "no-units",  "manual",{"value": True}),
        obj("analog-input",  3, "Fan-1-kW",    "kilowatts", "noise", {"base": 9.5, "noise": 1.0}),
        obj("analog-output", 4, "Fan-2-Speed", "percent",   "sine",  {"base": 55.0, "amplitude": 22.0, "period_hours": 12}),
        obj("binary-input",  5, "Fan-2-Run",   "no-units",  "manual",{"value": True}),
        obj("analog-input",  6, "Fan-2-kW",    "kilowatts", "noise", {"base": 8.8, "noise": 1.0}),
    ]))

    lighting_objs = [obj("analog-input", 1, "Total-Power", "kilowatts", "random_walk", {"value": 6.5, "step": 0.3, "min": 1.0, "max": 14.0})]
    next_inst = 2
    for zone in range(1, 5):
        lighting_objs += [
            obj("binary-input", next_inst,     f"Zone-{zone}-Occupancy", "no-units", "fault", {
                "base_behavior": "constant", "base_params": {"value": False},
                "fault_type": "stuck", "fault_value": 1, "mtbf_minutes": 15, "fault_duration_seconds": 480,
            }),
            obj("binary-value", next_inst + 1, f"Zone-{zone}-Lights-On", "no-units", "schedule", {
                "default": 1, "blocks": [{"start": "06:00", "value": 1}, {"start": "22:00", "value": 1}],
            }),
        ]
        next_inst += 2
    devices.append(device(4006, "HighBay-Lighting", "Wireless high-bay LED lighting — occupancy-based, 4 aisles",
                           "Lutron Electronic Co., Inc.", "Vive Wireless Gateway", lighting_objs))

    return {"devices": devices}


def build_medium_office_tower() -> dict:
    """Reuses the simulator's own default seed_default() logic (chiller/HW
    plant, 2 AHUs, 8 VAVs, 4 DALI lighting gateways) via a throwaway temp DB,
    so this profile always matches whatever a fresh install seeds — no
    duplicated device/object definitions to keep in sync."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db_path = Path(tmp) / "seed_default_snapshot.db"
        tmp_db = sim.Database(tmp_db_path)
        tmp_db.setup()
        tmp_db.seed_default()

        devices = tmp_db.get_devices()
        for dev in devices:
            dev["objects"] = tmp_db.get_objects(dev["id"])
        return {"devices": devices}


PROFILES = [
    ("Small Office (1 storey)",
     "Single packaged RTU + BACnet lighting relay panel — small single-tenant retrofit",
     build_small_office),
    ("Medium Office Tower (4 storey)",
     "Central chiller/boiler plant, 2 AHUs, 8 VAV zones, DALI lighting per floor",
     build_medium_office_tower),
    ("Large Corporate Campus (12 storey)",
     "N+1 chiller plant, 3 AHUs, sampled VAV zones, DALI lighting, power metering, VFDs",
     build_large_campus),
    ("Distribution Warehouse", "3 rooftop units, dock door heaters, exhaust fan VFDs, wireless high-bay lighting",
     build_warehouse),
]


def main() -> None:
    db = sim.Database(sim.DB_PATH)
    db.setup()

    existing_names = {p["name"] for p in db.get_profiles()}

    for name, description, builder in PROFILES:
        if name in existing_names:
            print(f"Skipping '{name}' — a profile with this name already exists")
            continue
        data = builder()
        result = db.import_profile(name, description, data)
        print(f"Imported '{result['name']}' — {result['device_count']} devices (profile id={result['id']})")


if __name__ == "__main__":
    main()
