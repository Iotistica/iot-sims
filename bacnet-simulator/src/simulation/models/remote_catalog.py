from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .registry import MappingHints, ModelDefinition, VariableDefinition


LEGACY_MODEL_IDS = {
    "simple_vav_zone_fmu": "SimpleVAVZone",
    "simple_ahu_fmu": "SimpleAHU",
}

_CACHE_TTL_SECONDS = 30.0
_catalog_cache: dict[tuple[str, float], tuple[float, list[dict[str, Any]]]] = {}
_metadata_cache: dict[tuple[str, float, str], tuple[float, dict[str, Any]]] = {}


def normalize_remote_model_id(model_id: str) -> str:
    return LEGACY_MODEL_IDS.get(model_id, model_id)


def _request_json(base_url: str, timeout_s: float, path: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"FMU model runtime HTTP {int(exc.code)} for {url}: {body}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"FMU model runtime cannot be reached at {base_url}: {exc.reason}"
        ) from exc

    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"FMU model runtime returned non-JSON response for {url}: {raw[:200]}"
        ) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(
            f"FMU model runtime returned {type(decoded).__name__}, expected object"
        )
    return decoded


def get_runtime_settings(settings: dict[str, Any]) -> tuple[str, float]:
    base_url = str(settings.get("fmu_runtime_url") or "http://localhost:8002").strip()
    timeout_s = float(settings.get("fmu_runtime_timeout_s") or 20.0)
    return base_url.rstrip("/"), timeout_s


def fetch_remote_catalog(
    settings: dict[str, Any],
    *,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    base_url, timeout_s = get_runtime_settings(settings)
    cache_key = (base_url, timeout_s)
    now = time.monotonic()
    cached = _catalog_cache.get(cache_key)
    if (
        not force_refresh
        and cached is not None
        and now - cached[0] < _CACHE_TTL_SECONDS
    ):
        return cached[1]

    payload = _request_json(base_url, timeout_s, "/models")
    models = payload.get("models")
    if not isinstance(models, list):
        raise RuntimeError("FMU model runtime /models response did not include a models list")
    result = [item for item in models if isinstance(item, dict)]
    _catalog_cache[cache_key] = (now, result)
    return result


def fetch_remote_metadata(
    settings: dict[str, Any],
    model_id: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    base_url, timeout_s = get_runtime_settings(settings)
    normalized_model_id = normalize_remote_model_id(model_id)
    cache_key = (base_url, timeout_s, normalized_model_id)
    now = time.monotonic()
    cached = _metadata_cache.get(cache_key)
    if (
        not force_refresh
        and cached is not None
        and now - cached[0] < _CACHE_TTL_SECONDS
    ):
        return cached[1]

    metadata = _request_json(
        base_url,
        timeout_s,
        f"/models/{normalized_model_id}/metadata",
    )
    _metadata_cache[cache_key] = (now, metadata)
    return metadata


def _unit_label(unit: Any) -> str | None:
    if unit is None:
        return None
    value = str(unit)
    aliases = {
        "degC": "°C",
        "percent": "%",
    }
    return aliases.get(value, value)


def _mapping_hints(data: dict[str, Any]) -> MappingHints | None:
    raw = data.get("mapping_hints")
    if not isinstance(raw, dict):
        return None
    return MappingHints(
        equipment_scope=raw.get("equipment_scope") or "self",
        preferred_equipment_types=tuple(raw.get("preferred_equipment_types") or ()),
        relationship=raw.get("relationship"),
        signal_role=raw.get("signal_role"),
    )


def _suggested_point_types(data: dict[str, Any]) -> tuple[str, ...]:
    values = data.get("suggested_point_types")
    if isinstance(values, list):
        return tuple(str(value) for value in values if value)
    semantic = data.get("semantic")
    if isinstance(semantic, dict) and semantic.get("point_class"):
        return (str(semantic["point_class"]),)
    return ()


def _variable(data: dict[str, Any], direction: str) -> VariableDefinition:
    return VariableDefinition(
        name=str(data["name"]),
        label=str(data.get("label") or data["name"]),
        direction=direction,
        unit=_unit_label(data.get("unit")),
        default=data.get("default"),
        required=bool(data.get("required", False)),
        suggested_point_types=_suggested_point_types(data),
        mapping_hints=_mapping_hints(data),
    )


def definition_from_metadata(metadata: dict[str, Any]) -> ModelDefinition:
    model_id = str(metadata["id"])
    variables = [
        *[
            _variable(item, "input")
            for item in metadata.get("inputs", [])
            if isinstance(item, dict) and item.get("name")
        ],
        *[
            _variable(item, "output")
            for item in metadata.get("outputs", [])
            if isinstance(item, dict) and item.get("name")
        ],
    ]
    return ModelDefinition(
        model_type=model_id,
        label=str(metadata.get("label") or model_id),
        provider_type="fmu",
        description=str(metadata.get("description") or ""),
        parameters=(),
        variables=tuple(variables),
        factory=lambda parameters: None,
        runtime_model=model_id,
    )


def get_remote_model_definition(settings: dict[str, Any], model_id: str) -> ModelDefinition:
    return definition_from_metadata(fetch_remote_metadata(settings, model_id))


def get_remote_model_catalog(settings: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for summary in fetch_remote_catalog(settings):
        model_id = summary.get("id")
        if not model_id:
            continue
        metadata = fetch_remote_metadata(settings, str(model_id))
        entries.append(definition_from_metadata(metadata).catalog_entry())
    return entries
