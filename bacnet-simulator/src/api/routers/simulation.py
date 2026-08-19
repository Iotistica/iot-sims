from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import asdict
from typing import Any, Literal
from urllib.error import HTTPError

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from ...core.config import BINARY_TYPES, MULTISTATE_TYPES
from ...integrations.azure_openai import AzureStructuredClient
from ...simulation.mapping_ai_suggestions import suggest_point_for_variable_via_ai
from ...simulation.mapping_suggestions import (
    build_shortlist,
    relationship_context_string,
    suggest_mappings_for_model,
)
from ...simulation.model_runtime import (
    provider_runtime_id,
    reconcile_enabled_models,
    reload_model,
)
from ...simulation.model_store import (
    create_simulation_model,
    delete_simulation_model,
    ensure_simulation_model_schema,
    get_explicit_exposure_owner,
    get_explicit_output_owner,
    get_simulation_model,
    list_simulation_models,
    update_simulation_model,
)
from ...simulation.models.remote_catalog import (
    get_remote_model_catalog,
    get_remote_model_definition,
    normalize_remote_model_id,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["simulation"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable",
        )
    return database


def get_engine(request: Request) -> Any:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail="Simulation engine is unavailable",
        )
    return engine


def get_energy_engine(request: Request) -> Any | None:
    return getattr(request.app.state, "energy_engine", None)


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------

class SimulationModelMappingPayload(BaseModel):
    variable: str = Field(min_length=1)
    direction: Literal["input", "output"]
    point_id: int = Field(gt=0)
    source: Literal["point"] = "point"


class SimulationModelAggregateMappingPayload(BaseModel):
    """An FMU input driven by an operation ("max" or "weighted_average")
    over several BACnet points, instead of exactly one point or a constant.
    direction/operation are Literal so "direction must be input" and
    "unsupported operation" are enforced structurally by Pydantic -- no
    manual check needed, same as SimulationModelMappingPayload.direction
    already does for point/output.

    weight_point_ids is positionally parallel to point_ids (index i's
    weight is weight_point_ids[i]) and is required -- every entry present,
    same length as point_ids -- iff operation == "weighted_average"; it
    must be omitted/None for "max" (and any future non-weighted
    operation). This structurally blocks saving a Weighted Average
    configuration with a missing weight mapping (task requirement), the
    same way point_ids' own min_length=1 already structurally blocks an
    empty aggregate -- both are enforced unconditionally, whether the
    model is being saved as a draft or applied, mirroring the existing
    point_ids convention rather than only checking on "apply". Each
    weight_point_id is itself validated against the database (exists,
    simulated device, numeric type) in _validate_mapping_contract below,
    the same way point_ids' own points already are."""
    variable: str = Field(min_length=1)
    direction: Literal["input"] = "input"
    operation: Literal["max", "weighted_average"] = "max"
    point_ids: list[int] = Field(min_length=1)
    weight_point_ids: list[int] | None = None

    @field_validator("point_ids")
    @classmethod
    def _no_duplicate_points(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("Aggregate point_ids must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _weight_point_ids_match_operation(self) -> "SimulationModelAggregateMappingPayload":
        if self.operation == "weighted_average":
            if self.weight_point_ids is None or len(self.weight_point_ids) != len(self.point_ids):
                raise ValueError(
                    f"Weighted Average aggregate {self.variable!r} requires exactly "
                    f"one weight point per value point ({len(self.point_ids)} value "
                    f"point(s), {len(self.weight_point_ids or [])} weight point(s))"
                )
        elif self.weight_point_ids is not None:
            raise ValueError(
                f"weight_point_ids is only valid for operation='weighted_average', "
                f"not {self.operation!r} (variable {self.variable!r})"
            )
        return self


class SimulationModelInputExposurePayload(BaseModel):
    """Mirrors an already-resolved model INPUT value (whichever of Point,
    Aggregate, or Constant currently sources `variable`) onto a second
    BACnet point's Present Value each step, without recomputing it. Not a
    "mapping" in the read/write direction=input/output sense
    SimulationModelMappingPayload uses -- see
    simulation_model_input_exposures's own schema comment in
    model_store.py for why this is a separate list/table instead of a
    third `direction` value."""
    variable: str = Field(min_length=1)
    point_id: int = Field(gt=0)


class SimulationModelPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    provider_type: Literal["fmu", "learned"] = "fmu"
    model_type: str = Field(min_length=1)
    enabled: bool = True
    parameters: dict[str, Any] = Field(default_factory=dict)

    # Context for "right-click Controller -> Add Model" only.
    # Runtime participants are derived from mapped points.
    created_from_device_id: int | None = None

    mappings: list[SimulationModelMappingPayload] = Field(
        default_factory=list
    )
    aggregate_mappings: list[SimulationModelAggregateMappingPayload] = Field(
        default_factory=list
    )
    input_exposures: list[SimulationModelInputExposurePayload] = Field(
        default_factory=list
    )


def _runtime_definition(database: Any, model_type: str):
    normalized = normalize_remote_model_id(model_type)
    try:
        return get_remote_model_definition(
            database.get_settings(),
            normalized,
        )
    except Exception as exc:
        # An HTTP 404 from the runtime means the catalog was reached fine
        # and genuinely doesn't recognize this model id -- most likely a
        # stale/orphaned model_type value (e.g. from before the model-id-
        # to-GUID catalog migration, or a config predating a catalog model
        # being removed). That's a different, more actionable problem than
        # "the runtime is unreachable", and shouldn't be reported as one --
        # a 503 tells an operator to check network/service health, when
        # the real fix is to re-map this simulation model to a current
        # catalog entry.
        http_cause = exc.__cause__ if isinstance(exc.__cause__, HTTPError) else None
        if http_cause is not None and http_cause.code == 404:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": (
                        f"FMU model {normalized!r} was not found in the model "
                        "runtime's catalog -- it may be stale/orphaned "
                        "(e.g. from before a catalog id change) and need to "
                        "be re-mapped to a current model"
                    ),
                    "model_type": normalized,
                },
            ) from exc
        raise HTTPException(
            status_code=503,
            detail={
                "message": "FMU model runtime cannot be reached",
                "runtime_error": str(exc),
            },
        ) from exc


class MappingSuggestionsRequest(BaseModel):
    model_type: str = Field(min_length=1)
    provider_type: Literal["fmu", "learned"] = "fmu"
    created_from_device_id: int | None = None
    current_model_id: int | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class MappingSuggestionAiRequest(BaseModel):
    model_type: str = Field(min_length=1)
    variable: str = Field(min_length=1)
    created_from_device_id: int | None = None
    current_model_id: int | None = None


class VariableCandidatesRequest(BaseModel):
    model_type: str = Field(min_length=1)
    variable: str = Field(min_length=1)
    created_from_device_id: int | None = None
    current_model_id: int | None = None
    limit: int = Field(default=50, ge=1, le=200)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _mapping_dicts(
    payload: SimulationModelPayload,
) -> list[dict[str, Any]]:
    parameters = payload.parameters if isinstance(payload.parameters, dict) else {}
    input_sources = parameters.get("input_sources") or {}
    return [
        {
            "variable": mapping.variable,
            "direction": mapping.direction,
            "point_id": mapping.point_id,
        }
        for mapping in payload.mappings
        if not (
            payload.provider_type == "fmu"
            and mapping.direction == "input"
            and input_sources.get(mapping.variable) in ("constant", "aggregate")
        )
    ]


def _aggregate_mapping_dicts(
    payload: SimulationModelPayload,
) -> list[dict[str, Any]]:
    """Mirrors _mapping_dicts: builds the persistence-ready dict shape from
    payload.aggregate_mappings, filtered to variables input_sources
    currently marks "aggregate" (same filter-at-serialization-boundary
    philosophy as the constant/point split above)."""
    if payload.provider_type != "fmu":
        return []
    parameters = payload.parameters if isinstance(payload.parameters, dict) else {}
    input_sources = parameters.get("input_sources") or {}
    return [
        {
            "variable": aggregate.variable,
            "direction": aggregate.direction,
            "operation": aggregate.operation,
            "point_ids": list(aggregate.point_ids),
            "weight_point_ids": (
                list(aggregate.weight_point_ids) if aggregate.weight_point_ids is not None else None
            ),
        }
        for aggregate in payload.aggregate_mappings
        if input_sources.get(aggregate.variable) == "aggregate"
    ]


def _input_exposure_dicts(
    payload: SimulationModelPayload,
) -> list[dict[str, Any]]:
    if payload.provider_type != "fmu":
        return []
    return [
        {"variable": exposure.variable, "point_id": exposure.point_id}
        for exposure in payload.input_exposures
    ]


def _normalized_parameters(
    database: Any,
    payload: SimulationModelPayload,
) -> dict[str, Any]:
    parameters = dict(payload.parameters or {})
    if payload.provider_type != "fmu":
        parameters.pop("input_sources", None)
        return parameters

    definition = _runtime_definition(database, payload.model_type)
    input_names = [
        variable.name
        for variable in definition.variables
        if variable.direction == "input"
    ]
    input_defaults = dict(parameters.get("input_defaults") or {})
    raw_sources = parameters.get("input_sources") or {}
    if raw_sources is not None and not isinstance(raw_sources, dict):
        raise HTTPException(
            status_code=422,
            detail="FMU input_sources parameter must be an object",
        )

    mapped_inputs = {
        mapping.variable
        for mapping in payload.mappings
        if mapping.direction == "input"
    }
    aggregate_mapped_inputs = {
        aggregate.variable for aggregate in payload.aggregate_mappings
    }
    input_sources: dict[str, str] = {}
    for name in input_names:
        explicit_source = raw_sources.get(name)
        if explicit_source is not None:
            if explicit_source not in ("constant", "point", "aggregate"):
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid FMU input source for {name!r}: {explicit_source!r}",
                )
            input_sources[name] = str(explicit_source)
        elif name in input_defaults and name not in mapped_inputs and name not in aggregate_mapped_inputs:
            input_sources[name] = "constant"
        elif name in aggregate_mapped_inputs:
            input_sources[name] = "aggregate"
        elif name in mapped_inputs:
            input_sources[name] = "point"
        else:
            input_sources[name] = "constant"

        if input_sources[name] == "constant" and name not in input_defaults:
            variable = next(v for v in definition.variables if v.name == name)
            input_defaults[name] = getattr(variable, "default", None)

    parameters["input_sources"] = input_sources
    parameters["input_defaults"] = {
        name: value
        for name, value in input_defaults.items()
        if input_sources.get(name) == "constant"
    }
    return parameters


def _format_point_label(point: dict[str, Any], device: dict[str, Any]) -> str:
    point_name = str(point.get("name") or f"Point {point['id']}")
    device_name = str(device.get("name") or f"Device {point['device_id']}")
    object_type = point.get("object_type")
    object_instance = point.get("object_instance")

    if object_type is not None and object_instance is not None:
        return (
            f"{device_name} / {point_name} "
            f"({object_type}:{object_instance}, id {point['id']})"
        )

    return f"{device_name} / {point_name} (id {point['id']})"


def _point_detail(point: dict[str, Any], device: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": point.get("id"),
        "name": point.get("name"),
        "device_id": point.get("device_id"),
        "device_name": device.get("name"),
        "object_type": point.get("object_type"),
        "object_instance": point.get("object_instance"),
    }


_AGGREGATE_OPERATION_LABELS = {"max": "Maximum", "weighted_average": "Weighted Average"}


def _validate_aggregate_source_point(
    database: Any,
    *,
    point_id: int,
    role: str,
    operation: str,
    variable_name: str,
    variable: Any,
) -> None:
    """Validates one aggregate source point (a value point, or a
    weighted_average weight point) against the database: exists, belongs to
    a simulated device, and is numeric (analog) -- the exact three checks
    the original Maximum-only validation loop already did for value points,
    now shared so weight points get the identical treatment rather than a
    second hand-copied block. `role` distinguishes the two in error
    messages ("value point" / "weight point") without changing the
    Maximum-only wording that existing tests already assert on."""
    point = database.get_object(point_id)
    if point is None:
        raise HTTPException(
            status_code=422,
            detail=f"Point {point_id} does not exist",
        )

    device = database.get_device(int(point["device_id"]))
    if device is None:
        raise HTTPException(
            status_code=422,
            detail=f"Point {point_id} belongs to a missing device",
        )

    op_label = _AGGREGATE_OPERATION_LABELS.get(operation, operation)
    aggregate_label = f"{op_label} aggregate" if role == "value point" else f"{op_label} aggregate weight point"

    if device.get("source_type", "simulated") != "simulated":
        point_label = _format_point_label(point, device)
        variable_label = _format_variable_label(variable)
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"{variable_label} ({aggregate_label}) is mapped to "
                    f"{point_label}, which belongs to an external "
                    "device. Simulation-model mappings currently "
                    "require simulated points."
                ),
                "variable": {
                    "name": variable_name,
                    "label": getattr(variable, "label", None),
                    "direction": "input",
                },
                "point": _point_detail(point, device),
            },
        )

    if point.get("object_type") in (BINARY_TYPES | MULTISTATE_TYPES):
        point_label = _format_point_label(point, device)
        variable_label = _format_variable_label(variable)
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"{variable_label} ({aggregate_label}) cannot use "
                    f"{point_label}: aggregate {role}s must be "
                    f"numeric (analog) points, not "
                    f"{point.get('object_type')!r}."
                ),
                "variable": {
                    "name": variable_name,
                    "label": getattr(variable, "label", None),
                    "direction": "input",
                },
                "point": _point_detail(point, device),
            },
        )


def _format_variable_label(variable: Any) -> str:
    label = getattr(variable, "label", None)
    name = getattr(variable, "name", "")
    direction = getattr(variable, "direction", "")

    if label and name:
        return f"{label} ({direction}:{name})"
    if label:
        return str(label)
    return f"{direction}:{name}"


def _validate_parameters(
    database: Any,
    payload: SimulationModelPayload,
) -> None:
    definition = _runtime_definition(database, payload.model_type)

    if definition.provider_type != payload.provider_type:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Model {payload.model_type!r} requires provider "
                f"{definition.provider_type!r}"
            ),
        )

    if payload.provider_type == "fmu":
        if not str(getattr(definition, "runtime_model", None) or "").strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    f"FMU runtime model is not registered for "
                    f"{payload.model_type!r}"
                ),
            )
        input_defaults = payload.parameters.get("input_defaults", {})
        if input_defaults is not None and not isinstance(input_defaults, dict):
            raise HTTPException(
                status_code=422,
                detail="FMU input_defaults parameter must be an object",
            )
        input_sources = payload.parameters.get("input_sources", {})
        if input_sources is not None and not isinstance(input_sources, dict):
            raise HTTPException(
                status_code=422,
                detail="FMU input_sources parameter must be an object",
            )


def _validate_mapping_contract(
    database: Any,
    payload: SimulationModelPayload,
    *,
    model_id: int | None = None,
) -> None:
    if payload.provider_type == "learned":
        return

    # A variable declared in BOTH raw lists is a structural conflict, not
    # the harmless single-list staleness the constant/point filters above
    # tolerate (that's the SAME list carrying a stale entry for a variable
    # now sourced differently). Two different top-level lists both
    # claiming a variable can only come from a UI bug or a hand-crafted
    # payload -- the frontend's own buildPayload never sends both for one
    # variable -- so this is rejected outright, checked before either list
    # gets filtered down to "effective" entries.
    point_mapped_inputs = {
        mapping.variable for mapping in payload.mappings if mapping.direction == "input"
    }
    aggregate_mapped = {aggregate.variable for aggregate in payload.aggregate_mappings}
    conflicting = sorted(point_mapped_inputs & aggregate_mapped)
    if conflicting:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Duplicate mapping for input:{conflicting[0]}: both a Point "
                "mapping and an Aggregate mapping were provided"
            ),
        )

    definition = _runtime_definition(database, payload.model_type)
    variable_defs = {
        (variable.name, variable.direction): variable
        for variable in definition.variables
    }

    seen: set[tuple[str, str]] = set()

    effective_mappings = _mapping_dicts(payload)

    for mapping in effective_mappings:
        key = (mapping["variable"], mapping["direction"])

        if key not in variable_defs:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{mapping['variable']!r} is not a declared "
                    f"{mapping['direction']} for model "
                    f"{payload.model_type!r}"
                ),
            )
        variable = variable_defs[key]

        if key in seen:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Duplicate mapping for "
                    f"{mapping['direction']}:{mapping['variable']}"
                ),
            )
        seen.add(key)

        point = database.get_object(mapping["point_id"])
        if point is None:
            raise HTTPException(
                status_code=422,
                detail=f"Point {mapping['point_id']} does not exist",
            )

        device = database.get_device(int(point["device_id"]))
        if device is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Point {mapping['point_id']} belongs to a missing device"
                ),
            )

        if device.get("source_type", "simulated") != "simulated":
            point_label = _format_point_label(point, device)
            variable_label = _format_variable_label(variable)
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        f"{variable_label} is mapped to {point_label}, "
                        "which belongs to an external device. "
                        "Simulation-model mappings currently require "
                        "simulated points."
                    ),
                    "variable": {
                        "name": mapping["variable"],
                        "label": getattr(variable, "label", None),
                        "direction": mapping["direction"],
                    },
                    "point": _point_detail(point, device),
                },
            )

        if mapping["direction"] == "output":
            owner = get_explicit_output_owner(
                database,
                mapping["point_id"],
                excluding_model_id=model_id,
            )
            if owner is not None:
                point_label = _format_point_label(point, device)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            f"{point_label} is already owned "
                            "by another simulation model"
                        ),
                        "point": _point_detail(point, device),
                        "owner": owner,
                    },
                )

    effective_aggregates = _aggregate_mapping_dicts(payload)

    for aggregate in effective_aggregates:
        key = (aggregate["variable"], "input")

        if key not in variable_defs:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{aggregate['variable']!r} is not a declared input for "
                    f"model {payload.model_type!r}"
                ),
            )
        variable = variable_defs[key]

        if key in seen:
            raise HTTPException(
                status_code=422,
                detail=f"Duplicate mapping for input:{aggregate['variable']}",
            )
        seen.add(key)

        weight_point_ids = aggregate.get("weight_point_ids") or [None] * len(aggregate["point_ids"])
        for point_id, weight_point_id in zip(aggregate["point_ids"], weight_point_ids):
            _validate_aggregate_source_point(
                database,
                point_id=point_id,
                role="value point",
                operation=aggregate["operation"],
                variable_name=aggregate["variable"],
                variable=variable,
            )
            # weight_point_id is None only for a non-weighted operation --
            # SimulationModelAggregateMappingPayload's own model_validator
            # already guarantees every value point has a real weight point
            # id for operation="weighted_average" before this code runs.
            if weight_point_id is not None:
                _validate_aggregate_source_point(
                    database,
                    point_id=weight_point_id,
                    role="weight point",
                    operation=aggregate["operation"],
                    variable_name=aggregate["variable"],
                    variable=variable,
                )
        # No output-ownership check -- aggregates are direction="input"
        # only (structurally enforced by SimulationModelAggregateMappingPayload).

    # Input exposures are deliberately NOT added to `seen` -- unlike a
    # plain/aggregate mapping, an exposure doesn't OWN a variable's
    # resolution, it just relays whatever already resolved it, so a
    # variable can be both, say, an Aggregate mapping AND exposed to a
    # point at the same time (that's the whole point of the feature).
    effective_exposures = _input_exposure_dicts(payload)
    seen_exposure_variables: set[str] = set()
    seen_exposure_points: set[int] = set()
    for exposure in effective_exposures:
        key = (exposure["variable"], "input")
        if key not in variable_defs:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{exposure['variable']!r} is not a declared input for "
                    f"model {payload.model_type!r}"
                ),
            )
        variable = variable_defs[key]

        if exposure["variable"] in seen_exposure_variables:
            raise HTTPException(
                status_code=422,
                detail=f"Duplicate input exposure for variable {exposure['variable']!r}",
            )
        seen_exposure_variables.add(exposure["variable"])

        if exposure["point_id"] in seen_exposure_points:
            raise HTTPException(
                status_code=422,
                detail=f"Point {exposure['point_id']} is targeted by more than one input exposure",
            )
        seen_exposure_points.add(exposure["point_id"])

        point = database.get_object(exposure["point_id"])
        if point is None:
            raise HTTPException(
                status_code=422,
                detail=f"Point {exposure['point_id']} does not exist",
            )

        device = database.get_device(int(point["device_id"]))
        if device is None:
            raise HTTPException(
                status_code=422,
                detail=f"Point {exposure['point_id']} belongs to a missing device",
            )

        if device.get("source_type", "simulated") != "simulated":
            point_label = _format_point_label(point, device)
            variable_label = _format_variable_label(variable)
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        f"{variable_label} cannot be exposed to {point_label}, "
                        "which belongs to an external device. Simulation-model "
                        "mappings currently require simulated points."
                    ),
                    "variable": {
                        "name": exposure["variable"],
                        "label": getattr(variable, "label", None),
                        "direction": "input",
                    },
                    "point": _point_detail(point, device),
                },
            )

        if point.get("object_type") in (BINARY_TYPES | MULTISTATE_TYPES):
            point_label = _format_point_label(point, device)
            variable_label = _format_variable_label(variable)
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        f"{variable_label} cannot be exposed to {point_label}: "
                        "an input exposure target must be a numeric (analog) "
                        f"point, not {point.get('object_type')!r}."
                    ),
                    "variable": {
                        "name": exposure["variable"],
                        "label": getattr(variable, "label", None),
                        "direction": "input",
                    },
                    "point": _point_detail(point, device),
                },
            )

        # Same "one writer per point" invariant the plain output-mapping
        # ownership check above enforces, extended across both tables: an
        # exposure point must not collide with another model's plain
        # output mapping, another model's exposure, or (within THIS
        # payload) this model's own output mapping.
        if exposure["point_id"] in {
            m["point_id"] for m in effective_mappings if m["direction"] == "output"
        }:
            point_label = _format_point_label(point, device)
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{point_label} cannot be both an output mapping and an "
                    "input exposure target in the same model"
                ),
            )

        output_owner = get_explicit_output_owner(
            database,
            exposure["point_id"],
            excluding_model_id=model_id,
        )
        if output_owner is not None:
            point_label = _format_point_label(point, device)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        f"{point_label} is already owned by another simulation model"
                    ),
                    "point": _point_detail(point, device),
                    "owner": output_owner,
                },
            )

        exposure_owner = get_explicit_exposure_owner(
            database,
            exposure["point_id"],
            excluding_model_id=model_id,
        )
        if exposure_owner is not None:
            point_label = _format_point_label(point, device)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        f"{point_label} is already an input-exposure target "
                        "of another simulation model"
                    ),
                    "point": _point_detail(point, device),
                    "owner": exposure_owner,
                },
            )

    required = {
        (variable.name, variable.direction)
        for variable in definition.variables
        if variable.required
    }
    if payload.provider_type == "fmu":
        input_defaults = payload.parameters.get("input_defaults") or {}
        for variable in definition.variables:
            if variable.direction == "input" and variable.name in input_defaults:
                required.discard((variable.name, variable.direction))
    missing = sorted(required - seen)

    # Disabled models are saved drafts: validate any mappings the caller did
    # provide, but do not require the full runtime contract until activation.
    if missing and payload.enabled:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Required model mappings are missing",
                "missing": [
                    {"variable": name, "direction": direction}
                    for name, direction in missing
                ],
            },
        )


def _validate_created_from_device(
    database: Any,
    device_id: int | None,
) -> None:
    if device_id is None:
        return

    device = database.get_device(device_id)
    if device is None:
        raise HTTPException(
            status_code=422,
            detail=f"Controller/device {device_id} does not exist",
        )


def _provider_catalog() -> list[dict[str, Any]]:
    return [
        {
            "provider_type": "builtin",
            "label": "Built-in",
            "available": True,
            "persistent_model_required": False,
            "description": (
                "Default per-point behavior provider. Automatically owns "
                "normal simulated points not claimed by another provider."
            ),
        },
        {
            "provider_type": "fmu",
            "label": "FMU",
            "available": True,
            "persistent_model_required": True,
            "description": (
                "FMU model driven through the configured runtime API. Inputs "
                "can use constants or BACnet point mappings; outputs write "
                "to mapped points."
            ),
        },
        {
            "provider_type": "learned",
            "label": "Learned Twin",
            "available": False,
            "persistent_model_required": True,
            "description": (
                "Learned-model runtime scaffold. Loading/training is not "
                "implemented yet."
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Existing simulation lifecycle API
# ---------------------------------------------------------------------------

@router.get("/health")
async def health(request: Request):
    database = get_database(request)
    engine = get_engine(request)

    devices = await asyncio.to_thread(database.get_devices)

    return {
        "status": "ok",
        "devices": len(devices),
        "bacnet_running": engine.app is not None,
        "sim_state": engine.clock_state,
        "elapsed_seconds": engine.state.elapsed_seconds,
        "providers": engine.get_simulation_providers(),
    }


@router.post("/sim/start")
async def start_simulation(request: Request):
    engine = get_engine(request)
    energy_engine = get_energy_engine(request)

    engine.resume()
    if energy_engine is not None:
        energy_engine.resume()

    return {
        "sim_state": engine.clock_state,
        "providers": engine.get_simulation_providers(),
    }


@router.post("/sim/pause")
async def pause_simulation(request: Request):
    engine = get_engine(request)
    energy_engine = get_energy_engine(request)

    engine.pause()
    if energy_engine is not None:
        energy_engine.pause()

    return {
        "sim_state": engine.clock_state,
        "providers": engine.get_simulation_providers(),
    }


@router.post("/sim/stop")
async def stop_simulation(request: Request):
    engine = get_engine(request)
    energy_engine = get_energy_engine(request)

    engine.reset()
    if energy_engine is not None:
        energy_engine.reset()

    return {
        "sim_state": engine.clock_state,
        "elapsed_seconds": engine.state.elapsed_seconds,
        "providers": engine.get_simulation_providers(),
    }


@router.get("/state")
async def get_simulation_state(request: Request):
    engine = get_engine(request)
    state = engine.get_state()

    # Add provider diagnostics without changing SimEngine.get_state() shape.
    if isinstance(state, dict):
        state = dict(state)
        state["providers"] = engine.get_simulation_providers()

    return state


@router.post("/reload")
async def reload_simulation(request: Request):
    database = get_database(request)
    engine = get_engine(request)

    async def _reload_and_reconcile() -> None:
        await engine.reload()
        await asyncio.to_thread(
            ensure_simulation_model_schema,
            database,
        )
        reconcile_enabled_models(database, engine)

    asyncio.create_task(_reload_and_reconcile())

    return {
        "ok": True,
        "message": "Reload and model reconciliation scheduled",
    }


# ---------------------------------------------------------------------------
# Provider inspection API
# ---------------------------------------------------------------------------

@router.get("/simulation/providers/catalog")
async def get_provider_catalog():
    return _provider_catalog()


@router.get("/simulation/providers/runtime")
async def get_runtime_providers(request: Request):
    engine = get_engine(request)
    return engine.get_simulation_providers()


# ---------------------------------------------------------------------------
# Model catalog API - drives the generic Add Model GUI
# ---------------------------------------------------------------------------

@router.get("/simulation/models/catalog")
async def simulation_model_catalog(request: Request):
    database = get_database(request)
    try:
        return await asyncio.to_thread(
            get_remote_model_catalog,
            database.get_settings(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "FMU model runtime cannot be reached",
                "runtime_error": str(exc),
            },
        ) from exc


# ---------------------------------------------------------------------------
# Auto Map: deterministic + AI-assisted point-mapping suggestions
# ---------------------------------------------------------------------------

@router.post("/simulation/models/mapping-suggestions")
async def simulation_mapping_suggestions(
    payload: MappingSuggestionsRequest,
    request: Request,
):
    """Read-only: ranks existing BACnet points against every input/output
    variable of the requested model type. Never mutates the database --
    the Simulation Model drawer's own Create/Save flow is what persists a
    mapping, once the user reviews and applies these suggestions."""
    database = get_database(request)

    definition = _runtime_definition(database, payload.model_type)

    suggestions = await asyncio.to_thread(
        suggest_mappings_for_model,
        definition,
        payload.created_from_device_id,
        database,
        excluding_model_id=payload.current_model_id,
    )

    return {"variables": [asdict(s) for s in suggestions]}


@router.post("/simulation/models/mapping-suggestions/ai")
async def simulation_mapping_suggestion_ai(
    payload: MappingSuggestionAiRequest,
    request: Request,
):
    """AI fallback for ONE variable, triggered only by an explicit per-row
    "Use AI" click -- used when the deterministic engine above returned
    low/none confidence for it. The Azure client is constructed inside the
    request, not at import time, so a missing/misconfigured Azure setup
    only fails this one call."""
    database = get_database(request)

    definition = _runtime_definition(database, payload.model_type)

    variable = next(
        (v for v in definition.variables if v.name == payload.variable),
        None,
    )
    if variable is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{payload.variable!r} is not a declared variable for "
                f"model {payload.model_type!r}"
            ),
        )

    candidates, scope_used, related_name = await asyncio.to_thread(
        build_shortlist,
        variable,
        payload.created_from_device_id,
        database,
        excluding_model_id=payload.current_model_id,
    )

    equipment_context = None
    if payload.created_from_device_id is not None:
        device = await asyncio.to_thread(
            database.get_device, payload.created_from_device_id,
        )
        if device is not None:
            equipment_context = device.get("equipment_type")

    relationship_context = relationship_context_string(variable, scope_used, related_name)

    try:
        client = AzureStructuredClient()
        result = await asyncio.to_thread(
            suggest_point_for_variable_via_ai,
            client,
            variable=variable,
            model_definition=definition,
            equipment_context=equipment_context,
            relationship_context=relationship_context,
            candidates=candidates,
        )
    except Exception:
        log.warning(
            "AI mapping suggestion failed for model=%s variable=%s",
            payload.model_type, payload.variable, exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail="AI suggestion is unavailable. Check Azure OpenAI configuration.",
        )

    # Authoritative shortlist check -- the model's response schema does
    # NOT constrain this on its own; a rejected/hallucinated point_id
    # collapses to no suggestion rather than being passed through.
    candidate_ids = {c.id for c in candidates}
    point_id = result.point_id
    if point_id is not None and point_id not in candidate_ids:
        point_id = None
    confidence = result.confidence if point_id is not None else "none"

    point_name = None
    if point_id is not None:
        match = next((c for c in candidates if c.id == point_id), None)
        if match is not None:
            point_name = f"{match.device_name} / {match.name}"

    return {
        "variable": variable.name,
        "direction": variable.direction,
        "suggested_point_id": point_id,
        "suggested_point_name": point_name,
        "confidence": confidence,
        "score": 0.0,
        "reasons": [result.reason],
        "alternatives": [],
        "source": "ai",
        "equipment_scope_used": scope_used,
        "related_equipment_name": related_name,
    }


@router.post("/simulation/models/variable-candidates")
async def simulation_variable_candidates(
    payload: VariableCandidatesRequest,
    request: Request,
):
    """Read-only ranked shortlist for ONE variable, reusing the exact same
    discovery/scoring path as Auto Map (mapping_suggestions.build_shortlist)
    -- used by the Aggregate multi-select picker to sort compatible points
    by mapping_hints/suggested_point_types relevance instead of duplicating
    that scoring logic client-side (the equipment_scope="downstream"
    topology traversal needs server-side access to the semantic-relationship
    graph, which the frontend has no client-side equivalent for). Never
    mutates anything."""
    database = get_database(request)

    definition = _runtime_definition(database, payload.model_type)

    variable = next(
        (v for v in definition.variables if v.name == payload.variable),
        None,
    )
    if variable is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{payload.variable!r} is not a declared variable for "
                f"model {payload.model_type!r}"
            ),
        )

    candidates, scope_used, related_name = await asyncio.to_thread(
        build_shortlist,
        variable,
        payload.created_from_device_id,
        database,
        limit=payload.limit,
        excluding_model_id=payload.current_model_id,
    )

    return {
        "variable": variable.name,
        "candidates": [asdict(c) for c in candidates],
        "equipment_scope_used": scope_used,
        "related_equipment_name": related_name,
    }


def _list_simulation_point_options(database: Any) -> list[dict[str, Any]]:
    with database._conn() as conn:
        rows = conn.execute(
            """
            SELECT
                o.id,
                o.name,
                o.device_id,
                o.object_type,
                o.object_instance,
                o.units,
                o.behavior,
                o.behavior_params,
                o.manual_value,
                o.point_type,
                d.name AS device_name
            FROM objects o
            JOIN devices d ON d.id = o.device_id
            ORDER BY d.name COLLATE NOCASE, o.name COLLATE NOCASE, o.id
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            configured_value = item.get("manual_value")
            if configured_value is None:
                try:
                    configured_value = json.loads(
                        item.get("behavior_params") or "{}"
                    ).get("value")
                except (TypeError, json.JSONDecodeError):
                    configured_value = None
            item["configured_value"] = configured_value
            result.append(item)
        return result


@router.get("/simulation/points/options")
async def simulation_point_options(request: Request):
    database = get_database(request)
    return await asyncio.to_thread(_list_simulation_point_options, database)


# ---------------------------------------------------------------------------
# Persistent model CRUD
# ---------------------------------------------------------------------------

@router.get("/simulation/models")
async def get_simulation_models(
    request: Request,
    created_from_device_id: int | None = Query(default=None),
):
    database = get_database(request)

    return await asyncio.to_thread(
        list_simulation_models,
        database,
        created_from_device_id=created_from_device_id,
    )


@router.get("/simulation/models/{model_id}")
async def get_simulation_model_by_id(
    model_id: int,
    request: Request,
):
    database = get_database(request)

    model = await asyncio.to_thread(
        get_simulation_model,
        database,
        model_id,
    )

    if model is None:
        raise HTTPException(
            status_code=404,
            detail="Simulation model not found",
        )

    return model


@router.post("/simulation/models", status_code=201)
async def add_simulation_model(
    payload: SimulationModelPayload,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    await asyncio.to_thread(
        ensure_simulation_model_schema,
        database,
    )

    _validate_created_from_device(
        database,
        payload.created_from_device_id,
    )
    _validate_parameters(database, payload)
    payload.parameters = _normalized_parameters(database, payload)
    _validate_mapping_contract(database, payload)

    try:
        model = await asyncio.to_thread(
            create_simulation_model,
            database,
            name=payload.name,
            provider_type=payload.provider_type,
            model_type=normalize_remote_model_id(payload.model_type),
            enabled=payload.enabled,
            parameters=payload.parameters,
            created_from_device_id=payload.created_from_device_id,
            mappings=_mapping_dicts(payload),
            aggregate_mappings=_aggregate_mapping_dicts(payload),
            input_exposures=_input_exposure_dicts(payload),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "Simulation model conflicts with an existing mapping. "
                "An output point can have only one explicit model owner."
            ),
        ) from exc

    if model["enabled"]:
        try:
            reload_model(database, engine, int(model["id"]))
        except Exception as exc:
            # Persisted config is retained so the user can correct it.
            log.exception(
                "Simulation model activation failed: model_id=%s name=%s "
                "provider_type=%s model_type=%s",
                model["id"],
                model.get("name"),
                model.get("provider_type"),
                model.get("model_type"),
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        "Model was saved but could not be activated"
                    ),
                    "model": model,
                    "runtime_error": str(exc),
                },
            ) from exc

    model["runtime_id"] = provider_runtime_id(model)
    model["runtime"] = engine.get_simulation_providers().get(
        model["runtime_id"]
    )

    return model


@router.put("/simulation/models/{model_id}")
async def edit_simulation_model(
    model_id: int,
    payload: SimulationModelPayload,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    existing = await asyncio.to_thread(
        get_simulation_model,
        database,
        model_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Simulation model not found",
        )

    _validate_created_from_device(
        database,
        payload.created_from_device_id,
    )
    _validate_parameters(database, payload)
    payload.parameters = _normalized_parameters(database, payload)
    _validate_mapping_contract(
        database,
        payload,
        model_id=model_id,
    )

    old_runtime_id = provider_runtime_id(existing)

    try:
        model = await asyncio.to_thread(
            update_simulation_model,
            database,
            model_id,
            name=payload.name,
            provider_type=payload.provider_type,
            model_type=normalize_remote_model_id(payload.model_type),
            enabled=payload.enabled,
            parameters=payload.parameters,
            created_from_device_id=payload.created_from_device_id,
            mappings=_mapping_dicts(payload),
            aggregate_mappings=_aggregate_mapping_dicts(payload),
            input_exposures=_input_exposure_dicts(payload),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "Simulation model conflicts with an existing mapping. "
                "An output point can have only one explicit model owner."
            ),
        ) from exc

    if model is None:
        raise HTTPException(
            status_code=404,
            detail="Simulation model not found",
        )

    # Provider/model type can change, so explicitly release the old key.
    engine.unregister_simulation_provider(old_runtime_id)

    if model["enabled"]:
        try:
            reload_model(database, engine, model_id)
        except Exception as exc:
            log.exception(
                "Simulation model activation failed: model_id=%s name=%s "
                "provider_type=%s model_type=%s",
                model["id"],
                model.get("name"),
                model.get("provider_type"),
                model.get("model_type"),
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "message": (
                        "Model was updated but could not be activated"
                    ),
                    "model": model,
                    "runtime_error": str(exc),
                },
            ) from exc

    model["runtime_id"] = provider_runtime_id(model)
    model["runtime"] = engine.get_simulation_providers().get(
        model["runtime_id"]
    )

    return model


@router.delete("/simulation/models/{model_id}")
async def remove_simulation_model(
    model_id: int,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    model = await asyncio.to_thread(
        get_simulation_model,
        database,
        model_id,
    )
    if model is None:
        raise HTTPException(
            status_code=404,
            detail="Simulation model not found",
        )

    runtime_id = provider_runtime_id(model)
    engine.unregister_simulation_provider(runtime_id)

    deleted = await asyncio.to_thread(
        delete_simulation_model,
        database,
        model_id,
    )

    return {
        "ok": deleted,
        "model_id": model_id,
        "runtime_id": runtime_id,
    }


# ---------------------------------------------------------------------------
# Explicit runtime actions
# ---------------------------------------------------------------------------

@router.post("/simulation/models/{model_id}/reload")
async def reload_simulation_model(
    model_id: int,
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    try:
        model = await asyncio.to_thread(
            get_simulation_model,
            database,
            model_id,
        )
        if model is None:
            raise HTTPException(
                status_code=404,
                detail="Simulation model not found",
            )

        reload_model(database, engine, model_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    runtime_id = provider_runtime_id(model)

    return {
        "ok": True,
        "model_id": model_id,
        "runtime_id": runtime_id,
        "runtime": engine.get_simulation_providers().get(runtime_id),
    }


@router.post("/simulation/models/reconcile")
async def reconcile_simulation_model_runtime(
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    result = await asyncio.to_thread(
        reconcile_enabled_models,
        database,
        engine,
    )

    result["providers"] = engine.get_simulation_providers()
    return result


# ---------------------------------------------------------------------------
# Startup hook called by application lifespan
# ---------------------------------------------------------------------------

async def bootstrap_simulation_models(app: Any) -> dict[str, Any]:
    """
    Call once AFTER `await engine.start()` in the application lifespan.

    This is intentionally exported from the router module so the current
    legacy.py lifespan can adopt persistence without putting model-specific
    VAV/Chiller construction back into legacy.py.
    """
    database = getattr(app.state, "db", None)
    engine = getattr(app.state, "engine", None)

    if database is None or engine is None:
        return {
            "loaded": [],
            "removed": [],
            "errors": [{
                "error": "Database or simulation engine is unavailable"
            }],
        }

    await asyncio.to_thread(
        ensure_simulation_model_schema,
        database,
    )

    return await asyncio.to_thread(
        reconcile_enabled_models,
        database,
        engine,
    )
