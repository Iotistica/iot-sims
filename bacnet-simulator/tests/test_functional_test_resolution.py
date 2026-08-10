"""Tests for the graph-aware semantic point resolver
(src/functional_tests/resolution.py). A/B use the plain `database` fixture
with raw INSERTs and no semantic_entities rows at all, genuinely exercising
the flat device_point_type fallback. C/D/E build real semantic_entities/
semantic_relationships rows via the same upsert_semantic_entity/
upsert_semantic_relationship helpers seed_default() itself uses, proving
the graph path is preferred and never silently overridden by the flat scan
when explicit (even ambiguous or inconsistent) graph data exists."""
from __future__ import annotations

from src.functional_tests.resolution import (
    resolve_point_for_device,
    resolve_point_for_equipment_entity,
)
from src.semantics.backfill import upsert_semantic_entity, upsert_semantic_relationship


def _make_device(database, name="HW-Plant", instance=9001):
    with database._conn() as conn:
        conn.execute("INSERT INTO devices (device_instance, name) VALUES (?,?)", (instance, name))
        conn.commit()
        return conn.execute("SELECT id FROM devices WHERE device_instance=?", (instance,)).fetchone()[0]


def _make_object(database, device_id, name, point_type, object_instance):
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO objects (device_id, object_type, object_instance, name, point_type) "
            "VALUES (?,?,?,?,?)",
            (device_id, "binary-input", object_instance, name, point_type),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM objects WHERE device_id=? AND name=?", (device_id, name)
        ).fetchone()[0]


def _make_equipment_entity(database, device_id, name, brick_class, local_slug=None):
    with database._conn() as conn:
        # local_slug disambiguates multiple entities of the SAME brick_class
        # on the SAME device -- semantic_key is derived from
        # (entity_kind, brick_class, device_id, object_id, location_id,
        # local_slug) (src/semantics/keys.py), so two same-class equipment
        # entities on one device with no local_slug would collide and
        # collapse into a single upserted row, exactly the ambiguity this
        # graph-aware resolver exists to distinguish.
        entity = upsert_semantic_entity(
            conn, name=name, brick_class=brick_class, entity_kind="equipment",
            device_id=device_id, local_slug=local_slug,
        )
        conn.commit()
        return entity


def _link_point(database, device_id, object_id, point_name, point_type, equipment_entity):
    with database._conn() as conn:
        point_entity = upsert_semantic_entity(
            conn, name=point_name, brick_class=point_type, entity_kind="point", object_id=object_id,
        )
        upsert_semantic_relationship(conn, point_entity["id"], "isPointOf", equipment_entity["id"])
        conn.commit()
        return point_entity


# ─── A. Simple device, no graph relationship, one match -> flat resolves ─

def test_a_flat_fallback_resolves_single_match(database):
    device_id = _make_device(database)
    _make_object(database, device_id, "BLR-Run", "Run_Status", 1)

    resolution = resolve_point_for_device(database, device_id, "Boiler", "Run_Status")

    assert resolution.status == "resolved"
    assert resolution.resolution_source == "device_point_type"
    assert resolution.object["name"] == "BLR-Run"


# ─── B. Simple device, no graph relationship, two matches -> ambiguous ──

def test_b_flat_fallback_ambiguous_on_two_matches(database):
    device_id = _make_device(database)
    _make_object(database, device_id, "BLR-1-Run", "Run_Status", 1)
    _make_object(database, device_id, "BLR-2-Run", "Run_Status", 2)

    resolution = resolve_point_for_device(database, device_id, "Boiler", "Run_Status")

    assert resolution.status == "ambiguous"
    assert resolution.resolution_source == "device_point_type"
    assert set(resolution.candidates) == {"BLR-1-Run", "BLR-2-Run"}


# ─── C. Two Boiler equipment entities, each with its own Run_Status ─────

def test_c_graph_resolution_scopes_to_the_correct_boiler(database):
    device_id = _make_device(database)

    boiler1 = _make_equipment_entity(database, device_id, "Boiler 1", "Boiler", local_slug="boiler-1")
    boiler2 = _make_equipment_entity(database, device_id, "Boiler 2", "Boiler", local_slug="boiler-2")

    obj1 = _make_object(database, device_id, "BLR-1-Run", "Run_Status", 1)
    obj2 = _make_object(database, device_id, "BLR-2-Run", "Run_Status", 2)

    _link_point(database, device_id, obj1, "BLR-1-Run", "Run_Status", boiler1)
    _link_point(database, device_id, obj2, "BLR-2-Run", "Run_Status", boiler2)

    resolution1 = resolve_point_for_equipment_entity(database, boiler1, "Run_Status")
    resolution2 = resolve_point_for_equipment_entity(database, boiler2, "Run_Status")

    assert resolution1.status == "resolved"
    assert resolution1.resolution_source == "semantic_graph"
    assert resolution1.object["name"] == "BLR-1-Run"

    assert resolution2.status == "resolved"
    assert resolution2.object["name"] == "BLR-2-Run"

    # The device-target wrapper can't disambiguate which Boiler applies
    # (target selection stays device-granular this phase) -- must report
    # ambiguous at the equipment level rather than guessing.
    device_resolution = resolve_point_for_device(database, device_id, "Boiler", "Run_Status")
    assert device_resolution.status == "ambiguous"
    assert device_resolution.resolution_source == "semantic_graph"
    assert set(device_resolution.candidates) == {"Boiler 1", "Boiler 2"}


# ─── D. Explicit graph relationship exists but is ambiguous ─────────────

def test_d_ambiguous_graph_relationship_is_not_overridden_by_flat_fallback(database):
    device_id = _make_device(database)
    boiler = _make_equipment_entity(database, device_id, "Boiler A", "Boiler")

    obj1 = _make_object(database, device_id, "BLR-A-Run-1", "Run_Status", 1)
    obj2 = _make_object(database, device_id, "BLR-A-Run-2", "Run_Status", 2)
    _link_point(database, device_id, obj1, "BLR-A-Run-1", "Run_Status", boiler)
    _link_point(database, device_id, obj2, "BLR-A-Run-2", "Run_Status", boiler)

    resolution = resolve_point_for_equipment_entity(database, boiler, "Run_Status")

    assert resolution.status == "ambiguous"
    assert resolution.resolution_source == "semantic_graph"
    assert resolution.resolution_source != "device_point_type"
    assert set(resolution.candidates) == {"BLR-A-Run-1", "BLR-A-Run-2"}


# ─── E. Graph points to an object whose point_type no longer matches ────

def test_e_semantic_model_inconsistency_is_reported_not_silently_accepted(database):
    device_id = _make_device(database)
    boiler = _make_equipment_entity(database, device_id, "Boiler A", "Boiler")

    obj_id = _make_object(database, device_id, "BLR-A-Run", "Run_Status", 1)
    _link_point(database, device_id, obj_id, "BLR-A-Run", "Run_Status", boiler)

    # Simulate drift: the object's own point_type column no longer agrees
    # with the semantic entity's brick_class (edited independently).
    with database._conn() as conn:
        conn.execute("UPDATE objects SET point_type=? WHERE id=?", ("Zone_Air_Temperature_Sensor", obj_id))
        conn.commit()

    resolution = resolve_point_for_equipment_entity(database, boiler, "Run_Status")

    assert resolution.status == "inconsistent"
    assert resolution.status != "resolved"
    assert resolution.status != "missing"
