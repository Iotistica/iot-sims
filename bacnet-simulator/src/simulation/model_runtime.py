from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from ..monitoring.event_log import _log_event
from .model_store import (
    get_simulation_model,
    list_enabled_simulation_models,
    purge_unsupported_simulation_models,
)
from .models.remote_catalog import (
    get_remote_model_definition,
    get_runtime_settings,
    normalize_remote_model_id,
)
from .weather.epw import start_hour_of_year
from .providers import (
    FMUAggregateInput,
    FMUInputExposure,
    FMUPointBinding,
    FMUSimulationProvider,
    SimulationContext,
)
from .providers.fmu import FMURuntimeClient


log = logging.getLogger("bacnet-sim")


RUNTIME_PREFIXES = ("fmu:", "learned:")

# Circuit breaker for recover_unhealthy_simulation_models: a model whose
# model_type doesn't resolve in the catalog (e.g. orphaned by an upstream
# catalog id change) can never become healthy on its own -- without this,
# every recovery sweep would retry and fail it forever, forever logging
# "FMU SESSION RECOVER FAILED". Keyed by (id(engine), model id) rather than
# just model id: there is normally exactly one long-lived SimEngine per
# process, so id(engine) is effectively a process/engine-lifetime scope --
# it naturally resets counts if the engine is ever replaced, and (as a
# side benefit) keeps this state from leaking between independently-
# constructed engine instances in tests, where small integer model ids are
# routinely reused across unrelated test databases. Reset to 0 the moment
# a model is next seen already running (including after an operator fixes
# it via PUT /simulation/models/{id}, which calls reload_model directly
# and so succeeds outside this sweep entirely).
_recovery_failure_counts: dict[tuple[int, int], int] = {}
_MAX_CONSECUTIVE_RECOVERY_FAILURES = 5


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


def runtime_signature(config: dict) -> tuple:
    """A hashable snapshot of everything about a simulation model config
    that actually determines how its provider is built -- provider/model
    identity, parameters, and point wiring. Deliberately excludes `name`,
    `description`, and every denormalized display field a mapping/exposure
    row carries (point_name, device_name, object_type, units, ...) --
    those can change (e.g. renaming the point a mapping already points at)
    without the wiring itself changing at all. Two configs with an equal
    signature would build an identical provider, so the caller (the model
    PUT route) can skip an unnecessary unregister+reregister -- for an
    EnergyPlus/Spawn-backed FMU that restart re-runs the model's full
    warmup, so doing it on every Apply click even when nothing runtime-
    relevant changed was needlessly resetting simulated time and costing
    real wall-clock minutes."""
    mappings = config.get("mappings", [])
    point_rows = tuple(sorted(
        (
            mapping.get("variable"),
            mapping.get("direction"),
            mapping.get("point_id"),
            mapping.get("conversion"),
        )
        for mapping in mappings if not _is_aggregate_row(mapping)
    ))
    aggregate_rows = tuple(sorted(
        (
            mapping.get("variable"),
            mapping.get("direction"),
            mapping.get("operation"),
            tuple(mapping.get("point_ids") or []),
            tuple(mapping.get("weight_point_ids") or []),
        )
        for mapping in mappings if _is_aggregate_row(mapping)
    ))
    exposure_rows = tuple(sorted(
        (exposure.get("variable"), exposure.get("point_id"))
        for exposure in config.get("input_exposures", [])
    ))
    return (
        config.get("provider_type"),
        config.get("model_type"),
        json.dumps(config.get("parameters") or {}, sort_keys=True),
        config.get("created_from_device_id"),
        point_rows,
        aggregate_rows,
        exposure_rows,
    )


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
            conversion=mapping.get("conversion"),
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

    input_variable_names = {
        variable.name for variable in definition.variables if variable.direction == "input"
    }
    exposure_rows = config.get("input_exposures") or []
    input_exposures = [
        FMUInputExposure(
            variable=str(exposure["variable"]),
            point_id=int(exposure["point_id"]),
        )
        for exposure in exposure_rows
    ]
    for exposure in input_exposures:
        if exposure.variable not in input_variable_names:
            raise ValueError(
                f"Model {config['model_type']!r} has an input exposure for "
                f"{exposure.variable!r}, which is not a declared input variable"
            )
    output_binding_point_ids = {
        binding.point_id for binding in bindings if binding.direction == "output"
    }
    exposure_point_id_conflicts = sorted(
        {exposure.point_id for exposure in input_exposures} & output_binding_point_ids
    )
    if exposure_point_id_conflicts:
        raise ValueError(
            f"Model {config['model_type']!r} has input exposure(s) targeting "
            f"point(s) already claimed by an output mapping: {exposure_point_id_conflicts}"
        )

    aggregate_member_point_ids = {
        pid
        for agg in aggregate_inputs
        for pid in (*agg.point_ids, *(w for w in agg.weight_point_ids if w is not None))
    }

    # A point cannot be both an input source and an output target for the
    # SAME model -- that's a direct self-loop (the model's own output would
    # feed back in as its own input every tick, with no independent driving
    # signal). Found via a live production incident: RTU-1-Supply-Fan-Command
    # was mapped as both fan_command_pct's input (uFan) AND output (yFan);
    # once a stale/zeroed value ever got fed in, the FMU's own
    # "uFan>0.01 else yFan=0" interlock latched the fan off forever, with no
    # way to recover short of a manual point edit -- see
    # _validate_mapping_contract in api/routers/simulation.py for the
    # equivalent save-time guard (this one is defense-in-depth for configs
    # that predate that check, or are constructed by any other path).
    point_input_source_ids = {
        binding.point_id for binding in bindings if binding.direction == "input"
    } | aggregate_member_point_ids
    self_loop_point_ids = sorted(point_input_source_ids & output_binding_point_ids)
    if self_loop_point_ids:
        raise ValueError(
            f"Model {config['model_type']!r} maps point(s) {self_loop_point_ids} "
            "as both an input source and an output target -- a point cannot "
            "feed its own model as an input while also being written by "
            "that model's output"
        )

    inputs = {
        binding.point_id
        for binding in bindings
        if binding.direction == "input"
    } | aggregate_member_point_ids
    outputs = output_binding_point_ids | {exposure.point_id for exposure in input_exposures}

    initial_point_inputs = _resolve_initial_point_inputs(engine, inputs)

    # Session-lifetime FMI String parameter overrides (e.g. Weather's
    # wea_filename) -- whatever the simulation model's own `parameters`
    # blob holds under a name the catalog declared as type="string" or
    # "file" (see remote_catalog.py's _string_parameter_definition -- both
    # are real FMI String parameters at the runtime level, "file" only
    # differs in how the drawer's Parameters UI renders the control for
    # it). Missing/blank values are simply omitted here rather than
    # substituted with param.default -- the FMU runtime already falls back
    # to the catalog default itself (manager.py's
    # _make_string_parameter_payload), so duplicating that here would just
    # be a second place to keep in sync.
    string_parameters = {
        param.name: str(parameters[param.name])
        for param in definition.parameters
        if param.type in ("string", "file") and parameters.get(param.name)
    }

    # "month" parameters (currently only Weather's playback_start_month --
    # see remote_catalog.py's _PLAYBACK_START_MONTH_PARAMETER) never reach
    # the FMU as a String/Real value at all -- they're purely local
    # bacnet-simulator UX, converted into a session-level warmup_seconds
    # override (day fixed at 1; the weather table wraps every exactly one
    # year, so fast-forwarding through a warmup this long lands the session
    # on the chosen month before it ever starts reporting). None (the
    # common case: no such parameter declared, or none selected) means
    # "use the FMU's own default warmup_seconds", unchanged from today.
    warmup_seconds: float | None = None
    for param in definition.parameters:
        if param.type != "month":
            continue
        selected_month = parameters.get(param.name)
        if selected_month is None:
            continue
        warmup_seconds = start_hour_of_year(date(2001, int(selected_month), 1)) * 3600.0
        break

    provider_kwargs: dict[str, Any] = dict(
        runtime_url=runtime_url,
        model=runtime_model,
        bindings=bindings,
        aggregate_inputs=aggregate_inputs,
        input_exposures=input_exposures,
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
    # Only passed when non-empty -- keeps the exact old constructor call
    # shape for every model with no string_parameters (the overwhelming
    # majority), so existing test doubles standing in for
    # FMUSimulationProvider that predate this kwarg keep working unchanged.
    if string_parameters:
        provider_kwargs["string_parameters"] = string_parameters
    if warmup_seconds is not None:
        provider_kwargs["warmup_seconds"] = warmup_seconds
    provider = FMUSimulationProvider(**provider_kwargs)

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

    exposure_device_ids = sorted({
        int(exposure["device_id"])
        for exposure in exposure_rows
        if exposure.get("device_id") is not None
    })
    participant_device_ids = sorted(
        set(_derive_participant_device_ids(mappings)) | set(exposure_device_ids)
    )
    output_device_ids = sorted(
        set(_mapping_device_ids(mappings, direction="output")) | set(exposure_device_ids)
    )

    context = SimulationContext(
        participant_device_ids=participant_device_ids,
        point_configs=[],
        metadata={
            "simulation_model_id": int(config["id"]),
            "provider_type": str(config["provider_type"]),
            "model_type": str(config["model_type"]),
            "name": str(config["name"]),
            "runtime_url": runtime_url,
            "model": runtime_model,
            "participant_device_ids": participant_device_ids,
            "input_device_ids": _mapping_device_ids(mappings, direction="input"),
            "output_device_ids": output_device_ids,
            "initial_point_inputs": initial_point_inputs,
            "input_exposures": [
                {
                    "point_id": int(exposure["point_id"]),
                    "variable": str(exposure["variable"]),
                    "point_name": exposure.get("point_name"),
                    "device_name": exposure.get("device_name"),
                    "device_id": exposure.get("device_id"),
                    "object_type": exposure.get("object_type"),
                    "object_instance": exposure.get("object_instance"),
                    "units": exposure.get("units"),
                }
                for exposure in exposure_rows
            ],
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


def reload_model(
    database: Any,
    engine: Any,
    model_id: int,
    *,
    log_success: bool = True,
    action: str = "started",
) -> dict:
    """
    log_success=False lets a caller that already logs its own, more specific
    success event (currently only set_simulation_model_enabled_route's
    "simulation enabled") suppress this function's generic "FMU model ...
    started" -- otherwise a single Enabled-toggle click produced two log
    lines for the one action. The failure log always fires regardless: it's
    the only place that failure is ever recorded, so suppressing it would
    lose real information, not just deduplicate it.

    action lets a caller that knows this call is actually replacing an
    already-running session (e.g. the model PUT route, when
    runtime_signature() shows a real config change while the model was
    already enabled) say "restarted" instead of the default "started" --
    the default reads as a fresh start even when it's really a restart
    caused by an edit. Kept as a single verb rather than a full override
    string so every caller's message stays the same
    `FMU model "<name>" <action>` shape -- naming which model changed,
    consistently, everywhere this fires.
    """
    config = get_simulation_model(database, model_id)
    if config is None:
        raise ValueError(f"Simulation model {model_id} does not exist")

    runtime_id = provider_runtime_id(config)
    engine.unregister_simulation_provider(runtime_id)

    if config["enabled"]:
        config = {**config, "_settings": database.get_settings()}
        device_id = config.get("created_from_device_id")
        try:
            register_model_config(engine, config)
        except Exception as exc:
            _log_event(
                device_id, "error", f"FMU registration failed ({exc})",
                category="simulation",
            )
            raise
        if log_success:
            _log_event(
                device_id, "info", f'FMU model "{config["name"]}" {action}',
                category="simulation",
            )

    return config


def reconcile_enabled_models(database: Any, engine: Any) -> dict[str, Any]:
    """
    Make runtime registrations match persisted enabled model configs.

    Built-in is never touched. Explicit model providers are rebuilt from DB.
    """
    for purged in purge_unsupported_simulation_models(database):
        _log_event(
            purged.get("created_from_device_id"), "info",
            f'Removed simulation model "{purged["name"]}" '
            f'(unsupported provider type: {purged["provider_type"]})',
            category="simulation",
        )

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
        device_id = config.get("created_from_device_id")
        try:
            runtime_id = register_model_config(
                engine,
                config,
                replace=True,
            )
            loaded.append(runtime_id)
        except Exception as exc:
            _log_event(
                device_id, "error", f"FMU registration failed ({exc})",
                category="simulation",
            )
            errors.append({
                "model_id": str(config["id"]),
                "name": str(config["name"]),
                "error": str(exc),
            })
        else:
            _log_event(
                device_id, "info", f'FMU model "{config["name"]}" started',
                category="simulation",
            )

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
        model_id = int(config["id"])
        failure_key = (id(engine), model_id)
        runtime_id = provider_runtime_id(config)
        status = current_providers.get(runtime_id, {}).get("status")
        if status == "running":
            _recovery_failure_counts.pop(failure_key, None)
            continue

        consecutive_failures = _recovery_failure_counts.get(failure_key, 0)
        if consecutive_failures >= _MAX_CONSECUTIVE_RECOVERY_FAILURES:
            # Circuit open: this model has failed every sweep for a while
            # (most likely a permanently-orphaned model_type, not a
            # transient issue) -- stop retrying it, but keep surfacing it
            # in `errors` so it stays visible rather than silently vanishing.
            log.debug(
                "FMU SESSION RECOVER circuit open, skipping retry: "
                "model_id=%s name=%s consecutive_failures=%s -- fix via "
                "PUT /simulation/models/%s to clear",
                model_id,
                config.get("name"),
                consecutive_failures,
                model_id,
            )
            errors.append({
                "model_id": str(model_id),
                "name": str(config["name"]),
                "error": (
                    f"Giving up after {consecutive_failures} consecutive "
                    "recovery failures -- this model likely needs to be "
                    "re-mapped to a current catalog model, not just retried"
                ),
            })
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
            reload_model(database, engine, model_id)
            new_diagnostics = engine.get_simulation_providers().get(runtime_id, {})
            new_session = (new_diagnostics.get("diagnostics") or {}).get("session_id")
            log.info(
                "FMU SESSION RECOVER OK model_id=%s runtime_id=%s new_session=%s",
                config["id"],
                runtime_id,
                new_session,
            )
            _recovery_failure_counts.pop(failure_key, None)
            recovered.append(runtime_id)
        except Exception as exc:
            new_failure_count = consecutive_failures + 1
            _recovery_failure_counts[failure_key] = new_failure_count
            log.warning(
                "FMU SESSION RECOVER FAILED model_id=%s runtime_id=%s error=%s",
                config["id"],
                runtime_id,
                exc,
            )
            # reload_model() already logged the registration failure itself
            # (device-scoped, category="simulation") -- only log here when
            # this sweep is the one that trips the circuit breaker, so
            # "giving up" is reported once rather than every 30s thereafter
            # (the skip branch above, taken on every later sweep once the
            # circuit is open, deliberately does not log again).
            if new_failure_count == _MAX_CONSECUTIVE_RECOVERY_FAILURES:
                _log_event(
                    config.get("created_from_device_id"), "error",
                    f"FMU recovery abandoned after {new_failure_count} consecutive attempts",
                    category="simulation",
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
