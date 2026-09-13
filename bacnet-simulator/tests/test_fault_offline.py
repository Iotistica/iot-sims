"""FaultBehavior's `offline` fault type: a BACnet Reliability/Status_Flags fault, NOT a value
fault. Before this change, `offline` hard-coded Present_Value to 0.0 -- semantically wrong (0 is
a legitimate reading for plenty of real points: 0 CFM airflow, 0% damper position -- and
conflating "unavailable" with "reads as zero" is exactly the wrong signal for FDD training).

New semantics:
  - Present_Value keeps reflecting the live base signal, unchanged, for as long as offline is
    active (same "never freeze" treatment bias/drift/delay already get) -- see behaviors.py's
    FaultBehavior.compute() and its class docstring.
  - The actual unavailability signal is BACnet Reliability=communication-failure +
    Status_Flags.fault=1, applied every tick by SimEngine.tick() via the new
    `_offline_reliability_override()` pure function (engine.py) feeding the pre-existing
    `_apply_reliability()` helper (bacnet/app.py) -- the SAME helper _create_object() already
    used once, at object creation, for an object's own persisted `reliability` field.
  - Recovery / switching to a different fault_type / Behavior != "fault" all self-correct with
    NO dedicated cleanup code: `_offline_reliability_override` always falls through to restoring
    the object's own configured `reliability` unless offline is active on THIS exact tick.

Two things are tested here at different levels, mirroring how this feature is actually
implemented across two files:
  1. FaultBehavior.compute()'s own Present_Value passthrough (mirrors test_fault_bias.py's
     conventions -- direct make_behavior() construction, deterministic activation).
  2. `_offline_reliability_override()` (engine.py) -- a pure function, unit-tested directly
     without running a full engine tick, PLUS one test that also feeds its result through the
     real `_apply_reliability()` against a real bacpypes3 object built by `SimEngine._create_object`
     (same pattern test_fault_restore.py's own test_restore_to_raw_does_not_reset_provider_owned_
     point_to_zero already uses for exercising _create_object directly) -- this is the actual
     BACnet-client-visible effect, not just internal Python state.
"""
from __future__ import annotations

import json

from src.bacnet import object_factory
from src.bacnet.app import _apply_reliability
from src.simulation.behaviors import make_behavior
from src.simulation.engine import SimEngine, _offline_reliability_override
from src.simulation.state import SimState


def _offline_behavior(*, activation_mode="always", base_value=20, mtbf_minutes=60, fault_duration_seconds=30):
    return make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": base_value},
        "fault_type": "offline", "activation_mode": activation_mode,
        "mtbf_minutes": mtbf_minutes, "fault_duration_seconds": fault_duration_seconds,
    }))


def _force_active(behavior, *, until: float = 999999.0) -> None:
    behavior._fault_active = True
    behavior._fault_end_elapsed = until


# ═══════════════════════════════════════════════════════════════════════════
# FaultBehavior.compute(): Present_Value is a live passthrough, never 0/frozen
# ═══════════════════════════════════════════════════════════════════════════

def test_offline_active_does_not_force_present_value_to_zero():
    """The core semantic fix: 0 is a legitimate reading (0 CFM, 0% damper) -- offline must never
    be indistinguishable from "the value is actually zero". Present_Value keeps reporting the
    live base value while offline is active."""
    behavior = _offline_behavior(base_value=20)
    result = behavior.compute(SimState(elapsed_seconds=0.0))
    assert result == 20.0
    assert result != 0.0


def test_offline_active_tracks_a_dynamic_noise_base_instead_of_freezing():
    behavior = make_behavior("fault", json.dumps({
        "base_behavior": "noise", "base_params": {"base": 20.0, "noise": 0.0},
        "fault_type": "offline", "activation_mode": "always",
    }))
    first = behavior.compute(SimState(elapsed_seconds=0.0))
    assert first == 20.0
    behavior._inner.base = 19.7  # type: ignore[attr-defined]
    second = behavior.compute(SimState(elapsed_seconds=5.0))
    assert second == 19.7
    assert second != first


# ═══════════════════════════════════════════════════════════════════════════
# 1-2. Activation integration: Always Active / Intermittent
# ═══════════════════════════════════════════════════════════════════════════

def test_offline_always_active_ignores_mtbf_and_stays_active_continuously():
    behavior = _offline_behavior(activation_mode="always", mtbf_minutes=10_000_000, base_value=20)
    behavior.compute(SimState(elapsed_seconds=0.0))
    assert behavior._fault_active is True
    # No automatic expiry despite fault_duration_seconds=30 -- see FaultBehavior's own "always"
    # activation branch (shared by every fault_type, unchanged for this task).
    behavior.compute(SimState(elapsed_seconds=3600.0))
    assert behavior._fault_active is True
    assert behavior._fault_end_elapsed == float("inf")


def test_offline_intermittent_only_active_during_the_existing_fault_window():
    behavior = _offline_behavior(activation_mode="intermittent")
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 20.0
    assert behavior._fault_active is False  # never triggered by the mtbf roll yet

    _force_active(behavior, until=20.0)
    assert _offline_reliability_override(behavior, "no-fault-detected") == "communication-failure"

    behavior.compute(SimState(elapsed_seconds=25.0))  # past fault_end -- expires
    assert behavior._fault_active is False
    assert _offline_reliability_override(behavior, "no-fault-detected") == "no-fault-detected"


# ═══════════════════════════════════════════════════════════════════════════
# 3-5. _offline_reliability_override: recovery / switching away / Behavior != fault
# ═══════════════════════════════════════════════════════════════════════════

def test_offline_reliability_override_reports_communication_failure_while_active():
    behavior = _offline_behavior(activation_mode="always")
    behavior.compute(SimState(elapsed_seconds=0.0))  # activates
    assert _offline_reliability_override(behavior, "no-fault-detected") == "communication-failure"


def test_offline_reliability_override_restores_configured_reliability_on_recovery():
    """3. Recovery: when the fault ends, Reliability returns to the object's own configured
    value -- no stale offline flag left behind, no dedicated cleanup path needed."""
    behavior = _offline_behavior(activation_mode="intermittent")
    _force_active(behavior, until=10.0)
    assert _offline_reliability_override(behavior, "over-range") == "communication-failure"

    behavior.compute(SimState(elapsed_seconds=15.0))  # past fault_end -- expires
    assert behavior._fault_active is False
    assert _offline_reliability_override(behavior, "over-range") == "over-range"


def test_offline_reliability_override_clears_on_switching_fault_type_away_from_offline():
    """4. Switching offline -> another fault_type must clear offline-specific protocol state --
    achieved for free here since _offline_reliability_override only ever returns
    "communication-failure" when fault_type == "offline" AND active."""
    bias = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "bias",
        "fault_value": 2.0, "activation_mode": "always",
    }))
    bias.compute(SimState(elapsed_seconds=0.0))  # active bias, not offline
    assert _offline_reliability_override(bias, "no-fault-detected") == "no-fault-detected"


def test_offline_reliability_override_is_never_applied_when_behavior_is_not_fault():
    """5. Behavior != fault: a non-FaultBehavior instance (or no live behavior at all, e.g. a
    mirror/replay/linked point whose Behavior stays inactive -- see engine.py's tick()) must
    never report offline, regardless of what's stored in the object's own `reliability` field."""
    noise = make_behavior("noise", json.dumps({"base": 20.0, "noise": 0.0}))
    assert _offline_reliability_override(noise, "no-fault-detected") == "no-fault-detected"
    assert _offline_reliability_override(None, "over-range") == "over-range"


# ═══════════════════════════════════════════════════════════════════════════
# 6. Persistence/API round trip
# ═══════════════════════════════════════════════════════════════════════════

def test_offline_fault_config_round_trips_through_persistence(client, database):
    """Storage-layer round trip -- same database.create_object()/get_object() pattern as
    test_fault_bias.py/test_fault_drift.py/test_fault_delay.py's own equivalent tests. Confirms
    an existing `fault_type: "offline"` config (with no schema change needed at all -- offline
    has no new persisted fields, unlike bias/drift/delay) still loads correctly."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9406, "name": "Offline-Device"}).json()
    created = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 1, "name": "Zone-Temp", "units": "degrees-celsius",
        "behavior": "fault", "behavior_params": json.dumps({
            "base_behavior": "noise", "base_params": {"base": 20.0, "noise": 0.3},
            "fault_type": "offline", "activation_mode": "always",
            "mtbf_minutes": 60, "fault_duration_seconds": 30,
        }),
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal",
        "point_type": None,
    })

    fetched = database.get_object(created["id"])
    params = json.loads(fetched["behavior_params"])
    assert params["fault_type"] == "offline"
    assert params["activation_mode"] == "always"


def test_legacy_offline_config_without_activation_mode_still_loads():
    """An offline config saved before activation_mode existed (spike/stuck/offline were the
    ORIGINAL three fault types, long before this session's bias/drift/delay/activation-mode
    work) -- must still construct without error and default to intermittent."""
    behavior = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "offline",
        "fault_value": 999, "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    assert behavior.activation_mode == "intermittent"
    assert behavior.fault_type == "offline"


# ═══════════════════════════════════════════════════════════════════════════
# 7. BACnet-visible behavior -- the real bacpypes3 object's Reliability/Status_Flags
# ═══════════════════════════════════════════════════════════════════════════

def test_offline_sets_communication_failure_reliability_and_fault_status_flag_on_the_real_bacnet_object(database):
    """Exercises the ACTUAL code path a real BACnet client's ReadProperty would see: a real
    bacpypes3 object built by SimEngine._create_object (same helper _apply_reliability() already
    uses at creation), with _offline_reliability_override()'s result applied to it exactly as
    SimEngine.tick() does every tick. Not just internal Python fault-timer state."""
    engine = SimEngine(database)
    obj_id = 54321
    obj_row = {
        "id": obj_id, "object_type": "analog-input", "object_instance": 1,
        "name": "Zone-Temp", "units": "degrees-celsius",
        "behavior": "fault", "behavior_params": json.dumps({
            "base_behavior": "constant", "base_params": {"value": 20},
            "fault_type": "offline", "activation_mode": "always",
        }),
        "manual_value": None, "reliability": "no-fault-detected",
    }
    bacnet_obj, behavior = object_factory.create_object(engine, obj_row, slot=0, device_name="RTU")

    # Present_Value at creation is the live base value, never 0 -- see _create_object's own
    # `val = behavior.compute(self.state)` seed call.
    assert float(bacnet_obj.presentValue) == 20.0

    # Simulate what tick() does every tick: re-evaluate the live behavior, then sync reliability.
    behavior.compute(engine.state)  # activates (Always Active, first call)
    _apply_reliability(bacnet_obj, _offline_reliability_override(behavior, obj_row.get("reliability")))

    assert str(bacnet_obj.reliability) == "communication-failure"
    assert bool(bacnet_obj.statusFlags[1]) is True  # Status_Flags.fault

    # Fault ends (mirrors what a real Edit Object save / activation-mode switch to Intermittent-
    # then-expired would do) -- reliability/status must return to normal, not stay stuck offline.
    behavior._fault_active = False
    _apply_reliability(bacnet_obj, _offline_reliability_override(behavior, obj_row.get("reliability")))
    assert str(bacnet_obj.reliability) == "no-fault-detected"
    assert bool(bacnet_obj.statusFlags[1]) is False


def test_offline_does_not_affect_out_of_service(database):
    """This implementation deliberately never touches Out_Of_Service -- it isn't wired to the
    live BACnet object anywhere in this codebase (confirmed by inspection: no `outOfService`
    reference exists outside bacpypes3 itself), and Out_Of_Service has different BACnet
    semantics (allows Present_Value to be externally written independent of physical input) that
    don't match "point unavailable" -- see behaviors.py's FaultBehavior class docstring."""
    engine = SimEngine(database)
    obj_row = {
        "id": 54322, "object_type": "analog-input", "object_instance": 2,
        "name": "Zone-Temp-2", "units": "degrees-celsius",
        "behavior": "fault", "behavior_params": json.dumps({
            "base_behavior": "constant", "base_params": {"value": 20},
            "fault_type": "offline", "activation_mode": "always",
        }),
        "manual_value": None, "reliability": "no-fault-detected",
    }
    bacnet_obj, behavior = object_factory.create_object(engine, obj_row, slot=0, device_name="RTU")
    behavior.compute(engine.state)
    _apply_reliability(bacnet_obj, _offline_reliability_override(behavior, obj_row.get("reliability")))
    assert bool(bacnet_obj.outOfService) is False
