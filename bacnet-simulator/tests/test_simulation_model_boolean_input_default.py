"""A "boolean"-unit FMI input (e.g. a genset start/breaker command) is
driven as a 0/1 Constant. _normalized_parameters must turn a real
True/False (catalog `default` fallback, or a legacy saved config) into
1.0/0.0 so iot-models' dict[str, float] /initialize doesn't reject it."""
from __future__ import annotations

from src.api.routers import simulation as simulation_router
from src.simulation.models.registry import ModelDefinition, VariableDefinition
from src.semantics.contract import SemanticContract


def _genset_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="DieselGenset",
        label="Diesel Genset",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(
            VariableDefinition("start_cmd", "Start Command", "input", unit="boolean",
                               default=False,
                               semantic=SemanticContract(id="start_cmd", role="command")),
            VariableDefinition("speed_ref_hz", "Frequency Setpoint", "input", unit="Hz",
                               default=50.0),
            VariableDefinition("active_power_kw", "Active Power", "output", unit="kW"),
        ),
        factory=lambda parameters: None,
        runtime_model="DieselGenset",
    )


def test_boolean_constant_default_coerced_to_float(database, monkeypatch):
    monkeypatch.setattr(
        simulation_router, "_runtime_definition", lambda _db, _mt: _genset_definition()
    )
    payload = simulation_router.SimulationModelPayload(
        name="GC Diesel Genset",
        provider_type="fmu",
        model_type="DieselGenset",
        enabled=True,
        created_from_device_id=None,
        parameters={
            "input_sources": {"start_cmd": "constant", "speed_ref_hz": "constant"},
            "input_defaults": {"start_cmd": True, "speed_ref_hz": 50.0},
        },
        mappings=[],
    )
    normalized = simulation_router._normalized_parameters(database, payload)
    assert normalized["input_defaults"]["start_cmd"] == 1.0
    assert normalized["input_defaults"]["speed_ref_hz"] == 50.0


def test_boolean_default_backfilled_from_catalog_is_float(database, monkeypatch):
    monkeypatch.setattr(
        simulation_router, "_runtime_definition", lambda _db, _mt: _genset_definition()
    )
    payload = simulation_router.SimulationModelPayload(
        name="GC Diesel Genset",
        provider_type="fmu",
        model_type="DieselGenset",
        enabled=True,
        created_from_device_id=None,
        parameters={"input_sources": {"start_cmd": "constant"}},
        mappings=[],
    )
    normalized = simulation_router._normalized_parameters(database, payload)
    # catalog default is False -> must not leak a bool into dict[str, float]
    assert normalized["input_defaults"]["start_cmd"] == 0.0
