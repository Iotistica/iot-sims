from __future__ import annotations

import logging
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
    FMUAggregateInput,
    FMUPointBinding,
    FMUSimulationProvider,
    SimulationContext,
)
from .providers.fmu import FMURuntimeClient


log = logging.getLogger("bacnet-sim")


RUNTIME_PREFIXES = ("fmu:", "learned:")


def provider_runtime_id(config: dict) -> str:
    return (
        f"{config['provider_type']}:"
        f"{normalize_remote_model_id(str(config['model_type']))}:"
        f"{int(config['id'])}"
    )


def _is_aggregate_row(mapping: dict) -> bool:
    """Discriminates an aggregate mapping row (plural "point_ids") from an
    ordinary Point row (singular "point_id"). Aggregate rows are always
    direction="input" -- output mappings are never aggregated."""
    return "point_ids" in mapping


def _aggregate_row_device_ids(mapping: dict) -> list[int]:
    point_metadata = mapping.get("point_metadata") or {}
    return sorted(
        {
            int(meta["device_id"])
            for meta in point_metadata.values()
            if isinstance(meta, dict) and meta.get("device_id") is not None
        }
    )


def _mapping_device_ids(mappings: list[dict], direction: str | None = None) -> list[int]:
    """Every device_id referenced by the given mappings, folding in an
    aggregate row's several member devices (via its point_metadata) as well
    as an ordinary row's single device_id. direction=None means "any"."""
    device_ids: set[int] = set()
    for mapping in mappings:
        if direction is not None and mapping.get("direction") != direction:
            continue
        if _is_aggregate_row(mapping):
            device_ids.update(_aggregate_row_device_ids(mapping))
        elif mapping.get("device_id") is not None:
            device_ids.add(int(mapping["device_id"]))
    return sorted(device_ids)


def _derive_participant_device_ids(mappings: list[dict]) -> list[int]:
    return _mapping_device_ids(mappings)


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


def _resolve_initial_point_inputs(
    engine: Any,
    input_point_ids: set[int],
) -> dict[int, Any]:
    """Resolve each input point's (ordinary Point mapping or aggregate
    member) current live value from the engine so
    FMUSimulationProvider.initialize() can seed its warmup with real data
    instead of the FMU's metadata/catalog default. Unresolvable points are
    simply omitted here -- FMUSimulationProvider.initialize() is responsible
    for treating a missing input as fatal (it must never silently
    substitute a default for a configured mapping, Point or aggregate).
    """
    resolver = getattr(engine, "resolve_provider_input_value", None)
    if resolver is None:
        return {}
    initial_point_inputs: dict[int, Any] = {}
    for point_id in input_point_ids:
        value = resolver(point_id)
        if value is not None:
            initial_point_inputs[point_id] = value
    return initial_point_inputs


def _build_fmu_provider(
    config: dict,
    engine: Any,
) -> tuple[Any, SimulationContext, set[int], set[int]]:
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

    point_rows = [m for m in mappings if not _is_aggregate_row(m)]
    aggregate_rows = [m for m in mappings if _is_aggregate_row(m)]

    bindings = [
        FMUPointBinding(
            point_id=int(mapping["point_id"]),
            variable=str(mapping["variable"]),
            direction=str(mapping["direction"]),
        )
        for mapping in point_rows
    ]
    aggregate_inputs = [
        FMUAggregateInput(
            variable=str(mapping["variable"]),
            operation=str(mapping.get("operation") or "max"),
            point_ids=tuple(int(pid) for pid in mapping["point_ids"]),
            # Absent entirely on a "max" row (or a hand-built dict that
            # predates this field) -- () is FMUAggregateInput's own default,
            # so this stays a no-op for every non-weighted aggregate.
            weight_point_ids=tuple(
                int(w) if w is not None else None
                for w in (mapping.get("weight_point_ids") or [])
            ),
        )
        for mapping in aggregate_rows
    ]

    # Reject more than one source for the same input variable -- covers
    # Point+Point, Point+Aggregate, and Aggregate+Aggregate. Two ordinary
    # Point rows for one variable would otherwise silently last-wins
    # overwrite each other in the payload built from self._inputs; this is
    # normally caught by the API layer's own duplicate-mapping check, which
    # doesn't run on this runtime-only aggregate path, so it's re-checked
    # here at registration time (FMUSimulationProvider.validate() also
    # re-checks it defensively for providers constructed directly).
    input_variable_counts: dict[str, int] = {}
    for binding in bindings:
        if binding.direction == "input":
            input_variable_counts[binding.variable] = input_variable_counts.get(binding.variable, 0) + 1
    for agg in aggregate_inputs:
        input_variable_counts[agg.variable] = input_variable_counts.get(agg.variable, 0) + 1
    conflicting = sorted(name for name, count in input_variable_counts.items() if count > 1)
    if conflicting:
        raise ValueError(
            f"Model {config['model_type']!r} has more than one input source "
            f"mapped to the same variable(s): {conflicting}"
        )

    aggregate_member_point_ids = {
        pid
        for agg in aggregate_inputs
        for pid in (*agg.point_ids, *(w for w in agg.weight_point_ids if w is not None))
    }

    inputs = {
        binding.point_id
        for binding in bindings
        if binding.direction == "input"
    } | aggregate_member_point_ids
    outputs = {
        binding.point_id
        for binding in bindings
        if binding.direction == "output"
    }

    initial_point_inputs = _resolve_initial_point_inputs(engine, inputs)

    provider = FMUSimulationProvider(
        runtime_url=runtime_url,
        model=runtime_model,
        bindings=bindings,
        aggregate_inputs=aggregate_inputs,
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

    aggregate_member_bindings_metadata = [
        {
            "point_id": int(point_id),
            "variable": str(mapping["variable"]),
            "direction": "input",
            **{
                key: value
                for key, value in (mapping.get("point_metadata", {}).get(point_id) or {}).items()
            },
        }
        for mapping in aggregate_rows
        for point_id in [
            *mapping["point_ids"],
            # weighted_average's weight points get a binding-metadata row
            # the same way value points do, so _aggregate_member_metadata
            # can look them up by (point_id, variable, direction) too --
            # absent/empty for "max" rows, a no-op.
            *(w for w in (mapping.get("weight_point_ids") or []) if w is not None),
        ]
    ]

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
            "input_device_ids": _mapping_device_ids(mappings, direction="input"),
            "output_device_ids": _mapping_device_ids(mappings, direction="output"),
            "initial_point_inputs": initial_point_inputs,
            "bindings": [
                {
                    "point_id": int(mapping["point_id"]),
                    "variable": str(mapping["variable"]),
                    "direction": str(mapping["direction"]),
                    "point_name": mapping.get("point_name"),
                    "device_name": mapping.get("device_name"),
                    "device_id": mapping.get("device_id"),
                    "object_type": mapping.get("object_type"),
                    "object_instance": mapping.get("object_instance"),
                    "units": mapping.get("units"),
                }
                for mapping in point_rows
            ] + aggregate_member_bindings_metadata,
            "aggregate_inputs": [
                {
                    "variable": agg.variable,
                    "source": "aggregate",
                    "operation": agg.operation,
                    "point_ids": list(agg.point_ids),
                    # Only present for weighted_average -- keeps "max"
                    # entries byte-identical to before this field existed.
                    **(
                        {"weight_point_ids": list(agg.weight_point_ids)}
                        if agg.operation == "weighted_average"
                        else {}
                    ),
                }
                for agg in aggregate_inputs
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
        provider, context, inputs, outputs = _build_fmu_provider(config, engine)
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


def recover_unhealthy_simulation_models(database: Any, engine: Any) -> dict[str, Any]:
    """
    Self-healing sweep: reload only enabled model configs whose runtime
    registration is missing entirely or reports status "error" -- e.g. after
    the FMU runtime process restarted mid-run and the session was lost, or a
    previous initialize() aborted because a Point mapping had no live value
    yet (see FMUInputResolutionError in providers/fmu.py). Healthy (RUNNING)
    providers are left completely untouched, so this never creates a
    duplicate session for an already-working model.

    Does one cheap health() probe against the shared FMU runtime URL first;
    if it's unreachable, defers every candidate to the next sweep instead of
    attempting (and timing out on) a per-model reload -- this is the guard
    against a retry storm during an extended runtime outage.
    """
    settings = database.get_settings()
    persisted = [
        {**config, "_settings": settings}
        for config in list_enabled_simulation_models(database)
        if str(config.get("provider_type")) == "fmu"
    ]
    if not persisted:
        return {"recovered": [], "skipped": [], "errors": [], "runtime_unreachable": False}

    runtime_url, timeout_s = get_runtime_settings(settings)
    try:
        FMURuntimeClient(runtime_url, timeout_s).health()
    except Exception as exc:
        log.warning(
            "FMU SESSION RECOVER runtime unreachable at %s, deferring %d "
            "model(s): %s",
            runtime_url,
            len(persisted),
            exc,
        )
        return {
            "recovered": [],
            "skipped": [config["id"] for config in persisted],
            "errors": [],
            "runtime_unreachable": True,
        }

    current_providers = engine.get_simulation_providers()

    recovered: list[str] = []
    errors: list[dict[str, str]] = []

    for config in persisted:
        runtime_id = provider_runtime_id(config)
        status = current_providers.get(runtime_id, {}).get("status")
        if status == "running":
            continue

        reason = "missing" if runtime_id not in current_providers else str(status)
        log.info(
            "FMU SESSION RECOVER model_id=%s name=%s runtime_id=%s reason=%s",
            config["id"],
            config.get("name"),
            runtime_id,
            reason,
        )
        try:
            reload_model(database, engine, int(config["id"]))
            new_diagnostics = engine.get_simulation_providers().get(runtime_id, {})
            new_session = (new_diagnostics.get("diagnostics") or {}).get("session_id")
            log.info(
                "FMU SESSION RECOVER OK model_id=%s runtime_id=%s new_session=%s",
                config["id"],
                runtime_id,
                new_session,
            )
            recovered.append(runtime_id)
        except Exception as exc:
            log.warning(
                "FMU SESSION RECOVER FAILED model_id=%s runtime_id=%s error=%s",
                config["id"],
                runtime_id,
                exc,
            )
            errors.append({
                "model_id": str(config["id"]),
                "name": str(config["name"]),
                "error": str(exc),
            })

    return {
        "recovered": recovered,
        "skipped": [],
        "errors": errors,
        "runtime_unreachable": False,
    }
