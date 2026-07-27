"""Folder CRUD routes — organizational containers for devices (Phase 1 of
the address-space hierarchy refactor). See docs/address-space-modeling.md
for what folders are and aren't in this pass."""
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import lib.state as state

router = APIRouter()


class FolderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    parent_folder_id: Optional[int] = None
    description: str = ""


class FolderUpdate(FolderCreate):
    pass


class FolderEnabledUpdate(BaseModel):
    enabled: bool


@router.get("/folders")
async def list_folders():
    return await asyncio.to_thread(state.db.get_folders)


@router.post("/folders", status_code=201)
async def create_folder(body: FolderCreate):
    if body.parent_folder_id is not None and not await asyncio.to_thread(state.db.get_folder, body.parent_folder_id):
        raise HTTPException(404, "Parent folder not found")
    folder = await asyncio.to_thread(
        state.db.create_folder, body.name, body.parent_folder_id, body.description
    )
    state.track(state.engine.add_folder_live(folder), f"add_folder_live({folder['id']})")
    return folder


@router.get("/folders/{folder_id}")
async def get_folder(folder_id: int):
    f = await asyncio.to_thread(state.db.get_folder, folder_id)
    if not f:
        raise HTTPException(404, "Folder not found")
    return f


def _is_descendant(db, candidate_id: int, of_folder_id: int) -> bool:
    """True if candidate_id is of_folder_id itself or lives anywhere under
    it — used to refuse a reparent that would create a cycle."""
    fid: Optional[int] = candidate_id
    while fid is not None:
        if fid == of_folder_id:
            return True
        folder = db.get_folder(fid)
        fid = folder["parent_folder_id"] if folder else None
    return False


@router.put("/folders/{folder_id}")
async def update_folder(folder_id: int, body: FolderUpdate):
    existing = await asyncio.to_thread(state.db.get_folder, folder_id)
    if not existing:
        raise HTTPException(404, "Folder not found")
    if body.parent_folder_id is not None:
        if not await asyncio.to_thread(state.db.get_folder, body.parent_folder_id):
            raise HTTPException(404, "Parent folder not found")
        if await asyncio.to_thread(_is_descendant, state.db, body.parent_folder_id, folder_id):
            raise HTTPException(400, "Cannot move a folder into itself or one of its own sub-folders")

    updated = await asyncio.to_thread(
        state.db.update_folder, folder_id, body.name, body.parent_folder_id, body.description
    )
    reparented = existing["parent_folder_id"] != body.parent_folder_id
    if reparented:
        state.log_event(0, "info", f"Folder '{body.name}' moved")
        # A parent change can't be applied as a live attribute edit — OPC UA
        # has no "move" primitive, and a scoped delete+recreate of just this
        # folder's own node would orphan any live devices/sub-folders
        # somewhere underneath it in NodeManager's separate registries.
        # Reusing the engine's existing full-resync path (already used for
        # NodeSet-import failure recovery) keeps everything consistent with
        # the DB at the cost of a brief full rebuild — acceptable since
        # folder moves are a deliberate, infrequent action (decision 5: no
        # drag-and-drop), not a hot path.
        state.track(state.engine.resync_live_state(), f"resync_live_state(folder move {folder_id})")
    elif existing["name"] != body.name:
        state.log_event(0, "info", f"Folder renamed to '{body.name}'")
        state.track(state.engine.rename_folder_live(folder_id, body.name), f"rename_folder_live({folder_id})")
    return updated


@router.post("/folders/{folder_id}/enabled")
async def set_folder_enabled(folder_id: int, body: FolderEnabledUpdate):
    if not await asyncio.to_thread(state.db.get_folder, folder_id):
        raise HTTPException(404, "Folder not found")
    await asyncio.to_thread(state.db.update_folder_enabled, folder_id, body.enabled)
    state.log_event(0, "info", f"Folder {'enabled' if body.enabled else 'disabled'}")
    state.track(state.engine.set_folder_enabled_live(folder_id, body.enabled), f"set_folder_enabled_live({folder_id})")
    return {"ok": True}


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(folder_id: int):
    if not await asyncio.to_thread(state.db.get_folder, folder_id):
        raise HTTPException(404, "Folder not found")
    deleted = await asyncio.to_thread(state.db.delete_folder, folder_id)
    if not deleted:
        raise HTTPException(409, "Folder is not empty — move or remove its sub-folders and devices first")
    state.track(state.engine.delete_folder_live(folder_id), f"delete_folder_live({folder_id})")
