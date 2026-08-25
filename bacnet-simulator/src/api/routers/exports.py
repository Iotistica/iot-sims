from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ...bacnet import brick_export, ede


router = APIRouter(tags=["exports"])


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
    """Unlike get_database, a missing engine is not an error here -- EDE
    export's "Current live values" mode simply has no live values to offer
    (every point exports blank) rather than failing the whole export."""
    return getattr(request.app.state, "engine", None)


# ─── Filename helpers ─────────────────────────────────────────────────────────

def export_filename(
    name: str | None,
    extension: str,
) -> str:
    """
    Preserve the current filename behavior while preventing path
    characters from appearing in Content-Disposition.
    """
    safe_name = (name or "export").strip() or "export"

    safe_name = (
        safe_name
        .replace(" ", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace('"', "")
    )

    return f"{safe_name}.{extension}"


# ─── Response builders ────────────────────────────────────────────────────────

def project_json_response(
    project: dict,
) -> Response:
    try:
        project_data = json.loads(
            project["data"]
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Stored project data is invalid",
        ) from exc

    content = json.dumps(
        project_data,
        indent=2,
    )

    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                "attachment; "
                f'filename="{export_filename(project["name"], "json")}"'
            ),
        },
    )


def ede_response(
    devices: list[dict],
    name: str,
    *,
    live_values: dict[int, Any] | None = None,
) -> Response:
    content = ede.devices_to_ede(
        devices,
        name,
        live_values=live_values,
    )

    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                "attachment; "
                f'filename="{export_filename(name, "ede")}"'
            ),
        },
    )


def brick_response(
    devices: list[dict],
    name: str,
    *,
    project_id: int | None = None,
    entities: list[dict] | None = None,
    relationships: list[dict] | None = None,
) -> Response:
    graph, warnings = brick_export.build_brick_graph(
        devices,
        project_id=project_id,
        entities=entities,
        relationships=relationships,
    )

    content = brick_export.graph_to_ttl(
        graph,
        warnings,
    )

    return Response(
        content=content,
        media_type="text/turtle",
        headers={
            "Content-Disposition": (
                "attachment; "
                f'filename="{export_filename(name, "ttl")}"'
            ),
        },
    )


# ─── Project JSON export ──────────────────────────────────────────────────────

@router.get("/profiles/{project_id}/export")
async def export_project_json(
    project_id: int,
    request: Request,
):
    database = get_database(request)

    project = await asyncio.to_thread(
        database.get_project,
        project_id,
    )

    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

    return project_json_response(project)


# ─── EDE exports ──────────────────────────────────────────────────────────────

@router.get("/devices/{device_id}/export/ede")
async def export_device_ede(
    device_id: int,
    request: Request,
    mode: str = Query("defaults", pattern="^(defaults|live)$"),
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

    # Copy the row before adding response-only data in case the
    # database layer returns a reusable mapping.
    device_data = dict(device)

    device_data["objects"] = await asyncio.to_thread(
        database.get_objects,
        device_id,
    )

    live_values: dict[int, Any] | None = None
    if mode == "live":
        engine = get_engine(request)
        # engine=None (e.g. not yet booted) -- {} still switches
        # devices_to_ede into "live mode", exporting every point blank
        # rather than silently falling back to configuration defaults.
        live_values = {
            obj["id"]: engine.get_object_value(obj["id"])
            for obj in device_data["objects"]
        } if engine is not None else {}

    return ede_response(
        [device_data],
        device_data["name"],
        live_values=live_values,
    )


@router.get("/profiles/{project_id}/export/ede")
async def export_project_ede(
    project_id: int,
    request: Request,
):
    database = get_database(request)

    project = await asyncio.to_thread(
        database.get_project,
        project_id,
    )

    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

    try:
        project_data = json.loads(
            project["data"]
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Stored project data is invalid",
        ) from exc

    devices = project_data.get("devices", [])

    if not isinstance(devices, list):
        raise HTTPException(
            status_code=500,
            detail="Stored project devices are invalid",
        )

    return ede_response(
        devices,
        project["name"],
    )


# ─── Brick Core semantic data (entities/relationships) ────────────────────────

def _semantic_data_for_devices(
    database: Any,
    devices: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Live semantic_entities/semantic_relationships scoped to the given
    devices and their objects -- entities/relationships tables don't have
    a "device_id or any of these object_ids" bulk filter, and export isn't
    a hot path, so this fetches everything for the device(s) + objects,
    then filters relationships in Python rather than adding a new
    Database query method for a single caller."""
    entities: list[dict] = []
    for dev in devices:
        entities.extend(database.get_semantic_entities(device_id=dev["id"]))
        for obj in dev.get("objects", []):
            entities.extend(database.get_semantic_entities(object_id=obj["id"]))

    entity_ids = {e["id"] for e in entities}
    relationships = [
        rel for rel in database.get_semantic_relationships()
        if rel["source_entity_id"] in entity_ids and rel["target_entity_id"] in entity_ids
    ]

    return entities, relationships


# ─── Brick exports ────────────────────────────────────────────────────────────

@router.get("/devices/{device_id}/export/brick")
async def export_device_brick(
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

    device_data = dict(device)

    device_data["objects"] = await asyncio.to_thread(
        database.get_objects,
        device_id,
    )

    entities, relationships = await asyncio.to_thread(
        _semantic_data_for_devices, database, [device_data],
    )

    return brick_response(
        [device_data],
        device_data["name"],
        entities=entities,
        relationships=relationships,
    )


@router.get("/profiles/{project_id}/export/brick")
async def export_project_brick(
    project_id: int,
    request: Request,
):
    database = get_database(request)

    project = await asyncio.to_thread(
        database.get_project,
        project_id,
    )

    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

    try:
        project_data = json.loads(
            project["data"]
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Stored project data is invalid",
        ) from exc

    devices = project_data.get("devices", [])

    if not isinstance(devices, list):
        raise HTTPException(
            status_code=500,
            detail="Stored project devices are invalid",
        )

    # Unlike export_device_brick, this can't pull semantic_entities/
    # semantic_relationships live from the database -- a stored project
    # isn't necessarily the one currently loaded into the live tables, so
    # there'd be nothing (or the WRONG project's data) to query. Instead
    # this reads them straight out of the stored snapshot, which
    # save_project()/update_project() now embed alongside devices/locations.
    return brick_response(
        devices,
        project["name"],
        project_id=project_id,
        entities=project_data.get("semantic_entities", []),
        relationships=project_data.get("semantic_relationships", []),
    )