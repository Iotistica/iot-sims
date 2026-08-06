from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from bacpypes3.primitivedata import Date, Time

from ...bacnet import calendar as bacnet_calendar
from ...bacnet import schedule as bacnet_schedule
from ...bacnet.schemas import ScheduleCreate, ScheduleUpdate


router = APIRouter(tags=["schedules"])


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


# ─── Conversion and validation ────────────────────────────────────────────────

SCHEDULE_TARGET_TYPES: dict[str, tuple[str, ...]] = {
    "real": (
        "analog-input",
        "analog-output",
        "analog-value",
    ),
    "boolean": (
        "binary-input",
        "binary-output",
        "binary-value",
    ),
    "unsigned": (
        "multi-state-input",
        "multi-state-output",
        "multi-state-value",
    ),
}


def schedule_to_api(
    row: dict,
    targets: list[dict],
) -> dict:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "name": row["name"],
        "description": row["description"],
        "value_type": row["value_type"],
        "schedule_default": json.loads(
            row["schedule_default"]
        ),
        "effective_start": row["effective_start"],
        "effective_end": row["effective_end"],
        "weekly_schedule": json.loads(
            row["weekly_schedule"] or "{}"
        ),
        "exception_schedule": json.loads(
            row["exception_schedule"] or "[]"
        ),
        "priority_for_writing": row[
            "priority_for_writing"
        ],
        "enabled": bool(row["enabled"]),
        "targets": [
            {
                "object_id": target["object_id"],
                "property_identifier": target[
                    "property_identifier"
                ],
                "object_name": target["object_name"],
                "object_type": target["object_type"],
                "object_instance": target[
                    "object_instance"
                ],
            }
            for target in targets
        ],
    }


def schedule_to_db(
    body: ScheduleCreate | ScheduleUpdate,
) -> dict:
    return {
        "name": body.name,
        "description": body.description,
        "value_type": body.value_type,
        "schedule_default": json.dumps(
            body.schedule_default
        ),
        "effective_start": body.effective_start,
        "effective_end": body.effective_end,
        "weekly_schedule": json.dumps(
            body.weekly_schedule
        ),
        "exception_schedule": json.dumps(
            body.exception_schedule
        ),
        "priority_for_writing": (
            body.priority_for_writing
        ),
        "enabled": 1 if body.enabled else 0,
    }


async def validate_schedule(
    database: Any,
    device_id: int,
    body: ScheduleCreate | ScheduleUpdate,
) -> None:
    if body.value_type not in SCHEDULE_TARGET_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "value_type must be one of: "
                f"{sorted(SCHEDULE_TARGET_TYPES)}"
            ),
        )

    allowed_types = SCHEDULE_TARGET_TYPES[
        body.value_type
    ]

    for target in body.targets:
        obj = await asyncio.to_thread(
            database.get_object,
            target.object_id,
        )

        if obj is None or obj["device_id"] != device_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"target object {target.object_id} "
                    "must belong to this device"
                ),
            )

        if obj["object_type"] not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"target object {target.object_id} "
                    f"({obj['object_type']}) doesn't match "
                    f"value_type {body.value_type!r} — "
                    f"expected one of: {allowed_types}"
                ),
            )

    calendar_names: set[str] | None = None

    for exception in body.exception_schedule:
        period = (exception or {}).get("period") or {}

        if period.get("type") != "calendar-reference":
            continue

        if calendar_names is None:
            calendars = await asyncio.to_thread(
                database.get_calendars,
                device_id,
            )

            calendar_names = {
                calendar["name"]
                for calendar in calendars
            }

        calendar_name = period.get("calendar_name")

        if calendar_name not in calendar_names:
            raise HTTPException(
                status_code=400,
                detail=(
                    "exception references unknown calendar "
                    f"{calendar_name!r} on this device"
                ),
            )


async def load_schedule_response(
    database: Any,
    schedule_row: dict,
) -> dict:
    targets = await asyncio.to_thread(
        database.get_schedule_targets,
        schedule_row["id"],
    )

    return schedule_to_api(
        schedule_row,
        targets,
    )


# ─── Schedule CRUD ────────────────────────────────────────────────────────────

@router.get("/devices/{device_id}/schedules")
async def list_schedules(
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

    rows = await asyncio.to_thread(
        database.get_schedules,
        device_id,
    )

    return [
        await load_schedule_response(
            database,
            row,
        )
        for row in rows
    ]


@router.post(
    "/devices/{device_id}/schedules",
    status_code=201,
)
async def create_schedule(
    device_id: int,
    body: ScheduleCreate,
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

    await validate_schedule(
        database,
        device_id,
        body,
    )

    row = await asyncio.to_thread(
        database.create_schedule,
        device_id,
        schedule_to_db(body),
        [
            target.model_dump()
            for target in body.targets
        ],
    )

    schedule_engine_reload(request)

    return await load_schedule_response(
        database,
        row,
    )


@router.get("/schedules/{schedule_id}")
async def get_schedule(
    schedule_id: int,
    request: Request,
):
    database = get_database(request)

    row = await asyncio.to_thread(
        database.get_schedule,
        schedule_id,
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Schedule not found",
        )

    return await load_schedule_response(
        database,
        row,
    )


@router.put("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: int,
    body: ScheduleUpdate,
    request: Request,
):
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_schedule,
        schedule_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Schedule not found",
        )

    await validate_schedule(
        database,
        existing["device_id"],
        body,
    )

    row = await asyncio.to_thread(
        database.update_schedule,
        schedule_id,
        schedule_to_db(body),
        [
            target.model_dump()
            for target in body.targets
        ],
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Schedule not found",
        )

    schedule_engine_reload(request)

    return await load_schedule_response(
        database,
        row,
    )


@router.delete(
    "/schedules/{schedule_id}",
    status_code=204,
)
async def delete_schedule(
    schedule_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    deleted = await asyncio.to_thread(
        database.delete_schedule,
        schedule_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Schedule not found",
        )

    schedule_engine_reload(request)

    return Response(status_code=204)


# ─── Enable and disable ───────────────────────────────────────────────────────

@router.post("/schedules/{schedule_id}/enable")
async def enable_schedule(
    schedule_id: int,
    request: Request,
):
    database = get_database(request)

    row = await asyncio.to_thread(
        database.set_schedule_enabled,
        schedule_id,
        True,
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Schedule not found",
        )

    schedule_engine_reload(request)

    return {"ok": True}


@router.post("/schedules/{schedule_id}/disable")
async def disable_schedule(
    schedule_id: int,
    request: Request,
):
    database = get_database(request)

    row = await asyncio.to_thread(
        database.set_schedule_enabled,
        schedule_id,
        False,
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Schedule not found",
        )

    schedule_engine_reload(request)

    return {"ok": True}


# ─── Schedule evaluation ──────────────────────────────────────────────────────

@router.post("/schedules/{schedule_id}/evaluate")
async def evaluate_schedule(
    schedule_id: int,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    schedule_row = await asyncio.to_thread(
        database.get_schedule,
        schedule_id,
    )

    if schedule_row is None:
        raise HTTPException(
            status_code=404,
            detail="Schedule not found",
        )

    # This currently accesses the live BACpypes schedule-object
    # registry directly. Keep it during the migration to preserve
    # existing behavior.
    bacnet_object = engine._schedule_objects.get(
        schedule_id
    )

    if bacnet_object is None:
        return {
            "present_value": None,
            "source": "not-active",
            "matching_exception": None,
            "next_transition": None,
        }

    now = datetime.now()

    current_date = Date(
        (
            now.year - 1900,
            now.month,
            now.day,
            now.isoweekday(),
        )
    )

    current_time = Time(
        (
            now.hour,
            now.minute,
            now.second,
            0,
        )
    )

    result = bacnet_object.eval(
        current_date,
        current_time,
    )

    if result is None:
        return {
            "present_value": None,
            "source": "outside-effective-period",
            "matching_exception": None,
            "next_transition": None,
        }

    value, next_transition_time = result

    python_value = bacnet_schedule.atomic_to_python(
        value,
        schedule_row["value_type"],
    )

    source = "weekly"
    matching_exception = None

    exceptions = json.loads(
        schedule_row["exception_schedule"] or "[]"
    )

    for exception in exceptions:
        period = exception.get("period") or {}

        try:
            period_type = period.get("type")

            if period_type == "date":
                year, month, day = (
                    bacnet_schedule.parse_date_tuple(
                        period["date"]
                    )
                )

                matches = (
                    year,
                    month,
                    day,
                ) == (
                    current_date[0],
                    current_date[1],
                    current_date[2],
                )

            elif period_type == "date-range":
                start_year, start_month, start_day = (
                    bacnet_schedule.parse_date_tuple(
                        period["start"]
                    )
                )

                end_year, end_month, end_day = (
                    bacnet_schedule.parse_date_tuple(
                        period["end"]
                    )
                )

                current_tuple = (
                    current_date[0],
                    current_date[1],
                    current_date[2],
                )

                matches = (
                    (
                        start_year,
                        start_month,
                        start_day,
                    )
                    <= current_tuple
                    <= (
                        end_year,
                        end_month,
                        end_day,
                    )
                )

            elif period_type == "calendar-reference":
                calendars = await asyncio.to_thread(
                    database.get_calendars,
                    schedule_row["device_id"],
                )

                calendar_row = next(
                    (
                        calendar
                        for calendar in calendars
                        if calendar["name"]
                        == period.get("calendar_name")
                    ),
                    None,
                )

                matches = bool(
                    calendar_row
                ) and bacnet_calendar.today_in_date_list(
                    json.loads(
                        calendar_row["date_list"]
                        or "[]"
                    )
                )

            else:
                matches = False

        except Exception:
            matches = False

        if matches:
            source = "exception"
            matching_exception = period
            break

    if next_transition_time[0] == 24:
        next_transition = datetime.combine(
            now.date() + timedelta(days=1),
            datetime.min.time(),
        )
    else:
        next_transition = now.replace(
            hour=next_transition_time[0],
            minute=next_transition_time[1],
            second=next_transition_time[2],
            microsecond=0,
        )

    return {
        "present_value": python_value,
        "source": source,
        "matching_exception": matching_exception,
        "next_transition": (
            next_transition.isoformat()
        ),
    }