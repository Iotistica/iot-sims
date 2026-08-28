"""Replay Recording API -- device-wide, SQLite-backed recording of an
external BACnet device's values (see src/db/database.py's "Replay
Recordings" section), later played back through a cloned simulated device.
Same router shape as trend_logs.py; unlike trend logs, sampling is driven by
a dedicated background loop (src/simulation/runtime.py's
replay_recording_loop), not schedule_engine_reload -- creating/stopping a
recording doesn't need the SimEngine to reload anything.
"""
from __future__ import annotations

import asyncio
import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from pydantic import BaseModel, Field

from ...bacnet.schemas import ReplayRecordingCreate, ReplayRecordingUpdate
from .exports import export_filename

router = APIRouter(tags=["replay-recordings"])


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
    if device.get("simulation_mode") != "replay" or device.get("replay_recording_id") is None:
        raise HTTPException(status_code=400, detail="Not a Replay device")
    return device


@router.get("/devices/{device_id}/replay-recordings")
async def list_replay_recordings(device_id: int, request: Request):
    database = get_database(request)

    device = await asyncio.to_thread(database.get_device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    return await asyncio.to_thread(database.get_replay_recordings, device_id)


@router.post("/devices/{device_id}/replay-recordings", status_code=201)
async def create_replay_recording(device_id: int, body: ReplayRecordingCreate, request: Request):
    database = get_database(request)

    device = await asyncio.to_thread(database.get_device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    # Available for every device type -- external BACnet devices are sampled
    # over the wire (see _replay_recording_sample_once's external-bacnet
    # branch), while simulated/mirror/replay devices are sampled directly
    # from their own already-live in-process values (that function's else
    # branch). No source_type restriction here.

    if body.buffer_mode not in ("overwrite", "stop"):
        raise HTTPException(status_code=400, detail='buffer_mode must be "overwrite" or "stop"')

    if body.point_ids is not None:
        objects = await asyncio.to_thread(database.get_objects, device_id)
        valid_ids = {o["id"] for o in objects}
        if not body.point_ids or any(pid not in valid_ids for pid in body.point_ids):
            raise HTTPException(
                status_code=400,
                detail="point_ids must be a non-empty list of object ids on this device",
            )

    # Starts sampling immediately -- there is no separate idle/draft state
    # (see Database.create_replay_recording's own docstring).
    return await asyncio.to_thread(database.create_replay_recording, device_id, body.model_dump())


@router.get("/replay-recordings/{recording_id}")
async def get_replay_recording(recording_id: int, request: Request):
    database = get_database(request)

    recording = await asyncio.to_thread(database.get_replay_recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Replay recording not found")

    return recording


@router.get("/replay-recordings/{recording_id}/samples")
async def export_replay_recording_samples(recording_id: int, request: Request):
    """Raw recorded samples as a downloadable CSV, pivoted wide (one row per
    sample_index, one column per point) -- e.g. checking that a point with a
    randomized Behavior (noise/sine/random_walk) genuinely varied
    sample-to-sample rather than being recorded at a single stale value.
    Not used by the calibration flow (which reads
    Database.get_replay_recording_all_samples directly and builds its own
    narrower, mapping-driven CSV -- see calibration_export.py); this is a
    plain, full, human-facing export of everything a recording holds."""
    database = get_database(request)

    recording = await asyncio.to_thread(database.get_replay_recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Replay recording not found")

    rows = await asyncio.to_thread(database.get_replay_recording_all_samples, recording_id)

    by_index: dict[int, dict] = {}
    for row in rows:
        entry = by_index.setdefault(row["sample_index"], {"timestamp": row["timestamp"], "values": {}})
        entry["values"][row["recording_point_id"]] = row["value"]

    points = recording["points"]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp", *(f'{p["object_name"]} ({p["units"] or "no-units"})' for p in points)])
    for sample_index in sorted(by_index):
        entry = by_index[sample_index]
        writer.writerow([entry["timestamp"], *(entry["values"].get(p["id"], "") for p in points)])

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{export_filename(recording["name"], "csv")}"'},
    )


@router.put("/replay-recordings/{recording_id}")
async def update_replay_recording(recording_id: int, body: ReplayRecordingUpdate, request: Request):
    database = get_database(request)

    existing = await asyncio.to_thread(database.get_replay_recording, recording_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Replay recording not found")
    if body.buffer_mode not in ("overwrite", "stop"):
        raise HTTPException(status_code=400, detail='buffer_mode must be "overwrite" or "stop"')

    return await asyncio.to_thread(database.update_replay_recording, recording_id, body.model_dump())


@router.post("/replay-recordings/{recording_id}/stop")
async def stop_replay_recording(recording_id: int, request: Request):
    database = get_database(request)

    existing = await asyncio.to_thread(database.get_replay_recording, recording_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Replay recording not found")

    return await asyncio.to_thread(database.stop_replay_recording, recording_id)


@router.delete("/replay-recordings/{recording_id}", status_code=204)
async def delete_replay_recording(recording_id: int, request: Request) -> Response:
    database = get_database(request)

    deleted = await asyncio.to_thread(database.delete_replay_recording, recording_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Replay recording not found")

    return Response(status_code=204)


# ─── Replay playback transport ──────────────────────────────────────────────
# Thin wrappers over SimEngine's in-memory replay_* methods (see
# simulation/engine.py) -- playback position/state is never persisted, so
# these just mutate that in-memory state and return it back.

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
        database.get_replay_recording_sample_index_bounds, device["replay_recording_id"]
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
