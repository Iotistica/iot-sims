from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import (
    ProjectCreate,
    ProjectImport,
    ProjectUpdate,
)


router = APIRouter(
    prefix="/profiles",
    tags=["projects"],
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


def schedule_engine_reload(request: Request) -> None:
    engine = get_engine(request)
    asyncio.create_task(engine.reload())


def payload_fields(body: Any) -> set[str]:
    return set(
        getattr(
            body,
            "model_fields_set",
            getattr(body, "__fields_set__", set()),
        )
    )


# ─── Project CRUD ──────────────────────────────────────────────────────────────

@router.get("")
async def list_projects(
    request: Request,
):
    database = get_database(request)

    return await asyncio.to_thread(
        database.get_projects
    )


@router.post(
    "",
    status_code=201,
)
async def create_project(
    body: ProjectCreate,
    request: Request,
):
    database = get_database(request)

    if body.above_ground_levels > 0 or body.below_ground_levels > 0:
        await asyncio.to_thread(
            database.generate_building_levels,
            body.name,
            body.above_ground_levels,
            body.below_ground_levels,
        )

    return await asyncio.to_thread(
        database.save_project,
        body.name,
        body.description,
        body.connection_config,
        body.discovery_connections,
    )


@router.put("/{project_id}")
async def update_project(
    project_id: int,
    body: ProjectUpdate,
    request: Request,
):
    database = get_database(request)
    fields = payload_fields(body)

    updated = await asyncio.to_thread(
        database.update_project,
        project_id,
        body.name,
        body.description,
        body.connection_config,
        body.discovery_connections,
        "connection_config" in fields,
        "discovery_connections" in fields,
    )

    if not updated:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

    return {"ok": True}


@router.delete(
    "/{project_id}",
    status_code=204,
)
async def delete_project(
    project_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    deleted = await asyncio.to_thread(
        database.delete_project,
        project_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

    return Response(status_code=204)


# ─── Load project into the live simulator ─────────────────────────────────────

@router.post("/{project_id}/load")
async def load_project(
    project_id: int,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    loaded = await asyncio.to_thread(
        database.load_project,
        project_id,
    )

    if loaded is None:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

    # Rebuild the live BACnet application from the newly loaded
    # project contents.
    schedule_engine_reload(request)

    # Preserve the existing behavior: a loaded project starts
    # paused/reset until the user explicitly starts the simulation.
    engine.reset()

    return {
        "ok": True,
        "connection_config": loaded["connection_config"],
        "discovery_connections": loaded.get("discovery_connections"),
    }


@router.post("/clear")
async def clear_live_project(
    request: Request,
):
    """
    Wipes live project state back to blank for the "New Project" flow --
    reuses load_project()'s own wipe sequence (Database.clear_live_state())
    rather than the frontend looping individual device/location deletes,
    which could leave locations permanently stuck (delete_location()
    refuses to remove a location a leftover semantic entity still points
    at, and per-row deletes never touch semantic tables at all).
    """
    database = get_database(request)
    engine = get_engine(request)

    await asyncio.to_thread(database.clear_live_state)

    schedule_engine_reload(request)
    engine.reset()

    return {"ok": True}


# ─── JSON project import ───────────────────────────────────────────────────────

@router.post(
    "/import",
    status_code=201,
)
async def import_project(
    body: ProjectImport,
    request: Request,
):
    database = get_database(request)

    return await asyncio.to_thread(
        database.import_project,
        body.name,
        body.description,
        body.data,
    )
