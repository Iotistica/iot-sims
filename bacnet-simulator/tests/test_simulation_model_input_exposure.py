"""Input exposures: mirroring an already-resolved model INPUT value onto a
second BACnet point's Present Value, without recomputing it.

Motivating example (see FMUInputExposure's docstring in
src/simulation/providers/fmu.py): an RTU's `return_air_temp_c` input is
resolved as a Weighted Average of zone temperatures weighted by VAV
airflows. That resolved value should ALSO update a real BACnet point
(e.g. `RTU-1-Return-Air-Temp`) so operators/trends can see it, without the
FMU provider computing the weighted average a second time.

Same two-layer split as the existing aggregate test suite
(tests/test_simulation_model_weighted_average.py):
  - Provider-level: construct FMUSimulationProvider + FMUAggregateInput +
    FMUInputExposure directly, fake only the FMURuntimeClient HTTP methods,
    no DB. This is where the core "same value, not recomputed" proof lives.
  - DB/API-level: real device/object creation via the `client` fixture,
    following test_simulation_model_aggregate_persistence.py's conventions.
"""
from __future__ import annotations

import pytest

from src.api.routers import simulation as simulation_router
from src.simulation import model_runtime
from src.simulation.model_store import get_simulation_model
from src.simulation.models.remote_catalog import normalize_remote_model_id
from src.simulation.models.registry import ModelDefinition, VariableDefinition
from src.simulation.providers import (
    FMUAggregateInput,
    FMUInputExposure,
    FMUPointBinding,
    FMUSimulationProvider,
    SimulationContext,
)
from src.simulation.providers.fmu import FMURuntimeResponse


RETURN_AIR_TEMP = "return_air_temp_c"
SUPPLY_AIR_TEMP = "supply_air_temp_c"


# ═══════════════════════════════════════════════════════════════════════════
# Provider-level tests
# ═══════════════════════════════════════════════════════════════════════════

def _fake_health(self):
    return {"status": "ok"}


def _member_binding_metadata(point_id: int, name: str, variable: str) -> dict:
    return {
        "point_id": point_id,
        "variable": variable,
        "direction": "input",
        "point_name": name,
        "device_name": f"Zone-{point_id}",
        "device_id": point_id,
        "object_type": "analog-input",
        "object_instance": 1,
        "units": "degC",
    }


def _rtu_context(
    initial_point_inputs: dict,
    *,
    value_ids=(101, 102, 103),
    weight_ids=(201, 202, 203),
    exposure_point_id: int = 888,
    exposure_metadata: dict | None = None,
) -> SimulationContext:
    bindings = [
        _member_binding_metadata(pid, f"Zone{i + 1}-Temp", RETURN_AIR_TEMP)
        for i, pid in enumerate(value_ids)
    ] + [
        _member_binding_metadata(wid, f"VAV{i + 1}-Airflow", RETURN_AIR_TEMP)
        for i, wid in enumerate(weight_ids)
    ]
    return SimulationContext(
        participant_device_ids=[],
        point_configs=[],
        metadata={
            "provider_id": "fmu:RTU:8",
            "simulation_model_id": 8,
            "model": "RTU",
            "initial_point_inputs": initial_point_inputs,
            "bindings": bindings,
            "input_exposures": [
                exposure_metadata or {
                    "point_id": exposure_point_id,
                    "variable": RETURN_AIR_TEMP,
                    "point_name": "RTU-1-Return-Air-Temp",
                    "device_name": "RTU-1",
                    "device_id": 1,
                    "object_type": "analog-input",
                    "object_instance": 9,
                    "units": "degC",
                },
            ],
        },
    )


def _provider(*, bindings=None, aggregate_inputs=None, input_exposures=None) -> FMUSimulationProvider:
    return FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="RTU",
        bindings=bindings or [],
        aggregate_inputs=aggregate_inputs or [],
        input_exposures=input_exposures or [],
        input_defaults={},
        input_variables={RETURN_AIR_TEMP, SUPPLY_AIR_TEMP},
        output_variables={"supply_air_temp_c_out"},
    )


# ─── 1. THE core proof: Weighted Average -> TRet -> mapped point, same value ─

def test_weighted_average_input_exposure_matches_resolved_value(monkeypatch):
    """The exact scenario from the task spec: return_air_temp_c is resolved
    as a Weighted Average of zone temps (values) weighted by VAV airflows
    (weights); an exposure mirrors that SAME resolved value onto BACnet
    point 888 (standing in for RTU-1-Return-Air-Temp). Also proves a real
    FMU output (supply_air_temp_c_out -> point 999) keeps working
    unchanged alongside the exposure."""
    agg = FMUAggregateInput(
        variable=RETURN_AIR_TEMP, operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    exposure = FMUInputExposure(variable=RETURN_AIR_TEMP, point_id=888)
    output_binding = FMUPointBinding(point_id=999, variable="supply_air_temp_c_out", direction="output")
    provider = _provider(aggregate_inputs=[agg], input_exposures=[exposure], bindings=[output_binding])
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})

    captured_payloads: list[dict] = []

    def _fake_step(self, model_id, payload):
        captured_payloads.append(dict(payload["inputs"]))
        # The FMU runtime's response never echoes back an INPUT variable
        # (return_air_temp_c) -- only its own outputs. If the exposure
        # were (incorrectly) trying to read it from `result` instead of
        # from the already-resolved input_report, it would find nothing.
        return FMURuntimeResponse(
            status_code=200, raw_body="{}",
            body={"state": "RUNNING", "current_time": 5.0, "supply_air_temp_c_out": 13.0},
        )

    monkeypatch.setattr(type(provider._client), "step", _fake_step)

    # Zone temps 20/22/24 C, weights (airflow) 1.0/2.0/1.0 m3/s.
    context = _rtu_context({101: 20.0, 102: 22.0, 103: 24.0, 201: 1.0, 202: 2.0, 203: 1.0})
    provider.initialize(context)
    provider.start()
    provider.step(5.0)

    expected = (20.0 * 1.0 + 22.0 * 2.0 + 24.0 * 1.0) / (1.0 + 2.0 + 1.0)
    assert expected == pytest.approx(22.0)  # sanity-check the hand computation

    outputs = provider.get_outputs()
    # The exposure point holds exactly the resolved weighted-average value.
    assert outputs[888] == pytest.approx(expected)
    # Not recomputed: it is the literal SAME value the FMU payload sent for
    # the input variable this step (bit-identical, not just numerically
    # close -- proves no second/independent calculation happened).
    assert outputs[888] == captured_payloads[-1][RETURN_AIR_TEMP]
    # The real output mapping is completely unaffected by the exposure.
    assert outputs[999] == pytest.approx(13.0)


def test_weighted_average_input_exposure_stays_within_zone_temp_bounds(monkeypatch):
    """The exact regression the task spec asked for: three zone temps in
    the 22-25 C band, each weighted by a plausible VAV airflow (CFM-scale,
    i.e. two to three orders of magnitude larger than the temperatures
    themselves -- the scale most likely to expose a units/accumulator bug,
    since a broken pipeline that divides by the wrong quantity would
    produce a value far outside this narrow band, e.g. ~0.01). A weighted
    average can never fall outside [min(inputs), max(inputs)] by
    definition -- if the exposed BACnet value isn't in that band, it is
    not actually the resolved weighted average."""
    agg = FMUAggregateInput(
        variable=RETURN_AIR_TEMP, operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    exposure = FMUInputExposure(variable=RETURN_AIR_TEMP, point_id=888)
    output_binding = FMUPointBinding(point_id=999, variable="supply_air_temp_c_out", direction="output")
    provider = _provider(aggregate_inputs=[agg], input_exposures=[exposure], bindings=[output_binding])
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})
    monkeypatch.setattr(
        type(provider._client), "step",
        lambda self, model_id, payload: FMURuntimeResponse(
            status_code=200, raw_body="{}",
            body={"state": "RUNNING", "current_time": 5.0, "supply_air_temp_c_out": 13.0},
        ),
    )

    zone_temps = {101: 22.4, 102: 24.1, 103: 23.0}
    airflows = {201: 320.0, 202: 480.0, 203: 350.0}
    context = _rtu_context({**zone_temps, **airflows})
    provider.initialize(context)
    provider.start()
    provider.step(5.0)

    numerator = sum(zone_temps[v] * airflows[w] for v, w in zip((101, 102, 103), (201, 202, 203)))
    denominator = sum(airflows.values())
    expected = numerator / denominator

    exposed_value = provider.get_outputs()[888]
    assert exposed_value == pytest.approx(expected)
    assert min(zone_temps.values()) <= exposed_value <= max(zone_temps.values())
    # Not the FMU's own output for this step (13.0), not a normalized
    # weight (each airflow / sum(airflow) would be < 1.0), not the bare
    # accumulator (sum(value*weight) or sum(weight) alone, both far
    # outside the zone-temp band).
    assert exposed_value != pytest.approx(13.0)
    assert exposed_value != pytest.approx(numerator)
    assert exposed_value != pytest.approx(denominator)
    for w in airflows:
        assert exposed_value != pytest.approx(airflows[w] / denominator)


def test_input_exposure_tracks_recomputed_value_across_steps(monkeypatch):
    """Weighted Average is re-resolved every step (per its own dynamic-
    input contract); the exposure must track each new resolved value, not
    freeze at whatever was true on the first step."""
    agg = FMUAggregateInput(
        variable=RETURN_AIR_TEMP, operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    exposure = FMUInputExposure(variable=RETURN_AIR_TEMP, point_id=888)
    output_binding = FMUPointBinding(point_id=999, variable="supply_air_temp_c_out", direction="output")
    provider = _provider(aggregate_inputs=[agg], input_exposures=[exposure], bindings=[output_binding])
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})
    monkeypatch.setattr(
        type(provider._client), "step",
        lambda self, model_id, payload: FMURuntimeResponse(
            status_code=200, raw_body="{}",
            body={"state": "RUNNING", "current_time": 5.0, "supply_air_temp_c_out": 13.0},
        ),
    )

    context = _rtu_context({101: 20.0, 102: 22.0, 103: 24.0, 201: 1.0, 202: 1.0, 203: 1.0})
    provider.initialize(context)
    provider.start()

    provider.step(5.0)
    assert provider.get_outputs()[888] == pytest.approx((20.0 + 22.0 + 24.0) / 3.0)

    # Zone 1 heats up and its VAV opens further.
    provider.set_inputs({101: 30.0, 201: 3.0})
    provider.step(5.0)
    expected = (30.0 * 3.0 + 22.0 * 1.0 + 24.0 * 1.0) / (3.0 + 1.0 + 1.0)
    assert provider.get_outputs()[888] == pytest.approx(expected)


def test_input_exposure_works_for_plain_point_sourced_input(monkeypatch):
    """Generic across all three input modes: a Point-sourced input (not an
    Aggregate) can be exposed too."""
    input_binding = FMUPointBinding(point_id=501, variable=RETURN_AIR_TEMP, direction="input")
    output_binding = FMUPointBinding(point_id=999, variable="supply_air_temp_c_out", direction="output")
    exposure = FMUInputExposure(variable=RETURN_AIR_TEMP, point_id=888)
    provider = _provider(bindings=[input_binding, output_binding], input_exposures=[exposure])
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})
    monkeypatch.setattr(
        type(provider._client), "step",
        lambda self, model_id, payload: FMURuntimeResponse(
            status_code=200, raw_body="{}",
            body={"state": "RUNNING", "current_time": 5.0, "supply_air_temp_c_out": 13.0},
        ),
    )

    context = _rtu_context({501: 21.5}, value_ids=(), weight_ids=())
    provider.initialize(context)
    provider.start()
    provider.step(5.0)

    assert provider.get_outputs()[888] == pytest.approx(21.5)


def test_input_exposure_works_for_constant_sourced_input(monkeypatch):
    """Generic across all three input modes: a Constant/default-sourced
    input (no BACnet mapping at all) can be exposed too."""
    output_binding = FMUPointBinding(point_id=999, variable="supply_air_temp_c_out", direction="output")
    exposure = FMUInputExposure(variable=RETURN_AIR_TEMP, point_id=888)
    provider = FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="RTU",
        bindings=[output_binding],
        input_exposures=[exposure],
        input_defaults={RETURN_AIR_TEMP: 24.0},
        input_variables={RETURN_AIR_TEMP, SUPPLY_AIR_TEMP},
        output_variables={"supply_air_temp_c_out"},
    )
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})
    monkeypatch.setattr(
        type(provider._client), "step",
        lambda self, model_id, payload: FMURuntimeResponse(
            status_code=200, raw_body="{}",
            body={"state": "RUNNING", "current_time": 5.0, "supply_air_temp_c_out": 13.0},
        ),
    )

    context = _rtu_context({}, value_ids=(), weight_ids=())
    provider.initialize(context)
    provider.start()
    provider.step(5.0)

    assert provider.get_outputs()[888] == pytest.approx(24.0)


def test_input_exposure_skipped_when_source_unresolved(monkeypatch):
    """If the sourcing Point mapping has no live value yet, the exposure
    simply doesn't write this tick (no None/garbage value) -- same
    tolerance an ordinary unresolved Point input already gets."""
    input_binding = FMUPointBinding(point_id=501, variable=RETURN_AIR_TEMP, direction="input")
    output_binding = FMUPointBinding(point_id=999, variable="supply_air_temp_c_out", direction="output")
    exposure = FMUInputExposure(variable=RETURN_AIR_TEMP, point_id=888)
    provider = _provider(bindings=[input_binding, output_binding], input_exposures=[exposure])
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})
    monkeypatch.setattr(
        type(provider._client), "step",
        lambda self, model_id, payload: FMURuntimeResponse(
            status_code=200, raw_body="{}",
            body={"state": "RUNNING", "current_time": 5.0, "supply_air_temp_c_out": 13.0},
        ),
    )

    # No live value seeded for point 501 at all.
    context = _rtu_context({}, value_ids=(), weight_ids=())
    monkeypatch.setattr(
        FMUSimulationProvider, "_resolve_init_inputs",
        lambda self: ({}, []),  # bypass the "cannot initialize" guard for this test
    )
    provider.initialize(context)
    provider.start()
    provider.step(5.0)

    assert 888 not in provider.get_outputs()
    assert provider.get_outputs()[999] == pytest.approx(13.0)


def test_validate_rejects_exposure_point_also_used_as_output():
    output_binding = FMUPointBinding(point_id=999, variable="supply_air_temp_c_out", direction="output")
    exposure = FMUInputExposure(variable=RETURN_AIR_TEMP, point_id=999)
    provider = _provider(bindings=[output_binding], input_exposures=[exposure])
    result = provider.validate()
    assert not result.valid
    assert any("cannot be both an output binding and an input exposure" in e for e in result.errors)


def test_validate_rejects_duplicate_exposure_target_point():
    exposure_a = FMUInputExposure(variable=RETURN_AIR_TEMP, point_id=888)
    exposure_b = FMUInputExposure(variable=SUPPLY_AIR_TEMP, point_id=888)
    provider = _provider(input_exposures=[exposure_a, exposure_b])
    result = provider.validate()
    assert not result.valid
    assert any("targeted by more than one input exposure" in e for e in result.errors)


def test_validate_rejects_exposure_for_unknown_input_variable():
    exposure = FMUInputExposure(variable="not_a_real_input", point_id=888)
    provider = _provider(input_exposures=[exposure])
    result = provider.validate()
    assert not result.valid
    assert any("Unsupported FMU input field: not_a_real_input" in e for e in result.errors)


# ═══════════════════════════════════════════════════════════════════════════
# DB/API-level tests
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


def _rtu_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="RTU",
        label="RTU",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(
            VariableDefinition(RETURN_AIR_TEMP, "Return Air Temperature", "input"),
            VariableDefinition(SUPPLY_AIR_TEMP, "Supply Air Temperature", "output"),
        ),
        factory=lambda parameters: None,
        runtime_model="RTU",
    )


def _patch_definition(monkeypatch) -> ModelDefinition:
    definition = _rtu_definition()
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _model_type: definition)
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _settings, _model_type: definition)
    return definition


def _make_rtu_device(client, *, count=3, instance=4101):
    """One device with `count` zone-temp (value) points, `count` VAV-
    airflow (weight) points, an output point, and a spare analog point to
    use as the input-exposure target (RTU-1-Return-Air-Temp)."""
    device = client.post("/devices", json={"device_instance": instance, "name": "RTU-1"}).json()
    value_points = [
        client.post(f"/devices/{device['id']}/objects", json={
            "object_type": "analog-input", "object_instance": i + 1,
            "name": f"Zone{i + 1}-Temp", "units": "degC",
        }).json()
        for i in range(count)
    ]
    weight_points = [
        client.post(f"/devices/{device['id']}/objects", json={
            "object_type": "analog-input", "object_instance": 100 + i + 1,
            "name": f"VAV{i + 1}-Airflow", "units": "cfm",
        }).json()
        for i in range(count)
    ]
    output_point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-output", "object_instance": 200,
        "name": "Supply-Air-Temp", "units": "degC",
    }).json()
    exposure_point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 201,
        "name": "RTU-1-Return-Air-Temp", "units": "degC",
    }).json()
    return device, value_points, weight_points, output_point, exposure_point


def _rtu_payload(
    device_id: int, point_ids: list[int], weight_point_ids: list[int],
    output_point_id: int, exposure_point_id: int | None, *, enabled: bool = False,
) -> dict:
    payload = {
        "name": "RTU-1 RTU",
        "provider_type": "fmu",
        "model_type": "RTU",
        "enabled": enabled,
        "created_from_device_id": device_id,
        "parameters": {"input_sources": {RETURN_AIR_TEMP: "aggregate"}},
        "mappings": [
            {"variable": SUPPLY_AIR_TEMP, "direction": "output", "point_id": output_point_id},
        ],
        "aggregate_mappings": [
            {
                "variable": RETURN_AIR_TEMP, "direction": "input",
                "operation": "weighted_average",
                "point_ids": point_ids, "weight_point_ids": weight_point_ids,
            },
        ],
    }
    if exposure_point_id is not None:
        payload["input_exposures"] = [{"variable": RETURN_AIR_TEMP, "point_id": exposure_point_id}]
    return payload


def test_create_weighted_average_input_exposure(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points, output_point, exposure_point = _make_rtu_device(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    resp = client.post(
        "/simulation/models",
        json=_rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], exposure_point["id"]),
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()

    assert created["input_exposures"] == [
        {
            "id": created["input_exposures"][0]["id"],
            "model_config_id": created["id"],
            "variable": RETURN_AIR_TEMP,
            "point_id": exposure_point["id"],
            "device_id": device["id"],
            "point_name": "RTU-1-Return-Air-Temp",
            "object_type": "analog-input",
            "object_instance": 201,
            "units": "degC",
            "point_type": None,
            "device_name": "RTU-1",
        }
    ]


def test_reload_preserves_input_exposures(client, database, monkeypatch):
    """Serialization/deserialization: GET after POST returns the same
    exposure -- proves persistence round-trips, not just the create
    response."""
    _patch_definition(monkeypatch)
    device, value_points, weight_points, output_point, exposure_point = _make_rtu_device(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    created = client.post(
        "/simulation/models",
        json=_rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], exposure_point["id"]),
    ).json()

    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    assert len(reloaded["input_exposures"]) == 1
    assert reloaded["input_exposures"][0]["variable"] == RETURN_AIR_TEMP
    assert reloaded["input_exposures"][0]["point_id"] == exposure_point["id"]


def test_update_replaces_input_exposures(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points, output_point, exposure_point = _make_rtu_device(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    created = client.post(
        "/simulation/models",
        json=_rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], exposure_point["id"]),
    ).json()

    # Update with the exposure removed entirely.
    payload = _rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], None)
    updated = client.put(f"/simulation/models/{created['id']}", json=payload).json()
    assert updated["input_exposures"] == []

    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    assert reloaded["input_exposures"] == []


def test_create_input_exposure_rejects_unknown_variable(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points, output_point, exposure_point = _make_rtu_device(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    payload = _rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], None)
    payload["input_exposures"] = [{"variable": "not_a_real_input", "point_id": exposure_point["id"]}]
    resp = client.post("/simulation/models", json=payload)
    assert resp.status_code == 422


def test_create_input_exposure_rejects_output_point_conflict(client, database, monkeypatch):
    """An exposure can't target the SAME point this model already owns as
    a plain output mapping."""
    _patch_definition(monkeypatch)
    device, value_points, weight_points, output_point, _exposure_point = _make_rtu_device(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    payload = _rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], None)
    payload["input_exposures"] = [{"variable": RETURN_AIR_TEMP, "point_id": output_point["id"]}]
    resp = client.post("/simulation/models", json=payload)
    assert resp.status_code == 422


def test_create_input_exposure_rejects_point_owned_by_another_model(client, database, monkeypatch):
    """Cross-model conflict: point already an output of a DIFFERENT model
    is rejected with 409, mirroring the existing output-ownership check."""
    _patch_definition(monkeypatch)
    device, value_points, weight_points, output_point, exposure_point = _make_rtu_device(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    # First model explicitly owns exposure_point as a plain output.
    other_payload = {
        "name": "Other Model", "provider_type": "fmu", "model_type": "RTU", "enabled": False,
        "created_from_device_id": device["id"],
        "parameters": {"input_sources": {RETURN_AIR_TEMP: "constant"}, "input_defaults": {RETURN_AIR_TEMP: 22.0}},
        "mappings": [{"variable": SUPPLY_AIR_TEMP, "direction": "output", "point_id": exposure_point["id"]}],
        "aggregate_mappings": [],
    }
    client.post("/simulation/models", json=other_payload)

    payload = _rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], exposure_point["id"])
    resp = client.post("/simulation/models", json=payload)
    assert resp.status_code == 409


def test_reconstructed_runtime_config_wires_input_exposure(client, database, monkeypatch):
    """Closes the persistence -> runtime gap: a saved exposure comes back
    from model_store, gets wired into _build_fmu_provider, ends up in the
    provider's output point-id claim set (required so the engine doesn't
    discard it as an "undeclared output point"), and the real provider
    resolves the exact same weighted-average value onto it."""
    _patch_definition(monkeypatch)
    device, value_points, weight_points, output_point, exposure_point = _make_rtu_device(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    created = client.post(
        "/simulation/models",
        json=_rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], exposure_point["id"]),
    ).json()

    class _FakeEngine:
        def resolve_provider_input_value(self, point_id: int):
            return None

    config = {**get_simulation_model(database, created["id"]), "_settings": {}}
    provider, context, inputs, outputs = model_runtime._build_fmu_provider(config, _FakeEngine())

    # The exposure point must be claimed as an output, alongside the real
    # supply_air_temp_c output mapping -- otherwise the engine would log
    # "undeclared output point" and silently drop it (see
    # legacy.py::_run_registered_providers).
    assert outputs == {output_point["id"], exposure_point["id"]}
    assert context.metadata["input_exposures"] == [{
        "point_id": exposure_point["id"],
        "variable": RETURN_AIR_TEMP,
        "point_name": "RTU-1-Return-Air-Temp",
        "device_name": "RTU-1",
        "device_id": device["id"],
        "object_type": "analog-input",
        "object_instance": 201,
        "units": "degC",
    }]

    values = {point_ids[0]: 19.0, point_ids[1]: 23.0, point_ids[2]: 29.0}
    weights = {weight_point_ids[0]: 2.0, weight_point_ids[1]: 1.0, weight_point_ids[2]: 1.0}
    provider.set_inputs({**values, **weights})

    agg_input = provider._aggregate_inputs[0]
    result, detail, _diag = provider._resolve_one_aggregate(agg_input)
    assert detail is None
    expected = (19.0 * 2.0 + 23.0 * 1.0 + 29.0 * 1.0) / (2.0 + 1.0 + 1.0)
    assert result == pytest.approx(expected)


def test_existing_output_mapping_unaffected_by_input_exposures(client, database, monkeypatch):
    """Regression: a model with NO input_exposures configured behaves
    byte-identically to before this feature existed."""
    _patch_definition(monkeypatch)
    device, value_points, weight_points, output_point, _exposure_point = _make_rtu_device(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    created = client.post(
        "/simulation/models",
        json=_rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], None),
    ).json()
    assert created.get("input_exposures") == []

    class _FakeEngine:
        def resolve_provider_input_value(self, point_id: int):
            return None

    config = {**get_simulation_model(database, created["id"]), "_settings": {}}
    provider, context, inputs, outputs = model_runtime._build_fmu_provider(config, _FakeEngine())
    assert outputs == {output_point["id"]}
    assert context.metadata["input_exposures"] == []


def test_list_objects_reports_exposure_owner_for_exposure_point(client, database, monkeypatch):
    """The admin UI's Objects table Behavior column reads each object's
    `simulation_output_owner` field to decide whether to show an "FMU" tag
    instead of the point's raw, stale `behavior` -- previously this only
    checked plain output mappings (`get_output_owners_by_point`), so an
    exposure-owned point like RTU-1-Return-Air-Temp fell through and showed
    "constant" even though it's genuinely driven every tick. Confirms
    GET /devices/{id}/objects now also surfaces exposure ownership, without
    disturbing the existing plain-output-owner reporting."""
    _patch_definition(monkeypatch)
    device, value_points, weight_points, output_point, exposure_point = _make_rtu_device(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    # Created as a draft (enabled=False) -- this file's _MinimalSimEngine
    # fixture doesn't implement register_simulation_provider, so an
    # enabled=True POST would fail activation (same reason every other
    # test in this file creates drafts). Flip enabled=1 directly on the
    # DB row afterward: this test is about list_objects()'s SQL wiring
    # (get_exposure_owners_by_point's enabled=1 filter), not about the
    # runtime engine actually registering a provider.
    created = client.post(
        "/simulation/models",
        json=_rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], exposure_point["id"], enabled=False),
    ).json()
    with database._conn() as conn:
        conn.execute("UPDATE simulation_model_configs SET enabled=1 WHERE id=?", (created["id"],))
        conn.commit()

    objects = client.get(f"/devices/{device['id']}/objects").json()
    exposure_row = next(o for o in objects if o["id"] == exposure_point["id"])
    output_row = next(o for o in objects if o["id"] == output_point["id"])

    # model_type is asserted via normalize_remote_model_id("RTU") rather
    # than the literal "RTU": ensure_simulation_model_schema's backfill
    # migration rewrites any legacy/pre-GUID model_type to the catalog's
    # current id the moment it's next touched (see
    # model_store.py::_backfill_legacy_model_type_ids), so the persisted
    # row genuinely holds the current catalog id, not the string this
    # test's payload happened to submit.
    current_model_type = normalize_remote_model_id("RTU")
    assert exposure_row["simulation_output_owner"] == {
        "id": created["id"],
        "name": "RTU-1 RTU",
        "provider_type": "fmu",
        "model_type": current_model_type,
        "variable": RETURN_AIR_TEMP,
    }
    # The plain output point's ownership is unaffected by the merge, and
    # correctly reports its OWN variable (not cross-contaminated with the
    # exposure's variable).
    assert output_row["simulation_output_owner"] == {
        "id": created["id"],
        "name": "RTU-1 RTU",
        "provider_type": "fmu",
        "model_type": current_model_type,
        "variable": SUPPLY_AIR_TEMP,
    }


def test_list_objects_omits_exposure_owner_when_model_disabled(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points, output_point, exposure_point = _make_rtu_device(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    client.post(
        "/simulation/models",
        json=_rtu_payload(device["id"], point_ids, weight_point_ids, output_point["id"], exposure_point["id"], enabled=False),
    )

    objects = client.get(f"/devices/{device['id']}/objects").json()
    exposure_row = next(o for o in objects if o["id"] == exposure_point["id"])
    assert exposure_row["simulation_output_owner"] is None
