from __future__ import annotations

from dataclasses import dataclass
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

    def point(self, point_type: str) -> PointSnapshot | None:
        return self.points.get(point_type)

    def value(self, point_type: str, default: Any = None) -> Any:
        point = self.point(point_type)
        return default if point is None else point.value
