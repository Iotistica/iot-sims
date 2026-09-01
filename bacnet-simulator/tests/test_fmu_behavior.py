"""FMU-backed point Behavior integration: for a point with an FMU/model
provider, the live FMU output is the baseline, and the point's configured
Behavior is applied on top of that baseline as a perturbation or override
(SimEngine._apply_fmu_behavior). The FMU always keeps running and updating
its raw value underneath, regardless of which Behavior is configured.

Core regression this file protects against: `constant` must mean "no
transformation" for an FMU-backed point -- an existing/legacy `constant`
value (e.g. a point's pre-FMU seed value) must never be silently added to
the live FMU output. Every other Behavior type gets its own FMU-specific
semantics, exercised below against a bare SimEngine(database) with no
running BACnet app, following this codebase's established unit-test
convention (see tests/test_sim_engine_object_value.py).
"""
from __future__ import annotations

import json
import math
from typing import Any

import pytest

from src.api.routers import simulation as simulation_router
from src.simulation.models import runtime as model_runtime
from src.simulation.behaviors import Behavior, make_behavior
from src.simulation.engine import SimEngine
from src.simulation.models.registry import ModelDefinition, VariableDefinition
from src.simulation.state import SimState


def _engine(database) -> SimEngine:
    return SimEngine(database)


# ─── 1. constant: no transformation, ever ───────────────────────────────────

@pytest.mark.parametrize("legacy_value", [0, 22, 24, 1.6, 320])
def test_constant_is_always_a_passthrough(database, legacy_value):
    engine = _engine(database)
    behavior = make_behavior("constant", json.dumps({"value": legacy_value}))
    result = engine._apply_fmu_behavior(behavior, "constant", 204.0, engine.state)
    assert result == 204.0


# ─── 1b. raw: the explicit, discoverable reset-to-FMU-value behavior ────────

def test_raw_behavior_is_a_passthrough(database):
    engine = _engine(database)
    behavior = make_behavior("raw", "{}")
    result = engine._apply_fmu_behavior(behavior, "raw", 204.0, engine.state)
    assert result == 204.0


def test_make_behavior_raw_preserves_stored_seed_value(database):
    """Regression: make_behavior("raw", ...) must NOT fall through to the
    generic unrecognized-type fallback (ConstantBehavior({"value": 0})),
    which silently discards behavior_params. A point relabeled from
    'constant' to 'raw' by models.store.reconcile_provider_owned_raw_behavior
    keeps its original behavior_params untouched -- its pre-FMU-tick
    initial seed (SimEngine._create_object's `behavior.compute(state)`
    call, used before the provider has ever produced a value) must still
    honor that stored value, not silently reset to 0. This exact bug once
    caused every "raw" point's seed -- and any weighted-average aggregate
    depending on one before its own FMU had ticked -- to read 0.0."""
    behavior = make_behavior("raw", json.dumps({"value": 22.5}))
    assert behavior.compute(SimState()) == 22.5


def test_make_behavior_raw_defaults_to_zero_with_no_stored_value(database):
    behavior = make_behavior("raw", "{}")
    assert behavior.compute(SimState()) == 0.0


# ─── 2. manual: absolute override, unaffected by raw ────────────────────────

def test_manual_is_an_absolute_override(database):
    engine = _engine(database)
    behavior = make_behavior("manual", "{}", manual_value=999.0)
    result = engine._apply_fmu_behavior(behavior, "manual", 204.0, engine.state)
    assert result == 999.0


# ─── 3. noise: FMU raw replaces the editable Base ───────────────────────────

def test_noise_uses_fmu_raw_as_base(database):
    engine = _engine(database)
    behavior = make_behavior("noise", json.dumps({"base": 999.0, "noise": 5.0}))
    result = engine._apply_fmu_behavior(behavior, "noise", 204.0, engine.state)
    assert 204.0 - 5.0 <= result <= 204.0 + 5.0
    # The stored (now-irrelevant) base is overwritten on the instance too.
    assert behavior.base == 204.0


# ─── 4. sine: FMU raw replaces the editable Base ────────────────────────────

def test_sine_uses_fmu_raw_as_base(database):
    engine = _engine(database)
    behavior = make_behavior("sine", json.dumps({
        "base": 999.0, "amplitude": 5.0, "period_hours": 24.0, "phase_hours": 0.0,
    }))
    state = SimState(time_of_day=6.0, elapsed_seconds=0.0)
    result = engine._apply_fmu_behavior(behavior, "sine", 204.0, state)
    expected = 204.0 + 5.0 * math.sin(2 * math.pi * 6.0 / 24.0)
    assert result == pytest.approx(expected)


# ─── 5. ramp: offset/drift starting from zero relative to the FMU baseline ──

def test_ramp_drifts_from_zero_toward_offset_to(database):
    engine = _engine(database)
    behavior = make_behavior("ramp", json.dumps({
        "from": 999.0, "to": 5.0, "duration_minutes": 10.0, "repeat": False,
    }))
    engine.state.elapsed_seconds = 0.0
    result = engine._apply_fmu_behavior(behavior, "ramp", 22.0, engine.state)
    assert result == pytest.approx(22.0)  # at t=0, offset is 0

    engine.state.elapsed_seconds = 300.0  # halfway through the 600s duration
    result = engine._apply_fmu_behavior(behavior, "ramp", 22.0, engine.state)
    assert result == pytest.approx(22.0 + 2.5)  # halfway to +5

    engine.state.elapsed_seconds = 600.0  # end of duration
    result = engine._apply_fmu_behavior(behavior, "ramp", 22.0, engine.state)
    assert result == pytest.approx(22.0 + 5.0)


# ─── 6. random_walk: persistent offset initialized at 0, min/max are offset bounds ──

def test_random_walk_offset_starts_at_zero_and_persists(database):
    engine = _engine(database)
    behavior = make_behavior("random_walk", json.dumps({
        "value": 999.0, "step": 0.5, "min": -5.0, "max": 5.0,
    }))
    first = engine._apply_fmu_behavior(behavior, "random_walk", 22.0, engine.state)
    # First call seeds the offset at 0 then applies at most one step.
    assert 22.0 - 0.5 <= first <= 22.0 + 0.5

    second = engine._apply_fmu_behavior(behavior, "random_walk", 22.0, engine.state)
    # Second call continues accumulating from the same offset, not reset to 0 --
    # both calls combined can drift at most two steps from the raw value.
    assert 22.0 - 1.0 <= second <= 22.0 + 1.0


def test_random_walk_offset_respects_bounds(database):
    engine = _engine(database)
    behavior = make_behavior("random_walk", json.dumps({
        "value": 999.0, "step": 100.0, "min": -2.0, "max": 2.0,
    }))
    result = engine._apply_fmu_behavior(behavior, "random_walk", 22.0, engine.state)
    assert 22.0 - 2.0 <= result <= 22.0 + 2.0


# ─── 7. schedule: FMU raw when no block active, absolute value when one is ──

def test_schedule_falls_back_to_fmu_raw_outside_any_block(database):
    engine = _engine(database)
    behavior = make_behavior("schedule", json.dumps({
        "default": 999.0,
        "blocks": [{"start": "08:00", "value": 72.0}, {"start": "18:00", "value": 68.0}],
    }))
    state = SimState(time_of_day=3.0, elapsed_seconds=0.0)  # before any block
    result = engine._apply_fmu_behavior(behavior, "schedule", 22.0, state)
    assert result == 22.0


def test_schedule_active_block_is_an_absolute_override(database):
    engine = _engine(database)
    behavior = make_behavior("schedule", json.dumps({
        "default": 999.0,
        "blocks": [{"start": "08:00", "value": 72.0}, {"start": "18:00", "value": 68.0}],
    }))
    state = SimState(time_of_day=10.0, elapsed_seconds=0.0)  # inside the 08:00 block
    result = engine._apply_fmu_behavior(behavior, "schedule", 22.0, state)
    assert result == 72.0  # absolute, not 22.0-derived


# ─── 8. fault: FMU raw when inactive (ignoring the inner/base behavior entirely) ──

def test_fault_inactive_uses_fmu_raw_not_inner_behavior(database):
    """The nested base_behavior='sine' would normally produce an absolute
    value far from the FMU's raw value while the fault is inactive -- for
    an FMU-owned point that inner value must be discarded entirely."""
    engine = _engine(database)
    behavior = make_behavior("fault", json.dumps({
        "base_behavior": "sine", "base_params": {"base": 500.0, "amplitude": 5.0},
        "fault_type": "spike", "fault_value": 300, "mtbf_minutes": 999999, "fault_duration_seconds": 30,
    }))
    result = engine._apply_fmu_behavior(behavior, "fault", 22.0, engine.state)
    assert result == 22.0


def test_fault_active_applies_the_configured_fault_result(database):
    engine = _engine(database)
    behavior = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 0},
        "fault_type": "spike", "fault_value": 300, "mtbf_minutes": 60, "fault_duration_seconds": 30,
    }))
    behavior._fault_active = True
    behavior._fault_end_elapsed = 999999.0
    result = engine._apply_fmu_behavior(behavior, "fault", 22.0, engine.state)
    assert result == 300.0  # the configured fault result, not raw+delta


def test_fault_cleared_returns_to_fmu_raw(database):
    engine = _engine(database)
    behavior = make_behavior("fault", json.dumps({
        "base_behavior": "constant", "base_params": {"value": 0},
        "fault_type": "spike", "fault_value": 300, "mtbf_minutes": 999999, "fault_duration_seconds": 30,
    }))
    # Never triggered (astronomically high mtbf) -- stays inactive.
    result = engine._apply_fmu_behavior(behavior, "fault", 22.0, engine.state)
    assert result == 22.0


# ─── 9. Behavior exceptions propagate out of the helper (tick() catches them) ──

class _RaisingBehavior(Behavior):
    def compute(self, state):
        raise RuntimeError("boom")


def test_apply_fmu_behavior_lets_exceptions_propagate(database):
    engine = _engine(database)
    with pytest.raises(RuntimeError, match="boom"):
        engine._apply_fmu_behavior(_RaisingBehavior(), "sine", 204.0, engine.state)


# ═══════════════════════════════════════════════════════════════════════════
# API-level: raw_provider_value diagnostics + unrestricted behavior editing
# ═══════════════════════════════════════════════════════════════════════════

OUTPUT_VARIABLE = "supply_duct_static_pressure_pa"


class _DiagnosticsEngine:
    def __init__(self) -> None:
        self._raw_provider_values: dict[int, Any] = {}

    async def reload(self) -> None:
        pass

    async def add_object_hot(self, device_instance: int, obj: dict) -> None:
        pass


class _FakeFMUProvider:
    def __init__(self, *, runtime_url, model, bindings, aggregate_inputs=None,
                 input_exposures=None, input_defaults, timeout_s, input_variables, output_variables) -> None:
        self.runtime_url = runtime_url
        self.model = model
        self.bindings = list(bindings)


@pytest.fixture
def client(client):
    client.app.state.engine = _DiagnosticsEngine()
    return client


@pytest.fixture
def engine(client) -> _DiagnosticsEngine:
    return client.app.state.engine


def _fake_rtu_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="RTU", label="RTU", provider_type="fmu", description="", parameters=(),
        variables=(VariableDefinition(OUTPUT_VARIABLE, "Supply Duct Static Pressure", "output"),),
        factory=lambda parameters: None, runtime_model="RTU",
    )


def _patch_definition(monkeypatch) -> ModelDefinition:
    definition = _fake_rtu_definition()
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _model_type: definition)
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _settings, _model_type: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    return definition


def _make_provider_owned_point(client, monkeypatch, *, instance: int):
    _patch_definition(monkeypatch)
    device = client.post("/devices", json={"device_instance": instance, "name": "RTU-Test"}).json()
    point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "Duct-Static-Pressure", "units": "pascals",
    }).json()
    client.post("/simulation/models", json={
        "name": "RTU-Test RTU", "provider_type": "fmu", "model_type": "RTU", "enabled": True,
        "created_from_device_id": device["id"], "parameters": {},
        "mappings": [{"variable": OUTPUT_VARIABLE, "direction": "output", "point_id": point["id"]}],
        "aggregate_mappings": [],
    })
    return device, point


def _update_payload(point: dict, *, behavior: str, behavior_params: dict) -> dict:
    return {
        "object_type": point["object_type"], "object_instance": point["object_instance"],
        "name": point["name"], "units": point["units"],
        "behavior": behavior, "behavior_params": json.dumps(behavior_params),
        "enabled": point["enabled"], "number_of_states": point.get("number_of_states", 2),
        "reliability": point.get("reliability", "no-fault-detected"), "polarity": point.get("polarity", "normal"),
    }


@pytest.mark.parametrize("behavior,params", [
    ("raw", {}),
    ("constant", {"value": 300}),
    ("noise", {"base": 0, "noise": 5}),
    ("sine", {"base": 20, "amplitude": 5, "period_hours": 24, "phase_hours": 0}),
    ("random_walk", {"value": 50, "step": 1, "min": -5, "max": 5}),
    ("ramp", {"from": 0, "to": 50, "duration_minutes": 10}),
    ("schedule", {"default": 18, "blocks": []}),
    ("manual", {"value": 500}),
    ("fault", {"base_behavior": "constant", "base_params": {"value": 0}, "fault_type": "spike", "fault_value": 300, "mtbf_minutes": 60, "fault_duration_seconds": 30}),
])
def test_every_behavior_type_is_accepted_on_a_provider_owned_point(client, database, monkeypatch, behavior, params):
    """Regression against the old freeze-behavior guard: every Behavior type
    now has defined FMU semantics, so none should be rejected or silently
    discarded when editing a provider-owned point."""
    device, point = _make_provider_owned_point(client, monkeypatch, instance=6201)
    resp = client.put(
        f"/devices/{device['id']}/objects/{point['id']}",
        json=_update_payload(point, behavior=behavior, behavior_params=params),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["behavior"] == behavior


def test_raw_provider_value_exposed_for_provider_owned_point(client, database, monkeypatch, engine):
    device, point = _make_provider_owned_point(client, monkeypatch, instance=6202)
    engine._raw_provider_values[point["id"]] = 204.0

    objects = client.get(f"/devices/{device['id']}/objects").json()
    obj = next(o for o in objects if o["id"] == point["id"])
    assert obj["raw_provider_value"] == 204.0
    assert obj["simulation_output_owner"] is not None


def test_raw_provider_value_absent_for_plain_point(client, database):
    device = client.post("/devices", json={"device_instance": 6203, "name": "Plain-Device"}).json()
    point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "Plain-Point", "units": "no-units",
    }).json()

    objects = client.get(f"/devices/{device['id']}/objects").json()
    obj = next(o for o in objects if o["id"] == point["id"])
    assert obj["raw_provider_value"] is None
