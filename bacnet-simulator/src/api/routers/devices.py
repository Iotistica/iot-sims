from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, Callable, MutableMapping

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import DeviceCreate, DeviceUpdate, EnergyModelConfigCreate
from ...energy.registry import energy_model_config_to_api, validate_energy_model_parameters
from ...simulation.models.store import (
    get_active_simulation_models_by_device,
    get_devices_with_disabled_simulation_model,
)
from ..guards import reject_external_device, reject_external_source_mutation


router = APIRouter(
    prefix="/devices",
    tags=["devices"],
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


def get_device_names(
    request: Request,
) -> MutableMapping[int, str]:
    device_names = getattr(
        request.app.state,
        "device_names",
        None,
    )

    if device_names is None:
        raise HTTPException(
            status_code=503,
            detail="Device-name registry is unavailable",
        )

    return device_names


def get_event_logger(
    request: Request,
) -> Callable[[int | None, str, str], None] | None:
    return getattr(
        request.app.state,
        "log_event",
        None,
    )


def effective_can_receive_events(
    request: Request,
    device: dict,
) -> bool:
    resolver = getattr(
        request.app.state,
        "effective_can_receive_events",
        None,
    )

    if resolver is not None:
        return bool(resolver(device))

    # Defensive fallback matching the existing simulator behavior.
    override = device.get(
        "can_receive_event_notifications"
    )

    if override is not None:
        return bool(override)

    return device.get("equipment_type") is None


def log_event(
    request: Request,
    device_id: int,
    level: str,
    message: str,
) -> None:
    callback = get_event_logger(request)

    if callback is not None:
        callback(device_id, level, message)


def schedule_engine_reload(
    request: Request,
) -> None:
    engine = get_engine(request)
    asyncio.create_task(engine.reload())


# ─── Validation ────────────────────────────────────────────────────────────────

async def validate_location(
    database: Any,
    location_id: int | None,
) -> None:
    if location_id is None:
        return

    location = await asyncio.to_thread(
        database.get_location,
        location_id,
    )

    if location is None:
        raise HTTPException(
            status_code=404,
            detail="Location not found",
        )


# ─── Device routes ─────────────────────────────────────────────────────────────

@router.get("")
async def list_devices(
    request: Request,
):
    database = get_database(request)

    devices = await asyncio.to_thread(
        database.get_devices
    )
    simulation_models_by_device = await asyncio.to_thread(
        get_active_simulation_models_by_device,
        database,
    )
    stopped_simulation_device_ids = await asyncio.to_thread(
        get_devices_with_disabled_simulation_model,
        database,
    )
    replay_recording_ids = [
        int(d["replay_recording_id"]) for d in devices if d.get("replay_recording_id") is not None
    ]
    replay_recording_names = await asyncio.to_thread(
        database.get_replay_recording_names,
        replay_recording_ids,
    )

    for device in devices:
        device[
            "effective_can_receive_event_notifications"
        ] = effective_can_receive_events(
            request,
            device,
        )
        device_id = int(device["id"])
        active_model = simulation_models_by_device.get(device_id)
        device["active_simulation_model"] = active_model
        device["simulation_model_stopped"] = (
            active_model is None and device_id in stopped_simulation_device_ids
        )
        recording_id = device.get("replay_recording_id")
        device["active_replay_recording"] = (
            {"id": recording_id, "name": replay_recording_names[recording_id]}
            if recording_id is not None and recording_id in replay_recording_names
            else None
        )

    return devices


@router.post(
    "",
    status_code=201,
)
async def create_device(
    body: DeviceCreate,
    request: Request,
):
    database = get_database(request)

    body.validate_device_info()
    body.validate_semantic()

    await validate_location(
        database,
        body.location_id,
    )

    try:
        device = await asyncio.to_thread(
            database.create_device,
            body.model_dump(),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Device instance "
                f"{body.device_instance} already exists"
            ),
        ) from exc

    device_names = get_device_names(request)
    device_names[device["id"]] = device["name"]

    log_event(
        request,
        device["id"],
        "info",
        (
            f"Device created: {device['name']} "
            f"(instance {device['device_instance']})"
        ),
    )

    schedule_engine_reload(request)

    return device


@router.get("/{device_id}")
async def get_device(
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

    return device


@router.put("/{device_id}")
async def update_device(
    device_id: int,
    body: DeviceUpdate,
    request: Request,
):
    database = get_database(request)

    body.validate_device_info()
    body.validate_semantic()

    existing = await asyncio.to_thread(
        database.get_device,
        device_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )

    body_dict = body.model_dump()
    reject_external_source_mutation(existing, body_dict)

    await validate_location(
        database,
        body.location_id,
    )

    try:
        updated = await asyncio.to_thread(
            database.update_device,
            device_id,
            body_dict,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Device instance "
                f"{body.device_instance} already exists"
            ),
        ) from exc

    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )

    device_names = get_device_names(request)
    device_names[device_id] = body.name

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
            f"Device {state}",
        )

    elif existing["name"] != body.name:
        log_event(
            request,
            device_id,
            "info",
            f"Device renamed to {body.name!r}",
        )

    elif (
        existing.get("location_id")
        != body.location_id
    ):
        log_event(
            request,
            device_id,
            "info",
            "Device moved to a different location",
        )

    else:
        log_event(
            request,
            device_id,
            "info",
            "Device configuration updated",
        )

    schedule_engine_reload(request)

    return updated


@router.delete(
    "/{device_id}",
    status_code=204,
)
async def delete_device(
    device_id: int,
    request: Request,
) -> Response:
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

    # Deliberately no reject_external_device() here: removing a device from
    # the project inventory performs no BACnet action and doesn't touch
    # simulator ownership, so it's allowed for external devices too --
    # unlike every object/value/priority/simulation mutation below, which
    # stays blocked.
    log_event(
        request,
        device_id,
        "warn",
        (
            f"Device removed: {device['name']} "
            f"(instance {device['device_instance']})"
        ),
    )

    try:
        deleted = await asyncio.to_thread(
            database.delete_device,
            device_id,
        )
    except sqlite3.IntegrityError as exc:
        # A cascade from this device's objects hit an aggregate member's
        # ON DELETE RESTRICT (see models.store.ensure_simulation_model_schema).
        # Coarser than delete_object's error (doesn't name the exact point/
        # aggregate) since this is a safety-net path, not the primary one --
        # the actionable error is delete_object's.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Device {device['name']!r} cannot be deleted: it owns one "
                "or more points that are still aggregate-mapping members."
            ),
        ) from exc

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )


    schedule_engine_reload(request)

    return Response(status_code=204)


# ─── Controller (explicit semantic role) ────────────────────────────────────

@router.post("/{device_id}/controller")
async def mark_device_as_controller(
    device_id: int,
    request: Request,
):
    """The ONLY code path in the backend that ever creates/updates an
    entity_kind='controller' semantic entity -- deliberately not folded
    into create_device()/update_device() (see Database.ensure_controller_entity
    and sync_controller_entity()'s docstrings). Idempotent: calling this
    again on a device that already has a controller entity just keeps its
    name in sync, never creates a second one."""
    database = get_database(request)

    entity = await asyncio.to_thread(
        database.ensure_controller_entity,
        device_id,
    )

    if entity is None:
        raise HTTPException(
            status_code=404,
            detail="Device not found",
        )

    return entity


# ─── Energy model configs ───────────────────────────────────────────────────

@router.get("/{device_id}/energy-models")
async def list_device_energy_models(
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
        database.get_energy_model_configs,
        device_id,
    )

    return [energy_model_config_to_api(row) for row in rows]


@router.post(
    "/{device_id}/energy-models",
    status_code=201,
)
async def create_device_energy_model(
    device_id: int,
    body: EnergyModelConfigCreate,
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

    reject_external_device(device)

    try:
        validate_energy_model_parameters(body.model_type, body.parameters)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # device_id + model_type + instance_key is the composite identity
    # (UNIQUE constraint) -- upsert so POSTing the same tuple again updates
    # it in place rather than raising a duplicate-key error. Multiple
    # instances of the same model_type on one device are allowed by
    # design (e.g. scenario-comparison chiller configs), disambiguated by
    # instance_key ("Model Name" in the UI) -- no cardinality restriction.
    row = await asyncio.to_thread(
        database.upsert_energy_model_config,
        device_id,
        body.model_type,
        json.dumps(body.parameters),
        body.enabled,
        body.instance_key,
    )

    log_event(
        request,
        device_id,
        "info",
        (
            f"Energy model configured: {body.model_type} "
            f"({body.instance_key})"
        ),
    )

    return energy_model_config_to_api(row)
