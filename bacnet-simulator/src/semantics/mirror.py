"""Bidirectional sync between Brick semantic_entities and the legacy flat
classification fields (devices.equipment_type, objects.point_type,
locations.kind).

Brick is the semantic source of truth going forward. The flat fields are
compatibility mirrors, kept in lockstep automatically so a user only ever
assigns a classification once -- through whichever UI surface is
convenient (the Device/Object/Location drawer, or the Semantic Model
panel) -- and both systems agree, with no risk of silently diverging.
They stay in the schema, and stay written, because Energy, Fault
Detection, Commissioning, and SimEngine.get_device_point_values() all
still read them directly and have not been migrated to SemanticResolver
(src/semantics/resolver.py) -- new consumers should use SemanticResolver
instead of reading these fields directly.

Two sync directions live here, neither ever calls the other's "big" CRUD
method (only raw, targeted single-column writes) -- so there is no update
loop:

  1. flat field -> Brick entity (sync_entity_from_flat_field): called from
     Database.create_device/update_device/create_object/update_object/
     create_location/update_location (src/legacy.py). This is the
     PRIMARY, everyday path -- a user assigns a class via the ordinary
     drawer, and the corresponding Brick entity is created/updated/
     deleted to match, server-side, in the same request. Clearing the
     field (None) deletes the entity rather than leaving it dangling with
     a stale brick_class.

  2. Brick entity -> flat field (sync_flat_field_from_entity): called from
     Database.create_semantic_entity/update_semantic_entity/
     delete_semantic_entity (the generic entity CRUD backing the Semantic
     Model panel, src/api/routers/semantic.py). Implemented ONLY for
     point/location entities -- object_id/location_id are unambiguously
     1:1 with their entity thanks to semantic_entities' own partial
     unique indexes (idx_semantic_entities_object_unique /
     idx_semantic_entities_location_unique), so there's never a question
     of which entity "counts". Deliberately NOT implemented for
     entity_kind='equipment': a device_id alone can't distinguish a
     device's own top-level entity from sub-equipment (a Supply_Fan also
     has device_id set) without already knowing its isPartOf
     relationships, which may not exist yet at entity-creation time --
     mirroring here could incorrectly overwrite devices.equipment_type
     with a sub-equipment's class the moment it's created, before its
     isPartOf relationship is added. Top-level equipment classification
     is a Device-drawer-only action (direction 1, via
     find_direct_equipment_entity's isPartOf-source exclusion); the
     Semantic panel is for sub-equipment and relationships, which must
     never touch devices.equipment_type -- see find_direct_equipment_entity.

Startup backfill (backfill_semantic_entities(), in this package's
backfill.py) is a THIRD, one-time-per-row direction -- flat field ->
Brick, but ONLY when no Brick entity exists yet for that row. It is a
migration mechanism for pre-existing data (old databases/projects that
predate Brick Core), not a steady-state sync path, and it must never
overwrite an existing Brick entity with a stale flat tag -- it uses the
same find_direct_*_entity lookups as direction 1 above to decide whether
to skip a row.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from .keys import derive_semantic_key
from .validation import validate_semantic_entity


def find_direct_equipment_entity(conn: sqlite3.Connection, device_id: int) -> Optional[dict]:
    """The device's own top-level equipment entity: entity_kind='equipment',
    this device_id, and NOT itself the source of an isPartOf edge (which
    would make it sub-equipment, e.g. a Supply_Fan/Return_Fan hosted by an
    AHU). At most one such entity should exist under normal use of
    sync_entity_from_flat_field(); if ambiguous data exists (e.g.
    hand-edited), the lowest-id match wins, deterministically."""
    for row in conn.execute(
        "SELECT * FROM semantic_entities WHERE entity_kind='equipment' AND device_id=? ORDER BY id",
        (device_id,),
    ):
        row = dict(row)
        is_sub_equipment = conn.execute(
            "SELECT 1 FROM semantic_relationships WHERE source_entity_id=? AND predicate='isPartOf'",
            (row["id"],),
        ).fetchone()
        if not is_sub_equipment:
            return row
    return None


def find_point_entity(conn: sqlite3.Connection, object_id: int) -> Optional[dict]:
    """Unambiguous: idx_semantic_entities_object_unique guarantees at most
    one entity_kind='point' row can ever reference a given object_id."""
    row = conn.execute(
        "SELECT * FROM semantic_entities WHERE entity_kind='point' AND object_id=?", (object_id,)
    ).fetchone()
    return dict(row) if row else None


def find_real_location_entity(conn: sqlite3.Connection, location_id: int) -> Optional[dict]:
    """Unambiguous: idx_semantic_entities_location_unique guarantees at
    most one entity_kind='location' row can ever reference a given
    location_id -- and it's always a REAL locations row, never a virtual,
    device-hosted one (those have location_id IS NULL by construction,
    see validate_semantic_entity)."""
    row = conn.execute(
        "SELECT * FROM semantic_entities WHERE entity_kind='location' AND location_id=?", (location_id,)
    ).fetchone()
    return dict(row) if row else None


def _write_direct_entity(
    conn: sqlite3.Connection,
    *,
    existing: Optional[dict],
    name: str,
    brick_class: Optional[str],
    entity_kind: str,
    device_id: Optional[int] = None,
    object_id: Optional[int] = None,
    location_id: Optional[int] = None,
) -> None:
    """brick_class=None deletes `existing` (if any) -- clearing a
    classification removes its Brick entity rather than leaving it behind
    with a stale brick_class. Otherwise creates or updates `existing` in
    place (preserving its local_slug, though direct entities normally
    don't have one)."""
    if brick_class is None:
        if existing is not None:
            conn.execute("DELETE FROM semantic_entities WHERE id=?", (existing["id"],))
        return

    validate_semantic_entity(
        entity_kind, brick_class,
        device_id=device_id, object_id=object_id, location_id=location_id,
    )
    local_slug = existing.get("local_slug") if existing else None
    semantic_key = derive_semantic_key(
        entity_kind, brick_class,
        device_id=device_id, object_id=object_id, location_id=location_id,
        local_slug=local_slug,
    )

    if existing is not None:
        conn.execute(
            "UPDATE semantic_entities SET name=?, brick_class=?, semantic_key=? WHERE id=?",
            (name, brick_class, semantic_key, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO semantic_entities "
            "(name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id),
        )


def sync_entity_from_flat_field(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    name: str,
    brick_class: Optional[str],
    device_id: Optional[int] = None,
    object_id: Optional[int] = None,
    location_id: Optional[int] = None,
) -> None:
    """Direction 1 (primary path): a flat field (equipment_type/point_type/
    kind) was just written via the ordinary Device/Object/Location drawer
    -- keep that row's DIRECT Brick entity in lockstep, server-side, so
    the user never has to separately visit the Semantic Model panel for
    ordinary (non-sub-equipment, non-relationship) classification."""
    if entity_kind == "equipment":
        existing = find_direct_equipment_entity(conn, device_id)
    elif entity_kind == "point":
        existing = find_point_entity(conn, object_id)
    elif entity_kind == "location":
        existing = find_real_location_entity(conn, location_id)
    else:
        raise ValueError(f"unknown entity_kind: {entity_kind!r}")

    _write_direct_entity(
        conn, existing=existing, name=name, brick_class=brick_class,
        entity_kind=entity_kind, device_id=device_id, object_id=object_id, location_id=location_id,
    )


def sync_flat_field_from_entity(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    brick_class: Optional[str],
    object_id: Optional[int] = None,
    location_id: Optional[int] = None,
) -> None:
    """Direction 2 (secondary path, point/location only -- see this
    module's docstring for why equipment is excluded): a semantic entity
    was just created/updated/deleted via the generic Semantic Model panel
    CRUD -- if it directly represents an object/location row, keep that
    row's flat field in lockstep too, so a user who classifies a point
    through the Semantic panel instead of the Object drawer still sees it
    reflected there (and vice versa). brick_class=None clears the flat
    field (used on delete, or when an entity is re-linked away from this
    row)."""
    if entity_kind == "point" and object_id is not None:
        conn.execute("UPDATE objects SET point_type=? WHERE id=?", (brick_class, object_id))
    elif entity_kind == "location" and location_id is not None:
        conn.execute("UPDATE locations SET kind=? WHERE id=?", (brick_class, location_id))
