"""Cross-device point search/list -- backs the Functional Test builder's
PointPicker (admin/src/components/PointPicker.vue). Unlike the existing
per-equipment/per-entity point queries (get_assignable_points_for_equipment,
get_entity_points), this is intentionally unscoped: every point on every
device, with live value annotated in, so the picker can search/filter by
Device, Point Name, Object Type, and Semantic Type all at once.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Request

router = APIRouter(
    prefix="/points",
    tags=["points"],
)


def get_database(request: Request) -> Any:
    return getattr(request.app.state, "db", None)


@router.get("")
async def list_points(
    request: Request,
    search: Optional[str] = None,
    object_type: Optional[str] = None,
    device_id: Optional[int] = None,
    point_type: Optional[str] = None,
):
    database = get_database(request)
    if database is None:
        return []

    rows = await asyncio.to_thread(
        database.get_all_points,
        object_type=object_type,
        device_id=device_id,
        point_type=point_type,
    )

    if search:
        needle = search.lower()
        rows = [
            r for r in rows
            if needle in (r.get("name") or "").lower() or needle in (r.get("device_name") or "").lower()
        ]

    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        for row in rows:
            if row.get("source_type") == "simulated":
                try:
                    row["live_value"] = engine.get_object_value(row["object_id"])
                except Exception:
                    row["live_value"] = None
            else:
                row["live_value"] = None
    else:
        for row in rows:
            row["live_value"] = None

    return rows
