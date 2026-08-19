from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import (
    FunctionalTestCreate,
    FunctionalTestUpdate,
)
from ...functional_tests import runs as ft_runs
from ...functional_tests.readiness import check_readiness


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
    request: Request,
):
    """Read-only preview: checks every point the saved definition
    references still exists (and, for Set nodes, is simulated), powering
    the pre-flight readiness UI. Never creates a run. Bodyless -- every
    point in the definition already carries its own device, so there is no
    per-run target to supply."""
    database = get_database(request)

    test = await asyncio.to_thread(database.get_functional_test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Functional test not found")

    readiness = await asyncio.to_thread(check_readiness, database, test["definition"])

    return {"points": [r.to_dict() for r in readiness]}


@router.post("/{test_id}/runs", status_code=201)
async def create_functional_test_run(
    test_id: int,
    request: Request,
):
    """Bodyless -- the definition is always loaded fresh from the DB
    (never from the request body), and readiness is always re-checked
    server-side (never trusts whatever the frontend's own /resolve preview
    already showed)."""
    database = get_database(request)

    test = await asyncio.to_thread(database.get_functional_test, test_id)
    if test is None:
        raise HTTPException(status_code=404, detail="Functional test not found")

    try:
        run_row, point_cache = await asyncio.to_thread(ft_runs.prepare_run, database, test)
    except ft_runs.ReadinessError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Cannot run test -- one or more required points are not ready",
                "points": [r.to_dict() for r in exc.readiness],
            },
        )
    except ft_runs.ActiveRunExistsError:
        raise HTTPException(
            status_code=409,
            detail="A run for this test is already active",
        )

    ft_runs.start_execution(
        request.app.state, database, run_row, test["name"], test["definition"], point_cache,
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
