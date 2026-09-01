"""API-level coverage for SimulationModelMappingPayload.conversion: a
mapping can declare a named value conversion (see
mapping/conversions.CONVERSIONS) applied to its FMU output at the mapping
boundary. Exercises the real create/persist/read round-trip (POST
/simulation/models -> DB -> GET /simulation/models/{id}), following this
codebase's established _make_provider_owned_point pattern from
test_fmu_behavior.py, with a multi-state-input point instead of an
analog one.
"""
from __future__ import annotations

from src.api.routers import simulation as simulation_router
from src.simulation.models import runtime as model_runtime
from src.simulation.models.registry import ModelDefinition, VariableDefinition

OUTPUT_VARIABLE = "compressorStage"


class _FakeFMUProvider:
    def __init__(self, *, runtime_url, model, bindings, aggregate_inputs=None,
                 input_exposures=None, input_defaults=None, timeout_s=20.0,
                 input_variables=None, output_variables=None, api_key=None) -> None:
        self.runtime_url = runtime_url
        self.model = model
        self.bindings = list(bindings)


def _fake_rtu_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="RTU", label="RTU", provider_type="fmu", description="", parameters=(),
        variables=(VariableDefinition(OUTPUT_VARIABLE, "Compressor Stage", "output"),),
        factory=lambda parameters: None, runtime_model="RTU",
    )


def _patch_definition(monkeypatch) -> ModelDefinition:
    definition = _fake_rtu_definition()
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _model_type: definition)
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _settings, _model_type: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    return definition


def _make_device_and_multistate_point(client, *, instance: int):
    dev_resp = client.post("/devices", json={"device_instance": instance, "name": "RTU-Test"})
    assert dev_resp.status_code in (200, 201), dev_resp.text
    device = dev_resp.json()
    resp = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "multi-state-input", "object_instance": 1,
        "name": "Compressor-Stage", "units": "no-units", "number_of_states": 3,
    })
    assert resp.status_code in (200, 201), resp.text
    return device, resp.json()


def test_mapping_persists_and_returns_conversion(client, database, monkeypatch):
    monkeypatch.setattr(simulation_router, "get_engine", lambda _request: None)
    _patch_definition(monkeypatch)
    device, point = _make_device_and_multistate_point(client, instance=6301)

    resp = client.post("/simulation/models", json={
        "name": "RTU-Test RTU", "provider_type": "fmu", "model_type": "RTU",
        "enabled": False, "created_from_device_id": device["id"], "parameters": {},
        "mappings": [{
            "variable": OUTPUT_VARIABLE, "direction": "output", "point_id": point["id"],
            "conversion": "zero_based_to_multistate",
        }],
        "aggregate_mappings": [],
    })
    assert resp.status_code == 201, resp.text
    model_id = resp.json()["id"]

    fetched = client.get(f"/simulation/models/{model_id}").json()
    mapping = next(m for m in fetched["mappings"] if m["variable"] == OUTPUT_VARIABLE)
    assert mapping["conversion"] == "zero_based_to_multistate"


def test_mapping_without_conversion_still_works(client, database, monkeypatch):
    """Backward compatibility: an ordinary mapping that never declares
    `conversion` (the overwhelming majority of existing/future mappings)
    must be accepted exactly as before, with conversion coming back as
    None/absent rather than being required."""
    monkeypatch.setattr(simulation_router, "get_engine", lambda _request: None)
    _patch_definition(monkeypatch)
    device, point = _make_device_and_multistate_point(client, instance=6302)

    resp = client.post("/simulation/models", json={
        "name": "RTU-Test RTU", "provider_type": "fmu", "model_type": "RTU",
        "enabled": False, "created_from_device_id": device["id"], "parameters": {},
        "mappings": [{
            "variable": OUTPUT_VARIABLE, "direction": "output", "point_id": point["id"],
        }],
        "aggregate_mappings": [],
    })
    assert resp.status_code == 201, resp.text
    model_id = resp.json()["id"]

    fetched = client.get(f"/simulation/models/{model_id}").json()
    mapping = next(m for m in fetched["mappings"] if m["variable"] == OUTPUT_VARIABLE)
    assert mapping["conversion"] is None


def test_unknown_conversion_name_is_rejected(client, database, monkeypatch):
    monkeypatch.setattr(simulation_router, "get_engine", lambda _request: None)
    _patch_definition(monkeypatch)
    device, point = _make_device_and_multistate_point(client, instance=6303)

    resp = client.post("/simulation/models", json={
        "name": "RTU-Test RTU", "provider_type": "fmu", "model_type": "RTU",
        "enabled": False, "created_from_device_id": device["id"], "parameters": {},
        "mappings": [{
            "variable": OUTPUT_VARIABLE, "direction": "output", "point_id": point["id"],
            "conversion": "not_a_real_conversion",
        }],
        "aggregate_mappings": [],
    })
    assert resp.status_code == 422, resp.text
