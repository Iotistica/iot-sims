"""Single source of truth for whether an entity_kind/brick_class/FK
combination is a legal semantic_entities row.

Called from every write path that can create or update a semantic_entities
row: the API layer (SemanticEntityCreate.validate_semantic() delegates
here), and directly from src/legacy.py's Database.create_semantic_entity()/
update_semantic_entity()/get_or_create_*_entity() — so seed data, project
import/load, and the backfill step (none of which go through the API/
Pydantic layer) can't bypass it.
"""
from __future__ import annotations

from typing import Optional

from ..core.config import CONTROLLER_TYPES, EQUIPMENT_TYPES, LOCATION_KINDS, POINT_TYPES

VALID_ENTITY_KINDS = {"equipment", "point", "location", "controller"}

_VOCAB_BY_KIND = {
    "equipment": EQUIPMENT_TYPES,
    "point": POINT_TYPES,
    "location": LOCATION_KINDS,
    "controller": CONTROLLER_TYPES,
}


def validate_semantic_entity(
    entity_kind: str,
    brick_class: str,
    *,
    device_id: Optional[int],
    object_id: Optional[int],
    location_id: Optional[int],
    equipment_id: Optional[int] = None,
) -> None:
    """Raises ValueError on any invalid combination. Returns None (no-op)
    when the combination is legal."""
    if entity_kind not in VALID_ENTITY_KINDS:
        raise ValueError(
            f"entity_kind must be one of: {sorted(VALID_ENTITY_KINDS)}"
        )

    vocab = _VOCAB_BY_KIND[entity_kind]
    if brick_class not in vocab:
        raise ValueError(
            f"brick_class {brick_class!r} is not a canonical Brick class "
            f"for entity_kind={entity_kind!r}; must be one of: {sorted(vocab)}"
        )

    if entity_kind == "point":
        if object_id is None:
            raise ValueError("point entities require object_id to be set")
        if device_id is not None or location_id is not None or equipment_id is not None:
            raise ValueError(
                "point entities must not set device_id, location_id, or equipment_id "
                "(the point's device is derived via object_id -> objects.device_id)"
            )

    elif entity_kind == "equipment":
        if object_id is not None:
            raise ValueError("equipment entities must not set object_id")
        has_device = device_id is not None
        has_equipment = equipment_id is not None
        if has_device and has_equipment:
            raise ValueError(
                "equipment entities must not set both device_id and "
                "equipment_id — a legacy device-rooted equipment entity "
                "and a real `equipment` row are mutually exclusive "
                "representations"
            )
        if not has_device and not has_equipment:
            raise ValueError(
                "equipment entities require exactly one of device_id "
                "(a legacy device-rooted equipment entity) or equipment_id "
                "(a real `equipment` row) to be set"
            )
        # location_id is deliberately not an equipment FK — physical
        # placement is a hasLocation relationship, not a foreign key.

    elif entity_kind == "controller":
        if device_id is None:
            raise ValueError("controller entities require device_id to be set")
        if object_id is not None or equipment_id is not None or location_id is not None:
            raise ValueError(
                "controller entities must not set object_id, equipment_id, "
                "or location_id (physical placement is a hasLocation "
                "relationship, not a foreign key -- same principle as equipment)"
            )

    elif entity_kind == "location":
        if object_id is not None or equipment_id is not None:
            raise ValueError("location entities must not set object_id or equipment_id")
        has_location = location_id is not None
        has_device = device_id is not None
        if has_location and has_device:
            raise ValueError(
                "location entities must not set both location_id and "
                "device_id — a real locations row (location_id) and a "
                "virtual device-hosted zone (device_id) are mutually "
                "exclusive representations"
            )
        if not has_location and not has_device:
            raise ValueError(
                "location entities require exactly one of location_id "
                "(a real locations row) or device_id (a virtual, "
                "device-hosted location such as a Lighting_Zone) to be set"
            )
