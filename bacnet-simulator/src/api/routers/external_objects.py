from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ...bacnet.client import DiscoveredDevice
from .discovery import DiscoveryBindError, _discovery_session, raise_discovery_bind_error

router = APIRouter(
    prefix="/devices/{device_id}/external-objects",
    tags=["external-objects"],
)

# Deliberately does NOT import from objects.py (engine/write_priority/
# set_manual_value/add_object_hot live there) -- kept in its own file so
# "external objects are never writable" is a structural, file-boundary
# fact, not just a convention every route here happens to follow.


def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return database


async def require_external_device(database: Any, device_id: int) -> dict:
    device = await asyncio.to_thread(database.get_device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.get("source_type") != "external-bacnet":
        raise HTTPException(status_code=400, detail="Not an external BACnet device")
    return device


@router.post("/discover")
async def discover_external_objects(device_id: int, request: Request):
    """
    Reads Object_List + per-object properties for this external device
    (reusing the existing read-only BACnetDiscovery.validate()), upserts
    the structural object identity into the project inventory, and returns
    those rows merged with the just-read present values (not persisted).
    """
    database = get_database(request)
    device = await require_external_device(database, device_id)

    try:
        async with _discovery_session() as discovery:
            result = await discovery.validate(DiscoveredDevice(
                name=device["name"],
                fingerprint="",
                host=device["external_host"],
                port=device["external_port"] or 47808,
                device_instance=device["device_instance"],
            ))
    except DiscoveryBindError as exc:
        raise_discovery_bind_error(request, exc)

    points = [
        {
            "object_type": p["objectType"],
            "object_instance": p["objectInstance"],
            "name": p["objectName"] or f"{p['objectType']}_{p['objectInstance']}",
            "units": p["unit"] or "no-units",
            "description": p.get("description"),
        }
        for p in result["dataPoints"]
    ]
    persisted = await asyncio.to_thread(database.sync_external_objects, device_id, points)
    await asyncio.to_thread(database.touch_external_device_last_seen, device_id)

    present_values = {
        (p["objectType"], p["objectInstance"]): p["presentValue"]
        for p in result["dataPoints"]
    }
    return {
        "objects": [
            {**row, "present_value": present_values.get((row["object_type"], row["object_instance"]))}
            for row in persisted
        ]
    }


@router.post("/refresh")
async def refresh_external_objects(device_id: int, request: Request):
    """
    Re-reads Present-Value only for this device's already-persisted
    objects -- no Object_List re-read, distinct from /discover (see
    BACnetDiscovery.read_present_values's docstring).
    """
    database = get_database(request)
    device = await require_external_device(database, device_id)

    existing = await asyncio.to_thread(database.get_objects, device_id)
    points = [(o["object_type"], o["object_instance"]) for o in existing]

    try:
        async with _discovery_session() as discovery:
            values = await discovery.read_present_values(
                device["external_host"], device["device_instance"], points,
            )
    except DiscoveryBindError as exc:
        raise_discovery_bind_error(request, exc)

    await asyncio.to_thread(database.touch_external_device_last_seen, device_id)

    return {
        "objects": [
            {**o, "present_value": values.get((o["object_type"], o["object_instance"]))}
            for o in existing
        ]
    }
