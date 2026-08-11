from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import EquipmentCreate, EquipmentUpdate


router = APIRouter(
    prefix="/equipment",
    tags=["equipment"],
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


@router.get("")
async def list_equipment(
    request: Request,
):
    database = get_database(request)

    return await asyncio.to_thread(
        database.get_equipment_list
    )


@router.post(
    "",
    status_code=201,
)
async def create_equipment(
    body: EquipmentCreate,
    request: Request,
):
    database = get_database(request)

    body.validate_semantic()

    if body.location_id is not None:
        location = await asyncio.to_thread(
            database.get_location,
            body.location_id,
        )

        if location is None:
            raise HTTPException(
                status_code=404,
                detail="Location not found",
            )

    return await asyncio.to_thread(
        database.create_equipment,
        body.name,
        body.description,
        body.location_id,
        body.equipment_type,
    )


@router.get("/{equipment_id}")
async def get_equipment(
    equipment_id: int,
    request: Request,
):
    database = get_database(request)

    equipment = await asyncio.to_thread(
        database.get_equipment,
        equipment_id,
    )

    if equipment is None:
        raise HTTPException(
            status_code=404,
            detail="Equipment not found",
        )

    return equipment


@router.get("/{equipment_id}/assignable-points")
async def get_assignable_points(
    equipment_id: int,
    request: Request,
):
    """Candidate objects for the Equipment panel's Assign Points action --
    see Database.get_assignable_points_for_equipment()'s docstring. A
    single aggregate read, not a per-object round trip, to avoid N+1 from
    the frontend."""
    database = get_database(request)

    equipment = await asyncio.to_thread(
        database.get_equipment,
        equipment_id,
    )

    if equipment is None:
        raise HTTPException(
            status_code=404,
            detail="Equipment not found",
        )

    return await asyncio.to_thread(
        database.get_assignable_points_for_equipment,
        equipment_id,
    )


@router.put("/{equipment_id}")
async def update_equipment(
    equipment_id: int,
    body: EquipmentUpdate,
    request: Request,
):
    database = get_database(request)

    body.validate_semantic()

    existing = await asyncio.to_thread(
        database.get_equipment,
        equipment_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Equipment not found",
        )

    if body.location_id is not None:
        location = await asyncio.to_thread(
            database.get_location,
            body.location_id,
        )

        if location is None:
            raise HTTPException(
                status_code=404,
                detail="Location not found",
            )

    updated = await asyncio.to_thread(
        database.update_equipment,
        equipment_id,
        body.name,
        body.description,
        body.location_id,
        body.equipment_type,
    )

    return updated


@router.delete(
    "/{equipment_id}",
    status_code=204,
)
async def delete_equipment(
    equipment_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_equipment,
        equipment_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Equipment not found",
        )

    await asyncio.to_thread(
        database.delete_equipment,
        equipment_id,
    )

    return Response(status_code=204)
