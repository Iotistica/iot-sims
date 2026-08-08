from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(
    prefix="/commissioning",
    tags=["commissioning"],
)

_engine: Any | None = None


def configure_commissioning_router(engine: Any) -> None:
    global _engine
    _engine = engine


@router.get("/tests")
async def list_tests() -> list[dict]:
    return [
        {
            "id": "ahu-baseline",
            "name": "AHU Baseline",
            "equipment_type": "Air_Handling_Unit",
            "mode": "read-only",
            "description": "Verifies required AHU semantic points and live values.",
        }
    ]


@router.post("/devices/{device_id}/tests/{test_id}")
async def run_test(device_id: int, test_id: str) -> dict:
    if _engine is None:
        raise HTTPException(503, "Commissioning engine unavailable")

    try:
        return await _engine.run_test(
            device_id=device_id,
            test_id=test_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/results")
async def get_results() -> list[dict]:
    if _engine is None:
        return []
    return _engine.get_results()


@router.get("/devices/{device_id}/tests/{test_id}/result")
async def get_result(device_id: int, test_id: str) -> dict:
    if _engine is None:
        raise HTTPException(503, "Commissioning engine unavailable")

    result = _engine.get_result(
        device_id=device_id,
        test_id=test_id,
    )
    if result is None:
        raise HTTPException(404, "Commissioning result not found")
    return result


@router.post("/reset")
async def reset_results() -> dict:
    if _engine is not None:
        _engine.reset()
    return {"ok": True}
