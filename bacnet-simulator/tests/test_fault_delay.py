"""FaultBehavior's `delay` fault type: a PURE TIME SHIFT, not a magnitude change --

    output(t) = base_value(t - delay_seconds)

-- looked up from a rolling (elapsed_seconds, value) history of the base behavior's own output
(see FaultBehavior._record_base/_history_value_at_or_before/_prune_history and the class
docstring's "rolling history tradeoff" note). Deliberately NOT a frozen/held value (that's
`stuck`) and NOT first-order-lag/smoothing (a distinct, unimplemented future fault type) --
delay looks up a genuine past sample, never blends toward one.

Mirrors test_fault_bias.py/test_fault_drift.py/test_fault_activation_mode.py's established
conventions: direct Behavior construction via make_behavior() with deterministic base-value
mutation between compute() calls (same technique those files use for noise/random_walk),
BuiltInSimulationProvider for the real reconfiguration-across-an-edit path, and a database-layer
persistence round trip."""
from __future__ import annotations

import json

from src.simulation import behaviors as behaviors_module
from src.simulation.behaviors import make_behavior
from src.simulation.providers.base import PointConfig, PointDataType, SimulationContext
from src.simulation.providers.builtin import BuiltInSimulationProvider
from src.simulation.state import SimState


def _delay_behavior(
    *, delay_seconds, activation_mode="always", base_value=20,
    mtbf_minutes=60, fault_duration_seconds=600,
):
    return make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": base_value},
        "fault_type": "delay", "delay_seconds": delay_seconds,
        "activation_mode": activation_mode,
        "mtbf_minutes": mtbf_minutes, "fault_duration_seconds": fault_duration_seconds,
    }))


# ─── 1. Basic delay -- the exact worked example from the spec ──────────────────

def test_delay_basic_lookup_returns_the_value_from_delay_seconds_ago():
    behavior = _delay_behavior(delay_seconds=20, base_value=20)
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 20   # only sample so far -- lazily
    # constructs self._inner from base_value above; mutated directly on every call after this.
    behavior._inner.value = 21
    behavior.compute(SimState(elapsed_seconds=10.0))
    behavior._inner.value = 22
    behavior.compute(SimState(elapsed_seconds=20.0))
    behavior._inner.value = 24
    result = behavior.compute(SimState(elapsed_seconds=30.0))
    assert result == 21  # target = 30 - 20 = 10 -> the sample recorded at t=10


# ─── 2. Non-exact timestamp lookup -- most recent sample AT OR BEFORE target ───

def test_delay_lookup_uses_most_recent_sample_at_or_before_target_no_interpolation():
    behavior = _delay_behavior(delay_seconds=999)  # irrelevant here -- testing the helper directly
    behavior._history = [(0.0, 20), (7.0, 21), (14.0, 23)]
    assert behavior._history_value_at_or_before(10.0) == 21


# ─── 3. Zero delay -- output matches the current base value ────────────────────

def test_delay_zero_seconds_matches_current_base_value():
    behavior = _delay_behavior(delay_seconds=0, base_value=20)
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 20
    behavior._inner.value = 25
    assert behavior.compute(SimState(elapsed_seconds=5.0)) == 25


# ─── 4. Insufficient history -- falls back to the OLDEST available sample ──────

def test_delay_falls_back_to_oldest_available_sample_when_history_is_insufficient():
    behavior = _delay_behavior(delay_seconds=30, base_value=20)
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 20
    behavior._inner.value = 21
    # Only 10s of history exists; target = 10 - 30 = -20, long before anything retained.
    result = behavior.compute(SimState(elapsed_seconds=10.0))
    assert result == 20  # oldest available -- never 0/None/NaN, never raises


# ─── 5. Always Active: continuous, ignores MTBF ─────────────────────────────────

def test_delay_always_active_ignores_mtbf_and_applies_immediately():
    behavior = _delay_behavior(delay_seconds=5, activation_mode="always", mtbf_minutes=10_000_000, base_value=20)
    behavior.compute(SimState(elapsed_seconds=0.0))
    assert behavior._fault_active is True
    behavior._inner.value = 25
    result = behavior.compute(SimState(elapsed_seconds=5.0))
    assert result == 20  # target = 5 - 5 = 0 -> the t=0 sample, no MTBF wait needed at all


# ─── 6. Intermittent: only applies during the active window ────────────────────

def test_delay_intermittent_returns_current_base_value_when_inactive():
    behavior = _delay_behavior(delay_seconds=100, activation_mode="intermittent", mtbf_minutes=10_000_000, base_value=20)
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 20
    behavior._inner.value = 25
    # Never triggered -- plain current base value, delay irrelevant while inactive.
    assert behavior.compute(SimState(elapsed_seconds=50.0)) == 25


# ─── 7. Deactivation: output immediately returns to current base value ─────────

def test_delay_deactivation_returns_immediately_to_current_base_value():
    behavior = _delay_behavior(delay_seconds=10, activation_mode="intermittent", base_value=20)
    behavior.compute(SimState(elapsed_seconds=0.0))

    behavior._fault_active = True
    behavior._fault_start_elapsed = 0.0
    behavior._fault_end_elapsed = 20.0
    behavior._inner.value = 30
    active_result = behavior.compute(SimState(elapsed_seconds=15.0))
    assert active_result == 20  # target = 15 - 10 = 5 -> most recent <=5 is the t=0 sample

    behavior._inner.value = 99
    inactive_result = behavior.compute(SimState(elapsed_seconds=25.0))  # past fault_end -> expires
    assert inactive_result == 99  # immediate, current value -- no gradual recovery
    assert behavior._fault_active is False


# ─── 8. Changing base signal -- delayed output follows history, never frozen ───

def test_delay_active_tracks_a_changing_base_signal_instead_of_freezing_one_value():
    behavior = _delay_behavior(delay_seconds=10, activation_mode="always", base_value=20.0)
    values_by_time = {0.0: 20.0, 5.0: 20.5, 10.0: 21.0, 15.0: 20.8, 20.0: 21.2}
    outputs = {}
    for i, (t, v) in enumerate(values_by_time.items()):
        if i > 0:  # t=0 is seeded by base_value above -- self._inner doesn't exist until then
            behavior._inner.value = v
        outputs[t] = behavior.compute(SimState(elapsed_seconds=t))
    # Not every output is identical -- proves the delayed signal itself is moving, not frozen at
    # whatever the very first recorded value happened to be.
    assert len(set(outputs.values())) > 1
    assert outputs[20.0] == 21.0  # target = 20 - 10 = 10 -> the sample recorded at t=10


# ─── 9. Reconfiguration through the REAL reconcile path (10s -> 30s while running) ──

def _delay_context(*, delay_seconds: float, activation_mode: str = "always") -> SimulationContext:
    params = {
        "base_behavior": "constant", "base_params": {"value": 0},
        "fault_type": "delay", "delay_seconds": delay_seconds, "activation_mode": activation_mode,
        "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }
    return SimulationContext(
        participant_device_ids=[1],
        point_configs=[PointConfig(
            point_id=1, behavior="fault", behavior_params=json.dumps(params),
            data_type=PointDataType.REAL,
        )],
    )


def test_delay_reconfiguration_carries_history_across_an_edit_and_uses_the_new_offset():
    """BuiltInSimulationProvider.sync_point_configs -- what actually runs when a fault object's
    config is edited (see providers/builtin.py's own updated comment: history is carried over
    unconditionally, unlike activation timing, because it's orthogonal to activation_mode/
    fault_type)."""
    provider = BuiltInSimulationProvider()
    provider.initialize(_delay_context(delay_seconds=10))
    provider.start()

    provider.step(0.0)  # t=0 -- lazily seeds self._inner from base_params (value=0)
    for t in range(1, 21):
        provider._behaviors[1]._inner.value = t
        provider.step(1.0)
    assert provider.get_outputs()[1] == 10  # elapsed=20, delay=10 -> the sample recorded at t=10

    # Edit: 10s -> 30s while running.
    provider.sync_point_configs(_delay_context(delay_seconds=30).point_configs)
    new_behavior = provider._behaviors[1]
    assert new_behavior.delay_seconds == 30
    assert len(new_behavior._history) > 0  # carried over from the old instance, not reset to empty

    provider.step(0.0)  # re-evaluate at the SAME elapsed_seconds (20) under the NEW delay
    # target = 20 - 30 = -10, before all retained history -> documented fallback: oldest
    # available sample (t=0, value 0), not 0/None/NaN as a coincidence of the math, and not a
    # crash despite 30s of history not actually being available yet.
    assert provider.get_outputs()[1] == 0


# ─── 10. Negative delay_seconds is clamped, never accepted as "look into the future" ──

def test_delay_negative_seconds_is_clamped_to_zero():
    behavior = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "delay",
        "delay_seconds": -15, "activation_mode": "always",
    }))
    assert behavior.delay_seconds == 0.0


# ─── 11. Persistence/API round trip ─────────────────────────────────────────────

def test_delay_fault_config_round_trips_through_persistence(client, database):
    """Storage-layer round trip -- same database.create_object()/get_object() pattern as
    test_fault_bias.py/test_fault_drift.py/test_fault_activation_mode.py's own equivalent tests
    (client.post(.../objects) has a pre-existing, unrelated 404 in this checkout; not something
    this change should touch)."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9405, "name": "Delay-Device"}).json()
    created = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 1, "name": "Zone-Temp", "units": "degrees-celsius",
        "behavior": "fault", "behavior_params": json.dumps({
            "base_behavior": "noise", "base_params": {"base": 20.0, "noise": 0.3},
            "fault_type": "delay", "delay_seconds": 30, "activation_mode": "always",
            "mtbf_minutes": 60, "fault_duration_seconds": 30,
        }),
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal",
        "point_type": None,
    })

    fetched = database.get_object(created["id"])
    params = json.loads(fetched["behavior_params"])
    assert params["fault_type"] == "delay"
    assert params["delay_seconds"] == 30
    assert params["activation_mode"] == "always"


# ─── 13. History pruning -- bounded, never grows unboundedly ───────────────────

def test_delay_history_is_pruned_to_roughly_delay_plus_margin():
    behavior = _delay_behavior(delay_seconds=10, base_value=0)
    behavior.compute(SimState(elapsed_seconds=0.0))
    for t in range(1, 500):
        behavior._inner.value = t
        behavior.compute(SimState(elapsed_seconds=float(t)))
    # Retention window is delay_seconds(10) + margin(30) = 40s of 1-second-apart samples --
    # nowhere near all 500 seconds' worth should still be retained.
    assert len(behavior._history) < 100
    oldest_timestamp = behavior._history[0][0]
    assert oldest_timestamp >= 499.0 - 10.0 - 30.0 - 1.0  # within the retained cutoff (small slack)


def test_delay_history_sample_count_cap_is_enforced_independent_of_time_cutoff():
    """Defensive backstop against a pathological config (an extremely large delay_seconds, here)
    that would make the time-based cutoff alone never prune anything."""
    behavior = _delay_behavior(delay_seconds=10_000_000)
    behavior._history = [
        (float(i), i) for i in range(behaviors_module._DELAY_HISTORY_MAX_SAMPLES + 500)
    ]
    behavior._prune_history(float(behaviors_module._DELAY_HISTORY_MAX_SAMPLES + 500))
    assert len(behavior._history) == behaviors_module._DELAY_HISTORY_MAX_SAMPLES


# ─── Value-type support: delay is a pure passthrough, not restricted to analog ──

def test_delay_supports_boolean_base_values_without_coercing_to_float():
    """delay never wraps its result in float() (unlike bias/drift, which must add a numeric
    offset) -- it's a passthrough of whatever the base behavior actually produced, so a binary/
    manual point's bool stays a real bool all the way through, matching this file's own
    normalize_present_value expectations elsewhere in the codebase."""
    behavior = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": True}, "fault_type": "delay",
        "delay_seconds": 0, "activation_mode": "always",
    }))
    result = behavior.compute(SimState(elapsed_seconds=0.0))
    assert result is True


# ─── 12. Existing fault types unaffected by delay ──────────────────────────────

def test_spike_stuck_offline_bias_drift_behavior_is_unchanged_by_delay():
    spike = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "spike",
        "fault_value": 300, "activation_mode": "intermittent",
        "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    spike._fault_active = True
    spike._fault_end_elapsed = 999999.0
    assert spike.compute(SimState()) == 300.0

    stuck = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "stuck",
        "fault_value": 30, "activation_mode": "intermittent",
        "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    stuck._fault_active = True
    stuck._fault_end_elapsed = 999999.0
    assert stuck.compute(SimState()) == 30.0

    # offline is a BACnet Reliability/Status_Flags fault, not a value fault -- Present_Value
    # keeps reflecting the live base signal (20, unchanged) while active; see
    # test_fault_offline.py for the dedicated coverage of its actual (protocol-level) effect.
    offline = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "offline",
        "fault_value": 300, "activation_mode": "intermittent",
        "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    offline._fault_active = True
    offline._fault_end_elapsed = 999999.0
    assert offline.compute(SimState()) == 20.0

    bias = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "bias",
        "fault_value": 2.0, "activation_mode": "always",
    }))
    assert bias.compute(SimState()) == 22.0

    drift = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "drift",
        "drift_rate_per_minute": 1.0, "maximum_drift": 100.0, "activation_mode": "always",
    }))
    drift.compute(SimState(elapsed_seconds=0.0))  # activates immediately -- fault starts at t=0
    assert drift.compute(SimState(elapsed_seconds=60.0)) == 21.0  # 1 min later -> +1.0


def test_missing_delay_fields_still_load_with_safe_defaults():
    """A pre-existing spike/stuck/offline/bias/drift behavior_params blob (saved before `delay`
    existed) has no delay_seconds key at all -- must not error, must resolve to the documented
    default."""
    behavior = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "stuck",
        "fault_value": 30, "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    assert behavior.delay_seconds == 30.0
    assert behavior._history == []
