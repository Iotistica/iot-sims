"""A model.json "boolean"-unit FMI input (e.g. the genset breakerCmd /
startCmd Boolean variables) is driven as a 0/1 Constant. The value that
reaches the model runtime's /initialize + /step payloads must be a plain
float 0.0/1.0 -- iot-models' InitializeRequest/StepRequest.inputs are
dict[str, float], and the runtime itself casts 1.0 -> True before
fmi2SetBoolean (shared/runtime/models/manager.py::_apply_discrete_inputs).
A real Python bool must never leak into the payload."""
from __future__ import annotations

from src.simulation.providers.fmu import FMUPointBinding, FMUSimulationProvider


def _provider(input_defaults):
    return FMUSimulationProvider(
        runtime_url="http://fake-runtime.invalid",
        model="DieselGenset",
        bindings=[FMUPointBinding(point_id=1, variable="active_power_kw", direction="output")],
        input_defaults=input_defaults,
    )


def test_bool_input_defaults_normalised_to_float_in_constructor():
    provider = _provider({"start_cmd": True, "breaker_cmd": False, "speed_ref_hz": 50.0})
    assert provider._input_defaults == {
        "start_cmd": 1.0, "breaker_cmd": 0.0, "speed_ref_hz": 50.0,
    }
    for value in provider._input_defaults.values():
        assert not isinstance(value, bool)


def test_bool_input_reaches_initialize_payload_as_float():
    provider = _provider({"start_cmd": True, "breaker_cmd": False})
    init_inputs, unresolved = provider._resolve_init_inputs()
    assert unresolved == []
    assert init_inputs["start_cmd"] == 1.0
    assert init_inputs["breaker_cmd"] == 0.0
    assert all(not isinstance(v, bool) for v in init_inputs.values())


def test_bool_input_reaches_step_payload_as_float():
    provider = _provider({"start_cmd": 1, "breaker_cmd": 0})
    provider._session_id = "sess-1"
    payload, _report = provider._build_step_payload(dt=1.0)
    assert payload["inputs"]["start_cmd"] == 1.0
    assert payload["inputs"]["breaker_cmd"] == 0.0
    assert all(not isinstance(v, bool) for v in payload["inputs"].values())
