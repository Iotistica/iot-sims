from __future__ import annotations

import asyncio
import json
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
from ...core.config import COMMANDABLE_TYPES, VALID_BEHAVIORS
from ...simulation.model_store import (
    get_aggregate_membership_owner,
    get_exposure_owners_by_point,
    get_output_owners_by_point,
)
from ..guards import reject_external_device, reject_external_object_source_mutation


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


# Fields the Activity Log describes individually when they change on a PUT
# .../objects/{id}. behavior_params and pre_fault_* (the fault-restore
# snapshot -- see update_object() below) are deliberately excluded here and
# diffed separately: behavior_params is a JSON blob, not a scalar, and
# pre_fault_* is an internal bookkeeping field a user never directly edits,
# so surfacing it verbatim would just be noise on every fault edit.
_LOGGED_OBJECT_FIELDS = (
    "name", "units", "behavior", "enabled",
    "reliability", "polarity", "point_type", "number_of_states",
)


def _describe_object_changes(existing: dict, body: "ObjectUpdate") -> str:
    """Builds the Activity Log's "what changed" summary for an object edit
    -- e.g. "units 'percent' -> 'no-units'; reliability 'no-fault-detected'
    -> 'communication-failure'" -- instead of the old generic
    "configuration updated", which gave no way to tell what a given edit
    actually did without cross-referencing the DB."""
    changes: list[str] = []

    for field in _LOGGED_OBJECT_FIELDS:
        old = existing.get(field)
        new = getattr(body, field)
        if field == "enabled":
            old, new = bool(old), bool(new)
        if old != new:
            changes.append(f"{field} {old!r} -> {new!r}")

    try:
        old_params = json.loads(existing.get("behavior_params") or "{}")
    except (TypeError, ValueError):
        old_params = {}
    try:
        new_params = json.loads(body.behavior_params or "{}")
    except (TypeError, ValueError):
        new_params = {}
    if not isinstance(old_params, dict):
        old_params = {}
    if not isinstance(new_params, dict):
        new_params = {}
    # pre_fault_behavior/pre_fault_params are update_object()'s own
    # internal snapshot fields (see below) -- never something the caller
    # deliberately changed, so excluded from the diff even though they
    # live inside the same JSON blob as e.g. a constant's "value".
    old_params = {k: v for k, v in old_params.items() if not k.startswith("pre_fault_")}
    new_params = {k: v for k, v in new_params.items() if not k.startswith("pre_fault_")}
    if old_params != new_params:
        param_keys = sorted(set(old_params) | set(new_params))
        param_changes = [
            f"{key} {old_params.get(key)!r} -> {new_params.get(key)!r}"
            for key in param_keys
            if old_params.get(key) != new_params.get(key)
        ]
        if param_changes:
            changes.append("params: " + ", ".join(param_changes))

    return "; ".join(changes) if changes else "configuration updated"


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
    engine = get_engine(request)

    await require_device(
        database,
        device_id,
    )

    objects = await asyncio.to_thread(
        database.get_objects,
        device_id,
    )
    point_ids = [int(obj["id"]) for obj in objects]
    output_owners = await asyncio.to_thread(
        get_output_owners_by_point,
        database,
        point_ids,
    )
    exposure_owners = await asyncio.to_thread(
        get_exposure_owners_by_point,
        database,
        point_ids,
    )
    # getattr defensively -- test doubles and any future engine
    # implementation don't all carry this attribute, and its absence just
    # means "no diagnostics available," never an error.
    raw_provider_values = getattr(engine, "_raw_provider_values", None)
    for obj in objects:
        pid = int(obj["id"])
        owner = output_owners.get(pid) or exposure_owners.get(pid)
        obj["simulation_output_owner"] = owner
        obj["raw_provider_value"] = (
            raw_provider_values.get(pid) if owner and raw_provider_values else None
        )
    return objects


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
    reject_external_device(device)

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

    device = await require_device(
        database,
        device_id,
    )

    existing = await require_object(
        database,
        device_id,
        object_id,
    )
    body_dict = body.model_dump()
    reject_external_object_source_mutation(device, existing, body_dict)

    if body.behavior == "fault" and existing["behavior"] != "fault":
        # Genuinely entering fault (not already there): unconditionally
        # snapshot the point's pre-fault behavior/params under
        # pre_fault_behavior/pre_fault_params -- fields dedicated to this
        # restore feature, deliberately separate from FaultBehavior's own
        # base_behavior/base_params (see behaviors.py's FaultBehavior),
        # which mean something different (what to compute BETWEEN fault
        # injections while fault is still the active behavior -- a real,
        # independently-configurable choice, not necessarily "whatever it
        # was a moment ago", so it must never be silently overwritten
        # here). Always captured from the real prior DB row, never from
        # whatever the caller happened to submit -- that's what makes
        # POST .../restore-behavior below able to put the point back
        # exactly, for any prior behavior type (constant, manual, schedule,
        # raw, ...), independent of what the drawer's own Base Behavior
        # editor even offers as options. Guarded on existing["behavior"] !=
        # "fault" so a later in-fault edit (e.g. tweaking fault_value)
        # never re-snapshots and loses the original pre-fault state.
        try:
            submitted_params = json.loads(body.behavior_params or "{}")
        except (TypeError, ValueError):
            submitted_params = {}
        if not isinstance(submitted_params, dict):
            submitted_params = {}
        try:
            existing_params = json.loads(existing["behavior_params"] or "{}")
        except (TypeError, ValueError):
            existing_params = {}
        submitted_params["pre_fault_behavior"] = existing["behavior"]
        submitted_params["pre_fault_params"] = existing_params if isinstance(existing_params, dict) else {}
        body_dict["behavior_params"] = json.dumps(submitted_params)

    try:
        updated = await asyncio.to_thread(
            database.update_object,
            object_id,
            body_dict,
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

    log_event(
        request,
        device_id,
        "info",
        f"Object {existing['name']}: {_describe_object_changes(existing, body)}",
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

    device = await require_device(
        database,
        device_id,
    )
    reject_external_device(device)

    obj = await require_object(
        database,
        device_id,
        object_id,
    )

    aggregate_owner = await asyncio.to_thread(get_aggregate_membership_owner, database, object_id)
    if aggregate_owner is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"Point {obj['name']!r} is a source member of the "
                    f"{aggregate_owner['variable']!r} aggregate mapping on "
                    f"simulation model {aggregate_owner['model_name']!r}. "
                    "Remove it from the aggregate first."
                ),
                "owner": aggregate_owner,
            },
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

    device = await require_device(
        database,
        device_id,
    )
    reject_external_device(device)

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


@router.post("/{object_id}/restore-behavior")
async def restore_object_behavior(
    device_id: int,
    object_id: int,
    request: Request,
):
    """"Restore Previous"/"Return to Normal": undoes a temporary `fault`
    behavior, putting the point back exactly as it was configured before
    the fault was applied.

    Reads pre_fault_behavior/pre_fault_params snapshotted onto the fault's
    own behavior_params by update_object() above, the moment behavior was
    switched to "fault" -- deliberately separate fields from
    FaultBehavior's own base_behavior/base_params (see behaviors.py),
    which mean "what to compute between fault injections" rather than
    "what to restore to". Restoring is a plain behavior/behavior_params
    field update through the same database.update_object()/
    schedule_engine_reload() path an ordinary edit uses -- it never
    touches simulation_model_mappings, so it can't create a self-loop or
    change provider ownership, and (per engine.py's reload()/
    _create_object() preservation logic) a provider-owned point being
    restored to "raw" keeps its current live value across the resulting
    reload rather than resetting to 0.
    """
    database = get_database(request)

    device = await require_device(database, device_id)
    reject_external_device(device)

    obj = await require_object(database, device_id, object_id)

    if obj["behavior"] != "fault":
        raise HTTPException(
            status_code=409,
            detail="Object is not currently in fault behavior -- nothing to restore",
        )

    try:
        fault_params = json.loads(obj.get("behavior_params") or "{}")
    except (TypeError, ValueError):
        fault_params = {}
    if not isinstance(fault_params, dict):
        fault_params = {}

    restore_behavior = fault_params.get("pre_fault_behavior")
    restore_params = fault_params.get("pre_fault_params")

    if not isinstance(restore_behavior, str) or restore_behavior not in VALID_BEHAVIORS or restore_behavior == "fault":
        # No usable snapshot (a fault created before this feature existed,
        # or with a corrupted/self-referential pre_fault_behavior). Fall
        # back by ownership rather than guessing: "raw" only ever makes
        # sense for a point a simulation model is actively writing to --
        # never invent it for a point that isn't provider-owned.
        point_ids = [object_id]
        output_owners = await asyncio.to_thread(get_output_owners_by_point, database, point_ids)
        exposure_owners = await asyncio.to_thread(get_exposure_owners_by_point, database, point_ids)
        is_provider_owned = bool(output_owners.get(object_id) or exposure_owners.get(object_id))
        restore_behavior = "raw" if is_provider_owned else "constant"
        restore_params = {} if is_provider_owned else {"value": 0}
    if not isinstance(restore_params, dict):
        restore_params = {}

    body_dict = dict(obj)
    body_dict["behavior"] = restore_behavior
    body_dict["behavior_params"] = json.dumps(restore_params)

    updated = await asyncio.to_thread(
        database.update_object,
        object_id,
        body_dict,
    )

    if updated is None:
        raise HTTPException(status_code=404, detail="Object not found")

    log_event(
        request,
        device_id,
        "info",
        f"Object {obj['name']}: fault removed, restored to {restore_behavior}",
    )

    schedule_engine_reload(request)

    return updated


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

    device = await require_device(
        database,
        device_id,
    )
    reject_external_device(device)

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
