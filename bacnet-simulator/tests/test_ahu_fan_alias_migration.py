"""migrate_ahu_fan_aliases() (src/semantics/backfill.py): the REAL fix for
existing/already-seeded AHUs still carrying the old, non-canonical
Supply_Fan_Speed_Command/Return_Fan_Speed_Command/Supply_Fan_Status/
Return_Fan_Status point types (removed from POINT_TYPES). Rather than
just displaying these more nicely, this actively rewrites
objects.point_type to the canonical Fan_Speed_Command/Fan_Status and
builds the matching Supply_Fan/Return_Fan sub-equipment entity +
isPartOf + isPointOf relationships -- converging an old AHU onto exactly
the same shape seed_default() creates for a fresh one."""
from __future__ import annotations

from src.semantics.resolver import SemanticResolver


def _make_legacy_tagged_ahu(database, *, instance=951, with_equipment_type=True):
    """Raw SQL insert -- simulates a device/objects that predate Brick
    Core, tagged only with the old flat aliases, no semantic entities."""
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name, equipment_type) VALUES (?,?,?)",
            (instance, "Legacy-AHU", "Air_Handling_Unit" if with_equipment_type else None),
        )
        device_id = conn.execute("SELECT id FROM devices WHERE device_instance=?", (instance,)).fetchone()[0]
        rows = [
            (device_id, "binary-input", 1, "SF-Run", "Supply_Fan_Status"),
            (device_id, "analog-input", 2, "SF-Speed", "Supply_Fan_Speed_Command"),
            (device_id, "binary-input", 3, "RF-Run", "Return_Fan_Status"),
            (device_id, "analog-input", 4, "RF-Speed", "Return_Fan_Speed_Command"),
        ]
        conn.executemany(
            "INSERT INTO objects (device_id, object_type, object_instance, name, point_type) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return device_id


class _StubSimulationEngine:
    def __init__(self, values):
        self._values = values

    def get_object_value(self, object_id):
        return self._values.get(object_id)


def test_setup_rewrites_alias_point_types_to_canonical(database):
    device_id = _make_legacy_tagged_ahu(database)

    database.setup()  # this is what runs the migration

    objects = {o["name"]: o for o in database.get_objects(device_id)}
    assert objects["SF-Run"]["point_type"] == "Fan_Status"
    assert objects["SF-Speed"]["point_type"] == "Fan_Speed_Command"
    assert objects["RF-Run"]["point_type"] == "Fan_Status"
    assert objects["RF-Speed"]["point_type"] == "Fan_Speed_Command"


def test_setup_builds_subequipment_and_relationships(database):
    device_id = _make_legacy_tagged_ahu(database)
    database.setup()

    ahu_entity = database.get_semantic_entities(
        device_id=device_id, entity_kind="equipment", brick_class="Air_Handling_Unit",
    )[0]
    fans = database.get_related_entities(ahu_entity["id"], "isPartOf", direction="in")
    assert {f["brick_class"] for f in fans} == {"Supply_Fan", "Return_Fan"}

    for fan in fans:
        points = database.get_entity_points(fan["id"])
        assert {p["brick_class"] for p in points} == {"Fan_Status", "Fan_Speed_Command"}


def test_migration_is_idempotent_across_repeated_setup(database):
    device_id = _make_legacy_tagged_ahu(database)
    database.setup()

    entities_after_first = database.get_semantic_entities(device_id=device_id)
    database.setup()
    database.setup()
    entities_after_repeat = database.get_semantic_entities(device_id=device_id)

    assert len(entities_after_first) == len(entities_after_repeat)
    objects = {o["name"]: o for o in database.get_objects(device_id)}
    assert objects["SF-Run"]["point_type"] == "Fan_Status"  # still canonical, not re-touched


def test_device_without_equipment_entity_left_untouched(database):
    """No Air_Handling_Unit (or any) top-level equipment entity to attach
    the fan sub-equipment to -- migration must not guess, so the alias
    tags are left exactly as they were."""
    device_id = _make_legacy_tagged_ahu(database, with_equipment_type=False)
    database.setup()

    objects = {o["name"]: o for o in database.get_objects(device_id)}
    assert objects["SF-Run"]["point_type"] == "Supply_Fan_Status"
    assert objects["SF-Speed"]["point_type"] == "Supply_Fan_Speed_Command"
    assert database.get_semantic_entities(device_id=device_id) == []


def test_resolver_resolves_migrated_ahu_via_brick_graph_not_fallback(database):
    device_id = _make_legacy_tagged_ahu(database)
    database.setup()

    objects = {o["name"]: o for o in database.get_objects(device_id)}
    stub = _StubSimulationEngine({
        objects["SF-Run"]["id"]: True,
        objects["SF-Speed"]["id"]: 88.0,
        objects["RF-Run"]["id"]: False,
        objects["RF-Speed"]["id"]: 33.0,
    })
    resolver = SemanticResolver(database=database, simulation_engine=stub)

    fan_points = resolver.resolve_ahu_fans(device_id)
    # All four resolved via the graph (not empty/partial as the pre-
    # migration fallback test scenarios exercise in test_ahu_fans.py).
    assert fan_points == {
        "supply_fan_running": True,
        "supply_fan_speed_percent": 88.0,
        "return_fan_running": False,
        "return_fan_speed_percent": 33.0,
    }
