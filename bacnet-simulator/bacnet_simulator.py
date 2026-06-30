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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from bacpypes3.local.device import DeviceObject
from bacpypes3.local.analog import AnalogInputObject
from bacpypes3.local.binary import BinaryInputObject
from bacpypes3.app import Application
from bacpypes3.primitivedata import Real, ObjectIdentifier
from bacpypes3.basetypes import EngineeringUnits, BinaryPV
from bacpypes3.debugging import bacpypes_debugging, ModuleLogger
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.apdu import ReadPropertyACK

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

VALID_BEHAVIORS = {"constant", "sine", "noise", "random_walk", "manual"}

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
            """)
        log.info("Database ready at %s", self.path)

    def seed_default(self) -> None:
        """Populate with condo building if DB is empty."""
        with self._conn() as conn:
            if conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] > 0:
                return
            conn.executemany(
                "INSERT OR IGNORE INTO devices (device_instance, name, description) VALUES (?,?,?)",
                [
                    (1001, "Central-Plant", "Central chiller plant"),
                    (1002, "AHU-1-Controller", "Air handling unit 1 controller"),
                    (1003, "AHU-2-Controller", "Air handling unit 2 controller"),
                ],
            )
            p = conn.execute("SELECT id FROM devices WHERE device_instance=1001").fetchone()[0]
            a1 = conn.execute("SELECT id FROM devices WHERE device_instance=1002").fetchone()[0]
            a2 = conn.execute("SELECT id FROM devices WHERE device_instance=1003").fetchone()[0]
            objects = [
                (p, "binary-input", 1, "Chiller Status",    "no-units",        "manual",      '{"value":true}'),
                (p, "analog-input", 2, "Supply Temp",        "degrees-celsius", "noise",       '{"base":7.0,"noise":0.3}'),
                (p, "analog-input", 3, "Return Temp",        "degrees-celsius", "noise",       '{"base":12.0,"noise":0.3}'),
                (p, "analog-input", 4, "Power",              "kilowatts",       "random_walk", '{"value":65,"step":2,"min":25,"max":85}'),
                (a1,"binary-input", 1, "Fan Status",         "no-units",        "manual",      '{"value":true}'),
                (a1,"analog-input", 2, "Supply Temp",        "degrees-celsius", "noise",       '{"base":18.0,"noise":1.0}'),
                (a1,"analog-input", 3, "Return Temp",        "degrees-celsius", "sine",        '{"base":22.0,"amplitude":3.0,"period_hours":24}'),
                (a1,"analog-input", 4, "Airflow",            "cubic-feet-per-minute","noise",  '{"base":5000,"noise":200}'),
                (a1,"analog-input", 5, "Cooling Valve",      "percent",         "sine",        '{"base":45.0,"amplitude":20.0,"period_hours":12}'),
                (a2,"binary-input", 1, "Fan Status",         "no-units",        "manual",      '{"value":true}'),
                (a2,"analog-input", 2, "Supply Temp",        "degrees-celsius", "noise",       '{"base":18.5,"noise":1.0}'),
                (a2,"analog-input", 3, "Return Temp",        "degrees-celsius", "sine",        '{"base":22.5,"amplitude":3.0,"period_hours":24}'),
                (a2,"analog-input", 4, "Airflow",            "cubic-feet-per-minute","noise",  '{"base":4500,"noise":150}'),
                (a2,"analog-input", 5, "Cooling Valve",      "percent",         "sine",        '{"base":40.0,"amplitude":18.0,"period_hours":12}'),
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO objects "
                "(device_id, object_type, object_instance, name, units, behavior, behavior_params) "
                "VALUES (?,?,?,?,?,?,?)",
                objects,
            )
        log.info("Seeded default condo-building data")

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

    def delete_object(self, obj_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM objects WHERE id=?", (obj_id,))
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
                bacnet_obj, behavior = self._create_object(obj_row, slot)
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

    def _create_object(self, obj_row: dict, slot: int) -> tuple[Any, Behavior]:
        phys = slot * 1000 + obj_row["object_instance"]
        behavior = make_behavior(
            obj_row["behavior"],
            obj_row["behavior_params"],
            obj_row.get("manual_value"),
        )
        val = behavior.compute(self.state)
        otype = obj_row["object_type"]

        if otype in ("analog-input", "analog-output", "analog-value"):
            units_str = obj_row.get("units") or "no-units"
            try:
                units = EngineeringUnits(units_str)
            except Exception:
                units = EngineeringUnits("no-units")
            bacnet_obj = AnalogInputObject(
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_row["name"],
                presentValue=Real(float(val)),
                units=units,
            )
        else:
            active = bool(val) if not isinstance(val, bool) else val
            bacnet_obj = BinaryInputObject(
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_row["name"],
                presentValue=BinaryPV("active" if active else "inactive"),
            )
        return bacnet_obj, behavior

    def _update_value(self, bacnet_obj: Any, otype: str, val: Any) -> None:
        if otype in ("analog-input", "analog-output", "analog-value"):
            bacnet_obj.presentValue = Real(float(val))
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
        bacnet_obj, behavior = self._create_object(obj_row, slot)
        self.app.add_object(bacnet_obj)
        self._objects[obj_row["id"]] = (bacnet_obj, behavior)
        # Update device object list
        dev_obj = self.app._virtual_devices.get(device_instance)
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


# ─── Globals (shared between FastAPI and engine) ──────────────────────────────

db: Database = None  # type: ignore
engine: SimEngine = None  # type: ignore
ws_clients: list[WebSocket] = []


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
    # Hot-add the device to the running stack
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
    # Try hot-add
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
    asyncio.create_task(engine.reload())
    return updated


@api.delete("/devices/{device_id}/objects/{obj_id}", status_code=204)
async def delete_object(device_id: int, obj_id: int):
    obj = await asyncio.to_thread(db.get_object, obj_id)
    if not obj or obj["device_id"] != device_id:
        raise HTTPException(404, "Object not found")
    await asyncio.to_thread(db.delete_object, obj_id)
    asyncio.create_task(engine.reload())


@api.post("/devices/{device_id}/objects/{obj_id}/value")
async def set_object_value(device_id: int, obj_id: int, body: SetValueRequest):
    obj = await asyncio.to_thread(db.get_object, obj_id)
    if not obj or obj["device_id"] != device_id:
        raise HTTPException(404, "Object not found")
    await asyncio.to_thread(db.set_manual_value, obj_id, body.value)
    engine.set_manual_value(obj_id, body.value)
    return {"ok": True}


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


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main():
    config = uvicorn.Config(api, host="0.0.0.0", port=SIM_API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
