"""Calibration API -- glue between a completed Recording (see recordings.py) and
iot-models' calibration HTTP API (dataset upload + HEBO job lifecycle), plus the durable
workflow/audit record for each calibration.

The *compute* record -- uploaded datasets, the HEBO run, validation replay, logs, raw
result artifacts, transient job state -- lives entirely in Models Runtime, keyed by its
`job_id`. What lives HERE (in the `calibration_runs` table, migration 033) is the
reproducibility/audit summary: which recording + point mapping + semantic-resolution
provenance produced the calibration, the exact FMU/model.json revision it ran against,
whether it was validated on held-out data, and whether/where it was applied. Models
Runtime never learns any of that -- it only ever sees opaque dataset ids.

Flat paths, not nested under a device or recording -- the Calibration screen
(admin/src/components/calibration/CalibrationView.vue) is its own entry point.
"""
from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ...simulation.calibration_export import build_calibration_dataset
from ...simulation.mapping.suggestions import MappingAlternative, suggest_mapping_for_variable
from ...simulation.models.remote_calibration import (
    cancel_calibration_job, create_calibration_job, get_calibration_job,
    get_calibration_results, upload_calibration_dataset,
)
from ...simulation.models.runtime import reload_model
from ...simulation.models.store import merge_simulation_model_parameter_overrides
# _variable is a "private" (underscore) helper in remote_catalog.py, but
# it's the exact input/output-dict -> VariableDefinition parser
# suggest_mapping_for_variable expects -- reused directly here rather than
# writing a second parser for the same model.json shape.
from ...simulation.models.remote_catalog import (
    _variable, fetch_remote_catalog, fetch_remote_metadata,
)
from ...semantics.candidates import build_resolution_candidate_from_recording_point
from ...semantics.providers import get_semantic_provider
from ...semantics.resolution import ResolutionContext, resolve as resolve_semantic

router = APIRouter(prefix="/calibration", tags=["calibration"])


def _semantic_reason(resolution) -> str:
    """A human-readable line for a Tier-1 result -- reads only the provider's neutral
    ``summary`` / ``reason`` provenance keys, never a representation-specific field."""
    prov = resolution.provenance or {}
    return prov.get("summary") or prov.get("reason") or f"Resolved by {resolution.provider}"


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


@router.get("/recordings")
async def list_calibration_recordings(request: Request, model_id: str | None = None):
    """Every completed, non-empty recording across all devices -- the calibration screen's
    own recording picker. `model_id` is accepted for backward compatibility but no longer
    narrows the list: the old equipment-type narrowing relied on
    `mapping_hints.preferred_equipment_types`, which was removed in the semantic-contract
    clean break. The user picks the right recording."""
    database = get_database(request)
    recordings = await asyncio.to_thread(database.get_recordings)
    devices = await asyncio.to_thread(database.get_devices)
    devices_by_id = {d["id"]: d for d in devices}

    return [
        {**r, "device_name": devices_by_id.get(r["source_device_id"], {}).get("name")}
        for r in recordings
        if r["status"] == "completed" and r["sample_count"] > 0
    ]


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


def _all_calibration_variables(metadata: dict[str, Any]) -> dict[str, Any]:
    """{name: VariableDefinition} for every declared input and output -- the superset the
    final mapping can draw from (a user may map an optional input, not just the required
    set _required_calibration_variables returns)."""
    variables: dict[str, Any] = {}
    for direction, key in (("input", "inputs"), ("output", "outputs")):
        for item in metadata.get(key, []):
            if item.get("name"):
                variables[item["name"]] = _variable(item, direction)
    return variables


def _resolve_semantic_bindings(database, recording, variables, *, provider=None, context=None):
    """{variable_name: SemanticResolution}, one entry per variable, resolved against the
    FULL recording-point candidate list via the single provider-neutral resolve() path --
    the same resolution /mapping-suggestions performs, so the provenance persisted with a
    job matches exactly what the user was shown (ambiguity + alternatives included, never
    collapsed by re-resolving only the chosen point)."""
    if context is None:
        context = ResolutionContext(anchor_device_id=recording["source_device_id"])
    if provider is None:
        provider = get_semantic_provider(database, context)
    resolutions = {}
    for variable in variables:
        candidates = [
            build_resolution_candidate_from_recording_point(p)
            for p in recording["points"]
            if _setpoint_consistent(variable.name, p["object_name"] or "")
        ]
        resolutions[variable.name] = resolve_semantic(
            database, provider, variable.semantic, candidates, context=context,
        )
    return resolutions


def _mapping_provenance(resolutions: dict, mapping: dict[str, int], variables_by_name: dict) -> dict:
    """Per-variable audit record for the FINAL confirmed mapping:
    ``{recording_point_id, semantic_id, selection_source, semantic_resolution}``.
    ``selection_source`` is ``semantic_match`` when the chosen point is exactly what the
    resolver identified, else ``user_override`` -- the backend can't tell an actively
    accepted auto-suggestion from an independent identical pick, so it does not claim to.
    """
    provenance: dict[str, Any] = {}
    for variable_name, point_id in mapping.items():
        variable = variables_by_name.get(variable_name)
        contract = getattr(variable, "semantic", None)
        semantic_id = getattr(contract, "id", None) or None
        res = resolutions.get(variable_name)
        if res is None:
            provenance[variable_name] = {
                "recording_point_id": point_id,
                "semantic_id": semantic_id,
                "selection_source": "user_override",
                "semantic_resolution": None,
            }
            continue
        provenance[variable_name] = {
            "recording_point_id": point_id,
            "semantic_id": semantic_id,
            "selection_source": "semantic_match" if res.candidate_id == point_id else "user_override",
            "semantic_resolution": {
                "provider": res.provider,
                "quality": res.quality,
                "alternatives": list(res.alternatives),
            },
        }
    return provenance


def _remap_points_to_recording(source_recording: dict, target_recording: dict, mapping: dict[str, int]) -> dict[str, int]:
    """Translate ``{variable: point_id_in_source}`` to the equivalent point ids in
    ``target_recording`` by matching each source recording point to a target one with the
    same live object (``source_object_id``) or, failing that, the same ``object_name``.
    Raises 422 if the target recording has no counterpart for a mapped point -- never a
    silent drop (that would validate the model against a different signal set)."""
    source_by_id = {p["id"]: p for p in source_recording["points"]}
    target_by_object = {
        p["source_object_id"]: p for p in target_recording["points"] if p["source_object_id"] is not None
    }
    target_by_name = {p["object_name"]: p for p in target_recording["points"] if p["object_name"]}

    translated: dict[str, int] = {}
    missing: list[str] = []
    for variable_name, point_id in mapping.items():
        source_point = source_by_id.get(point_id)
        target_point = None
        if source_point is not None:
            if source_point["source_object_id"] is not None:
                target_point = target_by_object.get(source_point["source_object_id"])
            if target_point is None:
                target_point = target_by_name.get(source_point["object_name"])
        if target_point is None:
            missing.append(variable_name)
        else:
            translated[variable_name] = target_point["id"]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                "The validation recording has no matching point for: "
                + ", ".join(sorted(missing))
                + ". Pick a validation recording of the same equipment."
            ),
        )
    return translated


def _now() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' -- the format SQLite's own datetime('now') produces, so
    timestamps this router writes sort alongside the DEFAULT-generated created_at."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _runtime_error_detail(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("runtime_error") or detail.get("message") or detail)
    return str(detail)


def _bool_int(value) -> int | None:
    return None if value is None else (1 if value else 0)


def _sync_run_from_terminal_job(database, run: dict, job: dict) -> None:
    """Poll-driven reconciliation (no background worker): once Models Runtime reports a
    terminal state for the compute job, fold its result into the calibration_runs row.
    Idempotent -- a run row already in a terminal state is left untouched."""
    if run.get("status") in ("completed", "failed", "cancelled"):
        return
    runtime_status = str(job.get("status") or "").upper()

    if runtime_status == "COMPLETED":
        settings = database.get_settings()
        try:
            result = get_calibration_results(settings, run["model_id"], job["job_id"])
        except Exception as exc:  # results not fetchable yet -- try again on the next poll
            database.update_calibration_run(run["id"], error_summary=f"result fetch failed: {exc}")
            return
        objective = result.get("objective") or {}
        g14 = result.get("ashrae_g14") or {}
        validation_metrics = result.get("validation_metrics")
        fields: dict[str, Any] = {
            "status": "completed",
            "completed_at": _now(),
            "error_summary": None,
            "baseline_metric": objective.get("baseline"),
            "calibration_metric": objective.get("best"),
            "improvement_pct": objective.get("improvement_pct"),
            "best_parameters_json": result.get("best_parameters") or {},
            "fmu_defaults_json": result.get("fmu_defaults") or {},
            "g14_interval": g14.get("interval"),
            "g14_based_on": g14.get("based_on"),
            "g14_applicable": _bool_int(g14.get("applicable")),
            "g14_pass": _bool_int(g14.get("pass")),
        }
        if validation_metrics:
            fields.update({
                "validation_status": "completed",
                "validation_cv_rmse": validation_metrics.get("cv_rmse"),
                "validation_nmbe": validation_metrics.get("nmbe"),
                "validation_rmse": validation_metrics.get("rmse"),
                "validation_mae": validation_metrics.get("mae"),
                "validation_r2": validation_metrics.get("r2"),
            })
        elif run.get("runtime_validation_dataset_id"):
            # A validation dataset was sent but the replay produced no metrics.
            fields["validation_status"] = "failed"
        database.update_calibration_run(run["id"], **fields)

    elif runtime_status == "FAILED":
        fields = {
            "status": "failed",
            "completed_at": _now(),
            "failure_stage": "calibrating",
            "error_summary": job.get("error"),
        }
        if run.get("runtime_validation_dataset_id"):
            fields["validation_status"] = "failed"
        database.update_calibration_run(run["id"], **fields)

    elif runtime_status == "CANCELLED":
        # Leave validation_status as-is -- "failed" would misrepresent a run cancelled
        # before validation was ever attempted.
        database.update_calibration_run(run["id"], status="cancelled", completed_at=_now())


@router.get("/mapping-suggestions")
async def get_calibration_mapping_suggestions(recording_id: int, model_id: str, request: Request):
    database = get_database(request)
    recording = await asyncio.to_thread(database.get_recording, recording_id)
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
    # (== recording_points.source_object_id) -- a DIFFERENT id space
    # than recording_points.id (recording_point_id), which is what
    # every sample row is actually keyed by (recording_samples.recording_point_id)
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

    # Semantic resolution works directly in recording_points.id space (each recording
    # point carries its own id) -- no objects.id -> recording_point_id translation.
    name_by_recording_point_id = {p["id"]: p["object_name"] for p in recording["points"]}
    resolution_context = ResolutionContext(anchor_device_id=recording["source_device_id"])
    provider = await asyncio.to_thread(get_semantic_provider, database, resolution_context)
    resolutions = await asyncio.to_thread(
        _resolve_semantic_bindings, database, recording, [v for v, _ in variables],
        provider=provider, context=resolution_context,
    )

    def _name_match(variable) -> dict | None:
        """The deterministic name/unit scorer, run as a SEPARATE, explicitly-labelled
        suggestion mechanism -- never a hidden fallback dressed up as a semantic match.
        Returned only so the UI can offer it; it never pre-fills the calibration mapping.
        """
        suggestion = suggest_mapping_for_variable(
            variable, recording["source_device_id"], database,
        )
        candidates = list(suggestion.alternatives)
        if suggestion.suggested_point_id is not None:
            candidates.insert(0, MappingAlternative(
                point_id=suggestion.suggested_point_id,
                point_name=suggestion.suggested_point_name or "",
                score=suggestion.score, reasons=suggestion.reasons,
            ))
        eligible = [
            c for c in candidates
            if c.point_id in allowed_object_ids and _setpoint_consistent(variable.name, c.point_name)
        ]
        if not eligible:
            return None
        best = eligible[0]
        return {
            "mechanism": "name",
            "point_id": recording_point_id_by_object_id.get(best.point_id),
            "point_name": best.point_name,
            "confidence": suggestion.confidence if best.point_id == suggestion.suggested_point_id else "low",
            "reasons": best.reasons,
        }

    results = []
    for variable, required in variables:
        # Provider-neutral semantic resolution against THIS recording (precomputed, same
        # call POST /jobs persists provenance from). The consumer never learns which
        # representation (Brick, Haystack, ...) answered -- only candidate_id + quality.
        res = resolutions[variable.name]

        if res.quality in ("exact", "class_only"):
            suggested_point_id = res.candidate_id
            confidence = "high" if res.quality == "exact" else "low"
            reasons = [_semantic_reason(res)]
            alternatives: list[dict] = []
        elif res.quality == "ambiguous":
            suggested_point_id = None
            confidence = "none"
            reasons = [_semantic_reason(res)]
            alternatives = [
                {
                    "point_id": cid,
                    "point_name": name_by_recording_point_id.get(cid, ""),
                    "score": 0.0,
                    "reasons": ["Matches this semantic identity"],
                }
                for cid in res.alternatives
            ]
        else:  # unresolved -- no automatic semantic binding; user maps manually.
            suggested_point_id = None
            confidence = "none"
            reasons = []
            alternatives = []

        # The name-based mechanism is exposed separately and ONLY when semantics did not
        # bind -- it is never merged into `suggested_point_id`.
        name_match = None
        if suggested_point_id is None:
            name_match = await asyncio.to_thread(_name_match, variable)

        results.append({
            "name": variable.name,
            "direction": variable.direction,
            "unit": variable.unit,
            "required": required,
            "suggested_point_id": suggested_point_id,
            "confidence": confidence,
            "reasons": reasons,
            "alternatives": alternatives,
            "resolution": {"provider": res.provider, "quality": res.quality},
            "name_match": name_match,
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
    # Optional held-out validation recording (must differ from `recording_id`). When set,
    # the best HEBO parameters are replayed once against it; the run records a real
    # validation verdict instead of "not validated".
    validation_recording_id: int | None = None
    # Optional link to the simulation_model_configs row this calibration is for -- lets
    # the history view and Apply target a specific configured model instance.
    simulation_model_id: int | None = None


@router.post("/jobs", status_code=201)
async def create_calibration_job_route(body: CreateCalibrationJobRequest, request: Request):
    database = get_database(request)
    recording = await asyncio.to_thread(database.get_recording, body.recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    if not body.mapping:
        raise HTTPException(status_code=400, detail="At least one point mapping is required")

    validation_recording = None
    if body.validation_recording_id is not None:
        if body.validation_recording_id == body.recording_id:
            raise HTTPException(
                status_code=400,
                detail="The validation recording must be different from the calibration recording.",
            )
        validation_recording = await asyncio.to_thread(
            database.get_recording, body.validation_recording_id
        )
        if validation_recording is None:
            raise HTTPException(status_code=404, detail="Validation recording not found")

    settings = await asyncio.to_thread(database.get_settings)
    metadata = await asyncio.to_thread(_relay, fetch_remote_metadata, settings, body.model_id)
    calibration_meta = metadata.get("calibration") or {}
    tuner_names = [
        t["parameter"] for t in (calibration_meta.get("tuners") or []) if t.get("parameter")
    ]
    objective_metric = (calibration_meta.get("goal") or {}).get("metric") or "cv_rmse"

    # Provenance for the FINAL confirmed mapping, from the same full-candidate resolution
    # /mapping-suggestions ran -- so the persisted record matches what the user saw
    # (ambiguity preserved, not collapsed).
    all_variables = _all_calibration_variables(metadata)
    mapped_variables = [v for name, v in all_variables.items() if name in body.mapping]
    resolutions = await asyncio.to_thread(
        _resolve_semantic_bindings, database, recording, mapped_variables,
    )
    mapping_provenance = _mapping_provenance(resolutions, body.mapping, all_variables)

    run = await asyncio.to_thread(
        database.create_calibration_run,
        model_id=body.model_id,
        simulation_model_id=body.simulation_model_id,
        recording_id=body.recording_id,
        validation_recording_id=body.validation_recording_id,
        fmu_sha256=metadata.get("fmu_sha256"),
        metadata_sha256=metadata.get("metadata_sha256"),
        tuner_names=tuner_names,
        objective_metric=objective_metric,
        mapping_json=mapping_provenance,
        status="pending",
        validation_status="pending" if body.validation_recording_id is not None else "not_requested",
    )
    run_id = run["id"]

    def _fail(stage: str, exc: HTTPException, *, validation_failed: bool = False):
        extra = {"validation_status": "failed"} if validation_failed else {}
        database.update_calibration_run(
            run_id, status="failed", failure_stage=stage,
            error_summary=_runtime_error_detail(exc), **extra,
        )

    try:
        csv_bytes = await asyncio.to_thread(
            build_calibration_dataset, database, body.recording_id, body.mapping,
        )
        dataset = await asyncio.to_thread(
            _relay, upload_calibration_dataset, settings, body.model_id,
            f"recording-{body.recording_id}.csv", io.BytesIO(csv_bytes),
        )
    except HTTPException as exc:
        await asyncio.to_thread(_fail, "dataset_upload", exc)
        raise

    validation_dataset_id = None
    if validation_recording is not None:
        try:
            validation_mapping = _remap_points_to_recording(
                recording, validation_recording, body.mapping
            )
            v_csv = await asyncio.to_thread(
                build_calibration_dataset, database, body.validation_recording_id, validation_mapping,
            )
            v_dataset = await asyncio.to_thread(
                _relay, upload_calibration_dataset, settings, body.model_id,
                f"recording-{body.validation_recording_id}.csv", io.BytesIO(v_csv),
            )
            validation_dataset_id = v_dataset["dataset_id"]
        except HTTPException as exc:
            await asyncio.to_thread(_fail, "validation_dataset_upload", exc, validation_failed=True)
            raise

    try:
        job = await asyncio.to_thread(
            _relay, create_calibration_job, settings, body.model_id, dataset["dataset_id"],
            validation_dataset_id=validation_dataset_id, experiment_id=str(run_id),
        )
    except HTTPException as exc:
        await asyncio.to_thread(_fail, "job_create", exc)
        raise

    await asyncio.to_thread(
        database.update_calibration_run,
        run_id,
        status="queued",
        runtime_job_id=job.get("job_id"),
        runtime_dataset_id=dataset["dataset_id"],
        runtime_validation_dataset_id=validation_dataset_id,
        started_at=_now(),
    )
    return {**job, "calibration_run_id": run_id}


@router.get("/runs")
async def list_calibration_runs_route(
    request: Request,
    model_id: str | None = None,
    simulation_model_id: int | None = None,
    recording_id: int | None = None,
):
    database = get_database(request)
    return await asyncio.to_thread(
        database.list_calibration_runs,
        model_id=model_id,
        simulation_model_id=simulation_model_id,
        recording_id=recording_id,
    )


@router.get("/runs/{run_id}")
async def get_calibration_run_route(run_id: int, request: Request):
    database = get_database(request)
    run = await asyncio.to_thread(database.get_calibration_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Calibration run not found")
    return run


class ApplyCalibrationRunRequest(BaseModel):
    simulation_model_id: int


@router.post("/runs/{run_id}/apply")
async def apply_calibration_run_route(
    run_id: int, body: ApplyCalibrationRunRequest, request: Request
):
    """Push a completed calibration's best parameters onto a configured simulation model
    (merge into parameters.parameter_overrides) and reload it, then record the
    applied-to/applied-at on the run.

    Deliberately minimal for this pass: it requires only that the run is `completed` and
    has parameters. It does NOT yet check the tuner names still exist, that the values are
    finite, or that the model revision matches what was calibrated -- that hardening is a
    separate follow-up. `improvement_pct` is never a gate here.
    """
    database = get_database(request)
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Simulation engine is unavailable")

    run = await asyncio.to_thread(database.get_calibration_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Calibration run not found")
    if run["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Calibration run is {run['status']!r}, not completed -- nothing to apply.",
        )
    best_parameters = run.get("best_parameters_json") or {}
    if not best_parameters:
        raise HTTPException(status_code=409, detail="Calibration run has no best parameters to apply.")

    config = await asyncio.to_thread(
        merge_simulation_model_parameter_overrides,
        database, body.simulation_model_id, best_parameters,
    )
    if config is None:
        raise HTTPException(status_code=404, detail="Simulation model not found")

    # Reuse the existing reload path -- pushes the merged config to the FMU runtime
    # (restarts the session if the model is enabled; no-op registration if disabled).
    # Raises on a real registration failure, which surfaces as a 500 -- the applied-state
    # below is only recorded once the reload has actually gone through.
    await asyncio.to_thread(reload_model, database, engine, body.simulation_model_id, action="restarted")

    updated_run = await asyncio.to_thread(
        database.update_calibration_run,
        run_id,
        applied_at=_now(),
        applied_to_simulation_model_id=body.simulation_model_id,
    )
    return {"calibration_run": updated_run, "simulation_model": config}


@router.get("/jobs/{job_id}")
async def get_calibration_job_route(job_id: str, model_id: str, request: Request):
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    job = await asyncio.to_thread(_relay, get_calibration_job, settings, model_id, job_id)

    # Poll-driven: fold a terminal compute job's outcome into its calibration_runs row.
    run = await asyncio.to_thread(database.get_calibration_run_by_runtime_job, job_id)
    if run is not None:
        await asyncio.to_thread(_sync_run_from_terminal_job, database, run, job)
        job = {**job, "calibration_run_id": run["id"]}
    return job


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
