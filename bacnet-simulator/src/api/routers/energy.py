from __future__ import annotations
import time
import json
import asyncio
import sqlite3
from typing import Any
from fastapi import APIRouter, HTTPException, Query, Request, Response

from ...bacnet.schemas import EnergyModelConfigUpdate
from ...energy.registry import energy_model_config_to_api, validate_energy_model_parameters

router = APIRouter(prefix="/energy", tags=["energy"])


def get_energy_engine(request: Request) -> Any:
    engine = getattr(request.app.state, "energy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Energy engine is unavailable")
    return engine


def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return database

@router.get("/history")
async def get_energy_history(
    request: Request,
    hours: float = Query(
        default=24.0,
        gt=0,
        le=168,
    ),
    device_id: int | None = None,
    model_type: str | None = None,
):
    db = request.app.state.db

    end_timestamp = time.time()
    start_timestamp = (
        end_timestamp - hours * 60 * 60
    )

    rows = await asyncio.to_thread(
        db.get_energy_history,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        device_id=device_id,
        model_type=model_type,
    )

    output: list[dict] = []

    for row in rows:
        try:
            metrics = json.loads(
                row.get("metrics") or "{}"
            )
        except (TypeError, ValueError):
            metrics = {}

        output.append(
            {
                **row,
                "metrics": metrics,
            }
        )

    return output

@router.get("/equipment")
async def list_energy_results(request: Request):
    return get_energy_engine(request).get_latest()


@router.post("/evaluate")
async def evaluate_energy(request: Request, elapsed_seconds: float = Query(default=5.0, gt=0, le=3600)):
    return await get_energy_engine(request).evaluate_all(elapsed_seconds=elapsed_seconds)


# ─── Energy model config: row-id-addressed update/delete ───────────────────
# List/create are device-scoped and live in devices.py
# (GET/POST /devices/{device_id}/energy-models) -- these two operate on an
# existing row's own id, matching the notification-classes precedent
# (PUT/DELETE /notification-classes/{id}).

@router.put("/models/{config_id}")
async def update_energy_model_config(
    config_id: int,
    body: EnergyModelConfigUpdate,
    request: Request,
):
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_energy_model_config_by_id,
        config_id,
    )

    if existing is None:
        raise HTTPException(status_code=404, detail="Energy model config not found")

    try:
        validate_energy_model_parameters(body.model_type, body.parameters)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        row = await asyncio.to_thread(
            database.update_energy_model_config_by_id,
            config_id,
            model_type=body.model_type,
            instance_key=body.instance_key,
            enabled=body.enabled,
            parameters=json.dumps(body.parameters),
        )
    except sqlite3.IntegrityError as e:
        raise HTTPException(
            status_code=409,
            detail=(
                "Another energy model config already has this "
                "device/model type/instance key combination"
            ),
        ) from e

    if row is None:
        raise HTTPException(status_code=404, detail="Energy model config not found")

    return energy_model_config_to_api(row)


@router.delete("/models/{config_id}", status_code=204)
async def delete_energy_model_config(
    config_id: int,
    request: Request,
):
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_energy_model_config_by_id,
        config_id,
    )

    if existing is None:
        raise HTTPException(status_code=404, detail="Energy model config not found")

    await asyncio.to_thread(
        database.delete_energy_model_config_by_id,
        config_id,
    )

    return Response(status_code=204)
