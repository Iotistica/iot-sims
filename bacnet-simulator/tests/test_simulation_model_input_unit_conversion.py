"""FMUSimulationProvider._convert_input_value: a Point-mapped INPUT whose
bound BACnet point is in CFM (e.g. a VAV's own Zone-Airflow output) must be
converted to m3/s before being sent to a downstream model's
supply_airflow_m3_s input -- the FMU always speaks the model's declared
unit, never the BACnet point's own unit.

Found via a live production bug: RTU / VAV / ThermalZone chain (see
test_simulation_model_input_exposure.py for the sibling weighted_average /
input-exposure feature) -- a VAV's Zone-Airflow point (~300-500
cubic-feet-per-minute) was being sent straight through as if it were
already m3/s, a ~2119x overshoot that flooded the receiving zone/AHU
model's heat balance and drove its temperature to a physically-wrong
near-zero-Celsius steady state. _convert_output_value (the mirror-image
conversion for FMU OUTPUTS written to a CFM-unit BACnet point,
e.g. RTU's own supply_airflow_m3_s output) already existed; this is its
missing INPUT-direction counterpart.
"""
from __future__ import annotations

import pytest

from src.simulation.providers import FMUPointBinding, FMUSimulationProvider, SimulationContext
from src.simulation.providers.fmu import FMURuntimeResponse


CFM_PER_M3_S = 2118.880003


def _binding_metadata(point_id: int, name: str, variable: str, units: str) -> dict:
    return {
        "point_id": point_id,
        "variable": variable,
        "direction": "input",
        "point_name": name,
        "device_name": "VAV-1 Zone 1",
        "device_id": 1,
        "object_type": "analog-input",
        "object_instance": 1,
        "units": units,
    }


def _context(bindings_metadata: list[dict], initial_point_inputs: dict) -> SimulationContext:
    return SimulationContext(
        participant_device_ids=[],
        point_configs=[],
        metadata={
            "provider_id": "fmu:ThermalZone:1",
            "simulation_model_id": 1,
            "model": "ThermalZone",
            "initial_point_inputs": initial_point_inputs,
            "bindings": bindings_metadata,
        },
    )


def _provider(*, bindings) -> FMUSimulationProvider:
    return FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="ThermalZone",
        bindings=bindings,
        input_defaults={},
        input_variables={"supply_airflow_m3_s", "discharge_air_temp_c"},
        output_variables={"zone_temp_c"},
    )


def _fake_health(self):
    return {"status": "ok"}


def test_cfm_point_input_converted_to_m3_s_at_initialize(monkeypatch):
    binding = FMUPointBinding(point_id=101, variable="supply_airflow_m3_s", direction="input")
    provider = _provider(bindings=[binding])
    monkeypatch.setattr(type(provider._client), "health", _fake_health)

    captured: dict = {}

    def _capturing_initialize(self, model_id, inputs=None):
        captured["inputs"] = dict(inputs or {})
        return {"session_id": "s1", "state": "RUNNING"}

    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize)

    # A VAV zone airflow reading of 350 CFM -- the exact scale the live bug
    # was found at.
    context = _context(
        [_binding_metadata(101, "VAV-L1-01.Zone-Airflow", "supply_airflow_m3_s", "cubic-feet-per-minute")],
        {101: 350.0},
    )
    provider.initialize(context)

    sent = captured["inputs"]["supply_airflow_m3_s"]
    assert sent == pytest.approx(350.0 / CFM_PER_M3_S)
    # Not the raw CFM number -- the whole point of the fix.
    assert sent != pytest.approx(350.0)
    # Physically sane VAV-zone-scale airflow, not ~350 m3/s (728,000 CFM).
    assert sent < 1.0


def test_cfm_point_input_converted_to_m3_s_at_step(monkeypatch):
    binding = FMUPointBinding(point_id=101, variable="supply_airflow_m3_s", direction="input")
    output_binding = FMUPointBinding(point_id=999, variable="zone_temp_c", direction="output")
    provider = _provider(bindings=[binding, output_binding])
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

    context = _context(
        [_binding_metadata(101, "VAV-L1-01.Zone-Airflow", "supply_airflow_m3_s", "cubic-feet-per-minute")],
        {101: 350.0},
    )
    provider.initialize(context)
    provider.start()
    provider.step(5.0)

    sent = captured_payloads[-1]["supply_airflow_m3_s"]
    assert sent == pytest.approx(350.0 / CFM_PER_M3_S)
    assert sent < 1.0

    # Live values can change tick to tick -- the conversion must track,
    # not just apply once at initialize.
    provider.set_inputs({101: 500.0})
    provider.step(5.0)
    assert captured_payloads[-1]["supply_airflow_m3_s"] == pytest.approx(500.0 / CFM_PER_M3_S)


def test_non_cfm_units_are_not_converted(monkeypatch):
    """A point already in m3/s (no unit mismatch) must pass through
    unchanged -- the conversion is gated on the bound point's OWN units,
    not applied unconditionally to every supply_airflow_m3_s input."""
    binding = FMUPointBinding(point_id=101, variable="supply_airflow_m3_s", direction="input")
    provider = _provider(bindings=[binding])
    monkeypatch.setattr(type(provider._client), "health", _fake_health)

    captured: dict = {}
    monkeypatch.setattr(
        type(provider._client), "initialize",
        lambda self, model_id, inputs=None: (captured.__setitem__("inputs", dict(inputs or {})), {"session_id": "s1", "state": "RUNNING"})[1],
    )

    context = _context(
        [_binding_metadata(101, "AHU-1.SA-Flow-m3s", "supply_airflow_m3_s", "m3/s")],
        {101: 0.165},
    )
    provider.initialize(context)

    assert captured["inputs"]["supply_airflow_m3_s"] == pytest.approx(0.165)


def test_other_variables_are_never_converted(monkeypatch):
    """The conversion is scoped to exactly supply_airflow_m3_s -- any other
    variable name, even one bound to a CFM-labeled point (a hypothetical
    mis-mapping), passes through unchanged."""
    binding = FMUPointBinding(point_id=101, variable="discharge_air_temp_c", direction="input")
    provider = _provider(bindings=[binding])
    monkeypatch.setattr(type(provider._client), "health", _fake_health)

    captured: dict = {}
    monkeypatch.setattr(
        type(provider._client), "initialize",
        lambda self, model_id, inputs=None: (captured.__setitem__("inputs", dict(inputs or {})), {"session_id": "s1", "state": "RUNNING"})[1],
    )

    context = _context(
        [_binding_metadata(101, "Weirdly-Labeled-Point", "discharge_air_temp_c", "cubic-feet-per-minute")],
        {101: 13.0},
    )
    provider.initialize(context)

    assert captured["inputs"]["discharge_air_temp_c"] == pytest.approx(13.0)
