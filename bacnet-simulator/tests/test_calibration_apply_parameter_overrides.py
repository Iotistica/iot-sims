"""Applied calibration results (CalibrationFlow.vue's "Apply to Model"
button) -- FMI causality="parameter" Real overrides, stored under
parameters.parameter_overrides and forwarded to the FMU runtime as
real_parameters. Mirrors the existing `parameters` precedent at
each layer (see manager.py's own _apply_parameters/
_apply_real_parameters on the iot-models side); this file covers the
bacnet-simulator side: shape validation, _build_fmu_provider forwarding,
and FMUSimulationProvider's own initialize() call.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.routers import simulation as simulation_router
from src.simulation.models import runtime as model_runtime
from src.simulation.models.registry import ModelDefinition, VariableDefinition
from src.simulation.providers import FMUSimulationProvider, SimulationContext


# ─── 1. _validate_parameters: shape check ───────────────────────────────────

def _fake_fmu_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="RTU",
        label="RTU",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(VariableDefinition("cooling_command_pct", "Cooling", "output"),),
        factory=lambda parameters: None,
        runtime_model="RTU",
    )


def test_validate_parameters_rejects_non_dict_parameter_overrides(monkeypatch):
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _fake_fmu_definition())
    payload = simulation_router.SimulationModelPayload(
        name="RTU-1", provider_type="fmu", model_type="RTU",
        parameters={"parameter_overrides": "not-an-object"},
    )
    with pytest.raises(HTTPException) as exc_info:
        simulation_router._validate_parameters(None, payload)
    assert exc_info.value.status_code == 422
    assert "parameter_overrides" in str(exc_info.value.detail)


def test_validate_parameters_accepts_valid_parameter_overrides(monkeypatch):
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _fake_fmu_definition())
    payload = simulation_router.SimulationModelPayload(
        name="RTU-1", provider_type="fmu", model_type="RTU",
        parameters={"parameter_overrides": {"COP_nominal": 4.2}},
    )
    simulation_router._validate_parameters(None, payload)  # must not raise


# ─── 2. _build_fmu_provider: parameter_overrides -> real_parameters ─────────

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
        parameters=None,
        real_parameters=None,
        warmup_seconds=None,
        timeout_s,
        input_variables,
        output_variables,
        api_key=None,
    ) -> None:
        self.real_parameters = dict(real_parameters or {})
        self.__class__.created.append(self)


class _FakeEngine:
    def resolve_provider_input_value(self, point_id: int):
        return None

    def register_simulation_provider(self, *args, **kwargs) -> None:
        pass


def _base_config(parameters: dict) -> dict:
    return {
        "id": 8,
        "provider_type": "fmu",
        "model_type": "RTU",
        "name": "RTU-1",
        "parameters": parameters,
        "mappings": [],
        "_settings": {},
    }


def test_build_fmu_provider_forwards_parameter_overrides_as_real_parameters(monkeypatch):
    definition = _fake_fmu_definition()
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _s, _m: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    _FakeFMUProvider.created.clear()

    config = _base_config({"parameter_overrides": {"QCoo_flow_nominal": -9500.0, "COP_nominal": 4.2}})
    model_runtime._build_fmu_provider(config, _FakeEngine())

    assert _FakeFMUProvider.created[-1].real_parameters == {
        "QCoo_flow_nominal": -9500.0, "COP_nominal": 4.2,
    }


def test_build_fmu_provider_omits_real_parameters_when_none_applied(monkeypatch):
    definition = _fake_fmu_definition()
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _s, _m: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    _FakeFMUProvider.created.clear()

    config = _base_config({})
    model_runtime._build_fmu_provider(config, _FakeEngine())

    assert _FakeFMUProvider.created[-1].real_parameters == {}


# ─── 3. FMUSimulationProvider.initialize(): real_parameters -> client ───────

def _provider(*, real_parameters: dict | None = None) -> FMUSimulationProvider:
    return FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="RTU",
        bindings=[],
        input_defaults={},
        real_parameters=real_parameters,
        input_variables=set(),
        output_variables={"cooling_command_pct"},
    )


def _context() -> SimulationContext:
    return SimulationContext(
        participant_device_ids=[],
        point_configs=[],
        metadata={
            "provider_id": "fmu:RTU:1",
            "simulation_model_id": 1,
            "model": "RTU",
            "initial_point_inputs": {},
            "bindings": [],
        },
    )


def _fake_health(self):
    return {"status": "ok"}


def test_real_parameters_forwarded_to_client_initialize(monkeypatch):
    provider = _provider(real_parameters={"COP_nominal": 4.2})
    monkeypatch.setattr(type(provider._client), "health", _fake_health)

    captured: dict = {}

    def _capturing_initialize(self, model_id, inputs=None, **kwargs):
        captured.update(kwargs)
        return {"session_id": "s1", "state": "RUNNING"}

    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize)

    provider.initialize(_context())

    assert captured["real_parameters"] == {"COP_nominal": 4.2}


def test_no_real_parameters_kwarg_sent_when_none_configured(monkeypatch):
    """Matches the `parameters` precedent (see _initialize_runtime_session's
    own comment): the kwarg is only sent when actually set, so a test double
    monkeypatching the older (self, model_id, inputs=None) signature -- with
    no **kwargs at all -- keeps working unchanged for the overwhelming
    majority of models that use neither `parameters` nor
    real_parameters."""
    provider = _provider(real_parameters=None)
    monkeypatch.setattr(type(provider._client), "health", _fake_health)

    def _strict_initialize(self, model_id, inputs=None):
        return {"session_id": "s1", "state": "RUNNING"}

    monkeypatch.setattr(type(provider._client), "initialize", _strict_initialize)

    provider.initialize(_context())  # must not raise TypeError
