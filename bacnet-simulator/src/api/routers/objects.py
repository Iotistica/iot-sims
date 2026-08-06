from __future__ import annotations

import asyncio
import sqlite3
from collections import deque
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import (
    ObjectCreate,
    ObjectUpdate,
    PriorityWrite,
    SetValueRequest,
)
from ...core.config import COMMANDABLE_TYPES


router = APIRouter(
    prefix="/devices/{device_id}/objects",
    tags=["objects"],
)


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


def get_event_logger(
    request: Request,
) -> Callable[[int | None, str, str], None] | None:
    return getattr(
        request.app.state,
        "log_event",
        None,
    )


def log_event(
    request: Request,
    device_id: int,
    level: str,
    message: str,
) -> None:
    callback = get_event_logger(request)

    if callback is not None:
        callback(
            device_id,
            level,
            message,
        )


def schedule_engine_reload(
    request: Request,
) -> None:
    engine = get_engine(request)
    asyncio.create_task(engine.reload())


# ─── Lookup helpers ────────────────────────────────────────────────────────────

async def require_device(
    database: Any,
    device_id: int,
) -> dict:
    device = await asyncio.to_thread(
        database.get_device,
        device_id,
    )

    if device is None:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )

    return device


async def require_object(
    database: Any,
    device_id: int,
    object_id: int,
) -> dict:
    obj = await asyncio.to_thread(
        database.get_object,
        object_id,
    )

    if (
        obj is None
        or obj["device_id"] != device_id
    ):
        raise HTTPException(
            status_code=404,
            detail="Object not found",
        )

    return obj


def require_commandable_object(
    obj: dict,
) -> None:
    if obj["object_type"] not in COMMANDABLE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{obj['object_type']} has no BACnet "
                "priority array (not Commandable)"
            ),
        )


# ─── Object CRUD ───────────────────────────────────────────────────────────────

@router.get("")
async def list_objects(
    device_id: int,
    request: Request,
):
    database = get_database(request)

    await require_device(
        database,
        device_id,
    )

    return await asyncio.to_thread(
        database.get_objects,
        device_id,
    )


@router.post(
    "",
    status_code=201,
)
async def create_object(
    device_id: int,
    body: ObjectCreate,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    body.validate_type()
    body.validate_semantic()

    device = await require_device(
        database,
        device_id,
    )

    try:
        obj = await asyncio.to_thread(
            database.create_object,
            device_id,
            body.model_dump(),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Object {body.object_type},"
                f"{body.object_instance} already exists "
                "on this device"
            ),
        ) from exc

    log_event(
        request,
        device_id,
        "info",
        (
            f"Object added: {body.name} "
            f"({body.object_type}:"
            f"{body.object_instance})"
        ),
    )

    # Preserve the current hot-add behavior. A full engine reload is
    # unnecessary when both the device and object are enabled.
    if device["enabled"] and body.enabled:
        asyncio.create_task(
            engine.add_object_hot(
                device["device_instance"],
                obj,
            )
        )

    return obj


@router.get("/{object_id}")
async def get_object(
    device_id: int,
    object_id: int,
    request: Request,
):
    database = get_database(request)

    return await require_object(
        database,
        device_id,
        object_id,
    )


@router.put("/{object_id}")
async def update_object(
    device_id: int,
    object_id: int,
    body: ObjectUpdate,
    request: Request,
):
    database = get_database(request)

    body.validate_type()
    body.validate_semantic()

    existing = await require_object(
        database,
        device_id,
        object_id,
    )

    try:
        updated = await asyncio.to_thread(
            database.update_object,
            object_id,
            body.model_dump(),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Object {body.object_type},"
                f"{body.object_instance} already exists "
                "on this device"
            ),
        ) from exc

    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="Object not found",
        )

    enabled_changed = (
        bool(existing["enabled"])
        != bool(body.enabled)
    )

    if enabled_changed:
        state = (
            "enabled"
            if body.enabled
            else "disabled"
        )

        log_event(
            request,
            device_id,
            "info",
            (
                f"Object {existing['name']}: "
                f"{state}"
            ),
        )

    elif existing["behavior"] != body.behavior:
        log_event(
            request,
            device_id,
            "info",
            (
                f"Object {existing['name']}: "
                f"behavior changed to {body.behavior}"
            ),
        )

    else:
        log_event(
            request,
            device_id,
            "info",
            (
                f"Object {existing['name']}: "
                "configuration updated"
            ),
        )

    schedule_engine_reload(request)

    return updated


@router.delete(
    "/{object_id}",
    status_code=204,
)
async def delete_object(
    device_id: int,
    object_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    obj = await require_object(
        database,
        device_id,
        object_id,
    )

    log_event(
        request,
        device_id,
        "warn",
        (
            f"Object removed: {obj['name']} "
            f"({obj['object_type']}:"
            f"{obj['object_instance']})"
        ),
    )

    deleted = await asyncio.to_thread(
        database.delete_object,
        object_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Object not found",
        )

    schedule_engine_reload(request)

    return Response(status_code=204)


# ─── Runtime value and history ─────────────────────────────────────────────────

@router.post("/{object_id}/value")
async def set_object_value(
    device_id: int,
    object_id: int,
    body: SetValueRequest,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    obj = await require_object(
        database,
        device_id,
        object_id,
    )

    await asyncio.to_thread(
        database.set_manual_value,
        object_id,
        body.value,
    )

    engine.set_manual_value(
        object_id,
        body.value,
    )

    value_text = str(body.value)

    units = obj.get("units")

    if units and units != "no-units":
        value_text = f"{value_text} {units}"

    log_event(
        request,
        device_id,
        "info",
        (
            f"Manual override: {obj['name']} "
            f"→ {value_text}"
        ),
    )

    return {"ok": True}


@router.get("/{object_id}/history")
async def get_object_history(
    device_id: int,
    object_id: int,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    await require_object(
        database,
        device_id,
        object_id,
    )

    # Keep the existing private-engine access during migration.
    history = engine._history.get(
        object_id,
        deque(),
    )

    return [
        {
            "ts": timestamp,
            "value": value,
        }
        for timestamp, value in history
    ]


# ─── BACnet priority array ─────────────────────────────────────────────────────

@router.get("/{object_id}/priority-array")
async def get_priority_array(
    device_id: int,
    object_id: int,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    obj = await require_object(
        database,
        device_id,
        object_id,
    )

    require_commandable_object(obj)

    result = engine.get_priority_array(
        object_id
    )

    if result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Object is not currently live in the "
                "running application"
            ),
        )

    return result


@router.put(
    "/{object_id}/priority-array/{priority}"
)
async def write_priority_array(
    device_id: int,
    object_id: int,
    priority: int,
    body: PriorityWrite,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    if not 1 <= priority <= 16:
        raise HTTPException(
            status_code=400,
            detail="priority must be between 1 and 16",
        )

    obj = await require_object(
        database,
        device_id,
        object_id,
    )

    require_commandable_object(obj)

    written = await engine.write_priority(
        object_id,
        priority,
        body.value,
    )

    if not written:
        raise HTTPException(
            status_code=409,
            detail=(
                "Object is not currently live in the "
                "running application"
            ),
        )

    action = (
        "relinquished"
        if body.value is None
        else f"set to {body.value}"
    )

    log_event(
        request,
        device_id,
        "info",
        (
            f"Priority array: {obj['name']} "
            f"priority {priority} {action}"
        ),
    )

    result = engine.get_priority_array(
        object_id
    )

    if result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Priority was written, but the live "
                "priority array is unavailable"
            ),
        )

    return result