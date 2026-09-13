"""Recording API -- device-wide, SQLite-backed recording of an external
BACnet device's values (see src/db/database.py's "Recordings" section),
later played back through a cloned simulated device (see
replay_playback.py, split out from this file in migration 29's rename --
"replay" is legacy naming for what's now used by Training/Calibration/
general trend inspection at least as much as actual replay). Same router
shape as trend_logs.py; unlike trend logs, sampling is driven by a
dedicated background loop (src/simulation/runtime.py's recording_loop),
not schedule_engine_reload -- creating/stopping a recording doesn't need
the SimEngine to reload anything.
"""
from __future__ import annotations

import asyncio
import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import RecordingCreate, RecordingUpdate
from ...simulation.models.store import get_active_simulation_models_by_device, get_simulation_model
from .exports import export_filename

router = APIRouter(tags=["recordings"])


def _constant_sourced_inputs(database: Any, device_id: int) -> dict[str, float]:
    """variable_name -> value, for every input this device's own active
    FMU-, AI-, or Classifier-backed simulation model currently sources as a
    constant -- read straight from simulation_model_configs.parameters, the
    exact same input_sources/input_defaults the simulation itself feeds
    into every /step() call (see migration 024's own docstring for why this
    gets snapshotted onto the recording rather than re-read live at
    training time). AI and Classifier models both share the same
    Constant/Point/Aggregate input sourcing FMU already had (see
    simulation.py's own _normalized_parameters), so this applies
    identically to all three now. A device with no active model, or whose
    active model isn't fmu/ai/classifier (e.g. builtin) at all, returns {}
    -- nothing to snapshot, not an error."""
    active_models = get_active_simulation_models_by_device(database)
    model = active_models.get(device_id)
    if model is None:
        return {}
    config = get_simulation_model(database, int(model["id"]))
    if config is None or config.get("provider_type") not in ("fmu", "ai", "classifier"):
        return {}
    parameters = config.get("parameters") or {}
    input_sources: dict[str, str] = parameters.get("input_sources") or {}
    input_defaults: dict[str, float] = parameters.get("input_defaults") or {}
    return {
        variable: input_defaults[variable]
        for variable, source in input_sources.items()
        if source == "constant" and variable in input_defaults
    }


def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return database


@router.get("/devices/{device_id}/recordings")
async def list_recordings(device_id: int, request: Request):
    database = get_database(request)

    device = await asyncio.to_thread(database.get_device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    return await asyncio.to_thread(database.get_recordings, device_id)


@router.post("/devices/{device_id}/recordings", status_code=201)
async def create_recording(device_id: int, body: RecordingCreate, request: Request):
    database = get_database(request)

    device = await asyncio.to_thread(database.get_device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    # Available for every device type -- external BACnet devices are sampled
    # over the wire (see _recording_sample_once's external-bacnet branch),
    # while simulated/mirror/replay devices are sampled directly from their
    # own already-live in-process values (that function's else branch). No
    # source_type restriction here.

    if body.buffer_mode not in ("overwrite", "stop"):
        raise HTTPException(status_code=400, detail='buffer_mode must be "overwrite" or "stop"')

    if body.point_ids is not None:
        if not body.point_ids:
            raise HTTPException(status_code=400, detail="point_ids must be a non-empty list of object ids")
        if device.get("source_type") == "external-bacnet":
            # Sampled over the wire from this device's own external_host/
            # device_instance connection (see _recording_sample_once) -- a
            # point belonging to a different device can't be read that way,
            # so this source type stays restricted to its own objects.
            objects = await asyncio.to_thread(database.get_objects, device_id)
            valid_ids = {o["id"] for o in objects}
            if any(pid not in valid_ids for pid in body.point_ids):
                raise HTTPException(
                    status_code=400,
                    detail="point_ids must be object ids on this device (external BACnet devices are sampled over their own connection)",
                )
        else:
            # Simulated/mirror/replay-source devices sample via
            # SimEngine.get_object_value(source_object_id) -- a plain global
            # lookup with no device scoping (see _sample_due_simulated_
            # recordings in runtime.py) -- a point on a DIFFERENT device
            # works exactly the same as one on this device. This is what
            # lets a "Model points" recording capture a model's upstream
            # inputs (e.g. an AHU's discharge-air-temp feeding a zone model)
            # alongside its own outputs, confirmed live this validation was
            # the only thing blocking it.
            for pid in body.point_ids:
                if await asyncio.to_thread(database.get_object, pid) is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"point_ids must reference existing objects (unknown id {pid})",
                    )

    # Starts sampling immediately -- there is no separate idle/draft state
    # (see Database.create_recording's own docstring).
    recording = await asyncio.to_thread(database.create_recording, device_id, body.model_dump())

    # Snapshot this device's constant-sourced inputs onto the recording now
    # -- while "now" still means what it says -- not re-derived from the
    # device's (possibly since-changed) live config whenever training later
    # runs. See _constant_sourced_inputs and migration 024's own docstrings.
    constants = await asyncio.to_thread(_constant_sourced_inputs, database, device_id)
    if constants:
        await asyncio.to_thread(database.add_recording_input_constants, recording["id"], constants)
        recording = await asyncio.to_thread(database.get_recording, recording["id"])

    return recording


@router.get("/recordings/{recording_id}")
async def get_recording(recording_id: int, request: Request):
    database = get_database(request)

    recording = await asyncio.to_thread(database.get_recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    return recording


@router.get("/recordings/{recording_id}/samples")
async def export_recording_samples(recording_id: int, request: Request):
    """Raw recorded samples as a downloadable CSV, pivoted wide (one row per
    sample_index, one column per point) -- e.g. checking that a point with a
    randomized Behavior (noise/sine/random_walk) genuinely varied
    sample-to-sample rather than being recorded at a single stale value.
    Not used by the calibration flow (which reads
    Database.get_recording_all_samples directly and builds its own
    narrower, mapping-driven CSV -- see calibration_export.py); this is a
    plain, full, human-facing export of everything a recording holds."""
    database = get_database(request)

    recording = await asyncio.to_thread(database.get_recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    rows = await asyncio.to_thread(database.get_recording_all_samples, recording_id)

    by_index: dict[int, dict] = {}
    for row in rows:
        entry = by_index.setdefault(row["sample_index"], {"timestamp": row["timestamp"], "values": {}})
        entry["values"][row["recording_point_id"]] = row["value"]

    points = recording["points"]
    # The recording's own ground-truth labels, repeated on every row (same convention
    # training_export.py's build_classification_dataset already uses for its `label` column) --
    # without this, a human downloading/archiving/sharing this CSV has no way to tell what
    # condition/fault it was recorded under; the label lives only in the app's own DB otherwise.
    # Blank (not omitted) for an unset field, same "never guess, show what's actually there"
    # precedent as everywhere else condition/fault_type/tags are displayed.
    condition = recording.get("condition") or ""
    fault_type = recording.get("fault_type") or ""
    tags = "; ".join(recording.get("tags") or [])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "timestamp", "Condition", "Fault Type", "Tags",
        *(f'{p["object_name"]} ({p["units"] or "no-units"})' for p in points),
    ])
    for sample_index in sorted(by_index):
        entry = by_index[sample_index]
        writer.writerow([
            entry["timestamp"], condition, fault_type, tags,
            *(entry["values"].get(p["id"], "") for p in points),
        ])

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{export_filename(recording["name"], "csv")}"'},
    )


@router.put("/recordings/{recording_id}")
async def update_recording(recording_id: int, body: RecordingUpdate, request: Request):
    database = get_database(request)

    existing = await asyncio.to_thread(database.get_recording, recording_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    if body.buffer_mode not in ("overwrite", "stop"):
        raise HTTPException(status_code=400, detail='buffer_mode must be "overwrite" or "stop"')

    return await asyncio.to_thread(database.update_recording, recording_id, body.model_dump())


@router.post("/recordings/{recording_id}/stop")
async def stop_recording(recording_id: int, request: Request):
    database = get_database(request)

    existing = await asyncio.to_thread(database.get_recording, recording_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    return await asyncio.to_thread(database.stop_recording, recording_id)


@router.delete("/recordings/{recording_id}", status_code=204)
async def delete_recording(recording_id: int, request: Request) -> Response:
    database = get_database(request)

    deleted = await asyncio.to_thread(database.delete_recording, recording_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Recording not found")

    return Response(status_code=204)
