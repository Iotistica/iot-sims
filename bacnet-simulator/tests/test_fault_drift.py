"""FaultBehavior's `drift` fault type: a time-varying additive offset that starts at 0 when a
fault event becomes active and grows/shrinks linearly with elapsed time SINCE THAT EVENT
STARTED (drift_rate_per_minute * elapsed_fault_minutes), clamped to +/-maximum_drift --

    faulted_value = base_behavior_value + drift_offset

Same "add to the base's own live value every tick, never freeze it" property as `bias` (see
test_fault_bias.py), but the offset itself is time-dependent instead of fixed. Mirrors
test_fault_bias.py's own structure/conventions (direct Behavior construction via
make_behavior(), deterministic fault activation by setting FaultBehavior's own timer fields
rather than the random mtbf roll, database-layer round trip for persistence)."""
from __future__ import annotations

import json

from src.simulation.behaviors import make_behavior
from src.simulation.state import SimState


def _drift_behavior(
    *, base_behavior="constant", base_params, rate, max_drift=2.0,
    mtbf_minutes=60, fault_duration_seconds=600,
):
    return make_behavior("fault", json.dumps({
        "base_behavior": base_behavior, "base_params": base_params,
        "fault_type": "drift", "drift_rate_per_minute": rate, "maximum_drift": max_drift,
        "mtbf_minutes": mtbf_minutes, "fault_duration_seconds": fault_duration_seconds,
    }))


def _force_active(behavior, *, start: float = 0.0, until: float = 999999.0) -> None:
    """Deterministic fault activation -- sets exactly the three bookkeeping fields the real
    activation code (FaultBehavior.compute()'s own mtbf-roll branch) sets together, same
    approach as test_fault_bias.py's own _force_active. `start` matters here specifically: it's
    what drift's own elapsed-time clock is measured from (state.elapsed_seconds -
    self._fault_start_elapsed), so tests can place a fault event's start anywhere on the
    timeline rather than only at t=0."""
    behavior._fault_active = True
    behavior._fault_start_elapsed = start
    behavior._fault_end_elapsed = until


# ─── 1-2. linear offset arithmetic (positive / negative rate) ──────────────────

def test_drift_positive_rate_grows_offset_linearly_with_elapsed_fault_time():
    behavior = _drift_behavior(base_params={"value": 20}, rate=1.0, max_drift=100.0)
    _force_active(behavior)
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 20.0
    assert behavior.compute(SimState(elapsed_seconds=60.0)) == 21.0
    assert behavior.compute(SimState(elapsed_seconds=120.0)) == 22.0


def test_drift_negative_rate_shrinks_offset_linearly_with_elapsed_fault_time():
    behavior = _drift_behavior(base_params={"value": 20}, rate=-1.0, max_drift=100.0)
    _force_active(behavior)
    assert behavior.compute(SimState(elapsed_seconds=60.0)) == 19.0
    assert behavior.compute(SimState(elapsed_seconds=120.0)) == 18.0


# ─── 3. fractional elapsed time -- time-based, not tick-count-based ────────────

def test_drift_offset_is_time_based_not_tick_count_based():
    """30 real seconds at +2/min must yield exactly +1, regardless of how many compute() calls
    (ticks) happened to occur in between -- proves the math divides by elapsed real time, not by
    a tick counter that would vary with the simulation's tick rate."""
    behavior = _drift_behavior(base_params={"value": 20}, rate=2.0, max_drift=100.0)
    _force_active(behavior)
    # Several ticks land on the exact same elapsed_seconds (a finer tick rate than 1/s would
    # produce this) -- must not accumulate per-call, only per elapsed real time.
    for _ in range(5):
        assert behavior.compute(SimState(elapsed_seconds=30.0)) == 21.0


# ─── 4. Maximum Drift clamp (positive and negative) ────────────────────────────

def test_drift_clamps_at_positive_maximum_drift():
    behavior = _drift_behavior(base_params={"value": 20}, rate=1.0, max_drift=2.0)
    _force_active(behavior)
    assert behavior.compute(SimState(elapsed_seconds=60.0)) == 21.0    # 1 min -> +1
    assert behavior.compute(SimState(elapsed_seconds=120.0)) == 22.0   # 2 min -> +2 (at the cap)
    assert behavior.compute(SimState(elapsed_seconds=180.0)) == 22.0   # 3 min -> still +2
    assert behavior.compute(SimState(elapsed_seconds=600.0)) == 22.0   # 10 min -> still +2


def test_drift_clamps_at_negative_maximum_drift():
    behavior = _drift_behavior(base_params={"value": 20}, rate=-1.0, max_drift=2.0)
    _force_active(behavior)
    assert behavior.compute(SimState(elapsed_seconds=120.0)) == 18.0   # 2 min -> -2 (at the cap)
    assert behavior.compute(SimState(elapsed_seconds=600.0)) == 18.0   # 10 min -> still -2


def test_drift_maximum_drift_is_a_magnitude_regardless_of_sign_stored():
    """A negative rate clamped against a maximum_drift stored as a positive magnitude (the UI/
    schema convention -- "should normally be non-negative") clamps correctly via abs()."""
    behavior = _drift_behavior(base_params={"value": 20}, rate=-5.0, max_drift=2.0)
    _force_active(behavior)
    assert behavior.compute(SimState(elapsed_seconds=600.0)) == 18.0


# ─── 5. dynamic base keeps changing -- drift never freezes the value ───────────

def test_drift_active_tracks_a_dynamic_noise_base_instead_of_freezing():
    """Same property as bias's own equivalent test: each tick's OWN base value gets the
    (now time-varying) drift offset, not a base value captured once and held."""
    behavior = _drift_behavior(base_behavior="noise", base_params={"base": 20.0, "noise": 0.0}, rate=1.0, max_drift=100.0)
    _force_active(behavior)
    first = behavior.compute(SimState(elapsed_seconds=60.0))
    assert first == 21.0

    behavior._inner.base = 19.7  # type: ignore[attr-defined]
    second = behavior.compute(SimState(elapsed_seconds=120.0))
    assert second == 21.7  # 19.7 base + 2.0 drift offset at t=2min
    assert second != first


# ─── 6. fault inactive -- no drift effect ───────────────────────────────────────

def test_drift_inactive_returns_the_plain_base_value():
    behavior = _drift_behavior(base_behavior="noise", base_params={"base": 20.0, "noise": 0.0}, rate=1.0, mtbf_minutes=999999)
    # Never triggered (astronomically high mtbf) -- stays inactive.
    assert behavior.compute(SimState(elapsed_seconds=600.0)) == 20.0


# ─── 7. a new fault event starts its own drift clock from zero ─────────────────

def test_drift_new_fault_event_resets_offset_to_zero():
    """Activate drift, let the offset accumulate, end the fault, then begin a SEPARATE later
    fault event. The new event must start back at offset 0 -- not continue accumulating from
    where the previous event left off, and not measured from simulation startup / object
    creation / application wall-clock time. Forcing each event exactly the way the real
    activation code sets _fault_start_elapsed/_fault_end_elapsed together (see _force_active)
    is what proves this: the second force_active call is indistinguishable, from compute()'s own
    point of view, from a real second mtbf-triggered activation."""
    behavior = _drift_behavior(base_params={"value": 20}, rate=1.0, max_drift=100.0)

    # First event: starts at t=0, drift accumulates to +2 by t=2min.
    _force_active(behavior, start=0.0, until=200.0)
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 20.0
    assert behavior.compute(SimState(elapsed_seconds=120.0)) == 22.0

    # End the fault (past its own end time) -- compute() itself clears _fault_active.
    behavior.compute(SimState(elapsed_seconds=250.0))
    assert behavior._fault_active is False

    # A brand-new, later fault event begins -- its own start is a different point on the
    # timeline (t=500s), unrelated to the first event's t=0 or its accumulated +2.
    _force_active(behavior, start=500.0, until=700.0)
    result = behavior.compute(SimState(elapsed_seconds=500.0))
    assert result == 20.0  # offset reset to 0 for the NEW event

    # And it accumulates independently from ITS OWN start, not "+2 continued".
    assert behavior.compute(SimState(elapsed_seconds=560.0)) == 21.0  # 1 min into the NEW event


# ─── 8. existing fault types (spike/stuck/offline/bias) unaffected ─────────────

def test_spike_stuck_offline_bias_behavior_is_unchanged_by_drift():
    spike = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "spike",
        "fault_value": 300, "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    _force_active(spike)
    assert spike.compute(SimState()) == 300.0

    stuck = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "stuck",
        "fault_value": 30, "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    _force_active(stuck)
    assert stuck.compute(SimState()) == 30.0

    # offline is a BACnet Reliability/Status_Flags fault, not a value fault -- Present_Value
    # keeps reflecting the live base signal (20, unchanged) while active; see
    # test_fault_offline.py for the dedicated coverage of its actual (protocol-level) effect.
    offline = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "offline",
        "fault_value": 300, "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    _force_active(offline)
    assert offline.compute(SimState()) == 20.0

    bias = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "bias",
        "fault_value": 2.0, "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    _force_active(bias)
    assert bias.compute(SimState()) == 22.0


# ─── 9. serialization/API round trip preserves drift_rate_per_minute/maximum_drift ──

def test_drift_fault_config_round_trips_through_persistence(client, database):
    """Storage-layer round trip -- same database.create_object()/get_object() pattern as
    test_fault_bias.py's own equivalent test (client.post(.../objects) has a pre-existing,
    unrelated 404 in this checkout, confirmed via `git stash` in the bias work; not something
    this change should touch). Confirms the two new fields persist as plain JSON alongside the
    existing generic ones, with no schema/DB migration needed, and a negative drift_rate_per_
    minute survives the round trip without being clamped."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9402, "name": "Drift-Device"}).json()
    created = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 1, "name": "Zone-Temp", "units": "degrees-celsius",
        "behavior": "fault", "behavior_params": json.dumps({
            "base_behavior": "noise", "base_params": {"base": 20.0, "noise": 0.3},
            "fault_type": "drift", "drift_rate_per_minute": -0.05, "maximum_drift": 2.0,
            "mtbf_minutes": 60, "fault_duration_seconds": 1800,
        }),
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal",
        "point_type": None,
    })

    fetched = database.get_object(created["id"])
    params = json.loads(fetched["behavior_params"])
    assert fetched["behavior"] == "fault"
    assert params["fault_type"] == "drift"
    assert params["drift_rate_per_minute"] == -0.05
    assert params["maximum_drift"] == 2.0


def test_legacy_fault_config_without_drift_fields_still_loads_with_safe_defaults():
    """A pre-existing spike/stuck/offline/bias behavior_params blob (saved before `drift`
    existed) has no drift_rate_per_minute/maximum_drift keys at all -- FaultBehavior must not
    error constructing from it, and must fall back to the documented safe-bounded defaults
    (irrelevant unless fault_type is later changed to drift, but must not crash either way)."""
    behavior = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "stuck",
        "fault_value": 30, "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    assert behavior.drift_rate_per_minute == 0.1
    assert behavior.maximum_drift == 2.0
