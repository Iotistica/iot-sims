"""Aggregate/MIN FMU input mappings -- adds "min" alongside the existing
"max" and "weighted_average" aggregate operations (see
tests/test_simulation_model_aggregate_inputs.py and
tests/test_simulation_model_weighted_average.py for those). MIN accepts
multiple selected value points and resolves as min(value1, ..., valueN),
following the exact same validation, persistence, UI, and runtime behavior
already used by Maximum -- the resolver, schema, and Pydantic layers were
already generic over "any non-weighted operation" before this change (see
FMUSimulationProvider._compute_aggregate in src/simulation/providers/fmu.py),
so this suite mirrors the existing MAX suites structurally, swapping the
operation and expected value.

Same three layers as the existing MAX/weighted_average suites:
  - Provider-level: construct FMUSimulationProvider + FMUAggregateInput
    directly, fake only the FMURuntimeClient HTTP methods, no DB.
  - model_runtime-level: call _build_fmu_provider(config, engine) directly
    with a hand-built config dict.
  - DB/API-level: real device/object creation via the `client` fixture,
    following test_simulation_model_aggregate_persistence.py's conventions
    -- covers configuration save/load (create, reload, update membership).

Also covers "Also write this resolved value to a point" (FMUInputExposure)
for a MIN aggregate, exactly as already proven for weighted_average in
tests/test_simulation_model_input_exposure.py -- the exposure mechanism
itself is operation-agnostic (it mirrors whatever _build_step_payload
already resolved for that input variable), so this is a direct regression
check that MIN participates in it identically.
"""
from __future__ import annotations

import pytest

from src.api.routers import simulation as simulation_router
from src.simulation.models import runtime as model_runtime
from src.simulation.models.store import get_simulation_model
from src.simulation.models.registry import ModelDefinition, VariableDefinition
from src.simulation.providers import (
    FMUAggregateInput,
    FMUInputExposure,
    FMUPointBinding,
    FMUSimulationProvider,
    ProviderStatus,
    SimulationContext,
)
from src.simulation.providers.fmu import FMUInputResolutionError, FMURuntimeResponse

AGGREGATE_VARIABLE = "min_zone_temp_c"


# ═══════════════════════════════════════════════════════════════════════════
# Provider-level tests (mirrors test_simulation_model_aggregate_inputs.py)
# ═══════════════════════════════════════════════════════════════════════════

def _fake_health(self):
    return {"status": "ok"}


def _zone_member_binding_metadata(point_id: int, name: str) -> dict:
    return {
        "point_id": point_id,
        "variable": AGGREGATE_VARIABLE,
        "direction": "input",
        "point_name": name,
        "device_name": f"Zone-{point_id}",
        "device_id": point_id,
        "object_type": "analog-input",
        "object_instance": 1,
        "units": "degC",
    }


def _aggregate_context(initial_point_inputs: dict, *, extra_bindings: list[dict] | None = None,
                        input_exposures: list[dict] | None = None) -> SimulationContext:
    return SimulationContext(
        participant_device_ids=[],
        point_configs=[],
        metadata={
            "provider_id": "fmu:SimpleAHU:8",
            "simulation_model_id": 8,
            "model": "SimpleAHU",
            "initial_point_inputs": initial_point_inputs,
            "bindings": [
                _zone_member_binding_metadata(101, "Zone1-Temp"),
                _zone_member_binding_metadata(102, "Zone2-Temp"),
                _zone_member_binding_metadata(103, "Zone3-Temp"),
                *(extra_bindings or []),
            ],
            "input_exposures": input_exposures or [],
        },
    )


def _provider(*, bindings=None, aggregate_inputs=None, input_exposures=None) -> FMUSimulationProvider:
    return FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="SimpleAHU",
        bindings=bindings or [],
        aggregate_inputs=aggregate_inputs or [],
        input_exposures=input_exposures or [],
        input_defaults={},
        input_variables={AGGREGATE_VARIABLE, "supply_air_temp_c"},
        output_variables={"zone_temp_c"},
    )


def test_initialize_min_of_three_points_uses_lowest_raw_value(monkeypatch):
    agg = FMUAggregateInput(variable=AGGREGATE_VARIABLE, operation="min", point_ids=(101, 102, 103))
    provider = _provider(aggregate_inputs=[agg])

    captured: dict = {}

    def _fake_initialize(self, model_id, inputs=None):
        captured["inputs"] = dict(inputs or {})
        return {"session_id": "s1", "state": "RUNNING"}

    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _fake_initialize)

    context = _aggregate_context({101: 22.0, 102: 18.5, 103: 24.0})
    provider.initialize(context)

    assert captured["inputs"][AGGREGATE_VARIABLE] == 18.5


def test_step_min_recomputes_when_a_different_point_becomes_lowest(monkeypatch):
    agg = FMUAggregateInput(variable=AGGREGATE_VARIABLE, operation="min", point_ids=(101, 102, 103))
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

    context = _aggregate_context({101: 22.0, 102: 18.5, 103: 24.0})
    provider.initialize(context)
    provider.start()

    provider.step(5.0)
    assert captured_payloads[-1][AGGREGATE_VARIABLE] == 18.5

    # Zone3 becomes the new coldest zone.
    provider.set_inputs({103: 12.0})
    provider.step(5.0)
    assert captured_payloads[-1][AGGREGATE_VARIABLE] == 12.0


def test_initialize_aborts_on_missing_aggregate_member_for_min(monkeypatch):
    agg = FMUAggregateInput(variable=AGGREGATE_VARIABLE, operation="min", point_ids=(101, 102, 103))
    provider = _provider(aggregate_inputs=[agg])

    def _never_call_runtime(*_args, **_kwargs):
        raise AssertionError("FMU runtime must not be contacted for an unresolved aggregate")

    monkeypatch.setattr(provider._client, "health", _never_call_runtime)
    monkeypatch.setattr(provider._client, "initialize", _never_call_runtime)

    # Point 102 (which would otherwise be the minimum) has no live value.
    context = _aggregate_context({101: 22.0, 103: 24.0})

    with pytest.raises(FMUInputResolutionError) as excinfo:
        provider.initialize(context)

    assert "missing" in str(excinfo.value).lower()
    assert "102" in str(excinfo.value)


def test_step_fails_when_one_aggregate_member_is_lost_mid_run_for_min(monkeypatch):
    """Mirrors MAX's own partial-resolution guarantee: MIN must never
    silently compute over the remaining members when one is lost -- the
    truly-coldest zone happening to be the member that went stale would
    make MIN(remaining) quietly over-report the true minimum, the exact
    MAX-side failure mode FMUAggregateStepError's docstring warns about,
    mirrored for MIN."""
    agg = FMUAggregateInput(variable=AGGREGATE_VARIABLE, operation="min", point_ids=(101, 102, 103))
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

    context = _aggregate_context({101: 22.0, 102: 18.5, 103: 24.0})
    provider.initialize(context)
    provider.start()

    provider.step(5.0)
    assert step_calls[-1][AGGREGATE_VARIABLE] == 18.5

    def _never_call_runtime(*_args, **_kwargs):
        raise AssertionError("FMU runtime must not be contacted for a partially-resolved MIN")

    # Point 102 (currently the coldest/minimum) goes stale/lost.
    provider._inputs.pop(102)
    monkeypatch.setattr(type(provider._client), "step", _never_call_runtime)

    provider.step(5.0)

    assert provider.get_status() == ProviderStatus.ERROR
    assert "102" in provider._error
    assert len(step_calls) == 1


def test_validate_accepts_min_operation():
    agg = FMUAggregateInput(variable=AGGREGATE_VARIABLE, operation="min", point_ids=(101, 102, 103))
    provider = _provider(aggregate_inputs=[agg])

    result = provider.validate()

    assert result.valid


# ─── "Also write this resolved value to a point" for MIN ──────────────────

def test_min_aggregate_with_input_exposure_writes_resolved_value_to_point(monkeypatch):
    """Mirrors test_weighted_average_input_exposure_matches_resolved_value
    in tests/test_simulation_model_input_exposure.py -- proves a MIN
    aggregate's resolved value is mirrored onto a second BACnet point
    without being recomputed, the same "Also write this resolved value to a
    point" behavior already available for Maximum/Weighted Average."""
    agg = FMUAggregateInput(variable=AGGREGATE_VARIABLE, operation="min", point_ids=(101, 102, 103))
    exposure = FMUInputExposure(variable=AGGREGATE_VARIABLE, point_id=888)
    output_binding = FMUPointBinding(point_id=999, variable="zone_temp_c", direction="output")
    provider = _provider(aggregate_inputs=[agg], input_exposures=[exposure], bindings=[output_binding])

    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})

    captured_payloads: list[dict] = []

    def _fake_step(self, model_id, payload):
        captured_payloads.append(dict(payload["inputs"]))
        return FMURuntimeResponse(
            status_code=200, raw_body="{}",
            body={"state": "RUNNING", "current_time": 5.0, "zone_temp_c": 22.0},
        )

    monkeypatch.setattr(type(provider._client), "step", _fake_step)

    context = _aggregate_context(
        {101: 22.0, 102: 18.5, 103: 24.0},
        input_exposures=[{
            "point_id": 888,
            "variable": AGGREGATE_VARIABLE,
            "point_name": "Zone-1-Min-Zone-Temp",
            "device_name": "Zone-1",
            "device_id": 1,
            "object_type": "analog-input",
            "object_instance": 9,
            "units": "degC",
        }],
    )
    provider.initialize(context)
    provider.start()
    provider.step(5.0)

    outputs = provider.get_outputs()
    # The exposure point holds exactly the resolved MIN value.
    assert outputs[888] == pytest.approx(18.5)
    # Not recomputed: bit-identical to the value the FMU payload actually
    # sent for the input variable this step.
    assert outputs[888] == captured_payloads[-1][AGGREGATE_VARIABLE]
    # The real output mapping is completely unaffected by the exposure.
    assert outputs[999] == pytest.approx(22.0)


# ═══════════════════════════════════════════════════════════════════════════
# model_runtime-level wiring (mirrors test_build_fmu_provider_registers_all_
# aggregate_member_points in test_simulation_model_aggregate_inputs.py)
# ═══════════════════════════════════════════════════════════════════════════

def _fake_ahu_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="SimpleAHU",
        label="Simple AHU",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(
            VariableDefinition(AGGREGATE_VARIABLE, "Minimum Zone Temperature", "input"),
            VariableDefinition("supply_air_temp_c", "Supply Air Temperature", "input"),
            VariableDefinition("zone_temp_c", "Zone Temperature", "output"),
        ),
        factory=lambda parameters: None,
        runtime_model="SimpleAHU",
    )


class _FakeFMUProvider:
    created: list["_FakeFMUProvider"] = []

    def __init__(
        self, *, runtime_url, model, bindings, aggregate_inputs=None, input_exposures=None,
        input_defaults, timeout_s, input_variables, output_variables,
    ) -> None:
        self.bindings = list(bindings)
        self.aggregate_inputs = list(aggregate_inputs or [])
        self.input_exposures = list(input_exposures or [])
        self.created.append(self)


class _FakeEngine:
    def resolve_provider_input_value(self, point_id: int):
        return None

    def register_simulation_provider(self, *args, **kwargs) -> None:
        pass


def _aggregate_mapping_row(point_ids: list[int]) -> dict:
    return {
        "variable": AGGREGATE_VARIABLE,
        "direction": "input",
        "operation": "min",
        "point_ids": point_ids,
        "point_metadata": {
            pid: {"point_name": f"Zone{pid}-Temp", "device_name": f"Zone-{pid}", "device_id": pid, "units": "degC"}
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


def test_build_fmu_provider_registers_min_aggregate_with_operation(monkeypatch):
    definition = _fake_ahu_definition()
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _s, _m: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    _FakeFMUProvider.created.clear()

    config = _base_config([_aggregate_mapping_row([101, 102, 103])])
    provider, context, inputs, outputs = model_runtime._build_fmu_provider(config, _FakeEngine())

    assert inputs == {101, 102, 103}
    assert context.metadata["aggregate_inputs"] == [
        {"variable": AGGREGATE_VARIABLE, "source": "aggregate", "operation": "min", "point_ids": [101, 102, 103]}
    ]
    assert _FakeFMUProvider.created[-1].aggregate_inputs[0].operation == "min"
    assert _FakeFMUProvider.created[-1].aggregate_inputs[0].point_ids == (101, 102, 103)


# ═══════════════════════════════════════════════════════════════════════════
# DB/API-level: configuration save/load (mirrors
# test_simulation_model_aggregate_persistence.py)
# ═══════════════════════════════════════════════════════════════════════════

class _MinimalSimEngine:
    async def reload(self) -> None:
        pass

    async def add_object_hot(self, device_instance: int, obj: dict) -> None:
        pass

    def get_simulation_providers(self):
        return {}

    def unregister_simulation_provider(self, runtime_id: str) -> bool:
        return False


@pytest.fixture
def client(client):
    client.app.state.engine = _MinimalSimEngine()
    return client


def _patch_definition(monkeypatch) -> ModelDefinition:
    definition = _fake_ahu_definition()
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _model_type: definition)
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _settings, _model_type: definition)
    return definition


def _make_device_and_points(client, *, count=3, instance=3101, object_type="analog-input"):
    device = client.post("/devices", json={"device_instance": instance, "name": "AHU-Min-Test"}).json()
    points = [
        client.post(f"/devices/{device['id']}/objects", json={
            "object_type": object_type,
            "object_instance": i + 1,
            "name": f"Zone{i + 1}-Temp",
            "units": "degC",
        }).json()
        for i in range(count)
    ]
    return device, points


def _min_aggregate_payload(device_id: int, point_ids: list[int], *, enabled: bool = False) -> dict:
    return {
        "name": "AHU-Min-Test Simple AHU",
        "provider_type": "fmu",
        "model_type": "SimpleAHU",
        "enabled": enabled,
        "created_from_device_id": device_id,
        "parameters": {"input_sources": {AGGREGATE_VARIABLE: "aggregate"}},
        "mappings": [],
        "aggregate_mappings": [
            {"variable": AGGREGATE_VARIABLE, "direction": "input", "operation": "min", "point_ids": point_ids},
        ],
    }


def test_create_min_aggregate_mapping_with_multiple_points(client, database, monkeypatch):
    """Multiple selected value points (task requirement) -- 4 points here,
    more than the minimum needed to prove "multiple", saved and returned
    with operation='min'."""
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client, count=4)
    point_ids = [p["id"] for p in points]

    resp = client.post("/simulation/models", json=_min_aggregate_payload(device["id"], point_ids))
    assert resp.status_code == 201
    agg = next(m for m in resp.json()["mappings"] if m["variable"] == AGGREGATE_VARIABLE)
    assert agg["operation"] == "min"
    assert sorted(agg["point_ids"]) == sorted(point_ids)


def test_reload_preserves_min_operation_and_point_ids(client, database, monkeypatch):
    """Configuration save/load (task requirement): operation='min' and the
    full point_ids list must survive a create -> GET round trip."""
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client)
    point_ids = [p["id"] for p in points]
    created = client.post("/simulation/models", json=_min_aggregate_payload(device["id"], point_ids)).json()

    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    agg = next(m for m in reloaded["mappings"] if m["variable"] == AGGREGATE_VARIABLE)
    assert agg["operation"] == "min"
    assert agg["point_ids"] == point_ids  # insertion order preserved


def test_update_min_aggregate_membership_replaces_members(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client, count=4)
    point_ids = [p["id"] for p in points]
    created = client.post("/simulation/models", json=_min_aggregate_payload(device["id"], point_ids[:3])).json()

    resp = client.put(f"/simulation/models/{created['id']}", json=_min_aggregate_payload(device["id"], point_ids[1:4]))
    assert resp.status_code == 200
    agg = next(m for m in resp.json()["mappings"] if m["variable"] == AGGREGATE_VARIABLE)
    assert agg["operation"] == "min"
    assert sorted(agg["point_ids"]) == sorted(point_ids[1:4])


def test_reconstructed_min_runtime_config_participates_in_min(client, database, monkeypatch):
    """Feeds a persisted min aggregate config into the REAL
    FMUSimulationProvider (not a fake), proving the full round trip --
    create -> persist -> reload -> runtime wiring -> actual MIN computation
    over multiple values -- works end to end, mirroring
    test_ten_point_aggregate_survives_persistence_and_participates_in_max."""
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client, count=5)
    point_ids = [p["id"] for p in points]

    created = client.post("/simulation/models", json=_min_aggregate_payload(device["id"], point_ids)).json()
    agg = next(m for m in created["mappings"] if m["variable"] == AGGREGATE_VARIABLE)
    assert len(agg["point_ids"]) == 5

    config = {**get_simulation_model(database, created["id"]), "_settings": {}}
    provider, context, inputs, outputs = model_runtime._build_fmu_provider(config, _FakeEngine())

    assert inputs == set(point_ids)
    agg_input = provider._aggregate_inputs[0]
    assert agg_input.operation == "min"
    assert sorted(agg_input.point_ids) == sorted(point_ids)

    # Lowest value deliberately placed on an interior point (index 2 of 5),
    # not the first or last, so a slice/truncation bug at either end of the
    # list would surface as a wrong MIN instead of accidentally still
    # passing.
    values = {pid: float(20 + i) for i, pid in enumerate(point_ids)}
    lowest_point_id = point_ids[2]
    values[lowest_point_id] = -5.0
    provider.set_inputs(values)

    raw_values, missing, non_numeric = provider._resolve_aggregate_source_values(agg_input)
    assert missing == [] and non_numeric == []
    assert len(raw_values) == 5
    assert provider._compute_aggregate(agg_input, raw_values) == -5.0


def test_min_and_max_aggregates_on_different_variables_coexist(client, database, monkeypatch):
    """Not mutually exclusive: a model can have one variable driven by MIN
    and another by MAX at the same time -- regression check that adding MIN
    didn't somehow make it a replacement for MAX rather than an addition."""
    definition = ModelDefinition(
        model_type="SimpleAHU",
        label="Simple AHU",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(
            VariableDefinition(AGGREGATE_VARIABLE, "Minimum Zone Temperature", "input"),
            VariableDefinition("most_open_vav_damper_pct", "Most-Open VAV Damper", "input"),
        ),
        factory=lambda parameters: None,
        runtime_model="SimpleAHU",
    )
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _model_type: definition)
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _settings, _model_type: definition)

    device, points = _make_device_and_points(client, count=3)
    point_ids = [p["id"] for p in points]

    payload = {
        "name": "AHU-Min-Max-Test",
        "provider_type": "fmu",
        "model_type": "SimpleAHU",
        "enabled": False,
        "created_from_device_id": device["id"],
        "parameters": {"input_sources": {
            AGGREGATE_VARIABLE: "aggregate",
            "most_open_vav_damper_pct": "aggregate",
        }},
        "mappings": [],
        "aggregate_mappings": [
            {"variable": AGGREGATE_VARIABLE, "direction": "input", "operation": "min", "point_ids": point_ids},
            {"variable": "most_open_vav_damper_pct", "direction": "input", "operation": "max", "point_ids": point_ids},
        ],
    }
    resp = client.post("/simulation/models", json=payload)
    assert resp.status_code == 201
    mappings_by_variable = {m["variable"]: m for m in resp.json()["mappings"]}
    assert mappings_by_variable[AGGREGATE_VARIABLE]["operation"] == "min"
    assert mappings_by_variable["most_open_vav_damper_pct"]["operation"] == "max"
