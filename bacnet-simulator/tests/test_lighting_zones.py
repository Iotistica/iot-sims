"""DALI multi-zone point resolution via SemanticResolver +
build_lighting_snapshot's per-field fallback."""
from __future__ import annotations

from src.energy.context import build_lighting_snapshot
from src.semantics.resolver import SemanticResolver


class _StubSimulationEngine:
    def __init__(self, values: dict[int, object]):
        self._values = values

    def get_object_value(self, object_id: int):
        return self._values.get(object_id)


def test_seeded_dali_zones_resolve_distinct_values_via_entity(seeded_database):
    devices = seeded_database.get_devices()
    gw1_id = next(d["id"] for d in devices if d["device_instance"] == 1501)
    objects = seeded_database.get_objects(gw1_id)
    by_name = {o["name"]: o for o in objects}

    stub = _StubSimulationEngine({
        by_name["Zone-A-Power"]["id"]: 2.1,
        by_name["Zone-A-Lights-On"]["id"]: True,
        by_name["Zone-A-Dim-Level"]["id"]: 80.0,
        by_name["Zone-B-Power"]["id"]: 3.4,
        by_name["Zone-B-Lights-On"]["id"]: False,
        by_name["Zone-B-Dim-Level"]["id"]: 20.0,
    })
    resolver = SemanticResolver(database=seeded_database, simulation_engine=stub)

    snapshot_a = build_lighting_snapshot(
        objects=objects, simulation_engine=stub, instance_key="zone-a",
        resolver=resolver, device_id=gw1_id,
    )
    snapshot_b = build_lighting_snapshot(
        objects=objects, simulation_engine=stub, instance_key="zone-b",
        resolver=resolver, device_id=gw1_id,
    )

    assert snapshot_a.measured_power_kw == 2.1
    assert snapshot_a.on is True
    assert snapshot_a.lighting_level_percent == 80.0

    assert snapshot_b.measured_power_kw == 3.4
    assert snapshot_b.on is False
    assert snapshot_b.lighting_level_percent == 20.0


def _make_dali_device(database, *, with_zone_a_entity: bool):
    with database._conn() as conn:
        conn.execute("INSERT INTO devices (device_instance, name) VALUES (401, 'DALI-Test')")
        device_id = conn.execute("SELECT id FROM devices WHERE device_instance=401").fetchone()[0]
        rows = [
            (device_id, "analog-input", 1, "Zone-A-Power", "Power_Sensor"),
            (device_id, "binary-value", 2, "Zone-A-Lights-On", "On_Off_Command"),
            (device_id, "analog-value", 3, "Zone-A-Dim-Level", "Lighting_Level_Command"),
            (device_id, "analog-input", 4, "Zone-B-Power", None),
            (device_id, "binary-value", 5, "Zone-B-Lights-On", None),
            (device_id, "analog-value", 6, "Zone-B-Dim-Level", None),
        ]
        conn.executemany(
            "INSERT INTO objects (device_id, object_type, object_instance, name, point_type) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()

    if with_zone_a_entity:
        zone_a = database.create_semantic_entity(
            "Zone A", "Lighting_Zone", "location", device_id=device_id, local_slug="zone-a",
        )
        objects = database.get_objects(device_id)
        by_name = {o["name"]: o for o in objects}
        for point_name, brick_class in (
            ("Zone-A-Power", "Power_Sensor"),
            ("Zone-A-Lights-On", "On_Off_Command"),
            ("Zone-A-Dim-Level", "Lighting_Level_Command"),
        ):
            point_entity = database.create_semantic_entity(
                point_name, brick_class, "point", object_id=by_name[point_name]["id"],
            )
            database.create_semantic_relationship(point_entity["id"], "isPointOf", zone_a["id"])

    return device_id


def test_partial_migration_mixes_entity_and_legacy_name_scan(database):
    """Zone A has a Lighting_Zone entity; Zone B is untagged and must be
    resolved via the legacy Zone-B-* object-name-prefix scan."""
    device_id = _make_dali_device(database, with_zone_a_entity=True)
    objects = database.get_objects(device_id)
    by_name = {o["name"]: o for o in objects}

    stub = _StubSimulationEngine({
        by_name["Zone-A-Power"]["id"]: 1.5,
        by_name["Zone-A-Lights-On"]["id"]: True,
        by_name["Zone-A-Dim-Level"]["id"]: 60.0,
        by_name["Zone-B-Power"]["id"]: 2.7,
        by_name["Zone-B-Lights-On"]["id"]: True,
        by_name["Zone-B-Dim-Level"]["id"]: 45.0,
    })
    resolver = SemanticResolver(database=database, simulation_engine=stub)

    snapshot_a = build_lighting_snapshot(
        objects=objects, simulation_engine=stub, instance_key="zone-a",
        resolver=resolver, device_id=device_id,
    )
    snapshot_b = build_lighting_snapshot(
        objects=objects, simulation_engine=stub, instance_key="zone-b",
        resolver=resolver, device_id=device_id,
    )

    assert snapshot_a.measured_power_kw == 1.5
    assert snapshot_a.on is True
    assert snapshot_a.lighting_level_percent == 60.0

    # Zone B has no entity -- resolved via the legacy name-prefix scan.
    assert snapshot_b.measured_power_kw == 2.7
    assert snapshot_b.on is True
    assert snapshot_b.lighting_level_percent == 45.0


def test_no_resolver_falls_back_to_legacy_scan_unchanged(database):
    device_id = _make_dali_device(database, with_zone_a_entity=False)
    objects = database.get_objects(device_id)
    by_name = {o["name"]: o for o in objects}

    stub = _StubSimulationEngine({
        by_name["Zone-A-Power"]["id"]: 1.1,
        by_name["Zone-A-Lights-On"]["id"]: False,
        by_name["Zone-A-Dim-Level"]["id"]: 10.0,
    })

    snapshot = build_lighting_snapshot(
        objects=objects, simulation_engine=stub, instance_key="zone-a",
    )
    assert snapshot.measured_power_kw == 1.1
    assert snapshot.on is False
    assert snapshot.lighting_level_percent == 10.0
