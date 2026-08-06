from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet import alarms
from ...bacnet.schemas import (
    AckAlarmRequest,
    AlarmConfigSet,
    EventEnrollmentCreate,
    EventEnrollmentUpdate,
    NotificationClassCreate,
    NotificationClassUpdate,
)


router = APIRouter(tags=["alarms"])


# ─── Shared runtime dependencies ──────────────────────────────────────────────

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


def get_current_user(request: Request) -> dict:
    """
    Resolve the authenticated user through the callback installed
    by the application startup code.

    The global authentication middleware has already rejected invalid
    requests, but alarm acknowledgement also needs the username.
    """
    resolver: Callable[[Request], dict] | None = getattr(
        request.app.state,
        "get_current_user",
        None,
    )

    if resolver is None:
        raise HTTPException(
            status_code=503,
            detail="Authentication service is unavailable",
        )

    return resolver(request)


def log_event(
    request: Request,
    device_id: int | None,
    level: str,
    message: str,
) -> None:
    """
    Send an event to the existing simulator event log when its callback
    has been installed. Logging failure must not break API operations.
    """
    callback = getattr(
        request.app.state,
        "log_event",
        None,
    )

    if callback is not None:
        callback(device_id, level, message)


# ─── Serialization helpers ───────────────────────────────────────────────────

def notification_class_to_api(row: dict) -> dict:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "name": row["name"],
        "priority_to_offnormal": row[
            "priority_to_offnormal"
        ],
        "priority_to_fault": row["priority_to_fault"],
        "priority_to_normal": row["priority_to_normal"],
        "ack_required_transitions": json.loads(
            row["ack_required_transitions"] or "[]"
        ),
        "recipients": json.loads(
            row["recipients"] or "[]"
        ),
    }


def notification_class_to_db(
    body: NotificationClassCreate | NotificationClassUpdate,
) -> dict:
    return {
        "name": body.name,
        "priority_to_offnormal": (
            body.priority_to_offnormal
        ),
        "priority_to_fault": body.priority_to_fault,
        "priority_to_normal": body.priority_to_normal,
        "ack_required_transitions": json.dumps(
            body.ack_required_transitions
        ),
        "recipients": json.dumps(body.recipients),
    }


def alarm_config_to_api(row: dict) -> dict:
    return {
        "object_id": row["object_id"],
        "notification_class_id": row[
            "notification_class_id"
        ],
        "enabled": bool(row["enabled"]),
        "event_enable": json.loads(
            row["event_enable"] or "[]"
        ),
        "notify_type": row["notify_type"],
        "time_delay": row["time_delay"],
        "time_delay_normal": row["time_delay_normal"],
        "params": json.loads(row["params"] or "{}"),
    }


def event_enrollment_to_api(row: dict) -> dict:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "name": row["name"],
        "monitored_object_id": row[
            "monitored_object_id"
        ],
        "algorithm": row["algorithm"],
        "event_parameters": json.loads(
            row["event_parameters"] or "{}"
        ),
        "notification_class_id": row[
            "notification_class_id"
        ],
        "enabled": bool(row["enabled"]),
        "event_enable": json.loads(
            row["event_enable"] or "[]"
        ),
        "notify_type": row["notify_type"],
        "time_delay": row["time_delay"],
        "time_delay_normal": row["time_delay_normal"],
    }


def event_enrollment_to_db(
    body: EventEnrollmentCreate | EventEnrollmentUpdate,
) -> dict:
    return {
        "name": body.name,
        "monitored_object_id": body.monitored_object_id,
        "algorithm": body.algorithm,
        "event_parameters": json.dumps(
            body.event_parameters
        ),
        "notification_class_id": (
            body.notification_class_id
        ),
        "enabled": 1 if body.enabled else 0,
        "event_enable": json.dumps(body.event_enable),
        "notify_type": body.notify_type,
        "time_delay": body.time_delay,
        "time_delay_normal": body.time_delay_normal,
    }


async def validate_event_enrollment(
    database: Any,
    device_id: int,
    body: EventEnrollmentCreate | EventEnrollmentUpdate,
) -> None:
    if body.algorithm not in alarms.ENROLLMENT_ALGORITHMS:
        allowed = sorted(
            alarms.ENROLLMENT_ALGORITHMS
        )

        raise HTTPException(
            status_code=400,
            detail=(
                "Unknown algorithm. Must be one of: "
                f"{allowed}"
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

    object_type = obj["object_type"]

    if (
        body.algorithm == "change-of-state"
        and object_type
        not in (
            alarms.BINARY_TYPES
            | alarms.MULTISTATE_TYPES
        )
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Change-of-State enrollments can only "
                "monitor binary-*/multi-state-* objects "
                "(analog uses the Out-of-Range algorithm "
                "instead)"
            ),
        )

    if (
        body.algorithm == "out-of-range"
        and object_type not in alarms.ANALOG_TYPES
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Out-of-Range enrollments can only "
                "monitor analog-* objects"
            ),
        )


# ─── Notification classes ─────────────────────────────────────────────────────

@router.get(
    "/devices/{device_id}/notification-classes"
)
async def list_notification_classes(
    device_id: int,
    request: Request,
):
    database = get_database(request)

    rows = await asyncio.to_thread(
        database.get_notification_classes,
        device_id,
    )

    return [
        notification_class_to_api(row)
        for row in rows
    ]


@router.post(
    "/devices/{device_id}/notification-classes",
    status_code=201,
)
async def create_notification_class(
    device_id: int,
    body: NotificationClassCreate,
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

    row = await asyncio.to_thread(
        database.create_notification_class,
        device_id,
        notification_class_to_db(body),
    )

    recipient_count = len(body.recipients)
    recipient_label = (
        "recipient"
        if recipient_count == 1
        else "recipients"
    )

    log_event(
        request,
        device_id,
        "info",
        (
            f"Notification class created: "
            f"{row['name']} "
            f"({recipient_count} {recipient_label})"
        ),
    )

    return notification_class_to_api(row)


@router.put("/notification-classes/{nc_id}")
async def update_notification_class(
    nc_id: int,
    body: NotificationClassUpdate,
    request: Request,
):
    database = get_database(request)

    row = await asyncio.to_thread(
        database.update_notification_class,
        nc_id,
        notification_class_to_db(body),
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Notification class not found",
        )

    recipient_count = len(body.recipients)
    recipient_label = (
        "recipient"
        if recipient_count == 1
        else "recipients"
    )

    log_event(
        request,
        row["device_id"],
        "info",
        (
            f"Notification class updated: "
            f"{row['name']} "
            f"({recipient_count} {recipient_label})"
        ),
    )

    return notification_class_to_api(row)


@router.delete(
    "/notification-classes/{nc_id}",
    status_code=204,
)
async def delete_notification_class(
    nc_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_notification_class,
        nc_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Notification class not found",
        )

    deleted = await asyncio.to_thread(
        database.delete_notification_class,
        nc_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Notification class not found",
        )

    log_event(
        request,
        existing["device_id"],
        "warn",
        (
            "Notification class removed: "
            f"{existing['name']}"
        ),
    )

    return Response(status_code=204)


# ─── Per-object intrinsic alarm configuration ─────────────────────────────────

@router.get(
    "/devices/{device_id}/objects/{obj_id}/alarm-config"
)
async def get_object_alarm_config(
    device_id: int,
    obj_id: int,
    request: Request,
):
    database = get_database(request)

    obj = await asyncio.to_thread(
        database.get_object,
        obj_id,
    )

    if obj is None or obj["device_id"] != device_id:
        raise HTTPException(
            status_code=404,
            detail="Object not found",
        )

    row = await asyncio.to_thread(
        database.get_alarm_config,
        obj_id,
    )

    return (
        alarm_config_to_api(row)
        if row is not None
        else None
    )


@router.put(
    "/devices/{device_id}/objects/{obj_id}/alarm-config"
)
async def set_object_alarm_config(
    device_id: int,
    obj_id: int,
    body: AlarmConfigSet,
    request: Request,
):
    database = get_database(request)

    obj = await asyncio.to_thread(
        database.get_object,
        obj_id,
    )

    if obj is None or obj["device_id"] != device_id:
        raise HTTPException(
            status_code=404,
            detail="Object not found",
        )

    if body.notification_class_id is not None:
        notification_class = await asyncio.to_thread(
            database.get_notification_class,
            body.notification_class_id,
        )

        if (
            notification_class is None
            or notification_class["device_id"]
            != device_id
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "notification_class_id must refer "
                    "to a notification class on this device"
                ),
            )

    data = {
        "notification_class_id": (
            body.notification_class_id
        ),
        "enabled": 1 if body.enabled else 0,
        "event_enable": json.dumps(
            body.event_enable
        ),
        "notify_type": body.notify_type,
        "time_delay": body.time_delay,
        "time_delay_normal": body.time_delay_normal,
        "params": json.dumps(body.params),
    }

    row = await asyncio.to_thread(
        database.set_alarm_config,
        obj_id,
        data,
    )

    return alarm_config_to_api(row)


@router.delete(
    "/devices/{device_id}/objects/{obj_id}/alarm-config",
    status_code=204,
)
async def delete_object_alarm_config(
    device_id: int,
    obj_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    obj = await asyncio.to_thread(
        database.get_object,
        obj_id,
    )

    if obj is None or obj["device_id"] != device_id:
        raise HTTPException(
            status_code=404,
            detail="Object not found",
        )

    await asyncio.to_thread(
        database.delete_alarm_config,
        obj_id,
    )

    return Response(status_code=204)


# ─── Alarm history and acknowledgement ────────────────────────────────────────

@router.get("/alarms")
async def list_alarms(
    request: Request,
    limit: int = 200,
    unacked_only: bool = False,
):
    database = get_database(request)

    limit = max(1, min(limit, 1000))

    return await asyncio.to_thread(
        database.get_alarm_log,
        limit,
        unacked_only,
    )


@router.post("/alarms/{alarm_id}/ack")
async def acknowledge_alarm(
    alarm_id: int,
    body: AckAlarmRequest,
    request: Request,
):
    database = get_database(request)
    current_user = get_current_user(request)

    acknowledged_by = (
        body.ack_by
        or current_user["username"]
    )

    row = await asyncio.to_thread(
        database.ack_alarm,
        alarm_id,
        acknowledged_by,
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Alarm not found",
        )

    return row


# ─── Event enrollments ────────────────────────────────────────────────────────

@router.get(
    "/devices/{device_id}/event-enrollments"
)
async def list_event_enrollments(
    device_id: int,
    request: Request,
):
    database = get_database(request)

    rows = await asyncio.to_thread(
        database.get_event_enrollments,
        device_id,
    )

    return [
        event_enrollment_to_api(row)
        for row in rows
    ]


@router.post(
    "/devices/{device_id}/event-enrollments",
    status_code=201,
)
async def create_event_enrollment(
    device_id: int,
    body: EventEnrollmentCreate,
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

    await validate_event_enrollment(
        database,
        device_id,
        body,
    )

    row = await asyncio.to_thread(
        database.create_event_enrollment,
        device_id,
        event_enrollment_to_db(body),
    )

    return event_enrollment_to_api(row)


@router.put("/event-enrollments/{ee_id}")
async def update_event_enrollment(
    ee_id: int,
    body: EventEnrollmentUpdate,
    request: Request,
):
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_event_enrollment,
        ee_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Event enrollment not found",
        )

    await validate_event_enrollment(
        database,
        existing["device_id"],
        body,
    )

    row = await asyncio.to_thread(
        database.update_event_enrollment,
        ee_id,
        event_enrollment_to_db(body),
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Event enrollment not found",
        )

    return event_enrollment_to_api(row)


@router.delete(
    "/event-enrollments/{ee_id}",
    status_code=204,
)
async def delete_event_enrollment(
    ee_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    deleted = await asyncio.to_thread(
        database.delete_event_enrollment,
        ee_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Event enrollment not found",
        )

    return Response(status_code=204)