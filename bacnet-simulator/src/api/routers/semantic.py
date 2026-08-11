from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ...bacnet.schemas import SemanticEntityCreate, SemanticEntityUpdate, SemanticRelationshipCreate


router = APIRouter(tags=["semantic"])


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


def _would_create_ispartof_cycle(
    database: Any,
    source_entity_id: int,
    target_entity_id: int,
) -> bool:
    """
    Walk isPartOf edges outward from target_entity_id (what target is
    itself part of, transitively). If source_entity_id is reachable,
    adding source isPartOf target would close a cycle.
    """
    visited: set[int] = set()
    stack = [target_entity_id]

    while stack:
        entity_id = stack.pop()

        if entity_id == source_entity_id:
            return True

        if entity_id in visited:
            continue

        visited.add(entity_id)

        parents = database.get_related_entities(entity_id, "isPartOf", direction="out")
        stack.extend(p["id"] for p in parents)

    return False


# ─── Semantic entities ─────────────────────────────────────────────────────

@router.get("/semantic-entities")
async def list_semantic_entities(
    request: Request,
    device_id: Optional[int] = Query(None),
    object_id: Optional[int] = Query(None),
    location_id: Optional[int] = Query(None),
    equipment_id: Optional[int] = Query(None),
    entity_kind: Optional[str] = Query(None),
    brick_class: Optional[str] = Query(None),
):
    database = get_database(request)

    return await asyncio.to_thread(
        database.get_semantic_entities,
        device_id=device_id,
        object_id=object_id,
        location_id=location_id,
        equipment_id=equipment_id,
        entity_kind=entity_kind,
        brick_class=brick_class,
    )


@router.post(
    "/semantic-entities",
    status_code=201,
)
async def create_semantic_entity(
    body: SemanticEntityCreate,
    request: Request,
):
    database = get_database(request)

    body.validate_semantic()

    try:
        return await asyncio.to_thread(
            database.create_semantic_entity,
            body.name,
            body.brick_class,
            body.entity_kind,
            device_id=body.device_id,
            object_id=body.object_id,
            location_id=body.location_id,
            equipment_id=body.equipment_id,
            local_slug=body.local_slug,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.IntegrityError as e:
        raise HTTPException(
            status_code=409,
            detail=(
                "An entity of this class already exists for the selected "
                "device/object/location -- edit the existing entity instead "
                "of creating a duplicate, or give this one a distinct local "
                "slug to disambiguate it."
            ),
        ) from e


@router.get("/semantic-entities/{entity_id}")
async def get_semantic_entity(
    entity_id: int,
    request: Request,
):
    database = get_database(request)

    entity = await asyncio.to_thread(
        database.get_semantic_entity,
        entity_id,
    )

    if entity is None:
        raise HTTPException(
            status_code=404,
            detail="Semantic entity not found",
        )

    return entity


@router.put("/semantic-entities/{entity_id}")
async def update_semantic_entity(
    entity_id: int,
    body: SemanticEntityUpdate,
    request: Request,
):
    database = get_database(request)

    body.validate_semantic()

    existing = await asyncio.to_thread(
        database.get_semantic_entity,
        entity_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Semantic entity not found",
        )

    try:
        updated = await asyncio.to_thread(
            database.update_semantic_entity,
            entity_id,
            body.name,
            body.brick_class,
            body.entity_kind,
            device_id=body.device_id,
            object_id=body.object_id,
            location_id=body.location_id,
            equipment_id=body.equipment_id,
            local_slug=body.local_slug,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.IntegrityError as e:
        raise HTTPException(
            status_code=409,
            detail=(
                "Another entity already has this class/device/object/"
                "location/slug combination -- choose a different class, "
                "target, or local slug."
            ),
        ) from e

    return updated


@router.delete(
    "/semantic-entities/{entity_id}",
    status_code=204,
)
async def delete_semantic_entity(
    entity_id: int,
    request: Request,
) -> Response:
    """Cascades to semantic_relationships referencing this entity (real
    ON DELETE CASCADE FK) -- unlike locations.py's refuse-and-409 pattern,
    since a relationship pointing at a deleted entity has no independent
    meaning left to protect."""
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_semantic_entity,
        entity_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Semantic entity not found",
        )

    await asyncio.to_thread(
        database.delete_semantic_entity,
        entity_id,
    )

    return Response(status_code=204)


@router.get("/semantic-entities/{entity_id}/points")
async def get_semantic_entity_points(
    entity_id: int,
    request: Request,
    brick_class: Optional[str] = Query(None),
):
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_semantic_entity,
        entity_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Semantic entity not found",
        )

    return await asyncio.to_thread(
        database.get_entity_points,
        entity_id,
        brick_class,
    )


@router.get("/semantic-entities/{entity_id}/related")
async def get_semantic_entity_related(
    entity_id: int,
    request: Request,
    predicate: str = Query(...),
    direction: str = Query("out"),
):
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_semantic_entity,
        entity_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Semantic entity not found",
        )

    if direction not in ("out", "in"):
        raise HTTPException(
            status_code=400,
            detail="direction must be 'out' or 'in'",
        )

    return await asyncio.to_thread(
        database.get_related_entities,
        entity_id,
        predicate,
        direction,
    )


# ─── Semantic relationships ─────────────────────────────────────────────────

@router.get("/semantic-relationships")
async def list_semantic_relationships(
    request: Request,
    source_entity_id: Optional[int] = Query(None),
    target_entity_id: Optional[int] = Query(None),
    predicate: Optional[str] = Query(None),
):
    database = get_database(request)

    return await asyncio.to_thread(
        database.get_semantic_relationships,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        predicate=predicate,
    )


@router.post(
    "/semantic-relationships",
    status_code=201,
)
async def create_semantic_relationship(
    body: SemanticRelationshipCreate,
    request: Request,
):
    database = get_database(request)

    body.validate_semantic()

    for entity_id in (body.source_entity_id, body.target_entity_id):
        entity = await asyncio.to_thread(
            database.get_semantic_entity,
            entity_id,
        )
        if entity is None:
            raise HTTPException(
                status_code=404,
                detail=f"Semantic entity {entity_id} not found",
            )

    if body.predicate == "isPartOf":
        creates_cycle = await asyncio.to_thread(
            _would_create_ispartof_cycle,
            database,
            body.source_entity_id,
            body.target_entity_id,
        )

        if creates_cycle:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot create this isPartOf relationship -- it would "
                    "make an entity part of itself, directly or transitively"
                ),
            )

    return await asyncio.to_thread(
        database.create_semantic_relationship,
        body.source_entity_id,
        body.predicate,
        body.target_entity_id,
    )


@router.delete(
    "/semantic-relationships/{relationship_id}",
    status_code=204,
)
async def delete_semantic_relationship(
    relationship_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_semantic_relationship,
        relationship_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Semantic relationship not found",
        )

    await asyncio.to_thread(
        database.delete_semantic_relationship,
        relationship_id,
    )

    return Response(status_code=204)
