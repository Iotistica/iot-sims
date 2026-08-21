"""Regression test for a real production bug found while investigating
binary-value display inconsistencies: SimEngine.get_object_value() used to
read self._current_values, which is NEVER populated as a per-object dict
in production -- it's only ever assigned wholesale as the /sim/state
snapshot cache ({"devices": [...], "tick": ...}) or {}. This meant
get_object_value() always returned None once the sim had ticked at least
once, silently breaking get_device_point_values(), the Energy Engine,
Fault Detection, and SemanticResolver in the real running app (all masked
in other tests by stub simulation engines).

self._prev_values IS correctly refreshed per-object every tick (set right
before each object's snapshot entry is built) -- that's the real live-value
store. This test encodes the exact production shape of _current_values to
prove get_object_value() no longer depends on it at all."""
from __future__ import annotations

from src.simulation.engine import SimEngine


def test_get_object_value_reads_prev_values_not_current_values(database):
    engine = SimEngine(database)

    # The real shape _current_values takes in production after any tick --
    # NOT a per-object dict. If get_object_value() ever again reads this
    # instead of _prev_values, this test must fail.
    engine._current_values = {"devices": [], "tick": 123.0}
    engine._prev_values[42] = "real-value"

    assert engine.get_object_value(42) == "real-value"


def test_get_object_value_returns_none_for_unknown_object(database):
    engine = SimEngine(database)
    engine._current_values = {"devices": [], "tick": 0.0}
    assert engine.get_object_value(999) is None


def test_resolve_wire_object_current_value_uses_prev_values(database):
    """Same bug, same fix, in resolve_wire_object() (used by packet capture
    to show a captured packet's referenced object's live value)."""
    with database._conn() as conn:
        conn.execute("INSERT INTO devices (device_instance, name) VALUES (901, 'Wire-Test')")
        device_id = conn.execute("SELECT id FROM devices WHERE device_instance=901").fetchone()[0]
        conn.execute(
            "INSERT INTO objects (device_id, object_type, object_instance, name) VALUES (?,?,?,?)",
            (device_id, "analog-input", 1, "Wire-Point"),
        )
        object_id = conn.execute(
            "SELECT id FROM objects WHERE device_id=? AND name='Wire-Point'", (device_id,)
        ).fetchone()[0]
        conn.commit()

    engine = SimEngine(database)
    engine._current_values = {"devices": [], "tick": 0.0}
    engine._prev_values[object_id] = 42.5

    class _FakeBacnetObj:
        objectIdentifier = ("analog-input", 1)

    class _FakeBehavior:
        pass

    engine._objects[object_id] = (_FakeBacnetObj(), _FakeBehavior())

    resolved = engine.resolve_wire_object("analog-input", 1)
    assert resolved is not None
    assert resolved["current_value"] == 42.5
