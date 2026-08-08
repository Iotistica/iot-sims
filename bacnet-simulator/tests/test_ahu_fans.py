"""AHU supply/return fan differentiation via SemanticResolver +
build_ahu_snapshot's per-field fallback -- the core scenario this
migration exists to fix."""
from __future__ import annotations

from src.energy.context import build_ahu_snapshot
from src.semantics.resolver import SemanticResolver


class _StubSimulationEngine:
    def __init__(self, values: dict[int, object]):
        self._values = values

    def get_object_value(self, object_id: int):
        return self._values.get(object_id)


def test_seeded_ahu_fans_resolve_distinct_values(seeded_database):
    devices = seeded_database.get_devices()
    ahu1_id = next(d["id"] for d in devices if d["device_instance"] == 1003)
    objects = seeded_database.get_objects(ahu1_id)
    by_name = {o["name"]: o for o in objects}

    stub = _StubSimulationEngine({
        by_name["SF-Run"]["id"]: True,
        by_name["SF-Speed"]["id"]: 82.0,
        by_name["RF-Run"]["id"]: False,
        by_name["RF-Speed"]["id"]: 41.0,
    })
    resolver = SemanticResolver(database=seeded_database, simulation_engine=stub)

    fan_points = resolver.resolve_ahu_fans(ahu1_id)
    assert fan_points == {
        "supply_fan_running": True,
        "supply_fan_speed_percent": 82.0,
        "return_fan_running": False,
        "return_fan_speed_percent": 41.0,
    }

    values = {}  # legacy flat dict irrelevant here, all 4 fields resolved via entities
    snapshot = build_ahu_snapshot(values, fan_points=fan_points)
    assert snapshot.supply_fan_running is True
    assert snapshot.supply_fan_speed_percent == 82.0
    assert snapshot.return_fan_running is False
    assert snapshot.return_fan_speed_percent == 41.0


def _make_ahu_device(database, *, with_supply_fan_entity: bool, legacy_alias_return: bool):
    """Builds a minimal AHU device, optionally with ONLY a Supply_Fan
    semantic entity (no Return_Fan), and optionally with the return fan
    still tagged via the legacy pre-migration alias point types instead of
    canonical Fan_Status/Fan_Speed_Command."""
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name, equipment_type) VALUES (?,?,?)",
            (301, "AHU-Test", "Air_Handling_Unit"),
        )
        device_id = conn.execute("SELECT id FROM devices WHERE device_instance=301").fetchone()[0]

        return_run_type = "Return_Fan_Status" if legacy_alias_return else "Fan_Status"
        return_speed_type = "Return_Fan_Speed_Command" if legacy_alias_return else "Fan_Speed_Command"

        rows = [
            (device_id, "binary-input", 1, "SF-Run", "Fan_Status"),
            (device_id, "analog-input", 2, "SF-Speed", "Fan_Speed_Command"),
            (device_id, "binary-input", 3, "RF-Run", return_run_type),
            (device_id, "analog-input", 4, "RF-Speed", return_speed_type),
        ]
        conn.executemany(
            "INSERT INTO objects (device_id, object_type, object_instance, name, point_type) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()

    if with_supply_fan_entity:
        ahu_entity = database.create_semantic_entity(
            "AHU-Test", "Air_Handling_Unit", "equipment", device_id=device_id,
        )
        supply_fan = database.create_semantic_entity(
            "AHU-Test Supply Fan", "Supply_Fan", "equipment",
            device_id=device_id, local_slug="supply-fan",
        )
        database.create_semantic_relationship(supply_fan["id"], "isPartOf", ahu_entity["id"])

        objects = database.get_objects(device_id)
        by_name = {o["name"]: o for o in objects}
        for point_name, brick_class in (("SF-Run", "Fan_Status"), ("SF-Speed", "Fan_Speed_Command")):
            point_entity = database.create_semantic_entity(
                point_name, brick_class, "point", object_id=by_name[point_name]["id"],
            )
            database.create_semantic_relationship(point_entity["id"], "isPointOf", supply_fan["id"])

    return device_id


def test_partial_migration_mixes_entity_and_legacy_fallback(database):
    """Only Supply_Fan has semantic entities; the return side is tagged
    with the pre-migration alias point types. build_ahu_snapshot should
    resolve supply fields via the entity graph AND return fields via the
    legacy alias fallback, in the same snapshot."""
    device_id = _make_ahu_device(database, with_supply_fan_entity=True, legacy_alias_return=True)
    objects = database.get_objects(device_id)
    by_name = {o["name"]: o for o in objects}

    stub = _StubSimulationEngine({
        by_name["SF-Run"]["id"]: True,
        by_name["SF-Speed"]["id"]: 90.0,
        by_name["RF-Run"]["id"]: True,
        by_name["RF-Speed"]["id"]: 55.0,
    })
    resolver = SemanticResolver(database=database, simulation_engine=stub)

    fan_points = resolver.resolve_ahu_fans(device_id)
    # Only the supply side resolved via the graph -- return_fan_* keys absent.
    assert fan_points == {
        "supply_fan_running": True,
        "supply_fan_speed_percent": 90.0,
    }

    # Legacy flat dict as SimEngine.get_device_point_values() would build it
    # (last write wins, but there's only one object per legacy alias key
    # here so no collision to worry about for this test).
    legacy_values = {
        "Return_Fan_Status": stub.get_object_value(by_name["RF-Run"]["id"]),
        "Return_Fan_Speed_Command": stub.get_object_value(by_name["RF-Speed"]["id"]),
    }

    snapshot = build_ahu_snapshot(legacy_values, fan_points=fan_points)
    assert snapshot.supply_fan_running is True
    assert snapshot.supply_fan_speed_percent == 90.0
    # Resolved via the legacy alias fallback, not the (nonexistent) graph path.
    assert snapshot.return_fan_running is True
    assert snapshot.return_fan_speed_percent == 55.0


def test_no_semantic_entities_falls_back_fully(database):
    """A device with no semantic entities at all resolves entirely via the
    legacy alias lookup, exactly as before Brick Core -- resolve_ahu_fans()
    returns an empty dict (nothing to walk from), not a partial/broken one."""
    device_id = _make_ahu_device(database, with_supply_fan_entity=False, legacy_alias_return=True)
    objects = database.get_objects(device_id)
    by_name = {o["name"]: o for o in objects}

    stub = _StubSimulationEngine({
        by_name["SF-Run"]["id"]: True,
        by_name["SF-Speed"]["id"]: 70.0,
        by_name["RF-Run"]["id"]: False,
        by_name["RF-Speed"]["id"]: 30.0,
    })
    resolver = SemanticResolver(database=database, simulation_engine=stub)

    fan_points = resolver.resolve_ahu_fans(device_id)
    assert fan_points == {}

    # SF-Run/SF-Speed are tagged with the CANONICAL Fan_Status/
    # Fan_Speed_Command point types in this fixture, not the legacy
    # Supply_Fan_Status/Supply_Fan_Speed_Command aliases -- so the legacy
    # flat-dict fallback (which only ever looks for the alias keys) can't
    # see them either. This is expected: a device tagged with the new
    # canonical point types but with NO semantic entities is not a
    # scenario the pre-Brick-Core code path was ever designed to resolve.
    legacy_values = {
        "Return_Fan_Status": stub.get_object_value(by_name["RF-Run"]["id"]),
        "Return_Fan_Speed_Command": stub.get_object_value(by_name["RF-Speed"]["id"]),
    }
    snapshot = build_ahu_snapshot(legacy_values, fan_points=fan_points)
    assert snapshot.supply_fan_running is None
    assert snapshot.supply_fan_speed_percent is None
    assert snapshot.return_fan_running is False
    assert snapshot.return_fan_speed_percent == 30.0
