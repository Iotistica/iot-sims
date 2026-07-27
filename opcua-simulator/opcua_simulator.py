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
be edited live via a management UI.

Route handlers live in lib/routes_*.py (FastAPI APIRouters) — this file kept
growing into a single-file monolith as they were added one at a time; they
now share state via lib/state.py instead of this module's own globals. What
stays here: the SimEngine itself (owns the live OPC UA server + tick loop,
needed by nearly every router), the FastAPI app/middleware/lifespan wiring,
and the handful of routes (health, sim clock, device-value /state + /ws)
that are simple enough not to warrant their own file.
"""
import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from asyncua import Server, ua
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import lib.state as state
from lib.analytics import Metrics, install_analytics, record_fault_transition
from lib.behaviors import (
    TICK_SECONDS,
    VALID_BEHAVIORS,
    FaultBehavior,
    ManualBehavior,
    RandomWalkBehavior,
    SimState,
    make_behavior,
)
from lib.db import Database, is_effectively_enabled, user_from_token
from lib.nodes import NodeManager
from lib import routes_analytics, routes_auth, routes_devices, routes_folders, routes_nodesets, routes_profiles

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
        # Serializes structural live-node mutations (add/delete folder/device/
        # tag, bulk NodeSet import) against each other and against tick()'s
        # walk over the node registry — without this, a bulk import running
        # concurrently with the 5s tick loop can hit "dict changed size
        # during iteration" or write to a node mid-delete.
        self.structural_lock = asyncio.Lock()

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

        # Must be installed before server.start() so every hook is live
        # before any client can connect — see lib/analytics.py.
        install_analytics(self.server, state.metrics)

        await self._load_all_from_db()

        await self.server.start()
        log.info("OPC UA server started at %s", OPCUA_ENDPOINT)

    async def stop(self) -> None:
        if self.server:
            await self.server.stop()

    def _resolve_parent_node(self, folder_id: Optional[int]):
        """None (root, under Objects) or another live folder's own node —
        the caller is responsible for creation order (a folder's parent must
        already be live) since only the caller knows what order it's
        iterating in."""
        if folder_id is None:
            return None
        live_folder = self.node_manager.get_folder(folder_id)
        return live_folder.node if live_folder else None

    async def _load_all_from_db(self) -> None:
        """Create live nodes for every folder, and every effectively-enabled
        device/tag, currently in the DB. Used both at startup and by
        rebuild_live_state() — the recovery path after a partial structural
        mutation failure (e.g. a NodeSet import that fails partway through
        live node creation, or a folder move)."""
        folders = await asyncio.to_thread(self.db.get_folders)
        folders_by_id = {f["id"]: f for f in folders}

        # Folders reference their own parent by id — create root-first
        # (parent_folder_id is None or already live) so add_folder() always
        # has a real parent Node to attach to.
        remaining = list(folders)
        guard = 0
        while remaining and guard <= len(folders) + 1:
            guard += 1
            still_remaining = []
            for f in remaining:
                parent_id = f["parent_folder_id"]
                if parent_id is not None and self.node_manager.get_folder(parent_id) is None:
                    still_remaining.append(f)
                    continue
                await self.node_manager.create_folder(f, self._resolve_parent_node(parent_id))
            remaining = still_remaining

        devices = await asyncio.to_thread(self.db.get_devices)
        for dev in devices:
            if not is_effectively_enabled(dev, folders_by_id):
                continue
            await self.node_manager.create_device(dev, self._resolve_parent_node(dev.get("folder_id")))
            tags = await asyncio.to_thread(self.db.get_tags, dev["id"])
            for tag in tags:
                if tag["enabled"]:
                    await self._create_live_tag(dev["id"], tag)

    async def rebuild_live_state(self) -> None:
        """Tear down every live node and recreate it from persisted DB
        state. Callers must hold self.structural_lock. This is the recovery
        mechanism when a structural mutation fails partway through and the
        live address space can no longer be trusted to match the DB — also
        reused for folder moves and profile load, where the alternative
        (a scoped delete+recreate) risks orphaning live devices/sub-folders
        registered separately from the folder/device being moved."""
        for dev in list(self.node_manager.get_all_devices()):
            self._tag_behaviors.pop(dev.device_id, None)
            for t in self.node_manager.get_tags_for_device(dev.device_id):
                self._tag_behaviors.pop(t.tag_id, None)
                self._history.pop(t.tag_id, None)
            await self.node_manager.delete_device(dev.device_id)

        # Folders: leaf-first (a parent's recursive delete must never race a
        # child's own already-completed one). Parent/child derived from the
        # DB, not the live registry (LiveFolder doesn't track parent) — any
        # live folder no longer present in the DB is treated as a leaf and
        # deleted first, which is always safe.
        db_folders_by_id = {f["id"]: f for f in await asyncio.to_thread(self.db.get_folders)}
        live_folder_ids = {lf.folder_id for lf in self.node_manager.get_all_folders()}
        child_count: dict[int, int] = {fid: 0 for fid in live_folder_ids}
        for fid in live_folder_ids:
            parent = db_folders_by_id.get(fid, {}).get("parent_folder_id")
            if parent in child_count:
                child_count[parent] += 1
        queue = [fid for fid, c in child_count.items() if c == 0]
        while queue:
            fid = queue.pop()
            parent = db_folders_by_id.get(fid, {}).get("parent_folder_id")
            await self.node_manager.delete_folder(fid)
            if parent in child_count:
                child_count[parent] -= 1
                if child_count[parent] == 0:
                    queue.append(parent)

        await self._load_all_from_db()
        log.info("Live OPC UA address space rebuilt from persisted DB state")

    async def resync_live_state(self) -> None:
        """Locked wrapper around rebuild_live_state() for callers outside
        the engine (folder move, profile load) that don't already hold
        structural_lock."""
        async with self.structural_lock:
            await self.rebuild_live_state()

    async def _write_value(self, live_tag, val: Any) -> None:
        dt = live_tag.data_type
        if dt == "Boolean":
            await live_tag.node.write_value(bool(val), varianttype=ua.VariantType.Boolean)
        elif dt == "Int32":
            # asyncua infers Int64 for a plain Python int, which mismatches
            # the node's actual Int32 variant type (created via lib/nodes.py)
            # and gets the write rejected with BadTypeMismatch — the type
            # must be explicit here.
            await live_tag.node.write_value(int(val), varianttype=ua.VariantType.Int32)
        elif dt == "String":
            await live_tag.node.write_value(str(val), varianttype=ua.VariantType.String)
        else:
            await live_tag.node.write_value(float(val), varianttype=ua.VariantType.Double)

    async def _create_live_tag(self, device_id: int, tag_row: dict):
        behavior = make_behavior(tag_row["behavior"], tag_row["behavior_params"], tag_row.get("manual_value"))
        live_tag = await self.node_manager.create_tag(device_id, tag_row, behavior)
        val = behavior.compute(self.state)
        await self._write_value(live_tag, val)
        self._tag_behaviors[tag_row["id"]] = behavior
        # This initial compute() can itself flip a fresh FaultBehavior active
        # before tick()'s own before/after edge-detection ever runs on it —
        # without this, that first activation would go unrecorded (was_active
        # is unconditionally False here since the tag has no prior state).
        if isinstance(behavior, FaultBehavior) and behavior._fault_active:
            dev = await asyncio.to_thread(self.db.get_device, device_id)
            record_fault_transition(
                state.metrics, tag_row["id"], tag_row["name"], dev["name"] if dev else f"#{device_id}",
                True, False, behavior.fault_type,
            )
        return live_tag

    async def add_device_live(self, device_row: dict) -> None:
        async with self.structural_lock:
            await self._add_device_live_locked(device_row)

    async def add_tag_live(self, device_id: int, tag_row: dict) -> None:
        async with self.structural_lock:
            await self._create_live_tag(device_id, tag_row)

    async def _add_device_live_locked(self, device_row: dict) -> None:
        """Caller must hold self.structural_lock — used directly by callers
        (e.g. NodeSet import) that are already holding it for a whole batch,
        since asyncio.Lock isn't reentrant."""
        await self.node_manager.create_device(device_row, self._resolve_parent_node(device_row.get("folder_id")))

    async def update_device_live(self, device_id: int, device_row: dict) -> None:
        """Node identity is now a stable id-based NodeId (lib/nodes.py), not
        derived from name/key/folder — but asyncua still has no in-place
        "move" primitive, and a device's folder_id may have changed, so this
        stays delete + recreate rather than an in-place attribute edit."""
        async with self.structural_lock:
            await self._delete_device_live_locked(device_id)
            folders_by_id = {f["id"]: f for f in await asyncio.to_thread(self.db.get_folders)}
            if is_effectively_enabled(device_row, folders_by_id):
                await self.node_manager.create_device(
                    device_row, self._resolve_parent_node(device_row.get("folder_id"))
                )
                tags = await asyncio.to_thread(self.db.get_tags, device_id)
                for tag in tags:
                    if tag["enabled"]:
                        await self._create_live_tag(device_id, tag)

    async def update_tag_live(self, device_id: int, tag_id: int, tag_row: dict) -> None:
        async with self.structural_lock:
            await self._delete_tag_live_locked(tag_id)
            if tag_row["enabled"]:
                await self._create_live_tag(device_id, tag_row)

    async def delete_device_live(self, device_id: int) -> None:
        async with self.structural_lock:
            await self._delete_device_live_locked(device_id)

    async def delete_tag_live(self, tag_id: int) -> None:
        async with self.structural_lock:
            await self._delete_tag_live_locked(tag_id)

    async def _delete_device_live_locked(self, device_id: int) -> None:
        """Caller must hold self.structural_lock."""
        for t in self.node_manager.get_tags_for_device(device_id):
            self._tag_behaviors.pop(t.tag_id, None)
            self._history.pop(t.tag_id, None)
        await self.node_manager.delete_device(device_id)

    async def _delete_tag_live_locked(self, tag_id: int) -> None:
        """Caller must hold self.structural_lock."""
        await self.node_manager.delete_tag(tag_id)
        self._tag_behaviors.pop(tag_id, None)
        self._history.pop(tag_id, None)

    # ── Folders ──────────────────────────────────────────────────────────────

    async def add_folder_live(self, folder_row: dict) -> None:
        async with self.structural_lock:
            await self.node_manager.create_folder(folder_row, self._resolve_parent_node(folder_row.get("parent_folder_id")))

    async def rename_folder_live(self, folder_id: int, name: str) -> None:
        """Name-only change — a folder's NodeId is id-based (lib/nodes.py),
        so this never needs to touch the wire node's identity, just its
        DisplayName attribute. Not a structural mutation, no lock needed."""
        live = self.node_manager.get_folder(folder_id)
        if not live:
            return
        await live.node.write_attribute(ua.AttributeIds.DisplayName, ua.DataValue(ua.LocalizedText(name)))

    async def delete_folder_live(self, folder_id: int) -> None:
        async with self.structural_lock:
            await self.node_manager.delete_folder(folder_id)

    async def set_folder_enabled_live(self, folder_id: int, enabled: bool) -> None:
        """Only ever changes which devices are LIVE — never touches any
        device's own `enabled` DB value (that would violate the
        non-destructive cascade: a device disabled on its own must stay
        disabled even after its folder re-enables, and vice versa). Doesn't
        scope to just this folder's descendants — recomputes every device's
        effective-enabled state fresh from DB and reconciles the diff, since
        the DB write for folders.enabled already happened by the time this
        runs (see routes_folders.py) — simplicity over the optimization of
        walking just the affected subtree, fine at simulator scale."""
        async with self.structural_lock:
            folders_by_id = {f["id"]: f for f in await asyncio.to_thread(self.db.get_folders)}
            devices = await asyncio.to_thread(self.db.get_devices)
            changed = 0
            for dev in devices:
                eff = is_effectively_enabled(dev, folders_by_id)
                live = self.node_manager.get_device(dev["id"]) is not None
                if eff and not live:
                    await self._add_device_live_locked(dev)
                    tags = await asyncio.to_thread(self.db.get_tags, dev["id"])
                    for tag in tags:
                        if tag["enabled"]:
                            await self._create_live_tag(dev["id"], tag)
                    changed += 1
                elif not eff and live:
                    await self._delete_device_live_locked(dev["id"])
                    changed += 1
            log.info(
                "Folder %d enabled=%s — %d device(s) changed live state", folder_id, enabled, changed,
            )

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

        # Held for the whole walk so a concurrent structural mutation (add/
        # delete folder/device/tag, a NodeSet import) can't change
        # node_manager's registry out from under this iteration.
        async with self.structural_lock:
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

                # Captured before compute() — _fault_active was already
                # carried forward from prev above, so this is the state as
                # of the end of the previous tick; compute() below may flip
                # it during this tick.
                was_fault_active = isinstance(new_b, FaultBehavior) and new_b._fault_active

                val = new_b.compute(self.state)
                await self._write_value(live_tag, val)

                if isinstance(new_b, FaultBehavior):
                    record_fault_transition(
                        state.metrics, live_tag.tag_id, tag_row["name"], dev["name"],
                        new_b._fault_active, was_fault_active, new_b.fault_type,
                    )

                hist = self._history.setdefault(live_tag.tag_id, deque(maxlen=720))
                hist.append((time.time(), val))

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


# ─── WebSocket broadcaster ─────────────────────────────────────────────────────

async def broadcast_state() -> None:
    if not state.ws_clients:
        return
    data = json.dumps(state.engine.get_state())
    dead = []
    for ws in state.ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.ws_clients.remove(ws)


async def broadcast_metrics() -> None:
    from lib.analytics import build_metrics_snapshot
    if not state.metrics_ws_clients:
        return
    data = json.dumps(await build_metrics_snapshot(state.engine, state.metrics))
    dead = []
    for ws in state.metrics_ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.metrics_ws_clients.remove(ws)


# ─── Background tasks ──────────────────────────────────────────────────────────

async def tick_loop() -> None:
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await state.engine.tick()
            await broadcast_state()
        except Exception as e:
            log.error("Tick error: %s", e)


async def metrics_loop() -> None:
    # Deliberately independent of TICK_SECONDS/tick_loop() — device-value
    # simulation and analytics refresh are different concerns with different
    # natural cadences (5s vs 1s); coupling them would mean either slowing
    # down analytics or speeding up (and adding load to) the actual device
    # simulation just to serve the dashboard.
    while True:
        await asyncio.sleep(1.0)
        try:
            await broadcast_metrics()
        except Exception as e:
            log.error("Metrics tick error: %s", e)


# ─── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    state.db = Database(DB_PATH)
    await asyncio.to_thread(state.db.setup)
    for d in state.db.get_devices():
        state._device_names[d["id"]] = d["name"]
    state.metrics = Metrics()
    state.engine = SimEngine(state.db)
    await state.engine.start()
    tick_task = asyncio.create_task(tick_loop())
    metrics_task = asyncio.create_task(metrics_loop())
    log.info("OPC UA Simulator API ready on port %d", SIM_API_PORT)
    yield
    log.info("Shutting down")
    tick_task.cancel()
    metrics_task.cancel()
    for t in (tick_task, metrics_task):
        try:
            await t
        except asyncio.CancelledError:
            pass
    await state.engine.stop()


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
    if not auth_header.lower().startswith("bearer ") or not user_from_token(state.db, auth_header[7:].strip()):
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


# ── Health / meta ──

@api.get("/health")
async def health():
    devices = await asyncio.to_thread(state.db.get_devices)
    return {
        "status": "ok",
        "devices": len(devices),
        "opcua_running": state.engine.server is not None,
        "sim_state": state.engine.clock_state,
        "elapsed_seconds": state.engine.state.elapsed_seconds,
    }


@api.get("/meta")
async def meta():
    from lib.db import VALID_DATA_TYPES
    return {
        "data_types": sorted(VALID_DATA_TYPES),
        "behaviors": sorted(VALID_BEHAVIORS),
    }


# ── Sim clock ──

@api.post("/sim/start")
async def sim_start():
    state.engine.resume()
    return {"sim_state": state.engine.clock_state}


@api.post("/sim/pause")
async def sim_pause():
    state.engine.pause()
    return {"sim_state": state.engine.clock_state}


@api.post("/sim/stop")
async def sim_stop():
    state.engine.reset()
    return {"sim_state": state.engine.clock_state, "elapsed_seconds": state.engine.state.elapsed_seconds}


# ── State ──

@api.get("/state")
async def get_state():
    return state.engine.get_state()


# ── WebSocket (device values) ──

@api.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # Browsers can't set custom headers on the WS handshake, so the token
    # travels as a query param instead of Authorization: Bearer.
    token = websocket.query_params.get("token", "")
    if not await asyncio.to_thread(user_from_token, state.db, token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    state.ws_clients.append(websocket)
    try:
        await websocket.send_text(json.dumps(state.engine.get_state()))
        while True:
            await websocket.receive_text()  # keep alive (ping)
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in state.ws_clients:
            state.ws_clients.remove(websocket)


# ── Routers ──

api.include_router(routes_auth.router)
api.include_router(routes_devices.router)
api.include_router(routes_folders.router)
api.include_router(routes_profiles.router)
api.include_router(routes_nodesets.router)
api.include_router(routes_analytics.router)


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
