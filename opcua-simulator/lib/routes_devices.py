"""Device + tag CRUD routes — split out of opcua_simulator.py."""
import asyncio
import json
import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import lib.state as state
from lib.db import VALID_DATA_TYPES, is_effectively_enabled

router = APIRouter()


class DeviceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    manufacturer: str = ""
    model: str = ""
    enabled: bool = True
    folder_id: Optional[int] = None


class DeviceUpdate(DeviceCreate):
    pass


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    data_type: str = "Double"
    writable: bool = False
    unit: str = ""
    behavior: str = "constant"
    behavior_params: str = '{"value":0}'
    enabled: bool = True

    def validate_choices(self) -> None:
        from lib.behaviors import VALID_BEHAVIORS
        if self.data_type not in VALID_DATA_TYPES:
            raise HTTPException(400, f"data_type must be one of {sorted(VALID_DATA_TYPES)}")
        if self.behavior not in VALID_BEHAVIORS:
            raise HTTPException(400, f"behavior must be one of {sorted(VALID_BEHAVIORS)}")
        try:
            json.loads(self.behavior_params)
        except Exception:
            raise HTTPException(400, "behavior_params must be valid JSON")


class TagUpdate(TagCreate):
    pass


class SetValueRequest(BaseModel):
    value: Any


# ── Devices ──

@router.get("/devices")
async def list_devices():
    return await asyncio.to_thread(state.db.get_devices)


@router.post("/devices", status_code=201)
async def create_device(body: DeviceCreate):
    if body.folder_id is not None and not await asyncio.to_thread(state.db.get_folder, body.folder_id):
        raise HTTPException(404, "Folder not found")
    device = await asyncio.to_thread(
        state.db.create_device, body.name, body.description, body.manufacturer, body.model,
        body.enabled, body.folder_id,
    )
    state._device_names[device["id"]] = device["name"]
    state.log_event(device["id"], "info", f"Device created: {device['name']}")
    folders_by_id = {f["id"]: f for f in await asyncio.to_thread(state.db.get_folders)}
    if is_effectively_enabled(device, folders_by_id):
        state.track(state.engine.add_device_live(device), f"add_device_live({device['id']})")
    return device


@router.get("/devices/{device_id}")
async def get_device(device_id: int):
    d = await asyncio.to_thread(state.db.get_device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")
    return d


@router.put("/devices/{device_id}")
async def update_device(device_id: int, body: DeviceUpdate):
    d = await asyncio.to_thread(state.db.get_device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")
    if body.folder_id is not None and not await asyncio.to_thread(state.db.get_folder, body.folder_id):
        raise HTTPException(404, "Folder not found")
    updated = await asyncio.to_thread(
        state.db.update_device, device_id, body.name, body.description, body.manufacturer, body.model,
        body.enabled, body.folder_id,
    )
    state._device_names[device_id] = body.name
    if d["enabled"] != body.enabled:
        state.log_event(device_id, "info", f"Device {'enabled' if body.enabled else 'disabled'}")
    elif d.get("folder_id") != body.folder_id:
        state.log_event(device_id, "info", "Device moved to a different folder")
    elif d["name"] != body.name:
        state.log_event(device_id, "info", f"Device renamed to '{body.name}'")
    else:
        state.log_event(device_id, "info", "Device configuration updated")
    state.track(state.engine.update_device_live(device_id, updated), f"update_device_live({device_id})")
    return updated


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(device_id: int):
    deleted = await asyncio.to_thread(state.db.delete_device, device_id)
    if not deleted:
        raise HTTPException(404, "Device not found")
    state.track(state.engine.delete_device_live(device_id), f"delete_device_live({device_id})")


# ── Tags ──

@router.get("/devices/{device_id}/tags")
async def list_tags(device_id: int):
    d = await asyncio.to_thread(state.db.get_device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")
    return await asyncio.to_thread(state.db.get_tags, device_id)


@router.post("/devices/{device_id}/tags", status_code=201)
async def create_tag(device_id: int, body: TagCreate):
    body.validate_choices()
    d = await asyncio.to_thread(state.db.get_device, device_id)
    if not d:
        raise HTTPException(404, "Device not found")
    try:
        tag = await asyncio.to_thread(
            state.db.create_tag, device_id, body.name, body.data_type, body.writable,
            body.unit, body.behavior, body.behavior_params, body.enabled,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"Tag '{body.name}' already exists on this device")
    state.log_event(device_id, "info", f"Tag added: {body.name} ({body.data_type})")
    folders_by_id = {f["id"]: f for f in await asyncio.to_thread(state.db.get_folders)}
    if is_effectively_enabled(d, folders_by_id) and body.enabled:
        state.track(state.engine.add_tag_live(device_id, tag), f"add_tag_live({device_id}, {tag['id']})")
    return tag


@router.get("/devices/{device_id}/tags/{tag_id}")
async def get_tag(device_id: int, tag_id: int):
    t = await asyncio.to_thread(state.db.get_tag, device_id, tag_id)
    if not t:
        raise HTTPException(404, "Tag not found")
    return t


@router.put("/devices/{device_id}/tags/{tag_id}")
async def update_tag(device_id: int, tag_id: int, body: TagUpdate):
    body.validate_choices()
    existing = await asyncio.to_thread(state.db.get_tag, device_id, tag_id)
    if not existing:
        raise HTTPException(404, "Tag not found")
    updated = await asyncio.to_thread(
        state.db.update_tag, device_id, tag_id, body.name, body.data_type, body.writable,
        body.unit, body.behavior, body.behavior_params, body.enabled,
    )
    if existing["enabled"] != body.enabled:
        state.log_event(device_id, "info", f"Tag '{body.name}' {'enabled' if body.enabled else 'disabled'}")
    elif existing["behavior"] != body.behavior:
        state.log_event(device_id, "info", f"Tag '{body.name}' behavior changed to {body.behavior}")
    else:
        state.log_event(device_id, "info", f"Tag '{body.name}' updated")
    state.track(state.engine.update_tag_live(device_id, tag_id, updated), f"update_tag_live({device_id}, {tag_id})")
    return updated


@router.delete("/devices/{device_id}/tags/{tag_id}", status_code=204)
async def delete_tag(device_id: int, tag_id: int):
    deleted = await asyncio.to_thread(state.db.delete_tag, device_id, tag_id)
    if not deleted:
        raise HTTPException(404, "Tag not found")
    state.log_event(device_id, "warn", f"Tag removed (id {tag_id})")
    state.track(state.engine.delete_tag_live(tag_id), f"delete_tag_live({tag_id})")


@router.post("/devices/{device_id}/tags/{tag_id}/value")
async def set_tag_value(device_id: int, tag_id: int, body: SetValueRequest):
    t = await asyncio.to_thread(state.db.get_tag, device_id, tag_id)
    if not t:
        raise HTTPException(404, "Tag not found")
    await asyncio.to_thread(state.db.set_manual_value, device_id, tag_id, body.value)
    state.engine.set_manual_value(tag_id, body.value)
    state.log_event(device_id, "info", f"Manual override: '{t['name']}' = {body.value}")
    return {"ok": True}


@router.get("/devices/{device_id}/tags/{tag_id}/history")
async def tag_history(device_id: int, tag_id: int):
    return [{"ts": ts, "value": v} for ts, v in state.engine.get_history(tag_id)]


# ── Logs ──

@router.get("/devices/{device_id}/logs")
async def device_logs(device_id: int, limit: int = 100):
    entries = list(state._device_logs.get(device_id, []))
    return entries[-limit:]


@router.get("/logs")
async def all_logs(limit: int = 200):
    entries = list(state._global_log)
    return entries[-limit:]
