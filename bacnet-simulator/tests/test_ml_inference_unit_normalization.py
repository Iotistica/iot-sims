"""FMUSimulationProvider, for a promoted ML model that carries an `ml_input_contract`:
each live BACnet value is normalised into the model's expected catalog unit exactly ONCE
before /step -- and the FMU-native CFM / conversion layers are skipped for that model, so
a CFM point against an m3/s contract is not divided by 2118.88 twice.

An FMU model with no ml_input_contract keeps its exact previous behaviour (incl. the
name-heuristic CFM<->m3/s special case)."""
from __future__ import annotations

import pytest

from src.simulation.providers import FMUPointBinding, FMUSimulationProvider, SimulationContext
from src.simulation.providers.fmu import FMURuntimeResponse

CFM_PER_M3_S = 2118.880003


def _binding_metadata(point_id: int, variable: str, units: str) -> dict:
    return {
        "point_id": point_id, "variable": variable, "direction": "input",
        "point_name": f"P{point_id}", "device_name": "Target-VAV", "device_id": 7,
        "object_type": "analog-input", "object_instance": 1, "units": units,
    }


def _context(bindings_metadata, initial_point_inputs) -> SimulationContext:
    return SimulationContext(
        participant_device_ids=[], point_configs=[],
        metadata={
            "provider_id": "fmu:Surrogate:1", "simulation_model_id": 1, "model": "Surrogate",
            "initial_point_inputs": initial_point_inputs, "bindings": bindings_metadata,
        },
    )


def _provider(*, bindings, ml_input_contract=None, input_vars) -> FMUSimulationProvider:
    kw = dict(
        runtime_url="http://fmu-runtime:8002", model="Surrogate", bindings=bindings,
        input_defaults={}, input_variables=set(input_vars), output_variables={"supply_air_temp_c"},
    )
    if ml_input_contract is not None:
        kw["ml_input_contract"] = ml_input_contract
    return FMUSimulationProvider(**kw)


def _run_step(monkeypatch, provider, context):
    monkeypatch.setattr(type(provider._client), "health", lambda self: {"status": "ok"})
    monkeypatch.setattr(
        type(provider._client), "initialize",
        lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"},
    )
    payloads: list[dict] = []

    def _fake_step(self, model_id, payload):
        payloads.append(dict(payload["inputs"]))
        return FMURuntimeResponse(
            status_code=200, raw_body="{}",
            body={"state": "RUNNING", "current_time": 5.0, "supply_air_temp_c": 13.0},
        )

    monkeypatch.setattr(type(provider._client), "step", _fake_step)
    provider.initialize(context)
    provider.start()
    provider.step(5.0)
    return payloads[-1]


def test_fahrenheit_point_reaches_step_payload_as_celsius_once(monkeypatch):
    binding = FMUPointBinding(point_id=1, variable="oat_c", direction="input")
    provider = _provider(
        bindings=[binding], input_vars={"oat_c"},
        ml_input_contract=[{"variable": "oat_c", "expected_unit": "degC"}],
    )
    sent = _run_step(
        monkeypatch, provider,
        _context([_binding_metadata(1, "oat_c", "degrees-fahrenheit")], {1: 55.4}),
    )
    assert sent["oat_c"] == pytest.approx(13.0)


def test_cfm_point_against_m3s_contract_is_converted_exactly_once(monkeypatch):
    # Variable name ends in _m3_s -- the FMU CFM special-case WOULD also fire if it were
    # not skipped for an ML model. Assert the value is divided by the constant once, not
    # twice (twice would be ~7.8e-8, far below any real airflow).
    binding = FMUPointBinding(point_id=2, variable="supply_airflow_m3_s", direction="input")
    provider = _provider(
        bindings=[binding], input_vars={"supply_airflow_m3_s"},
        ml_input_contract=[{"variable": "supply_airflow_m3_s", "expected_unit": "m3/s"}],
    )
    sent = _run_step(
        monkeypatch, provider,
        _context([_binding_metadata(2, "supply_airflow_m3_s", "cubic-feet-per-minute")], {2: 350.0}),
    )
    assert sent["supply_airflow_m3_s"] == pytest.approx(350.0 / CFM_PER_M3_S)


def test_incompatible_unit_raises_rather_than_sending_a_bad_number(monkeypatch):
    binding = FMUPointBinding(point_id=3, variable="oat_c", direction="input")
    provider = _provider(
        bindings=[binding], input_vars={"oat_c"},
        ml_input_contract=[{"variable": "oat_c", "expected_unit": "degC"}],
    )
    monkeypatch.setattr(type(provider._client), "health", lambda self: {"status": "ok"})
    captured: dict = {}
    monkeypatch.setattr(
        type(provider._client), "initialize",
        lambda self, model_id, inputs=None: captured.setdefault("inputs", dict(inputs or {})) or {"session_id": "s1", "state": "RUNNING"},
    )
    context = _context([_binding_metadata(3, "oat_c", "percent")], {3: 42.0})
    with pytest.raises(ValueError, match="not convertible"):
        provider.initialize(context)


def test_fmu_model_without_contract_keeps_the_legacy_cfm_special_case(monkeypatch):
    binding = FMUPointBinding(point_id=4, variable="supply_airflow_m3_s", direction="input")
    provider = _provider(bindings=[binding], input_vars={"supply_airflow_m3_s"})  # no ml_input_contract
    sent = _run_step(
        monkeypatch, provider,
        _context([_binding_metadata(4, "supply_airflow_m3_s", "cubic-feet-per-minute")], {4: 350.0}),
    )
    # Unchanged from before this feature: the name-heuristic CFM branch still fires.
    assert sent["supply_airflow_m3_s"] == pytest.approx(350.0 / CFM_PER_M3_S)
