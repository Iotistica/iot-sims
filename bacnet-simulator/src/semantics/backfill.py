"""Connection-level primitives for writing semantic_entities/
semantic_relationships rows (upsert_semantic_entity/
upsert_semantic_relationship), plus backfill_semantic_entities() -- a
ONE-TIME MIGRATION MECHANISM for pre-existing data, not the steady-state
workflow. Brick is the semantic source of truth going forward (see
src/semantics/mirror.py); a user classifying a device/object/location
through the ordinary drawer already gets a matching Brick entity created
server-side, automatically, in the same request (mirror.py's
sync_entity_from_flat_field()). backfill_semantic_entities() exists only
to catch up databases/projects that predate that -- rows whose flat tag
was set some other way (an old project import, a direct DB edit, a
pre-Brick-Core deployment) and have no Brick entity yet.

Because of that, backfill is now GATED on absence: for each flat-tagged
row, it first checks whether a direct Brick entity already exists (via
mirror.py's find_direct_equipment_entity/find_point_entity/
find_real_location_entity) and SKIPS the row entirely if one does --
an existing Brick entity is authoritative and must never be overwritten
by a stale flat tag on startup, no matter what the flat tag currently
says. Only genuinely un-migrated rows (no Brick entity at all yet) get
backfilled.

These take a raw sqlite3.Connection (not a Database instance) so callers
that already hold one open -- backfill itself (called from
Database.setup()) and src/legacy.py's seed_default() (which inserts
devices/objects in one long-lived uncommitted transaction) -- can use
them without opening a second connection that wouldn't yet see their own
uncommitted rows.

Non-canonical values (removed aliases like Supply_Fan_Speed_Command, or any
stale/renamed value from a prior schema version) are silently skipped by
backfill_semantic_entities(), not backfilled with an invalid brick_class —
the semantic graph is meant to hold only real Brick classes. The flat tag
column itself is never touched, so the legacy alias-based fallback path
keeps working for skipped rows exactly as before.

backfill_semantic_entities() does NOT create any isPointOf/isPartOf
relationships between the entities it mints — that's a data-modeling
decision that belongs in seed data (see seed_default()'s AHU fan / DALI
zone semantic setup), not a mechanical migration that could fabricate an
incorrect hierarchy.

Called once from Database.setup() with the same live connection setup()
already has open, so it runs on every process start (cheap: it's a
SELECT + existence-check + get-or-create per row, and get-or-create is a
no-op once a matching semantic_key already exists).

migrate_ahu_fan_aliases() (bottom of this file) is a second, more targeted
migration, also run from Database.setup() right after
backfill_semantic_entities(): it actively REWRITES objects.point_type for
the four specific AHU fan aliases removed from POINT_TYPES (rather than
just skipping them, which is all backfill_semantic_entities() does for
non-canonical values), converging existing/already-seeded AHUs onto the
same canonical Supply_Fan/Return_Fan + Fan_Status/Fan_Speed_Command shape
seed_default() creates for a fresh one.
"""
from __future__ import annotations

import sqlite3

from .keys import derive_semantic_key
from .mirror import find_direct_equipment_entity, find_point_entity, find_real_location_entity
from .validation import validate_semantic_entity


def upsert_semantic_entity(
    conn: sqlite3.Connection,
    *,
    name: str,
    brick_class: str,
    entity_kind: str,
    device_id: int | None = None,
    object_id: int | None = None,
    location_id: int | None = None,
    local_slug: str | None = None,
) -> dict:
    """Validates, then get-or-creates by the derived semantic_key via an
    atomic SQLite UPSERT. Raises ValueError (from validate_semantic_entity)
    on an invalid entity_kind/brick_class/FK combination -- callers that
    want to skip invalid rows rather than fail (backfill_semantic_entities)
    catch it themselves; callers writing known-good seed data let it
    propagate."""
    validate_semantic_entity(
        entity_kind, brick_class,
        device_id=device_id, object_id=object_id, location_id=location_id,
    )
    semantic_key = derive_semantic_key(
        entity_kind,
        brick_class,
        device_id=device_id,
        object_id=object_id,
        location_id=location_id,
        local_slug=local_slug,
    )

    # semantic_entities also carries partial unique indexes on object_id
    # (one point entity per object) and location_id (one entity per real
    # location) -- independent of semantic_key, which embeds brick_class
    # and therefore changes whenever the class does. Resolve those FKs to
    # an existing row FIRST, same as mirror.py's direct-sync path, so
    # re-classifying an already-migrated point/location updates that row
    # in place instead of colliding with it: an INSERT whose semantic_key
    # doesn't match the existing row's key (e.g. migrate_ahu_fan_aliases
    # rewriting a point from an old alias brick_class to the canonical
    # one) would otherwise hit the object_id/location_id unique index
    # directly, bypassing the ON CONFLICT(semantic_key) clause below and
    # raising IntegrityError instead of updating.
    existing_id: int | None = None
    if entity_kind == "point" and object_id is not None:
        row = conn.execute(
            "SELECT id FROM semantic_entities WHERE entity_kind='point' AND object_id=?",
            (object_id,),
        ).fetchone()
        existing_id = row["id"] if row else None
    elif entity_kind == "location" and location_id is not None:
        row = conn.execute(
            "SELECT id FROM semantic_entities WHERE entity_kind='location' AND location_id=?",
            (location_id,),
        ).fetchone()
        existing_id = row["id"] if row else None

    if existing_id is not None:
        conn.execute(
            """
            UPDATE semantic_entities SET
                name=?, local_slug=?, semantic_key=?, brick_class=?,
                entity_kind=?, device_id=?, object_id=?, location_id=?
            WHERE id=?
            """,
            (name, local_slug, semantic_key, brick_class, entity_kind,
             device_id, object_id, location_id, existing_id),
        )
        return dict(
            conn.execute(
                "SELECT * FROM semantic_entities WHERE id=?", (existing_id,)
            ).fetchone()
        )

    conn.execute(
        """
        INSERT INTO semantic_entities
            (name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(semantic_key) WHERE semantic_key IS NOT NULL DO UPDATE SET
            name=excluded.name,
            brick_class=excluded.brick_class,
            entity_kind=excluded.entity_kind,
            device_id=excluded.device_id,
            object_id=excluded.object_id,
            location_id=excluded.location_id,
            local_slug=excluded.local_slug
        """,
        (name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id),
    )
    return dict(
        conn.execute(
            "SELECT * FROM semantic_entities WHERE semantic_key=?", (semantic_key,)
        ).fetchone()
    )


def upsert_semantic_relationship(
    conn: sqlite3.Connection,
    source_entity_id: int,
    predicate: str,
    target_entity_id: int,
) -> dict:
    """Re-creating an identical relationship is a no-op returning the
    existing row, not an error -- same idempotency as upsert_semantic_entity."""
    conn.execute(
        "INSERT INTO semantic_relationships (source_entity_id, predicate, target_entity_id) "
        "VALUES (?,?,?) "
        "ON CONFLICT(source_entity_id, predicate, target_entity_id) DO NOTHING",
        (source_entity_id, predicate, target_entity_id),
    )
    return dict(
        conn.execute(
            "SELECT * FROM semantic_relationships "
            "WHERE source_entity_id=? AND predicate=? AND target_entity_id=?",
            (source_entity_id, predicate, target_entity_id),
        ).fetchone()
    )


def backfill_semantic_entities(conn: sqlite3.Connection) -> None:
    for row in conn.execute(
        "SELECT id, name, equipment_type FROM devices WHERE equipment_type IS NOT NULL"
    ):
        if find_direct_equipment_entity(conn, row["id"]) is not None:
            continue  # already migrated (or user-assigned) -- never overwritten by a stale flat tag
        try:
            upsert_semantic_entity(
                conn,
                name=row["name"],
                brick_class=row["equipment_type"],
                entity_kind="equipment",
                device_id=row["id"],
            )
        except ValueError:
            continue

    for row in conn.execute(
        "SELECT id, name, point_type FROM objects WHERE point_type IS NOT NULL"
    ):
        if find_point_entity(conn, row["id"]) is not None:
            continue
        try:
            upsert_semantic_entity(
                conn,
                name=row["name"],
                brick_class=row["point_type"],
                entity_kind="point",
                object_id=row["id"],
            )
        except ValueError:
            continue

    for row in conn.execute(
        "SELECT id, name, kind FROM locations WHERE kind IS NOT NULL"
    ):
        if find_real_location_entity(conn, row["id"]) is not None:
            continue
        try:
            upsert_semantic_entity(
                conn,
                name=row["name"],
                brick_class=row["kind"],
                entity_kind="location",
                location_id=row["id"],
            )
        except ValueError:
            continue


# Old, non-canonical AHU fan point-type alias -> (canonical point class,
# fan equipment class, local_slug). All four were removed from POINT_TYPES
# by the Brick Core migration (src/core/config.py) -- see the comment
# there. Kept here, not in config.py, because this is migration-specific
# data, not current vocabulary.
_AHU_FAN_ALIAS_MIGRATIONS: dict[str, tuple[str, str, str]] = {
    "Supply_Fan_Speed_Command": ("Fan_Speed_Command", "Supply_Fan", "supply-fan"),
    "Return_Fan_Speed_Command": ("Fan_Speed_Command", "Return_Fan", "return-fan"),
    "Supply_Fan_Status": ("Fan_Status", "Supply_Fan", "supply-fan"),
    "Return_Fan_Status": ("Fan_Status", "Return_Fan", "return-fan"),
}


def migrate_ahu_fan_aliases(conn: sqlite3.Connection) -> None:
    """One-time, idempotent DATA migration (not just a semantic_entities
    backfill) for databases/projects seeded before Brick Core: any object
    still carrying one of the four now-removed alias point types (see
    _AHU_FAN_ALIAS_MIGRATIONS) is rewritten in place to the canonical
    Fan_Speed_Command/Fan_Status, and a matching Supply_Fan/Return_Fan
    sub-equipment entity + isPartOf + isPointOf relationships are built --
    exactly what seed_default() creates for a fresh AHU (src/legacy.py),
    so an existing, already-seeded AHU converges to the same shape instead
    of being left permanently on the old, ambiguous alias tags.

    Must run AFTER backfill_semantic_entities() in the same
    Database.setup() call: it needs the device's own top-level equipment
    entity (e.g. the AHU's Air_Handling_Unit entity) to already exist --
    via find_direct_equipment_entity() -- so it has something to attach
    the fan sub-equipment to. If a device has no such entity (its
    equipment_type isn't set, or isn't canonical), its fan objects are
    left untouched rather than guessing what to attach them to; they'll
    be picked up automatically on a later run once that's resolved.

    Idempotent by construction, not by a separate existence check: once
    an object's point_type is rewritten to the canonical value, it no
    longer matches any alias key in _AHU_FAN_ALIAS_MIGRATIONS, so
    re-running this on every Database.setup() is a no-op for already-
    migrated objects.
    """
    for old_alias, (canonical, fan_class, local_slug) in _AHU_FAN_ALIAS_MIGRATIONS.items():
        for row in conn.execute(
            "SELECT id, device_id, name FROM objects WHERE point_type=?", (old_alias,)
        ):
            device_row = conn.execute(
                "SELECT id, name FROM devices WHERE id=?", (row["device_id"],)
            ).fetchone()
            if device_row is None:
                continue

            ahu_entity = find_direct_equipment_entity(conn, device_row["id"])
            if ahu_entity is None:
                continue  # nothing sensible to attach the fan to yet -- leave untouched

            try:
                fan_entity = upsert_semantic_entity(
                    conn,
                    name=f"{device_row['name']} {fan_class.replace('_', ' ')}",
                    brick_class=fan_class,
                    entity_kind="equipment",
                    device_id=device_row["id"],
                    local_slug=local_slug,
                )
            except ValueError:
                continue  # fan_class somehow not canonical -- shouldn't happen, skip defensively

            upsert_semantic_relationship(conn, fan_entity["id"], "isPartOf", ahu_entity["id"])

            conn.execute("UPDATE objects SET point_type=? WHERE id=?", (canonical, row["id"]))

            try:
                point_entity = upsert_semantic_entity(
                    conn,
                    name=row["name"],
                    brick_class=canonical,
                    entity_kind="point",
                    object_id=row["id"],
                )
            except ValueError:
                continue

            upsert_semantic_relationship(conn, point_entity["id"], "isPointOf", fan_entity["id"])
