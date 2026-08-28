"""SQLite persistence layer for the BACnet simulator.

Physically extracted from src/legacy.py's `Database` class (GH #15 refactor,
next pass after the API routers were extracted into src/api/routers/*.py) --
moved verbatim, no behavior changes, same standard the router extraction
already set. Schema creation now goes through src/db/migrations/ (a
baseline schema + numbered, idempotent migration functions tracked in a
schema_migrations table) instead of one giant inline executescript() +
ad-hoc PRAGMA table_info() checks -- see src/db/migrations/runner.py and
registry.py for that mechanism, and their own docstrings for why every
migration still keeps its own idempotency guard rather than trusting the
tracking table alone.
"""
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..semantics.backfill import (
    backfill_device_location_relationships,
    backfill_point_membership_relationships,
    backfill_semantic_entities,
    migrate_ahu_fan_aliases,
    upsert_semantic_entity,
    upsert_semantic_relationship,
)
from ..semantics.keys import derive_semantic_key
from ..semantics.mirror import sync_entity_from_flat_field, sync_flat_field_from_entity, sync_device_location_relationship, sync_controller_entity
from ..semantics.validation import validate_semantic_entity
from ..simulation.model_store import (
    ensure_simulation_model_schema,
    get_aggregate_membership_owner,
    insert_simulation_model,
    list_all_simulation_models,
    reconcile_provider_owned_raw_behavior,
)
from .migrations.runner import run_migrations


log = logging.getLogger("bacnet-sim")


# ─── Runtime settings ──────────────────────────────────────────────────────────
# Persisted key/value overrides for constants that used to be hardcoded. Values
# are stored as TEXT in the `settings` table; SETTINGS_SCHEMA casts them back.
SETTINGS_SCHEMA: dict[str, type] = {
    "tick_seconds": float,
    "device_log_maxlen": int,
    "global_log_maxlen": int,
    "metrics_errors_maxlen": int,
    "metrics_new_devices_maxlen": int,
    "metrics_duplicate_id_maxlen": int,
    "metrics_traffic_feed_maxlen": int,
    "object_history_maxlen": int,
    "trend_log_default_interval": int,
    "trend_log_default_buffer_size": int,
    "jwt_expire_hours": int,
    "fmu_runtime_url": str,
    "fmu_runtime_timeout_s": float,
}


def _default_settings() -> dict:
    return {
        "tick_seconds": 5.0,
        "device_log_maxlen": 300,
        "global_log_maxlen": 1000,
        "metrics_errors_maxlen": 200,
        "metrics_new_devices_maxlen": 200,
        "metrics_duplicate_id_maxlen": 100,
        "metrics_traffic_feed_maxlen": 500,
        "object_history_maxlen": 720,
        "trend_log_default_interval": 60,
        "trend_log_default_buffer_size": 1000,
        # Seeded from the env var so upgrading an existing deployment that
        # already sets JWT_EXPIRE_HOURS doesn't silently change on first read.
        "jwt_expire_hours": int(os.environ.get("JWT_EXPIRE_HOURS", "24")),
        "fmu_runtime_url": os.environ.get(
            "FMU_MODEL_RUNTIME_URL",
            "http://localhost:8002",
        ),
        "fmu_runtime_timeout_s": float(
            os.environ.get("FMU_MODEL_RUNTIME_TIMEOUT_S", "20")
        ),
    }


# ─── Database ─────────────────────────────────────────────────────────────────

class Database:
    def __init__(self, path: Path):
        self.path = str(path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def setup(self) -> None:
        self.path_obj = Path(self.path)
        self.path_obj.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            run_migrations(conn)

            backfill_semantic_entities(conn)
            # Must run AFTER backfill_semantic_entities() -- see
            # migrate_ahu_fan_aliases()'s own docstring for why (it needs
            # each AHU's own top-level equipment entity, which backfill is
            # what creates it from devices.equipment_type).
            migrate_ahu_fan_aliases(conn)
            # Also after backfill_semantic_entities() -- needs the point/
            # equipment entities that call just minted to link them.
            backfill_point_membership_relationships(conn)
            backfill_device_location_relationships(conn)
        # Own connection management (mirrors ensure_simulation_model_schema),
        # so run after the block above's `conn` is closed rather than inside
        # it -- opening a second connection while the first is still open
        # risks a locked-database error on this file-backed SQLite DB.
        reconcile_provider_owned_raw_behavior(self)
        log.info("Database ready at %s", self.path)

    def seed_default(self) -> None:
        """Populate with a 4-storey commercial office tower if DB is empty.

        Device layout
        -------------
        1000  BMS-Gateway        Honeywell WEBs-N4          (supervisory)
        1001  Chiller-Plant      Trane Tracer SC+            (basement – 2 chillers + CT)
        1002  HW-Plant           Honeywell Excel 500         (basement – 2 boilers)
        1003  AHU-1              Johnson Controls FEC26B     (floors 1-2)
        1004  AHU-2              Johnson Controls FEC26B     (floors 3-4)
        1005  Main-Meter         Honeywell WEBs-N4           (main electrical utility meter)
        1101  VAV-L1-01          Siemens RXB29.1             (floor 1 zone A – north)
        1102  VAV-L1-02          Siemens RXB29.1             (floor 1 zone B – south)
        1201  VAV-L2-01          Siemens RXB29.1             (floor 2 zone A – conference)
        1202  VAV-L2-02          Siemens RXB29.1             (floor 2 zone B – open plan)
        1301  VAV-L3-01          Siemens RXB29.1             (floor 3 zone A – exec suites)
        1302  VAV-L3-02          Siemens RXB29.1             (floor 3 zone B – server room)
        1401  VAV-L4-01          Siemens RXB29.1             (floor 4 zone A – open plan)
        1402  VAV-L4-02          Siemens RXB29.1             (floor 4 zone B – board room)
        1501  DALI-GW-L1         LOYTEC L-DALI/4             (floor 1 lighting – zones A/B)
        1502  DALI-GW-L2         LOYTEC L-DALI/4             (floor 2 lighting – zones A/B)
        1503  DALI-GW-L3         LOYTEC L-DALI/4             (floor 3 lighting – zones A/B)
        1504  DALI-GW-L4         LOYTEC L-DALI/4             (floor 4 lighting – zones A/B)

        DALI is its own low-level addressed bus for lamp ballasts/drivers
        (IEC 62386) — it isn't BACnet. What actually shows up on the BMS
        network is a DALI-to-BACnet gateway (LOYTEC L-DALI, WAGO, Helvar,
        Lutron Quantum, etc.) that bridges each DALI group/zone to a handful
        of BACnet points: dim level, on/off, scene, daylight sensor, and lamp/
        emergency-lighting fault status. Modeled here as one gateway per
        floor aggregating 2 zones, matching the VAV zone layout above.
        """
        HONEYWELL = "Honeywell International"
        JCI       = "Johnson Controls"
        SIEMENS   = "Siemens Building Technologies"
        TRANE     = "Trane Technologies"
        LOYTEC    = "LOYTEC electronics GmbH"

        with self._conn() as conn:
            if conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] > 0:
                return

            conn.executemany(
                "INSERT OR IGNORE INTO devices "
                "(device_instance, name, description, vendor_name, model_name, equipment_type) "
                "VALUES (?,?,?,?,?,?)",
                [
                    (1000, "BMS-Gateway",  "Building management system gateway – supervisory controller",  HONEYWELL, "WEBs-N4",   None),
                    (1001, "Chiller-Plant","Basement chiller plant – 2 × centrifugal chillers + cooling tower", TRANE, "Tracer SC+", "Chiller"),
                    (1002, "HW-Plant",     "Basement hot-water plant – 2 × condensing boilers",           HONEYWELL, "Excel 500", "Boiler"),
                    (1003, "AHU-1",        "Air handling unit 1 – serves floors 1 & 2",                   JCI,       "FEC26B",    "Air_Handling_Unit"),
                    (1004, "AHU-2",        "Air handling unit 2 – serves floors 3 & 4",                   JCI,       "FEC26B",    "Air_Handling_Unit"),
                    (1005, "Main-Meter",   "Main electrical utility meter",                                HONEYWELL, "WEBs-N4",   "Meter"),
                    (1101, "VAV-L1-01",    "Floor 1 VAV – Zone A north offices",                           SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1102, "VAV-L1-02",    "Floor 1 VAV – Zone B south offices",                           SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1201, "VAV-L2-01",    "Floor 2 VAV – Zone A conference rooms",                        SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1202, "VAV-L2-02",    "Floor 2 VAV – Zone B open-plan",                               SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1301, "VAV-L3-01",    "Floor 3 VAV – Zone A executive suites",                        SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1302, "VAV-L3-02",    "Floor 3 VAV – Zone B server room",                             SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1401, "VAV-L4-01",    "Floor 4 VAV – Zone A open-plan",                               SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1402, "VAV-L4-02",    "Floor 4 VAV – Zone B board room",                              SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1501, "DALI-GW-L1",   "Floor 1 DALI-to-BACnet lighting gateway – zones A/B",         LOYTEC,    "L-DALI/4",  None),
                    (1502, "DALI-GW-L2",   "Floor 2 DALI-to-BACnet lighting gateway – zones A/B",         LOYTEC,    "L-DALI/4",  None),
                    (1503, "DALI-GW-L3",   "Floor 3 DALI-to-BACnet lighting gateway – zones A/B",         LOYTEC,    "L-DALI/4",  None),
                    (1504, "DALI-GW-L4",   "Floor 4 DALI-to-BACnet lighting gateway – zones A/B",         LOYTEC,    "L-DALI/4",  None),
                ],
            )

            def did(instance: int) -> int:
                return conn.execute(
                    "SELECT id FROM devices WHERE device_instance=?", (instance,)
                ).fetchone()[0]

            objects: list = []

            # ── BMS Gateway (1000) ─────────────────────────────────────────────
            bms = did(1000)
            objects += [
                (bms, "binary-value",  1, "Building-Occupied",   "no-units",        "manual",      '{"value":true}'),
                (bms, "analog-value",  2, "Active-Alarms",       "no-units",        "random_walk", '{"value":2,"step":1,"min":0,"max":8}'),
                (bms, "analog-input",  3, "Energy-Today-kWh",    "kilowatt-hours",  "random_walk", '{"value":430,"step":12,"min":0,"max":2000}', "Energy_Sensor"),
                (bms, "analog-input",  4, "Peak-Demand-kW",      "kilowatts",       "random_walk", '{"value":182,"step":4,"min":50,"max":320}', "Demand_Sensor"),
                (bms, "analog-input",  5, "Outside-Air-Temp",    "degrees-celsius", "sine",        '{"base":12.0,"amplitude":8.0,"period_hours":24}', "Outside_Air_Temperature_Sensor"),
                (bms, "analog-input",  6, "Outside-Air-Humidity","percent",         "sine",        '{"base":55.0,"amplitude":15.0,"period_hours":24}', "Outside_Air_Humidity_Sensor"),
            ]

            # ── Chiller Plant (1001) ───────────────────────────────────────────
            cp = did(1001)
            objects += [
                (cp, "binary-input",  1, "CH-1-Run",             "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (cp, "analog-input",  2, "CH-1-kW",              "kilowatts",       "random_walk", '{"value":212,"step":8,"min":80,"max":320}', "Power_Sensor"),
                (cp, "analog-input",  3, "CH-1-COP",             "no-units",        "noise",       '{"base":5.8,"noise":0.2}'),
                (cp, "binary-input",  4, "CH-2-Run",             "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (cp, "analog-input",  5, "CH-2-kW",              "kilowatts",       "random_walk", '{"value":198,"step":8,"min":80,"max":320}', "Power_Sensor"),
                (cp, "analog-input",  6, "CH-2-COP",             "no-units",        "noise",       '{"base":5.6,"noise":0.2}'),
                (cp, "analog-input",  7, "CW-Supply-Temp",       "degrees-celsius", "noise",       '{"base":6.5,"noise":0.2}', "Leaving_Chilled_Water_Temperature_Sensor"),
                (cp, "analog-input",  8, "CW-Return-Temp",       "degrees-celsius", "noise",       '{"base":12.2,"noise":0.2}', "Entering_Chilled_Water_Temperature_Sensor"),
                (cp, "analog-input",  9, "CW-Flow",              "liters-per-second","noise",      '{"base":48.0,"noise":1.5}', "Water_Flow_Sensor"),
                (cp, "analog-input", 10, "CW-Diff-Pressure",     "pascals",         "noise",       '{"base":225,"noise":8}', "Water_Differential_Pressure_Sensor"),
                (cp, "binary-input", 11, "CT-Fan-1-Run",         "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (cp, "binary-input", 12, "CT-Fan-2-Run",         "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (cp, "analog-input", 13, "CT-Leaving-Water-Temp","degrees-celsius", "noise",       '{"base":29.5,"noise":0.5}'),
                (cp, "analog-input", 14, "CT-Approach-Temp",     "degrees-celsius", "noise",       '{"base":3.2,"noise":0.3}'),
                (cp, "binary-input", 15, "CW-Pump-1-Run",        "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (cp, "binary-input", 16, "CW-Pump-2-Run",        "no-units",        "manual",      '{"value":false}', "Run_Status"),
            ]

            # ── Hot Water Plant (1002) ─────────────────────────────────────────
            hw = did(1002)
            objects += [
                (hw, "binary-input",  1, "BLR-1-Run",            "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (hw, "analog-input",  2, "BLR-1-Firing-Rate",    "percent",         "noise",       '{"base":62,"noise":5}'),
                (hw, "analog-input",  3, "BLR-1-Flue-Temp",      "degrees-celsius", "noise",       '{"base":88,"noise":3}'),
                (hw, "binary-input",  4, "BLR-2-Run",            "no-units",        "manual",      '{"value":false}', "Run_Status"),
                (hw, "analog-input",  5, "BLR-2-Firing-Rate",    "percent",         "manual",      '{"value":0}'),
                (hw, "analog-input",  6, "HW-Supply-Temp",       "degrees-celsius", "noise",       '{"base":71.0,"noise":0.8}', "Leaving_Hot_Water_Temperature_Sensor"),
                (hw, "analog-input",  7, "HW-Return-Temp",       "degrees-celsius", "noise",       '{"base":58.5,"noise":0.8}', "Entering_Hot_Water_Temperature_Sensor"),
                (hw, "analog-input",  8, "HW-Diff-Pressure",     "pascals",         "noise",       '{"base":180,"noise":6}', "Water_Differential_Pressure_Sensor"),
                (hw, "analog-input",  9, "Gas-Flow",             "cubic-feet-per-minute","random_walk",'{"value":44,"step":3,"min":10,"max":85}'),
                (hw, "binary-input", 10, "HW-Pump-1-Run",        "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (hw, "binary-input", 11, "HW-Pump-2-Run",        "no-units",        "manual",      '{"value":false}', "Run_Status"),
            ]

            # ── Main Meter (1005) ──────────────────────────────────────────────
            meter = did(1005)
            objects += [
                (meter, "analog-input", 1, "Total-Energy-kWh", "kilowatt-hours", "random_walk", '{"value":48200,"step":25,"min":0,"max":500000}', "Energy_Sensor"),
                (meter, "analog-input", 2, "Total-Power-kW",   "kilowatts",      "random_walk", '{"value":210,"step":6,"min":50,"max":450}', "Power_Sensor"),
                (meter, "analog-input", 3, "Peak-Demand-kW",   "kilowatts",      "random_walk", '{"value":182,"step":4,"min":50,"max":320}', "Demand_Sensor"),
            ]

            # ── AHU-1  floors 1-2 (1003) ──────────────────────────────────────
            # SF-Run/RF-Run and SF-Speed/RF-Speed are tagged with the
            # canonical Fan_Status/Fan_Speed_Command point classes -- the
            # supply/return distinction comes from which Supply_Fan/
            # Return_Fan semantic equipment entity each isPointOf, set up
            # below, not from separate alias point-type names.
            ahu1 = did(1003)
            objects += [
                (ahu1, "binary-input",  1, "SF-Run",             "no-units",        "manual",      '{"value":true}', "Fan_Status"),
                (ahu1, "binary-input",  2, "RF-Run",             "no-units",        "manual",      '{"value":true}', "Fan_Status"),
                (ahu1, "analog-input",  3, "SF-Speed",           "percent",         "sine",        '{"base":75.0,"amplitude":15.0,"period_hours":12}', "Fan_Speed_Command"),
                (ahu1, "analog-input",  4, "RF-Speed",           "percent",         "sine",        '{"base":70.0,"amplitude":12.0,"period_hours":12}', "Fan_Speed_Command"),
                (ahu1, "analog-input",  5, "SAT",                "degrees-celsius", "noise",       '{"base":13.0,"noise":0.4}', "Supply_Air_Temperature_Sensor"),
                (ahu1, "analog-input",  6, "RAT",                "degrees-celsius", "sine",        '{"base":22.0,"amplitude":2.0,"period_hours":24}', "Return_Air_Temperature_Sensor"),
                (ahu1, "analog-input",  7, "MAT",                "degrees-celsius", "noise",       '{"base":16.0,"noise":0.8}', "Mixed_Air_Temperature_Sensor"),
                (ahu1, "analog-input",  8, "OAT",                "degrees-celsius", "sine",        '{"base":12.0,"amplitude":8.0,"period_hours":24}', "Outside_Air_Temperature_Sensor"),
                (ahu1, "analog-output", 9, "OAD-Position",       "percent",         "sine",        '{"base":28.0,"amplitude":18.0,"period_hours":24}', "Damper_Position_Command"),
                (ahu1, "analog-output",10, "CC-Valve",           "percent",         "sine",        '{"base":55.0,"amplitude":25.0,"period_hours":12}', "Valve_Position_Command"),
                (ahu1, "analog-output",11, "HC-Valve",           "percent",         "sine",        '{"base":10.0,"amplitude":9.0,"period_hours":24}', "Valve_Position_Command"),
                (ahu1, "analog-input", 12, "SA-Flow",            "cubic-feet-per-minute","noise",  '{"base":8500,"noise":250}', "Air_Flow_Sensor"),
                (ahu1, "analog-input", 13, "SA-Static-Pressure", "pascals",         "noise",       '{"base":375,"noise":12}', "Static_Pressure_Sensor"),
                (ahu1, "binary-input", 14, "Filter-DP-Alarm",    "no-units",        "manual",      '{"value":false}', "Change_Filter_Alarm"),
                (ahu1, "binary-input", 15, "Freeze-Stat",        "no-units",        "manual",      '{"value":false}', "Freeze_Status"),
            ]

            # ── AHU-2  floors 3-4 (1004) ──────────────────────────────────────
            ahu2 = did(1004)
            objects += [
                (ahu2, "binary-input",  1, "SF-Run",             "no-units",        "manual",      '{"value":true}', "Fan_Status"),
                (ahu2, "binary-input",  2, "RF-Run",             "no-units",        "manual",      '{"value":true}', "Fan_Status"),
                (ahu2, "analog-input",  3, "SF-Speed",           "percent",         "sine",        '{"base":70.0,"amplitude":18.0,"period_hours":12}', "Fan_Speed_Command"),
                (ahu2, "analog-input",  4, "RF-Speed",           "percent",         "sine",        '{"base":65.0,"amplitude":14.0,"period_hours":12}', "Fan_Speed_Command"),
                (ahu2, "analog-input",  5, "SAT",                "degrees-celsius", "noise",       '{"base":13.5,"noise":0.4}', "Supply_Air_Temperature_Sensor"),
                (ahu2, "analog-input",  6, "RAT",                "degrees-celsius", "sine",        '{"base":21.5,"amplitude":2.0,"period_hours":24}', "Return_Air_Temperature_Sensor"),
                (ahu2, "analog-input",  7, "MAT",                "degrees-celsius", "noise",       '{"base":15.5,"noise":0.8}', "Mixed_Air_Temperature_Sensor"),
                (ahu2, "analog-input",  8, "OAT",                "degrees-celsius", "sine",        '{"base":12.0,"amplitude":8.0,"period_hours":24}', "Outside_Air_Temperature_Sensor"),
                (ahu2, "analog-output", 9, "OAD-Position",       "percent",         "sine",        '{"base":25.0,"amplitude":16.0,"period_hours":24}', "Damper_Position_Command"),
                (ahu2, "analog-output",10, "CC-Valve",           "percent",         "sine",        '{"base":50.0,"amplitude":22.0,"period_hours":12}', "Valve_Position_Command"),
                (ahu2, "analog-output",11, "HC-Valve",           "percent",         "sine",        '{"base":12.0,"amplitude":9.0,"period_hours":24}', "Valve_Position_Command"),
                (ahu2, "analog-input", 12, "SA-Flow",            "cubic-feet-per-minute","noise",  '{"base":7800,"noise":220}', "Air_Flow_Sensor"),
                (ahu2, "analog-input", 13, "SA-Static-Pressure", "pascals",         "noise",       '{"base":360,"noise":12}', "Static_Pressure_Sensor"),
                (ahu2, "binary-input", 14, "Filter-DP-Alarm",    "no-units",        "manual",      '{"value":false}', "Change_Filter_Alarm"),
                (ahu2, "binary-input", 15, "Freeze-Stat",        "no-units",        "manual",      '{"value":false}', "Freeze_Status"),
            ]

            # ── VAV boxes ─────────────────────────────────────────────────────
            # (instance, zone_temp_base, cool_sp, heat_sp, damper_base, flow_base, reheat_base)
            vav_cfg = [
                (1101, 21.5, 23.0, 20.0, 68,  350, 12.0),   # L1 Zone A – north offices
                (1102, 22.0, 23.0, 20.5, 72,  320, 10.0),   # L1 Zone B – south offices
                (1201, 21.8, 23.0, 20.0, 65,  400, 14.0),   # L2 Zone A – conference rooms
                (1202, 22.2, 23.5, 20.5, 70,  370, 11.0),   # L2 Zone B – open plan
                (1301, 21.0, 22.5, 20.0, 60,  310, 16.0),   # L3 Zone A – exec suites
                (1302, 19.0, 20.5, 18.0, 90,  520, 5.0),    # L3 Zone B – server room (runs cold, high airflow)
                (1401, 22.0, 23.0, 20.5, 67,  360, 12.0),   # L4 Zone A – open plan
                (1402, 21.5, 23.0, 20.0, 63,  300, 18.0),   # L4 Zone B – board room
            ]
            for (inst, zt, csp, hsp, dmp, flow, rh) in vav_cfg:
                vd = did(inst)
                objects += [
                    (vd, "analog-input",  1, "Zone-Temp",         "degrees-celsius", "noise",  f'{{"base":{zt},"noise":0.3}}', "Zone_Air_Temperature_Sensor"),
                    (vd, "analog-value",  2, "Cooling-SP",        "degrees-celsius", "manual", f'{{"value":{csp}}}', "Cooling_Temperature_Setpoint"),
                    (vd, "analog-value",  3, "Heating-SP",        "degrees-celsius", "manual", f'{{"value":{hsp}}}', "Heating_Temperature_Setpoint"),
                    (vd, "analog-output", 4, "Damper-Cmd",        "percent",         "sine",   f'{{"base":{dmp},"amplitude":14.0,"period_hours":8}}', "Damper_Position_Command"),
                    (vd, "analog-input",  5, "Zone-Airflow",      "cubic-feet-per-minute","noise",f'{{"base":{flow},"noise":18}}', "Air_Flow_Sensor"),
                    (vd, "analog-output", 6, "Reheat-Valve",      "percent",         "sine",   f'{{"base":{rh},"amplitude":10.0,"period_hours":12}}', "Valve_Position_Command"),
                    (vd, "binary-input",  7, "Occupancy",         "no-units",        "manual", '{"value":true}', "Occupancy_Sensor"),
                    (vd, "analog-input",  8, "Zone-CO2",          "parts-per-million","random_walk",f'{{"value":650,"step":30,"min":400,"max":1200}}', "CO2_Level_Sensor"),
                ]

            # ── DALI-to-BACnet lighting gateways ─────────────────────────────
            # (instance, on_time, off_time) — occupied-hours schedule per floor.
            # Each gateway aggregates 2 zones (A/B), matching the VAV layout.
            dali_cfg = [
                (1501, "07:00", "19:00"),   # Floor 1
                (1502, "07:00", "19:00"),   # Floor 2
                (1503, "06:30", "20:00"),   # Floor 3 – executive suites, longer hours
                (1504, "07:00", "19:30"),   # Floor 4
            ]
            for (inst, on_time, off_time) in dali_cfg:
                gw = did(inst)
                objects += [
                    # Device-wide DALI Type-1 emergency lighting self-test status
                    (gw, "binary-value", 1, "Emergency-Test-OK", "no-units", "fault", json.dumps({
                        "base_behavior": "constant", "base_params": {"value": True},
                        "fault_type": "stuck", "fault_value": 0,
                        "mtbf_minutes": 10080, "fault_duration_seconds": 600,
                    })),
                ]
                next_inst = 2
                for zone in ("A", "B"):
                    objects += [
                        (gw, "binary-value", next_inst,     f"Zone-{zone}-Lights-On",  "no-units", "schedule", json.dumps({
                            "default": 0,
                            "blocks": [{"start": on_time, "value": 1}, {"start": off_time, "value": 0}],
                        }), "On_Off_Command"),
                        (gw, "analog-value", next_inst + 1, f"Zone-{zone}-Dim-Level",  "percent",  "sine",     json.dumps({
                            "base": 75.0, "amplitude": 15.0, "period_hours": 24,
                        }), "Lighting_Level_Command"),
                        (gw, "analog-value", next_inst + 2, f"Zone-{zone}-Scene",      "no-units", "manual",   json.dumps({
                            "value": 2,   # 1=Off 2=Occupied 3=Evening 4=Cleaning 5=Emergency
                        })),
                        (gw, "analog-input", next_inst + 3, f"Zone-{zone}-Daylight",   "luxes",    "sine",     json.dumps({
                            "base": 250.0, "amplitude": 240.0, "period_hours": 24,
                        })),
                        (gw, "binary-input", next_inst + 4, f"Zone-{zone}-Lamp-Fault", "no-units", "fault",    json.dumps({
                            "base_behavior": "constant", "base_params": {"value": False},
                            "fault_type": "stuck", "fault_value": 1,
                            "mtbf_minutes": 4320, "fault_duration_seconds": 1800,
                        })),
                        (gw, "analog-input", next_inst + 5, f"Zone-{zone}-Power",      "kilowatts","random_walk", json.dumps({
                            "value": 2.4, "step": 0.15, "min": 0.2, "max": 4.5,
                        }), "Power_Sensor"),
                    ]
                    next_inst += 6

            # Most tuples above are the original 7-element shape (no point_type
            # tag); only the curated subset tagged with an 8th element carries
            # one. Normalize before the bulk insert so both shapes work.
            objects = [(t + (None,)) if len(t) == 7 else t for t in objects]
            conn.executemany(
                "INSERT OR IGNORE INTO objects "
                "(device_id, object_type, object_instance, name, units, behavior, behavior_params, point_type) "
                "VALUES (?,?,?,?,?,?,?,?)",
                objects,
            )

            # Seed the default energy model for the Chiller-Plant device.
            #
            # Use the database ID resolved from device instance 1001 rather
            # than a hard-coded row ID, because SQLite row IDs can change
            # when the database is recreated or devices are imported.
            conn.execute(
                """
                INSERT INTO energy_model_configs (
                    device_id,
                    model_type,
                    enabled,
                    parameters
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(device_id, model_type, instance_key)
                DO UPDATE SET
                    enabled = excluded.enabled,
                    parameters = excluded.parameters
                """,
                (
                    cp,
                    "chiller",
                    1,
                    json.dumps(
                        {
                            "rated_capacity_kw": 3000.0,
                            "full_load_cop": 5.8,
                            "iplv_cop": 6.6,
                            "rated_electrical_power_kw": 520.0,
                        }
                    ),
                ),
            )

            # ── Brick Core semantic entities/relationships ──────────────────────
            # Demonstrates the canonical representation this migration exists
            # to enable: Supply_Fan/Return_Fan as their own sub-equipment
            # (isPartOf the AHU) each isPointOf-ing its own Fan_Status/
            # Fan_Speed_Command points, and Lighting_Zone as a location entity
            # directly hosting its own zone points -- instead of the
            # duplicate-Fan_Status collision the flat point_type tags above
            # still show (unavoidably, since two fans share one device) for
            # any caller that hasn't opted into semantic resolution.
            def oid(device_id: int, name: str) -> int:
                return conn.execute(
                    "SELECT id FROM objects WHERE device_id=? AND name=?",
                    (device_id, name),
                ).fetchone()[0]

            for ahu_device_id, ahu_name in ((ahu1, "AHU-1"), (ahu2, "AHU-2")):
                ahu_entity = upsert_semantic_entity(
                    conn, name=ahu_name, brick_class="Air_Handling_Unit",
                    entity_kind="equipment", device_id=ahu_device_id,
                )
                for brick_class, run_name, speed_name in (
                    ("Supply_Fan", "SF-Run", "SF-Speed"),
                    ("Return_Fan", "RF-Run", "RF-Speed"),
                ):
                    fan_entity = upsert_semantic_entity(
                        conn,
                        name=f"{ahu_name} {brick_class.replace('_', ' ')}",
                        brick_class=brick_class,
                        entity_kind="equipment",
                        device_id=ahu_device_id,
                        local_slug=brick_class.lower().replace("_", "-"),
                    )
                    upsert_semantic_relationship(conn, fan_entity["id"], "isPartOf", ahu_entity["id"])

                    for point_name, point_class in (
                        (run_name, "Fan_Status"),
                        (speed_name, "Fan_Speed_Command"),
                    ):
                        point_entity = upsert_semantic_entity(
                            conn, name=point_name, brick_class=point_class,
                            entity_kind="point", object_id=oid(ahu_device_id, point_name),
                        )
                        upsert_semantic_relationship(conn, point_entity["id"], "isPointOf", fan_entity["id"])

            for (dali_instance, _on_time, _off_time) in dali_cfg:
                gw = did(dali_instance)
                for zone in ("A", "B"):
                    zone_entity = upsert_semantic_entity(
                        conn,
                        name=f"Lighting Zone {zone} (device {dali_instance})",
                        brick_class="Lighting_Zone",
                        entity_kind="location",
                        device_id=gw,
                        local_slug=f"zone-{zone.lower()}",
                    )
                    for point_name, point_class in (
                        (f"Zone-{zone}-Power", "Power_Sensor"),
                        (f"Zone-{zone}-Lights-On", "On_Off_Command"),
                        (f"Zone-{zone}-Dim-Level", "Lighting_Level_Command"),
                    ):
                        point_entity = upsert_semantic_entity(
                            conn, name=point_name, brick_class=point_class,
                            entity_kind="point", object_id=oid(gw, point_name),
                        )
                        upsert_semantic_relationship(conn, point_entity["id"], "isPointOf", zone_entity["id"])

            # setup() already ran backfill_semantic_entities() once before
            # seed_default() populated any rows (fresh-install ordering:
            # setup() -> seed_default()), so that first pass found nothing
            # to backfill. Run it again now that the seeded equipment_type/
            # point_type/kind tags actually exist, so first boot doesn't
            # need a process restart before semantic entities show up.
            # (Also picks up every device/object/location NOT given explicit
            # semantic entities above, e.g. Chiller-Plant/HW-Plant/VAVs.)
            backfill_semantic_entities(conn)
            backfill_point_membership_relationships(conn)
            backfill_device_location_relationships(conn)

            conn.commit()
        log.info("Seeded 4-storey office tower: Honeywell/Trane/JCI/Siemens/LOYTEC – 18 devices, %d objects", len(objects))

        
    # ── Locations ────────────────────────────────────────────────────────────
    # Pure organizational grouping for the admin UI/sidebar tree — no BACnet
    # protocol representation, unlike devices/objects. Devices reference a
    # location via the nullable devices.location_id FK; NULL = top level.

    def get_locations(self) -> list[dict]:
        with self._conn() as conn:
            # Locations with an explicit sort_order (auto-generated Building/
            # Level hierarchies) sort by that value first; everything else
            # (sort_order IS NULL) falls back to alphabetical-by-name, exactly
            # as before this column existed.
            return [dict(r) for r in conn.execute(
                "SELECT * FROM locations ORDER BY (sort_order IS NULL), sort_order, name"
            )]

    def get_location(self, location_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
            return dict(r) if r else None

    def create_location(self, name: str, parent_location_id: Optional[int], description: str, kind: Optional[str] = None) -> dict:
        # `kind` is presented in the UI as the location's Brick class --
        # Brick is the source of truth, `locations.kind` is a compatibility
        # mirror kept in lockstep automatically (see src/semantics/mirror.py).
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO locations (name, parent_location_id, description, kind) VALUES (?,?,?,?)",
                (name, parent_location_id, description, kind),
            )
            sync_entity_from_flat_field(
                conn, entity_kind="location", name=name, brick_class=kind, location_id=cur.lastrowid,
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM locations WHERE id=?", (cur.lastrowid,)).fetchone())

    def generate_building_levels(
        self, building_name: str, above_ground_levels: int, below_ground_levels: int,
    ) -> None:
        """Called once, only from project creation (POST /profiles), when
        the caller asked for an auto-generated Building + Level hierarchy.
        Never called for update_project/"Save As" -- existing projects are
        never retroactively given levels.

        Everything below runs inside one connection/one commit rather than
        looping create_location() (which opens and commits its own
        connection per call) -- this must be all-or-nothing, not N
        independent transactions. sync_entity_from_flat_field() and
        everything it calls only ever operate on the connection passed to
        them (see src/semantics/mirror.py) -- no nested connection/commit --
        so this is genuinely one atomic transaction, not just shaped like one.
        """
        if above_ground_levels <= 0 and below_ground_levels <= 0:
            return
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO locations (name, parent_location_id, description, kind) VALUES (?,?,?,?)",
                (building_name, None, "", "Building"),
            )
            building_id = cur.lastrowid
            sync_entity_from_flat_field(
                conn, entity_kind="location", name=building_name, brick_class="Building", location_id=building_id,
            )

            # Deepest basement first, then up to L1 -- see sort_order's own
            # comment for why this can't just be alphabetical-by-name.
            for i in range(below_ground_levels, 0, -1):
                level_name = f"B{i}"
                cur = conn.execute(
                    "INSERT INTO locations (name, parent_location_id, description, kind, sort_order) VALUES (?,?,?,?,?)",
                    (level_name, building_id, "", "Floor", -i),
                )
                sync_entity_from_flat_field(
                    conn, entity_kind="location", name=level_name, brick_class="Floor", location_id=cur.lastrowid,
                )

            for i in range(1, above_ground_levels + 1):
                level_name = f"L{i}"
                cur = conn.execute(
                    "INSERT INTO locations (name, parent_location_id, description, kind, sort_order) VALUES (?,?,?,?,?)",
                    (level_name, building_id, "", "Floor", i),
                )
                sync_entity_from_flat_field(
                    conn, entity_kind="location", name=level_name, brick_class="Floor", location_id=cur.lastrowid,
                )

            conn.commit()

    def update_location(self, location_id: int, name: str, parent_location_id: Optional[int], description: str, kind: Optional[str] = None) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE locations SET name=?, parent_location_id=?, description=?, kind=? WHERE id=?",
                (name, parent_location_id, description, kind, location_id),
            )
            sync_entity_from_flat_field(
                conn, entity_kind="location", name=name, brick_class=kind, location_id=location_id,
            )
            conn.commit()
            r = conn.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
            return dict(r) if r else None

    def delete_location(self, location_id: int) -> bool:
        """Refuses to delete a non-empty location (sub-locations, devices,
        or equipment still reference it) -- returns False rather than
        silently cascading real project contents."""
        with self._conn() as conn:
            has_sublocations = conn.execute(
                "SELECT 1 FROM locations WHERE parent_location_id=?", (location_id,)
            ).fetchone()
            has_devices = conn.execute(
                "SELECT 1 FROM devices WHERE location_id=?", (location_id,)
            ).fetchone()
            has_equipment = conn.execute(
                "SELECT 1 FROM equipment WHERE location_id=?", (location_id,)
            ).fetchone()
            if has_sublocations or has_devices or has_equipment:
                return False
            # A location's own Brick/semantic mirror is metadata, not a
            # child occupant. Remove it and its relationships so generated
            # empty Building/Level rows can be deleted from the UI.
            conn.execute(
                "DELETE FROM semantic_relationships "
                "WHERE source_entity_id IN (SELECT id FROM semantic_entities WHERE location_id=?) "
                "OR target_entity_id IN (SELECT id FROM semantic_entities WHERE location_id=?)",
                (location_id, location_id),
            )
            conn.execute("DELETE FROM semantic_entities WHERE location_id=?", (location_id,))
            cur = conn.execute("DELETE FROM locations WHERE id=?", (location_id,))
            conn.commit()
            return cur.rowcount > 0

    def _subtree_location_ids(self, conn: sqlite3.Connection, location_id: int) -> list[int]:
        """location_id and every location beneath it, in BFS (parent-
        before-child) order -- a child is only ever appended to the list
        in the round after its parent, so a caller that needs children
        deleted before their parents (avoiding locations.parent_location_id's
        FK) can just iterate reversed(result)."""
        ids = [location_id]
        frontier = [location_id]
        while frontier:
            placeholders = ",".join("?" * len(frontier))
            rows = conn.execute(
                f"SELECT id FROM locations WHERE parent_location_id IN ({placeholders})",
                frontier,
            ).fetchall()
            frontier = [r["id"] for r in rows]
            ids.extend(frontier)
        return ids

    def get_location_deletion_impact(self, location_id: int) -> dict:
        """Read-only preview of what delete_location_cascade(location_id)
        would do -- used by the admin UI to show a friendly confirmation
        (counts + what else references the points inside) instead of just
        letting the cascade fail or silently drop simulation-model/graph
        references. Never mutates anything."""
        with self._conn() as conn:
            location_ids = self._subtree_location_ids(conn, location_id)
            sub_location_count = len(location_ids) - 1

            loc_placeholders = ",".join("?" * len(location_ids))
            device_rows = conn.execute(
                f"SELECT id, name FROM devices WHERE location_id IN ({loc_placeholders})",
                location_ids,
            ).fetchall()
            device_ids = [r["id"] for r in device_rows]
            device_name_by_id = {r["id"]: r["name"] for r in device_rows}

            equipment_count = conn.execute(
                f"SELECT COUNT(*) FROM equipment WHERE location_id IN ({loc_placeholders})",
                location_ids,
            ).fetchone()[0]

            point_rows = []
            if device_ids:
                dev_placeholders = ",".join("?" * len(device_ids))
                point_rows = conn.execute(
                    f"SELECT id, name, device_id FROM objects WHERE device_id IN ({dev_placeholders})",
                    device_ids,
                ).fetchall()

            # N+1 is deliberate here: reuses get_aggregate_membership_owner
            # as-is (same function objects.py::delete_object already
            # trusts for the single-point case) rather than re-deriving its
            # JOIN logic -- point counts under one location are small, and
            # this is a read-only preview, not a hot path.
            blocking_points: list[dict] = []
            for point in point_rows:
                owner = get_aggregate_membership_owner(self, point["id"])
                if owner is not None:
                    blocking_points.append({
                        "point_name": point["name"],
                        "device_name": device_name_by_id.get(point["device_id"], ""),
                        "model_name": owner["model_name"],
                        "variable": owner["variable"],
                    })

            affected_simulation_models: list[dict] = []
            affected_custom_graphs: list[dict] = []
            if point_rows:
                point_ids = [p["id"] for p in point_rows]
                pt_placeholders = ",".join("?" * len(point_ids))
                model_rows = conn.execute(
                    f"""
                    SELECT DISTINCT c.id, c.name
                    FROM simulation_model_configs c
                    WHERE c.id IN (
                        SELECT model_config_id FROM simulation_model_mappings
                        WHERE point_id IN ({pt_placeholders})
                        UNION
                        SELECT model_config_id FROM simulation_model_input_exposures
                        WHERE point_id IN ({pt_placeholders})
                    )
                    ORDER BY c.name
                    """,
                    point_ids + point_ids,
                ).fetchall()
                affected_simulation_models = [dict(r) for r in model_rows]

                # custom_graphs.definition_json's series entries are a JSON
                # blob, not an FK (see that table's own comment) -- has to
                # be scanned in Python, there's no cascade to rely on.
                point_id_set = set(point_ids)
                device_id_set = set(device_ids)
                for g in conn.execute("SELECT id, name, definition_json FROM custom_graphs").fetchall():
                    try:
                        definition = json.loads(g["definition_json"])
                    except (TypeError, ValueError):
                        continue
                    series = definition.get("series") if isinstance(definition, dict) else None
                    if not series:
                        continue
                    hit = any(
                        isinstance(s, dict)
                        and s.get("device_id") in device_id_set
                        and s.get("object_id") in point_id_set
                        for s in series
                    )
                    if hit:
                        affected_custom_graphs.append({"id": g["id"], "name": g["name"]})

            return {
                "sub_location_count": sub_location_count,
                "device_count": len(device_ids),
                "equipment_count": equipment_count,
                "point_count": len(point_rows),
                "blocked": bool(blocking_points),
                "blocking_points": blocking_points,
                "affected_simulation_models": affected_simulation_models,
                "affected_custom_graphs": affected_custom_graphs,
            }

    def delete_location_cascade(self, location_id: int) -> bool:
        """Cascade variant of delete_location(): deletes the location and
        everything under it. Devices and equipment in scope are deleted
        first -- devices.id -> objects.device_id ON DELETE CASCADE takes
        each device's objects, and those objects' simulation_model_mappings/
        input_exposures, with them automatically. If any surviving object
        is still an aggregate-mapping member (ON DELETE RESTRICT, see
        model_store.ensure_simulation_model_schema's own comment), that
        bulk DELETE raises sqlite3.IntegrityError here -- deliberately left
        uncaught so the whole transaction rolls back; the API layer
        (locations.py::delete_location) translates it into a clean 409,
        mirroring devices.py::delete_device's existing pattern. Locations
        themselves are deleted one at a time, deepest-first (reversed
        BFS order), since parent_location_id has no ON DELETE clause and a
        single bulk `DELETE ... WHERE id IN (...)` can't guarantee that
        ordering itself.
        """
        with self._conn() as conn:
            location_ids = self._subtree_location_ids(conn, location_id)
            loc_placeholders = ",".join("?" * len(location_ids))

            conn.execute(
                f"DELETE FROM devices WHERE location_id IN ({loc_placeholders})",
                location_ids,
            )
            conn.execute(
                f"DELETE FROM equipment WHERE location_id IN ({loc_placeholders})",
                location_ids,
            )

            deleted_count = 0
            for loc_id in reversed(location_ids):
                conn.execute(
                    "DELETE FROM semantic_relationships "
                    "WHERE source_entity_id IN (SELECT id FROM semantic_entities WHERE location_id=?) "
                    "OR target_entity_id IN (SELECT id FROM semantic_entities WHERE location_id=?)",
                    (loc_id, loc_id),
                )
                conn.execute("DELETE FROM semantic_entities WHERE location_id=?", (loc_id,))
                cur = conn.execute("DELETE FROM locations WHERE id=?", (loc_id,))
                deleted_count += cur.rowcount

            conn.commit()
            return deleted_count > 0

    # ── Equipment ─────────────────────────────────────────────────────────────
    # A real, standalone piece of building equipment -- distinct from a
    # `devices` row (the runtime/BACnet controller that hosts points). See
    # src/semantics/mirror.py's module docstring and sync_controller_entity()
    # for the full Device/Equipment/Controller split rationale. Mirrors the
    # Locations CRUD above exactly.

    def get_equipment_list(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM equipment ORDER BY name")]

    def get_equipment(self, equipment_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
            return dict(r) if r else None

    def create_equipment(self, name: str, description: str, location_id: Optional[int], equipment_type: Optional[str] = None) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO equipment (name, description, location_id, equipment_type) VALUES (?,?,?,?)",
                (name, description, location_id, equipment_type),
            )
            sync_entity_from_flat_field(
                conn, entity_kind="equipment", name=name, brick_class=equipment_type, equipment_id=cur.lastrowid,
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM equipment WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_equipment(self, equipment_id: int, name: str, description: str, location_id: Optional[int], equipment_type: Optional[str] = None) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE equipment SET name=?, description=?, location_id=?, equipment_type=? WHERE id=?",
                (name, description, location_id, equipment_type, equipment_id),
            )
            sync_entity_from_flat_field(
                conn, entity_kind="equipment", name=name, brick_class=equipment_type, equipment_id=equipment_id,
            )
            conn.commit()
            r = conn.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
            return dict(r) if r else None

    def delete_equipment(self, equipment_id: int) -> bool:
        """No non-empty guard needed (unlike delete_location): equipment
        has no children of its own in the tree (no sub-equipment table row,
        no devices point at it via a required FK) and its semantic entity/
        relationships cascade via ON DELETE CASCADE."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM equipment WHERE id=?", (equipment_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_assignable_points_for_equipment(self, equipment_id: int) -> list[dict]:
        """Candidate objects for the Equipment panel's "Assign Points"
        action: every object belonging to a Controller that `controls`
        this equipment, plus (via a single query, not a per-object N+1)
        whether each object already has a point semantic entity and, if
        so, what it's currently isPointOf -- so the UI can warn before
        silently reassigning a point that's already semantically owned by
        something else. Returns [] if this equipment has no semantic
        entity yet (unclassified -- see sync_entity_from_flat_field) or no
        controlling Controller."""
        with self._conn() as conn:
            equipment_entity = conn.execute(
                "SELECT id FROM semantic_entities WHERE entity_kind='equipment' AND equipment_id=?",
                (equipment_id,),
            ).fetchone()
            if equipment_entity is None:
                return []

            controllers = self.get_related_entities(equipment_entity["id"], "controls", direction="in")
            controller_device_ids = [c["device_id"] for c in controllers if c.get("device_id") is not None]
            if not controller_device_ids:
                return []

            placeholders = ",".join("?" for _ in controller_device_ids)
            rows = conn.execute(
                f"""
                SELECT
                    o.id AS object_id, o.object_type, o.object_instance, o.name, o.point_type,
                    o.device_id, d.name AS device_name,
                    pe.id AS point_entity_id,
                    target.id AS current_target_entity_id, target.name AS current_target_name
                FROM objects o
                JOIN devices d ON d.id = o.device_id
                LEFT JOIN semantic_entities pe ON pe.entity_kind='point' AND pe.object_id = o.id
                LEFT JOIN semantic_relationships rel ON rel.source_entity_id = pe.id AND rel.predicate='isPointOf'
                LEFT JOIN semantic_entities target ON target.id = rel.target_entity_id
                WHERE o.device_id IN ({placeholders})
                ORDER BY o.device_id, o.object_type, o.object_instance
                """,
                controller_device_ids,
            ).fetchall()

            # A point entity could in principle have more than one isPointOf
            # edge (the graph doesn't forbid it) -- the LEFT JOIN would then
            # produce multiple rows for the same object. Collapse to one
            # candidate per object, keeping the first current assignment
            # found; a second/third simultaneous assignment is an edge case
            # this summary view doesn't need to enumerate exhaustively.
            candidates: dict[int, dict] = {}
            for r in rows:
                row = dict(r)
                obj_id = row["object_id"]
                if obj_id in candidates:
                    continue
                candidates[obj_id] = {
                    "object_id": obj_id,
                    "object_type": row["object_type"],
                    "object_instance": row["object_instance"],
                    "name": row["name"],
                    "point_type": row["point_type"],
                    "device_id": row["device_id"],
                    "device_name": row["device_name"],
                    "point_entity_id": row["point_entity_id"],
                    "current_assignment": (
                        {"entity_id": row["current_target_entity_id"], "name": row["current_target_name"]}
                        if row["current_target_entity_id"] is not None
                        else None
                    ),
                }
            return list(candidates.values())

    def get_controller_topology_point_ids(self, device_id: int) -> Optional[set[int]]:
        """One-hop Brick topology scope for the Simulation Model mapper's
        point candidates: Controller (this device) -> controls -> Equipment
        (anchor), plus Equipment directly upstream/downstream of the anchor
        via `feeds`, plus Locations directly connected to the anchor via
        `feeds` (points isPointOf those Locations directly, e.g. a
        Lighting_Zone -- NOT equipment merely sited at that Location; a
        Location is a leaf in this scope, never expanded back out through
        `hasLocation` to pull in unrelated equipment/controllers). For every
        scoped Equipment, includes points belonging to whichever
        Controller(s) `controls` it (same controller->objects idiom as
        get_assignable_points_for_equipment above).

        Returns None (not an empty set) when this device has no controller
        entity or controls no Equipment -- callers must treat None as "no
        scope resolved, fall back to unscoped behavior", and an empty set
        as "topology resolved but has no eligible points" (return none, do
        not fall back further)."""
        with self._conn() as conn:
            controller_entity = conn.execute(
                "SELECT id FROM semantic_entities WHERE entity_kind='controller' AND device_id=?",
                (device_id,),
            ).fetchone()
            if controller_entity is None:
                return None

            anchors = self.get_related_entities(controller_entity["id"], "controls", direction="out")
            equipment_ids = {e["id"] for e in anchors if e["entity_kind"] == "equipment"}
            if not equipment_ids:
                return None

            location_ids: set[int] = set()
            for anchor_id in list(equipment_ids):
                for direction in ("out", "in"):
                    for target in self.get_related_entities(anchor_id, "feeds", direction=direction):
                        if target["entity_kind"] == "equipment":
                            equipment_ids.add(target["id"])
                        elif target["entity_kind"] == "location":
                            location_ids.add(target["id"])

            controller_device_ids: set[int] = set()
            for equipment_id in equipment_ids:
                for c in self.get_related_entities(equipment_id, "controls", direction="in"):
                    if c.get("device_id") is not None:
                        controller_device_ids.add(c["device_id"])

            point_ids: set[int] = set()
            if controller_device_ids:
                placeholders = ",".join("?" for _ in controller_device_ids)
                rows = conn.execute(
                    f"SELECT id FROM objects WHERE device_id IN ({placeholders})",
                    list(controller_device_ids),
                ).fetchall()
                point_ids.update(r["id"] for r in rows)

            for location_id in location_ids:
                point_ids.update(pt["object_id"] for pt in self.get_entity_points(location_id))

            return point_ids

    # ── Semantic entities/relationships (Brick Core) ────────────────────────
    # Thin CRUD/traversal methods matching the shape of the locations
    # methods above — see src/semantics/ for the multi-hop graph-walking
    # logic (SemanticResolver) built on top of these.

    def get_semantic_entities(
        self,
        *,
        device_id: Optional[int] = None,
        object_id: Optional[int] = None,
        location_id: Optional[int] = None,
        equipment_id: Optional[int] = None,
        entity_kind: Optional[str] = None,
        brick_class: Optional[str] = None,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        for column, value in (
            ("device_id", device_id),
            ("object_id", object_id),
            ("location_id", location_id),
            ("equipment_id", equipment_id),
            ("entity_kind", entity_kind),
            ("brick_class", brick_class),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM semantic_entities{where} ORDER BY id", params
                )
            ]

    def get_semantic_entity(self, entity_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM semantic_entities WHERE id=?", (entity_id,)
            ).fetchone()
            return dict(r) if r else None

    def create_semantic_entity(
        self,
        name: str,
        brick_class: str,
        entity_kind: str,
        *,
        device_id: Optional[int] = None,
        object_id: Optional[int] = None,
        location_id: Optional[int] = None,
        equipment_id: Optional[int] = None,
        local_slug: Optional[str] = None,
    ) -> dict:
        validate_semantic_entity(
            entity_kind, brick_class,
            device_id=device_id, object_id=object_id, location_id=location_id, equipment_id=equipment_id,
        )
        semantic_key = derive_semantic_key(
            entity_kind, brick_class,
            device_id=device_id, object_id=object_id, location_id=location_id, equipment_id=equipment_id,
            local_slug=local_slug,
        )
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO semantic_entities "
                "(name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id, equipment_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id, equipment_id),
            )
            # Direction 2 (Semantic Model panel -> flat field): point/
            # location/equipment(equipment_id) entities are unambiguous
            # (DB-enforced 1:1 via their own partial unique indexes), so an
            # entity created here keeps objects.point_type/locations.kind/
            # equipment.equipment_type in lockstep too -- a user who
            # classifies through this panel instead of the Object/Location/
            # Equipment drawer still sees it reflected there. Deliberately
            # not done for device_id-rooted equipment or controller -- see
            # src/semantics/mirror.py.
            sync_flat_field_from_entity(
                conn, entity_kind=entity_kind, brick_class=brick_class,
                object_id=object_id, location_id=location_id, equipment_id=equipment_id,
            )
            conn.commit()
            return dict(
                conn.execute(
                    "SELECT * FROM semantic_entities WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def update_semantic_entity(
        self,
        entity_id: int,
        name: str,
        brick_class: str,
        entity_kind: str,
        *,
        device_id: Optional[int] = None,
        object_id: Optional[int] = None,
        location_id: Optional[int] = None,
        equipment_id: Optional[int] = None,
        local_slug: Optional[str] = None,
    ) -> Optional[dict]:
        validate_semantic_entity(
            entity_kind, brick_class,
            device_id=device_id, object_id=object_id, location_id=location_id, equipment_id=equipment_id,
        )
        semantic_key = derive_semantic_key(
            entity_kind, brick_class,
            device_id=device_id, object_id=object_id, location_id=location_id, equipment_id=equipment_id,
            local_slug=local_slug,
        )
        with self._conn() as conn:
            old = conn.execute(
                "SELECT * FROM semantic_entities WHERE id=?", (entity_id,)
            ).fetchone()
            old = dict(old) if old else None

            conn.execute(
                "UPDATE semantic_entities SET name=?, local_slug=?, semantic_key=?, "
                "brick_class=?, entity_kind=?, device_id=?, object_id=?, location_id=?, equipment_id=? "
                "WHERE id=?",
                (name, local_slug, semantic_key, brick_class, entity_kind,
                 device_id, object_id, location_id, equipment_id, entity_id),
            )

            # Direction 2, same as create_semantic_entity() above -- plus,
            # if this entity was re-linked away from a different object/
            # location/equipment (unusual, but the API allows it), clear
            # THAT row's flat field first so it doesn't keep pointing at a
            # class this entity no longer represents.
            if old is not None:
                if (old["entity_kind"] == "point" and old.get("object_id") is not None
                        and old["object_id"] != object_id):
                    sync_flat_field_from_entity(
                        conn, entity_kind="point", brick_class=None, object_id=old["object_id"],
                    )
                if (old["entity_kind"] == "location" and old.get("location_id") is not None
                        and old["location_id"] != location_id):
                    sync_flat_field_from_entity(
                        conn, entity_kind="location", brick_class=None, location_id=old["location_id"],
                    )
                if (old["entity_kind"] == "equipment" and old.get("equipment_id") is not None
                        and old["equipment_id"] != equipment_id):
                    sync_flat_field_from_entity(
                        conn, entity_kind="equipment", brick_class=None, equipment_id=old["equipment_id"],
                    )
            sync_flat_field_from_entity(
                conn, entity_kind=entity_kind, brick_class=brick_class,
                object_id=object_id, location_id=location_id, equipment_id=equipment_id,
            )

            conn.commit()
            r = conn.execute(
                "SELECT * FROM semantic_entities WHERE id=?", (entity_id,)
            ).fetchone()
            return dict(r) if r else None

    def delete_semantic_entity(self, entity_id: int) -> bool:
        """Cascades to semantic_relationships referencing this entity (via
        the ON DELETE CASCADE FK) — a relationship pointing at a deleted
        entity has no independent meaning to protect, unlike locations'
        refuse-and-409 pattern. Direction 2 (see src/semantics/mirror.py):
        if this was a point/location/equipment(equipment_id) entity, its
        flat field is cleared too -- deleting the Brick classification
        un-classifies the row everywhere, rather than leaving
        objects.point_type/locations.kind/equipment.equipment_type
        pointing at a class with no backing entity."""
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM semantic_entities WHERE id=?", (entity_id,)
            ).fetchone()
            cur = conn.execute("DELETE FROM semantic_entities WHERE id=?", (entity_id,))
            if existing is not None:
                existing = dict(existing)
                sync_flat_field_from_entity(
                    conn, entity_kind=existing["entity_kind"], brick_class=None,
                    object_id=existing.get("object_id"), location_id=existing.get("location_id"),
                    equipment_id=existing.get("equipment_id"),
                )
            conn.commit()
            return cur.rowcount > 0

    def _get_or_create_semantic_entity(
        self,
        name: str,
        brick_class: str,
        entity_kind: str,
        *,
        device_id: Optional[int] = None,
        object_id: Optional[int] = None,
        location_id: Optional[int] = None,
        local_slug: Optional[str] = None,
    ) -> dict:
        """Shared upsert used by the get_or_create_*_entity() wrappers below.
        Keys off the derived semantic_key via an atomic SQLite UPSERT (see
        src/semantics/backfill.py's upsert_semantic_entity, which does the
        actual write) rather than an app-level SELECT-then-INSERT, so
        concurrent callers (or Database.setup()'s backfill running twice)
        can't race into a duplicate row."""
        if derive_semantic_key(
            entity_kind, brick_class,
            device_id=device_id, object_id=object_id, location_id=location_id,
            local_slug=local_slug,
        ) is None:
            raise ValueError(
                "get_or_create requires at least one of device_id/object_id/"
                "location_id so a semantic_key can be derived"
            )
        with self._conn() as conn:
            entity = upsert_semantic_entity(
                conn,
                name=name, brick_class=brick_class, entity_kind=entity_kind,
                device_id=device_id, object_id=object_id, location_id=location_id,
                local_slug=local_slug,
            )
            conn.commit()
            return entity

    def get_or_create_equipment_entity(
        self, *, device_id: int, name: str, brick_class: str, local_slug: Optional[str] = None,
    ) -> dict:
        return self._get_or_create_semantic_entity(
            name, brick_class, "equipment", device_id=device_id, local_slug=local_slug,
        )

    def get_or_create_point_entity(
        self, *, object_id: int, name: str, brick_class: str, local_slug: Optional[str] = None,
    ) -> dict:
        return self._get_or_create_semantic_entity(
            name, brick_class, "point", object_id=object_id, local_slug=local_slug,
        )

    def get_or_create_location_entity(
        self,
        *,
        name: str,
        brick_class: str,
        location_id: Optional[int] = None,
        device_id: Optional[int] = None,
        local_slug: Optional[str] = None,
    ) -> dict:
        """location_id (a real locations row) and device_id (a virtual,
        device-hosted zone like a Lighting_Zone) are mutually exclusive —
        see validate_semantic_entity()."""
        return self._get_or_create_semantic_entity(
            name, brick_class, "location",
            device_id=device_id, location_id=location_id, local_slug=local_slug,
        )

    def get_semantic_relationships(
        self,
        *,
        source_entity_id: Optional[int] = None,
        target_entity_id: Optional[int] = None,
        predicate: Optional[str] = None,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        for column, value in (
            ("source_entity_id", source_entity_id),
            ("target_entity_id", target_entity_id),
            ("predicate", predicate),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM semantic_relationships{where} ORDER BY id", params
                )
            ]

    def get_semantic_relationship(self, relationship_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM semantic_relationships WHERE id=?", (relationship_id,)
            ).fetchone()
            return dict(r) if r else None

    def create_semantic_relationship(
        self, source_entity_id: int, predicate: str, target_entity_id: int,
    ) -> dict:
        """Re-creating an identical relationship is a no-op returning the
        existing row, not an error — matches the UPSERT idempotency used
        for semantic_entities (see upsert_semantic_relationship)."""
        with self._conn() as conn:
            relationship = upsert_semantic_relationship(conn, source_entity_id, predicate, target_entity_id)
            conn.commit()
            return relationship

    def delete_semantic_relationship(self, relationship_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM semantic_relationships WHERE id=?", (relationship_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def get_related_entities(
        self, entity_id: int, predicate: str, direction: str = "out",
    ) -> list[dict]:
        """direction='out': entity_id is source_entity_id (what does X
        relate to). direction='in': entity_id is target_entity_id (what
        relates to X) — this is the direction 'get all points isPointOf
        entity X' actually needs, since points are the source of isPointOf
        edges."""
        if direction not in ("out", "in"):
            raise ValueError("direction must be 'out' or 'in'")
        with self._conn() as conn:
            if direction == "out":
                rows = conn.execute(
                    "SELECT se.* FROM semantic_relationships sr "
                    "JOIN semantic_entities se ON se.id = sr.target_entity_id "
                    "WHERE sr.source_entity_id=? AND sr.predicate=?",
                    (entity_id, predicate),
                )
            else:
                rows = conn.execute(
                    "SELECT se.* FROM semantic_relationships sr "
                    "JOIN semantic_entities se ON se.id = sr.source_entity_id "
                    "WHERE sr.target_entity_id=? AND sr.predicate=?",
                    (entity_id, predicate),
                )
            return [dict(r) for r in rows]

    def get_entity_points(
        self, entity_id: int, brick_class: Optional[str] = None,
    ) -> list[dict]:
        """Sugar for get_related_entities(entity_id, 'isPointOf',
        direction='in'), joined to objects for object_type/object_instance/
        name/units so callers get a ready-to-use point row."""
        with self._conn() as conn:
            query = (
                "SELECT se.*, o.object_type, o.object_instance, o.units, o.name AS object_name "
                "FROM semantic_relationships sr "
                "JOIN semantic_entities se ON se.id = sr.source_entity_id "
                "JOIN objects o ON o.id = se.object_id "
                "WHERE sr.target_entity_id=? AND sr.predicate='isPointOf'"
            )
            params: list[Any] = [entity_id]
            if brick_class is not None:
                query += " AND se.brick_class=?"
                params.append(brick_class)
            return [dict(r) for r in conn.execute(query, params)]

    # has_controller_entity is computed at query time (never a stored
    # column) -- it's how the frontend's DeviceDrawer distinguishes "this
    # device already has a Controller semantic role, keep it in sync on
    # edit" from "this is a legacy device, never touch its semantics on
    # edit" (see sync_controller_entity()'s docstring for why that
    # distinction matters).
    _DEVICES_SELECT = (
        "SELECT devices.*, EXISTS("
        "  SELECT 1 FROM semantic_entities "
        "  WHERE entity_kind='controller' AND device_id=devices.id"
        ") AS has_controller_entity FROM devices"
    )

    def get_devices(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(f"{self._DEVICES_SELECT} ORDER BY device_instance")]

    def get_device(self, device_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute(f"{self._DEVICES_SELECT} WHERE devices.id=?", (device_id,)).fetchone()
            return dict(r) if r else None

    def sync_external_devices(self, discovered: list[dict]) -> list[dict]:
        """
        Reconciles a fresh discovery pass into the project's external-BACnet
        device inventory: updates the existing row if a device with this
        instance was already synced, inserts a new row if not. Never
        deletes -- a device missing from one Discover/Rediscover run may
        just be temporarily offline (see plan notes). (device_instance,
        source_type) is the reconciliation key, matching the table's own
        UNIQUE constraint -- this is the only place in the codebase that
        ever writes source_type='external-bacnet'.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            for d in discovered:
                meta = d.get("metadata") or {}
                conn.execute(
                    "INSERT INTO devices (device_instance, name, description, vendor_name, "
                    "model_name, enabled, source_type, external_host, external_port, "
                    "external_vendor_id, external_last_seen_at) "
                    "VALUES (:device_instance, :name, :description, :vendor_name, :model_name, "
                    "1, 'external-bacnet', :external_host, :external_port, "
                    ":external_vendor_id, :external_last_seen_at) "
                    "ON CONFLICT(device_instance, source_type) DO UPDATE SET "
                    "name=excluded.name, description=excluded.description, "
                    "vendor_name=excluded.vendor_name, model_name=excluded.model_name, "
                    "external_host=excluded.external_host, external_port=excluded.external_port, "
                    "external_vendor_id=excluded.external_vendor_id, "
                    "external_last_seen_at=excluded.external_last_seen_at",
                    {
                        "device_instance": d["device_instance"],
                        "name": meta.get("objectName") or d.get("name") or f"bacnet_device_{d['device_instance']}",
                        "description": meta.get("description") or "",
                        "vendor_name": meta.get("vendorName") or "Unknown",
                        "model_name": meta.get("modelName") or "Unknown",
                        "external_host": d.get("host"),
                        "external_port": d.get("port"),
                        "external_vendor_id": meta.get("vendorId"),
                        "external_last_seen_at": now,
                    },
                )
            conn.commit()
            return [dict(r) for r in conn.execute(
                "SELECT * FROM devices WHERE source_type='external-bacnet' ORDER BY device_instance"
            )]

    def create_device(self, data: dict) -> dict:
        # `equipment_type` is presented in the UI as the device's Brick
        # class -- Brick is the source of truth, `devices.equipment_type`
        # is a compatibility mirror kept in lockstep automatically (see
        # src/semantics/mirror.py). This only ever syncs the device's own
        # top-level equipment entity, never Supply_Fan/Return_Fan-style
        # sub-equipment sharing this device_id.
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO devices (device_instance, name, description, vendor_name, model_name, enabled, "
                "firmware_revision, protocol_revision, max_apdu_length_accepted, segmentation_supported, "
                "location_id, equipment_type, can_receive_event_notifications, "
                "simulation_mode, source_device_id, replay_recording_id) "
                "VALUES (:device_instance, :name, :description, :vendor_name, :model_name, :enabled, "
                ":firmware_revision, :protocol_revision, :max_apdu_length_accepted, :segmentation_supported, "
                ":location_id, :equipment_type, :can_receive_event_notifications, "
                ":simulation_mode, :source_device_id, :replay_recording_id)",
                {
                    **data,
                    "location_id": data.get("location_id"),
                    "equipment_type": data.get("equipment_type"),
                    "can_receive_event_notifications": data.get("can_receive_event_notifications"),
                    "simulation_mode": data.get("simulation_mode", "simulation"),
                    "source_device_id": data.get("source_device_id"),
                    "replay_recording_id": data.get("replay_recording_id"),
                },
            )
            sync_entity_from_flat_field(
                conn, entity_kind="equipment", name=data.get("name") or "",
                brick_class=data.get("equipment_type"), device_id=cur.lastrowid,
            )
            sync_device_location_relationship(
                conn, device_id=cur.lastrowid, location_id=data.get("location_id"),
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM devices WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_device(self, device_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT simulation_mode, source_device_id FROM devices WHERE id=?", (device_id,)
            ).fetchone()
            if not existing:
                return None
            # None from DeviceUpdate means "caller did not send this field" -- preserve
            # the stored value rather than resetting to a schema default.
            sim_mode = data.get("simulation_mode") if data.get("simulation_mode") is not None else existing["simulation_mode"]
            # source_device_id is provenance set at creation; always preserve existing.
            src_dev_id = existing["source_device_id"]
            conn.execute(
                "UPDATE devices SET device_instance=:device_instance, name=:name, "
                "description=:description, vendor_name=:vendor_name, model_name=:model_name, "
                "enabled=:enabled, firmware_revision=:firmware_revision, protocol_revision=:protocol_revision, "
                "max_apdu_length_accepted=:max_apdu_length_accepted, "
                "segmentation_supported=:segmentation_supported, location_id=:location_id, "
                "equipment_type=:equipment_type, "
                "can_receive_event_notifications=:can_receive_event_notifications, "
                "simulation_mode=:simulation_mode, source_device_id=:source_device_id WHERE id=:id",
                {
                    **data,
                    "location_id": data.get("location_id"),
                    "equipment_type": data.get("equipment_type"),
                    "can_receive_event_notifications": data.get("can_receive_event_notifications"),
                    "simulation_mode": sim_mode,
                    "source_device_id": src_dev_id,
                    "id": device_id,
                },
            )
            sync_entity_from_flat_field(
                conn, entity_kind="equipment", name=data.get("name") or "",
                brick_class=data.get("equipment_type"), device_id=device_id,
            )
            sync_device_location_relationship(
                conn, device_id=device_id, location_id=data.get("location_id"),
            )
            conn.commit()
            r = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            return dict(r) if r else None

    def delete_device(self, device_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            conn.commit()
            return cur.rowcount > 0

    def ensure_controller_entity(self, device_id: int) -> Optional[dict]:
        """Thin wrapper around sync_controller_entity() -- looks up the
        device's current name and upserts its entity_kind='controller'
        semantic entity. Returns None if the device doesn't exist. This is
        called from exactly one place: POST /devices/{id}/controller (see
        that endpoint's docstring for why nothing else may call it)."""
        with self._conn() as conn:
            device = conn.execute("SELECT name FROM devices WHERE id=?", (device_id,)).fetchone()
            if device is None:
                return None
            entity = sync_controller_entity(conn, device_id=device_id, name=device["name"])
            conn.commit()
            return entity

    def get_all_points(
        self,
        object_type: Optional[str] = None,
        device_id: Optional[int] = None,
        point_type: Optional[str] = None,
    ) -> list[dict]:
        """Cross-device point listing for the Functional Test builder's
        PointPicker (src/api/routers/points.py) -- every object on every
        device, joined with its device's name/source_type, unlike
        get_assignable_points_for_equipment/get_entity_points which are
        both scoped to one equipment/entity. Live value is intentionally
        NOT included here (this is a single indexed query, not per-row
        engine calls) -- the router annotates it in afterward, for
        simulated rows only, via engine.get_object_value()."""
        clauses = []
        params: list[Any] = []
        if object_type:
            clauses.append("o.object_type = ?")
            params.append(object_type)
        if device_id is not None:
            clauses.append("o.device_id = ?")
            params.append(device_id)
        if point_type:
            clauses.append("o.point_type = ?")
            params.append(point_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    o.id AS object_id, o.device_id, d.name AS device_name,
                    o.object_type, o.object_instance, o.name, o.units, o.point_type,
                    d.source_type
                FROM objects o
                JOIN devices d ON d.id = o.device_id
                {where}
                ORDER BY d.name, o.object_type, o.object_instance
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_objects(self, device_id: int) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM objects WHERE device_id=? ORDER BY object_type, object_instance",
                (device_id,),
            )]

    def get_object(self, obj_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM objects WHERE id=?", (obj_id,)).fetchone()
            return dict(r) if r else None

    def touch_external_device_last_seen(self, device_id: int) -> None:
        """Bumps external_last_seen_at after a successful discover/refresh
        touch -- the minimal "seen/not seen" signal, no device-health
        subsystem."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET external_last_seen_at=? WHERE id=? AND source_type='external-bacnet'",
                (datetime.now(timezone.utc).isoformat(), device_id),
            )
            conn.commit()

    def sync_external_objects(self, device_id: int, points: list[dict]) -> list[dict]:
        """
        Upserts structural object identity for an external-BACnet device's
        discovered points, keyed on the existing UNIQUE(device_id,
        object_type, object_instance) constraint -- the same identity
        already used for simulated objects. Each `points` entry is a plain
        dict with object_type/object_instance/name/units/description.
        Deliberately never touches behavior/behavior_params/manual_value
        (simulation-only columns, left at their harmless defaults) or any
        live present-value -- present values are read fresh and returned in
        the API response only, never persisted here (see the discovery-vs-
        refresh split in src/api/routers/external_objects.py).
        """
        with self._conn() as conn:
            for p in points:
                conn.execute(
                    "INSERT INTO objects (device_id, object_type, object_instance, name, units, description) "
                    "VALUES (:device_id, :object_type, :object_instance, :name, :units, :description) "
                    "ON CONFLICT(device_id, object_type, object_instance) DO UPDATE SET "
                    "name=excluded.name, units=excluded.units, description=excluded.description",
                    {
                        "device_id": device_id,
                        "object_type": p["object_type"],
                        "object_instance": p["object_instance"],
                        "name": p.get("name") or f"{p['object_type']}_{p['object_instance']}",
                        "units": p.get("units") or "no-units",
                        "description": p.get("description"),
                    },
                )
            conn.commit()
            return [dict(r) for r in conn.execute(
                "SELECT * FROM objects WHERE device_id=? ORDER BY object_type, object_instance",
                (device_id,),
            )]

    def create_object(self, device_id: int, data: dict) -> dict:
        # `point_type` is presented in the UI as the point's Brick class --
        # Brick is the source of truth, `objects.point_type` is a
        # compatibility mirror kept in lockstep automatically (see
        # src/semantics/mirror.py). Unambiguous: at most one point entity
        # can ever reference a given object_id (DB-enforced).
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO objects (device_id, object_type, object_instance, name, units, behavior, behavior_params, enabled, number_of_states, reliability, polarity, point_type) "
                "VALUES (:device_id, :object_type, :object_instance, :name, :units, :behavior, :behavior_params, :enabled, :number_of_states, :reliability, :polarity, :point_type)",
                {**data, "device_id": device_id},
            )
            sync_entity_from_flat_field(
                conn, entity_kind="point", name=data.get("name") or "",
                brick_class=data.get("point_type"), object_id=cur.lastrowid,
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM objects WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_object(self, obj_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE objects SET object_type=:object_type, object_instance=:object_instance, "
                "name=:name, units=:units, behavior=:behavior, behavior_params=:behavior_params, "
                "enabled=:enabled, number_of_states=:number_of_states, reliability=:reliability, "
                "polarity=:polarity, point_type=:point_type WHERE id=:id",
                {**data, "id": obj_id},
            )
            sync_entity_from_flat_field(
                conn, entity_kind="point", name=data.get("name") or "",
                brick_class=data.get("point_type"), object_id=obj_id,
            )
            conn.commit()
            r = conn.execute("SELECT * FROM objects WHERE id=?", (obj_id,)).fetchone()
            return dict(r) if r else None

    def set_manual_value(self, obj_id: int, value: Any) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE objects SET manual_value=? WHERE id=?", (value, obj_id))
            conn.commit()

    def write_object(self, obj_id: int, value: Any) -> None:
        """Switch object to manual behavior and persist the written value."""
        params = json.dumps({"value": value})
        with self._conn() as conn:
            conn.execute(
                "UPDATE objects SET behavior='manual', behavior_params=?, manual_value=? WHERE id=?",
                (params, value, obj_id),
            )
            conn.commit()

    def delete_object(self, obj_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM objects WHERE id=?", (obj_id,))
            conn.commit()
            return cur.rowcount > 0

    def import_ede_objects(self, device_id: int, objects: list[dict]) -> int:
        """Upsert EDE rows into an existing device, keyed on (object_type,
        object_instance) — matches the objects table's UNIQUE constraint."""
        with self._conn() as conn:
            for obj in objects:
                conn.execute(
                    "INSERT INTO objects "
                    "(device_id, object_type, object_instance, name, units, behavior, behavior_params, enabled) "
                    "VALUES (:device_id, :object_type, :object_instance, :name, :units, :behavior, :behavior_params, :enabled) "
                    "ON CONFLICT(device_id, object_type, object_instance) DO UPDATE SET "
                    "name=excluded.name, units=excluded.units, behavior=excluded.behavior, "
                    "behavior_params=excluded.behavior_params, enabled=excluded.enabled",
                    {**obj, "device_id": device_id},
                )
            conn.commit()
            return len(objects)

    # ── Energy────────────────────────────────────────────

    def insert_energy_history_batch(
    self,
    rows: list[dict],
        ) -> None:
            if not rows:
                return

            with self._conn() as conn:
                conn.executemany(
                    """
                    INSERT INTO energy_history (
                        timestamp,
                        device_id,
                        model_type,
                        power_kw,
                        total_energy_kwh,
                        source,
                        confidence,
                        metrics
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            row["timestamp"],
                            row["device_id"],
                            row["model_type"],
                            row.get("power_kw"),
                            row.get("total_energy_kwh"),
                            row.get("source"),
                            row.get("confidence"),
                            row.get("metrics", "{}"),
                        )
                        for row in rows
                    ],
                )

                conn.commit()

    def delete_energy_history_before(
    self,
    cutoff_timestamp: float,
        ) -> int:
            with self._conn() as conn:
                cursor = conn.execute(
                    """
                    DELETE FROM energy_history
                    WHERE timestamp < ?
                    """,
                    (cutoff_timestamp,),
                )

                conn.commit()
                return cursor.rowcount

    def get_energy_history(
    self,
    *,
    start_timestamp: float,
    end_timestamp: float,
    device_id: int | None = None,
    model_type: str | None = None,
    ) -> list[dict]:
        sql = """
            SELECT
                id,
                timestamp,
                device_id,
                model_type,
                power_kw,
                total_energy_kwh,
                source,
                confidence,
                metrics
            FROM energy_history
            WHERE timestamp >= ?
            AND timestamp <= ?
        """

        params: list[object] = [
            start_timestamp,
            end_timestamp,
        ]

        if device_id is not None:
            sql += " AND device_id = ?"
            params.append(device_id)

        if model_type is not None:
            sql += " AND model_type = ?"
            params.append(model_type)

        sql += " ORDER BY timestamp ASC"

        with self._conn() as conn:
            rows = conn.execute(
                sql,
                params,
            ).fetchall()

            return [dict(row) for row in rows]
    
    def get_energy_model_config(self, device_id: int, model_type: str, instance_key: str = "default") -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM energy_model_configs WHERE device_id=? AND model_type=? AND instance_key=?",
                (device_id, model_type, instance_key),
            ).fetchone()
            return dict(row) if row else None

    def get_energy_model_configs(self, device_id: int) -> list[dict]:
        """Every config row for one device, any model_type/instance_key --
        used by the admin UI's per-device Energy Model list (unlike
        get_energy_model_config, which needs the full composite key)."""
        with self._conn() as conn:
            return [
                dict(row) for row in conn.execute(
                    "SELECT * FROM energy_model_configs WHERE device_id=? ORDER BY model_type, instance_key",
                    (device_id,),
                )
            ]

    def get_enabled_energy_model_configs(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM energy_model_configs WHERE enabled=1 ORDER BY device_id, model_type")]

    def upsert_energy_model_config(
        self, device_id: int, model_type: str, parameters: str, enabled: bool = True, instance_key: str = "default",
    ) -> dict:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO energy_model_configs (device_id, model_type, instance_key, enabled, parameters) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id, model_type, instance_key) DO UPDATE SET "
                "enabled=excluded.enabled, parameters=excluded.parameters",
                (device_id, model_type, instance_key, int(enabled), parameters),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM energy_model_configs WHERE device_id=? AND model_type=? AND instance_key=?",
                (device_id, model_type, instance_key),
            ).fetchone()
            if row is None:
                raise RuntimeError("Energy model configuration was not saved")
            return dict(row)

    def delete_energy_model_config(self, device_id: int, model_type: str, instance_key: str = "default") -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM energy_model_configs WHERE device_id=? AND model_type=? AND instance_key=?",
                (device_id, model_type, instance_key),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_energy_model_config_by_id(self, config_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM energy_model_configs WHERE id=?", (config_id,)).fetchone()
            return dict(row) if row else None

    def update_energy_model_config_by_id(
        self, config_id: int, *, model_type: str, instance_key: str, enabled: bool, parameters: str,
    ) -> dict | None:
        """Row-id-addressed update (used by PUT /energy/models/{id}) -- unlike
        upsert_energy_model_config, this can also change model_type/
        instance_key on an existing row, so a change that collides with a
        DIFFERENT existing row raises sqlite3.IntegrityError (caught by the
        router and turned into a 409, same pattern as semantic entities)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE energy_model_configs SET model_type=?, instance_key=?, enabled=?, parameters=? WHERE id=?",
                (model_type, instance_key, int(enabled), parameters, config_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM energy_model_configs WHERE id=?", (config_id,)).fetchone()
            return dict(row) if row else None

    def delete_energy_model_config_by_id(self, config_id: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM energy_model_configs WHERE id=?", (config_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ── Alarms: Notification Classes ────────────────────────────────────────────

    def get_notification_classes(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute(
                    "SELECT * FROM notification_classes WHERE device_id=? ORDER BY name", (device_id,)
                )
            else:
                rows = conn.execute("SELECT * FROM notification_classes ORDER BY device_id, name")
            return [dict(r) for r in rows]

    def get_notification_class(self, nc_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM notification_classes WHERE id=?", (nc_id,)).fetchone()
            return dict(r) if r else None

    def create_notification_class(self, device_id: int, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO notification_classes "
                "(device_id, name, priority_to_offnormal, priority_to_fault, priority_to_normal, "
                "ack_required_transitions, recipients) "
                "VALUES (:device_id, :name, :priority_to_offnormal, :priority_to_fault, :priority_to_normal, "
                ":ack_required_transitions, :recipients)",
                {**data, "device_id": device_id},
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT * FROM notification_classes WHERE id=?", (cur.lastrowid,)
            ).fetchone())

    def update_notification_class(self, nc_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE notification_classes SET name=:name, "
                "priority_to_offnormal=:priority_to_offnormal, priority_to_fault=:priority_to_fault, "
                "priority_to_normal=:priority_to_normal, ack_required_transitions=:ack_required_transitions, "
                "recipients=:recipients WHERE id=:id",
                {**data, "id": nc_id},
            )
            conn.commit()
            r = conn.execute("SELECT * FROM notification_classes WHERE id=?", (nc_id,)).fetchone()
            return dict(r) if r else None

    def delete_notification_class(self, nc_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM notification_classes WHERE id=?", (nc_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Alarms: per-object intrinsic reporting config ───────────────────────────

    def get_alarm_config(self, object_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM object_alarm_configs WHERE object_id=?", (object_id,)
            ).fetchone()
            return dict(r) if r else None

    def set_alarm_config(self, object_id: int, data: dict) -> dict:
        """Upsert the intrinsic-reporting config for one object."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO object_alarm_configs "
                "(object_id, notification_class_id, enabled, event_enable, notify_type, "
                "time_delay, time_delay_normal, params) "
                "VALUES (:object_id, :notification_class_id, :enabled, :event_enable, :notify_type, "
                ":time_delay, :time_delay_normal, :params) "
                "ON CONFLICT(object_id) DO UPDATE SET "
                "notification_class_id=excluded.notification_class_id, enabled=excluded.enabled, "
                "event_enable=excluded.event_enable, notify_type=excluded.notify_type, "
                "time_delay=excluded.time_delay, time_delay_normal=excluded.time_delay_normal, "
                "params=excluded.params",
                {**data, "object_id": object_id},
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT * FROM object_alarm_configs WHERE object_id=?", (object_id,)
            ).fetchone())

    def delete_alarm_config(self, object_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM object_alarm_configs WHERE object_id=?", (object_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_all_alarm_configs(self) -> list[dict]:
        """Every enabled intrinsic-reporting config, joined with its object row —
        used once per tick so the engine doesn't have to query per-object."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT oac.*, o.device_id AS obj_device_id, o.object_type, o.object_instance, "
                "o.name AS object_name "
                "FROM object_alarm_configs oac JOIN objects o ON o.id = oac.object_id "
                "WHERE oac.enabled = 1"
            )
            return [dict(r) for r in rows]

    # ── Alarms: event log ────────────────────────────────────────────────────────

    def log_alarm(self, entry: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO alarm_log "
                "(object_id, device_id, object_name, from_state, to_state, priority, value, "
                "message, ack_required) "
                "VALUES (:object_id, :device_id, :object_name, :from_state, :to_state, :priority, "
                ":value, :message, :ack_required)",
                entry,
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM alarm_log WHERE id=?", (cur.lastrowid,)).fetchone())

    def get_alarm_log(self, limit: int = 200, unacked_only: bool = False) -> list[dict]:
        with self._conn() as conn:
            query = "SELECT * FROM alarm_log"
            if unacked_only:
                query += " WHERE ack_required=1 AND acknowledged=0"
            query += " ORDER BY id DESC LIMIT ?"
            return [dict(r) for r in conn.execute(query, (limit,))]

    def ack_alarm(self, alarm_id: int, ack_by: str) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE alarm_log SET acknowledged=1, ack_ts=datetime('now'), ack_by=? WHERE id=?",
                (ack_by, alarm_id),
            )
            conn.commit()
            r = conn.execute("SELECT * FROM alarm_log WHERE id=?", (alarm_id,)).fetchone()
            return dict(r) if r else None

    # ── Alarms: Event Enrollments (Algorithmic Reporting) ───────────────────────

    def get_event_enrollments(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute(
                    "SELECT * FROM event_enrollments WHERE device_id=? ORDER BY name", (device_id,)
                )
            else:
                rows = conn.execute("SELECT * FROM event_enrollments ORDER BY device_id, name")
            return [dict(r) for r in rows]

    def get_event_enrollment(self, ee_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM event_enrollments WHERE id=?", (ee_id,)).fetchone()
            return dict(r) if r else None

    def create_event_enrollment(self, device_id: int, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO event_enrollments "
                "(device_id, name, monitored_object_id, algorithm, event_parameters, "
                "notification_class_id, enabled, event_enable, notify_type, time_delay, time_delay_normal) "
                "VALUES (:device_id, :name, :monitored_object_id, :algorithm, :event_parameters, "
                ":notification_class_id, :enabled, :event_enable, :notify_type, :time_delay, :time_delay_normal)",
                {**data, "device_id": device_id},
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT * FROM event_enrollments WHERE id=?", (cur.lastrowid,)
            ).fetchone())

    def update_event_enrollment(self, ee_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE event_enrollments SET name=:name, monitored_object_id=:monitored_object_id, "
                "algorithm=:algorithm, event_parameters=:event_parameters, "
                "notification_class_id=:notification_class_id, enabled=:enabled, "
                "event_enable=:event_enable, notify_type=:notify_type, time_delay=:time_delay, "
                "time_delay_normal=:time_delay_normal WHERE id=:id",
                {**data, "id": ee_id},
            )
            conn.commit()
            r = conn.execute("SELECT * FROM event_enrollments WHERE id=?", (ee_id,)).fetchone()
            return dict(r) if r else None

    def delete_event_enrollment(self, ee_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM event_enrollments WHERE id=?", (ee_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_all_event_enrollments(self) -> list[dict]:
        """Every enabled enrollment joined with its monitored object row —
        used once per tick, mirroring get_all_alarm_configs()."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ee.*, o.object_type, o.object_instance, o.name AS object_name, "
                "o.units AS object_units "
                "FROM event_enrollments ee JOIN objects o ON o.id = ee.monitored_object_id "
                "WHERE ee.enabled = 1"
            )
            return [dict(r) for r in rows]

    # ── Trend Logs ───────────────────────────────────────────────────────────────

    def get_trend_logs(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute("SELECT * FROM trend_logs WHERE device_id=? ORDER BY name", (device_id,))
            else:
                rows = conn.execute("SELECT * FROM trend_logs ORDER BY device_id, name")
            return [dict(r) for r in rows]

    def get_trend_log(self, tl_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM trend_logs WHERE id=?", (tl_id,)).fetchone()
            return dict(r) if r else None

    def create_trend_log(self, device_id: int, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO trend_logs "
                "(device_id, name, description, monitored_object_id, logging_type, log_interval, "
                "cov_increment, buffer_size, stop_when_full, enabled) "
                "VALUES (:device_id, :name, :description, :monitored_object_id, :logging_type, :log_interval, "
                ":cov_increment, :buffer_size, :stop_when_full, :enabled)",
                {**data, "device_id": device_id},
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM trend_logs WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_trend_log(self, tl_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE trend_logs SET name=:name, description=:description, "
                "monitored_object_id=:monitored_object_id, logging_type=:logging_type, "
                "log_interval=:log_interval, cov_increment=:cov_increment, buffer_size=:buffer_size, "
                "stop_when_full=:stop_when_full, enabled=:enabled WHERE id=:id",
                {**data, "id": tl_id},
            )
            conn.commit()
            r = conn.execute("SELECT * FROM trend_logs WHERE id=?", (tl_id,)).fetchone()
            return dict(r) if r else None

    def delete_trend_log(self, tl_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM trend_logs WHERE id=?", (tl_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_all_trend_logs(self) -> list[dict]:
        """Every enabled trend log joined with its monitored object row —
        used once per tick, mirroring get_all_alarm_configs()."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT tl.*, o.object_type, o.object_instance, o.name AS object_name "
                "FROM trend_logs tl JOIN objects o ON o.id = tl.monitored_object_id "
                "WHERE tl.enabled = 1"
            )
            return [dict(r) for r in rows]

    def add_trend_record(self, trend_log_id: int, value: Any, status_flags: str = "[]") -> Optional[int]:
        """Append a sample, enforcing the circular-buffer/stop-when-full
        semantics. Returns the new record's sequence number, or None if the
        buffer is full and stop_when_full is set."""
        with self._conn() as conn:
            cfg = conn.execute("SELECT * FROM trend_logs WHERE id=?", (trend_log_id,)).fetchone()
            if not cfg:
                return None
            if cfg["record_count"] >= cfg["buffer_size"]:
                if cfg["stop_when_full"]:
                    return None
                conn.execute(
                    "DELETE FROM trend_log_records WHERE id = ("
                    "SELECT id FROM trend_log_records WHERE trend_log_id=? ORDER BY sequence_number ASC LIMIT 1)",
                    (trend_log_id,),
                )
                new_count = cfg["record_count"]
            else:
                new_count = cfg["record_count"] + 1
            next_seq = cfg["total_record_count"] + 1
            conn.execute(
                "INSERT INTO trend_log_records (trend_log_id, sequence_number, value, status_flags) "
                "VALUES (?, ?, ?, ?)",
                (trend_log_id, next_seq, str(value), status_flags),
            )
            conn.execute(
                "UPDATE trend_logs SET record_count=?, total_record_count=?, last_sampled_at=? WHERE id=?",
                (new_count, next_seq, time.time(), trend_log_id),
            )
            conn.commit()
            return next_seq

    def get_trend_log_records(
        self, trend_log_id: int, from_ts: Optional[str] = None, to_ts: Optional[str] = None,
        start_sequence: Optional[int] = None, limit: int = 200, order: str = "asc",
    ) -> list[dict]:
        with self._conn() as conn:
            query = "SELECT * FROM trend_log_records WHERE trend_log_id=?"
            params: list[Any] = [trend_log_id]
            if from_ts:
                query += " AND ts >= ?"
                params.append(from_ts)
            if to_ts:
                query += " AND ts <= ?"
                params.append(to_ts)
            if start_sequence is not None:
                query += " AND sequence_number >= ?"
                params.append(start_sequence)
            query += f" ORDER BY sequence_number {'DESC' if order == 'desc' else 'ASC'} LIMIT ?"
            params.append(limit)
            return [dict(r) for r in conn.execute(query, params)]

    def clear_trend_log_records(self, trend_log_id: int) -> bool:
        """Clears the buffer but keeps total_record_count monotonic, matching
        real BACnet Trend Log clear-buffer semantics (sequence numbers never
        reuse, even across a clear)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM trend_log_records WHERE trend_log_id=?", (trend_log_id,))
            cur = conn.execute("UPDATE trend_logs SET record_count=0 WHERE id=?", (trend_log_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Replay Recordings ────────────────────────────────────────────────────────
    # Application-managed, SQLite-backed device-wide recording of an external
    # BACnet device's values, later replayed through a cloned simulated
    # device ("Replay" mode in CreateSimulatedCopyModal.vue). Distinct from
    # BACnet TrendLog (a BACnet-standard, single-point history) -- a
    # recording samples every selected point on a device together, once per
    # cycle, so buffering/eviction operates on whole snapshots
    # (sample_index), not individual point rows. point_count/sample_count
    # are computed here rather than stored, unlike trend_logs.record_count/
    # total_record_count -- both are cheap indexed lookups
    # (idx_replay_recording_points_recording / idx_replay_samples_recording_index).

    def _replay_recording_counts(self, conn: sqlite3.Connection, recording_id: int) -> dict[str, Any]:
        point_count = conn.execute(
            "SELECT COUNT(*) FROM replay_recording_points WHERE recording_id=?", (recording_id,)
        ).fetchone()[0]
        sample_count = conn.execute(
            "SELECT COUNT(DISTINCT sample_index) FROM replay_samples WHERE recording_id=?", (recording_id,)
        ).fetchone()[0]
        # min/max, not just the count -- buffer_mode='overwrite' eviction can
        # leave the stored range starting above 0 (see
        # get_replay_recording_sample_index_bounds's own comment), and the
        # UI (seek slider) needs the real bounds, not an assumed 0-based one.
        min_idx, max_idx = conn.execute(
            "SELECT MIN(sample_index), MAX(sample_index) FROM replay_samples WHERE recording_id=?", (recording_id,)
        ).fetchone()
        return {
            "point_count": point_count,
            "sample_count": sample_count,
            "sample_index_min": min_idx,
            "sample_index_max": max_idx,
        }

    def get_replay_recordings(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute(
                    "SELECT * FROM replay_recordings WHERE source_device_id=? ORDER BY created_at DESC", (device_id,)
                )
            else:
                rows = conn.execute("SELECT * FROM replay_recordings ORDER BY source_device_id, created_at DESC")
            result = []
            for r in rows:
                item = dict(r)
                item.update(self._replay_recording_counts(conn, item["id"]))
                result.append(item)
            return result

    def get_replay_recording(self, recording_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM replay_recordings WHERE id=?", (recording_id,)).fetchone()
            if not r:
                return None
            item = dict(r)
            item.update(self._replay_recording_counts(conn, recording_id))
            item["points"] = [
                dict(p) for p in conn.execute(
                    "SELECT * FROM replay_recording_points WHERE recording_id=? ORDER BY id", (recording_id,)
                )
            ]
            return item

    def create_replay_recording(self, device_id: int, data: dict) -> dict:
        """Creates the recording row (status='recording', started immediately
        -- there is no separate idle/draft state) and snapshots the selected
        points' identity into replay_recording_points. point_ids=None means
        "all of this device's current points"."""
        with self._conn() as conn:
            point_ids = data.get("point_ids")
            if point_ids is None:
                point_rows = conn.execute(
                    "SELECT * FROM objects WHERE device_id=? ORDER BY object_type, object_instance", (device_id,)
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in point_ids)
                point_rows = conn.execute(
                    f"SELECT * FROM objects WHERE device_id=? AND id IN ({placeholders})",
                    (device_id, *point_ids),
                ).fetchall()

            cur = conn.execute(
                "INSERT INTO replay_recordings "
                "(source_device_id, name, description, status, sample_interval_seconds, "
                "maximum_samples, buffer_mode) "
                "VALUES (:source_device_id, :name, :description, 'recording', "
                ":sample_interval_seconds, :maximum_samples, :buffer_mode)",
                {
                    "source_device_id": device_id,
                    "name": data["name"],
                    "description": data.get("description", ""),
                    "sample_interval_seconds": data["sample_interval_seconds"],
                    "maximum_samples": data["maximum_samples"],
                    "buffer_mode": data["buffer_mode"],
                },
            )
            recording_id = cur.lastrowid
            for p in point_rows:
                conn.execute(
                    "INSERT INTO replay_recording_points "
                    "(recording_id, source_object_id, object_type, object_instance, object_name, point_type, units) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (recording_id, p["id"], p["object_type"], p["object_instance"], p["name"],
                     p["point_type"], p["units"]),
                )
            conn.commit()
        return self.get_replay_recording(recording_id)

    def stop_replay_recording(self, recording_id: int) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE replay_recordings SET status='completed', ended_at=datetime('now') "
                "WHERE id=? AND status='recording'",
                (recording_id,),
            )
            conn.commit()
        return self.get_replay_recording(recording_id)

    def delete_replay_recording(self, recording_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM replay_recordings WHERE id=?", (recording_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_replay_recording_names(self, recording_ids: list[int]) -> dict[int, str]:
        """Bulk id->name lookup -- backs devices.py's list_devices()
        attaching active_replay_recording to each Replay device, the same
        way get_active_simulation_models_by_device() backs
        active_simulation_model. One query for every device on the page
        rather than one per device."""
        if not recording_ids:
            return {}
        with self._conn() as conn:
            placeholders = ",".join("?" for _ in recording_ids)
            rows = conn.execute(
                f"SELECT id, name FROM replay_recordings WHERE id IN ({placeholders})",
                recording_ids,
            ).fetchall()
            return {int(r["id"]): r["name"] for r in rows}

    def update_replay_recording(self, recording_id: int, data: dict) -> Optional[dict]:
        """Editable regardless of status -- unlike the points list (fixed at
        create time, see create_replay_recording's own docstring), name/
        description/interval/cap/buffer_mode are just operational config and
        don't affect any sample already stored. A raised maximum_samples
        just gives more headroom, taking effect on the next sample same as
        sample_interval_seconds. A LOWERED maximum_samples is applied
        immediately, though: trimming down to the new cap right here
        (oldest sample_index first, same eviction add_replay_sample already
        uses) rather than waiting for it to lazily converge one sample at a
        time as new snapshots arrive -- a user editing the cap down expects
        the count they see to reflect it right away, not on the next tick.
        A 'stop'-mode recording already at/over the new lower cap is
        completed immediately for the same reason, instead of waiting for
        the next sample attempt to discover it's full."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE replay_recordings SET name=:name, description=:description, "
                "sample_interval_seconds=:sample_interval_seconds, maximum_samples=:maximum_samples, "
                "buffer_mode=:buffer_mode WHERE id=:id",
                {**data, "id": recording_id},
            )
            row = conn.execute(
                "SELECT status, buffer_mode, maximum_samples FROM replay_recordings WHERE id=?",
                (recording_id,),
            ).fetchone()
            if row:
                sample_count = conn.execute(
                    "SELECT COUNT(DISTINCT sample_index) FROM replay_samples WHERE recording_id=?",
                    (recording_id,),
                ).fetchone()[0]
                excess = sample_count - row["maximum_samples"]
                if excess > 0:
                    if row["buffer_mode"] == "overwrite":
                        for _ in range(excess):
                            conn.execute(
                                "DELETE FROM replay_samples WHERE recording_id=? AND sample_index = ("
                                "SELECT MIN(sample_index) FROM replay_samples WHERE recording_id=?)",
                                (recording_id, recording_id),
                            )
                    elif row["status"] == "recording":
                        conn.execute(
                            "UPDATE replay_recordings SET status='completed', ended_at=datetime('now') WHERE id=?",
                            (recording_id,),
                        )
            conn.commit()
        return self.get_replay_recording(recording_id)

    def add_replay_sample(self, recording_id: int, values: dict[int, dict]) -> Optional[int]:
        """Appends one snapshot -- every entry in `values` (keyed by
        recording_point_id) shares the same sample_index and timestamp,
        enforcing "all point values captured during the same polling cycle
        use the same sample_index and timestamp." Enforces the
        maximum_samples/buffer_mode semantics on whole snapshots (distinct
        sample_index values), not individual rows. Returns the new
        sample_index, or None if the recording isn't active or is full with
        buffer_mode='stop' (in which case it's also auto-completed here)."""
        with self._conn() as conn:
            cfg = conn.execute("SELECT * FROM replay_recordings WHERE id=?", (recording_id,)).fetchone()
            if not cfg or cfg["status"] != "recording":
                return None
            sample_count = conn.execute(
                "SELECT COUNT(DISTINCT sample_index) FROM replay_samples WHERE recording_id=?", (recording_id,)
            ).fetchone()[0]
            if sample_count >= cfg["maximum_samples"]:
                if cfg["buffer_mode"] == "stop":
                    conn.execute(
                        "UPDATE replay_recordings SET status='completed', ended_at=datetime('now') WHERE id=?",
                        (recording_id,),
                    )
                    conn.commit()
                    return None
                conn.execute(
                    "DELETE FROM replay_samples WHERE recording_id=? AND sample_index = ("
                    "SELECT MIN(sample_index) FROM replay_samples WHERE recording_id=?)",
                    (recording_id, recording_id),
                )
            next_index_row = conn.execute(
                "SELECT MAX(sample_index) FROM replay_samples WHERE recording_id=?", (recording_id,)
            ).fetchone()
            next_index = (next_index_row[0] + 1) if next_index_row[0] is not None else 0
            timestamp = datetime.now(timezone.utc).isoformat()
            for recording_point_id, entry in values.items():
                conn.execute(
                    "INSERT INTO replay_samples "
                    "(recording_id, recording_point_id, sample_index, timestamp, value, reliability, out_of_service) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        recording_id, recording_point_id, next_index, timestamp,
                        json.dumps(entry.get("value")), entry.get("reliability"),
                        1 if entry.get("out_of_service") else 0,
                    ),
                )
            conn.commit()
            return next_index

    def get_replay_recording_sample_index_bounds(self, recording_id: int) -> Optional[tuple[int, int]]:
        """(min, max) sample_index currently stored -- eviction (buffer_mode=
        'overwrite') only ever removes the single lowest sample_index, so
        the remaining range is always contiguous (no internal gaps),
        letting playback step through it with a plain +1 rather than
        re-querying the full distinct-index list every frame. None if the
        recording has no samples yet."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MIN(sample_index), MAX(sample_index) FROM replay_samples WHERE recording_id=?",
                (recording_id,),
            ).fetchone()
            if row[0] is None:
                return None
            return (row[0], row[1])

    def get_replay_recording_samples(self, recording_id: int, sample_index: int) -> list[dict]:
        """All point rows for one snapshot -- used by playback, one call per
        advanced frame."""
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM replay_samples WHERE recording_id=? AND sample_index=?",
                (recording_id, sample_index),
            )]

    def get_replay_recording_all_samples(self, recording_id: int) -> list[dict]:
        """Every sample this recording has, long-format (one row per
        sample_index x point), ordered by sample_index then
        recording_point_id, `value` JSON-decoded back to its original type.
        Internal -- used only by the calibration dataset builder
        (src/simulation/calibration_export.py) to pivot into a wide CSV; not
        exposed as its own HTTP route (no raw-sample-browsing UI yet)."""
        with self._conn() as conn:
            rows = [
                dict(r) for r in conn.execute(
                    "SELECT s.sample_index, s.timestamp, s.recording_point_id, "
                    "p.object_type, p.object_instance, p.object_name, p.point_type, p.units, "
                    "s.value, s.reliability, s.out_of_service "
                    "FROM replay_samples s "
                    "JOIN replay_recording_points p ON p.id = s.recording_point_id "
                    "WHERE s.recording_id=? "
                    "ORDER BY s.sample_index, s.recording_point_id",
                    (recording_id,),
                )
            ]
            for row in rows:
                row["value"] = json.loads(row["value"])
            return rows

    def has_replayable_recording(self, device_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM replay_recordings r WHERE r.source_device_id=? "
                "AND r.status='completed' AND (SELECT COUNT(*) FROM replay_samples WHERE recording_id=r.id) > 0)",
                (device_id,),
            ).fetchone()
            return bool(row[0])

    # ── BACnet Schedules ─────────────────────────────────────────────────────────
    # Note: unlike alarms/trend logs, schedules don't need per-tick evaluation —
    # bacnet_schedule.LocalScheduleObject (built on bacpypes3's own
    # ScheduleObject) self-schedules its own next transition via asyncio, so
    # the engine only needs to (re)construct them on start()/reload().

    def get_schedules(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute(
                    "SELECT * FROM bacnet_schedules WHERE device_id=? ORDER BY name", (device_id,)
                )
            else:
                rows = conn.execute("SELECT * FROM bacnet_schedules ORDER BY device_id, name")
            return [dict(r) for r in rows]

    def get_schedule(self, schedule_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM bacnet_schedules WHERE id=?", (schedule_id,)).fetchone()
            return dict(r) if r else None

    def get_schedule_targets(self, schedule_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT t.*, o.object_type, o.object_instance, o.name AS object_name "
                "FROM bacnet_schedule_targets t JOIN objects o ON o.id = t.object_id "
                "WHERE t.schedule_id=?",
                (schedule_id,),
            )
            return [dict(r) for r in rows]

    def _set_schedule_targets(self, conn: sqlite3.Connection, schedule_id: int, targets: list[dict]) -> None:
        conn.execute("DELETE FROM bacnet_schedule_targets WHERE schedule_id=?", (schedule_id,))
        for t in targets:
            conn.execute(
                "INSERT INTO bacnet_schedule_targets (schedule_id, object_id, property_identifier) "
                "VALUES (?, ?, ?)",
                (schedule_id, t["object_id"], t.get("property_identifier", "present-value")),
            )

    def create_schedule(self, device_id: int, data: dict, targets: list[dict]) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO bacnet_schedules "
                "(device_id, name, description, value_type, schedule_default, effective_start, "
                "effective_end, weekly_schedule, exception_schedule, priority_for_writing, enabled) "
                "VALUES (:device_id, :name, :description, :value_type, :schedule_default, :effective_start, "
                ":effective_end, :weekly_schedule, :exception_schedule, :priority_for_writing, :enabled)",
                {**data, "device_id": device_id},
            )
            schedule_id = cur.lastrowid
            self._set_schedule_targets(conn, schedule_id, targets)
            conn.commit()
            return dict(conn.execute("SELECT * FROM bacnet_schedules WHERE id=?", (schedule_id,)).fetchone())

    def update_schedule(self, schedule_id: int, data: dict, targets: list[dict]) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE bacnet_schedules SET name=:name, description=:description, "
                "value_type=:value_type, schedule_default=:schedule_default, "
                "effective_start=:effective_start, effective_end=:effective_end, "
                "weekly_schedule=:weekly_schedule, exception_schedule=:exception_schedule, "
                "priority_for_writing=:priority_for_writing, enabled=:enabled WHERE id=:id",
                {**data, "id": schedule_id},
            )
            self._set_schedule_targets(conn, schedule_id, targets)
            conn.commit()
            r = conn.execute("SELECT * FROM bacnet_schedules WHERE id=?", (schedule_id,)).fetchone()
            return dict(r) if r else None

    def set_schedule_enabled(self, schedule_id: int, enabled: bool) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE bacnet_schedules SET enabled=? WHERE id=?", (1 if enabled else 0, schedule_id)
            )
            conn.commit()
            r = conn.execute("SELECT * FROM bacnet_schedules WHERE id=?", (schedule_id,)).fetchone()
            return dict(r) if r else None

    def delete_schedule(self, schedule_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM bacnet_schedules WHERE id=?", (schedule_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── BACnet Calendars (GH #18) ────────────────────────────────────────────────
    # Referenced by name from a Schedule's exception_schedule JSON (see
    # bacnet_schedule.build_exception_schedule) rather than by a DB foreign key —
    # that keeps the reference portable across project save/load the same way
    # object_type+object_instance already does for schedule targets.

    def get_calendars(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute(
                    "SELECT * FROM bacnet_calendars WHERE device_id=? ORDER BY name", (device_id,)
                )
            else:
                rows = conn.execute("SELECT * FROM bacnet_calendars ORDER BY device_id, name")
            return [dict(r) for r in rows]

    def get_calendar(self, calendar_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM bacnet_calendars WHERE id=?", (calendar_id,)).fetchone()
            return dict(r) if r else None

    def create_calendar(self, device_id: int, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO bacnet_calendars (device_id, name, description, date_list, enabled) "
                "VALUES (:device_id, :name, :description, :date_list, :enabled)",
                {**data, "device_id": device_id},
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM bacnet_calendars WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_calendar(self, calendar_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE bacnet_calendars SET name=:name, description=:description, "
                "date_list=:date_list, enabled=:enabled WHERE id=:id",
                {**data, "id": calendar_id},
            )
            conn.commit()
            r = conn.execute("SELECT * FROM bacnet_calendars WHERE id=?", (calendar_id,)).fetchone()
            return dict(r) if r else None

    def delete_calendar(self, calendar_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM bacnet_calendars WHERE id=?", (calendar_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Projects ──────────────────────────────────────────────────────────────
    # Persisted as "profiles" in the DB schema (table name unchanged, see
    # CREATE TABLE above) — only the Python-level naming was renamed to
    # "project" to match the admin UI ("New Project"/"Open Project"/"Save").

    def get_projects(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles ORDER BY created_at DESC"
            )]

    def get_project(self, project_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM profiles WHERE id=?", (project_id,)).fetchone()
            return dict(r) if r else None

    @staticmethod
    def _normalize_discovery_connections(
        connections: Optional[list[dict]],
        legacy_connection_config: Optional[dict] = None,
    ) -> list[dict]:
        normalized: list[dict] = []
        next_id = 1
        for raw in connections or []:
            target = raw.get("target", raw.get("discovery_target"))
            if target == "":
                target = None
            try:
                conn_id = int(raw.get("id", next_id))
            except (TypeError, ValueError):
                conn_id = next_id
            next_id = max(next_id, conn_id + 1)
            name = str(raw.get("name") or target or "Local BACnet").strip() or "Local BACnet"
            normalized.append({
                "id": conn_id,
                "name": name,
                "target": target,
                "device_instance_low": int(raw.get("device_instance_low", 0)),
                "device_instance_high": int(raw.get("device_instance_high", 4194303)),
                "timeout_ms": int(raw.get("timeout_ms", 5000)),
                "enabled": bool(raw.get("enabled", True)),
            })

        if not normalized and legacy_connection_config:
            target = legacy_connection_config.get("target", legacy_connection_config.get("discovery_target"))
            if target == "":
                target = None
            normalized.append({
                "id": 1,
                "name": str(target or "Local BACnet"),
                "target": target,
                "device_instance_low": int(legacy_connection_config.get("device_instance_low", 0)),
                "device_instance_high": int(legacy_connection_config.get("device_instance_high", 4194303)),
                "timeout_ms": int(legacy_connection_config.get("timeout_ms", 5000)),
                "enabled": True,
            })

        return normalized

    def _get_project_payload(self, conn: sqlite3.Connection, project_id: int) -> Optional[dict]:
        row = conn.execute("SELECT data FROM profiles WHERE id=?", (project_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row["data"])

    def _write_project_payload(self, conn: sqlite3.Connection, project_id: int, payload: dict) -> bool:
        cur = conn.execute(
            "UPDATE profiles SET data=? WHERE id=?",
            (json.dumps(payload), project_id),
        )
        return cur.rowcount > 0

    def get_project_discovery_connections(self, project_id: int) -> Optional[list[dict]]:
        with self._conn() as conn:
            payload = self._get_project_payload(conn, project_id)
            if payload is None:
                return None
            connections = self._normalize_discovery_connections(
                payload.get("discovery_connections"),
                payload.get("connection_config"),
            )
            payload["discovery_connections"] = connections
            if connections and not payload.get("connection_config"):
                first = connections[0]
                payload["connection_config"] = {
                    "discovery_target": first["target"],
                    "device_instance_low": first["device_instance_low"],
                    "device_instance_high": first["device_instance_high"],
                    "timeout_ms": first["timeout_ms"],
                }
            self._write_project_payload(conn, project_id, payload)
            conn.commit()
            return connections

    def create_project_discovery_connection(self, project_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            payload = self._get_project_payload(conn, project_id)
            if payload is None:
                return None
            connections = self._normalize_discovery_connections(
                payload.get("discovery_connections"),
                payload.get("connection_config"),
            )
            next_id = max((c["id"] for c in connections), default=0) + 1
            connection = self._normalize_discovery_connections([{**data, "id": next_id}])[0]
            connections.append(connection)
            payload["discovery_connections"] = connections
            self._write_project_payload(conn, project_id, payload)
            conn.commit()
            return connection

    def update_project_discovery_connection(
        self,
        project_id: int,
        connection_id: int,
        data: dict,
    ) -> Optional[dict]:
        with self._conn() as conn:
            payload = self._get_project_payload(conn, project_id)
            if payload is None:
                return None
            connections = self._normalize_discovery_connections(
                payload.get("discovery_connections"),
                payload.get("connection_config"),
            )
            updated = self._normalize_discovery_connections([{**data, "id": connection_id}])[0]
            found = False
            next_connections = []
            for connection in connections:
                if connection["id"] == connection_id:
                    next_connections.append(updated)
                    found = True
                else:
                    next_connections.append(connection)
            if not found:
                return {}
            payload["discovery_connections"] = next_connections
            self._write_project_payload(conn, project_id, payload)
            conn.commit()
            return updated

    def delete_project_discovery_connection(self, project_id: int, connection_id: int) -> Optional[bool]:
        with self._conn() as conn:
            payload = self._get_project_payload(conn, project_id)
            if payload is None:
                return None
            connections = self._normalize_discovery_connections(
                payload.get("discovery_connections"),
                payload.get("connection_config"),
            )
            next_connections = [c for c in connections if c["id"] != connection_id]
            if len(next_connections) == len(connections):
                return False
            payload["discovery_connections"] = next_connections
            if not next_connections:
                payload["connection_config"] = None
            self._write_project_payload(conn, project_id, payload)
            conn.commit()
            return True

    # ── Fault Detection  ──────────────────────────────────────────────────────────────
    def get_fault_rule_configs(
    self,
    device_id: int,
        ) -> list[dict]:
            with self._conn() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM fault_rule_configs
                    WHERE device_id = ?
                    ORDER BY rule_id
                    """,
                    (device_id,),
                )

                return [
                    dict(row)
                    for row in rows
                ]


    def upsert_fault_rule_config(
        self,
        device_id: int,
        rule_id: str,
        data: dict,
    ) -> dict:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO fault_rule_configs (
                    device_id,
                    rule_id,
                    enabled,
                    parameters,
                    persistence_seconds,
                    clear_seconds,
                    severity
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, rule_id)
                DO UPDATE SET
                    enabled = excluded.enabled,
                    parameters = excluded.parameters,
                    persistence_seconds =
                        excluded.persistence_seconds,
                    clear_seconds =
                        excluded.clear_seconds,
                    severity = excluded.severity
                """,
                (
                    device_id,
                    rule_id,
                    int(bool(data.get("enabled", True))),
                    data.get("parameters", "{}"),
                    data.get("persistence_seconds"),
                    data.get("clear_seconds"),
                    data.get("severity"),
                ),
            )

            conn.commit()

            row = conn.execute(
                """
                SELECT *
                FROM fault_rule_configs
                WHERE device_id = ?
                AND rule_id = ?
                """,
                (
                    device_id,
                    rule_id,
                ),
            ).fetchone()

            if row is None:
                raise RuntimeError(
                    "Fault rule configuration was not saved"
                )

            return dict(row)


    def record_fault_evaluation(
        self,
        data: dict,
    ) -> dict:
        with self._conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO fault_events (
                    device_id,
                    rule_id,
                    state,
                    previous_state,
                    severity,
                    message,
                    evidence,
                    timestamp,
                    activated_at,
                    cleared_at
                )
                VALUES (
                    :device_id,
                    :rule_id,
                    :state,
                    :previous_state,
                    :severity,
                    :message,
                    :evidence,
                    :timestamp,
                    :activated_at,
                    :cleared_at
                )
                """,
                data,
            )

            conn.commit()

            row = conn.execute(
                """
                SELECT *
                FROM fault_events
                WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()

            if row is None:
                raise RuntimeError(
                    "Fault event was not recorded"
                )

            return dict(row)


    def get_fault_events(
        self,
        *,
        device_id: int | None = None,
        active_only: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        query = """
            SELECT *
            FROM fault_events
        """

        clauses: list[str] = []
        values: list = []

        if device_id is not None:
            clauses.append(
                "device_id = ?"
            )
            values.append(device_id)

        if active_only:
            clauses.append(
                "state = 'active'"
            )

        if clauses:
            query += (
                " WHERE "
                + " AND ".join(clauses)
            )

        query += """
            ORDER BY id DESC
            LIMIT ?
        """

        values.append(limit)

        with self._conn() as conn:
            rows = conn.execute(
                query,
                values,
            )

            return [
                dict(row)
                for row in rows
            ]

    @staticmethod
    def _bulk_attach_project_snapshot_children(conn: sqlite3.Connection, devices: list[dict]) -> None:
        """Batched replacement for what used to be a per-device loop calling
        an objects SELECT + _attach_trend_logs + _attach_schedules +
        _attach_calendars once EACH -- for a project with N devices, that
        was 4+ round trips per device (O(N), plus a further nested query
        per trend-log/schedule-target row) purely from save_project()/
        update_project() being called, which is to say every single click
        of Save. Collapses that down to a small constant number of bulk
        `WHERE device_id IN (...)` queries, then groups results back onto
        each device dict in Python -- a device with zero trend
        logs/schedules/calendars (the common case) now costs nothing extra
        instead of three empty-result queries. Preserves the exact
        snapshot semantics the old per-device helpers had: trend logs and
        schedule targets still get their live object id replaced with a
        portable (object_type, object_instance) reference (object ids are
        reassigned on load, see also load_project()); a missing/orphaned
        object reference still becomes monitored_object_ref=None (trend
        logs) or a dropped target (schedules); calendars are untouched
        beyond stripping id/device_id, same as before."""
        if not devices:
            return

        device_ids = [d["id"] for d in devices]
        placeholders = ",".join("?" * len(device_ids))

        objects_by_device: dict[int, list[dict]] = {did: [] for did in device_ids}
        object_ref_by_id: dict[int, dict] = {}
        for r in conn.execute(
            f"SELECT * FROM objects WHERE device_id IN ({placeholders}) "
            "ORDER BY device_id, object_type, object_instance",
            device_ids,
        ):
            row = dict(r)
            objects_by_device[row["device_id"]].append(row)
            object_ref_by_id[row["id"]] = {
                "object_type": row["object_type"],
                "object_instance": row["object_instance"],
            }
        for dev in devices:
            dev["objects"] = objects_by_device[dev["id"]]

        trend_logs_by_device: dict[int, list[dict]] = {did: [] for did in device_ids}
        for r in conn.execute(
            f"SELECT * FROM trend_logs WHERE device_id IN ({placeholders})",
            device_ids,
        ):
            tl = dict(r)
            device_id = tl["device_id"]
            tl["monitored_object_ref"] = object_ref_by_id.get(tl.pop("monitored_object_id"))
            tl.pop("id", None)
            tl.pop("device_id", None)
            # Historical records aren't part of a project snapshot, only config.
            tl.pop("record_count", None)
            tl.pop("total_record_count", None)
            tl.pop("last_sampled_at", None)
            trend_logs_by_device[device_id].append(tl)
        for dev in devices:
            dev["trend_logs"] = trend_logs_by_device[dev["id"]]

        schedules_by_device: dict[int, list[dict]] = {did: [] for did in device_ids}
        schedule_rows_by_id: dict[int, dict] = {}
        schedule_device_by_id: dict[int, int] = {}
        for r in conn.execute(
            f"SELECT * FROM bacnet_schedules WHERE device_id IN ({placeholders})",
            device_ids,
        ):
            sched = dict(r)
            schedule_rows_by_id[sched["id"]] = sched
            schedule_device_by_id[sched["id"]] = sched["device_id"]

        targets_by_schedule: dict[int, list[dict]] = {sid: [] for sid in schedule_rows_by_id}
        if schedule_rows_by_id:
            schedule_ids = list(schedule_rows_by_id.keys())
            sched_placeholders = ",".join("?" * len(schedule_ids))
            for r in conn.execute(
                "SELECT schedule_id, object_id, property_identifier FROM bacnet_schedule_targets "
                f"WHERE schedule_id IN ({sched_placeholders})",
                schedule_ids,
            ):
                t = dict(r)
                mon = object_ref_by_id.get(t["object_id"])
                if mon:
                    targets_by_schedule[t["schedule_id"]].append({
                        "object_type": mon["object_type"],
                        "object_instance": mon["object_instance"],
                        "property_identifier": t["property_identifier"],
                    })

        for sid, sched in schedule_rows_by_id.items():
            sched["targets"] = targets_by_schedule[sid]
            sched.pop("id", None)
            sched.pop("device_id", None)
            schedules_by_device[schedule_device_by_id[sid]].append(sched)
        for dev in devices:
            dev["schedules"] = schedules_by_device[dev["id"]]

        calendars_by_device: dict[int, list[dict]] = {did: [] for did in device_ids}
        for r in conn.execute(
            f"SELECT * FROM bacnet_calendars WHERE device_id IN ({placeholders})",
            device_ids,
        ):
            cal = dict(r)
            device_id = cal["device_id"]
            cal.pop("id", None)
            cal.pop("device_id", None)
            calendars_by_device[device_id].append(cal)
        for dev in devices:
            dev["calendars"] = calendars_by_device[dev["id"]]

    # ─── Functional tests ──────────────────────────────────────────────────
    # Project-level data, independent of any single device/object/location
    # (identified only by equipment_type, a plain validated string) --
    # included verbatim in save_project/load_project below, no id-remapping
    # needed since nothing references a functional_tests row and it
    # references nothing itself.

    def create_functional_test(self, data: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO functional_tests (name, description, equipment_type, definition_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (data["name"], data.get("description", ""), data["equipment_type"], json.dumps(data["definition"]), now, now),
            )
            conn.commit()
            return self._functional_test_row(conn, cur.lastrowid)

    def get_functional_tests(self) -> list[dict]:
        with self._conn() as conn:
            return [
                self._functional_test_dict(r)
                for r in conn.execute("SELECT * FROM functional_tests ORDER BY name")
            ]

    def get_functional_test(self, test_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM functional_tests WHERE id=?", (test_id,)).fetchone()
            return self._functional_test_dict(row) if row else None

    def update_functional_test(self, test_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE functional_tests SET name=?, description=?, equipment_type=?, definition_json=?, updated_at=? WHERE id=?",
                (data["name"], data.get("description", ""), data["equipment_type"], json.dumps(data["definition"]),
                 datetime.now(timezone.utc).isoformat(), test_id),
            )
            if cur.rowcount == 0:
                conn.commit()
                return None
            conn.commit()
            return self._functional_test_row(conn, test_id)

    def delete_functional_test(self, test_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM functional_tests WHERE id=?", (test_id,))
            conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _functional_test_row(conn: sqlite3.Connection, test_id: int) -> dict:
        row = conn.execute("SELECT * FROM functional_tests WHERE id=?", (test_id,)).fetchone()
        return Database._functional_test_dict(row)

    @staticmethod
    def _functional_test_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["definition"] = json.loads(d.pop("definition_json"))
        return d

    # ─── Custom graphs ───────────────────────────────────────────────────
    # Project-level data, mirroring functional_tests exactly: a named
    # JSON-blob definition (device_id/object_id per series -- opaque
    # references, not FKs, resolved live against /points whenever a graph
    # is opened) with no relational table of its own and no id-remapping
    # needed on restore, included verbatim in save_project/load_project
    # below the same way functional_tests already is.

    def create_custom_graph(self, data: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO custom_graphs (name, definition_json, created_at, updated_at) "
                "VALUES (?,?,?,?)",
                (data["name"], json.dumps(data["definition"]), now, now),
            )
            conn.commit()
            return self._custom_graph_row(conn, cur.lastrowid)

    def get_custom_graphs(self) -> list[dict]:
        with self._conn() as conn:
            return [
                self._custom_graph_dict(r)
                for r in conn.execute("SELECT * FROM custom_graphs ORDER BY name")
            ]

    def get_custom_graph(self, graph_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM custom_graphs WHERE id=?", (graph_id,)).fetchone()
            return self._custom_graph_dict(row) if row else None

    def update_custom_graph(self, graph_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE custom_graphs SET name=?, definition_json=?, updated_at=? WHERE id=?",
                (data["name"], json.dumps(data["definition"]), datetime.now(timezone.utc).isoformat(), graph_id),
            )
            if cur.rowcount == 0:
                conn.commit()
                return None
            conn.commit()
            return self._custom_graph_row(conn, graph_id)

    def delete_custom_graph(self, graph_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM custom_graphs WHERE id=?", (graph_id,))
            conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _custom_graph_row(conn: sqlite3.Connection, graph_id: int) -> dict:
        row = conn.execute("SELECT * FROM custom_graphs WHERE id=?", (graph_id,)).fetchone()
        return Database._custom_graph_dict(row)

    @staticmethod
    def _custom_graph_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["definition"] = json.loads(d.pop("definition_json"))
        return d

    # ── Functional test runs (execution state -- not project data, see
    # save_project/load_project below: deliberately not included in the
    # project JSON blob, and ON DELETE CASCADE on both FKs means a project
    # reload's DELETE FROM functional_tests / device wipe already cleans
    # these up with no extra code needed here). ──────────────────────────

    _FUNCTIONAL_TEST_RUN_UPDATABLE_FIELDS = {
        "state", "started_at", "finished_at", "result", "result_message",
        "current_node_id", "error",
    }

    def create_functional_test_run(self, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO functional_test_runs (functional_test_id, execution_mode) "
                "VALUES (?,?)",
                (data["functional_test_id"], data["execution_mode"]),
            )
            conn.commit()
            return self._functional_test_run_row(conn, cur.lastrowid)

    def get_functional_test_run(self, run_id: int) -> Optional[dict]:
        with self._conn() as conn:
            return self._functional_test_run_row(conn, run_id)

    def find_active_functional_test_run(self, functional_test_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM functional_test_runs WHERE functional_test_id=? "
                "AND state IN ('pending','running') ORDER BY id DESC LIMIT 1",
                (functional_test_id,),
            ).fetchone()
            return self._functional_test_run_dict(row) if row else None

    def update_functional_test_run(self, run_id: int, **fields) -> Optional[dict]:
        updates = {k: v for k, v in fields.items() if k in self._FUNCTIONAL_TEST_RUN_UPDATABLE_FIELDS}
        if not updates:
            with self._conn() as conn:
                return self._functional_test_run_row(conn, run_id)
        set_clause = ", ".join(f"{k}=?" for k in updates)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE functional_test_runs SET {set_clause} WHERE id=?",
                (*updates.values(), run_id),
            )
            conn.commit()
            return self._functional_test_run_row(conn, run_id)

    def append_functional_test_run_detail(self, run_id: int, node_id: str, entry: dict) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT details_json FROM functional_test_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                return
            details = json.loads(row["details_json"] or "[]")
            details.append(entry)
            conn.execute(
                "UPDATE functional_test_runs SET details_json=?, current_node_id=? WHERE id=?",
                (json.dumps(details), node_id, run_id),
            )
            conn.commit()

    @staticmethod
    def _functional_test_run_row(conn: sqlite3.Connection, run_id: int) -> Optional[dict]:
        row = conn.execute("SELECT * FROM functional_test_runs WHERE id=?", (run_id,)).fetchone()
        return Database._functional_test_run_dict(row) if row else None

    @staticmethod
    def _functional_test_run_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["details"] = json.loads(d.pop("details_json") or "[]")
        return d

    def save_project(
        self,
        name: str,
        description: str,
        connection_config: Optional[dict] = None,
        discovery_connections: Optional[list[dict]] = None,
    ) -> dict:
        # simulation_model_* tables are created lazily (see
        # ensure_simulation_model_schema's own docstring), not by setup()'s
        # own executescript -- must be ensured before the SELECT below on a
        # database where no simulation model has ever been created yet.
        ensure_simulation_model_schema(self)
        with self._conn() as conn:
            discovery_connections = self._normalize_discovery_connections(
                discovery_connections,
                connection_config,
            )
            locations = [dict(r) for r in conn.execute("SELECT * FROM locations")]
            equipment = [dict(r) for r in conn.execute("SELECT * FROM equipment")]
            devices = [dict(r) for r in conn.execute(
                "SELECT * FROM devices ORDER BY device_instance"
            )]
            self._bulk_attach_project_snapshot_children(conn, devices)
            # Brick Core: stored verbatim, including the (soon-to-be-stale)
            # id/semantic_key values -- load_project() remaps ids and
            # recomputes semantic_key from the newly-assigned ones rather
            # than trusting what's stored here. See load_project()'s
            # comment for why semantic_key can't just be copied across.
            semantic_entities = [dict(r) for r in conn.execute("SELECT * FROM semantic_entities")]
            semantic_relationships = [dict(r) for r in conn.execute("SELECT * FROM semantic_relationships")]
            # Stored verbatim, including the (soon-to-be-stale) id/device_id
            # values -- load_project() remaps device_id through dev_id_map
            # the same way it already does for semantic_entities, rather
            # than trusting the stored id directly.
            energy_model_configs = [dict(r) for r in conn.execute("SELECT * FROM energy_model_configs")]
            # No id-remapping needed on restore -- functional_tests has no FK
            # to device/object/location at all (see create_functional_test's
            # own comment), so this is stored/restored verbatim like the
            # other blob fields but with none of their remap bookkeeping.
            functional_tests = [dict(r) for r in conn.execute("SELECT * FROM functional_tests")]
            # Same "no FK, store/restore verbatim" reasoning as
            # functional_tests immediately above.
            custom_graphs = [dict(r) for r in conn.execute("SELECT * FROM custom_graphs")]
            # Stored verbatim, including the (soon-to-be-stale) id/
            # created_from_device_id/mapping point_id values -- load_project()
            # remaps these through dev_id_map/global_obj_id_map, the same
            # pattern already used for energy_model_configs/semantic_entities
            # above. Previously MISSING from this snapshot entirely -- a
            # saved/reloaded project silently lost every simulation model
            # (FMU provider configs, point mappings, aggregate mappings)
            # even though the devices/objects they were attached to came
            # back fine. list_all_simulation_models reuses the same
            # assembly logic the /simulation/models API already returns
            # (mappings + aggregate_mappings merged, matching
            # model_runtime._is_aggregate_row's own "point_ids" discriminator).
            simulation_models = list_all_simulation_models(conn)
            data = json.dumps({
                "locations": locations,
                "equipment": equipment,
                "devices": devices,
                "semantic_entities": semantic_entities,
                "semantic_relationships": semantic_relationships,
                "energy_model_configs": energy_model_configs,
                "functional_tests": functional_tests,
                "custom_graphs": custom_graphs,
                "simulation_models": simulation_models,
                "connection_config": connection_config,
                "discovery_connections": discovery_connections,
            })
            cur = conn.execute(
                "INSERT INTO profiles (name, description, device_count, data) VALUES (?,?,?,?)",
                (name, description, len(devices), data),
            )
            conn.commit()
            row = dict(conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles WHERE id=?",
                (cur.lastrowid,),
            ).fetchone())
            row["connection_config"] = connection_config
            row["discovery_connections"] = discovery_connections
            return row

    def update_project(
        self,
        project_id: int,
        name: str,
        description: str,
        connection_config: Optional[dict] = None,
        discovery_connections: Optional[list[dict]] = None,
        connection_config_provided: bool = False,
        discovery_connections_provided: bool = False,
    ) -> bool:
        ensure_simulation_model_schema(self)
        with self._conn() as conn:
            # Discovery connection settings aren't part of live device/location
            # state -- they only exist in this row's own stored data blob. An
            # omitted field means "leave it as-is"; an explicit empty list means
            # "forget every saved discovery connection".
            existing = conn.execute(
                "SELECT data FROM profiles WHERE id=?", (project_id,)
            ).fetchone()
            existing_payload = json.loads(existing["data"]) if existing else {}
            if not connection_config_provided:
                connection_config = existing_payload.get("connection_config")
            if not discovery_connections_provided:
                discovery_connections = existing_payload.get("discovery_connections", [])
            discovery_connections = self._normalize_discovery_connections(
                discovery_connections,
                connection_config,
            )

            locations = [dict(r) for r in conn.execute("SELECT * FROM locations")]
            equipment = [dict(r) for r in conn.execute("SELECT * FROM equipment")]
            devices = [dict(r) for r in conn.execute(
                "SELECT * FROM devices ORDER BY device_instance"
            )]
            self._bulk_attach_project_snapshot_children(conn, devices)
            semantic_entities = [dict(r) for r in conn.execute("SELECT * FROM semantic_entities")]
            semantic_relationships = [dict(r) for r in conn.execute("SELECT * FROM semantic_relationships")]
            energy_model_configs = [dict(r) for r in conn.execute("SELECT * FROM energy_model_configs")]
            functional_tests = [dict(r) for r in conn.execute("SELECT * FROM functional_tests")]
            custom_graphs = [dict(r) for r in conn.execute("SELECT * FROM custom_graphs")]
            # See save_project's identical line for why this exists --
            # previously missing entirely, silently losing every simulation
            # model on save/reload.
            simulation_models = list_all_simulation_models(conn)
            data = json.dumps({
                "locations": locations,
                "equipment": equipment,
                "devices": devices,
                "semantic_entities": semantic_entities,
                "semantic_relationships": semantic_relationships,
                "energy_model_configs": energy_model_configs,
                "functional_tests": functional_tests,
                "custom_graphs": custom_graphs,
                "simulation_models": simulation_models,
                "connection_config": connection_config,
                "discovery_connections": discovery_connections,
            })
            cur = conn.execute(
                "UPDATE profiles SET name=?, description=?, device_count=?, data=? WHERE id=?",
                (name, description, len(devices), data, project_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def clear_live_state(self) -> None:
        """Wipes all live project state back to blank -- the exact same
        sequence load_project() uses to clear the slate before restoring a
        saved snapshot (see that method's own comment for why semantic
        tables must be cleared before devices/locations, not via cascade:
        semantic_entities.location_id has no ON DELETE CASCADE, so an
        unguarded location delete can be permanently blocked by a leftover
        semantic entity). Used by the "New Project" reset flow so it can't
        leave orphaned locations/semantic entities behind the way a pile of
        individual per-row API deletes could."""
        ensure_simulation_model_schema(self)
        with self._conn() as conn:
            # Must run BEFORE devices, not just before locations: a point
            # that's a simulation_model_aggregate_members row (max or
            # weighted_average) is ON DELETE RESTRICT (see
            # model_store.ensure_simulation_model_schema's own comment on
            # that table), so DELETE FROM devices below would otherwise
            # cascade into deleting that point's object row and hit a
            # foreign-key-constraint failure -- deleting the model config
            # first cascades away its members before devices/objects are
            # ever touched.
            conn.execute("DELETE FROM simulation_model_configs")
            conn.execute("DELETE FROM semantic_relationships")
            conn.execute("DELETE FROM semantic_entities")
            conn.execute("DELETE FROM devices")
            conn.execute("DELETE FROM equipment")
            conn.execute("DELETE FROM locations")
            conn.execute("DELETE FROM functional_tests")
            conn.execute("DELETE FROM custom_graphs")
            conn.commit()

    def load_project(self, project_id: int) -> Optional[dict]:
        ensure_simulation_model_schema(self)
        with self._conn() as conn:
            row = conn.execute("SELECT data FROM profiles WHERE id=?", (project_id,)).fetchone()
            if not row:
                return None
            payload = json.loads(row["data"])
            # Wipe semantic tables BEFORE devices/locations, not after and
            # not via cascade: semantic_entities.location_id has no
            # ON DELETE CASCADE (locations deletion normally goes through
            # delete_location()'s guard), so an unguarded DELETE FROM
            # locations below would otherwise hit a foreign-key-constraint
            # failure if any leftover semantic_entities row still pointed
            # at one of those locations. This also guarantees no stale rows
            # survive to collide with the semantic_key values recomputed
            # below once ids are reassigned.
            #
            # simulation_model_configs must be wiped before devices for the
            # identical reason (see clear_live_state's own comment): a
            # simulation_model_aggregate_members row is ON DELETE RESTRICT
            # on its point_id/weight_point_id, so DELETE FROM devices below
            # would otherwise fail outright the moment it cascades into
            # deleting an object that's still an aggregate member.
            conn.execute("DELETE FROM simulation_model_configs")
            conn.execute("DELETE FROM semantic_relationships")
            conn.execute("DELETE FROM semantic_entities")
            conn.execute("DELETE FROM devices")
            conn.execute("DELETE FROM equipment")
            conn.execute("DELETE FROM locations")
            conn.execute("DELETE FROM functional_tests")
            conn.execute("DELETE FROM custom_graphs")
            conn.commit()

            # Restore locations parent-before-child, remapping old ids -> new
            # ids as we go (a fresh INSERT can't reuse the old autoincrement
            # ids) — same approach as objects/devices below, just one level
            # up since locations can nest into each other.
            location_id_map: dict[int, int] = {}
            remaining = list(payload.get("locations", []))
            while remaining:
                progressed = False
                still_remaining = []
                for loc in remaining:
                    old_id = loc["id"]
                    old_parent = loc.get("parent_location_id")
                    if old_parent is None or old_parent in location_id_map:
                        cur = conn.execute(
                            "INSERT INTO locations (name, parent_location_id, description, kind, sort_order) VALUES (?,?,?,?,?)",
                            (loc["name"], location_id_map.get(old_parent) if old_parent is not None else None, loc.get("description", ""), loc.get("kind"), loc.get("sort_order")),
                        )
                        location_id_map[old_id] = cur.lastrowid
                        progressed = True
                    else:
                        still_remaining.append(loc)
                if not progressed:
                    # Malformed/cyclic parent references in old data — bail
                    # rather than looping forever; leftover locations are
                    # simply not restored.
                    break
                remaining = still_remaining
            conn.commit()

            # Brick Core: semantic_entities.device_id/object_id can reference
            # ANY device/object in the project, not just "the current one" --
            # unlike obj_lookup below (kept as-is, per-device, keyed by
            # (object_type, object_instance) for trend_logs/schedules), these
            # two maps accumulate across the WHOLE devices loop so entities
            # can be restored afterward regardless of which device they
            # belong to.
            dev_id_map: dict[int, int] = {}
            global_obj_id_map: dict[int, int] = {}
            # Deferred remap: source_device_id FK references IDs that change on load.
            source_device_remap: dict[int, list[int]] = {}

            for dev in payload.get("devices", []):
                objects = dev.pop("objects", [])
                trend_logs = dev.pop("trend_logs", [])
                schedules = dev.pop("schedules", [])
                calendars = dev.pop("calendars", [])
                old_dev_id = dev.pop("id", None)
                old_location_id = dev.get("location_id")
                cur = conn.execute(
                    "INSERT INTO devices (device_instance, name, description, vendor_name, model_name, enabled, "
                    "firmware_revision, protocol_revision, max_apdu_length_accepted, segmentation_supported, "
                    "location_id, equipment_type, source_type, external_host, external_port, "
                    "external_vendor_id, external_last_seen_at, simulation_mode) "
                    "VALUES (:device_instance, :name, :description, :vendor_name, :model_name, :enabled, "
                    ":firmware_revision, :protocol_revision, :max_apdu_length_accepted, :segmentation_supported, "
                    ":location_id, :equipment_type, :source_type, :external_host, :external_port, "
                    ":external_vendor_id, :external_last_seen_at, :simulation_mode)",
                    {
                        **dev,
                        "firmware_revision": dev.get("firmware_revision") or "N/A",
                        "protocol_revision": dev.get("protocol_revision") or 22,
                        "max_apdu_length_accepted": dev.get("max_apdu_length_accepted") or 1024,
                        "segmentation_supported": dev.get("segmentation_supported") or "segmented-both",
                        "location_id": location_id_map.get(old_location_id) if old_location_id is not None else None,
                        "equipment_type": dev.get("equipment_type"),
                        # Older saved projects (pre-dating this column) have no
                        # source_type at all -- default to 'simulated', same
                        # fallback SimEngine._simulated_enabled_devices() uses,
                        # so a device never silently becomes external (or vice
                        # versa, see the safety-boundary requirement) just by
                        # being saved/reloaded.
                        "source_type": dev.get("source_type") or "simulated",
                        "external_host": dev.get("external_host"),
                        "external_port": dev.get("external_port"),
                        "external_vendor_id": dev.get("external_vendor_id"),
                        "external_last_seen_at": dev.get("external_last_seen_at"),
                        "simulation_mode": dev.get("simulation_mode") or "simulation",
                    },
                )
                dev_id = cur.lastrowid
                if old_dev_id is not None:
                    dev_id_map[old_dev_id] = dev_id
                old_src_dev_id = dev.get("source_device_id")
                if old_src_dev_id is not None:
                    source_device_remap.setdefault(old_src_dev_id, []).append(dev_id)
                obj_lookup: dict[tuple[str, int], int] = {}
                for obj in objects:
                    old_obj_id = obj.pop("id", None)
                    obj.pop("device_id", None)
                    obj_cur = conn.execute(
                        "INSERT OR IGNORE INTO objects "
                        "(device_id, object_type, object_instance, name, units, behavior, "
                        "behavior_params, enabled, manual_value, number_of_states, reliability, polarity, point_type, description) "
                        "VALUES (:device_id, :object_type, :object_instance, :name, :units, "
                        ":behavior, :behavior_params, :enabled, :manual_value, :number_of_states, :reliability, :polarity, :point_type, :description)",
                        {
                            **obj,
                            "device_id": dev_id,
                            "manual_value": obj.get("manual_value"),
                            "number_of_states": obj.get("number_of_states") or 2,
                            "reliability": obj.get("reliability") or "no-fault-detected",
                            "polarity": obj.get("polarity") or "normal",
                            "point_type": obj.get("point_type"),
                            "description": obj.get("description"),
                        },
                    )
                    obj_id = obj_cur.lastrowid if obj_cur.lastrowid else conn.execute(
                        "SELECT id FROM objects WHERE device_id=? AND object_type=? AND object_instance=?",
                        (dev_id, obj["object_type"], obj["object_instance"]),
                    ).fetchone()[0]
                    obj_lookup[(obj["object_type"], obj["object_instance"])] = obj_id
                    if old_obj_id is not None:
                        global_obj_id_map[old_obj_id] = obj_id

                for tl in trend_logs:
                    ref = tl.pop("monitored_object_ref", None)
                    if not ref:
                        continue
                    mon_id = obj_lookup.get((ref["object_type"], ref["object_instance"]))
                    if mon_id is None:
                        continue
                    conn.execute(
                        "INSERT INTO trend_logs "
                        "(device_id, name, description, monitored_object_id, logging_type, log_interval, "
                        "cov_increment, buffer_size, stop_when_full, enabled) "
                        "VALUES (:device_id, :name, :description, :monitored_object_id, :logging_type, "
                        ":log_interval, :cov_increment, :buffer_size, :stop_when_full, :enabled)",
                        {
                            "device_id": dev_id,
                            "name": tl.get("name", "Trend Log"),
                            "description": tl.get("description", ""),
                            "monitored_object_id": mon_id,
                            "logging_type": tl.get("logging_type", "polled"),
                            "log_interval": tl.get("log_interval", 60),
                            "cov_increment": tl.get("cov_increment", 1.0),
                            "buffer_size": tl.get("buffer_size", 1000),
                            "stop_when_full": tl.get("stop_when_full", 0),
                            "enabled": tl.get("enabled", 1),
                        },
                    )

                for sched in schedules:
                    portable_targets = sched.pop("targets", [])
                    sched_cur = conn.execute(
                        "INSERT INTO bacnet_schedules "
                        "(device_id, name, description, value_type, schedule_default, effective_start, "
                        "effective_end, weekly_schedule, exception_schedule, priority_for_writing, enabled) "
                        "VALUES (:device_id, :name, :description, :value_type, :schedule_default, "
                        ":effective_start, :effective_end, :weekly_schedule, :exception_schedule, "
                        ":priority_for_writing, :enabled)",
                        {
                            "device_id": dev_id,
                            "name": sched.get("name", "Schedule"),
                            "description": sched.get("description", ""),
                            "value_type": sched.get("value_type", "real"),
                            "schedule_default": sched.get("schedule_default", "0"),
                            "effective_start": sched.get("effective_start"),
                            "effective_end": sched.get("effective_end"),
                            "weekly_schedule": sched.get("weekly_schedule", "{}"),
                            "exception_schedule": sched.get("exception_schedule", "[]"),
                            "priority_for_writing": sched.get("priority_for_writing", 10),
                            "enabled": sched.get("enabled", 1),
                        },
                    )
                    schedule_id = sched_cur.lastrowid
                    for t in portable_targets:
                        target_id = obj_lookup.get((t["object_type"], t["object_instance"]))
                        if target_id is None:
                            continue
                        conn.execute(
                            "INSERT INTO bacnet_schedule_targets (schedule_id, object_id, property_identifier) "
                            "VALUES (?, ?, ?)",
                            (schedule_id, target_id, t.get("property_identifier", "present-value")),
                        )

                for cal in calendars:
                    conn.execute(
                        "INSERT INTO bacnet_calendars (device_id, name, description, date_list, enabled) "
                        "VALUES (:device_id, :name, :description, :date_list, :enabled)",
                        {
                            "device_id": dev_id,
                            "name": cal.get("name", "Calendar"),
                            "description": cal.get("description", ""),
                            "date_list": cal.get("date_list", "[]"),
                            "enabled": cal.get("enabled", 1),
                        },
                    )

            # Remap source_device_id now that all devices have new IDs.
            for old_src_id, new_mirror_ids in source_device_remap.items():
                new_src_id = dev_id_map.get(old_src_id)
                if new_src_id is not None:
                    for mid in new_mirror_ids:
                        conn.execute(
                            "UPDATE devices SET source_device_id=? WHERE id=?",
                            (new_src_id, mid),
                        )

            # Equipment references a location only -- location_id_map is
            # already fully populated by the locations restore above.
            equipment_id_map: dict[int, int] = {}
            for eq in payload.get("equipment", []):
                old_equipment_id = eq.pop("id", None)
                old_location_id = eq.get("location_id")
                eq_cur = conn.execute(
                    "INSERT INTO equipment (name, description, location_id, equipment_type) VALUES (?,?,?,?)",
                    (
                        eq["name"],
                        eq.get("description", ""),
                        location_id_map.get(old_location_id) if old_location_id is not None else None,
                        eq.get("equipment_type"),
                    ),
                )
                if old_equipment_id is not None:
                    equipment_id_map[old_equipment_id] = eq_cur.lastrowid

            # Energy model configs reference a device only (no objects/
            # locations involved), so they just need dev_id_map, already
            # fully populated by the devices loop above. Skip silently if
            # the owning device wasn't restored, rather than fail the whole
            # import -- same tolerance as the relationship restore below.
            for cfg in payload.get("energy_model_configs", []):
                new_device_id = dev_id_map.get(cfg.get("device_id"))
                if new_device_id is None:
                    continue
                conn.execute(
                    "INSERT INTO energy_model_configs (device_id, model_type, instance_key, enabled, parameters) "
                    "VALUES (?,?,?,?,?)",
                    (
                        new_device_id,
                        cfg["model_type"],
                        cfg.get("instance_key") or "default",
                        cfg.get("enabled", 1),
                        cfg.get("parameters") or "{}",
                    ),
                )

            # Simulation models reference a device (created_from_device_id,
            # via dev_id_map) and, per mapping, a point (point_id, via
            # global_obj_id_map) -- both already fully populated by the
            # devices loop above, same as energy_model_configs just above.
            # created_from_device_id alone missing its device is tolerated
            # (set to NULL, matching how the live create/update API already
            # allows a null created_from_device_id) -- but a mapping/
            # aggregate-member point that didn't come back is NOT silently
            # kept with a stale id: that mapping (or, for weighted_average,
            # that specific value/weight pair) is dropped instead, the same
            # tolerance semantic_relationships uses below for an endpoint
            # that didn't restore. A weighted_average pair missing its
            # weight after remap is dropped as a whole pair (not left with
            # a value point and no weight), mirroring validate()'s own
            # "every value point needs a weight" rule -- never insert a
            # structurally invalid aggregate row.
            for model in payload.get("simulation_models", []):
                old_created_from_device_id = model.get("created_from_device_id")
                new_created_from_device_id = (
                    dev_id_map.get(old_created_from_device_id)
                    if old_created_from_device_id is not None else None
                )
                restored_mappings: list[dict] = []
                restored_aggregate_mappings: list[dict] = []
                for m in model.get("mappings", []):
                    if "point_ids" in m:
                        new_point_ids = [global_obj_id_map.get(pid) for pid in m["point_ids"]]
                        if any(pid is None for pid in new_point_ids):
                            continue
                        operation = m.get("operation") or "max"
                        weight_point_ids = None
                        if operation == "weighted_average":
                            raw_weights = m.get("weight_point_ids") or [None] * len(m["point_ids"])
                            new_weight_ids = [
                                (global_obj_id_map.get(w) if w is not None else None)
                                for w in raw_weights
                            ]
                            if any(w is None for w in new_weight_ids):
                                continue
                            weight_point_ids = new_weight_ids
                        restored_aggregate_mappings.append({
                            "variable": m["variable"],
                            "direction": m["direction"],
                            "operation": operation,
                            "point_ids": new_point_ids,
                            "weight_point_ids": weight_point_ids,
                        })
                    else:
                        new_point_id = global_obj_id_map.get(m["point_id"])
                        if new_point_id is None:
                            continue
                        restored_mappings.append({
                            "variable": m["variable"],
                            "direction": m["direction"],
                            "point_id": new_point_id,
                        })
                # input_exposures: same tolerant remap-or-drop as an ordinary
                # mapping above -- an exposure targeting a point that didn't
                # come back is dropped, never left pointing at a stale id.
                restored_input_exposures: list[dict] = []
                for e in model.get("input_exposures", []):
                    new_point_id = global_obj_id_map.get(e["point_id"])
                    if new_point_id is None:
                        continue
                    restored_input_exposures.append({
                        "variable": e["variable"],
                        "point_id": new_point_id,
                    })
                insert_simulation_model(
                    conn,
                    name=model["name"],
                    provider_type=model["provider_type"],
                    model_type=model["model_type"],
                    enabled=bool(model.get("enabled")),
                    parameters=model.get("parameters") or {},
                    created_from_device_id=new_created_from_device_id,
                    mappings=restored_mappings,
                    aggregate_mappings=restored_aggregate_mappings,
                    input_exposures=restored_input_exposures,
                )

            # No FK to device/object/location at all -- unlike everything
            # else restored above, nothing to remap, just re-insert verbatim.
            for ft in payload.get("functional_tests", []):
                conn.execute(
                    "INSERT INTO functional_tests (name, description, equipment_type, definition_json, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        ft["name"],
                        ft.get("description", ""),
                        ft["equipment_type"],
                        ft.get("definition_json") or "{}",
                        ft.get("created_at") or datetime.now(timezone.utc).isoformat(),
                        ft.get("updated_at") or datetime.now(timezone.utc).isoformat(),
                    ),
                )

            # Same "no FK, re-insert verbatim" reasoning as functional_tests
            # immediately above -- a custom_graphs definition's device_id/
            # object_id references are opaque JSON, resolved live against
            # /points whenever a graph is opened, not real foreign keys.
            for cg in payload.get("custom_graphs", []):
                conn.execute(
                    "INSERT INTO custom_graphs (name, definition_json, created_at, updated_at) "
                    "VALUES (?,?,?,?)",
                    (
                        cg["name"],
                        cg.get("definition_json") or "{}",
                        cg.get("created_at") or datetime.now(timezone.utc).isoformat(),
                        cg.get("updated_at") or datetime.now(timezone.utc).isoformat(),
                    ),
                )

            # Brick Core: restore entities AFTER locations/devices/objects
            # (their device_id/object_id/location_id FKs need the remaps
            # above to already exist), relationships LAST (they reference
            # entities by id, which only exist once this loop has run).
            #
            # semantic_key is RECOMPUTED from the newly-assigned ids, never
            # copied from the stored value -- a stored semantic_key embeds
            # the OLD surrogate device/object/location ids, which this same
            # load_project() call just reassigned above. local_slug never
            # references a surrogate id, so it survives verbatim and is
            # what makes recomputing the correct key possible.
            entity_id_map: dict[int, int] = {}
            for ent in payload.get("semantic_entities", []):
                old_entity_id = ent["id"]
                new_device_id = (
                    dev_id_map.get(ent.get("device_id"))
                    if ent.get("device_id") is not None else None
                )
                new_object_id = (
                    global_obj_id_map.get(ent.get("object_id"))
                    if ent.get("object_id") is not None else None
                )
                new_location_id = (
                    location_id_map.get(ent.get("location_id"))
                    if ent.get("location_id") is not None else None
                )
                new_equipment_id = (
                    equipment_id_map.get(ent.get("equipment_id"))
                    if ent.get("equipment_id") is not None else None
                )
                computed_key = derive_semantic_key(
                    ent["entity_kind"], ent["brick_class"],
                    device_id=new_device_id, object_id=new_object_id, location_id=new_location_id,
                    equipment_id=new_equipment_id,
                    local_slug=ent.get("local_slug"),
                )
                ent_cur = conn.execute(
                    "INSERT INTO semantic_entities "
                    "(name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id, equipment_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ent["name"], ent.get("local_slug"), computed_key, ent["brick_class"], ent["entity_kind"],
                        new_device_id, new_object_id, new_location_id, new_equipment_id,
                    ),
                )
                entity_id_map[old_entity_id] = ent_cur.lastrowid

            for rel in payload.get("semantic_relationships", []):
                new_source_id = entity_id_map.get(rel["source_entity_id"])
                new_target_id = entity_id_map.get(rel["target_entity_id"])
                if new_source_id is None or new_target_id is None:
                    # Endpoint entity wasn't restorable -- skip this
                    # relationship rather than fail the whole import.
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO semantic_relationships (source_entity_id, predicate, target_entity_id) "
                    "VALUES (?,?,?)",
                    (new_source_id, rel["predicate"], new_target_id),
                )

            conn.commit()
            discovery_connections = self._normalize_discovery_connections(
                payload.get("discovery_connections"),
                payload.get("connection_config"),
            )
            return {
                "connection_config": payload.get("connection_config"),
                "discovery_connections": discovery_connections,
            }

    def import_project(self, name: str, description: str, data: dict) -> dict:
        with self._conn() as conn:
            device_count = len(data.get("devices", []))
            cur = conn.execute(
                "INSERT INTO profiles (name, description, device_count, data) VALUES (?,?,?,?)",
                (name, description, device_count, json.dumps(data)),
            )
            conn.commit()
            row = dict(conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles WHERE id=?",
                (cur.lastrowid,),
            ).fetchone())
            row["connection_config"] = data.get("connection_config")
            row["discovery_connections"] = self._normalize_discovery_connections(
                data.get("discovery_connections"),
                data.get("connection_config"),
            )
            return row

    def delete_project(self, project_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM profiles WHERE id=?", (project_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Users ─────────────────────────────────────────────────────────────────

    def count_users(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def list_users(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, username, created_at, last_login_at FROM users ORDER BY created_at"
            )]

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(r) if r else None

    def get_user_by_username(self, username: str) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return dict(r) if r else None

    def create_user(self, username: str, password_hash: str) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?,?)",
                (username, password_hash),
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT id, username, created_at, last_login_at FROM users WHERE id=?",
                (cur.lastrowid,),
            ).fetchone())

    def update_user_password(self, user_id: int, password_hash: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id)
            )
            conn.commit()
            return cur.rowcount > 0

    def touch_last_login(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET last_login_at=datetime('now') WHERE id=?", (user_id,)
            )
            conn.commit()

    def delete_user(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Settings ──────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        values = _default_settings()
        with self._conn() as conn:
            for row in conn.execute("SELECT key, value FROM settings"):
                key = row["key"]
                if key in SETTINGS_SCHEMA:
                    values[key] = SETTINGS_SCHEMA[key](row["value"])
        return values

    def save_settings(self, values: dict) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, str(v)) for k, v in values.items() if k in SETTINGS_SCHEMA],
            )
            conn.commit()


