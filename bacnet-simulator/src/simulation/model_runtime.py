from __future__ import annotations

from typing import Any

from .model_store import (
    get_simulation_model,
    list_enabled_simulation_models,
)
from .models.remote_catalog import (
    get_remote_model_definition,
    get_runtime_settings,
    normalize_remote_model_id,
)
from .providers import (
    FMUPointBinding,
    FMUSimulationProvider,
    SimulationContext,
)


RUNTIME_PREFIXES = ("fmu:", "learned:")


def provider_runtime_id(config: dict) -> str:
    return (
        f"{config['provider_type']}:"
        f"{normalize_remote_model_id(str(config['model_type']))}:"
        f"{int(config['id'])}"
    )


def _derive_participant_device_ids(mappings: list[dict]) -> list[int]:
    return sorted(
        {
            int(mapping["device_id"])
            for mapping in mappings
            if mapping.get("device_id") is not None
        }
    )


def _effective_mappings(config: dict) -> list[dict]:
    parameters = dict(config.get("parameters") or {})
    input_sources = parameters.get("input_sources") or {}
    return [
        mapping
        for mapping in config.get("mappings", [])
        if not (
            str(config.get("provider_type")) == "fmu"
            and mapping.get("direction") == "input"
            and input_sources.get(mapping.get("variable")) == "constant"
        )
    ]


def _build_fmu_provider(config: dict) -> tuple[Any, SimulationContext, set[int], set[int]]:
    settings = config.get("_settings") or {}
    definition = get_remote_model_definition(settings, str(config["model_type"]))

    if definition.provider_type != "fmu":
        raise ValueError(
            f"Model {config['model_type']!r} is not an FMU model"
        )

    parameters = dict(config.get("parameters") or {})
    mappings = _effective_mappings(config)
    runtime_url, timeout_s = get_runtime_settings(settings)
    runtime_model = definition.runtime_model or normalize_remote_model_id(
        str(config["model_type"])
    )
    bindings = [
        FMUPointBinding(
            point_id=int(mapping["point_id"]),
            variable=str(mapping["variable"]),
            direction=str(mapping["direction"]),
        )
        for mapping in mappings
    ]

    provider = FMUSimulationProvider(
        runtime_url=runtime_url,
        model=runtime_model,
        bindings=bindings,
        input_defaults=dict(parameters.get("input_defaults") or {}),
        timeout_s=timeout_s,
        input_variables={
            variable.name
            for variable in definition.variables
            if variable.direction == "input"
        },
        output_variables={
            variable.name
            for variable in definition.variables
            if variable.direction == "output"
        },
    )

    inputs = {
        binding.point_id
        for binding in bindings
        if binding.direction == "input"
    }
    outputs = {
        binding.point_id
        for binding in bindings
        if binding.direction == "output"
    }

    context = SimulationContext(
        participant_device_ids=_derive_participant_device_ids(
            mappings
        ),
        point_configs=[],
        metadata={
            "simulation_model_id": int(config["id"]),
            "provider_type": str(config["provider_type"]),
            "model_type": str(config["model_type"]),
            "name": str(config["name"]),
            "runtime_url": runtime_url,
            "model": runtime_model,
            "participant_device_ids": _derive_participant_device_ids(
                mappings
            ),
            "input_device_ids": sorted(
                {
                    int(mapping["device_id"])
                    for mapping in mappings
                    if mapping.get("direction") == "input"
                    and mapping.get("device_id") is not None
                }
            ),
            "output_device_ids": sorted(
                {
                    int(mapping["device_id"])
                    for mapping in mappings
                    if mapping.get("direction") == "output"
                    and mapping.get("device_id") is not None
                }
            ),
            "bindings": [
                {
                    "point_id": int(mapping["point_id"]),
                    "variable": str(mapping["variable"]),
                    "direction": str(mapping["direction"]),
                    "point_name": mapping.get("point_name"),
                    "device_name": mapping.get("device_name"),
                    "object_type": mapping.get("object_type"),
                    "object_instance": mapping.get("object_instance"),
                    "units": mapping.get("units"),
                }
                for mapping in mappings
            ],
        },
    )

    return provider, context, inputs, outputs


def register_model_config(
    engine: Any,
    config: dict,
    *,
    replace: bool = True,
) -> str:
    config = dict(config)
    provider_type = str(config["provider_type"])

    if provider_type == "fmu":
        provider, context, inputs, outputs = _build_fmu_provider(config)
    elif provider_type == "learned":
        raise ValueError(
            "Learned Twin model persistence is recognized but learned-model "
            "loading is not implemented yet"
        )
    else:
        raise ValueError(f"Unsupported provider type: {provider_type}")

    runtime_id = provider_runtime_id(config)

    engine.register_simulation_provider(
        runtime_id,
        provider,
        context=context,
        input_point_ids=inputs,
        output_point_ids=outputs,
        replace=replace,
    )

    return runtime_id


def unregister_model_config(engine: Any, config: dict) -> bool:
    return bool(
        engine.unregister_simulation_provider(
            provider_runtime_id(config)
        )
    )


def reload_model(database: Any, engine: Any, model_id: int) -> dict:
    config = get_simulation_model(database, model_id)
    if config is None:
        raise ValueError(f"Simulation model {model_id} does not exist")

    runtime_id = provider_runtime_id(config)
    engine.unregister_simulation_provider(runtime_id)

    if config["enabled"]:
        config = {**config, "_settings": database.get_settings()}
        register_model_config(engine, config)

    return config


def reconcile_enabled_models(database: Any, engine: Any) -> dict[str, Any]:
    """
    Make runtime registrations match persisted enabled model configs.

    Built-in is never touched. Explicit model providers are rebuilt from DB.
    """
    settings = database.get_settings()
    persisted = [
        {**config, "_settings": settings}
        for config in list_enabled_simulation_models(database)
    ]
    desired_ids = {
        provider_runtime_id(config)
        for config in persisted
    }

    current = set(engine.get_simulation_providers())

    removed: list[str] = []
    for runtime_id in current:
        if runtime_id == "builtin":
            continue
        if runtime_id.startswith(RUNTIME_PREFIXES) and runtime_id not in desired_ids:
            if engine.unregister_simulation_provider(runtime_id):
                removed.append(runtime_id)

    loaded: list[str] = []
    errors: list[dict[str, str]] = []

    for config in persisted:
        try:
            runtime_id = register_model_config(
                engine,
                config,
                replace=True,
            )
            loaded.append(runtime_id)
        except Exception as exc:
            errors.append({
                "model_id": str(config["id"]),
                "name": str(config["name"]),
                "error": str(exc),
            })

    return {
        "loaded": loaded,
        "removed": removed,
        "errors": errors,
    }
