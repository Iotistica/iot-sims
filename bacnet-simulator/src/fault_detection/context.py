from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PointSnapshot:
    object_id: int
    object_identifier: str
    name: str
    point_type: str | None
    value: Any
    units: str | None = None
    reliability: str | None = None
    out_of_service: bool = False


@dataclass(frozen=True)
class FaultContext:
    device_id: int
    device_name: str
    equipment_type: str | None
    timestamp: float
    points: dict[str, PointSnapshot]
    parameters: dict[str, Any]
    # Additive, non-collapsing sibling of `points` -- every PointSnapshot
    # sharing a point_type, not just the last one written. `points`/
    # .point()/.value() are left exactly as they were (last-write-wins) so
    # every existing rule in rules/ahu.py and rules/sensors.py stays
    # untouched; new/updated rules that need to see duplicate-class points
    # (e.g. two Run_Status points on one device) use .values() instead.
    points_by_type: dict[str, list[PointSnapshot]] = field(default_factory=dict)

    def point(self, point_type: str) -> PointSnapshot | None:
        return self.points.get(point_type)

    def value(self, point_type: str, default: Any = None) -> Any:
        point = self.point(point_type)
        return default if point is None else point.value

    def values(self, point_type: str) -> list[Any]:
        """All values for every point sharing this point_type, not just
        the last one -- the general fix for the duplicate-point-class
        collapsing bug, at the Fault Detection layer."""
        return [p.value for p in self.points_by_type.get(point_type, [])]
