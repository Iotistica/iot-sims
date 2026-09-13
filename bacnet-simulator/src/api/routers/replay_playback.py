"""Replay playback transport -- thin wrappers over SimEngine's in-memory
replay_* methods (see simulation/engine.py) -- playback position/state is
never persisted, so these just mutate that in-memory state and return it
back. Split out of recordings.py in migration 29's rename: this file is
genuinely about replaying a recording back to a device (a Replay-mode
device, devices.simulation_mode='replay'/active_recording_id), so it kept
its "replay" naming; recordings.py (the recording/capture side) did not --
see that file's own module docstring.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["replay-playback"])


def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return database


def get_engine(request: Request) -> Any:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Simulation engine is unavailable")
    return engine


async def require_replay_device(database: Any, device_id: int) -> dict:
    device = await asyncio.to_thread(database.get_device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.get("simulation_mode") != "replay" or device.get("active_recording_id") is None:
        raise HTTPException(status_code=400, detail="Not a Replay device")
    return device


class ReplaySeekBody(BaseModel):
    sample_index: int = Field(..., ge=0)


class ReplayLoopBody(BaseModel):
    loop: bool


class ReplaySpeedBody(BaseModel):
    speed: float = Field(..., gt=0)


_VALID_SPEEDS = {0.5, 1.0, 2.0, 5.0, 10.0}


@router.get("/devices/{device_id}/replay/state")
async def get_replay_playback_state(device_id: int, request: Request):
    database = get_database(request)
    engine = get_engine(request)
    await require_replay_device(database, device_id)
    return engine.get_replay_state(device_id)


@router.post("/devices/{device_id}/replay/play")
async def play_replay(device_id: int, request: Request):
    database = get_database(request)
    engine = get_engine(request)
    await require_replay_device(database, device_id)
    return engine.replay_play(device_id)


@router.post("/devices/{device_id}/replay/pause")
async def pause_replay(device_id: int, request: Request):
    database = get_database(request)
    engine = get_engine(request)
    await require_replay_device(database, device_id)
    return engine.replay_pause(device_id)


@router.post("/devices/{device_id}/replay/stop")
async def stop_replay(device_id: int, request: Request):
    database = get_database(request)
    engine = get_engine(request)
    await require_replay_device(database, device_id)
    return await engine.replay_stop(device_id)


@router.post("/devices/{device_id}/replay/seek")
async def seek_replay(device_id: int, body: ReplaySeekBody, request: Request):
    database = get_database(request)
    engine = get_engine(request)
    device = await require_replay_device(database, device_id)
    bounds = await asyncio.to_thread(
        database.get_recording_sample_index_bounds, device["active_recording_id"]
    )
    if bounds is None:
        raise HTTPException(status_code=409, detail="Recording has no samples")
    min_index, max_index = bounds
    if not (min_index <= body.sample_index <= max_index):
        raise HTTPException(status_code=400, detail=f"sample_index must be between {min_index} and {max_index}")
    return engine.replay_seek(device_id, body.sample_index)


@router.post("/devices/{device_id}/replay/loop")
async def set_replay_loop(device_id: int, body: ReplayLoopBody, request: Request):
    database = get_database(request)
    engine = get_engine(request)
    await require_replay_device(database, device_id)
    return engine.replay_set_loop(device_id, body.loop)


@router.post("/devices/{device_id}/replay/speed")
async def set_replay_speed(device_id: int, body: ReplaySpeedBody, request: Request):
    database = get_database(request)
    engine = get_engine(request)
    await require_replay_device(database, device_id)
    if body.speed not in _VALID_SPEEDS:
        raise HTTPException(status_code=400, detail=f"speed must be one of {sorted(_VALID_SPEEDS)}")
    return engine.replay_set_speed(device_id, body.speed)
