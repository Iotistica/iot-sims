from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import (
    FunctionalTestCreate,
    FunctionalTestRunRequest,
    FunctionalTestUpdate,
)
from ...functional_tests import runs as ft_runs
from ...functional_tests.resolution import build_resolution, collect_point_types


router = APIRouter(
    prefix="/functional-tests",
    tags=["functional-tests"],
)


def get_database(request: Request) -> Any:
    database = getattr(
        request.app.state,
        "db",
        None,
    )

    if database is None:
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable",
        )

    return database


async def _require_target_device(database: Any, device_id: int) -> dict:
    device = await asyncio.to_thread(database.get_device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Target device not found")
    return device


@router.get("")
async def list_functional_tests(
    request: Request,
):
    database = get_database(request)

    return await asyncio.to_thread(
        database.get_functional_tests
    )


@router.post(
    "",
    status_code=201,
)
async def create_functional_test(
    body: FunctionalTestCreate,
    request: Request,
):
    database = get_database(request)

    body.validate_semantic()
    body.validate_definition()

    return await asyncio.to_thread(
        database.create_functional_test,
        body.model_dump(),
    )


@router.get("/{test_id}")
async def get_functional_test(
    test_id: int,
    request: Request,
):
    database = get_database(request)

    test = await asyncio.to_thread(
        database.get_functional_test,
        test_id,
    )

    if test is None:
        raise HTTPException(
            status_code=404,
            detail="Functional test not found",
        )

    return test


@router.post("/{test_id}/resolve")
async def resolve_functional_test(
    test_id: int,
    body: FunctionalTestRunRequest,
    request: Request,
):
    """Read-only preview: resolves every semantic point the saved
    definition references against a candidate target, powering the
    pre-flight readiness UI. Never creates a run."""
    database = get_database(request)

    test = await asyncio.to_thread(database.get_functional_test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Functional test not found")

    device = await _require_target_device(database, body.target_device_id)

    def _resolve() -> dict:
        point_types = collect_point_types(test["definition"])
        return build_resolution(database, device["id"], test["equipment_type"], point_types)

    resolution = await asyncio.to_thread(_resolve)
    execution_mode = "external" if device.get("source_type") == "external-bacnet" else "simulation"

    return {
        "execution_mode": execution_mode,
        "points": {point_type: r.to_dict() for point_type, r in resolution.items()},
    }


@router.post("/{test_id}/runs", status_code=201)
async def create_functional_test_run(
    test_id: int,
    body: FunctionalTestRunRequest,
    request: Request,
):
    """Only ever accepts a target_device_id -- the definition is always
    loaded fresh from the DB (never from the request body), and semantic
    resolution is always re-done server-side (never trusts whatever the
    frontend's own /resolve preview already showed)."""
    database = get_database(request)

    test = await asyncio.to_thread(database.get_functional_test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Functional test not found")

    device = await _require_target_device(database, body.target_device_id)

    try:
        run_row, resolution_map = await asyncio.to_thread(ft_runs.prepare_run, database, test, device)
    except ft_runs.ResolutionError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Cannot run test -- one or more required points did not resolve",
                "points": {point_type: r.to_dict() for point_type, r in exc.resolution.items()},
            },
        )
    except ft_runs.ActiveRunExistsError:
        raise HTTPException(
            status_code=409,
            detail="A run for this test and target is already active",
        )

    ft_runs.start_execution(
        request.app.state, database, run_row, device,
        run_row["execution_mode"], test["definition"], resolution_map,
    )

    return run_row


@router.put("/{test_id}")
async def update_functional_test(
    test_id: int,
    body: FunctionalTestUpdate,
    request: Request,
):
    database = get_database(request)

    body.validate_semantic()
    body.validate_definition()

    existing = await asyncio.to_thread(
        database.get_functional_test,
        test_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Functional test not found",
        )

    updated = await asyncio.to_thread(
        database.update_functional_test,
        test_id,
        body.model_dump(),
    )

    return updated


@router.delete(
    "/{test_id}",
    status_code=204,
)
async def delete_functional_test(
    test_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_functional_test,
        test_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Functional test not found",
        )

    await asyncio.to_thread(
        database.delete_functional_test,
        test_id,
    )

    return Response(status_code=204)
