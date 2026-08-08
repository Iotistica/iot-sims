"""Entity-aware point resolution built on top of the Database CRUD/
traversal primitives (src/legacy.py) and derive_semantic_key() (keys.py).

get_device_point_values() (src/legacy.py's SimEngine) is left completely
unchanged elsewhere in the codebase -- it's the guaranteed-backward-
compatible path for any caller that hasn't opted in. SemanticResolver is
additive and only used by call sites that explicitly opt in (currently:
the Energy Engine's AHU and lighting evaluation, src/energy/engine.py).

Completeness is judged per FIELD, not per device: resolve_ahu_fans()
returns only the keys whose full graph path resolved, so a partially
migrated device (e.g. only the supply fan has semantic entities) still
gets entity-resolved values for the migrated side and leaves the other
side's keys absent -- callers (src/energy/context.py's snapshot builders)
fall back to the legacy flat-tag lookup per missing field, never for the
whole device just because SOME entity exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .keys import derive_semantic_key


@dataclass(frozen=True)
class ResolvedPoint:
    object_id: int
    object_name: str
    brick_class: str
    value: Any
    entity_id: Optional[int] = None
    owner_entity_id: Optional[int] = None


class SemanticResolver:
    def __init__(self, *, database: Any, simulation_engine: Any) -> None:
        self.database = database
        self.simulation_engine = simulation_engine

    def resolve_device_points(self, device_id: int) -> dict[str, list[ResolvedPoint]]:
        """Multi-match replacement for SimEngine.get_device_point_values() --
        same point_type-keyed shape, but every value is a list of every
        matching point rather than the last one silently overwriting the
        rest. Grouped directly off objects.point_type (not via the
        semantic graph) -- fixing the collapsing-dict bug doesn't require
        entity resolution, just not throwing duplicates away."""
        objects = self.database.get_objects(device_id)
        result: dict[str, list[ResolvedPoint]] = {}
        for obj in objects:
            point_type = obj.get("point_type")
            if not point_type:
                continue
            value = self.simulation_engine.get_object_value(obj["id"])
            result.setdefault(point_type, []).append(
                ResolvedPoint(
                    object_id=obj["id"],
                    object_name=obj.get("name", ""),
                    brick_class=point_type,
                    value=value,
                )
            )
        return result

    def get_equipment_entity(self, device_id: int) -> Optional[dict]:
        """The device's own top-level equipment entity -- an 'equipment'
        entity for this device_id that is never itself the source of an
        isPartOf edge (i.e. not sub-equipment like a Supply_Fan)."""
        for entity in self.database.get_semantic_entities(
            device_id=device_id, entity_kind="equipment",
        ):
            parents = self.database.get_related_entities(entity["id"], "isPartOf", direction="out")
            if not parents:
                return entity
        return None

    def get_sub_equipment(self, parent_entity_id: int, brick_class: Optional[str] = None) -> list[dict]:
        """isPartOf traversal (reverse direction): entities that ARE this
        parent, i.e. entities where predicate='isPartOf', target=parent_entity_id."""
        children = self.database.get_related_entities(parent_entity_id, "isPartOf", direction="in")
        if brick_class is not None:
            children = [c for c in children if c["brick_class"] == brick_class]
        return children

    def get_entity_points(self, entity_id: int, brick_class: Optional[str] = None) -> list[ResolvedPoint]:
        """isPointOf traversal (reverse direction) + live value via
        simulation_engine.get_object_value()."""
        rows = self.database.get_entity_points(entity_id, brick_class)
        return [
            ResolvedPoint(
                object_id=row["object_id"],
                object_name=row.get("object_name", ""),
                brick_class=row["brick_class"],
                value=self.simulation_engine.get_object_value(row["object_id"]),
                entity_id=row["id"],
                owner_entity_id=entity_id,
            )
            for row in rows
        ]

    def resolve_ahu_fans(self, device_id: int) -> dict[str, Any]:
        """AHU equipment entity -> Supply_Fan/Return_Fan sub-equipment
        (isPartOf) -> their Fan_Status/Fan_Speed_Command points (isPointOf).
        Returns only the keys whose point actually resolved -- e.g. if only
        the supply fan has been given semantic entities, only
        supply_fan_running/supply_fan_speed_percent are present; the
        return_fan_* keys are simply absent so the caller (build_ahu_snapshot)
        falls back to the legacy alias lookup for just that side. Never
        returns a fixed shape, and never short-circuits to 'no semantic
        model' just because SOME entity exists for the device -- only
        get_equipment_entity() returning None does that (nothing to walk
        from at all)."""
        result: dict[str, Any] = {}

        ahu_entity = self.get_equipment_entity(device_id)
        if ahu_entity is None:
            return result

        for side, brick_class in (("supply", "Supply_Fan"), ("return", "Return_Fan")):
            fans = self.get_sub_equipment(ahu_entity["id"], brick_class=brick_class)
            if not fans:
                continue

            by_class = {p.brick_class: p.value for p in self.get_entity_points(fans[0]["id"])}

            if "Fan_Status" in by_class:
                result[f"{side}_fan_running"] = by_class["Fan_Status"]
            if "Fan_Speed_Command" in by_class:
                result[f"{side}_fan_speed_percent"] = by_class["Fan_Speed_Command"]

        return result

    def get_lighting_zone_entity(self, device_id: int, instance_key: str) -> Optional[dict]:
        """Looked up by (device_id, local_slug=instance_key) -- equivalent
        to matching on derive_semantic_key('location', 'Lighting_Zone',
        device_id=device_id, local_slug=instance_key), NEVER by entity.name
        (cosmetic, user-editable). Always recomputed against the CURRENT
        live device_id at call time, so it stays correct across project
        reloads that reassign device_id without the caller needing to know
        anything changed."""
        expected_key = derive_semantic_key(
            "location", "Lighting_Zone", device_id=device_id, local_slug=instance_key,
        )
        for entity in self.database.get_semantic_entities(
            device_id=device_id, entity_kind="location", brick_class="Lighting_Zone",
        ):
            if entity["semantic_key"] == expected_key:
                return entity
        return None
