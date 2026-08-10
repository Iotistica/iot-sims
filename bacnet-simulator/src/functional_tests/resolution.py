"""Semantic point resolution for Functional Test execution.

Resolves a semantic point_type (e.g. "Run_Status") against a target device's
ACTUAL objects. Prefers the semantic graph (semantic_entities/
semantic_relationships, isPointOf) when it has an applicable equipment
entity, since a flat device_id + object.point_type scan cannot distinguish
two pieces of sub-equipment on the same device that share a point_type (e.g.
two boilers each with their own Run_Status) -- see resolve_point_for_device.
Falls back to the flat scan only when the graph has no applicable
relationship information for the target at all.

Two-tier design, deliberately kept separable from device-based target
selection: resolve_point_for_equipment_entity() is graph-only and takes an
already-identified equipment entity -- this is the primitive a future
"target a specific semantic equipment entity" phase can call directly.
resolve_point_for_device() is the device-target wrapper this phase's API
actually uses; it decides which equipment entity (if any) applies to a
device before delegating to the entity-scoped resolver.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PointResolution:
    status: str  # "resolved" | "missing" | "ambiguous" | "inconsistent"
    resolution_source: str  # "semantic_graph" | "device_point_type"
    object: Optional[dict] = None
    candidates: list[str] = field(default_factory=list)
    message: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "resolution_source": self.resolution_source,
            "object": self.object,
            "candidates": self.candidates,
            "message": self.message,
        }


def collect_point_types(definition: dict) -> set[str]:
    """Walks a FunctionalTestDefinition for every semantic point_type
    referenced by wait_until/capture/verify/compare -- the set that must be
    resolved before a run can start."""
    point_types: set[str] = set()
    for node in definition.get("nodes", []):
        node_type = node.get("type")
        params = node.get("params") or {}
        if node_type in ("wait_until", "capture"):
            point_type = params.get("point_type")
            if point_type:
                point_types.add(point_type)
        elif node_type in ("verify", "compare"):
            for side in ("left", "right"):
                operand = params.get(side) or {}
                if operand.get("kind") == "point" and operand.get("point_type"):
                    point_types.add(operand["point_type"])
    return point_types


def resolve_point_for_equipment_entity(
    database: Any, equipment_entity: dict, point_type: str,
) -> PointResolution:
    """Graph-only: resolves point_type among the points isPointOf this
    already-identified equipment entity. Never falls back to a flat scan --
    once the graph has identified the equipment, it is trusted fully for
    that equipment's points, so an unrelated object that merely happens to
    share the point_type tag is never silently pulled in."""
    matches = database.get_entity_points(equipment_entity["id"], brick_class=point_type)

    if len(matches) == 1:
        match = matches[0]
        obj = database.get_object(match["object_id"])
        if obj is None or obj.get("point_type") != point_type:
            return PointResolution(
                status="inconsistent",
                resolution_source="semantic_graph",
                message=(
                    f"{equipment_entity['name']}'s {point_type} relationship points to "
                    f"an object whose point_type no longer matches (semantic model drift)"
                ),
            )
        return PointResolution(status="resolved", resolution_source="semantic_graph", object=obj)

    if len(matches) > 1:
        return PointResolution(
            status="ambiguous",
            resolution_source="semantic_graph",
            candidates=[m.get("object_name") or m.get("name") for m in matches],
            message=f"{equipment_entity['name']} has multiple {point_type} points",
        )

    return PointResolution(
        status="missing",
        resolution_source="semantic_graph",
        message=f"{equipment_entity['name']} has no {point_type} point",
    )


def resolve_point_for_device(
    database: Any, device_id: int, equipment_type: str, point_type: str,
) -> PointResolution:
    """Device-target wrapper used by this phase's /resolve and run-creation
    flow. Picks the applicable semantic equipment entity for this device (if
    any) and delegates to resolve_point_for_equipment_entity; falls back to
    a flat device_id + object.point_type scan only when the graph has no
    applicable equipment entity for this device+equipment_type at all."""
    equipment_candidates = database.get_semantic_entities(
        device_id=device_id, entity_kind="equipment", brick_class=equipment_type,
    )

    if len(equipment_candidates) == 1:
        return resolve_point_for_equipment_entity(database, equipment_candidates[0], point_type)

    if len(equipment_candidates) > 1:
        return PointResolution(
            status="ambiguous",
            resolution_source="semantic_graph",
            candidates=[e["name"] for e in equipment_candidates],
            message=(
                f"This device hosts multiple {equipment_type} equipment entities -- "
                "select a specific one is not yet supported by target selection"
            ),
        )

    # No applicable graph info for this device+equipment_type -- flat fallback.
    objects = database.get_objects(device_id)
    matches = [o for o in objects if o.get("point_type") == point_type]

    if len(matches) == 1:
        return PointResolution(status="resolved", resolution_source="device_point_type", object=matches[0])
    if len(matches) > 1:
        return PointResolution(
            status="ambiguous",
            resolution_source="device_point_type",
            candidates=[o["name"] for o in matches],
            message=f"Multiple {point_type} points found on this device",
        )
    return PointResolution(
        status="missing",
        resolution_source="device_point_type",
        message=f"No {point_type} point found on this device",
    )


def build_resolution(
    database: Any, device_id: int, equipment_type: str, point_types: set[str],
) -> dict[str, PointResolution]:
    return {
        point_type: resolve_point_for_device(database, device_id, equipment_type, point_type)
        for point_type in point_types
    }
