from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ...functional_tests import runs as ft_runs


router = APIRouter(
    prefix="/functional-test-runs",
    tags=["functional-test-runs"],
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


@router.get("/{run_id}")
async def get_functional_test_run(
    run_id: int,
    request: Request,
):
    database = get_database(request)

    run = await asyncio.to_thread(database.get_functional_test_run, run_id)

    if run is None:
        raise HTTPException(
            status_code=404,
            detail="Functional test run not found",
        )

    return run


@router.post("/{run_id}/cancel")
async def cancel_functional_test_run(
    run_id: int,
    request: Request,
):
    database = get_database(request)

    run = await asyncio.to_thread(database.get_functional_test_run, run_id)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail="Functional test run not found",
        )

    ft_runs.cancel_run(request.app.state, run_id)

    # The executor observes the cancel flag between steps (and inside its
    # wait loops on ~1s ticks) rather than being force-killed -- state may
    # still read "running" for a moment after this call returns; the
    # frontend keeps polling GET .../{run_id} until it settles to
    # "cancelled".
    return await asyncio.to_thread(database.get_functional_test_run, run_id)
