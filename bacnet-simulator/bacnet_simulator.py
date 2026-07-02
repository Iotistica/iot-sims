"""
BACnet/IP Simulator with REST + WebSocket management API.

Serves multiple virtual BACnet devices on UDP port 47808 and a management
API on HTTP port 47900. Device/object config is persisted in SQLite so it
survives container restarts and can be edited live via the Iotistica admin UI.
"""
import asyncio
import json
import math
import os
import random
import socket
import sqlite3
import time
from abc import ABC, abstractmethod
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from bacpypes3.local.device import DeviceObject
from bacpypes3.local.analog import AnalogInputObject, AnalogOutputObject, AnalogValueObject
from bacpypes3.local.binary import BinaryInputObject, BinaryOutputObject, BinaryValueObject
from bacpypes3.app import Application
from bacpypes3.primitivedata import Real, ObjectIdentifier
from bacpypes3.basetypes import EngineeringUnits, BinaryPV
from bacpypes3.debugging import bacpypes_debugging, ModuleLogger
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.apdu import ReadPropertyACK, SimpleAckPDU

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bacnet-sim")

_debug = 0
_log = ModuleLogger(globals())


# ─── Constants ────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "bacnet_sim.db"
SIM_API_PORT = int(os.environ.get("SIM_API_PORT", "47900"))
BACNET_PORT = int(os.environ.get("BACNET_PORT", "47808"))

VALID_OBJECT_TYPES = {
    "analog-input", "analog-output", "analog-value",
    "binary-input", "binary-output", "binary-value",
}

VALID_BEHAVIORS = {"constant", "sine", "noise", "random_walk", "manual", "schedule", "ramp", "fault"}

BACNET_UNITS = [
    "no-units", "degrees-celsius", "degrees-fahrenheit", "kelvin",
    "percent", "parts-per-million", "kilowatts", "watts", "kilowatt-hours",
    "amperes", "volts", "cubic-feet-per-minute", "liters-per-second",
    "pascals", "kilopascals", "bars", "cubic-meters-per-hour",
    "revolutions-per-minute", "meters-per-second",
]


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
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_instance INTEGER NOT NULL UNIQUE
                        CHECK(device_instance >= 1 AND device_instance <= 4194302),
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    vendor_name TEXT NOT NULL DEFAULT 'Iotistica',
                    model_name TEXT NOT NULL DEFAULT 'BACnet Simulator',
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS objects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    object_type TEXT NOT NULL,
                    object_instance INTEGER NOT NULL CHECK(object_instance >= 0 AND object_instance <= 4194302),
                    name TEXT NOT NULL,
                    units TEXT NOT NULL DEFAULT 'no-units',
                    behavior TEXT NOT NULL DEFAULT 'constant',
                    behavior_params TEXT NOT NULL DEFAULT '{"value":0}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    manual_value REAL,
                    UNIQUE(device_id, object_type, object_instance)
                );

                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    device_count INTEGER NOT NULL DEFAULT 0,
                    data TEXT NOT NULL DEFAULT '{}'
                );
            """)
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
        1101  VAV-L1-01          Siemens RXB29.1             (floor 1 zone A – north)
        1102  VAV-L1-02          Siemens RXB29.1             (floor 1 zone B – south)
        1201  VAV-L2-01          Siemens RXB29.1             (floor 2 zone A – conference)
        1202  VAV-L2-02          Siemens RXB29.1             (floor 2 zone B – open plan)
        1301  VAV-L3-01          Siemens RXB29.1             (floor 3 zone A – exec suites)
        1302  VAV-L3-02          Siemens RXB29.1             (floor 3 zone B – server room)
        1401  VAV-L4-01          Siemens RXB29.1             (floor 4 zone A – open plan)
        1402  VAV-L4-02          Siemens RXB29.1             (floor 4 zone B – board room)
        """
        HONEYWELL = "Honeywell International"
        JCI       = "Johnson Controls"
        SIEMENS   = "Siemens Building Technologies"
        TRANE     = "Trane Technologies"

        with self._conn() as conn:
            if conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] > 0:
                return

            conn.executemany(
                "INSERT OR IGNORE INTO devices "
                "(device_instance, name, description, vendor_name, model_name) "
                "VALUES (?,?,?,?,?)",
                [
                    (1000, "BMS-Gateway",  "Building management system gateway – supervisory controller",  HONEYWELL, "WEBs-N4"),
                    (1001, "Chiller-Plant","Basement chiller plant – 2 × centrifugal chillers + cooling tower", TRANE, "Tracer SC+"),
                    (1002, "HW-Plant",     "Basement hot-water plant – 2 × condensing boilers",           HONEYWELL, "Excel 500"),
                    (1003, "AHU-1",        "Air handling unit 1 – serves floors 1 & 2",                   JCI,       "FEC26B"),
                    (1004, "AHU-2",        "Air handling unit 2 – serves floors 3 & 4",                   JCI,       "FEC26B"),
                    (1101, "VAV-L1-01",    "Floor 1 VAV – Zone A north offices",                           SIEMENS,   "RXB29.1"),
                    (1102, "VAV-L1-02",    "Floor 1 VAV – Zone B south offices",                           SIEMENS,   "RXB29.1"),
                    (1201, "VAV-L2-01",    "Floor 2 VAV – Zone A conference rooms",                        SIEMENS,   "RXB29.1"),
                    (1202, "VAV-L2-02",    "Floor 2 VAV – Zone B open-plan",                               SIEMENS,   "RXB29.1"),
                    (1301, "VAV-L3-01",    "Floor 3 VAV – Zone A executive suites",                        SIEMENS,   "RXB29.1"),
                    (1302, "VAV-L3-02",    "Floor 3 VAV – Zone B server room",                             SIEMENS,   "RXB29.1"),
                    (1401, "VAV-L4-01",    "Floor 4 VAV – Zone A open-plan",                               SIEMENS,   "RXB29.1"),
                    (1402, "VAV-L4-02",    "Floor 4 VAV – Zone B board room",                              SIEMENS,   "RXB29.1"),
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
                (bms, "analog-input",  3, "Energy-Today-kWh",    "kilowatt-hours",  "random_walk", '{"value":430,"step":12,"min":0,"max":2000}'),
                (bms, "analog-input",  4, "Peak-Demand-kW",      "kilowatts",       "random_walk", '{"value":182,"step":4,"min":50,"max":320}'),
                (bms, "analog-input",  5, "Outside-Air-Temp",    "degrees-celsius", "sine",        '{"base":12.0,"amplitude":8.0,"period_hours":24}'),
                (bms, "analog-input",  6, "Outside-Air-Humidity","percent",         "sine",        '{"base":55.0,"amplitude":15.0,"period_hours":24}'),
            ]

            # ── Chiller Plant (1001) ───────────────────────────────────────────
            cp = did(1001)
            objects += [
                (cp, "binary-input",  1, "CH-1-Run",             "no-units",        "manual",      '{"value":true}'),
                (cp, "analog-input",  2, "CH-1-kW",              "kilowatts",       "random_walk", '{"value":212,"step":8,"min":80,"max":320}'),
                (cp, "analog-input",  3, "CH-1-COP",             "no-units",        "noise",       '{"base":5.8,"noise":0.2}'),
                (cp, "binary-input",  4, "CH-2-Run",             "no-units",        "manual",      '{"value":true}'),
                (cp, "analog-input",  5, "CH-2-kW",              "kilowatts",       "random_walk", '{"value":198,"step":8,"min":80,"max":320}'),
                (cp, "analog-input",  6, "CH-2-COP",             "no-units",        "noise",       '{"base":5.6,"noise":0.2}'),
                (cp, "analog-input",  7, "CW-Supply-Temp",       "degrees-celsius", "noise",       '{"base":6.5,"noise":0.2}'),
                (cp, "analog-input",  8, "CW-Return-Temp",       "degrees-celsius", "noise",       '{"base":12.2,"noise":0.2}'),
                (cp, "analog-input",  9, "CW-Flow",              "liters-per-second","noise",      '{"base":48.0,"noise":1.5}'),
                (cp, "analog-input", 10, "CW-Diff-Pressure",     "pascals",         "noise",       '{"base":225,"noise":8}'),
                (cp, "binary-input", 11, "CT-Fan-1-Run",         "no-units",        "manual",      '{"value":true}'),
                (cp, "binary-input", 12, "CT-Fan-2-Run",         "no-units",        "manual",      '{"value":true}'),
                (cp, "analog-input", 13, "CT-Leaving-Water-Temp","degrees-celsius", "noise",       '{"base":29.5,"noise":0.5}'),
                (cp, "analog-input", 14, "CT-Approach-Temp",     "degrees-celsius", "noise",       '{"base":3.2,"noise":0.3}'),
                (cp, "binary-input", 15, "CW-Pump-1-Run",        "no-units",        "manual",      '{"value":true}'),
                (cp, "binary-input", 16, "CW-Pump-2-Run",        "no-units",        "manual",      '{"value":false}'),
            ]

            # ── Hot Water Plant (1002) ─────────────────────────────────────────
            hw = did(1002)
            objects += [
                (hw, "binary-input",  1, "BLR-1-Run",            "no-units",        "manual",      '{"value":true}'),
                (hw, "analog-input",  2, "BLR-1-Firing-Rate",    "percent",         "noise",       '{"base":62,"noise":5}'),
                (hw, "analog-input",  3, "BLR-1-Flue-Temp",      "degrees-celsius", "noise",       '{"base":88,"noise":3}'),
                (hw, "binary-input",  4, "BLR-2-Run",            "no-units",        "manual",      '{"value":false}'),
                (hw, "analog-input",  5, "BLR-2-Firing-Rate",    "percent",         "manual",      '{"value":0}'),
                (hw, "analog-input",  6, "HW-Supply-Temp",       "degrees-celsius", "noise",       '{"base":71.0,"noise":0.8}'),
                (hw, "analog-input",  7, "HW-Return-Temp",       "degrees-celsius", "noise",       '{"base":58.5,"noise":0.8}'),
                (hw, "analog-input",  8, "HW-Diff-Pressure",     "pascals",         "noise",       '{"base":180,"noise":6}'),
                (hw, "analog-input",  9, "Gas-Flow",             "cubic-feet-per-minute","random_walk",'{"value":44,"step":3,"min":10,"max":85}'),
                (hw, "binary-input", 10, "HW-Pump-1-Run",        "no-units",        "manual",      '{"value":true}'),
                (hw, "binary-input", 11, "HW-Pump-2-Run",        "no-units",        "manual",      '{"value":false}'),
            ]

            # ── AHU-1  floors 1-2 (1003) ──────────────────────────────────────
            ahu1 = did(1003)
            objects += [
                (ahu1, "binary-input",  1, "SF-Run",             "no-units",        "manual",      '{"value":true}'),
                (ahu1, "binary-input",  2, "RF-Run",             "no-units",        "manual",      '{"value":true}'),
                (ahu1, "analog-input",  3, "SF-Speed",           "percent",         "sine",        '{"base":75.0,"amplitude":15.0,"period_hours":12}'),
                (ahu1, "analog-input",  4, "RF-Speed",           "percent",         "sine",        '{"base":70.0,"amplitude":12.0,"period_hours":12}'),
                (ahu1, "analog-input",  5, "SAT",                "degrees-celsius", "noise",       '{"base":13.0,"noise":0.4}'),
                (ahu1, "analog-input",  6, "RAT",                "degrees-celsius", "sine",        '{"base":22.0,"amplitude":2.0,"period_hours":24}'),
                (ahu1, "analog-input",  7, "MAT",                "degrees-celsius", "noise",       '{"base":16.0,"noise":0.8}'),
                (ahu1, "analog-input",  8, "OAT",                "degrees-celsius", "sine",        '{"base":12.0,"amplitude":8.0,"period_hours":24}'),
                (ahu1, "analog-output", 9, "OAD-Position",       "percent",         "sine",        '{"base":28.0,"amplitude":18.0,"period_hours":24}'),
                (ahu1, "analog-output",10, "CC-Valve",           "percent",         "sine",        '{"base":55.0,"amplitude":25.0,"period_hours":12}'),
                (ahu1, "analog-output",11, "HC-Valve",           "percent",         "sine",        '{"base":10.0,"amplitude":9.0,"period_hours":24}'),
                (ahu1, "analog-input", 12, "SA-Flow",            "cubic-feet-per-minute","noise",  '{"base":8500,"noise":250}'),
                (ahu1, "analog-input", 13, "SA-Static-Pressure", "pascals",         "noise",       '{"base":375,"noise":12}'),
                (ahu1, "binary-input", 14, "Filter-DP-Alarm",    "no-units",        "manual",      '{"value":false}'),
                (ahu1, "binary-input", 15, "Freeze-Stat",        "no-units",        "manual",      '{"value":false}'),
            ]

            # ── AHU-2  floors 3-4 (1004) ──────────────────────────────────────
            ahu2 = did(1004)
            objects += [
                (ahu2, "binary-input",  1, "SF-Run",             "no-units",        "manual",      '{"value":true}'),
                (ahu2, "binary-input",  2, "RF-Run",             "no-units",        "manual",      '{"value":true}'),
                (ahu2, "analog-input",  3, "SF-Speed",           "percent",         "sine",        '{"base":70.0,"amplitude":18.0,"period_hours":12}'),
                (ahu2, "analog-input",  4, "RF-Speed",           "percent",         "sine",        '{"base":65.0,"amplitude":14.0,"period_hours":12}'),
                (ahu2, "analog-input",  5, "SAT",                "degrees-celsius", "noise",       '{"base":13.5,"noise":0.4}'),
                (ahu2, "analog-input",  6, "RAT",                "degrees-celsius", "sine",        '{"base":21.5,"amplitude":2.0,"period_hours":24}'),
                (ahu2, "analog-input",  7, "MAT",                "degrees-celsius", "noise",       '{"base":15.5,"noise":0.8}'),
                (ahu2, "analog-input",  8, "OAT",                "degrees-celsius", "sine",        '{"base":12.0,"amplitude":8.0,"period_hours":24}'),
                (ahu2, "analog-output", 9, "OAD-Position",       "percent",         "sine",        '{"base":25.0,"amplitude":16.0,"period_hours":24}'),
                (ahu2, "analog-output",10, "CC-Valve",           "percent",         "sine",        '{"base":50.0,"amplitude":22.0,"period_hours":12}'),
                (ahu2, "analog-output",11, "HC-Valve",           "percent",         "sine",        '{"base":12.0,"amplitude":9.0,"period_hours":24}'),
                (ahu2, "analog-input", 12, "SA-Flow",            "cubic-feet-per-minute","noise",  '{"base":7800,"noise":220}'),
                (ahu2, "analog-input", 13, "SA-Static-Pressure", "pascals",         "noise",       '{"base":360,"noise":12}'),
                (ahu2, "binary-input", 14, "Filter-DP-Alarm",    "no-units",        "manual",      '{"value":false}'),
                (ahu2, "binary-input", 15, "Freeze-Stat",        "no-units",        "manual",      '{"value":false}'),
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
                    (vd, "analog-input",  1, "Zone-Temp",         "degrees-celsius", "noise",  f'{{"base":{zt},"noise":0.3}}'),
                    (vd, "analog-value",  2, "Cooling-SP",        "degrees-celsius", "manual", f'{{"value":{csp}}}'),
                    (vd, "analog-value",  3, "Heating-SP",        "degrees-celsius", "manual", f'{{"value":{hsp}}}'),
                    (vd, "analog-output", 4, "Damper-Cmd",        "percent",         "sine",   f'{{"base":{dmp},"amplitude":14.0,"period_hours":8}}'),
                    (vd, "analog-input",  5, "Zone-Airflow",      "cubic-feet-per-minute","noise",f'{{"base":{flow},"noise":18}}'),
                    (vd, "analog-output", 6, "Reheat-Valve",      "percent",         "sine",   f'{{"base":{rh},"amplitude":10.0,"period_hours":12}}'),
                    (vd, "binary-input",  7, "Occupancy",         "no-units",        "manual", '{"value":true}'),
                    (vd, "analog-input",  8, "Zone-CO2",          "parts-per-million","random_walk",f'{{"value":650,"step":30,"min":400,"max":1200}}'),
                ]

            conn.executemany(
                "INSERT OR IGNORE INTO objects "
                "(device_id, object_type, object_instance, name, units, behavior, behavior_params) "
                "VALUES (?,?,?,?,?,?,?)",
                objects,
            )
        log.info("Seeded 4-storey office tower: Honeywell/Trane/JCI/Siemens – 13 devices, %d objects", len(objects))

    def get_devices(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM devices ORDER BY device_instance")]

    def get_device(self, device_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            return dict(r) if r else None

    def create_device(self, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO devices (device_instance, name, description, vendor_name, model_name, enabled) "
                "VALUES (:device_instance, :name, :description, :vendor_name, :model_name, :enabled)",
                data,
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM devices WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_device(self, device_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET device_instance=:device_instance, name=:name, "
                "description=:description, vendor_name=:vendor_name, model_name=:model_name, "
                "enabled=:enabled WHERE id=:id",
                {**data, "id": device_id},
            )
            conn.commit()
            r = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            return dict(r) if r else None

    def delete_device(self, device_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            conn.commit()
            return cur.rowcount > 0

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

    def create_object(self, device_id: int, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO objects (device_id, object_type, object_instance, name, units, behavior, behavior_params, enabled) "
                "VALUES (:device_id, :object_type, :object_instance, :name, :units, :behavior, :behavior_params, :enabled)",
                {**data, "device_id": device_id},
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM objects WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_object(self, obj_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE objects SET object_type=:object_type, object_instance=:object_instance, "
                "name=:name, units=:units, behavior=:behavior, behavior_params=:behavior_params, "
                "enabled=:enabled WHERE id=:id",
                {**data, "id": obj_id},
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

    # ── Profiles ──────────────────────────────────────────────────────────────

    def get_profiles(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles ORDER BY created_at DESC"
            )]

    def get_profile(self, profile_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
            return dict(r) if r else None

    def save_profile(self, name: str, description: str) -> dict:
        with self._conn() as conn:
            devices = [dict(r) for r in conn.execute(
                "SELECT * FROM devices ORDER BY device_instance"
            )]
            for dev in devices:
                dev["objects"] = [dict(r) for r in conn.execute(
                    "SELECT * FROM objects WHERE device_id=? ORDER BY object_type, object_instance",
                    (dev["id"],),
                )]
            data = json.dumps({"devices": devices})
            cur = conn.execute(
                "INSERT INTO profiles (name, description, device_count, data) VALUES (?,?,?,?)",
                (name, description, len(devices), data),
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles WHERE id=?",
                (cur.lastrowid,),
            ).fetchone())

    def update_profile(self, profile_id: int, name: str, description: str) -> bool:
        with self._conn() as conn:
            devices = [dict(r) for r in conn.execute(
                "SELECT * FROM devices ORDER BY device_instance"
            )]
            for dev in devices:
                dev["objects"] = [dict(r) for r in conn.execute(
                    "SELECT * FROM objects WHERE device_id=? ORDER BY object_type, object_instance",
                    (dev["id"],),
                )]
            data = json.dumps({"devices": devices})
            cur = conn.execute(
                "UPDATE profiles SET name=?, description=?, device_count=?, data=? WHERE id=?",
                (name, description, len(devices), data, profile_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def load_profile(self, profile_id: int) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT data FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if not row:
                return False
            payload = json.loads(row["data"])
            conn.execute("DELETE FROM devices")
            conn.commit()
            for dev in payload.get("devices", []):
                objects = dev.pop("objects", [])
                dev.pop("id", None)
                cur = conn.execute(
                    "INSERT INTO devices (device_instance, name, description, vendor_name, model_name, enabled) "
                    "VALUES (:device_instance, :name, :description, :vendor_name, :model_name, :enabled)",
                    dev,
                )
                dev_id = cur.lastrowid
                for obj in objects:
                    obj.pop("id", None)
                    obj.pop("device_id", None)
                    conn.execute(
                        "INSERT OR IGNORE INTO objects "
                        "(device_id, object_type, object_instance, name, units, behavior, "
                        "behavior_params, enabled, manual_value) "
                        "VALUES (:device_id, :object_type, :object_instance, :name, :units, "
                        ":behavior, :behavior_params, :enabled, :manual_value)",
                        {**obj, "device_id": dev_id, "manual_value": obj.get("manual_value")},
                    )
            conn.commit()
            return True

    def import_profile(self, name: str, description: str, data: dict) -> dict:
        with self._conn() as conn:
            device_count = len(data.get("devices", []))
            cur = conn.execute(
                "INSERT INTO profiles (name, description, device_count, data) VALUES (?,?,?,?)",
                (name, description, device_count, json.dumps(data)),
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles WHERE id=?",
                (cur.lastrowid,),
            ).fetchone())

    def delete_profile(self, profile_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
            conn.commit()
            return cur.rowcount > 0


# ─── Behaviors ────────────────────────────────────────────────────────────────

@dataclass
class SimState:
    time_of_day: float = 12.0
    elapsed_seconds: float = 0.0


class Behavior(ABC):
    @abstractmethod
    def compute(self, state: SimState) -> Union[float, bool]:
        ...


class ConstantBehavior(Behavior):
    def __init__(self, params: dict):
        self.value = params.get("value", 0)

    def compute(self, state: SimState) -> Any:
        if isinstance(self.value, bool):
            return self.value
        return float(self.value)


class SineBehavior(Behavior):
    def __init__(self, params: dict):
        self.base = float(params.get("base", 20.0))
        self.amplitude = float(params.get("amplitude", 5.0))
        self.period_hours = float(params.get("period_hours", 24.0))
        self.phase_hours = float(params.get("phase_hours", 0.0))

    def compute(self, state: SimState) -> float:
        t = state.time_of_day + self.phase_hours
        return self.base + self.amplitude * math.sin(2 * math.pi * t / self.period_hours)


class NoiseBehavior(Behavior):
    def __init__(self, params: dict):
        self.base = float(params.get("base", 0.0))
        self.noise = float(params.get("noise", 1.0))

    def compute(self, state: SimState) -> float:
        return self.base + random.uniform(-self.noise, self.noise)


class RandomWalkBehavior(Behavior):
    def __init__(self, params: dict):
        self._value = float(params.get("value", 50.0))
        self.step = float(params.get("step", 1.0))
        self.min = float(params.get("min", 0.0))
        self.max = float(params.get("max", 100.0))

    def compute(self, state: SimState) -> float:
        self._value = max(self.min, min(self.max, self._value + random.uniform(-self.step, self.step)))
        return self._value


class ManualBehavior(Behavior):
    def __init__(self, params: dict, stored_value: Any = None):
        raw = params.get("value", stored_value)
        if raw is None:
            raw = 0
        if isinstance(raw, bool) or str(raw).lower() in ("true", "false"):
            self._value = raw if isinstance(raw, bool) else str(raw).lower() == "true"
        else:
            self._value = float(raw)

    def set(self, v: Any) -> None:
        self._value = v

    def compute(self, state: SimState) -> Any:
        return self._value


class ScheduleBehavior(Behavior):
    """Returns different values based on time-of-day blocks (occupied/unoccupied scheduling)."""

    @staticmethod
    def _parse_time(t: str) -> float:
        try:
            h, m = t.split(":")
            return int(h) + int(m) / 60.0
        except Exception:
            return 0.0

    def __init__(self, params: dict):
        self.default = float(params.get("default", 0))
        raw_blocks = params.get("blocks", [])
        self.blocks = sorted(
            [{"start": self._parse_time(b.get("start", "00:00")), "value": float(b.get("value", 0))}
             for b in raw_blocks if isinstance(b, dict)],
            key=lambda b: b["start"],
        )

    def compute(self, state: SimState) -> float:
        current = state.time_of_day % 24
        value = self.default
        for block in self.blocks:
            if current >= block["start"]:
                value = block["value"]
            else:
                break
        return value


class RampBehavior(Behavior):
    """Linearly ramps from one value to another over a fixed duration, optionally repeating."""

    def __init__(self, params: dict):
        self.from_val = float(params.get("from", 0))
        self.to_val = float(params.get("to", 100))
        self.duration_seconds = float(params.get("duration_minutes", 60)) * 60
        self.repeat = bool(params.get("repeat", True))

    def compute(self, state: SimState) -> float:
        if self.duration_seconds <= 0:
            return self.to_val
        if self.repeat:
            t = state.elapsed_seconds % self.duration_seconds
        else:
            t = min(state.elapsed_seconds, self.duration_seconds)
        frac = t / self.duration_seconds
        return self.from_val + (self.to_val - self.from_val) * frac


class FaultBehavior(Behavior):
    """Wraps a base behavior and randomly injects fault conditions (spike, stuck, offline)."""

    def __init__(self, params: dict):
        self._base_behavior_name = params.get("base_behavior", "constant")
        self._base_params = params.get("base_params", {"value": 0})
        self._inner: Optional[Behavior] = None
        self.fault_type = params.get("fault_type", "spike")
        self.fault_value = float(params.get("fault_value", 999))
        self.mtbf_minutes = float(params.get("mtbf_minutes", 60))
        self.fault_duration_seconds = float(params.get("fault_duration_seconds", 30))
        self._fault_active = False
        self._fault_end_elapsed: float = -1.0

    def compute(self, state: SimState) -> float:
        if self._inner is None:
            self._inner = make_behavior(self._base_behavior_name, json.dumps(self._base_params))

        if self._fault_active and state.elapsed_seconds > self._fault_end_elapsed:
            self._fault_active = False

        if not self._fault_active:
            prob_per_tick = 1.0 / max(1.0, self.mtbf_minutes * 60.0)
            if random.random() < prob_per_tick:
                self._fault_active = True
                if self.fault_type == "spike":
                    self._fault_end_elapsed = state.elapsed_seconds
                else:
                    self._fault_end_elapsed = state.elapsed_seconds + self.fault_duration_seconds

        if self._fault_active:
            return 0.0 if self.fault_type == "offline" else self.fault_value

        return float(self._inner.compute(state))


def make_behavior(behavior: str, params_json: str, manual_value: Any = None) -> Behavior:
    try:
        params = json.loads(params_json) if params_json else {}
    except Exception:
        params = {}
    if behavior == "constant":
        return ConstantBehavior(params)
    if behavior == "sine":
        return SineBehavior(params)
    if behavior == "noise":
        return NoiseBehavior(params)
    if behavior == "random_walk":
        return RandomWalkBehavior(params)
    if behavior == "manual":
        return ManualBehavior(params, manual_value)
    if behavior == "schedule":
        return ScheduleBehavior(params)
    if behavior == "ramp":
        return RampBehavior(params)
    if behavior == "fault":
        return FaultBehavior(params)
    return ConstantBehavior({"value": 0})


# ─── BACnet Application ───────────────────────────────────────────────────────

def _is_broadcast_address(source) -> bool:
    if source is None:
        return True
    s = str(source)
    return s.endswith(".255:47808") or s in ("*:47808", "<broadcast>:47808")


def _is_device_objid(objid) -> bool:
    if not isinstance(objid, tuple) or len(objid) != 2:
        return False
    t = objid[0]
    return t == "device" or (isinstance(t, int) and t == 8)


def _resolve_base_ip() -> str:
    iface = os.environ.get("BACPYPES_IFACE", "")
    if iface:
        ip = iface.split(":")[0].split("/")[0]
        if ip:
            return ip
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        pass
    return "0.0.0.0"


@bacpypes_debugging
class SimApplication(Application):
    """Multi-device BACnet application — all virtual devices share one UDP socket."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._virtual_devices: dict[int, DeviceObject] = {}
        self._virtual_object_lists: dict[int, list] = {}
        self._sim_engine: Any = None  # set by SimEngine after construction

    def get_object_id(self, objid):
        obj = super().get_object_id(objid)
        if obj is not None:
            return obj
        if _is_device_objid(objid):
            return self._virtual_devices.get(int(objid[1]))
        return None

    async def do_WhoIsRequest(self, apdu) -> None:
        low = apdu.deviceInstanceRangeLowLimit
        high = apdu.deviceInstanceRangeHighLimit
        source = apdu.pduSource
        is_unicast = not _is_broadcast_address(source)
        saved = self.device_object
        try:
            for did, dev_obj in self._virtual_devices.items():
                in_range = (low is None and high is None) or (
                    low is not None and high is not None and low <= did <= high
                )
                if not in_range:
                    continue
                self.device_object = dev_obj
                if is_unicast:
                    self.i_am(address=source)
                else:
                    self.i_am()
        finally:
            self.device_object = saved

    async def do_ReadPropertyRequest(self, apdu) -> None:
        objid = apdu.objectIdentifier
        if _is_device_objid(objid):
            did = int(objid[1])
            virtual = self._virtual_devices.get(did)
            if virtual:
                prop = apdu.propertyIdentifier
                try:
                    prop_code = int(prop)
                except Exception:
                    prop_code = getattr(prop, "value", prop)
                if prop_code == 76:
                    cls = DeviceObject._elements.get("objectList")
                    raw = self._virtual_object_lists.get(did, [])
                    value = cls([ObjectIdentifier(o) for o in raw])
                elif prop_code == 77:
                    value = virtual.objectName
                elif prop_code == 121:
                    value = virtual.vendorName
                elif prop_code == 70:
                    value = virtual.modelName
                elif prop_code == 28:
                    value = virtual.description
                else:
                    await super().do_ReadPropertyRequest(apdu)
                    return
                resp = ReadPropertyACK(
                    objectIdentifier=objid,
                    propertyIdentifier=prop,
                    propertyArrayIndex=apdu.propertyArrayIndex,
                    propertyValue=value,
                    context=apdu,
                )
                await self.response(resp)
                return
        await super().do_ReadPropertyRequest(apdu)

    async def do_WritePropertyRequest(self, apdu) -> None:
        if self._sim_engine is None:
            await super().do_WritePropertyRequest(apdu)
            return

        # Only intercept present-value writes (property identifier 85)
        prop = apdu.propertyIdentifier
        try:
            prop_code = int(prop)
        except Exception:
            prop_code = getattr(prop, "value", None)
        if prop_code != 85:
            await super().do_WritePropertyRequest(apdu)
            return

        # Find the bacpypes3 object
        obj = self.get_object_id(apdu.objectIdentifier)
        if obj is None:
            await super().do_WritePropertyRequest(apdu)
            return

        # Resolve to DB id by object identity
        db_id: Optional[int] = None
        for did, (bobj, _) in self._sim_engine._objects.items():
            if bobj is obj:
                db_id = did
                break
        if db_id is None:
            await super().do_WritePropertyRequest(apdu)
            return

        obj_row = await asyncio.to_thread(self._sim_engine.db.get_object, db_id)
        if not obj_row:
            await super().do_WritePropertyRequest(apdu)
            return

        otype = obj_row["object_type"]
        WRITABLE = {"analog-output", "analog-value", "binary-output", "binary-value"}
        if otype not in WRITABLE:
            await super().do_WritePropertyRequest(apdu)
            return

        # Extract the written value
        try:
            if "analog" in otype:
                value: Any = float(apdu.propertyValue.cast_out(Real))
            else:
                bpv = apdu.propertyValue.cast_out(BinaryPV)
                value = (str(bpv) == "active")
        except Exception as e:
            log.warning("WriteProperty decode error on %s: %s", apdu.objectIdentifier, e)
            await super().do_WritePropertyRequest(apdu)
            return

        # Persist to DB and update in-memory sim
        await self._sim_engine.write_object(db_id, value)
        await self.response(SimpleAckPDU(context=apdu))


# ─── Sim Engine ───────────────────────────────────────────────────────────────

class SimEngine:
    """Manages the running BACnet application and the simulation tick loop."""

    def __init__(self, db: Database):
        self.db = db
        self.state = SimState()
        self.app: Optional[SimApplication] = None
        self.network_port: Optional[NetworkPortObject] = None
        # object DB id → (bacpypes3 object, Behavior)
        self._objects: dict[int, tuple[Any, Behavior]] = {}
        # device instance → slot index (for physical instance offset)
        self._device_slots: dict[int, int] = {}
        self._reload_event = asyncio.Event()
        self._current_values: dict = {}  # for API
        # object DB id → last logged value (for change detection)
        self._prev_values: dict[int, Any] = {}

    async def start(self) -> None:
        devices = await asyncio.to_thread(self.db.get_devices)
        enabled = [d for d in devices if d["enabled"]]
        if not enabled:
            log.info("No enabled devices — BACnet stack idle")
            self.app = None
            return

        base_ip = _resolve_base_ip()
        loop = asyncio.get_running_loop()
        orig = loop.get_exception_handler()

        def _exc_handler(loop, ctx):
            exc = ctx.get("exception")
            if isinstance(exc, RuntimeError) and str(exc) == "no broadcast":
                return
            if orig:
                orig(loop, ctx)
            else:
                loop.default_exception_handler(ctx)

        loop.set_exception_handler(_exc_handler)

        primary = enabled[0]
        bind_addr = f"{base_ip}:{BACNET_PORT}"

        primary_dev_obj = self._make_device_object(primary)
        self.network_port = NetworkPortObject(
            bind_addr,
            objectIdentifier=("network-port", 1),
            objectName="NetworkPort-1",
        )

        self.app = SimApplication.from_object_list([primary_dev_obj, self.network_port])
        self.app._sim_engine = self
        await asyncio.sleep(0.3)

        self.app._virtual_devices[primary["device_instance"]] = primary_dev_obj
        self._device_slots = {d["device_instance"]: i for i, d in enumerate(enabled)}

        log.info("BACnet socket bound to %s", bind_addr)

        for idx, dev in enumerate(enabled):
            slot = idx
            if idx == 0:
                dev_obj = primary_dev_obj
            else:
                dev_obj = self._make_device_object(dev)
                self.app._virtual_devices[dev["device_instance"]] = dev_obj

            objects = await asyncio.to_thread(self.db.get_objects, dev["id"])
            bacnet_ids = [dev_obj.objectIdentifier]
            if idx == 0:
                bacnet_ids.append(self.network_port.objectIdentifier)

            for obj_row in objects:
                if not obj_row["enabled"]:
                    continue
                bacnet_obj, behavior = self._create_object(obj_row, slot, dev["name"])
                self.app.add_object(bacnet_obj)
                self._objects[obj_row["id"]] = (bacnet_obj, behavior)
                bacnet_ids.append(bacnet_obj.objectIdentifier)

            dev_obj.objectList = bacnet_ids
            self.app._virtual_object_lists[dev["device_instance"]] = bacnet_ids
            log.info("Device %d (%s): %d objects", dev["device_instance"], dev["name"], len(objects))

        # Announce all devices
        saved = self.app.device_object
        try:
            for dev_obj in self.app._virtual_devices.values():
                self.app.device_object = dev_obj
                self.app.i_am()
        finally:
            self.app.device_object = saved

    def _make_device_object(self, dev: dict) -> DeviceObject:
        return DeviceObject(
            objectIdentifier=f"device,{dev['device_instance']}",
            objectName=dev["name"],
            vendorIdentifier=999,
            description=dev.get("description", ""),
            modelName=dev.get("model_name", "BACnet Simulator"),
            vendorName=dev.get("vendor_name", "Iotistica"),
            applicationSoftwareVersion="3.0",
            location=dev["name"],
        )

    def _create_object(self, obj_row: dict, slot: int, device_name: str = "") -> tuple[Any, Behavior]:
        phys = slot * 1000 + obj_row["object_instance"]
        behavior = make_behavior(
            obj_row["behavior"],
            obj_row["behavior_params"],
            obj_row.get("manual_value"),
        )
        val = behavior.compute(self.state)
        otype = obj_row["object_type"]
        # BACnet requires globally unique object names within a single application,
        # even across virtual devices — prefix with device name to guarantee uniqueness.
        obj_name = f"{device_name}.{obj_row['name']}" if device_name else obj_row["name"]

        _ANALOG_CLS = {
            "analog-input":  AnalogInputObject,
            "analog-output": AnalogOutputObject,
            "analog-value":  AnalogValueObject,
        }
        _BINARY_CLS = {
            "binary-input":  BinaryInputObject,
            "binary-output": BinaryOutputObject,
            "binary-value":  BinaryValueObject,
        }
        if otype in _ANALOG_CLS:
            units_str = obj_row.get("units") or "no-units"
            try:
                units = EngineeringUnits(units_str)
            except Exception:
                units = EngineeringUnits("no-units")
            bacnet_obj = _ANALOG_CLS[otype](
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                presentValue=Real(float(val)),
                units=units,
            )
        elif otype == "binary-output":
            # BinaryOutputObject is a bacpypes3 commandable type — its present-value is
            # priority-array-derived so setting presentValue= in the constructor raises
            # (silently aborting add_object during reload).  Set relinquishDefault instead:
            # bacpypes3 returns relinquishDefault when no priority-array command is active,
            # so ReadProperty(present-value) works correctly without a real command write.
            active = bool(val) if not isinstance(val, bool) else val
            bacnet_obj = BinaryOutputObject(
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                relinquishDefault=BinaryPV("active" if active else "inactive"),
            )
        else:
            active = bool(val) if not isinstance(val, bool) else val
            cls = _BINARY_CLS.get(otype, BinaryInputObject)
            bacnet_obj = cls(
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                presentValue=BinaryPV("active" if active else "inactive"),
            )
        return bacnet_obj, behavior

    def _update_value(self, bacnet_obj: Any, otype: str, val: Any) -> None:
        if otype in ("analog-input", "analog-output", "analog-value"):
            bacnet_obj.presentValue = Real(float(val))
        elif otype == "binary-output":
            # Commandable object: update via relinquishDefault so ReadProperty returns
            # the correct value when no priority-array command is active.
            active = bool(val) if not isinstance(val, bool) else val
            bacnet_obj.relinquishDefault = BinaryPV("active" if active else "inactive")
        else:
            active = bool(val) if not isinstance(val, bool) else val
            bacnet_obj.presentValue = BinaryPV("active" if active else "inactive")

    async def tick(self) -> None:
        """Advance sim state and update all object values."""
        self.state.elapsed_seconds += 5
        self.state.time_of_day = (self.state.time_of_day + 5 / 3600) % 24

        snapshot: dict[int, dict] = {}
        devices = await asyncio.to_thread(self.db.get_devices)
        dev_map = {d["id"]: d for d in devices}

        for obj_id, (bacnet_obj, behavior) in self._objects.items():
            obj_row = await asyncio.to_thread(self.db.get_object, obj_id)
            if not obj_row:
                continue
            dev = dev_map.get(obj_row["device_id"])
            if not dev:
                continue
            # Rebuild behavior if it changed
            new_b = make_behavior(obj_row["behavior"], obj_row["behavior_params"], obj_row.get("manual_value"))
            if isinstance(new_b, ManualBehavior) and isinstance(behavior, ManualBehavior):
                # Carry state for manual
                new_b.set(behavior._value)
            elif obj_row.get("manual_value") is not None and isinstance(new_b, ManualBehavior):
                new_b.set(obj_row["manual_value"])
            self._objects[obj_id] = (bacnet_obj, new_b)
            val = new_b.compute(self.state)
            self._update_value(bacnet_obj, obj_row["object_type"], val)

            # Log value changes
            prev = self._prev_values.get(obj_id)
            if prev is not None:
                changed = (isinstance(val, bool) and val != prev) or \
                          (isinstance(val, float) and abs(val - prev) >= 0.1)
                if changed:
                    units = obj_row.get("units", "")
                    unit_str = f" {units}" if units and units != "no-units" else ""
                    if isinstance(val, bool):
                        msg = f"{obj_row['name']}: {'ON' if prev else 'OFF'} → {'ON' if val else 'OFF'}"
                    else:
                        msg = f"{obj_row['name']}: {prev:.2f} → {val:.2f}{unit_str}"
                    _log_event(dev["id"], "info", msg)
            self._prev_values[obj_id] = val

            did = dev["device_instance"]
            if did not in snapshot:
                snapshot[did] = {"device_instance": did, "name": dev["name"], "objects": []}
            snapshot[did]["objects"].append({
                "id": obj_id,
                "name": obj_row["name"],
                "object_type": obj_row["object_type"],
                "object_instance": obj_row["object_instance"],
                "value": val,
                "units": obj_row.get("units", ""),
                "behavior": obj_row["behavior"],
            })

        self._current_values = {"devices": list(snapshot.values()), "tick": self.state.elapsed_seconds}

    async def reload(self) -> None:
        """Rebuild the BACnet stack from DB (called after config changes)."""
        log.info("Reloading BACnet stack...")
        if self.app:
            for (bacnet_obj, _) in list(self._objects.values()):
                try:
                    self.app.delete_object(bacnet_obj)
                except Exception:
                    pass
            self._objects.clear()
            self._prev_values.clear()
            self._current_values = {}
            # bacpypes3 doesn't expose a clean shutdown so we just clear our state
            self.app = None
        await self.start()
        log.info("Reload complete")

    async def add_object_hot(self, device_instance: int, obj_row: dict) -> None:
        """Hot-add a single object to the running BACnet app without full reload."""
        if not self.app:
            return
        slot = self._device_slots.get(device_instance, 0)
        dev_obj = self.app._virtual_devices.get(device_instance)
        dev_name = str(dev_obj.objectName) if dev_obj else ""
        bacnet_obj, behavior = self._create_object(obj_row, slot, dev_name)
        self.app.add_object(bacnet_obj)
        self._objects[obj_row["id"]] = (bacnet_obj, behavior)
        if dev_obj:
            existing = list(self.app._virtual_object_lists.get(device_instance, []))
            existing.append(bacnet_obj.objectIdentifier)
            dev_obj.objectList = existing
            self.app._virtual_object_lists[device_instance] = existing

    def set_manual_value(self, obj_id: int, value: Any) -> bool:
        if obj_id not in self._objects:
            return False
        bacnet_obj, behavior = self._objects[obj_id]
        if isinstance(behavior, ManualBehavior):
            behavior.set(value)
        else:
            new_b = ManualBehavior({"value": value})
            self._objects[obj_id] = (bacnet_obj, new_b)
        obj_row = self.db.get_object(obj_id)
        if obj_row:
            self._update_value(bacnet_obj, obj_row["object_type"], value)
        return True

    async def write_object(self, obj_id: int, value: Any) -> bool:
        """Handle a BACnet WriteProperty — switches the object to manual, persists, updates live."""
        if obj_id not in self._objects:
            return False
        bacnet_obj, _ = self._objects[obj_id]
        obj_row = await asyncio.to_thread(self.db.get_object, obj_id)
        if not obj_row:
            return False
        await asyncio.to_thread(self.db.write_object, obj_id, value)
        new_b = ManualBehavior({"value": value})
        self._objects[obj_id] = (bacnet_obj, new_b)
        self._update_value(bacnet_obj, obj_row["object_type"], value)
        return True

    def get_state(self) -> dict:
        return self._current_values


# ─── FastAPI models ───────────────────────────────────────────────────────────

class DeviceCreate(BaseModel):
    device_instance: int = Field(..., ge=1, le=4194302)
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    vendor_name: str = "Iotistica"
    model_name: str = "BACnet Simulator"
    enabled: int = 1


class DeviceUpdate(DeviceCreate):
    pass


class ObjectCreate(BaseModel):
    object_type: str
    object_instance: int = Field(..., ge=0, le=4194302)
    name: str = Field(..., min_length=1, max_length=100)
    units: str = "no-units"
    behavior: str = "constant"
    behavior_params: str = '{"value":0}'
    enabled: int = 1

    def validate_type(self):
        if self.object_type not in VALID_OBJECT_TYPES:
            raise HTTPException(400, f"Invalid object_type. Must be one of: {sorted(VALID_OBJECT_TYPES)}")
        if self.behavior not in VALID_BEHAVIORS:
            raise HTTPException(400, f"Invalid behavior. Must be one of: {sorted(VALID_BEHAVIORS)}")
        try:
            json.loads(self.behavior_params)
        except Exception:
            raise HTTPException(400, "behavior_params must be valid JSON")


class ObjectUpdate(ObjectCreate):
    pass


class SetValueRequest(BaseModel):
    value: Any


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)


class ProfileUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)


class ProfileImport(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    data: dict


# ─── Globals (shared between FastAPI and engine) ──────────────────────────────

db: Database = None  # type: ignore
engine: SimEngine = None  # type: ignore
ws_clients: list[WebSocket] = []

# ─── Per-device event log ─────────────────────────────────────────────────────
_device_logs: dict[int, deque] = {}
_MAX_LOG = 300


def _log_event(device_id: int, level: str, message: str) -> None:
    if device_id not in _device_logs:
        _device_logs[device_id] = deque(maxlen=_MAX_LOG)
    _device_logs[device_id].append({"ts": time.time(), "level": level, "message": message})


# ─── WebSocket broadcaster ────────────────────────────────────────────────────

async def broadcast_state() -> None:
    if not ws_clients:
        return
    data = json.dumps(engine.get_state())
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


# ─── Background tasks ─────────────────────────────────────────────────────────

async def tick_loop() -> None:
    while True:
        await asyncio.sleep(5)
        try:
            await engine.tick()
            await broadcast_state()
            state = engine.get_state()
            for dev in state.get("devices", []):
                vals = "  ".join(
                    f"{o['name']}={o['value']:.2f}" if isinstance(o["value"], float) else f"{o['name']}={o['value']}"
                    for o in dev["objects"]
                )
                log.info("[%s]  %s", dev["name"], vals)
        except Exception as e:
            log.error("Tick error: %s", e)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, engine
    db = Database(DB_PATH)
    await asyncio.to_thread(db.setup)
    await asyncio.to_thread(db.seed_default)
    engine = SimEngine(db)
    await engine.start()
    asyncio.create_task(tick_loop())
    log.info("BACnet Simulator API ready on port %d", SIM_API_PORT)
    yield
    log.info("Shutting down")


# ─── FastAPI app ──────────────────────────────────────────────────────────────

api = FastAPI(title="BACnet Simulator", lifespan=lifespan)
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


ADMIN_DIST = Path(__file__).parent / "admin" / "dist"
ADMIN_PUBLIC = Path(__file__).parent / "admin" / "public"


@api.get("/", include_in_schema=False)
async def admin_root():
    f = ADMIN_DIST / "index.html"
    if f.exists():
        return FileResponse(str(f), media_type="text/html")
    return {"message": "Admin not built. Run: cd admin && npm ci && npm run build"}


@api.get("/favicon.svg", include_in_schema=False)
async def admin_favicon():
    f = ADMIN_PUBLIC / "favicon.svg"
    if f.exists():
        return FileResponse(str(f), media_type="image/svg+xml")
    f = ADMIN_DIST / "favicon.svg"
    if f.exists():
        return FileResponse(str(f), media_type="image/svg+xml")
    from fastapi import HTTPException
    raise HTTPException(status_code=404)


@api.get("/bacnet-vendors.json", include_in_schema=False)
async def bacnet_vendors():
    f = ADMIN_DIST / "bacnet-vendors.json"
    if f.exists():
        return FileResponse(str(f), media_type="application/json")
    return JSONResponse({"vendors": []})


@api.get("/health")
async def health():
    devices = await asyncio.to_thread(db.get_devices)
    return {
        "status": "ok",
        "devices": len(devices),
        "bacnet_running": engine.app is not None,
    }


@api.get("/meta")
async def meta():
    return {
        "object_types": sorted(VALID_OBJECT_TYPES),
        "behaviors": sorted(VALID_BEHAVIORS),
        "units": BACNET_UNITS,
    }


# ── Devices ──

@api.get("/devices")
async def list_devices():
    return await asyncio.to_thread(db.get_devices)


@api.post("/devices", status_code=201)
async def create_device(body: DeviceCreate):
    try:
        device = await asyncio.to_thread(db.create_device, body.model_dump())
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"Device instance {body.device_instance} already exists")
    _log_event(device["id"], "info", f"Device created: {device['name']} (instance {device['device_instance']})")
    asyncio.create_task(engine.reload())
    return device


@api.get("/devices/{device_id}")
async def get_device(device_id: int):
    d = await asyncio.to_thread(db.get_device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")
    return d


@api.put("/devices/{device_id}")
async def update_device(device_id: int, body: DeviceUpdate):
    d = await asyncio.to_thread(db.get_device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")
    try:
        updated = await asyncio.to_thread(db.update_device, device_id, body.model_dump())
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"Device instance {body.device_instance} already exists")
    enabled_changed = d["enabled"] != body.enabled
    if enabled_changed:
        _log_event(device_id, "info", f"Device {'enabled' if body.enabled else 'disabled'}")
    elif d["name"] != body.name:
        _log_event(device_id, "info", f"Device renamed to '{body.name}'")
    else:
        _log_event(device_id, "info", "Device configuration updated")
    asyncio.create_task(engine.reload())
    return updated


@api.delete("/devices/{device_id}", status_code=204)
async def delete_device(device_id: int):
    deleted = await asyncio.to_thread(db.delete_device, device_id)
    if not deleted:
        raise HTTPException(404, "Device not found")
    asyncio.create_task(engine.reload())


# ── Objects ──

@api.get("/devices/{device_id}/objects")
async def list_objects(device_id: int):
    d = await asyncio.to_thread(db.get_device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")
    return await asyncio.to_thread(db.get_objects, device_id)


@api.post("/devices/{device_id}/objects", status_code=201)
async def create_object(device_id: int, body: ObjectCreate):
    body.validate_type()
    d = await asyncio.to_thread(db.get_device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")
    try:
        obj = await asyncio.to_thread(db.create_object, device_id, body.model_dump())
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"Object {body.object_type},{body.object_instance} already exists on this device")
    _log_event(device_id, "info", f"Object added: {body.name} ({body.object_type}:{body.object_instance})")
    if d["enabled"] and body.enabled:
        asyncio.create_task(engine.add_object_hot(d["device_instance"], obj))
    return obj


@api.get("/devices/{device_id}/objects/{obj_id}")
async def get_object(device_id: int, obj_id: int):
    obj = await asyncio.to_thread(db.get_object, obj_id)
    if not obj or obj["device_id"] != device_id:
        raise HTTPException(404, "Object not found")
    return obj


@api.put("/devices/{device_id}/objects/{obj_id}")
async def update_object(device_id: int, obj_id: int, body: ObjectUpdate):
    body.validate_type()
    obj = await asyncio.to_thread(db.get_object, obj_id)
    if not obj or obj["device_id"] != device_id:
        raise HTTPException(404, "Object not found")
    updated = await asyncio.to_thread(db.update_object, obj_id, body.model_dump())
    enabled_changed = obj["enabled"] != body.enabled
    if enabled_changed:
        _log_event(device_id, "info", f"Object {obj['name']}: {'enabled' if body.enabled else 'disabled'}")
    elif obj["behavior"] != body.behavior:
        _log_event(device_id, "info", f"Object {obj['name']}: behavior changed to {body.behavior}")
    else:
        _log_event(device_id, "info", f"Object {obj['name']}: configuration updated")
    asyncio.create_task(engine.reload())
    return updated


@api.delete("/devices/{device_id}/objects/{obj_id}", status_code=204)
async def delete_object(device_id: int, obj_id: int):
    obj = await asyncio.to_thread(db.get_object, obj_id)
    if not obj or obj["device_id"] != device_id:
        raise HTTPException(404, "Object not found")
    _log_event(device_id, "warn", f"Object removed: {obj['name']} ({obj['object_type']}:{obj['object_instance']})")
    await asyncio.to_thread(db.delete_object, obj_id)
    asyncio.create_task(engine.reload())


@api.get("/devices/{device_id}/logs")
async def get_device_logs(device_id: int, limit: int = 100):
    entries = list(_device_logs.get(device_id, []))
    return entries[-limit:]


@api.post("/devices/{device_id}/objects/{obj_id}/value")
async def set_object_value(device_id: int, obj_id: int, body: SetValueRequest):
    obj = await asyncio.to_thread(db.get_object, obj_id)
    if not obj or obj["device_id"] != device_id:
        raise HTTPException(404, "Object not found")
    await asyncio.to_thread(db.set_manual_value, obj_id, body.value)
    engine.set_manual_value(obj_id, body.value)
    val_str = str(body.value) + (f" {obj['units']}" if obj.get("units") and obj["units"] != "no-units" else "")
    _log_event(device_id, "info", f"Manual override: {obj['name']} → {val_str}")
    return {"ok": True}


# ── Profiles ──

@api.get("/profiles")
async def list_profiles():
    return await asyncio.to_thread(db.get_profiles)


@api.post("/profiles", status_code=201)
async def save_profile(body: ProfileCreate):
    return await asyncio.to_thread(db.save_profile, body.name, body.description)


@api.put("/profiles/{profile_id}")
async def update_profile(profile_id: int, body: ProfileUpdate):
    ok = await asyncio.to_thread(db.update_profile, profile_id, body.name, body.description)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"ok": True}


@api.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: int):
    deleted = await asyncio.to_thread(db.delete_profile, profile_id)
    if not deleted:
        raise HTTPException(404, "Profile not found")


@api.post("/profiles/{profile_id}/load")
async def load_profile(profile_id: int):
    ok = await asyncio.to_thread(db.load_profile, profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    asyncio.create_task(engine.reload())
    return {"ok": True}


@api.get("/profiles/{profile_id}/export")
async def export_profile(profile_id: int):
    row = await asyncio.to_thread(db.get_profile, profile_id)
    if not row:
        raise HTTPException(404, "Profile not found")
    content = json.dumps(json.loads(row["data"]), indent=2)
    filename = row["name"].replace(" ", "_") + ".json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@api.post("/profiles/import", status_code=201)
async def import_profile(body: ProfileImport):
    return await asyncio.to_thread(db.import_profile, body.name, body.description, body.data)


# ── State + reload ──

@api.get("/state")
async def get_state():
    return engine.get_state()


@api.post("/reload")
async def reload():
    asyncio.create_task(engine.reload())
    return {"ok": True, "message": "Reload scheduled"}


# ── WebSocket ──

@api.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        # Send current state immediately on connect
        await websocket.send_text(json.dumps(engine.get_state()))
        while True:
            await websocket.receive_text()  # keep alive (ping)
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


# ── Admin static assets (Vite build output) ──
# Must be mounted after all API routes so API paths take precedence.
_assets_dir = ADMIN_DIST / "assets"
if _assets_dir.exists():
    api.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="admin-assets")


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main():
    config = uvicorn.Config(api, host="0.0.0.0", port=SIM_API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
