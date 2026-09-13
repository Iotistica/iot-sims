"""Neutral ``ResolutionCandidate`` builders -- the compatibility boundary.

This is the ONLY module that knows today's flat ``objects.point_type`` /
``recording_points.point_type`` fields are a Brick-class mirror (see CLAUDE.md: the flat
tags are kept in lockstep with ``semantic_entities.brick_class`` by
``src/semantics/mirror.py``). Consumers (calibration, Auto-Map) call these builders and
receive a provider-neutral ``ResolutionCandidate``; they never touch ``point_type`` or a
provider namespace themselves.

A future record-time snapshot of structured identity / Haystack tags slots in here under
new ``representation_snapshot`` keys without any consumer change.
"""
from __future__ import annotations

from typing import Any, Mapping

from .resolution import ResolutionCandidate


def _legacy_representation_tags(point_type: str | None) -> dict[str, str]:
    # Today the flat mirror == semantic_entities.brick_class. Namespaced so other
    # providers / future structured snapshots coexist without collision.
    return {"brick.point_class": point_type} if point_type else {}


def build_resolution_candidate_from_object(obj: Mapping[str, Any]) -> ResolutionCandidate:
    """From a live ``objects`` row (as returned by ``database.get_objects`` /
    ``PointCandidate``-style dicts)."""
    return ResolutionCandidate(
        id=int(obj["id"]),
        display_name=str(obj.get("name") or ""),
        unit=obj.get("units"),
        source_object_id=int(obj["id"]),
        representation_snapshot=_legacy_representation_tags(obj.get("point_type")),
    )


def build_resolution_candidate_from_recording_point(point: Mapping[str, Any]) -> ResolutionCandidate:
    """From a ``recording["points"]`` entry (``recording_points`` row joined for display).
    ``source_object_id`` may be ``None`` if the recorded object was since deleted."""
    source_object_id = point.get("source_object_id")
    return ResolutionCandidate(
        id=int(point["id"]),
        display_name=str(point.get("object_name") or ""),
        unit=point.get("units"),
        source_object_id=int(source_object_id) if source_object_id is not None else None,
        representation_snapshot=_legacy_representation_tags(point.get("point_type")),
    )
