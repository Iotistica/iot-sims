"""Relay for the FMU runtime's training-job API (shared/runtime/training/,
shared/runtime/datasets/ in iot-models) -- dataset upload + training job
lifecycle. Same shape as remote_calibration.py (sibling relay for the
calibration side of the same runtime); reuses that module's own
requests-based _post_json/_get_json helpers directly rather than
duplicating them -- matches this codebase's existing precedent of reusing
"private" (underscore) helpers across modules when they're the exact right
tool (see calibration.py's own comment on reusing remote_catalog.py's
_variable for the identical reason).
"""
from __future__ import annotations

from typing import Any, BinaryIO

from .remote_calibration import _get_json, _post_json
from .remote_catalog import get_runtime_settings


def upload_training_dataset(
    settings: dict[str, Any],
    model_id: str,
    filename: str,
    file_obj: BinaryIO,
) -> dict[str, Any]:
    """POST /models/{model_id}/datasets -- the exact same generic dataset
    store calibration's own upload_calibration_dataset already uses;
    returns a DatasetResponse dict (dataset_id, model_id, filename,
    size_bytes, created_at, status, columns)."""
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    return _post_json(
        f"{base_url}/models/{model_id}/datasets", timeout_s, api_key,
        files={"file": (filename, file_obj)},
    )


def create_training_job(
    settings: dict[str, Any],
    model_id: str,
    dataset_id: str,
    *,
    history_window: int = 1,
    test_fraction: float = 0.2,
    seed: int = 42,
    task_type: str = "regression",
    classifier_target: str | None = None,
    semantic_features: list[dict] | None = None,
    equipment_class: str | None = None,
    sample_interval_seconds: float | None = None,
) -> dict[str, Any]:
    """POST /models/{model_id}/training-jobs -- returns a TrainingJobResponse
    dict (job_id, model_id, dataset_id, status, created_at, started_at,
    completed_at, error).

    task_type/classifier_target/semantic_features/equipment_class/
    sample_interval_seconds are additive fields (see iot-models'
    CreateTrainingRequest). The semantic/unit/cadence block is sent whenever it is
    present -- regression and classification alike; a plain pre-feature regression
    request (task_type "regression" with nothing resolved) still gets the exact same
    minimal body it always has."""
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    body: dict[str, Any] = {
        "dataset_id": dataset_id,
        "history_window": history_window,
        "test_fraction": test_fraction,
        "seed": seed,
    }
    if task_type != "regression":
        body["task_type"] = task_type
        body["classifier_target"] = classifier_target
    # Only sent when actually resolved -- an empty contract keeps the minimal body a
    # plain pre-feature regression request always had.
    if semantic_features:
        body["semantic_features"] = semantic_features
    if equipment_class:
        body["equipment_class"] = equipment_class
    if sample_interval_seconds is not None:
        body["sample_interval_seconds"] = sample_interval_seconds
    return _post_json(f"{base_url}/models/{model_id}/training-jobs", timeout_s, api_key, json=body)


def list_training_jobs(settings: dict[str, Any], model_id: str) -> dict[str, Any]:
    """GET /models/{model_id}/training-jobs -- {"jobs": [TrainingJobResponse,
    ...]}, every job ever run for this model, newest and oldest alike --
    iot-models persists each one to job.json on disk and reloads them on its
    own restart (see TrainingManager._load_persisted_jobs), so this survives
    both sides restarting, not just this process's own uptime."""
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    return _get_json(f"{base_url}/models/{model_id}/training-jobs", timeout_s, api_key)


def get_training_job(settings: dict[str, Any], model_id: str, job_id: str) -> dict[str, Any]:
    """GET /models/{model_id}/training-jobs/{job_id} -- same shape as
    create_training_job's response."""
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    return _get_json(f"{base_url}/models/{model_id}/training-jobs/{job_id}", timeout_s, api_key)


def get_training_job_results(settings: dict[str, Any], model_id: str, job_id: str) -> dict[str, Any]:
    """GET /models/{model_id}/training-jobs/{job_id}/results -- the raw
    train_baseline.py output JSON plus job_id/model_id/dataset_id/
    wall_clock_seconds merged in by iot-models' own TrainingRunner."""
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    return _get_json(f"{base_url}/models/{model_id}/training-jobs/{job_id}/results", timeout_s, api_key)


def cancel_training_job(settings: dict[str, Any], model_id: str, job_id: str) -> dict[str, Any]:
    """POST /models/{model_id}/training-jobs/{job_id}/cancel -- terminates
    the train_baseline.py subprocess; returns the updated
    TrainingJobResponse dict."""
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    return _post_json(f"{base_url}/models/{model_id}/training-jobs/{job_id}/cancel", timeout_s, api_key)


def promote_training_job(
    settings: dict[str, Any],
    model_id: str,
    job_id: str,
    *,
    slug: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """POST /models/{model_id}/training-jobs/{job_id}/promote -- turns a
    completed job's model.onnx into the model's own live, active artifact.
    First training from an FMU model_id mints a new AI model id (the FMU
    catalog entry is the shipped reference model, never overwritten);
    promoting again with model_id = that same AI model's own id updates it
    IN PLACE instead (same id, no new catalog entry) -- see iot-models' own
    route docstring for the full rule. Returns the resulting model's
    ModelMetadata.summary() dict (id, slug, label, runtime_type, onnx_path,
    onnx_exists, ...) either way; the caller can tell which case happened
    by comparing the returned id to the model_id it started from."""
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    body: dict[str, Any] = {}
    if slug:
        body["slug"] = slug
    if label:
        body["label"] = label
    return _post_json(
        f"{base_url}/models/{model_id}/training-jobs/{job_id}/promote", timeout_s, api_key, json=body,
    )
