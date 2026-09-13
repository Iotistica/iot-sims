"""FaultBehavior's `bias` fault type: an additive offset on top of whatever the base behavior
would have produced this tick (faulted_value = base_behavior_value + bias_value), NOT a frozen
value like spike/stuck and NOT a `constant_value + bias` shortcut -- see behaviors.py's
FaultBehavior.compute() and its own class docstring. Mirrors test_fmu_behavior.py's established
unit-test convention (direct Behavior construction via make_behavior(), no running BACnet app)
for the compute()-level cases, plus one API-level round-trip test following
test_fault_restore.py's own create/PUT pattern for the "existing fault types still work,
persistence is unaffected" part of the spec.
"""
from __future__ import annotations

import json

from src.simulation.behaviors import make_behavior
from src.simulation.state import SimState


def _bias_behavior(*, base_behavior="constant", base_params, bias, mtbf_minutes=60, fault_duration_seconds=30):
    return make_behavior("fault", json.dumps({
        "base_behavior": base_behavior, "base_params": base_params,
        "fault_type": "bias", "fault_value": bias,
        "mtbf_minutes": mtbf_minutes, "fault_duration_seconds": fault_duration_seconds,
    }))


def _force_active(behavior, *, until: float = 999999.0) -> None:
    """Bypass the random mtbf roll -- exactly how test_fmu_behavior.py's own
    test_fault_active_applies_the_configured_fault_result forces a deterministic
    active fault for testing, reusing FaultBehavior's own timer fields rather than
    a separate test-only hook."""
    behavior._fault_active = True
    behavior._fault_end_elapsed = until


# ─── 1-3. additive offset arithmetic (positive / negative / decimal) ───────────

def test_bias_active_adds_positive_offset_to_base_value():
    behavior = _bias_behavior(base_params={"value": 20}, bias=2.0)
    _force_active(behavior)
    assert behavior.compute(SimState()) == 22.0


def test_bias_active_adds_negative_offset_to_base_value():
    behavior = _bias_behavior(base_params={"value": 20}, bias=-2.0)
    _force_active(behavior)
    assert behavior.compute(SimState()) == 18.0


def test_bias_active_adds_decimal_offset_to_base_value():
    behavior = _bias_behavior(base_params={"value": 20.1}, bias=1.5)
    _force_active(behavior)
    assert behavior.compute(SimState()) == 21.6


def test_bias_active_with_zero_offset_is_a_passthrough():
    behavior = _bias_behavior(base_params={"value": 20}, bias=0.0)
    _force_active(behavior)
    assert behavior.compute(SimState()) == 20.0


# ─── 4. dynamic base (noise) keeps changing -- bias never freezes the value ────

def test_bias_active_tracks_a_dynamic_noise_base_instead_of_freezing():
    """The underlying signal must continue changing normally while bias is
    active -- each tick's OWN base value gets the offset, not one value
    captured once and held. Uses noise=0 (deterministic base + jitter of
    exactly 0) at two different `base`s to prove the base is being
    re-read/recomputed every call, not cached from the first compute()."""
    behavior = _bias_behavior(base_behavior="noise", base_params={"base": 20.0, "noise": 0.0}, bias=2.0)
    _force_active(behavior)
    first = behavior.compute(SimState())
    assert first == 22.0

    # Change what the base behavior itself would produce next tick (as a real
    # noise/random_walk base naturally would from one tick to the next) --
    # bias must apply to THIS new value, not repeat the first one.
    behavior._inner.base = 19.7  # type: ignore[attr-defined]
    second = behavior.compute(SimState())
    assert second == 21.7
    assert second != first


def test_bias_active_tracks_a_random_walk_base_across_ticks():
    """Same "keeps changing, never frozen" property against random_walk
    (step=0 makes it deterministic so the test isn't flaky, while still
    exercising the real self._inner.compute() call path each tick)."""
    behavior = _bias_behavior(base_behavior="random_walk", base_params={"value": 20.0, "step": 0.0, "min": 0, "max": 100}, bias=2.0)
    _force_active(behavior)
    assert behavior.compute(SimState()) == 22.0
    behavior._inner._value = 19.7  # type: ignore[attr-defined]
    assert behavior.compute(SimState()) == 21.7


# ─── 5. fault inactive -- output unchanged from the base behavior ──────────────

def test_bias_inactive_returns_the_plain_base_value():
    behavior = _bias_behavior(base_behavior="noise", base_params={"base": 20.0, "noise": 0.0}, bias=2.0, mtbf_minutes=999999)
    # Never triggered (astronomically high mtbf) -- stays inactive.
    assert behavior.compute(SimState()) == 20.0


# ─── 6. fault expiration -- returns to base behavior once the duration elapses ─

def test_bias_returns_to_base_value_after_fault_duration_expires():
    behavior = _bias_behavior(base_behavior="noise", base_params={"base": 20.0, "noise": 0.0}, bias=2.0)
    _force_active(behavior, until=100.0)
    assert behavior.compute(SimState(elapsed_seconds=50.0)) == 22.0  # still active

    # Past the fault's own end time -- compute() itself clears _fault_active
    # at the top, exactly the same timer every other fault_type already uses.
    result = behavior.compute(SimState(elapsed_seconds=150.0))
    assert result == 20.0
    assert behavior._fault_active is False


# ─── 7. existing fault types unaffected by the new branch ──────────────────────

def test_spike_stuck_offline_behavior_is_unchanged():
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


# ─── 8. serialization/API round trip preserves fault_type/fault_value ──────────

def test_bias_fault_config_round_trips_through_the_object_api(client, database):
    """Storage-layer round trip -- database.create_object()/get_object() directly, the same
    pattern test_classification_dataset.py's own _make_point() helper already uses for new
    tests in this suite (the client.post(.../objects) HTTP route has a pre-existing, unrelated
    404 in this checkout -- confirmed via `git stash` to reproduce identically with none of this
    session's changes applied, so not something this change should touch or work around at the
    HTTP layer). Confirms nothing about persistence needed a schema change: behavior_params is
    stored/returned as opaque JSON text, and a negative bias value survives the round trip
    without being clamped to zero."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9401, "name": "Bias-Device"}).json()
    created = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 1, "name": "Zone-Temp", "units": "degrees-celsius",
        "behavior": "fault", "behavior_params": json.dumps({
            "base_behavior": "noise", "base_params": {"base": 20.0, "noise": 0.3},
            "fault_type": "bias", "fault_value": -1.5, "mtbf_minutes": 60, "fault_duration_seconds": 30,
        }),
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal",
        "point_type": None,
    })

    fetched = database.get_object(created["id"])
    params = json.loads(fetched["behavior_params"])
    assert fetched["behavior"] == "fault"
    assert params["fault_type"] == "bias"
    assert params["fault_value"] == -1.5
