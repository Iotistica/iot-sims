from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ...core.config import POINT_TYPES
from ...integrations.azure_openai import AzureStructuredClient
from ...semantics.ai_suggestions import suggest_point_via_ai
from ...semantics.suggestions import (
    DeviceSnapshot,
    PointSnapshot,
    SemanticSuggestion,
    suggest_device_semantics,
    suggest_equipment,
)

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/devices/{device_id}/semantic-suggestions",
    tags=["semantic-suggestions"],
)


def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return database


def _to_snapshot(device: dict, objects: list[dict]) -> DeviceSnapshot:
    """Thin adapter from persisted Device/SimObject rows to the scorer's
    own small input shape -- works identically for source_type='simulated'
    or 'external-bacnet', nothing here branches on it."""
    return DeviceSnapshot(
        device_instance=device["device_instance"],
        name=device["name"],
        vendor_name=device.get("vendor_name"),
        model_name=device.get("model_name"),
        description=device.get("description"),
        points=[
            PointSnapshot(
                object_type=obj["object_type"],
                object_instance=obj["object_instance"],
                object_name=obj["name"],
                units=obj.get("units"),
                description=obj.get("description"),
            )
            for obj in objects
        ],
    )


def _to_response_entry(suggestion: SemanticSuggestion, *, source_id: int, existing_class: str | None) -> dict:
    """Suppresses a competing suggested_class whenever the record is
    already classified -- existing user-entered semantics are always
    authoritative and must never be silently replaced by a new suggestion
    (see requirement 20)."""
    suggested_class = suggestion.suggested_class if existing_class is None else None
    confidence = suggestion.confidence if existing_class is None else "none"
    return {
        "source_kind": suggestion.source_kind,
        "source_id": source_id,
        "source_name": suggestion.source_name,
        "suggested_class": suggested_class,
        "existing_class": existing_class,
        "confidence": confidence,
        "score": suggestion.score if existing_class is None else 0.0,
        "reasons": suggestion.reasons if existing_class is None else [],
        "alternatives": [
            {"brick_class": c.brick_class, "score": c.score, "reasons": c.reasons}
            for c in suggestion.alternatives
        ] if existing_class is None else [],
        "source": "rule",
    }


@router.post("")
async def suggest_semantics(device_id: int, request: Request):
    """
    Generates deterministic Brick classification suggestions for this
    device and its objects -- pure read + compute, no database writes.
    Works for any source_type; a discovered external device and an
    ordinary simulated one go through the exact same scoring path.
    """
    database = get_database(request)

    device = await asyncio.to_thread(database.get_device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    objects = await asyncio.to_thread(database.get_objects, device_id)

    snapshot = _to_snapshot(device, objects)
    suggestions = suggest_device_semantics(snapshot)
    device_suggestion, point_suggestions = suggestions[0], suggestions[1:]

    return {
        "device": _to_response_entry(
            device_suggestion, source_id=device["id"], existing_class=device.get("equipment_type"),
        ),
        "points": [
            _to_response_entry(point_suggestion, source_id=obj["id"], existing_class=obj.get("point_type"))
            for point_suggestion, obj in zip(point_suggestions, objects)
        ],
    }


@router.post("/points/{object_id}/ai")
async def suggest_point_ai(device_id: int, object_id: int, request: Request):
    """
    AI fallback for ONE point, triggered only by an explicit per-row "Use
    AI" click (never automatic, never batched) -- used when the
    deterministic engine above returned low/none confidence for it. Pure
    read + compute: point_type is never written here, only by the existing
    PUT /devices/{id}/objects/{id} path once the user reviews and applies.

    The Azure client is constructed here, inside the request, not at
    import/module-load time -- a missing/misconfigured Azure setup must
    only fail this one call, never break the deterministic suggestions
    route or the app itself.
    """
    database = get_database(request)

    device = await asyncio.to_thread(database.get_device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    objects = await asyncio.to_thread(database.get_objects, device_id)
    target = next((o for o in objects if o["id"] == object_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Object not found")
    siblings = [o for o in objects if o["id"] != object_id]

    equipment_context = device.get("equipment_type")
    if not equipment_context:
        # No explicit classification yet -- fall back to the same
        # deterministic equipment inference already used elsewhere,
        # rather than duplicating that logic here. Purely contextual for
        # the AI prompt; never persisted.
        snapshot = _to_snapshot(device, objects)
        equipment_context = suggest_equipment(snapshot).suggested_class

    try:
        client = AzureStructuredClient()
        result = suggest_point_via_ai(
            client, device=device, target=target, siblings=siblings, equipment_context=equipment_context,
        )
    except Exception:
        # The real exception (which can include endpoint/deployment/config
        # detail from the Azure SDK) is logged server-side only -- the
        # HTTP response must never carry it.
        log.warning(
            "AI semantic suggestion failed for device=%s object=%s", device_id, object_id, exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail="AI suggestion is unavailable. Check Azure OpenAI configuration.",
        )

    # Authoritative vocabulary check -- the model's response schema does
    # NOT constrain this on its own (see ai_suggestions.py's docstring);
    # this is the one enforcement point. A rejected/hallucinated class
    # collapses to no suggestion rather than being passed through.
    suggested_class = result.suggested_class
    if suggested_class is not None and suggested_class not in POINT_TYPES:
        suggested_class = None
    confidence = result.confidence if suggested_class is not None else "none"

    return {
        "source_kind": "point",
        "source_id": object_id,
        "source_name": target["name"],
        "suggested_class": suggested_class,
        "existing_class": target.get("point_type"),
        "confidence": confidence,
        "score": 0.0,
        "reasons": [result.reason],
        "alternatives": [],
        "source": "ai",
    }
