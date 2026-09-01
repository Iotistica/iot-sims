"""Calibration API -- thin glue between a completed Recording (see
replay_recordings.py) and iot-models' existing calibration HTTP API
(dataset upload + HEBO job lifecycle). This router owns no persistence of
its own: job state lives entirely in iot-models; the only "storage" here
is picking which recording/model/mapping to send.

Flat paths, not nested under a device or recording -- the Calibration
screen (admin/src/components/calibration/CalibrationView.vue) is its own
entry point, not reached through a specific device or recording.
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...simulation.calibration_export import build_calibration_dataset
from ...simulation.mapping.suggestions import MappingAlternative, suggest_mapping_for_variable
from ...simulation.models.remote_calibration import (
    cancel_calibration_job, create_calibration_job, get_calibration_job,
    get_calibration_results, upload_calibration_dataset,
)
# _variable is a "private" (underscore) helper in remote_catalog.py, but
# it's the exact input/output-dict -> VariableDefinition parser
# suggest_mapping_for_variable expects -- reused directly here rather than
# writing a second parser for the same model.json shape.
from ...simulation.models.remote_catalog import (
    _variable, fetch_remote_catalog, fetch_remote_metadata,
)
from ...core.config import EQUIPMENT_TYPES

router = APIRouter(prefix="/calibration", tags=["calibration"])


def _is_setpoint_name(name: str) -> bool:
    return "setpoint" in name.lower().replace("_", "").replace("-", "").replace(" ", "")


def _setpoint_consistent(variable_name: str, candidate_name: str) -> bool:
    """suggest_mapping_for_variable's name/token scoring treats "setpoint"
    as just another token to overlap on, so a variable and a candidate that
    share every other word but disagree on "is this a setpoint or the
    actual value" (e.g. variable supply_air_temp_setpoint_c vs. point
    "Supply-Air-Temp", or variable supply_air_temp_c vs. point
    "Supply-Air-Temp-Setpoint") can still out-score the correct match --
    confirmed live, the two ended up suggested backwards from each other.
    A setpoint and an actual value are opposite in meaning, not merely
    "similar names", so this is checked as a hard consistency requirement
    here rather than a scoring nudge inside the shared engine (which stays
    unmodified -- this filter is calibration-specific)."""
    return _is_setpoint_name(variable_name) == _is_setpoint_name(candidate_name)


def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return database


def _relay(fn, *args, **kwargs):
    """Runs a remote_calibration.py/remote_catalog.py call and translates
    its RuntimeError (network/HTTP-layer failure) into a 502, matching
    src/api/routers/simulation.py's existing /simulation/resources* relay
    routes exactly."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": "FMU model runtime request failed", "runtime_error": str(exc)},
        ) from exc


@router.get("/models")
async def list_calibration_models(request: Request):
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    return await asyncio.to_thread(_relay, fetch_remote_catalog, settings)


def _preferred_equipment_types(metadata: dict[str, Any]) -> set[str]:
    """Union of mapping_hints.preferred_equipment_types across every input
    and output the model declares (not just the calibration goal/inputs) --
    a broad, best-effort "what kind of equipment is this model for" hint,
    already authored in model.json for the Auto Map feature; reused as-is
    for recording-list filtering rather than a second classification."""
    types: set[str] = set()
    for item in (*metadata.get("inputs", []), *metadata.get("outputs", [])):
        hints = item.get("mapping_hints") or {}
        types.update(hints.get("preferred_equipment_types") or ())
    return types


def _equipment_type_matches(device_equipment_type: str | None, preferred: set[str]) -> bool:
    """device.equipment_type is stored as EQUIPMENT_TYPES' raw key (e.g.
    "Variable_Air_Volume_Box"); model.json's preferred_equipment_types uses
    a mix of the same raw Brick class names (e.g. "Rooftop_Unit") and
    common abbreviations (e.g. "VAV") -- EQUIPMENT_TYPES' own label side
    already carries that abbreviation ("Variable_Air_Volume_Box" -> "VAV"),
    so check both forms rather than requiring an exact key match."""
    if not device_equipment_type:
        return False
    if device_equipment_type in preferred:
        return True
    return EQUIPMENT_TYPES.get(device_equipment_type) in preferred


@router.get("/recordings")
async def list_calibration_recordings(request: Request, model_id: str | None = None):
    """Completed, non-empty recordings across every device -- the
    calibration screen's own recording picker, unscoped to any one device.
    When model_id is given, recordings are narrowed to devices whose
    equipment_type plausibly matches that model (see _equipment_type_matches)
    -- purely a convenience narrowing: if it would leave nothing (no device
    tagged that way yet, or the model declares no equipment hints at all),
    every completed recording is returned instead of a dead-end empty list."""
    database = get_database(request)
    recordings = await asyncio.to_thread(database.get_replay_recordings)
    devices = await asyncio.to_thread(database.get_devices)
    devices_by_id = {d["id"]: d for d in devices}

    completed = [
        {**r, "device_name": devices_by_id.get(r["source_device_id"], {}).get("name")}
        for r in recordings
        if r["status"] == "completed" and r["sample_count"] > 0
    ]

    if not model_id:
        return completed

    settings = await asyncio.to_thread(database.get_settings)
    try:
        metadata = await asyncio.to_thread(_relay, fetch_remote_metadata, settings, model_id)
    except HTTPException:
        return completed  # relay failed -- fall back to unfiltered rather than erroring the whole list

    preferred = _preferred_equipment_types(metadata)
    if not preferred:
        return completed

    filtered = [
        r for r in completed
        if _equipment_type_matches(devices_by_id.get(r["source_device_id"], {}).get("equipment_type"), preferred)
    ]
    return filtered or completed


def _required_calibration_variables(metadata: dict[str, Any]) -> list[tuple[Any, bool]]:
    """(VariableDefinition, required) pairs: every declared input (required
    = it has no `default`, i.e. iot-models' own prepare_inputs() can't
    silently omit it), plus the one calibration goal output (always
    required). Raises ValueError if the model has no usable calibration
    block."""
    calibration = metadata.get("calibration") or {}
    goal_name = (calibration.get("goal") or {}).get("output")
    if not calibration.get("enabled") or not goal_name:
        raise ValueError("This model has no calibration configuration")

    goal_item = next((o for o in metadata.get("outputs", []) if o.get("name") == goal_name), None)
    if goal_item is None:
        raise ValueError(f"Calibration goal output {goal_name!r} not found in this model's outputs")

    variables = [
        (_variable(item, "input"), item.get("default") is None)
        for item in metadata.get("inputs", [])
        if item.get("name")
    ]
    variables.append((_variable(goal_item, "output"), True))
    return variables


@router.get("/mapping-suggestions")
async def get_calibration_mapping_suggestions(recording_id: int, model_id: str, request: Request):
    database = get_database(request)
    recording = await asyncio.to_thread(database.get_replay_recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    settings = await asyncio.to_thread(database.get_settings)
    metadata = await asyncio.to_thread(_relay, fetch_remote_metadata, settings, model_id)

    try:
        variables = _required_calibration_variables(metadata)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # suggest_mapping_for_variable's candidates are scored against the live
    # `objects` table, so its point_id values are objects.id
    # (== replay_recording_points.source_object_id) -- a DIFFERENT id space
    # than replay_recording_points.id (recording_point_id), which is what
    # every sample row is actually keyed by (replay_samples.recording_point_id)
    # and therefore what build_calibration_dataset's `mapping` must use.
    # Everything below scores/filters in objects.id space (matching the
    # engine's own candidates) but translates to recording_point_id before
    # it leaves this route -- a recording point whose source object was
    # since deleted (source_object_id is NULL) simply can't be
    # auto-suggested, but still appears in "points" for manual mapping,
    # since its recorded samples are still perfectly usable.
    allowed_object_ids = {
        p["source_object_id"] for p in recording["points"] if p["source_object_id"] is not None
    }
    recording_point_id_by_object_id = {
        p["source_object_id"]: p["id"] for p in recording["points"] if p["source_object_id"] is not None
    }

    results = []
    for variable, required in variables:
        suggestion = await asyncio.to_thread(
            suggest_mapping_for_variable, variable, recording["source_device_id"], database,
        )
        # One score-ordered candidate list: the engine's own top pick (if
        # any) followed by its alternatives -- filtered down to (a) points
        # actually part of this recording and (b) points that agree with
        # this variable on setpoint-vs-actual-value (see
        # _setpoint_consistent's own docstring for why that's checked as a
        # hard requirement, not left to the shared scoring). The scoring
        # itself is reused unmodified; only candidate eligibility is
        # narrowed here. If nothing survives, this can legitimately report
        # no suggestion even though the recording holds a usable point --
        # accepted for this pass, the user can still pick manually.
        candidates = list(suggestion.alternatives)
        if suggestion.suggested_point_id is not None:
            candidates = [
                MappingAlternative(
                    point_id=suggestion.suggested_point_id,
                    point_name=suggestion.suggested_point_name or "",
                    score=suggestion.score,
                    reasons=suggestion.reasons,
                ),
                *candidates,
            ]
        eligible = [
            c for c in candidates
            if c.point_id in allowed_object_ids and _setpoint_consistent(variable.name, c.point_name)
        ]

        if eligible:
            suggested_object_id = eligible[0].point_id
            confidence = suggestion.confidence if eligible[0].point_id == suggestion.suggested_point_id else "low"
            alternatives = eligible[1:6]
        else:
            suggested_object_id = None
            confidence = "none"
            alternatives = []

        results.append({
            "name": variable.name,
            "direction": variable.direction,
            "unit": variable.unit,
            "required": required,
            "suggested_point_id": recording_point_id_by_object_id.get(suggested_object_id),
            "confidence": confidence,
            "reasons": suggestion.reasons,
            "alternatives": [
                {**dataclasses.asdict(a), "point_id": recording_point_id_by_object_id[a.point_id]}
                for a in alternatives
            ],
        })

    return {
        "variables": results,
        "points": [
            {
                "id": p["id"],
                "name": p["object_name"],
                "units": p["units"],
                "point_type": p["point_type"],
                "object_type": p["object_type"],
            }
            for p in recording["points"]
        ],
    }


class CreateCalibrationJobRequest(BaseModel):
    recording_id: int
    model_id: str
    mapping: dict[str, int]


@router.post("/jobs", status_code=201)
async def create_calibration_job_route(body: CreateCalibrationJobRequest, request: Request):
    database = get_database(request)
    recording = await asyncio.to_thread(database.get_replay_recording, body.recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    if not body.mapping:
        raise HTTPException(status_code=400, detail="At least one point mapping is required")

    settings = await asyncio.to_thread(database.get_settings)
    csv_bytes = await asyncio.to_thread(
        build_calibration_dataset, database, body.recording_id, body.mapping,
    )
    dataset = await asyncio.to_thread(
        _relay, upload_calibration_dataset, settings, body.model_id,
        f"recording-{body.recording_id}.csv", io.BytesIO(csv_bytes),
    )
    job = await asyncio.to_thread(
        _relay, create_calibration_job, settings, body.model_id, dataset["dataset_id"],
    )
    return job


@router.get("/jobs/{job_id}")
async def get_calibration_job_route(job_id: str, model_id: str, request: Request):
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    return await asyncio.to_thread(_relay, get_calibration_job, settings, model_id, job_id)


@router.get("/jobs/{job_id}/results")
async def get_calibration_job_results_route(job_id: str, model_id: str, request: Request):
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    return await asyncio.to_thread(_relay, get_calibration_results, settings, model_id, job_id)


@router.post("/jobs/{job_id}/cancel")
async def cancel_calibration_job_route(job_id: str, model_id: str, request: Request):
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    return await asyncio.to_thread(_relay, cancel_calibration_job, settings, model_id, job_id)
