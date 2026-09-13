"""Training-export API -- combines one or more completed Replay Recordings
into a single ML-training dataset for iot-models' training-job API (dataset
upload + training job lifecycle), the training-side sibling of
calibration.py (dataset upload + HEBO calibration job lifecycle). Same
"this router owns no persistence of its own" shape: job state lives
entirely in iot-models.

Recording listing has its own dedicated route here (GET /training/
recordings), deliberately stricter than GET /calibration/recordings --
confirmed live that combining recordings from devices running genuinely
different models (e.g. a VAV-2 Controller recording + an RTU Controller
recording) produces near-zero point overlap, flagging almost every
variable as missing from one side or the other. Calibration's own
equipment-type filter falls back to showing every recording when
narrowing would leave nothing; that fallback is exactly what let an
unrelated recording through here, so this route does not repeat it.
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from ...semantics import ml_units

from ...simulation.models.remote_catalog import fetch_remote_metadata, get_remote_model_definition
from ...simulation.models.remote_training import (
    cancel_training_job, create_training_job, get_training_job,
    get_training_job_results, list_training_jobs, promote_training_job, upload_training_dataset,
)
from ...simulation.models.store import get_active_simulation_models_by_device, get_simulation_model
from ...simulation.training_export import (
    build_classification_dataset, build_training_dataset, recording_class_distribution,
)
from ...semantics.feature_contract import (
    build_semantic_feature_contract, resolve_point_semantic_identity,
    resolve_semantic_contract_against_device,
)

router = APIRouter(prefix="/training", tags=["training"])


def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return database


def _relay(fn, *args, **kwargs):
    """Same shape as calibration.py's own _relay -- translates a remote_*.py
    RuntimeError (network/HTTP-layer failure) into a 502."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": "FMU model runtime request failed", "runtime_error": str(exc)},
        ) from exc


def _semantic_block(variable) -> dict:
    """The canonical domain-meaning block ({id, role, ...}) for a model variable, as a
    plain dict for the sidecar. `{}` when the model.json declared no `semantic`."""
    sc = getattr(variable, "semantic", None)
    if sc is None:
        return {}
    if dataclasses.is_dataclass(sc):
        return {k: v for k, v in dataclasses.asdict(sc).items() if v not in (None, "")}
    return dict(sc) if isinstance(sc, dict) else {}


async def _resolve_ml_semantic_contract(
    database, *, body, model_definition, recordings, direction_by_name, constant_mapping,
    training_device_id_out: list,
) -> tuple[list[dict], str | None, dict[str, str | None]]:
    """Shared by the classification and regression branches: resolves every mapped INPUT
    variable's semantic identity (Brick first, explicit body.feature_choices for the rest,
    ambiguity/undecided -> 400), enriches each spec with the model's own `semantic` block
    + `expected_unit` (the catalog unit), appends device_specific entries for
    constant-sourced inputs, and derives `equipment_class` from the single training
    device. Returns (semantic_features, equipment_class, expected_units_by_bare_name)."""
    input_object_ids = {
        variable_name: object_id
        for variable_name, object_id in body.mapping.items()
        if direction_by_name.get(variable_name) == "input"
    }
    device_ids = {
        (await asyncio.to_thread(database.get_object, object_id) or {}).get("device_id")
        for object_id in input_object_ids.values()
    }
    device_ids.discard(None)
    if len(device_ids) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "This mapping's points span more than one device -- the model's feature "
                "contract needs a single, well-defined training device."
            ),
        )
    training_device_id = next(iter(device_ids), None)
    training_device_id_out.append(training_device_id)

    contract = await asyncio.to_thread(
        build_semantic_feature_contract, database, input_object_ids, body.feature_choices,
    )
    if contract.undecided:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(contract.undecided)} recorded point(s) are not mapped to Brick "
                f"semantics: {', '.join(contract.undecided)}. Map these points in the "
                "Semantic Model panel first, or explicitly choose a custom or "
                "device-specific identity for each one."
            ),
        )
    if contract.ambiguous_brick_pairs:
        pairs = ", ".join(f"{a}/{b}" for a, b in contract.ambiguous_brick_pairs)
        raise HTTPException(
            status_code=400,
            detail=f"These variables resolve to the same Brick point role, which is ambiguous: {pairs}.",
        )
    if contract.duplicate_custom_label_pairs:
        pairs = ", ".join(f"{a}/{b}" for a, b in contract.duplicate_custom_label_pairs)
        raise HTTPException(
            status_code=400,
            detail=f"These variables were given the same custom label, which is ambiguous: {pairs}.",
        )

    variable_by_name = {v.name: v for v in model_definition.variables}
    expected_units: dict[str, str | None] = {}
    semantic_features: list[dict] = []
    for variable_name, spec in contract.specs.items():
        variable = variable_by_name.get(variable_name)
        expected_unit = getattr(variable, "unit", None)
        expected_units[variable_name] = expected_unit
        semantic_features.append({
            **spec,
            "variable_name": variable_name,
            "semantic": _semantic_block(variable),
            "expected_unit": expected_unit,
        })
    for variable in model_definition.variables:
        if f"input_{variable.name}" in constant_mapping:
            expected_units[variable.name] = variable.unit
            semantic_features.append({
                "feature_name": variable.name,
                "variable_name": variable.name,
                "identity_kind": "device_specific",
                "source_device_id": training_device_id,
                "source_object_id": None,
                "bacnet_object_type": None,
                "bacnet_object_instance": None,
                "object_name": None,
                "unit": variable.unit,
                "semantic": _semantic_block(variable),
                "expected_unit": variable.unit,
                "constant_sourced": True,
            })

    equipment_class = None
    if training_device_id is not None:
        training_device = await asyncio.to_thread(database.get_device, training_device_id)
        equipment_class = (training_device or {}).get("equipment_type")
    return semantic_features, equipment_class, expected_units


async def _build_csv(fn, *args):
    """Run a CSV builder, translating an engineering-unit contract failure into a clear
    400 (rather than a 500 from the raw ValueError)."""
    try:
        return await asyncio.to_thread(fn, *args)
    except ml_units.UnitError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "A selected recorded point's engineering unit is not compatible with what "
                f"the model expects: {exc}"
            ),
        ) from exc


def _shared_sample_interval(recordings: list[dict]) -> float | None:
    """The single sampling cadence across the selected recordings, or None when they
    disagree / it is unknown -- never invented (see the ML contract's history block)."""
    intervals = {
        float(r["sample_interval_seconds"])
        for r in recordings
        if r.get("sample_interval_seconds") is not None
    }
    return next(iter(intervals)) if len(intervals) == 1 else None


def _parse_recording_ids(raw: str) -> list[int]:
    try:
        ids = [int(part) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="recording_ids must be a comma-separated list of integers",
        ) from exc
    if not ids:
        raise HTTPException(status_code=400, detail="At least one recording_id is required")
    return ids


@router.get("/recordings")
async def list_training_recordings(model_id: str, device_id: int, request: Request):
    """Completed, non-empty recordings eligible for combining into a
    training dataset for this model -- ONLY from devices currently running
    this exact model_type (via simulation_model_configs), falling back to
    just the calling device's own recordings (never every recording in the
    system) if no other device shares this model. See this module's own
    docstring for why calibration's own broader-fallback filter isn't
    reused as-is here."""
    database = get_database(request)
    recordings = await asyncio.to_thread(database.get_recordings)
    devices = await asyncio.to_thread(database.get_devices)
    devices_by_id = {d["id"]: d for d in devices}
    active_models = await asyncio.to_thread(get_active_simulation_models_by_device, database)

    completed = [
        {**r, "device_name": devices_by_id.get(r["source_device_id"], {}).get("name")}
        for r in recordings
        if r["status"] == "completed" and r["sample_count"] > 0
    ]

    matching_device_ids = {
        did for did, model in active_models.items()
        if str(model.get("model_type")) == model_id
    }
    same_model = [r for r in completed if r["source_device_id"] in matching_device_ids]
    if same_model:
        return same_model

    return [r for r in completed if r["source_device_id"] == device_id]


def _authoritative_mapping(database: Any, device_id: int, model_id: str) -> dict[str, int]:
    """variable_name -> source_object_id (objects.id), read directly from
    the calling device's own active simulation_model_configs row -- this is
    the exact data already driving live simulation output writes and input
    reads for this device, so it's known, not guessed (confirmed live this
    session: guessing here, via the scoring engine, produced garbage
    suggestions when two unrelated recordings were combined -- this sidesteps
    that entirely for a device actually running this model).

    Aggregate mappings (weighted-average/min/max across several member
    points) are excluded: no single point's raw recorded value equals the
    model's own computed aggregate value, so those variables fall back to
    the scoring engine below, unchanged from today's behavior for them
    specifically -- see this function's own callers."""
    active_models = get_active_simulation_models_by_device(database)
    model = active_models.get(device_id)
    if model is None or str(model.get("model_type")) != model_id:
        return {}
    config = get_simulation_model(database, int(model["id"]))
    if config is None:
        return {}
    result: dict[str, int] = {}
    for row in config["mappings"]:
        if "point_ids" in row:
            continue  # aggregate mapping -- no single source point, see docstring
        result[row["variable"]] = row["point_id"]
    for row in config["input_exposures"]:
        result[row["variable"]] = row["point_id"]
    return result


@router.get("/mapping-suggestions")
async def get_training_mapping_suggestions(recording_ids: str, model_id: str, device_id: int, request: Request):
    """Deliberately NOT a guessing tool: a variable is either mapped on the
    calling device's own active Simulation Model config (see
    _authoritative_mapping) or it isn't -- there is no scoring-engine
    fallback here (unlike calibration's own mapping-suggestions route, which
    still guesses). Simulation -> Model is already the one place a device's
    inputs/outputs get wired to points; re-guessing that here duplicated it
    badly in practice -- confirmed live, an unmapped variable would get
    handed a plausible-looking but wrong same-model-owned point as its
    "suggestion" (e.g. an output point already claimed by one of this exact
    model's OTHER variables), simply because nothing here knew that point
    was already spoken for. The fix is to not guess at all: an unmapped
    variable is just excluded from the training dataset, with the fix being
    "go map it in Simulation -> Model", not a picker inside Training.

    Coverage ("missing_in_recordings") is still checked per mapped variable
    against every given recording independently -- this is what lets the UI
    warn *before* submission that a recording lacking a mapped variable's
    point will silently contribute zero rows (see build_training_dataset's
    own docstring for why a recording missing a mapped point contributes no
    rows rather than a partial one)."""
    database = get_database(request)
    ids = _parse_recording_ids(recording_ids)
    authoritative = await asyncio.to_thread(_authoritative_mapping, database, device_id, model_id)

    recordings = []
    for recording_id in ids:
        recording = await asyncio.to_thread(database.get_recording, recording_id)
        if recording is None:
            raise HTTPException(status_code=404, detail=f"Recording {recording_id} not found")
        recordings.append(recording)

    settings = await asyncio.to_thread(database.get_settings)
    model_definition = await asyncio.to_thread(_relay, get_remote_model_definition, settings, model_id)

    # source_object_id -> recording ids that actually captured a point for it.
    recording_ids_by_object_id: dict[int, list[int]] = {}
    for recording in recordings:
        for point in recording["points"]:
            if point["source_object_id"] is None:
                continue
            recording_ids_by_object_id.setdefault(point["source_object_id"], []).append(recording["id"])

    def _missing_in(object_id: int) -> list[int]:
        captured_by = set(recording_ids_by_object_id.get(object_id, ()))
        return [r["id"] for r in recordings if r["id"] not in captured_by]

    results = []
    for variable in model_definition.variables:
        object_id = authoritative.get(variable.name)
        point = await asyncio.to_thread(database.get_object, object_id) if object_id is not None else None

        mapped = object_id is not None
        source: str | None = "point" if mapped else None
        missing_in_recordings: list[int] = _missing_in(object_id) if mapped else []
        constant_values: dict[int, float] = {}

        if not mapped and variable.direction == "input":
            # No live point -- fall back to each recording's own
            # snapshotted constant value (migration 024 /
            # recordings.py's _constant_sourced_inputs), never to
            # the device's CURRENT config: a recording made before that
            # snapshot existed has nothing here, and that has to surface as
            # "this recording can't supply this variable", not be silently
            # backfilled from today's live parameters.
            constant_missing: list[int] = []
            for recording in recordings:
                value = next(
                    (c["value"] for c in recording["input_constants"] if c["variable"] == variable.name),
                    None,
                )
                if value is None:
                    constant_missing.append(recording["id"])
                else:
                    constant_values[recording["id"]] = value
            if constant_values:
                mapped = True
                source = "constant"
                missing_in_recordings = constant_missing

        # Auto Brick-resolution attempt only -- no choices applied here (this route is read-only,
        # called before the user has decided anything for an unresolved point). Only meaningful
        # for a point-sourced variable; a constant-sourced one has no object_id to resolve at all.
        semantic: dict[str, Any] | None = None
        if mapped and source == "point" and object_id is not None:
            resolved = await asyncio.to_thread(resolve_point_semantic_identity, database, object_id)
            semantic = (
                {"resolved": True, "point_class": resolved.point_class, "equipment_path": list(resolved.equipment_path)}
                if resolved is not None
                else {"resolved": False, "point_class": None, "equipment_path": None}
            )

        results.append({
            "name": variable.name,
            "direction": variable.direction,
            "unit": variable.unit,
            "required": variable.required,
            "default": variable.default,
            "mapped": mapped,
            "source": source,
            "object_id": object_id,
            "point_name": point["name"] if point else None,
            "point_units": point["units"] if point else None,
            "constant_values": constant_values or None,
            "missing_in_recordings": missing_in_recordings,
            "semantic": semantic,
        })

    return {"variables": results, "recording_ids": ids}


class CreateTrainingExportJobRequest(BaseModel):
    model_id: str
    recording_ids: list[int]
    mapping: dict[str, int] = Field(..., description="variable_name -> source_object_id (objects.id)")
    history_window: int = 1
    test_fraction: float = 0.2
    seed: int = 42
    # "regression" (default, unchanged behavior) trains the continuous surrogate model via
    # build_training_dataset. "classification" trains a condition/fault_type classifier via
    # build_classification_dataset instead -- requires classifier_target.
    task_type: Literal["regression", "classification"] = "regression"
    classifier_target: Optional[Literal["condition", "fault_type"]] = None
    # Explicit user decisions for input variables Brick couldn't auto-resolve (see
    # src/semantics/feature_contract.py) -- {variable_name: {"kind": "custom", "custom_label":
    # "..."} | {"kind": "device_specific"}}. Ignored for a variable Brick DID resolve (a real
    # classification always wins over a supplied choice). Classification-only; unused for
    # regression jobs.
    feature_choices: dict[str, dict] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _classifier_target_required_for_classification(self) -> "CreateTrainingExportJobRequest":
        if self.task_type == "classification" and not self.classifier_target:
            raise ValueError("classifier_target is required when task_type is 'classification'")
        return self


@router.post("/jobs", status_code=201)
async def create_training_export_job(body: CreateTrainingExportJobRequest, request: Request):
    database = get_database(request)
    if len(body.recording_ids) < 2:
        raise HTTPException(
            status_code=400,
            detail=(
                "At least 2 recordings are required -- iot-models' training pipeline needs multiple "
                "held-out trajectories to validate honestly (see shared/train_baseline.py's own "
                "train/test split-by-trajectory check)."
            ),
        )
    if not body.mapping:
        raise HTTPException(status_code=400, detail="At least one variable mapping is required")

    recordings = []
    for recording_id in body.recording_ids:
        recording = await asyncio.to_thread(database.get_recording, recording_id)
        if recording is None:
            raise HTTPException(status_code=404, detail=f"Recording {recording_id} not found")
        recordings.append(recording)

    settings = await asyncio.to_thread(database.get_settings)
    model_definition = await asyncio.to_thread(_relay, get_remote_model_definition, settings, body.model_id)

    # iot-models' training CSV column convention requires input_/output_
    # prefixes (see shared/train_baseline.py's build_supervised_pairs and
    # the dataset-generation job's own fieldnames) -- resolved from the
    # model's own catalog metadata here, not trusted from the caller, so a
    # stale/mistaken client can't silently mislabel a variable's direction.
    direction_by_name = {v.name: v.direction for v in model_definition.variables}
    prefixed_mapping: dict[str, int] = {}
    for variable_name, source_object_id in body.mapping.items():
        direction = direction_by_name.get(variable_name)
        if direction is None:
            raise HTTPException(status_code=400, detail=f"Unknown model variable: {variable_name!r}")
        if body.task_type == "classification" and direction != "input":
            # A classifier's features come entirely from input_* points -- there's no output_*
            # column to learn against, unlike regression. Reject rather than silently drop, so a
            # stale/mistaken mapping fails clearly here instead of quietly training on fewer
            # features than the caller thought it selected.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{variable_name!r} is an output variable -- a fault detection model can only "
                    "use input (recorded point) variables as features."
                ),
            )
        prefixed_mapping[f"{direction}_{variable_name}"] = source_object_id

    # Constant-sourced inputs (see migration 024 / recordings.py's
    # _constant_sourced_inputs): a variable with no live point at all still
    # counts as usable if at least one selected recording snapshotted a
    # value for it at record time -- re-derived here from the recordings
    # themselves, not trusted from the client, same "known, not guessed"
    # rule _authoritative_mapping already applies to points. Only
    # considered for a variable the caller didn't already point-map (a
    # variable can't be both).
    constant_mapping: dict[str, dict[int, float]] = {}
    for variable in model_definition.variables:
        if variable.direction != "input" or variable.name in body.mapping:
            continue
        per_recording: dict[int, float] = {}
        for recording in recordings:
            value = next(
                (c["value"] for c in recording["input_constants"] if c["variable"] == variable.name),
                None,
            )
            if value is not None:
                per_recording[recording["id"]] = value
        if per_recording:
            constant_mapping[f"input_{variable.name}"] = per_recording

    input_count = sum(1 for name in prefixed_mapping if name.startswith("input_")) + len(constant_mapping)

    if body.task_type == "classification":
        if input_count == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "This mapping has no usable input variables -- a fault detection model needs "
                    "at least one recorded point to learn from."
                ),
            )
        # Pre-training validation mirrors iot-models' own train_classifier.py validate_dataset()
        # exactly (>=2 distinct classes, >=2 total recordings) -- checked here too so a caller
        # gets a clear 400 before spending a CSV export + upload + job on a dataset iot-models
        # will just reject minutes later.
        dist = await asyncio.to_thread(
            recording_class_distribution, database, body.recording_ids, body.classifier_target,
        )
        if len(dist) < 2:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Need at least 2 distinct {body.classifier_target} values among the selected "
                    f"recordings to train a fault detection model, found {len(dist)}: {sorted(dist)}. "
                    "Select recordings covering more than one condition/fault, or label more "
                    "recordings first."
                ),
            )
        total_trajectories = sum(d["trajectories"] for d in dist.values())
        if total_trajectories < 2:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Only {total_trajectories} of the selected recordings have a "
                    f"{body.classifier_target} label set -- at least 2 labeled recordings are "
                    "required."
                ),
            )

        # Semantic + unit feature contract (shared with the regression branch). Blocks on a
        # MISSING DECISION, never on missing Brick coverage by itself.
        _device_holder: list = []
        semantic_features, equipment_class, expected_units = await _resolve_ml_semantic_contract(
            database, body=body, model_definition=model_definition, recordings=recordings,
            direction_by_name=direction_by_name, constant_mapping=constant_mapping,
            training_device_id_out=_device_holder,
        )

        csv_bytes = await _build_csv(
            build_classification_dataset, database, body.recording_ids, prefixed_mapping,
            body.classifier_target, constant_mapping, expected_units,
        )
    else:
        # Mirrors shared/train_baseline.py's own build_supervised_pairs check up front.
        output_count = sum(1 for name in prefixed_mapping if name.startswith("output_"))
        if input_count == 0 or output_count == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This mapping has {input_count} input variable(s) and {output_count} output "
                    "variable(s) -- training needs at least one of each to learn a relationship. "
                    "Check the selected recordings actually captured this device's input points, "
                    "not just its outputs."
                ),
            )
        # The regression runtime (OnnxSessionManager) only ever consumes a single
        # hist1_output_* lag -- a model trained with history_window > 1 would KeyError at
        # /step. Reject up front rather than produce an unusable model.
        if body.history_window != 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Regression surrogates support only history_window=1 (a single prior "
                    "output step). Multi-step output history is not available yet."
                ),
            )
        # Regression now gets the same semantic + unit input contract classification does.
        _device_holder = []
        semantic_features, equipment_class, expected_units = await _resolve_ml_semantic_contract(
            database, body=body, model_definition=model_definition, recordings=recordings,
            direction_by_name=direction_by_name, constant_mapping=constant_mapping,
            training_device_id_out=_device_holder,
        )
        csv_bytes = await _build_csv(
            build_training_dataset, database, body.recording_ids, prefixed_mapping,
            constant_mapping, expected_units,
        )

    filename = f"training-export-{'-'.join(str(r) for r in body.recording_ids)}.csv"
    dataset = await asyncio.to_thread(
        _relay, upload_training_dataset, settings, body.model_id, filename, io.BytesIO(csv_bytes),
    )
    # The semantic/unit/cadence contract is forwarded for BOTH task types now -- but only
    # the parts that were actually resolved, so a plain regression job with nothing to
    # resolve still makes the exact minimal call it always did. classifier_target stays
    # classification-only.
    extra_kwargs: dict[str, Any] = {}
    if semantic_features:
        extra_kwargs["semantic_features"] = semantic_features
    if equipment_class:
        extra_kwargs["equipment_class"] = equipment_class
    sample_interval = _shared_sample_interval(recordings)
    if sample_interval is not None:
        extra_kwargs["sample_interval_seconds"] = sample_interval
    if body.task_type == "classification":
        extra_kwargs["task_type"] = body.task_type
        extra_kwargs["classifier_target"] = body.classifier_target
    job = await asyncio.to_thread(
        _relay, create_training_job, settings, body.model_id, dataset["dataset_id"],
        history_window=body.history_window, test_fraction=body.test_fraction, seed=body.seed,
        **extra_kwargs,
    )
    return job


@router.get("/jobs")
async def list_training_export_jobs(model_id: str, request: Request):
    """Every training job ever run for this model, newest first -- purely
    informational history (see remote_training.py's own list_training_jobs
    docstring: iot-models persists these to disk and reloads them across its
    own restarts too, so this isn't scoped to "since this process started")."""
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    result = await asyncio.to_thread(_relay, list_training_jobs, settings, model_id)
    jobs = result.get("jobs", [])
    return sorted(jobs, key=lambda j: j.get("created_at") or 0, reverse=True)


@router.get("/jobs/{job_id}")
async def get_training_export_job(job_id: str, model_id: str, request: Request):
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    return await asyncio.to_thread(_relay, get_training_job, settings, model_id, job_id)


@router.get("/jobs/{job_id}/results")
async def get_training_export_job_results(job_id: str, model_id: str, request: Request):
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    return await asyncio.to_thread(_relay, get_training_job_results, settings, model_id, job_id)


@router.post("/jobs/{job_id}/cancel")
async def cancel_training_export_job(job_id: str, model_id: str, request: Request):
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    return await asyncio.to_thread(_relay, cancel_training_job, settings, model_id, job_id)


class PromoteTrainingExportJobRequest(BaseModel):
    slug: str | None = None
    label: str | None = None


@router.post("/jobs/{job_id}/promote", status_code=201)
async def promote_training_export_job(
    job_id: str, model_id: str, body: PromoteTrainingExportJobRequest, request: Request,
):
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    return await asyncio.to_thread(
        _relay, promote_training_job, settings, model_id, job_id, slug=body.slug, label=body.label,
    )


@router.get("/models/{model_id}/semantic-compatibility")
async def check_semantic_compatibility(model_id: str, device_id: int, request: Request):
    """Resolves a promoted classifier model's Brick-portable feature contract against a TARGET
    device -- the "Equipment Compatibility" check the attach flow (ClassifierTrainingFlow.vue's
    useThisModel()) runs before creating a simulation_model_configs row. Device-agnostic by
    design (see resolve_semantic_contract_against_device's own docstring): this phase's own UI
    only ever calls it against the device the model was already trained/attached to, but the
    exact same call already works for a genuinely different target device -- nothing here special-
    cases "same device" beyond what the resolver itself already does.

    training_device_id isn't a separate persisted field -- derived from the contract's own
    device_specific/custom entries' source_device_id (every such entry was resolved against the
    same one training device by construction); a fully brick-portable model has none of those, so
    training_device_id is simply irrelevant there (every entry resolves against any device with
    matching Brick classification)."""
    database = get_database(request)
    settings = await asyncio.to_thread(database.get_settings)
    metadata = await asyncio.to_thread(_relay, fetch_remote_metadata, settings, model_id)
    # Classifier models carry the lag-expanded `semantic_features`; regression surrogates
    # carry `ml_input_contract` (one per base input). Either shape resolves the same way.
    contract = metadata.get("ml_input_contract") or metadata.get("semantic_features") or []

    training_device_id = next(
        (f.get("source_device_id") for f in contract if f.get("source_device_id") is not None),
        None,
    )
    binding = await asyncio.to_thread(
        resolve_semantic_contract_against_device, database, device_id, contract,
        training_device_id=training_device_id,
    )

    # Unit compatibility for every successfully-bound feature. A required conversion
    # (e.g. degrees-fahrenheit -> degC) is INFORMATIONAL and does NOT block readiness --
    # only an incompatible dimension or an unknown source unit does.
    expected_unit_by_feature: dict[str, str | None] = {}
    for entry in contract:
        key = entry.get("feature_name") or entry.get("variable")
        if key is not None and key not in expected_unit_by_feature:
            expected_unit_by_feature[key] = entry.get("expected_unit")
    unit_conversions: list[dict] = []
    unit_incompatible: list[dict] = []
    unit_unknown: list[dict] = []
    for feature_name, object_id in binding.bound.items():
        expected_unit = expected_unit_by_feature.get(feature_name)
        if not expected_unit:
            continue
        obj = await asyncio.to_thread(database.get_object, object_id)
        source_unit = (obj or {}).get("units")
        verdict = ml_units.compatibility(source_unit, expected_unit)
        if verdict == "convertible":
            unit_conversions.append({
                "feature": feature_name, "source_unit": source_unit, "expected_unit": expected_unit,
            })
        elif verdict == "incompatible":
            unit_incompatible.append({
                "feature": feature_name, "source_unit": source_unit, "expected_unit": expected_unit,
            })
        elif verdict == "unknown":
            unit_unknown.append({"feature": feature_name, "expected_unit": expected_unit})

    return {
        "ok": binding.ok and not unit_incompatible and not unit_unknown,
        "portability": metadata.get("portability"),
        "bound": binding.bound,
        "missing": binding.missing,
        "ambiguous": binding.ambiguous,
        "unit_conversions": unit_conversions,
        "unit_incompatible": unit_incompatible,
        "unit_unknown": unit_unknown,
    }
