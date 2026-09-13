"""FaultBehavior's `activation_mode` ("intermittent" | "always"): a generic property of the
fault WRAPPER, orthogonal to fault_type -- see FaultBehavior's own class docstring in
behaviors.py. "intermittent" is the pre-existing MTBF/fault_duration_seconds scheduler,
completely unchanged. "always" skips that scheduler entirely (no MTBF wait, no automatic
expiry) and stays continuously active for as long as this object's Behavior stays "fault".
Missing activation_mode (every config saved before this feature existed) defaults to
"intermittent" for full backward compatibility.

Two distinct places matter for the mode-TRANSITION requirements (#8/#9 below), and both are
exercised here:
  - FaultBehavior.compute()'s own `_last_activation_mode` guard -- a generic safety net for any
    caller that mutates `.activation_mode` on an already-ticking instance in place.
  - BuiltInSimulationProvider.sync_point_configs() in providers/builtin.py -- the ACTUAL
    production mechanism: editing an object's fault config (including flipping activation_mode)
    reconciles a fresh FaultBehavior instance and selectively carries over the old instance's
    ongoing fault timing, but ONLY when activation_mode itself didn't change (see that method's
    own updated comment) -- otherwise the new instance starts completely fresh under the new
    mode, which is what makes Intermittent -> Always Active activate immediately and Always
    Active -> Intermittent get a truly fresh MTBF wait rather than inheriting stale timing.

Mirrors test_fault_bias.py/test_fault_drift.py's established conventions (direct Behavior
construction via make_behavior(), deterministic activation via FaultBehavior's own timer
fields, database-layer round trip for persistence)."""
from __future__ import annotations

import json

from src.simulation.behaviors import FaultBehavior, make_behavior
from src.simulation.providers.base import PointConfig, PointDataType, SimulationContext
from src.simulation.providers.builtin import BuiltInSimulationProvider
from src.simulation.state import SimState


def _fault_params(*, fault_type="bias", activation_mode="always", **overrides) -> dict:
    params = {
        "base_behavior": "constant", "base_params": {"value": 20},
        "fault_type": fault_type, "fault_value": 2.0,
        "activation_mode": activation_mode,
        "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }
    params.update(overrides)
    return params


# ─── 1-3. Always Active: immediate, ignores MTBF, ignores Fault Duration ───────

def test_always_active_bias_applies_immediately_with_no_mtbf_wait():
    behavior = make_behavior("fault", json.dumps(_fault_params()))
    # No _force_active call at all -- the very first compute(), with the default
    # mtbf_minutes=60 fully intact, must already be faulted.
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 22.0


def test_always_active_ignores_an_extremely_large_mtbf():
    behavior = make_behavior("fault", json.dumps(_fault_params(mtbf_minutes=10_000_000)))
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 22.0


def test_always_active_ignores_a_short_fault_duration():
    behavior = make_behavior("fault", json.dumps(_fault_params(fault_duration_seconds=1)))
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 22.0
    # Long past the configured 1-second duration -- still active, no automatic expiry.
    assert behavior.compute(SimState(elapsed_seconds=3600.0)) == 22.0
    assert behavior._fault_active is True
    assert behavior._fault_end_elapsed == float("inf")


# ─── 4-5. Intermittent unchanged; missing activation_mode defaults to it ───────

def test_intermittent_scheduling_is_unaffected_by_activation_mode_existing():
    """Same MTBF-roll-required, expires-after-fault_duration behavior as before this feature --
    forcing activation deterministically exactly like test_fault_bias.py's own _force_active."""
    behavior = make_behavior("fault", json.dumps(_fault_params(activation_mode="intermittent")))
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 20.0  # never triggered yet

    behavior._fault_active = True
    behavior._fault_start_elapsed = 100.0
    behavior._fault_end_elapsed = 130.0
    assert behavior.compute(SimState(elapsed_seconds=110.0)) == 22.0
    assert behavior.compute(SimState(elapsed_seconds=200.0)) == 20.0  # expired
    assert behavior._fault_active is False


def test_missing_activation_mode_key_defaults_to_intermittent():
    """A behavior_params blob saved before this feature existed has no activation_mode key at
    all -- must resolve to "intermittent", not error and not silently become "always"."""
    behavior = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "bias",
        "fault_value": 2.0, "mtbf_minutes": 10_000_000, "fault_duration_seconds": 30,
    }))
    assert behavior.activation_mode == "intermittent"
    # With mtbf effectively infinite and no forced activation, stays plain base value --
    # proves it did NOT default to "always" (which would report 22.0 immediately).
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 20.0


# ─── 6. Positive and negative bias, both under Always Active ───────────────────

def test_always_active_bias_positive_and_negative():
    positive = make_behavior("fault", json.dumps(_fault_params(fault_value=2.0)))
    assert positive.compute(SimState()) == 22.0

    negative = make_behavior("fault", json.dumps(_fault_params(fault_value=-2.0)))
    assert negative.compute(SimState()) == 18.0


# ─── 7. Dynamic Base Behavior is not frozen under Always Active ────────────────

def test_always_active_bias_tracks_a_dynamic_noise_base_instead_of_freezing():
    behavior = make_behavior("fault", json.dumps(_fault_params(
        base_behavior="noise", base_params={"base": 20.0, "noise": 0.0}, fault_value=2.0,
    )))
    assert behavior.compute(SimState(elapsed_seconds=0.0)) == 22.0
    behavior._inner.base = 20.2  # type: ignore[attr-defined]
    assert behavior.compute(SimState(elapsed_seconds=5.0)) == 22.2
    behavior._inner.base = 19.9  # type: ignore[attr-defined]
    assert behavior.compute(SimState(elapsed_seconds=10.0)) == 21.9


# ─── 8-9. Mode transitions through the REAL reconcile path ─────────────────────
# (BuiltInSimulationProvider.sync_point_configs -- what actually runs when a fault
# object's config is edited and reload() fires, see that method's own docstring.)

def _context(*, activation_mode: str, fault_type: str = "bias", extra: dict | None = None) -> SimulationContext:
    params = _fault_params(fault_type=fault_type, activation_mode=activation_mode, **(extra or {}))
    return SimulationContext(
        participant_device_ids=[1],
        point_configs=[PointConfig(
            point_id=1, behavior="fault", behavior_params=json.dumps(params),
            data_type=PointDataType.REAL,
        )],
    )


def test_intermittent_to_always_active_transition_activates_immediately():
    provider = BuiltInSimulationProvider()
    provider.initialize(_context(activation_mode="intermittent"))
    provider.start()

    # Deterministically make the intermittent instance active with time left on its own clock
    # (the scenario the transition guard actually protects against -- a fault that's mid-event
    # at the moment the user flips the mode, not merely idle).
    behavior = provider._behaviors[1]
    behavior._fault_active = True
    behavior._fault_start_elapsed = 0.0
    behavior._fault_end_elapsed = 9999.0

    # Edit: switch to Always Active. This is exactly what a real Edit Object save does --
    # reconcile with a changed behavior_params.
    provider.sync_point_configs(_context(activation_mode="always").point_configs)
    provider.step(0.0)
    assert provider.get_outputs()[1] == 22.0  # active immediately, no stale MTBF/end-time inherited

    new_behavior = provider._behaviors[1]
    assert new_behavior.activation_mode == "always"
    assert new_behavior._fault_end_elapsed == float("inf")  # no automatic expiry carried over


def test_always_active_to_intermittent_transition_resets_stale_state():
    provider = BuiltInSimulationProvider()
    provider.initialize(_context(activation_mode="always"))
    provider.start()
    provider.step(0.0)
    assert provider.get_outputs()[1] == 22.0  # continuously active, as expected

    always_behavior = provider._behaviors[1]
    assert always_behavior._fault_end_elapsed == float("inf")

    # Edit: switch back to Intermittent.
    provider.sync_point_configs(_context(activation_mode="intermittent").point_configs)
    new_behavior = provider._behaviors[1]
    assert new_behavior.activation_mode == "intermittent"
    # Must NOT have inherited the "always" instance's _fault_end_elapsed = +inf -- that would
    # leave it permanently stuck faulted (state.elapsed_seconds > inf is never true, so the
    # existing intermittent expiry check could never fire and the mtbf-roll branch could never
    # be reached again).
    assert new_behavior._fault_active is False
    assert new_behavior._fault_end_elapsed != float("inf")

    provider.step(0.0)
    # With mtbf_minutes=60 and no forced activation, the very next tick after the transition is
    # back to plain base behavior -- a fresh MTBF wait, not permanently faulted.
    assert provider.get_outputs()[1] == 20.0


def test_unrelated_config_tweak_still_preserves_ongoing_fault_state():
    """Regression guard on the OTHER half of the builtin.py fix: when activation_mode does NOT
    change, an unrelated tweak (here, the bias value itself) must still preserve the fault's
    ongoing timing exactly as it always has -- the state-preservation this reconcile step exists
    for in the first place must not have been broken by making it activation_mode-aware."""
    provider = BuiltInSimulationProvider()
    provider.initialize(_context(activation_mode="intermittent"))
    provider.start()

    behavior = provider._behaviors[1]
    behavior._fault_active = True
    behavior._fault_start_elapsed = 0.0
    behavior._fault_end_elapsed = 9999.0

    # Same activation_mode, different fault_value -- ongoing activation must carry over.
    provider.sync_point_configs(_context(activation_mode="intermittent", extra={"fault_value": 5.0}).point_configs)
    new_behavior = provider._behaviors[1]
    assert new_behavior._fault_active is True
    assert new_behavior._fault_end_elapsed == 9999.0
    provider.step(0.0)
    assert provider.get_outputs()[1] == 25.0  # still faulted, now with the NEW bias value


# ─── 10. activation_mode has no effect when Behavior != fault ──────────────────

def test_activation_mode_key_is_meaningless_for_a_non_fault_behavior():
    """A non-fault Behavior class (noise, here) simply ignores unknown params entirely --
    activation_mode never applies unless behavior == "fault", by construction (make_behavior's
    own dispatch), never by a runtime check that could be bypassed."""
    behavior = make_behavior("noise", json.dumps({
        "base": 20.0, "noise": 0.0, "activation_mode": "always", "fault_type": "bias",
    }))
    assert not isinstance(behavior, FaultBehavior)
    assert behavior.compute(SimState()) == 20.0  # plain noise output, no fault applied


# ─── 11. persistence/API round trip ─────────────────────────────────────────────

def test_activation_mode_always_round_trips_through_persistence(client, database):
    """Storage-layer round trip -- same database.create_object()/get_object() pattern as
    test_fault_bias.py/test_fault_drift.py's own equivalent tests (client.post(.../objects) has
    a pre-existing, unrelated 404 in this checkout; not something this change should touch)."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9403, "name": "Activation-Device"}).json()
    created = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 1, "name": "Zone-Temp", "units": "degrees-celsius",
        "behavior": "fault", "behavior_params": json.dumps({
            "base_behavior": "noise", "base_params": {"base": 20.0, "noise": 0.3},
            "fault_type": "bias", "fault_value": 2.0, "activation_mode": "always",
            "mtbf_minutes": 60, "fault_duration_seconds": 30,
        }),
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal",
        "point_type": None,
    })

    fetched = database.get_object(created["id"])
    params = json.loads(fetched["behavior_params"])
    assert params["activation_mode"] == "always"
    # MTBF/fault_duration_seconds are NOT erased just because Always Active is selected -- they
    # stay persisted so switching back to Intermittent retains the user's previous settings.
    assert params["mtbf_minutes"] == 60
    assert params["fault_duration_seconds"] == 30


def test_legacy_payload_without_activation_mode_field_still_accepted(client, database):
    """An old payload saved before this feature existed (no activation_mode key at all) must
    still load without error and behave as intermittent -- proven at the persistence layer, not
    just the in-memory make_behavior() layer covered by test_missing_activation_mode_key_
    defaults_to_intermittent above."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9404, "name": "Legacy-Fault-Device"}).json()
    created = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 1, "name": "Zone-Temp", "units": "degrees-celsius",
        "behavior": "fault", "behavior_params": json.dumps({
            "base_behavior": "constant", "base_params": {"value": 20}, "fault_type": "stuck",
            "fault_value": 30, "mtbf_minutes": 60, "fault_duration_seconds": 30,
        }),
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal",
        "point_type": None,
    })

    fetched = database.get_object(created["id"])
    behavior = make_behavior(fetched["behavior"], fetched["behavior_params"])
    assert isinstance(behavior, FaultBehavior)
    assert behavior.activation_mode == "intermittent"
