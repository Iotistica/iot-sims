"""Relay for the FMU runtime's calibration API (shared/runtime/calibration/,
shared/runtime/datasets/ in iot-models) -- dataset upload + calibration job
lifecycle. Same requests-based relay pattern as remote_resources.py (chosen
there for the same reason: multipart upload is what requests is good at,
and it's already a project dependency); remote_catalog.py's plain-urllib
_request_json is only used for the read-only, cached /models endpoints.

"hebo" is hardcoded as the calibration method everywhere here -- it's the
only CalibrationRunner iot-models registers today (see
shared/runtime/calibration/routes.py); nothing here assumes that stays
true forever, it's just not worth a parameter for a choice of one.
"""
from __future__ import annotations

from typing import Any, BinaryIO

import requests

from .remote_catalog import get_runtime_settings


def _post_json(url: str, timeout_s: float, **kwargs: Any) -> dict[str, Any]:
    try:
        response = requests.post(url, timeout=timeout_s, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"FMU model runtime cannot be reached at {url}: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"FMU model runtime HTTP {response.status_code} for {url}: {response.text}")
    try:
        decoded = response.json()
    except ValueError as exc:
        raise RuntimeError(f"FMU model runtime returned non-JSON response for {url}: {response.text[:200]}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"FMU model runtime returned {type(decoded).__name__}, expected object")
    return decoded


def _get_json(url: str, timeout_s: float) -> dict[str, Any]:
    try:
        response = requests.get(url, timeout=timeout_s)
    except requests.RequestException as exc:
        raise RuntimeError(f"FMU model runtime cannot be reached at {url}: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"FMU model runtime HTTP {response.status_code} for {url}: {response.text}")
    try:
        decoded = response.json()
    except ValueError as exc:
        raise RuntimeError(f"FMU model runtime returned non-JSON response for {url}: {response.text[:200]}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"FMU model runtime returned {type(decoded).__name__}, expected object")
    return decoded


def upload_calibration_dataset(
    settings: dict[str, Any],
    model_id: str,
    filename: str,
    file_obj: BinaryIO,
) -> dict[str, Any]:
    """POST /models/{model_id}/datasets -- returns a DatasetResponse dict
    (dataset_id, model_id, filename, size_bytes, created_at, status, columns)."""
    base_url, timeout_s = get_runtime_settings(settings)
    return _post_json(f"{base_url}/models/{model_id}/datasets", timeout_s, files={"file": (filename, file_obj)})


def create_calibration_job(
    settings: dict[str, Any],
    model_id: str,
    dataset_id: str,
    configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST /models/{model_id}/calibrations -- returns a CalibrationJobResponse
    dict (job_id, model_id, method, dataset_id, status, experiment_id,
    created_at, started_at, completed_at, error)."""
    base_url, timeout_s = get_runtime_settings(settings)
    body = {"method": "hebo", "dataset_id": dataset_id, "configuration": configuration or {}}
    return _post_json(f"{base_url}/models/{model_id}/calibrations", timeout_s, json=body)


def get_calibration_job(settings: dict[str, Any], model_id: str, job_id: str) -> dict[str, Any]:
    """GET /models/{model_id}/calibrations/{job_id} -- same shape as
    create_calibration_job's response."""
    base_url, timeout_s = get_runtime_settings(settings)
    return _get_json(f"{base_url}/models/{model_id}/calibrations/{job_id}", timeout_s)


def get_calibration_results(settings: dict[str, Any], model_id: str, job_id: str) -> dict[str, Any]:
    """GET /models/{model_id}/calibrations/{job_id}/results -- only valid once
    the job's status is COMPLETED (409 otherwise, surfaced here as a
    RuntimeError like any other non-2xx response)."""
    base_url, timeout_s = get_runtime_settings(settings)
    return _get_json(f"{base_url}/models/{model_id}/calibrations/{job_id}/results", timeout_s)


def cancel_calibration_job(settings: dict[str, Any], model_id: str, job_id: str) -> dict[str, Any]:
    """POST /models/{model_id}/calibrations/{job_id}/cancel -- returns the
    updated CalibrationJobResponse dict."""
    base_url, timeout_s = get_runtime_settings(settings)
    return _post_json(f"{base_url}/models/{model_id}/calibrations/{job_id}/cancel", timeout_s)
