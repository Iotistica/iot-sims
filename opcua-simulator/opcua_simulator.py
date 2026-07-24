#!/usr/bin/env python3
"""
OPC UA Simulator with REST + WebSocket management API.

Serves a live OPC UA server plus a management API in one process — mirrors
the sibling BACnet simulator's architecture (bacnet-simulator/bacnet_simulator.py):
one FastAPI app whose lifespan starts the OPC UA server + tick loop as a
background task in the same event loop, so REST handlers can mutate live
node state directly (confirmed safe via a throwaway spike: asyncua's
add_variable()/Node.delete() both work correctly after the server has
started and a client is already connected).

Device/object config is persisted in SQLite so it survives restarts and can
be edited live via a management UI (admin frontend is a separate follow-up).
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from asyncua import Server, ua
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from lib.behaviors import (
    TICK_SECONDS,
    VALID_BEHAVIORS,
    FaultBehavior,
    ManualBehavior,
    RandomWalkBehavior,
    SimState,
    make_behavior,
)
from lib.db import (
    VALID_DATA_TYPES,
    Database,
    create_access_token,
    hash_password,
    user_from_token,
    verify_password,
)
from lib.nodes import NodeManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("opcua-sim")

# ─── Constants ────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "opcua_sim.db"
SIM_API_PORT = int(os.environ.get("SIM_API_PORT", "47901"))
OPCUA_PORT = int(os.environ.get("OPCUA_PORT", "4840"))
OPCUA_ENDPOINT = f"opc.tcp://0.0.0.0:{OPCUA_PORT}/opcua-simulator/"


# ─── Simulation engine ─────────────────────────────────────────────────────────

class SimEngine:
    """Owns the live OPC UA server, the node registry, and the simulation tick loop."""

    def __init__(self, db: Database):
        self.db = db
        self.state = SimState()
        self.server: Optional[Server] = None
        self.node_manager: Optional[NodeManager] = None
        self._tag_behaviors: dict[int, Any] = {}   # tag id -> live Behavior instance
        self._history: dict[int, deque] = {}        # tag id -> rolling 1h history, never persisted
        self._current_values: dict = {}
        # Independent of self.server (the OPC UA endpoint) — nodes stay
        # reachable and hold their last value while paused/stopped.
        self.clock_state: str = "running"

    def pause(self) -> None:
        self.clock_state = "paused"

    def resume(self) -> None:
        self.clock_state = "running"

    def reset(self) -> None:
        """Stop the clock and rewind simulated time/history back to the start."""
        self.clock_state = "stopped"
        self.state.elapsed_seconds = 0.0
        self.state.time_of_day = 12.0
        self._history.clear()
        for behavior in self._tag_behaviors.values():
            if isinstance(behavior, FaultBehavior):
                behavior._fault_active = False
                behavior._fault_end_elapsed = -1.0

    async def start(self) -> None:
        self.server = Server()
        await self.server.init()
        self.server.set_endpoint(OPCUA_ENDPOINT)
        self.server.set_server_name("Iotistica OPC UA Simulator")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

        self.node_manager = NodeManager(self.server)
        await self.node_manager.register_namespace()

        devices = await asyncio.to_thread(self.db.get_devices)
        for dev in devices:
            if not dev["enabled"]:
                continue
            await self.node_manager.create_device(dev)
            tags = await asyncio.to_thread(self.db.get_tags, dev["id"])
            for tag in tags:
                if tag["enabled"]:
                    await self._create_live_tag(dev["id"], tag)

        await self.server.start()
        log.info("OPC UA server started at %s", OPCUA_ENDPOINT)

    async def stop(self) -> None:
        if self.server:
            await self.server.stop()

    async def _write_value(self, live_tag, val: Any) -> None:
        dt = live_tag.data_type
        if dt == "Boolean":
            v: Any = bool(val)
        elif dt == "Int32":
            v = int(val)
        elif dt == "String":
            v = str(val)
        else:
            v = float(val)
        await live_tag.node.write_value(v)

    async def _create_live_tag(self, device_id: int, tag_row: dict):
        behavior = make_behavior(tag_row["behavior"], tag_row["behavior_params"], tag_row.get("manual_value"))
        live_tag = await self.node_manager.create_tag(device_id, tag_row, behavior)
        val = behavior.compute(self.state)
        await self._write_value(live_tag, val)
        self._tag_behaviors[tag_row["id"]] = behavior
        return live_tag

    async def add_device_live(self, device_row: dict) -> None:
        await self.node_manager.create_device(device_row)

    async def add_tag_live(self, device_id: int, tag_row: dict) -> None:
        await self._create_live_tag(device_id, tag_row)

    async def update_device_live(self, device_id: int, device_row: dict) -> None:
        """Node identity (key) may have changed — delete + recreate the folder
        and everything under it, rather than BACnet's whole-stack-reload
        approach (unnecessary here since asyncua supports true item-level
        mutation, confirmed by the Step 0 spike)."""
        await self.delete_device_live(device_id)
        if device_row["enabled"]:
            await self.node_manager.create_device(device_row)
            tags = await asyncio.to_thread(self.db.get_tags, device_id)
            for tag in tags:
                if tag["enabled"]:
                    await self._create_live_tag(device_id, tag)

    async def update_tag_live(self, device_id: int, tag_id: int, tag_row: dict) -> None:
        await self.delete_tag_live(tag_id)
        if tag_row["enabled"]:
            await self._create_live_tag(device_id, tag_row)

    async def delete_device_live(self, device_id: int) -> None:
        for t in self.node_manager.get_tags_for_device(device_id):
            self._tag_behaviors.pop(t.tag_id, None)
            self._history.pop(t.tag_id, None)
        await self.node_manager.delete_device(device_id)

    async def delete_tag_live(self, tag_id: int) -> None:
        await self.node_manager.delete_tag(tag_id)
        self._tag_behaviors.pop(tag_id, None)
        self._history.pop(tag_id, None)

    def set_manual_value(self, tag_id: int, value: Any) -> bool:
        behavior = self._tag_behaviors.get(tag_id)
        if behavior is None:
            return False
        if isinstance(behavior, ManualBehavior):
            behavior.set(value)
        else:
            self._tag_behaviors[tag_id] = ManualBehavior({"value": value})
        return True

    async def tick(self) -> None:
        """Advance sim state and update every live tag's value."""
        if self.clock_state != "running":
            return

        self.state.elapsed_seconds += TICK_SECONDS
        self.state.time_of_day = (self.state.time_of_day + TICK_SECONDS / 3600) % 24

        snapshot: dict[int, dict] = {}
        devices = await asyncio.to_thread(self.db.get_devices)
        dev_map = {d["id"]: d for d in devices}

        for live_tag in self.node_manager.get_all_tags():
            tag_row = await asyncio.to_thread(self.db.get_tag, live_tag.device_id, live_tag.tag_id)
            if not tag_row:
                continue
            dev = dev_map.get(live_tag.device_id)
            if not dev:
                continue

            # Rebuild behavior fresh from DB (so config edits apply without a
            # node rebuild) but carry stateful internals across ticks.
            prev = self._tag_behaviors.get(live_tag.tag_id)
            new_b = make_behavior(tag_row["behavior"], tag_row["behavior_params"], tag_row.get("manual_value"))
            if isinstance(new_b, ManualBehavior) and isinstance(prev, ManualBehavior):
                new_b.set(prev._value)
            elif tag_row.get("manual_value") is not None and isinstance(new_b, ManualBehavior):
                new_b.set(tag_row["manual_value"])
            if isinstance(new_b, FaultBehavior) and isinstance(prev, FaultBehavior):
                new_b._fault_active = prev._fault_active
                new_b._fault_end_elapsed = prev._fault_end_elapsed
                new_b._inner = prev._inner
            if isinstance(new_b, RandomWalkBehavior) and isinstance(prev, RandomWalkBehavior):
                new_b._value = prev._value
            self._tag_behaviors[live_tag.tag_id] = new_b

            val = new_b.compute(self.state)
            await self._write_value(live_tag, val)

            hist = self._history.setdefault(live_tag.tag_id, deque(maxlen=720))
            hist.append((time.time(), 1.0 if val is True else 0.0 if val is False else float(val)))

            if dev["id"] not in snapshot:
                snapshot[dev["id"]] = {"device_id": dev["id"], "name": dev["name"], "tags": []}
            snapshot[dev["id"]]["tags"].append({
                "id": live_tag.tag_id,
                "name": tag_row["name"],
                "data_type": tag_row["data_type"],
                "value": val,
                "unit": tag_row.get("unit", ""),
                "behavior": tag_row["behavior"],
            })

        self._current_values = {"devices": list(snapshot.values()), "tick": self.state.elapsed_seconds}

    def get_state(self) -> dict:
        return self._current_values

    def get_history(self, tag_id: int) -> list:
        return list(self._history.get(tag_id, []))


# ─── FastAPI request models ───────────────────────────────────────────────────

class DeviceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    manufacturer: str = ""
    model: str = ""
    enabled: bool = True


class DeviceUpdate(DeviceCreate):
    pass


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    data_type: str = "Double"
    writable: bool = False
    unit: str = ""
    behavior: str = "constant"
    behavior_params: str = '{"value":0}'
    enabled: bool = True

    def validate_choices(self) -> None:
        if self.data_type not in VALID_DATA_TYPES:
            raise HTTPException(400, f"data_type must be one of {sorted(VALID_DATA_TYPES)}")
        if self.behavior not in VALID_BEHAVIORS:
            raise HTTPException(400, f"behavior must be one of {sorted(VALID_BEHAVIORS)}")
        try:
            json.loads(self.behavior_params)
        except Exception:
            raise HTTPException(400, "behavior_params must be valid JSON")


class TagUpdate(TagCreate):
    pass


class SetValueRequest(BaseModel):
    value: Any


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""


class ProfileUpdate(ProfileCreate):
    pass


class ProfileImport(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    data: dict


class Credentials(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=200)


class PasswordReset(BaseModel):
    password: str = Field(..., min_length=8, max_length=200)


# ─── Globals (shared between FastAPI and engine) ──────────────────────────────

db: Database = None  # type: ignore
engine: SimEngine = None  # type: ignore
ws_clients: list[WebSocket] = []

_device_logs: dict[int, deque] = {}
_global_log: deque = deque(maxlen=1000)
_device_names: dict[int, str] = {}
_MAX_LOG = 300


def _log_event(device_id: int, level: str, message: str) -> None:
    entry = {
        "ts": time.time(),
        "level": level,
        "device_id": device_id,
        "device_name": _device_names.get(device_id, f"#{device_id}"),
        "message": message,
    }
    _device_logs.setdefault(device_id, deque(maxlen=_MAX_LOG)).append(entry)
    _global_log.append(entry)


def get_current_user(request: Request) -> dict:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = user_from_token(db, auth_header[7:].strip())
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


# ─── WebSocket broadcaster ─────────────────────────────────────────────────────

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


# ─── Background tasks ──────────────────────────────────────────────────────────

async def tick_loop() -> None:
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await engine.tick()
            await broadcast_state()
        except Exception as e:
            log.error("Tick error: %s", e)


# ─── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, engine
    db = Database(DB_PATH)
    await asyncio.to_thread(db.setup)
    for d in db.get_devices():
        _device_names[d["id"]] = d["name"]
    engine = SimEngine(db)
    await engine.start()
    tick_task = asyncio.create_task(tick_loop())
    log.info("OPC UA Simulator API ready on port %d", SIM_API_PORT)
    yield
    log.info("Shutting down")
    tick_task.cancel()
    try:
        await tick_task
    except asyncio.CancelledError:
        pass
    await engine.stop()


# ─── FastAPI app ────────────────────────────────────────────────────────────────

api = FastAPI(title="OPC UA Simulator", lifespan=lifespan)
api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_PUBLIC_PATH_PREFIXES = ("/auth/", "/assets/")
_PUBLIC_PATHS = {"/", "/favicon.svg"}


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PATH_PREFIXES)


@api.middleware("http")
async def auth_gate(request: Request, call_next):
    """Require a valid bearer token for everything except the login/setup flow
    and the static admin SPA shell (see _is_public_path)."""
    if request.method == "OPTIONS" or _is_public_path(request.url.path):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer ") or not user_from_token(db, auth_header[7:].strip()):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return await call_next(request)


ADMIN_DIST = Path(__file__).parent / "admin" / "dist"


@api.get("/", include_in_schema=False)
async def root():
    f = ADMIN_DIST / "index.html"
    if f.exists():
        return FileResponse(f)
    return JSONResponse({"service": "OPC UA Simulator", "status": "ok"})


@api.get("/favicon.svg", include_in_schema=False)
async def favicon():
    f = ADMIN_DIST / "favicon.svg"
    if f.exists():
        return FileResponse(f)
    raise HTTPException(404)


# ── Auth / Users ──

@api.get("/auth/setup-required")
async def auth_setup_required():
    count = await asyncio.to_thread(db.count_users)
    return {"setup_required": count == 0}


@api.post("/auth/setup", status_code=201)
async def auth_setup(body: Credentials):
    count = await asyncio.to_thread(db.count_users)
    if count > 0:
        raise HTTPException(status_code=409, detail="Setup already completed")
    password_hash = hash_password(body.password)
    user = await asyncio.to_thread(db.create_user, body.username, password_hash)
    token = create_access_token(user["id"], user["username"])
    return {"access_token": token, "user": user}


@api.post("/auth/login")
async def auth_login(body: Credentials):
    user = await asyncio.to_thread(db.get_user_by_username, body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    await asyncio.to_thread(db.touch_last_login, user["id"])
    token = create_access_token(user["id"], user["username"])
    return {"access_token": token, "user": {"id": user["id"], "username": user["username"]}}


@api.get("/auth/me")
async def auth_me(current_user: dict = Depends(get_current_user)):
    return current_user


@api.get("/users")
async def list_users():
    return await asyncio.to_thread(db.list_users)


@api.post("/users", status_code=201)
async def create_user(body: Credentials):
    if await asyncio.to_thread(db.get_user_by_username, body.username):
        raise HTTPException(status_code=409, detail="Username already exists")
    password_hash = hash_password(body.password)
    return await asyncio.to_thread(db.create_user, body.username, password_hash)


@api.post("/users/{user_id}/password")
async def reset_user_password(user_id: int, body: PasswordReset):
    if not await asyncio.to_thread(db.get_user, user_id):
        raise HTTPException(status_code=404, detail="User not found")
    password_hash = hash_password(body.password)
    await asyncio.to_thread(db.update_user_password, user_id, password_hash)
    return {"ok": True}


@api.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: int):
    if not await asyncio.to_thread(db.get_user, user_id):
        raise HTTPException(status_code=404, detail="User not found")
    if await asyncio.to_thread(db.count_users) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last remaining user")
    await asyncio.to_thread(db.delete_user, user_id)


# ── Health / meta ──

@api.get("/health")
async def health():
    devices = await asyncio.to_thread(db.get_devices)
    return {
        "status": "ok",
        "devices": len(devices),
        "opcua_running": engine.server is not None,
        "sim_state": engine.clock_state,
        "elapsed_seconds": engine.state.elapsed_seconds,
    }


@api.get("/meta")
async def meta():
    return {
        "data_types": sorted(VALID_DATA_TYPES),
        "behaviors": sorted(VALID_BEHAVIORS),
    }


# ── Sim clock ──

@api.post("/sim/start")
async def sim_start():
    engine.resume()
    return {"sim_state": engine.clock_state}


@api.post("/sim/pause")
async def sim_pause():
    engine.pause()
    return {"sim_state": engine.clock_state}


@api.post("/sim/stop")
async def sim_stop():
    engine.reset()
    return {"sim_state": engine.clock_state, "elapsed_seconds": engine.state.elapsed_seconds}


# ── Devices ──

@api.get("/devices")
async def list_devices():
    return await asyncio.to_thread(db.get_devices)


@api.post("/devices", status_code=201)
async def create_device(body: DeviceCreate):
    device = await asyncio.to_thread(
        db.create_device, body.name, body.description, body.manufacturer, body.model, body.enabled
    )
    _device_names[device["id"]] = device["name"]
    _log_event(device["id"], "info", f"Device created: {device['name']}")
    if body.enabled:
        asyncio.create_task(engine.add_device_live(device))
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
    updated = await asyncio.to_thread(
        db.update_device, device_id, body.name, body.description, body.manufacturer, body.model, body.enabled
    )
    _device_names[device_id] = body.name
    if d["enabled"] != body.enabled:
        _log_event(device_id, "info", f"Device {'enabled' if body.enabled else 'disabled'}")
    elif d["name"] != body.name:
        _log_event(device_id, "info", f"Device renamed to '{body.name}'")
    else:
        _log_event(device_id, "info", "Device configuration updated")
    asyncio.create_task(engine.update_device_live(device_id, updated))
    return updated


@api.delete("/devices/{device_id}", status_code=204)
async def delete_device(device_id: int):
    deleted = await asyncio.to_thread(db.delete_device, device_id)
    if not deleted:
        raise HTTPException(404, "Device not found")
    asyncio.create_task(engine.delete_device_live(device_id))


# ── Tags ──

@api.get("/devices/{device_id}/tags")
async def list_tags(device_id: int):
    d = await asyncio.to_thread(db.get_device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")
    return await asyncio.to_thread(db.get_tags, device_id)


@api.post("/devices/{device_id}/tags", status_code=201)
async def create_tag(device_id: int, body: TagCreate):
    body.validate_choices()
    d = await asyncio.to_thread(db.get_device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")
    try:
        tag = await asyncio.to_thread(
            db.create_tag, device_id, body.name, body.data_type, body.writable,
            body.unit, body.behavior, body.behavior_params, body.enabled,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"Tag '{body.name}' already exists on this device")
    _log_event(device_id, "info", f"Tag added: {body.name} ({body.data_type})")
    if d["enabled"] and body.enabled:
        asyncio.create_task(engine.add_tag_live(device_id, tag))
    return tag


@api.get("/devices/{device_id}/tags/{tag_id}")
async def get_tag(device_id: int, tag_id: int):
    t = await asyncio.to_thread(db.get_tag, device_id, tag_id)
    if not t:
        raise HTTPException(404, "Tag not found")
    return t


@api.put("/devices/{device_id}/tags/{tag_id}")
async def update_tag(device_id: int, tag_id: int, body: TagUpdate):
    body.validate_choices()
    existing = await asyncio.to_thread(db.get_tag, device_id, tag_id)
    if not existing:
        raise HTTPException(404, "Tag not found")
    updated = await asyncio.to_thread(
        db.update_tag, device_id, tag_id, body.name, body.data_type, body.writable,
        body.unit, body.behavior, body.behavior_params, body.enabled,
    )
    if existing["enabled"] != body.enabled:
        _log_event(device_id, "info", f"Tag '{body.name}' {'enabled' if body.enabled else 'disabled'}")
    elif existing["behavior"] != body.behavior:
        _log_event(device_id, "info", f"Tag '{body.name}' behavior changed to {body.behavior}")
    else:
        _log_event(device_id, "info", f"Tag '{body.name}' updated")
    asyncio.create_task(engine.update_tag_live(device_id, tag_id, updated))
    return updated


@api.delete("/devices/{device_id}/tags/{tag_id}", status_code=204)
async def delete_tag(device_id: int, tag_id: int):
    deleted = await asyncio.to_thread(db.delete_tag, device_id, tag_id)
    if not deleted:
        raise HTTPException(404, "Tag not found")
    _log_event(device_id, "warn", f"Tag removed (id {tag_id})")
    asyncio.create_task(engine.delete_tag_live(tag_id))


@api.post("/devices/{device_id}/tags/{tag_id}/value")
async def set_tag_value(device_id: int, tag_id: int, body: SetValueRequest):
    t = await asyncio.to_thread(db.get_tag, device_id, tag_id)
    if not t:
        raise HTTPException(404, "Tag not found")
    await asyncio.to_thread(db.set_manual_value, device_id, tag_id, body.value)
    engine.set_manual_value(tag_id, body.value)
    _log_event(device_id, "info", f"Manual override: '{t['name']}' = {body.value}")
    return {"ok": True}


@api.get("/devices/{device_id}/tags/{tag_id}/history")
async def tag_history(device_id: int, tag_id: int):
    return [{"ts": ts, "value": v} for ts, v in engine.get_history(tag_id)]


# ── Logs ──

@api.get("/devices/{device_id}/logs")
async def device_logs(device_id: int, limit: int = 100):
    entries = list(_device_logs.get(device_id, []))
    return entries[-limit:]


@api.get("/logs")
async def all_logs(limit: int = 200):
    entries = list(_global_log)
    return entries[-limit:]


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
    row = await asyncio.to_thread(db.get_profile, profile_id)
    if not row:
        raise HTTPException(404, "Profile not found")
    data = json.loads(row["data"])

    # Tear down every live node before replacing DB state, then rebuild from scratch.
    for dev in await asyncio.to_thread(db.get_devices):
        await engine.delete_device_live(dev["id"])
    await asyncio.to_thread(db.replace_live_state, data)

    for dev in await asyncio.to_thread(db.get_devices):
        if not dev["enabled"]:
            continue
        await engine.add_device_live(dev)
        for tag in await asyncio.to_thread(db.get_tags, dev["id"]):
            if tag["enabled"]:
                await engine.add_tag_live(dev["id"], tag)
        _device_names[dev["id"]] = dev["name"]

    # A freshly loaded profile starts paused at t=0 — press Start when ready.
    engine.reset()
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


# ── State ──

@api.get("/state")
async def get_state():
    return engine.get_state()


# ── WebSocket ──

@api.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # Browsers can't set custom headers on the WS handshake, so the token
    # travels as a query param instead of Authorization: Bearer.
    token = websocket.query_params.get("token", "")
    if not await asyncio.to_thread(user_from_token, db, token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    ws_clients.append(websocket)
    try:
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


# ─── Entry point ────────────────────────────────────────────────────────────────

async def main():
    config = uvicorn.Config(api, host="0.0.0.0", port=SIM_API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
