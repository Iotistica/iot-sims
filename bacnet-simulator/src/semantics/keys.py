"""Deterministic semantic_key derivation for semantic_entities.

semantic_key is NOT a portable identity — it embeds live surrogate ids
(devices.id/objects.id/locations.id), so it is only valid as long as those
specific ids are live. It must always be recomputed from an entity's
CURRENT ids (never copied from a prior snapshot), which is exactly what
every caller below does: get_or_create_*_entity() computes it from ids it
was just given, resolver lookups compute it from the live device_id at
call time, and src/legacy.py's load_project() recomputes it from the
freshly-remapped ids on every project load instead of trusting the
value stored in the exported project JSON.

local_slug is the one piece derive_semantic_key cannot recover from ids
alone — a short, ID-independent disambiguator (e.g. "zone-a") needed when
entity_kind + brick_class + device_id alone aren't unique (two
Lighting_Zone entities on one DALI gateway device). It never references a
surrogate id, so it survives project export/import verbatim.
"""
from __future__ import annotations

from typing import Optional


def derive_semantic_key(
    entity_kind: str,
    brick_class: str,
    *,
    device_id: Optional[int] = None,
    object_id: Optional[int] = None,
    location_id: Optional[int] = None,
    equipment_id: Optional[int] = None,
    local_slug: Optional[str] = None,
) -> Optional[str]:
    """Pure function of (kind, class, current live ids, local_slug).

    Returns None when none of device_id/object_id/location_id/equipment_id
    is set — there's nothing to key off, so the entity gets no semantic_key
    (fine: ad hoc/manually-created entities don't need deterministic
    identity).
    """
    if device_id is None and object_id is None and location_id is None and equipment_id is None:
        return None

    return (
        f"{entity_kind}:{brick_class}:"
        f"device={device_id}:object={object_id}:location={location_id}:"
        f"equipment={equipment_id}:slug={local_slug}"
    )
