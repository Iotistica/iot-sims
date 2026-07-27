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


@router.get("/profiles")
async def list_profiles():
    return await asyncio.to_thread(state.db.get_profiles)


@router.post("/profiles", status_code=201)
async def save_profile(body: ProfileCreate):
    return await asyncio.to_thread(state.db.save_profile, body.name, body.description)


@router.put("/profiles/{profile_id}")
async def update_profile(profile_id: int, body: ProfileUpdate):
    ok = await asyncio.to_thread(state.db.update_profile, profile_id, body.name, body.description)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"ok": True}


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: int):
    deleted = await asyncio.to_thread(state.db.delete_profile, profile_id)
    if not deleted:
        raise HTTPException(404, "Profile not found")


@router.post("/profiles/{profile_id}/load")
async def load_profile(profile_id: int):
    row = await asyncio.to_thread(state.db.get_profile, profile_id)
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

    # A freshly loaded profile starts paused at t=0 — press Start when ready.
    state.engine.reset()
    return {"ok": True}


@router.get("/profiles/{profile_id}/export")
async def export_profile(profile_id: int):
    row = await asyncio.to_thread(state.db.get_profile, profile_id)
    if not row:
        raise HTTPException(404, "Profile not found")
    content = json.dumps(json.loads(row["data"]), indent=2)
    filename = row["name"].replace(" ", "_") + ".json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/profiles/import", status_code=201)
async def import_profile(body: ProfileImport):
    return await asyncio.to_thread(state.db.import_profile, body.name, body.description, body.data)
