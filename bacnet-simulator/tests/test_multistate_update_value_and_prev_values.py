"""engine.py's _update_value() now returns the exact value it wrote to
the live BACnet presentValue (post type-coercion/clamping), and every
caller that separately tracks a "current value" (the tick loop's
_prev_values / Admin UI payload, inject_mirror_values, set_manual_value)
uses that return instead of re-deriving its own -- see _update_value's
own docstring for why.

Core regression: before this fix, a multi-state object's tracked
"current value" (what /sim/state and the Admin UI read) could disagree
with the actual BACnet Present_Value whenever the raw source value fell
outside [1, numberOfStates] -- e.g. a zero-based FMU state feeding a
multi-state point directly showed 0 in /sim/state while a real BACnet
client reading Present_Value got 1 (the clamped floor). These tests
construct a real bacpypes3 multi-state object directly (mirroring
engine.py's own _create_object) and drive it through set_manual_value(),
which needs no HTTP object-creation route -- see test_sim_engine_
object_value.py for the same raw-DB-insert convention, used here because
the objects HTTP route can't be exercised on this dev machine (discovery
router import chain requires `fcntl`, Unix-only) -- unrelated to this
change, confirmed by the same failure on unmodified existing tests.
"""
from __future__ import annotations

from bacpypes3.local.multistate import MultiStateInputObject

from src.simulation.engine import SimEngine


def _insert_multistate_object(database, *, device_instance: int, number_of_states: int) -> tuple[int, int]:
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name) VALUES (?, ?)",
            (device_instance, f"Device-{device_instance}"),
        )
        device_id = conn.execute(
            "SELECT id FROM devices WHERE device_instance=?", (device_instance,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO objects "
            "(device_id, object_type, object_instance, name, number_of_states) "
            "VALUES (?, 'multi-state-input', 1, 'Compressor-Stage', ?)",
            (device_id, number_of_states),
        )
        object_id = conn.execute(
            "SELECT id FROM objects WHERE device_id=? AND name='Compressor-Stage'",
            (device_id,),
        ).fetchone()[0]
        conn.commit()
    return device_id, object_id


def _bind_live_multistate_object(engine: SimEngine, object_id: int, number_of_states: int) -> MultiStateInputObject:
    """Mirrors engine.py's own _create_object for MULTISTATE_TYPES --
    constructs the same real bacpypes3 object class it would, and
    registers it into engine._objects the same way, without needing a
    running BACnet application."""
    bacnet_obj = MultiStateInputObject(
        objectIdentifier=f"multi-state-input,{object_id}",
        objectName="Compressor-Stage",
        presentValue=1,
        numberOfStates=number_of_states,
    )

    class _NoOpBehavior:
        pass

    engine._objects[object_id] = (bacnet_obj, _NoOpBehavior())
    return bacnet_obj


def test_update_value_clamps_and_returns_the_written_multistate_value(database):
    engine = SimEngine(database)
    _, object_id = _insert_multistate_object(database, device_instance=7401, number_of_states=3)
    bacnet_obj = _bind_live_multistate_object(engine, object_id, number_of_states=3)

    # A pre-converted value (as FMUSimulationProvider._convert_output_value
    # would already have produced via zero_based_to_multistate) lands
    # exactly on the wire, unchanged.
    written = engine._update_value(bacnet_obj, "multi-state-input", 2)
    assert written == 2
    assert int(bacnet_obj.presentValue) == 2

    # An out-of-range/unconverted raw value (e.g. a source that was never
    # given a conversion) is still floored to 1, same as before this fix
    # -- _update_value's own clamp is unchanged, only its return value is
    # new.
    written_floor = engine._update_value(bacnet_obj, "multi-state-input", 0)
    assert written_floor == 1
    assert int(bacnet_obj.presentValue) == 1

    written_ceiling = engine._update_value(bacnet_obj, "multi-state-input", 99)
    assert written_ceiling == 3
    assert int(bacnet_obj.presentValue) == 3


def test_set_manual_value_prev_values_matches_actual_present_value_for_multistate(database):
    """The exact /sim/state inconsistency reported: setting a multi-state
    point to a raw value outside [1, numberOfStates] must leave
    _prev_values (what get_object_value()/the Admin UI read) equal to the
    real, clamped Present_Value -- not the raw pre-clamp input."""
    engine = SimEngine(database)
    _, object_id = _insert_multistate_object(database, device_instance=7402, number_of_states=3)
    bacnet_obj = _bind_live_multistate_object(engine, object_id, number_of_states=3)

    ok = engine.set_manual_value(object_id, 0)
    assert ok is True

    actual_present_value = int(bacnet_obj.presentValue)
    assert actual_present_value == 1  # engine's own multi-state floor
    assert engine.get_object_value(object_id) == actual_present_value
    assert engine.get_object_value(object_id) == 1  # not 0


def test_set_manual_value_prev_values_matches_present_value_for_already_valid_state(database):
    engine = SimEngine(database)
    _, object_id = _insert_multistate_object(database, device_instance=7403, number_of_states=3)
    bacnet_obj = _bind_live_multistate_object(engine, object_id, number_of_states=3)

    engine.set_manual_value(object_id, 2)

    assert int(bacnet_obj.presentValue) == 2
    assert engine.get_object_value(object_id) == 2
