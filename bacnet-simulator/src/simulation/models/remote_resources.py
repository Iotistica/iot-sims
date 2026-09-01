"""Relay for the FMU runtime's generic, model-agnostic /resources upload
endpoint (shared/runtime/resources/ in iot-models) -- lets a user upload a
file (e.g. a converted .mos weather file) that any catalog model's
"is_file" string parameter can then reference by its resolved server-side
path. Deliberately not scoped to weather or any one model, matching that
endpoint's own genericity -- see StringParameterMetadata.is_file in
iot-models' catalog.py for the flag that tells the Simulation Model
drawer's Parameters UI to render an upload control at all.

Uses `requests` rather than remote_catalog.py's plain-urllib `_request_json`
pattern -- multipart file upload is what requests is actually good at,
and it's already a project dependency (used elsewhere), so this doesn't
add anything new; urllib was only chosen for the JSON-only catalog calls
specifically to avoid adding a dependency for something it already does
fine.
"""
from __future__ import annotations

from typing import Any, BinaryIO

import requests

from .remote_catalog import get_runtime_settings


def upload_resource(
    settings: dict[str, Any],
    filename: str,
    file_obj: BinaryIO,
) -> dict[str, Any]:
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    url = f"{base_url}/resources"
    try:
        response = requests.post(
            url,
            files={"file": (filename, file_obj)},
            timeout=timeout_s,
            headers={"X-API-Key": api_key} if api_key else {},
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"FMU model runtime cannot be reached at {base_url}: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"FMU model runtime HTTP {response.status_code} for {url}: {response.text}"
        )
    try:
        decoded = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"FMU model runtime returned non-JSON response for {url}: {response.text[:200]}"
        ) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(
            f"FMU model runtime returned {type(decoded).__name__}, expected object"
        )
    return decoded


def download_resource_content(settings: dict[str, Any], filename: str) -> bytes:
    """Re-fetches an already-uploaded resource's raw bytes -- used by the
    weather-provenance endpoint to re-parse a file's #COMMENTS header on
    drawer reopen, when the upload response (computed once, at upload
    time) is no longer in hand."""
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    url = f"{base_url}/resources/{filename}/content"
    try:
        response = requests.get(url, timeout=timeout_s, headers={"X-API-Key": api_key} if api_key else {})
    except requests.RequestException as exc:
        raise RuntimeError(f"FMU model runtime cannot be reached at {base_url}: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"FMU model runtime HTTP {response.status_code} for {url}: {response.text}"
        )
    return response.content


def list_resources(settings: dict[str, Any]) -> list[dict[str, Any]]:
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    url = f"{base_url}/resources"
    try:
        response = requests.get(url, timeout=timeout_s, headers={"X-API-Key": api_key} if api_key else {})
    except requests.RequestException as exc:
        raise RuntimeError(f"FMU model runtime cannot be reached at {base_url}: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(
            f"FMU model runtime HTTP {response.status_code} for {url}: {response.text}"
        )
    payload = response.json()
    resources = payload.get("resources")
    if not isinstance(resources, list):
        raise RuntimeError("FMU model runtime /resources response did not include a resources list")
    return [item for item in resources if isinstance(item, dict)]
