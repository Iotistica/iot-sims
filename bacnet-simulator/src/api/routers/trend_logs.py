from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ...bacnet.schemas import TrendLogCreate, TrendLogUpdate


router = APIRouter(tags=["trend-logs"])


# ─── Runtime dependencies ─────────────────────────────────────────────────────

def get_database(request: Request) -> Any:
    database = getattr(
        request.app.state,
        "db",
        None,
    )

    if database is None:
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable",
        )

    return database


def get_engine(request: Request) -> Any:
    engine = getattr(
        request.app.state,
        "engine",
        None,
    )

    if engine is None:
        raise HTTPException(
            status_code=503,
            detail="Simulation engine is unavailable",
        )

    return engine


def schedule_engine_reload(request: Request) -> None:
    engine = get_engine(request)
    asyncio.create_task(engine.reload())


# ─── Validation ────────────────────────────────────────────────────────────────

async def validate_trend_log(
    database: Any,
    device_id: int,
    body: TrendLogCreate | TrendLogUpdate,
) -> None:
    if body.logging_type not in {
        "polled",
        "cov",
        "triggered",
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                'logging_type must be "polled", '
                '"cov", or "triggered"'
            ),
        )

    obj = await asyncio.to_thread(
        database.get_object,
        body.monitored_object_id,
    )

    if obj is None or obj["device_id"] != device_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "monitored_object_id must be an object "
                "on this device"
            ),
        )


# ─── Trend-log configuration ──────────────────────────────────────────────────

@router.get("/devices/{device_id}/trend-logs")
async def list_trend_logs(
    device_id: int,
    request: Request,
):
    database = get_database(request)

    device = await asyncio.to_thread(
        database.get_device,
        device_id,
    )

    if device is None:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )

    return await asyncio.to_thread(
        database.get_trend_logs,
        device_id,
    )


@router.post(
    "/devices/{device_id}/trend-logs",
    status_code=201,
)
async def create_trend_log(
    device_id: int,
    body: TrendLogCreate,
    request: Request,
):
    database = get_database(request)

    device = await asyncio.to_thread(
        database.get_device,
        device_id,
    )

    if device is None:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )

    await validate_trend_log(
        database,
        device_id,
        body,
    )

    data = body.model_dump()

    if (
        data["log_interval"] is None
        or data["buffer_size"] is None
    ):
        current_settings = await asyncio.to_thread(
            database.get_settings
        )

        if data["log_interval"] is None:
            data["log_interval"] = current_settings[
                "trend_log_default_interval"
            ]

        if data["buffer_size"] is None:
            data["buffer_size"] = current_settings[
                "trend_log_default_buffer_size"
            ]

    trend_log = await asyncio.to_thread(
        database.create_trend_log,
        device_id,
        data,
    )

    schedule_engine_reload(request)

    return trend_log


@router.get("/trend-logs/{trend_log_id}")
async def get_trend_log(
    trend_log_id: int,
    request: Request,
):
    database = get_database(request)

    trend_log = await asyncio.to_thread(
        database.get_trend_log,
        trend_log_id,
    )

    if trend_log is None:
        raise HTTPException(
            status_code=404,
            detail="Trend log not found",
        )

    return trend_log


@router.put("/trend-logs/{trend_log_id}")
async def update_trend_log(
    trend_log_id: int,
    body: TrendLogUpdate,
    request: Request,
):
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_trend_log,
        trend_log_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Trend log not found",
        )

    await validate_trend_log(
        database,
        existing["device_id"],
        body,
    )

    data = body.model_dump()

    # Preserve existing values when optional defaults are omitted
    # during an update.
    if data.get("log_interval") is None:
        data["log_interval"] = existing["log_interval"]

    if data.get("buffer_size") is None:
        data["buffer_size"] = existing["buffer_size"]

    trend_log = await asyncio.to_thread(
        database.update_trend_log,
        trend_log_id,
        data,
    )

    if trend_log is None:
        raise HTTPException(
            status_code=404,
            detail="Trend log not found",
        )

    schedule_engine_reload(request)

    return trend_log


@router.delete(
    "/trend-logs/{trend_log_id}",
    status_code=204,
)
async def delete_trend_log(
    trend_log_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    deleted = await asyncio.to_thread(
        database.delete_trend_log,
        trend_log_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Trend log not found",
        )

    schedule_engine_reload(request)

    return Response(status_code=204)


# ─── Trend-log records ────────────────────────────────────────────────────────

@router.get("/trend-logs/{trend_log_id}/records")
async def get_trend_log_records(
    trend_log_id: int,
    request: Request,
    from_: Optional[str] = Query(
        default=None,
        alias="from",
    ),
    to: Optional[str] = None,
    start_sequence: Optional[int] = None,
    limit: int = Query(
        default=200,
        ge=1,
        le=5000,
    ),
    order: str = "asc",
):
    database = get_database(request)

    trend_log = await asyncio.to_thread(
        database.get_trend_log,
        trend_log_id,
    )

    if trend_log is None:
        raise HTTPException(
            status_code=404,
            detail="Trend log not found",
        )

    if order not in {"asc", "desc"}:
        raise HTTPException(
            status_code=400,
            detail='order must be "asc" or "desc"',
        )

    return await asyncio.to_thread(
        database.get_trend_log_records,
        trend_log_id,
        from_,
        to,
        start_sequence,
        limit,
        order,
    )


@router.post("/trend-logs/{trend_log_id}/trigger")
async def trigger_trend_log(
    trend_log_id: int,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    trend_log = await asyncio.to_thread(
        database.get_trend_log,
        trend_log_id,
    )

    if trend_log is None:
        raise HTTPException(
            status_code=404,
            detail="Trend log not found",
        )

    value = engine.get_object_value(
        trend_log["monitored_object_id"]
    )

    if value is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Monitored object has no live value yet "
                "(simulator not ticked)"
            ),
        )

    sequence_number = await engine._sample_trend_log(
        trend_log["id"],
        value,
    )

    if sequence_number is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Buffer is full and stop_when_full is set"
            ),
        )

    return {
        "ok": True,
        "sequence_number": sequence_number,
        "value": value,
    }


@router.post("/trend-logs/{trend_log_id}/clear")
async def clear_trend_log(
    trend_log_id: int,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    cleared = await asyncio.to_thread(
        database.clear_trend_log_records,
        trend_log_id,
    )

    if not cleared:
        raise HTTPException(
            status_code=404,
            detail="Trend log not found",
        )

    engine.refresh_trend_log_buffer_empty(
        trend_log_id
    )

    return {"ok": True}