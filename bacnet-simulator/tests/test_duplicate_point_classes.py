"""General regression guard for the collapsing-dict bug this migration
exists to fix: N objects on one device sharing a point_type.

SimEngine.get_device_point_values() (src/legacy.py) is deliberately left
completely unchanged by this migration -- this test proves its old
last-write-wins contract wasn't silently altered. SemanticResolver.
resolve_device_points() is the new, additive, multi-match replacement."""
from __future__ import annotations

from src.legacy import SimEngine
from src.semantics.resolver import SemanticResolver


class _StubSimulationEngine:
    def __init__(self, values: dict[int, object]):
        self._values = values

    def get_object_value(self, object_id: int):
        return self._values.get(object_id)


def _make_device_with_duplicate_points(database):
    with database._conn() as conn:
        conn.execute("INSERT INTO devices (device_instance, name) VALUES (101, 'Dup-Device')")
        device_id = conn.execute("SELECT id FROM devices WHERE device_instance=101").fetchone()[0]
        for i, point_name in enumerate(("Point-A", "Point-B", "Point-C"), start=1):
            conn.execute(
                "INSERT INTO objects (device_id, object_type, object_instance, name, point_type) VALUES (?,?,?,?,?)",
                (device_id, "binary-input", i, point_name, "Run_Status"),
            )
        conn.commit()
    return device_id


def test_get_device_point_values_still_overwrites(database):
    device_id = _make_device_with_duplicate_points(database)
    objects = database.get_objects(device_id)

    sim_engine = SimEngine(database)
    # _prev_values is the real per-object live-value store (refreshed every
    # tick) -- see tests/test_sim_engine_object_value.py for why this is
    # _prev_values and NOT _current_values (which is only ever the whole
    # /sim/state snapshot, never per-object, in production).
    for i, obj in enumerate(objects):
        sim_engine._prev_values[obj["id"]] = f"value-{i}"

    values = sim_engine.get_device_point_values(objects)

    assert list(values.keys()) == ["Run_Status"]
    # Last object processed wins -- the exact pre-migration behavior,
    # unchanged.
    assert values["Run_Status"] == f"value-{len(objects) - 1}"


def test_resolve_device_points_returns_every_match(database):
    device_id = _make_device_with_duplicate_points(database)
    objects = database.get_objects(device_id)

    stub = _StubSimulationEngine({obj["id"]: f"value-{i}" for i, obj in enumerate(objects)})
    resolver = SemanticResolver(database=database, simulation_engine=stub)

    resolved = resolver.resolve_device_points(device_id)

    assert list(resolved.keys()) == ["Run_Status"]
    assert len(resolved["Run_Status"]) == 3
    assert {p.value for p in resolved["Run_Status"]} == {"value-0", "value-1", "value-2"}
    assert {p.object_name for p in resolved["Run_Status"]} == {"Point-A", "Point-B", "Point-C"}
