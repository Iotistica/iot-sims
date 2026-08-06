from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import CalendarCreate, CalendarUpdate


router = APIRouter(
    tags=["calendars"],
)


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


def calendar_to_api(row: dict) -> dict:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "name": row["name"],
        "description": row["description"],
        "date_list": json.loads(
            row["date_list"] or "[]"
        ),
        "enabled": bool(row["enabled"]),
    }


def schedule_engine_reload(
    request: Request,
) -> None:
    engine = get_engine(request)
    asyncio.create_task(engine.reload())


@router.get("/devices/{device_id}/calendars")
async def list_calendars(
    device_id: int,
    request: Request,
):
    database = get_database(request)

    rows = await asyncio.to_thread(
        database.get_calendars,
        device_id,
    )

    return [
        calendar_to_api(row)
        for row in rows
    ]


@router.post(
    "/devices/{device_id}/calendars",
    status_code=201,
)
async def create_calendar(
    device_id: int,
    body: CalendarCreate,
    request: Request,
):
    database = get_database(request)

    body.validate_date_list()

    device = await asyncio.to_thread(
        database.get_device,
        device_id,
    )

    if device is None:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )

    calendars = await asyncio.to_thread(
        database.get_calendars,
        device_id,
    )

    existing_names = {
        calendar["name"]
        for calendar in calendars
    }

    if body.name in existing_names:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Calendar {body.name!r} already exists "
                "on this device — names must be unique "
                "since Schedules reference calendars by name"
            ),
        )

    row = await asyncio.to_thread(
        database.create_calendar,
        device_id,
        {
            "name": body.name,
            "description": body.description,
            "date_list": json.dumps(
                body.date_list
            ),
            "enabled": 1 if body.enabled else 0,
        },
    )

    schedule_engine_reload(request)

    return calendar_to_api(row)


@router.get("/calendars/{calendar_id}")
async def get_calendar(
    calendar_id: int,
    request: Request,
):
    database = get_database(request)

    row = await asyncio.to_thread(
        database.get_calendar,
        calendar_id,
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Calendar not found",
        )

    return calendar_to_api(row)


@router.put("/calendars/{calendar_id}")
async def update_calendar(
    calendar_id: int,
    body: CalendarUpdate,
    request: Request,
):
    database = get_database(request)

    body.validate_date_list()

    existing = await asyncio.to_thread(
        database.get_calendar,
        calendar_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Calendar not found",
        )

    calendars = await asyncio.to_thread(
        database.get_calendars,
        existing["device_id"],
    )

    other_names = {
        calendar["name"]
        for calendar in calendars
        if calendar["id"] != calendar_id
    }

    if body.name in other_names:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Calendar {body.name!r} already exists "
                "on this device — names must be unique "
                "since Schedules reference calendars by name"
            ),
        )

    row = await asyncio.to_thread(
        database.update_calendar,
        calendar_id,
        {
            "name": body.name,
            "description": body.description,
            "date_list": json.dumps(
                body.date_list
            ),
            "enabled": 1 if body.enabled else 0,
        },
    )

    schedule_engine_reload(request)

    return calendar_to_api(row)


@router.delete(
    "/calendars/{calendar_id}",
    status_code=204,
)
async def delete_calendar(
    calendar_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    deleted = await asyncio.to_thread(
        database.delete_calendar,
        calendar_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Calendar not found",
        )

    schedule_engine_reload(request)

    return Response(status_code=204)