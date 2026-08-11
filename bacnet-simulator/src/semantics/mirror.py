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


def find_equipment_entity(conn: sqlite3.Connection, equipment_id: int) -> Optional[dict]:
    """Unambiguous: idx_semantic_entities_equipment_unique guarantees at
    most one entity_kind='equipment' row can ever reference a given
    equipment_id -- unlike device_id-rooted equipment (find_direct_
    equipment_entity), a real `equipment` row has no sub-equipment concept,
    so there's no isPartOf-exclusion needed here."""
    row = conn.execute(
        "SELECT * FROM semantic_entities WHERE entity_kind='equipment' AND equipment_id=?", (equipment_id,)
    ).fetchone()
    return dict(row) if row else None


def find_direct_controller_entity(conn: sqlite3.Connection, device_id: int) -> Optional[dict]:
    """Unambiguous: idx_semantic_entities_controller_unique guarantees at
    most one entity_kind='controller' row can ever reference a given
    device_id -- no sub-controller concept, so (unlike find_direct_
    equipment_entity) no isPartOf-exclusion is needed."""
    row = conn.execute(
        "SELECT * FROM semantic_entities WHERE entity_kind='controller' AND device_id=?", (device_id,)
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
    equipment_id: Optional[int] = None,
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
        device_id=device_id, object_id=object_id, location_id=location_id, equipment_id=equipment_id,
    )
    local_slug = existing.get("local_slug") if existing else None
    semantic_key = derive_semantic_key(
        entity_kind, brick_class,
        device_id=device_id, object_id=object_id, location_id=location_id, equipment_id=equipment_id,
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
            "(name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id, equipment_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id, equipment_id),
        )


def _sync_point_membership(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    device_id: Optional[int] = None,
    object_id: Optional[int] = None,
) -> None:
    """Keeps the single deterministic isPointOf relationship between a
    point's own entity and its owning device's top-level equipment entity
    in lockstep with the two flat fields that already own this decision --
    NOT relationship inference: object.device_id already says which device
    a point belongs to, so once both sides are classified, exactly one
    relationship must exist, order-independent (equipment classified
    first, point classified first, or either edited later all converge to
    the same state).

    Only ever ADDS a row here -- removal is handled for free by
    semantic_relationships' own ON DELETE CASCADE on both source_entity_id
    and target_entity_id: clearing a point's point_type (or a device's
    equipment_type) deletes ITS OWN entity row via _write_direct_entity's
    brick_class=None branch above, which cascades away any relationship
    row that referenced it. Same for deleting the device/object outright
    (devices/objects both cascade to semantic_entities, which cascades to
    semantic_relationships) -- nothing new needed for either case.

    upsert_semantic_relationship lives in backfill.py, which already
    imports this module's finders -- imported lazily here to avoid a
    circular top-level import between the two.
    """
    from .backfill import upsert_semantic_relationship

    if entity_kind == "point" and object_id is not None:
        point_entity = find_point_entity(conn, object_id)
        if point_entity is None:
            return  # point_type was just cleared -- nothing to link
        owner = conn.execute("SELECT device_id FROM objects WHERE id=?", (object_id,)).fetchone()
        if owner is None:
            return
        equipment_entity = find_direct_equipment_entity(conn, owner["device_id"])
        if equipment_entity is not None:
            upsert_semantic_relationship(conn, point_entity["id"], "isPointOf", equipment_entity["id"])

    elif entity_kind == "equipment" and device_id is not None:
        equipment_entity = find_direct_equipment_entity(conn, device_id)
        if equipment_entity is None:
            return  # equipment_type was just cleared -- cascade already removed incoming links
        for obj_row in conn.execute(
            "SELECT id FROM objects WHERE device_id=? AND point_type IS NOT NULL", (device_id,),
        ):
            point_entity = find_point_entity(conn, obj_row["id"])
            if point_entity is not None:
                upsert_semantic_relationship(conn, point_entity["id"], "isPointOf", equipment_entity["id"])


def sync_device_location_relationship(
    conn: sqlite3.Connection,
    *,
    device_id: int,
    location_id: Optional[int],
) -> None:
    """Keeps the deterministic hasLocation relationship between a device's
    top-level equipment entity and its assigned location's entity in
    lockstep with devices.location_id -- the same "both sides already
    classified" membership pattern as _sync_point_membership below, just
    for device<->location instead of point<->equipment.

    Unlike point membership, changing/clearing devices.location_id does
    NOT delete or recreate the device's own equipment entity (the device
    is still e.g. a Boiler, just relocated or unassigned) -- so this can't
    rely on cascade delete alone and explicitly removes a stale edge
    before adding the correct one. A no-op (no DB write at all) when the
    desired state already matches, so calling this on every device save
    doesn't churn the relationship row on unrelated edits (name changes,
    etc.).

    Called from three places, all ultimately reachable from Database.
    create_device/update_device (the only paths that ever set
    devices.location_id): directly after those two calls, from this
    module's own sync_entity_from_flat_field() (location branch, so a
    location becoming classified after a device was already pointed at it
    catches up every such device), and from backfill.py's one-time startup
    pass for pre-existing data.
    """
    from .backfill import upsert_semantic_relationship

    equipment_entity = find_direct_equipment_entity(conn, device_id)
    if equipment_entity is None:
        return  # device isn't classified as equipment yet -- nothing to attach a location to

    location_entity = find_real_location_entity(conn, location_id) if location_id is not None else None
    desired_target_id = location_entity["id"] if location_entity is not None else None

    existing = conn.execute(
        "SELECT target_entity_id FROM semantic_relationships WHERE source_entity_id=? AND predicate='hasLocation'",
        (equipment_entity["id"],),
    ).fetchone()
    current_target_id = existing["target_entity_id"] if existing else None

    if current_target_id == desired_target_id:
        return

    conn.execute(
        "DELETE FROM semantic_relationships WHERE source_entity_id=? AND predicate='hasLocation'",
        (equipment_entity["id"],),
    )
    if desired_target_id is not None:
        upsert_semantic_relationship(conn, equipment_entity["id"], "hasLocation", desired_target_id)


def _sync_devices_for_location(conn: sqlite3.Connection, *, location_id: int) -> None:
    """The location-side half of the same sync: when a location's own
    entity is just created/updated (its `kind` became classified), catch
    up every device already pointed at it via devices.location_id --
    covers the "device located before its location was classified" order.
    Clearing a location's kind deletes its own entity, which cascades away
    any hasLocation edges pointing at it automatically -- nothing to do
    here for that direction."""
    for device_row in conn.execute("SELECT id FROM devices WHERE location_id=?", (location_id,)):
        sync_device_location_relationship(conn, device_id=device_row["id"], location_id=location_id)


def sync_entity_from_flat_field(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    name: str,
    brick_class: Optional[str],
    device_id: Optional[int] = None,
    object_id: Optional[int] = None,
    location_id: Optional[int] = None,
    equipment_id: Optional[int] = None,
) -> None:
    """Direction 1 (primary path): a flat field (equipment_type/point_type/
    kind) was just written via the ordinary Device/Object/Location/Equipment
    drawer -- keep that row's DIRECT Brick entity in lockstep, server-side,
    so the user never has to separately visit the Semantic Model panel for
    ordinary (non-sub-equipment, non-relationship) classification.

    entity_kind='equipment' dispatches on which of device_id/equipment_id
    is provided: device_id is the legacy "this device is also its own
    equipment" path (find_direct_equipment_entity, unchanged); equipment_id
    is the new real `equipment` row path (find_equipment_entity, always
    unambiguous 1:1). Exactly one is ever passed by any real caller --
    validate_semantic_entity enforces that."""
    if entity_kind == "equipment":
        existing = find_equipment_entity(conn, equipment_id) if equipment_id is not None \
            else find_direct_equipment_entity(conn, device_id)
    elif entity_kind == "point":
        existing = find_point_entity(conn, object_id)
    elif entity_kind == "location":
        existing = find_real_location_entity(conn, location_id)
    else:
        raise ValueError(f"unknown entity_kind: {entity_kind!r}")

    _write_direct_entity(
        conn, existing=existing, name=name, brick_class=brick_class,
        entity_kind=entity_kind, device_id=device_id, object_id=object_id,
        location_id=location_id, equipment_id=equipment_id,
    )

    if entity_kind in ("equipment", "point"):
        # Auto-link-every-point-on-this-device only makes sense for the
        # legacy 1-device-1-equipment case (device_id) -- a real `equipment`
        # row (equipment_id) has no "its device" to scan objects from, so
        # points must be manually related to it via the Semantic panel
        # instead (see the module docstring's Brick-graph-not-FK-shortcut
        # principle). _sync_point_membership's own device_id-only condition
        # already makes this a no-op when equipment_id was used instead.
        _sync_point_membership(conn, entity_kind=entity_kind, device_id=device_id, object_id=object_id)
    elif entity_kind == "location":
        _sync_devices_for_location(conn, location_id=location_id)


def sync_flat_field_from_entity(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    brick_class: Optional[str],
    object_id: Optional[int] = None,
    location_id: Optional[int] = None,
    equipment_id: Optional[int] = None,
) -> None:
    """Direction 2 (secondary path): a semantic entity was just created/
    updated/deleted via the generic Semantic Model panel CRUD -- if it
    directly represents an object/location/equipment row, keep that row's
    flat field in lockstep too, so a user who classifies through the
    Semantic panel instead of the Object/Location/Equipment drawer still
    sees it reflected there (and vice versa). brick_class=None clears the
    flat field (used on delete, or when an entity is re-linked away from
    this row).

    Point/location were always safe here (object_id/location_id are
    unambiguously 1:1 via their own partial unique indexes). The
    device_id-rooted equipment case is still deliberately excluded (see
    module docstring: a device_id alone can't distinguish top-level from
    sub-equipment). The NEW equipment_id case is safe to implement, unlike
    device_id: idx_semantic_entities_equipment_unique guarantees it's
    always unambiguous 1:1, exactly like point/location."""
    if entity_kind == "point" and object_id is not None:
        conn.execute("UPDATE objects SET point_type=? WHERE id=?", (brick_class, object_id))
    elif entity_kind == "location" and location_id is not None:
        conn.execute("UPDATE locations SET kind=? WHERE id=?", (brick_class, location_id))
    elif entity_kind == "equipment" and equipment_id is not None:
        conn.execute("UPDATE equipment SET equipment_type=? WHERE id=?", (brick_class, equipment_id))


def sync_controller_entity(conn: sqlite3.Connection, *, device_id: int, name: str) -> dict:
    """Upserts (never deletes) the entity_kind='controller' semantic
    identity for a device -- a device can't be "un-controllered" once
    explicitly made one. Deliberately has exactly ONE call site in the
    whole codebase: the dedicated POST /devices/{id}/controller endpoint,
    itself only ever invoked by the frontend's explicit Controller-creation
    flow (see src/api/routers/devices.py). NOT wired into
    create_device()/update_device(), and there is no startup backfill for
    it -- a device is the runtime/communication abstraction; Controller is
    a semantic role a device MAY explicitly be given, never inferred or
    bulk-applied (would otherwise silently modify every legacy project the
    instant it's opened or any device is saved)."""
    existing = find_direct_controller_entity(conn, device_id)
    semantic_key = derive_semantic_key("controller", "Controller", device_id=device_id)

    if existing is not None:
        conn.execute(
            "UPDATE semantic_entities SET name=?, semantic_key=? WHERE id=?",
            (name, semantic_key, existing["id"]),
        )
        entity_id = existing["id"]
    else:
        validate_semantic_entity("controller", "Controller", device_id=device_id, object_id=None, location_id=None)
        cur = conn.execute(
            "INSERT INTO semantic_entities "
            "(name, local_slug, semantic_key, brick_class, entity_kind, device_id) "
            "VALUES (?,?,?,?,?,?)",
            (name, None, semantic_key, "Controller", "controller", device_id),
        )
        entity_id = cur.lastrowid

    row = conn.execute("SELECT * FROM semantic_entities WHERE id=?", (entity_id,)).fetchone()
    return dict(row)
