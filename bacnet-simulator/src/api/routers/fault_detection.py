from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/fault-detection", tags=["fault-detection"])


def get_fault_engine(request: Request) -> Any:
    engine = getattr(request.app.state, "fault_detection_engine", None)
    if engine is None:
        raise HTTPException(503, "Fault detection engine is unavailable")
    return engine


@router.get("/rules")
async def list_rules(request: Request):
    engine = get_fault_engine(request)
    return [{
        "rule_id": rule.definition.rule_id,
        "name": rule.definition.name,
        "equipment_type": rule.definition.equipment_type,
        "description": rule.definition.description,
        "persistence_seconds": rule.definition.persistence_seconds,
        "clear_seconds": rule.definition.clear_seconds,
        "severity": rule.definition.severity.value,
    } for rule in engine.registry.all()]


@router.post("/devices/{device_id}/evaluate")
async def evaluate_device(device_id: int, request: Request):
    engine = get_fault_engine(request)
    results = await engine.evaluate_device(device_id)
    return [{
        "device_id": item.device_id,
        "rule_id": item.rule_id,
        "state": item.state.value,
        "previous_state": item.previous_state.value,
        "message": item.message,
        "severity": item.severity.value,
        "timestamp": item.timestamp,
        "activated_at": item.activated_at,
        "cleared_at": item.cleared_at,
        "evidence": [e.__dict__ for e in item.evidence],
    } for item in results]
