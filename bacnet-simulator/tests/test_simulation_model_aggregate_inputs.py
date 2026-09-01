"""Aggregate/MAX FMU input mappings (see the plan this was built from: an FMU
input driven by an operation over several BACnet points' live values, e.g.
uVAVDamMax = MAX(VAV1, VAV2, VAV3), instead of exactly one point or a
constant).

Two layers, matching the existing convention in this test suite
(test_simulation_session_recovery.py):
  - Provider-level: construct FMUSimulationProvider + FMUAggregateInput
    directly, fake only the FMURuntimeClient HTTP methods, no DB.
  - model_runtime-level: call _build_fmu_provider(config, engine) directly
    with a hand-built config dict (an aggregate mapping row can't round-trip
    through create_simulation_model/_replace_mappings today -- the DB table
    has no columns for it, which is why this feature is runtime-only for
    now), monkeypatching get_remote_model_definition + FMUSimulationProvider.

Aggregates are deliberately stricter than ordinary Point mappings at *every*
resolution point, init and step alike: a partial aggregate must never
compute its operation over just the remaining members, and must never fall
back to the FMU runtime's own metadata default -- both would produce a
valid-looking but semantically wrong control signal.
"""
from __future__ import annotations

import logging

import pytest

from src.simulation.models import runtime as model_runtime
from src.simulation.models.registry import ModelDefinition, VariableDefinition
from src.simulation.providers import FMUAggregateInput, FMUPointBinding, SimulationContext
from src.simulation.providers.fmu import (
    FMUAggregateStepError,
    FMUInputResolutionError,
    FMURuntimeResponse,
    FMUSimulationProvider,
)
from src.simulation.providers.base import ProviderStatus


def _never_call_runtime(*_args, **_kwargs):
    raise AssertionError("FMU runtime must not be contacted for an unresolved aggregate")


def _fake_health(self):
    return {"status": "ok"}


def _capturing_initialize(captured: dict):
    def _fake_initialize(self, model_id, inputs=None):
        captured["inputs"] = dict(inputs or {})
        return {"session_id": "s1", "state": "RUNNING"}
    return _fake_initialize


def _vav_member_binding_metadata(point_id: int, name: str) -> dict:
    return {
        "point_id": point_id,
        "variable": "u_vav_dam_max",
        "direction": "input",
        "point_name": name,
        "device_name": f"VAV-{point_id}",
        "device_id": point_id,
        "object_type": "analog-input",
        "object_instance": 1,
        "units": "percent",
    }


def _aggregate_context(initial_point_inputs: dict, *, extra_bindings: list[dict] | None = None) -> SimulationContext:
    return SimulationContext(
        participant_device_ids=[],
        point_configs=[],
        metadata={
            "provider_id": "fmu:SimpleAHU:8",
            "simulation_model_id": 8,
            "model": "SimpleAHU",
            "initial_point_inputs": initial_point_inputs,
            "bindings": [
                _vav_member_binding_metadata(101, "VAV1-Damper"),
                _vav_member_binding_metadata(102, "VAV2-Damper"),
                _vav_member_binding_metadata(103, "VAV3-Damper"),
                *(extra_bindings or []),
            ],
        },
    )


def _provider(*, bindings=None, aggregate_inputs=None) -> FMUSimulationProvider:
    return FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="SimpleAHU",
        bindings=bindings or [],
        aggregate_inputs=aggregate_inputs or [],
        input_defaults={},
        input_variables={"u_vav_dam_max", "supply_air_temp_c"},
        output_variables={"zone_temp_c"},
    )


# ---------------------------------------------------------------------------
# 1 & 3. MAX of 3 points returns the highest raw value; no conversion happens
#    in bacnet-simulator (conversion is server-side, out of process) -- so
#    asserting the raw un-converted MAX is what's sent covers both.
# ---------------------------------------------------------------------------

def test_initialize_max_of_three_points_uses_highest_raw_value(monkeypatch):
    agg = FMUAggregateInput(variable="u_vav_dam_max", operation="max", point_ids=(101, 102, 103))
    provider = _provider(aggregate_inputs=[agg])

    captured: dict = {}

    def _fake_initialize(self, model_id, inputs=None):
        captured["inputs"] = dict(inputs or {})
        return {"session_id": "s1", "state": "RUNNING"}

    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _fake_initialize)

    context = _aggregate_context({101: 62.0, 102: 81.0, 103: 74.0})
    provider.initialize(context)

    assert captured["inputs"]["u_vav_dam_max"] == 81.0


# ---------------------------------------------------------------------------
# 2. MAX changes when a different point becomes most open -- dynamic
#    re-resolution across steps.
# ---------------------------------------------------------------------------

def test_step_max_recomputes_when_a_different_point_becomes_highest(monkeypatch):
    agg = FMUAggregateInput(variable="u_vav_dam_max", operation="max", point_ids=(101, 102, 103))
    output_binding = FMUPointBinding(point_id=999, variable="zone_temp_c", direction="output")
    provider = _provider(aggregate_inputs=[agg], bindings=[output_binding])

    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})

    captured_payloads: list[dict] = []

    def _fake_step(self, model_id, payload):
        captured_payloads.append(dict(payload["inputs"]))
        return FMURuntimeResponse(
            status_code=200,
            raw_body="{}",
            body={"state": "RUNNING", "current_time": 5.0, "zone_temp_c": 22.0},
        )

    monkeypatch.setattr(type(provider._client), "step", _fake_step)

    context = _aggregate_context({101: 62.0, 102: 81.0, 103: 74.0})
    provider.initialize(context)
    provider.start()

    provider.step(5.0)
    assert provider.get_status() == ProviderStatus.RUNNING
    assert captured_payloads[-1]["u_vav_dam_max"] == 81.0

    # VAV3 becomes the new most-open damper.
    provider.set_inputs({103: 95.0})
    provider.step(5.0)
    assert provider.get_status() == ProviderStatus.RUNNING
    assert captured_payloads[-1]["u_vav_dam_max"] == 95.0


# ---------------------------------------------------------------------------
# 4. Missing aggregate source point fails initialize() -- never contacts the
#    runtime, never falls back to a partial MAX.
# ---------------------------------------------------------------------------

def test_initialize_aborts_on_missing_aggregate_member(monkeypatch, caplog):
    agg = FMUAggregateInput(variable="u_vav_dam_max", operation="max", point_ids=(101, 102, 103))
    provider = _provider(aggregate_inputs=[agg])
    monkeypatch.setattr(provider._client, "health", _never_call_runtime)
    monkeypatch.setattr(provider._client, "initialize", _never_call_runtime)

    # Point 102 has no live value at all.
    context = _aggregate_context({101: 62.0, 103: 74.0})

    caplog.set_level(logging.WARNING, logger="bacnet-sim")
    with pytest.raises(FMUInputResolutionError) as excinfo:
        provider.initialize(context)

    assert "missing" in str(excinfo.value).lower()
    assert "102" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. Non-numeric aggregate source value fails clearly -- distinct message
#    from "missing".
# ---------------------------------------------------------------------------

def test_initialize_aborts_on_non_numeric_aggregate_member(monkeypatch):
    agg = FMUAggregateInput(variable="u_vav_dam_max", operation="max", point_ids=(101, 102, 103))
    provider = _provider(aggregate_inputs=[agg])
    monkeypatch.setattr(provider._client, "health", _never_call_runtime)
    monkeypatch.setattr(provider._client, "initialize", _never_call_runtime)

    context = _aggregate_context({101: 62.0, 102: "fault", 103: 74.0})

    with pytest.raises(FMUInputResolutionError) as excinfo:
        provider.initialize(context)

    message = str(excinfo.value).lower()
    assert "non-numeric" in message
    assert "missing" not in message
    assert "102" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6. Empty point_ids list is rejected by validate().
# ---------------------------------------------------------------------------

def test_validate_rejects_empty_aggregate_point_ids():
    agg = FMUAggregateInput(variable="u_vav_dam_max", operation="max", point_ids=())
    provider = _provider(aggregate_inputs=[agg])

    result = provider.validate()

    assert not result.valid
    assert any("no source points" in error for error in result.errors)


# ---------------------------------------------------------------------------
# 7 & 8. Ordinary Point and Constant mappings are completely unaffected --
#    regression checks that adding aggregate support didn't touch their path.
# ---------------------------------------------------------------------------

def test_ordinary_point_mapping_unaffected_by_aggregate_support(monkeypatch):
    binding = FMUPointBinding(point_id=10141, variable="supply_air_temp_c", direction="input")
    provider = _provider(bindings=[binding])

    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    context = SimulationContext(
        participant_device_ids=[],
        point_configs=[],
        metadata={
            "initial_point_inputs": {10141: 20.0},
            "bindings": [
                {"point_id": 10141, "variable": "supply_air_temp_c", "direction": "input"},
            ],
        },
    )
    provider.initialize(context)

    assert captured["inputs"]["supply_air_temp_c"] == 20.0
    assert provider._session_id == "s1"


def test_constant_mapping_unaffected_by_aggregate_support(monkeypatch):
    provider = FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="SimpleAHU",
        bindings=[],
        aggregate_inputs=[],
        input_defaults={"internal_gain_w": 5000},
        input_variables={"internal_gain_w"},
        output_variables=set(),
    )

    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    context = SimulationContext(participant_device_ids=[], point_configs=[], metadata={})
    provider.initialize(context)

    assert captured["inputs"]["internal_gain_w"] == 5000


# ---------------------------------------------------------------------------
# 10. The FMU receives exactly one value for the aggregate variable, never
#     one per source point.
# ---------------------------------------------------------------------------

def test_step_and_init_payload_has_single_value_for_aggregate_variable(monkeypatch):
    agg = FMUAggregateInput(variable="u_vav_dam_max", operation="max", point_ids=(101, 102, 103))
    provider = _provider(aggregate_inputs=[agg])

    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    context = _aggregate_context({101: 62.0, 102: 81.0, 103: 74.0})
    provider.initialize(context)

    matching_keys = [k for k in captured["inputs"] if k == "u_vav_dam_max"]
    assert matching_keys == ["u_vav_dam_max"]
    assert captured["inputs"]["u_vav_dam_max"] == 81.0
    assert 101 not in captured["inputs"] and 102 not in captured["inputs"] and 103 not in captured["inputs"]


# ---------------------------------------------------------------------------
# 12. Runtime loss of one aggregate member: the step fails clearly, does NOT
#     compute MAX from the remaining members, and does NOT fall back to the
#     FMU's metadata default.
# ---------------------------------------------------------------------------

def test_step_fails_when_one_aggregate_member_is_lost_mid_run(monkeypatch):
    agg = FMUAggregateInput(variable="u_vav_dam_max", operation="max", point_ids=(101, 102, 103))
    output_binding = FMUPointBinding(point_id=999, variable="zone_temp_c", direction="output")
    provider = _provider(aggregate_inputs=[agg], bindings=[output_binding])

    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})

    step_calls: list[dict] = []

    def _fake_step(self, model_id, payload):
        step_calls.append(dict(payload["inputs"]))
        return FMURuntimeResponse(
            status_code=200, raw_body="{}",
            body={"state": "RUNNING", "current_time": 5.0, "zone_temp_c": 22.0},
        )

    monkeypatch.setattr(type(provider._client), "step", _fake_step)

    context = _aggregate_context({101: 62.0, 102: 81.0, 103: 74.0})
    provider.initialize(context)
    provider.start()

    provider.step(5.0)
    assert provider.get_status() == ProviderStatus.RUNNING
    assert step_calls[-1]["u_vav_dam_max"] == 81.0

    # Point 102 (currently the highest) goes stale/lost.
    provider._inputs.pop(102)
    monkeypatch.setattr(type(provider._client), "step", _never_call_runtime)

    provider.step(5.0)

    assert provider.get_status() == ProviderStatus.ERROR
    assert "102" in provider._error
    # The remaining members (62.0, 74.0) must never have been silently
    # combined into a partial MAX, and the FMU default must never have
    # been used -- the whole step aborted before contacting the runtime at
    # all (the monkeypatched .step() above asserts this itself).
    assert len(step_calls) == 1


# ---------------------------------------------------------------------------
# 9 & 11. model_runtime._build_fmu_provider() wiring: participant/input
#     point-id registration, and rejecting more than one source for the
#     same variable.
# ---------------------------------------------------------------------------

def _fake_ahu_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="SimpleAHU",
        label="Simple AHU",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(
            VariableDefinition("u_vav_dam_max", "Most-open VAV damper", "input"),
            VariableDefinition("supply_air_temp_c", "Supply Air Temperature", "input"),
            VariableDefinition("zone_temp_c", "Zone Temperature", "output"),
        ),
        factory=lambda parameters: None,
        runtime_model="SimpleAHU",
    )


class _FakeFMUProvider:
    created: list["_FakeFMUProvider"] = []

    def __init__(
        self,
        *,
        runtime_url,
        model,
        bindings,
        aggregate_inputs=None,
        input_exposures=None,
        input_defaults,
        timeout_s,
        input_variables,
        output_variables,
    ) -> None:
        self.runtime_url = runtime_url
        self.model = model
        self.bindings = list(bindings)
        self.aggregate_inputs = list(aggregate_inputs or [])
        self.input_exposures = list(input_exposures or [])
        self.input_defaults = dict(input_defaults)
        self.timeout_s = timeout_s
        self.input_variables = set(input_variables)
        self.output_variables = set(output_variables)
        self.created.append(self)


class _FakeEngine:
    def resolve_provider_input_value(self, point_id: int):
        return None

    def register_simulation_provider(self, *args, **kwargs) -> None:
        pass


def _aggregate_mapping_row(point_ids: list[int]) -> dict:
    return {
        "variable": "u_vav_dam_max",
        "direction": "input",
        "operation": "max",
        "point_ids": point_ids,
        "point_metadata": {
            pid: {"point_name": f"VAV{pid}-Damper", "device_name": f"VAV-{pid}", "device_id": pid, "units": "percent"}
            for pid in point_ids
        },
    }


def _base_config(mappings: list[dict]) -> dict:
    return {
        "id": 8,
        "provider_type": "fmu",
        "model_type": "SimpleAHU",
        "name": "AHU-1",
        "parameters": {},
        "mappings": mappings,
        "_settings": {},
    }


def test_build_fmu_provider_registers_all_aggregate_member_points(monkeypatch):
    definition = _fake_ahu_definition()
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _s, _m: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    _FakeFMUProvider.created.clear()

    config = _base_config([_aggregate_mapping_row([101, 102, 103])])
    provider, context, inputs, outputs = model_runtime._build_fmu_provider(config, _FakeEngine())

    assert inputs == {101, 102, 103}
    assert outputs == set()
    assert context.metadata["participant_device_ids"] == [101, 102, 103]
    assert context.metadata["input_device_ids"] == [101, 102, 103]
    assert context.metadata["aggregate_inputs"] == [
        {"variable": "u_vav_dam_max", "source": "aggregate", "operation": "max", "point_ids": [101, 102, 103]}
    ]
    member_bindings = [b for b in context.metadata["bindings"] if b["variable"] == "u_vav_dam_max"]
    assert {b["point_id"] for b in member_bindings} == {101, 102, 103}
    assert _FakeFMUProvider.created[-1].aggregate_inputs[0].point_ids == (101, 102, 103)


def test_build_fmu_provider_rejects_point_plus_aggregate_on_same_variable(monkeypatch):
    definition = _fake_ahu_definition()
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _s, _m: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)

    config = _base_config([
        {"variable": "u_vav_dam_max", "direction": "input", "point_id": 200},
        _aggregate_mapping_row([101, 102, 103]),
    ])

    with pytest.raises(ValueError, match="u_vav_dam_max"):
        model_runtime._build_fmu_provider(config, _FakeEngine())


def test_build_fmu_provider_rejects_two_aggregates_on_same_variable(monkeypatch):
    definition = _fake_ahu_definition()
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _s, _m: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)

    config = _base_config([
        _aggregate_mapping_row([101, 102]),
        _aggregate_mapping_row([201, 202]),
    ])

    with pytest.raises(ValueError, match="u_vav_dam_max"):
        model_runtime._build_fmu_provider(config, _FakeEngine())
