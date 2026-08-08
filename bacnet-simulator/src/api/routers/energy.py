from __future__ import annotations
import time
import json
import asyncio
from typing import Any
from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/energy", tags=["energy"])


def get_energy_engine(request: Request) -> Any:
    engine = getattr(request.app.state, "energy_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="Energy engine is unavailable")
    return engine

@router.get("/history")
async def get_energy_history(
    request: Request,
    hours: float = Query(
        default=24.0,
        gt=0,
        le=168,
    ),
    device_id: int | None = None,
    model_type: str | None = None,
):
    db = request.app.state.db

    end_timestamp = time.time()
    start_timestamp = (
        end_timestamp - hours * 60 * 60
    )

    rows = await asyncio.to_thread(
        db.get_energy_history,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        device_id=device_id,
        model_type=model_type,
    )

    output: list[dict] = []

    for row in rows:
        try:
            metrics = json.loads(
                row.get("metrics") or "{}"
            )
        except (TypeError, ValueError):
            metrics = {}

        output.append(
            {
                **row,
                "metrics": metrics,
            }
        )

    return output

@router.get("/equipment")
async def list_energy_results(request: Request):
    return get_energy_engine(request).get_latest()


@router.post("/evaluate")
async def evaluate_energy(request: Request, elapsed_seconds: float = Query(default=5.0, gt=0, le=3600)):
    return await get_energy_engine(request).evaluate_all(elapsed_seconds=elapsed_seconds)
