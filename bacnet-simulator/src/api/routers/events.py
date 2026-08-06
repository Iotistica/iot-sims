from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request


router = APIRouter(tags=["events"])


def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)

    if database is None:
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable",
        )

    return database


def get_device_log_reader(
    request: Request,
) -> Callable[[int, int], list[dict]]:
    reader = getattr(
        request.app.state,
        "get_device_logs",
        None,
    )

    if reader is None:
        raise HTTPException(
            status_code=503,
            detail="Device event log is unavailable",
        )

    return reader


def get_global_log_reader(
    request: Request,
) -> Callable[[int], list[dict]]:
    reader = getattr(
        request.app.state,
        "get_global_logs",
        None,
    )

    if reader is None:
        raise HTTPException(
            status_code=503,
            detail="Global event log is unavailable",
        )

    return reader


@router.get("/devices/{device_id}/logs")
async def list_device_events(
    device_id: int,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
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

    reader = get_device_log_reader(request)
    return reader(device_id, limit)


@router.get("/logs")
async def list_all_events(
    request: Request,
    limit: int = Query(default=200, ge=1, le=5000),
):
    reader = get_global_log_reader(request)
    return reader(limit)