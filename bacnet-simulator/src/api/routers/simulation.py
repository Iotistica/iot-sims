from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request


router = APIRouter(tags=["simulation"])


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


def get_engine(request: Request) -> Any:
    engine = getattr(
        request.app.state,
        "engine",
        None,
    )

    if engine is None:
        raise HTTPException(
            status_code=503,
            detail="Simulation engine is unavailable",
        )

    return engine


@router.get("/health")
async def health(
    request: Request,
):
    database = get_database(request)
    engine = get_engine(request)

    devices = await asyncio.to_thread(
        database.get_devices
    )

    return {
        "status": "ok",
        "devices": len(devices),
        "bacnet_running": engine.app is not None,
        "sim_state": engine.clock_state,
        "elapsed_seconds": (
            engine.state.elapsed_seconds
        ),
    }


@router.post("/sim/start")
async def start_simulation(
    request: Request,
):
    engine = request.app.state.engine
    energy_engine = request.app.state.energy_engine

    engine.resume()
    energy_engine.resume()

    return {
        "sim_state": engine.clock_state,
    }


@router.post("/sim/pause")
async def pause_simulation(
    request: Request,
):
    engine = request.app.state.engine
    energy_engine = request.app.state.energy_engine
    
    engine.pause()
    energy_engine.pause()

    return {
        "sim_state": engine.clock_state,
    }


@router.post("/sim/stop")
async def stop_simulation(
    request: Request,
):
    engine = request.app.state.engine
    energy_engine = request.app.state.energy_engine

    engine.reset()
    energy_engine.reset()

    return {
        "sim_state": engine.clock_state,
        "elapsed_seconds": (
            engine.state.elapsed_seconds
        ),
    }


@router.get("/state")
async def get_simulation_state(
    request: Request,
):
    engine = get_engine(request)

    return engine.get_state()


@router.post("/reload")
async def reload_simulation(
    request: Request,
):
    engine = get_engine(request)

    asyncio.create_task(
        engine.reload()
    )

    return {
        "ok": True,
        "message": "Reload scheduled",
    }