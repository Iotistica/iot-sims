from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ...simulation.model_runtime import (
    provider_runtime_id,
    reconcile_enabled_models,
    reload_model,
)
from ...simulation.model_store import (
    create_simulation_model,
    delete_simulation_model,
    ensure_simulation_model_schema,
    get_explicit_output_owner,
    get_simulation_model,
    list_simulation_models,
    update_simulation_model,
)
from ...simulation.models.registry import (
    MODEL_REGISTRY,
    get_model_catalog,
    get_model_definition,
)

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


class SimulationModelPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    provider_type: Literal["system", "fmu", "learned"] = "system"
    model_type: str = Field(min_length=1)
    enabled: bool = True
    parameters: dict[str, Any] = Field(default_factory=dict)

    # Context for "right-click Controller -> Add Model" only.
    # Runtime participants are derived from mapped points.
    created_from_device_id: int | None = None

    mappings: list[SimulationModelMappingPayload] = Field(
        default_factory=list
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _mapping_dicts(
    payload: SimulationModelPayload,
) -> list[dict[str, Any]]:
    return [
        {
            "variable": mapping.variable,
            "direction": mapping.direction,
            "point_id": mapping.point_id,
        }
        for mapping in payload.mappings
    ]


def _validate_parameters(
    payload: SimulationModelPayload,
) -> None:
    if payload.provider_type != "system":
        # FMU/Learned payload persistence is reserved for future provider
        # implementations. They are not executable yet.
        return

    try:
        definition = get_model_definition(payload.model_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if definition.provider_type != payload.provider_type:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Model {payload.model_type!r} requires provider "
                f"{definition.provider_type!r}"
            ),
        )

    try:
        # Construct once during validation so dataclass parameter-name/type
        # errors are returned before persistence.
        model = definition.factory(dict(payload.parameters))
        validation = model.validate()
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid model parameters: {exc}",
        ) from exc

    if not validation.valid:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Model parameters are invalid",
                "errors": validation.errors,
                "warnings": validation.warnings,
            },
        )


def _validate_mapping_contract(
    database: Any,
    payload: SimulationModelPayload,
    *,
    model_id: int | None = None,
) -> None:
    if payload.provider_type != "system":
        return

    definition = get_model_definition(payload.model_type)
    variable_defs = {
        (variable.name, variable.direction): variable
        for variable in definition.variables
    }

    seen: set[tuple[str, str]] = set()

    for mapping in payload.mappings:
        key = (mapping.variable, mapping.direction)

        if key not in variable_defs:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{mapping.variable!r} is not a declared "
                    f"{mapping.direction} for model "
                    f"{payload.model_type!r}"
                ),
            )

        if key in seen:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Duplicate mapping for "
                    f"{mapping.direction}:{mapping.variable}"
                ),
            )
        seen.add(key)

        point = database.get_object(mapping.point_id)
        if point is None:
            raise HTTPException(
                status_code=422,
                detail=f"Point {mapping.point_id} does not exist",
            )

        device = database.get_device(int(point["device_id"]))
        if device is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Point {mapping.point_id} belongs to a missing device"
                ),
            )

        if device.get("source_type", "simulated") != "simulated":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Point {mapping.point_id} belongs to an external "
                    "device. Simulation-model mappings currently require "
                    "simulated points."
                ),
            )

        if mapping.direction == "output":
            owner = get_explicit_output_owner(
                database,
                mapping.point_id,
                excluding_model_id=model_id,
            )
            if owner is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            f"Point {mapping.point_id} is already owned "
                            "by another simulation model"
                        ),
                        "owner": owner,
                    },
                )

    required = {
        (variable.name, variable.direction)
        for variable in definition.variables
        if variable.required
    }
    missing = sorted(required - seen)

    if missing:
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
            "provider_type": "system",
            "label": "System Model",
            "available": True,
            "persistent_model_required": True,
            "description": (
                "Iotistica HVAC models."
            ),
        },
        {
            "provider_type": "fmu",
            "label": "FMU",
            "available": False,
            "persistent_model_required": True,
            "description": (
                "FMI/FMU runtime scaffold. Model loading is not implemented yet."
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
async def simulation_model_catalog():
    return get_model_catalog()


def _list_simulation_point_options(database: Any) -> list[dict[str, Any]]:
    with database._conn() as conn:
        rows = conn.execute(
            """
            SELECT
                o.id,
                o.name,
                o.device_id,
                d.name AS device_name
            FROM objects o
            JOIN devices d ON d.id = o.device_id
            ORDER BY d.name COLLATE NOCASE, o.name COLLATE NOCASE, o.id
            """
        ).fetchall()
        return [dict(row) for row in rows]


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
    _validate_parameters(payload)
    _validate_mapping_contract(database, payload)

    try:
        model = await asyncio.to_thread(
            create_simulation_model,
            database,
            name=payload.name,
            provider_type=payload.provider_type,
            model_type=payload.model_type,
            enabled=payload.enabled,
            parameters=payload.parameters,
            created_from_device_id=payload.created_from_device_id,
            mappings=_mapping_dicts(payload),
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
    _validate_parameters(payload)
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
            model_type=payload.model_type,
            enabled=payload.enabled,
            parameters=payload.parameters,
            created_from_device_id=payload.created_from_device_id,
            mappings=_mapping_dicts(payload),
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
