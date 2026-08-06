from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import LocationCreate, LocationUpdate


router = APIRouter(
    prefix="/locations",
    tags=["locations"],
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


def _is_descendant_location(
    database: Any,
    candidate_id: int,
    of_location_id: int,
) -> bool:
    """
    Return True when candidate_id is the same location as
    of_location_id or is located anywhere below it.

    This prevents circular parent relationships.
    """
    location_id: Optional[int] = candidate_id

    while location_id is not None:
        if location_id == of_location_id:
            return True

        location = database.get_location(location_id)

        location_id = (
            location["parent_location_id"]
            if location
            else None
        )

    return False


@router.get("")
async def list_locations(
    request: Request,
):
    database = get_database(request)

    return await asyncio.to_thread(
        database.get_locations
    )


@router.post(
    "",
    status_code=201,
)
async def create_location(
    body: LocationCreate,
    request: Request,
):
    database = get_database(request)

    body.validate_semantic()

    if body.parent_location_id is not None:
        parent = await asyncio.to_thread(
            database.get_location,
            body.parent_location_id,
        )

        if parent is None:
            raise HTTPException(
                status_code=404,
                detail="Parent location not found",
            )

    return await asyncio.to_thread(
        database.create_location,
        body.name,
        body.parent_location_id,
        body.description,
        body.kind,
    )


@router.get("/{location_id}")
async def get_location(
    location_id: int,
    request: Request,
):
    database = get_database(request)

    location = await asyncio.to_thread(
        database.get_location,
        location_id,
    )

    if location is None:
        raise HTTPException(
            status_code=404,
            detail="Location not found",
        )

    return location


@router.put("/{location_id}")
async def update_location(
    location_id: int,
    body: LocationUpdate,
    request: Request,
):
    database = get_database(request)

    body.validate_semantic()

    existing = await asyncio.to_thread(
        database.get_location,
        location_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Location not found",
        )

    if body.parent_location_id is not None:
        parent = await asyncio.to_thread(
            database.get_location,
            body.parent_location_id,
        )

        if parent is None:
            raise HTTPException(
                status_code=404,
                detail="Parent location not found",
            )

        creates_cycle = await asyncio.to_thread(
            _is_descendant_location,
            database,
            body.parent_location_id,
            location_id,
        )

        if creates_cycle:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot move a location into itself "
                    "or one of its own sub-locations"
                ),
            )

    updated = await asyncio.to_thread(
        database.update_location,
        location_id,
        body.name,
        body.parent_location_id,
        body.description,
        body.kind,
    )

    return updated


@router.delete(
    "/{location_id}",
    status_code=204,
)
async def delete_location(
    location_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_location,
        location_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Location not found",
        )

    deleted = await asyncio.to_thread(
        database.delete_location,
        location_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=409,
            detail=(
                "Location is not empty — move or remove "
                "its sub-locations and devices first"
            ),
        )

    return Response(status_code=204)