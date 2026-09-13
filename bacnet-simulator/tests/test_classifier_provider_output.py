"""End-to-end (provider-level, no real FMU runtime) coverage of a classifier-backed model's
/step() response flowing through FMUSimulationProvider -- the 0-based prediction_index -> 1-based
BACnet multi-state present-value conversion (via the existing zero_based_to_multistate mapping
conversion, see mapping/conversions.py), and that the full richer result (prediction_label/
confidence/probabilities) is preserved in step diagnostics rather than discarded once unwrapped
to a plain int for the BACnet write path. Mirrors test_simulation_model_input_unit_conversion.py's
own provider-level driving pattern (fake FMURuntimeClient.step, no real HTTP)."""
from __future__ import annotations

from src.simulation.providers import FMUPointBinding, FMUSimulationProvider, SimulationContext
from src.simulation.providers.fmu import FMURuntimeResponse


def _context() -> SimulationContext:
    return SimulationContext(
        participant_device_ids=[],
        point_configs=[],
        metadata={
            "provider_id": "fmu:condition-classifier:1",
            "simulation_model_id": 1,
            "model": "condition-classifier",
            "initial_point_inputs": {},
            "bindings": [],
        },
    )


def _provider() -> FMUSimulationProvider:
    binding = FMUPointBinding(
        point_id=501, variable="condition", direction="output",
        conversion="zero_based_to_multistate",
    )
    return FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="condition-classifier",
        bindings=[binding],
        input_defaults={},
        input_variables=set(),
        output_variables={"condition"},
    )


def _fake_health(self):
    return {"status": "ok"}


def _classifier_step_response(prediction_index: int, prediction_label: str) -> FMURuntimeResponse:
    return FMURuntimeResponse(
        status_code=200, raw_body="{}",
        body={
            "state": "RUNNING",
            "current_time": 60.0,
            "condition": {
                "prediction_index": prediction_index,
                "prediction_label": prediction_label,
                "confidence": 0.87,
                "probabilities": {"normal": 0.03, "sensor_bias": 0.10, prediction_label: 0.87},
            },
        },
    )


def test_classifier_prediction_index_reaches_bacnet_point_as_1_based_state(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(
        type(provider._client), "initialize",
        lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"},
    )
    monkeypatch.setattr(
        type(provider._client), "step",
        lambda self, model_id, payload: _classifier_step_response(2, "sensor_stuck"),
    )

    provider.initialize(_context())
    provider.start()
    provider.step(60.0)

    outputs = provider.get_outputs()
    # ML index 2 -> BACnet MultiStateValue state 3 (1-based), via the same
    # zero_based_to_multistate conversion an FMU compressor-stage output uses.
    assert outputs[501] == 3


def test_classifier_index_0_maps_to_bacnet_state_1(monkeypatch):
    """Explicit boundary case -- index 0 (never accidentally treated as
    falsy/"no value")."""
    provider = _provider()
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(
        type(provider._client), "initialize",
        lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"},
    )
    monkeypatch.setattr(
        type(provider._client), "step",
        lambda self, model_id, payload: _classifier_step_response(0, "normal"),
    )

    provider.initialize(_context())
    provider.start()
    provider.step(60.0)

    assert provider.get_outputs()[501] == 1


def test_classifier_rich_result_preserved_in_diagnostics(monkeypatch):
    """The full prediction_label/confidence/probabilities breakdown must
    survive in diagnostics even though only the bare 1-based state reaches
    the BACnet point -- retained for future FDD UI/analytics, not
    discarded at the unwrap point."""
    provider = _provider()
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(
        type(provider._client), "initialize",
        lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"},
    )
    monkeypatch.setattr(
        type(provider._client), "step",
        lambda self, model_id, payload: _classifier_step_response(1, "sensor_bias"),
    )

    provider.initialize(_context())
    provider.start()
    provider.step(60.0)

    diagnostics = provider.get_diagnostics()
    report = diagnostics["last_step_outputs"]["condition"]
    assert report["value"] == 2  # the 1-based BACnet state actually written
    result = report["classifier_result"]
    assert result["prediction_index"] == 1
    assert result["prediction_label"] == "sensor_bias"
    assert result["confidence"] == 0.87
    assert result["probabilities"]["sensor_bias"] == 0.87
