from __future__ import annotations

from typing import Any


class CommissioningPointResolver:
    """Compatibility boundary between commissioning tests and semantics.

    Today this resolves points from objects.point_type. After Brick Core is
    implemented, replace the internals with Brick graph traversal while keeping
    the commissioning tests unchanged.
    """

    def __init__(self, *, database: Any, simulation_engine: Any) -> None:
        self.database = database
        self.simulation_engine = simulation_engine

    def get_points(self, device_id: int) -> list[dict[str, Any]]:
        objects = self.database.get_objects(device_id)
        return [
            {
                **obj,
                "value": self.simulation_engine.get_object_value(int(obj["id"])),
            }
            for obj in objects
        ]

    def find_by_semantic(
        self,
        device_id: int,
        point_type: str,
    ) -> list[dict[str, Any]]:
        # Return ALL matches so duplicate Brick classes do not overwrite.
        return [
            point
            for point in self.get_points(device_id)
            if point.get("point_type") == point_type
        ]

    def first_by_semantic(
        self,
        device_id: int,
        point_type: str,
    ) -> dict[str, Any] | None:
        matches = self.find_by_semantic(device_id, point_type)
        return matches[0] if matches else None

    def find_by_name(
        self,
        device_id: int,
        name: str,
    ) -> dict[str, Any] | None:
        for point in self.get_points(device_id):
            if point.get("name") == name:
                return point
        return None
