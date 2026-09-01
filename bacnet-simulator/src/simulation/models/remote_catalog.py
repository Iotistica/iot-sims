from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .registry import MappingHints, ModelDefinition, ParameterDefinition, VariableDefinition


_CACHE_TTL_SECONDS = 30.0
_catalog_cache: dict[tuple[str, float], tuple[float, list[dict[str, Any]]]] = {}
_metadata_cache: dict[tuple[str, float, str], tuple[float, dict[str, Any]]] = {}


def normalize_remote_model_id(model_id: str) -> str:
    """No-op today -- kept as the single boundary every caller already
    normalizes a model id through, so a future id migration (like the
    pre-GUID -> GUID cutover this used to bridge, removed once no live
    config or project export still needed it) has one place to hook back
    in rather than touching every call site again."""
    return model_id


def _request_json(base_url: str, timeout_s: float, path: str, api_key: str | None = None) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = Request(url, headers=headers, method="GET")
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


def get_runtime_settings(settings: dict[str, Any]) -> tuple[str, float, str | None]:
    base_url = str(settings.get("fmu_runtime_url") or "http://localhost:8002").strip()
    timeout_s = float(settings.get("fmu_runtime_timeout_s") or 20.0)
    api_key = str(settings.get("fmu_runtime_api_key") or "").strip() or None
    return base_url.rstrip("/"), timeout_s, api_key


def fetch_remote_catalog(
    settings: dict[str, Any],
    *,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    base_url, timeout_s, api_key = get_runtime_settings(settings)
    cache_key = (base_url, timeout_s)
    now = time.monotonic()
    cached = _catalog_cache.get(cache_key)
    if (
        not force_refresh
        and cached is not None
        and now - cached[0] < _CACHE_TTL_SECONDS
    ):
        return cached[1]

    payload = _request_json(base_url, timeout_s, "/models", api_key)
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
    base_url, timeout_s, api_key = get_runtime_settings(settings)
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
        api_key,
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


def _string_parameter_definition(data: dict[str, Any]) -> ParameterDefinition:
    # "file" (not plain "string") when the catalog flags this parameter's
    # value as a file path (e.g. Weather's wea_filename) -- tells
    # SimulationModelDrawer.vue to render an upload control (via
    # /simulation/resources, remote_resources.py's relay to iot-models'
    # generic /resources endpoint) instead of a free-text box. Any future
    # string_parameters entry with is_file omitted/false stays a plain
    # free-text "string" field, e.g. a text label rather than a file
    # reference.
    param_type = "file" if data.get("is_file") else "string"
    return ParameterDefinition(
        name=str(data["name"]),
        label=str(data.get("label") or data["name"]),
        type=param_type,
        default=data.get("default"),
        required=bool(data.get("required", False)),
        advanced=bool(data.get("advanced", False)),
    )


# Weather-specific: appended to that one model's parameters below, never
# sourced from iot-models' model.json (iot-models has no idea this
# parameter exists -- it only ever receives the plain warmup_seconds float
# _build_fmu_provider computes from it, via the generic per-session
# warmup override every model could use for its own reasons). Kept here,
# hardcoded -- Weather-specific UX knowledge lives in bacnet-simulator, not
# in the FMU runtime.
_WEATHER_MODEL_SLUG = "Weather"
_PLAYBACK_START_MONTH_PARAMETER = ParameterDefinition(
    name="playback_start_month",
    label="Playback Start Month",
    type="month",
    default=1,
    required=False,
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
    # Session-lifetime FMI String parameters (e.g. Weather's wea_filename,
    # eventually EnergyPlusThermalZone's epw_filename/wea_filename) --
    # surfaced through the existing generic Parameters UI (renders a
    # free-text input for type="string") rather than a bespoke picker for
    # now. See _build_fmu_provider's own comment for how a value entered
    # here actually reaches the FMU runtime as an initialize()-time
    # string_parameters override.
    parameters = tuple(
        _string_parameter_definition(item)
        for item in metadata.get("string_parameters", [])
        if isinstance(item, dict) and item.get("name")
    )
    if str(metadata.get("slug") or "") == _WEATHER_MODEL_SLUG:
        parameters = parameters + (_PLAYBACK_START_MONTH_PARAMETER,)
    return ModelDefinition(
        model_type=model_id,
        label=str(metadata.get("label") or model_id),
        provider_type="fmu",
        description=str(metadata.get("description") or ""),
        parameters=parameters,
        variables=tuple(variables),
        factory=lambda parameters: None,
        runtime_model=model_id,
    )


def get_remote_model_definition(settings: dict[str, Any], model_id: str) -> ModelDefinition:
    return definition_from_metadata(fetch_remote_metadata(settings, model_id))


def get_remote_model_catalog(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Every catalog entry needs its own /metadata fetch (the summary list
    from fetch_remote_catalog() doesn't carry inputs/outputs) -- fetched
    concurrently, not one HTTP round trip after another, since a cold
    _metadata_cache (a fresh model added, a restarted runtime, or just the
    30s TTL having lapsed) previously meant the Simulation Model drawer's
    open() waited on N sequential round trips to the FMU runtime instead
    of one. Safe to run concurrently: fetch_remote_metadata()'s own cache
    is a plain dict, but every thread here writes a different model_id key,
    and dict item assignment is already atomic under the GIL."""
    model_ids = [
        str(summary["id"])
        for summary in fetch_remote_catalog(settings)
        if summary.get("id")
    ]
    if not model_ids:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(model_ids))) as pool:
        metadatas = pool.map(
            lambda model_id: fetch_remote_metadata(settings, model_id),
            model_ids,
        )
    return [
        definition_from_metadata(metadata).catalog_entry()
        for metadata in metadatas
    ]
