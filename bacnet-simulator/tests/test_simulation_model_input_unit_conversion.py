"""FMUSimulationProvider._convert_input_value / _convert_output_value: any
"*_m3_s" volumetric-airflow variable whose bound BACnet point is in CFM
must be converted before crossing the FMU boundary in either direction --
the FMU always speaks the model's declared unit (m3/s), never the BACnet
point's own unit.

Found via two live production bugs, same root cause, same fix:

1. INPUT direction: RTU / VAV / ThermalZone chain (see
   test_simulation_model_input_exposure.py for the sibling weighted_average
   / input-exposure feature) -- a VAV's Zone-Airflow point (~300-500
   cubic-feet-per-minute) was being sent straight through as if it were
   already m3/s, a ~2119x overshoot that flooded the receiving zone/AHU
   model's heat balance and drove its temperature to a physically-wrong
   near-zero-Celsius steady state.

2. OUTPUT direction: RTU's own outdoor_airflow_m3_s output (added after
   supply_airflow_m3_s's conversion already existed) was displayed
   unconverted under a CFM point label -- ~1.07 shown instead of ~2267.
   Both _convert_output_value and _convert_input_value were hardcoded to
   the single literal variable name "supply_airflow_m3_s", so neither one
   recognized outdoor_airflow_m3_s at all. Fixed by matching the "*_m3_s"
   naming convention every volumetric-flow model.json variable already
   follows, instead of one hardcoded name -- covers both variables (and
   any future one) with no per-variable edit needed. The actual conversion
   still only fires when the bound point's own declared units are
   CFM-recognized, so this can't affect any point already correctly
   labeled m3/s, or any non-flow variable (temperature, percent, power,
   pressure) -- see test_other_variables_are_never_converted below.
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


# ─── OUTPUT direction: FMU m3/s -> a CFM-labeled BACnet point ──────────────
# Mirrors the INPUT-direction tests above, but for _convert_output_value --
# the direction the live RTU-1-Outdoor-Airflow bug was actually found in.

def _output_provider(*, output_variable: str, point_id: int = 999) -> FMUSimulationProvider:
    binding = FMUPointBinding(point_id=point_id, variable=output_variable, direction="output")
    return FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="RTU",
        bindings=[binding],
        input_defaults={},
        input_variables=set(),
        output_variables={output_variable},
    )


def _step_context(point_id: int, point_name: str, variable: str, units: str) -> SimulationContext:
    return _context(
        [
            {
                "point_id": point_id,
                "variable": variable,
                "direction": "output",
                "point_name": point_name,
                "device_name": "RTU",
                "device_id": 1,
                "object_type": "analog-input",
                "object_instance": 1,
                "units": units,
            },
        ],
        {},
    )


def _run_one_step(provider: FMUSimulationProvider, context: SimulationContext, raw_output_value: float, variable: str) -> dict:
    provider._client.health = lambda: {"status": "ok"}
    provider._client.initialize = lambda model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"}
    provider._client.step = lambda model_id, payload: FMURuntimeResponse(
        status_code=200, raw_body="{}",
        body={"state": "RUNNING", "current_time": 5.0, variable: raw_output_value},
    )

    provider.initialize(context)
    provider.start()
    provider.step(5.0)
    return dict(provider.get_outputs())


def test_supply_airflow_output_converted_to_cfm():
    """The variable the conversion originally covered -- still works after
    generalizing the name check."""
    context = _step_context(999, "RTU-1-Supply-Airflow", "supply_airflow_m3_s", "cubic-feet-per-minute")
    provider = _output_provider(output_variable="supply_airflow_m3_s", point_id=999)

    outputs = _run_one_step(provider, context, 1.07, "supply_airflow_m3_s")

    assert outputs[999] == pytest.approx(1.07 * CFM_PER_M3_S)


def test_outdoor_airflow_output_converted_to_cfm():
    """The actual reported bug: RTU-1-Outdoor-Airflow displayed a raw
    ~1.07 m3/s value mislabeled as CFM (should read ~2267 cfm).
    outdoor_airflow_m3_s never matched the old hardcoded
    "supply_airflow_m3_s"-only check."""
    context = _step_context(999, "RTU-1-Outdoor-Airflow", "outdoor_airflow_m3_s", "cubic-feet-per-minute")
    provider = _output_provider(output_variable="outdoor_airflow_m3_s", point_id=999)

    outputs = _run_one_step(provider, context, 1.07, "outdoor_airflow_m3_s")

    assert outputs[999] == pytest.approx(1.07 * CFM_PER_M3_S)
    assert outputs[999] == pytest.approx(2267.2, abs=1.0)


def test_outdoor_airflow_output_not_converted_when_point_is_already_m3_s():
    """A point genuinely labeled m3/s must pass through unchanged --
    conversion is gated on the bound point's own units, not the variable
    name alone."""
    context = _step_context(999, "RTU-1-Outdoor-Airflow-m3s", "outdoor_airflow_m3_s", "m3/s")
    provider = _output_provider(output_variable="outdoor_airflow_m3_s", point_id=999)

    outputs = _run_one_step(provider, context, 1.07, "outdoor_airflow_m3_s")

    assert outputs[999] == pytest.approx(1.07)


def test_outdoor_airflow_input_converted_from_cfm(monkeypatch):
    """Symmetry check on the INPUT-direction path too -- confirms the same
    "*_m3_s" naming-convention generalization applies to
    _convert_input_value, not just _convert_output_value."""
    binding = FMUPointBinding(point_id=101, variable="outdoor_airflow_m3_s", direction="input")
    provider = FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="SomeDownstreamModel",
        bindings=[binding],
        input_defaults={},
        input_variables={"outdoor_airflow_m3_s"},
        output_variables=set(),
    )
    monkeypatch.setattr(type(provider._client), "health", _fake_health)

    captured: dict = {}
    monkeypatch.setattr(
        type(provider._client), "initialize",
        lambda self, model_id, inputs=None: (captured.__setitem__("inputs", dict(inputs or {})), {"session_id": "s1", "state": "RUNNING"})[1],
    )

    context = _context(
        [_binding_metadata(101, "RTU-1-Outdoor-Airflow", "outdoor_airflow_m3_s", "cubic-feet-per-minute")],
        {101: 2267.2},
    )
    provider.initialize(context)

    assert captured["inputs"]["outdoor_airflow_m3_s"] == pytest.approx(2267.2 / CFM_PER_M3_S)
    assert captured["inputs"]["outdoor_airflow_m3_s"] == pytest.approx(1.07, abs=0.01)
