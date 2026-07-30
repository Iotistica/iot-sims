"""Profile save/load/export routes — split out of opcua_simulator.py."""
import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import lib.state as state

router = APIRouter()


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""


class ProfileUpdate(ProfileCreate):
    pass


class ProfileImport(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    data: dict


@router.get("/projects")
async def list_projects():
    return await asyncio.to_thread(state.db.get_projects)


@router.post("/projects", status_code=201)
async def save_project(body: ProfileCreate):
    return await asyncio.to_thread(state.db.save_project, body.name, body.description)


@router.put("/projects/{project_id}")
async def update_project(project_id: int, body: ProfileUpdate):
    ok = await asyncio.to_thread(state.db.update_project, project_id, body.name, body.description)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"ok": True}


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: int):
    deleted = await asyncio.to_thread(state.db.delete_project, project_id)
    if not deleted:
        raise HTTPException(404, "Profile not found")


@router.post("/projects/{project_id}/load")
async def load_project(project_id: int):
    row = await asyncio.to_thread(state.db.get_project, project_id)
    if not row:
        raise HTTPException(404, "Profile not found")
    data = json.loads(row["data"])

    await asyncio.to_thread(state.db.replace_live_state, data)
    # Single full resync covers teardown of whatever was live before plus
    # folder-aware rebuild from the freshly-replaced DB state in one step —
    # replaces the old hand-rolled per-device teardown/rebuild loop that
    # predates folders and didn't know how to order folder creation.
    await state.engine.resync_live_state()

    for dev in await asyncio.to_thread(state.db.get_devices):
        state._device_names[dev["id"]] = dev["name"]

    # A freshly loaded project starts paused at t=0 — press Start when ready.
    state.engine.reset()
    return {"ok": True}


@router.get("/projects/{project_id}/export")
async def export_project(project_id: int):
    row = await asyncio.to_thread(state.db.get_project, project_id)
    if not row:
        raise HTTPException(404, "Profile not found")
    content = json.dumps(json.loads(row["data"]), indent=2)
    filename = row["name"].replace(" ", "_") + ".json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/projects/import", status_code=201)
async def import_project(body: ProfileImport):
    return await asyncio.to_thread(state.db.import_project, body.name, body.description, body.data)
