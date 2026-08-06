from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

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
) -> Response:
    content = ede.devices_to_ede(
        devices,
        name,
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
) -> Response:
    graph, warnings = brick_export.build_brick_graph(
        devices,
        project_id=project_id,
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

    return ede_response(
        [device_data],
        device_data["name"],
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

    return brick_response(
        [device_data],
        device_data["name"],
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

    return brick_response(
        devices,
        project["name"],
        project_id=project_id,
    )