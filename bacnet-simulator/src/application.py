"""FastAPI application composition.

Physically extracted from src/legacy.py -- continuing the GH #15 refactor,
same "moved verbatim, no behavior changes" standard as the Database and
simulation-engine extractions. This is the largest of the remaining
facades: the `api` FastAPI instance itself, every router include, the auth
middleware, and the handful of routes that were never split into their own
router module (admin SPA shell, /meta, /settings, the two EDE import
routes, the static-assets mount).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.guards import reject_external_device
from .api.middleware import auth_gate
from .bacnet import ede
from .bacnet.schemas import SettingsPayload
from .core.config import (
    BACNET_PORT, BACNET_UNITS, BRICK_VERSION, CONTROLLER_TYPES, EQUIPMENT_TYPES,
    LOCATION_KINDS, POINT_TYPES, SEMANTIC_PREDICATES, VALID_BEHAVIORS,
    VALID_OBJECT_TYPES, VALID_POLARITY, VALID_RELIABILITY, VALID_SEGMENTATION,
)
from . import dependencies
from .energy.registry import MODEL_TYPE_LABELS
from .simulation.runtime import lifespan

from .api.routers.packet_capture import router as packet_capture_router
from .api.routers.backups import router as backups_router
from .api.routers.locations import router as locations_router
from .api.routers.equipment import router as equipment_router
from .api.routers.semantic import router as semantic_router
from .api.routers.calendars import router as calendars_router
from .api.routers.alarms import router as alarms_router
from .api.routers.trend_logs import router as trend_logs_router
from .api.routers.replay_recordings import router as replay_recordings_router
from .api.routers.calibration import router as calibration_router
from .api.routers.schedules import router as schedules_router
from .api.routers.devices import router as devices_router
from .api.routers.analytics import router as analytics_router
from .api.routers.auth import router as auth_router
from .api.routers.events import router as events_router
from .api.routers.exports import router as exports_router
from .api.routers.objects import router as objects_router
from .api.routers.projects import router as projects_router
from .api.routers.simulation import router as simulation_router
from .api.routers.websocket import router as websocket_router
from .api.routers.fault_detection import router as fault_router
from .api.routers.energy import router as energy_router
from .api.routers.discovery import router as discovery_router
from .api.routers.external_objects import router as external_objects_router
from .api.routers.semantic_suggestions import router as semantic_suggestions_router
from .api.routers.functional_tests import router as functional_tests_router
from .api.routers.custom_graphs import router as custom_graphs_router
from .api.routers.functional_test_runs import router as functional_test_runs_router
from .api.routers.points import router as points_router
from .api.routers.templates import router as templates_router


api = FastAPI(title="BACnet Simulator", lifespan=lifespan)

api.include_router(packet_capture_router)
api.include_router(backups_router)
api.include_router(locations_router)
api.include_router(equipment_router)
api.include_router(semantic_router)
api.include_router(calendars_router)
api.include_router(alarms_router)
api.include_router(trend_logs_router)
api.include_router(replay_recordings_router)
api.include_router(calibration_router)
api.include_router(schedules_router)
api.include_router(devices_router)
api.include_router(analytics_router)
api.include_router(auth_router)
api.include_router(exports_router)
api.include_router(events_router)
api.include_router(objects_router)
api.include_router(projects_router)
api.include_router(simulation_router)
api.include_router(websocket_router)
api.include_router(fault_router)
api.include_router(energy_router)
api.include_router(discovery_router)
api.include_router(external_objects_router)
api.include_router(semantic_suggestions_router)
api.include_router(functional_tests_router)
api.include_router(custom_graphs_router)
api.include_router(functional_test_runs_router)
api.include_router(points_router)
api.include_router(templates_router)

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


api.middleware("http")(auth_gate)


ADMIN_DIST = Path(__file__).parent.parent / "admin" / "dist"
ADMIN_PUBLIC = Path(__file__).parent.parent / "admin" / "public"


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
    raise HTTPException(status_code=404)


@api.get("/bacnet-vendors.json", include_in_schema=False)
async def bacnet_vendors():
    f = ADMIN_DIST / "bacnet-vendors.json"
    if f.exists():
        return FileResponse(str(f), media_type="application/json")
    return JSONResponse({"vendors": []})


@api.get("/meta")
async def meta():
    # All virtual devices share this simulator's single BACnet/IP socket —
    # there's no per-device address, so notification recipients that target
    # one of our own devices all resolve to this same network address.
    engine = dependencies.engine
    own_ip = engine.app._own_ip if engine and engine.app else None
    return {
        "object_types": sorted(VALID_OBJECT_TYPES),
        "behaviors": sorted(VALID_BEHAVIORS),
        "units": BACNET_UNITS,
        "reliability_options": sorted(VALID_RELIABILITY),
        "polarity_options": sorted(VALID_POLARITY),
        "segmentation_options": sorted(VALID_SEGMENTATION),
        "brick_version": BRICK_VERSION,
        "equipment_types": [{"value": k, "label": v} for k, v in sorted(EQUIPMENT_TYPES.items())],
        "controller_types": [{"value": k, "label": v} for k, v in sorted(CONTROLLER_TYPES.items())],
        "point_types": [{"value": k, "label": v} for k, v in sorted(POINT_TYPES.items())],
        "location_kinds": [{"value": k, "label": v} for k, v in sorted(LOCATION_KINDS.items())],
        "semantic_predicates": [{"value": k, "label": v} for k, v in sorted(SEMANTIC_PREDICATES.items())],
        "energy_model_types": [{"value": k, "label": v} for k, v in MODEL_TYPE_LABELS.items()],
        "network_address": f"{own_ip}:{BACNET_PORT}" if own_ip and own_ip != "0.0.0.0" else None,
    }


@api.get("/settings")
async def get_settings():
    return await asyncio.to_thread(dependencies.db.get_settings)


@api.put("/settings")
async def update_settings(body: SettingsPayload):
    await asyncio.to_thread(dependencies.db.save_settings, body.model_dump())
    dependencies._apply_settings_live(body.model_dump())
    return body


@api.post("/devices/{device_id}/import/ede")
async def import_device_ede(device_id: int, file: UploadFile = File(...)):
    device = await asyncio.to_thread(dependencies.db.get_device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    reject_external_device(device)
    text = (await file.read()).decode("utf-8", errors="replace")
    rows = ede.parse_ede_rows(text)
    instances = sorted({row["device_instance"] for row in rows})
    if len(instances) > 1:
        raise HTTPException(
            400,
            f"This EDE file covers {len(instances)} devices (instances {instances}) — "
            "importing it into a single device would merge them and could overwrite "
            "points that collide by object type/instance. Use the project-level EDE "
            "import instead so each device is created separately.",
        )
    objects = [
        {k: v for k, v in row.items() if k != "device_instance"}
        for row in rows
    ]
    count = await asyncio.to_thread(dependencies.db.import_ede_objects, device_id, objects)
    asyncio.create_task(dependencies.engine.reload())
    return {"ok": True, "objects_imported": count}


@api.post("/profiles/import/ede", status_code=201)
async def import_project_ede(
    name: str = Form(...),
    description: str = Form(""),
    device_name: str = Form(""),
    file: UploadFile = File(...),
):
    text = (await file.read()).decode("utf-8", errors="replace")
    rows = ede.parse_ede_rows(text)
    if not rows:
        raise HTTPException(400, "No valid EDE rows found in file")
    data = ede.rows_to_devices(rows, device_name)
    return await asyncio.to_thread(dependencies.db.import_project, name, description, data)


# ── Admin static assets (Vite build output) ──
# Must be mounted after all API routes so API paths take precedence.
_assets_dir = ADMIN_DIST / "assets"
if _assets_dir.exists():
    api.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="admin-assets")


def create_app() -> FastAPI:
    return api


__all__ = ["api", "create_app"]
