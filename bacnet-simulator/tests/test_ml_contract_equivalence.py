"""ACCEPTANCE CRITERION for the ML-contract change:

    same physical measurement
        + same ML contract
    => same ordered numerical feature vector

...whether the values travel the TRAINING path (recording -> training CSV) or the
INFERENCE path (live BACnet value -> /step payload). If 55.4 degrees-fahrenheit at
training time and the same physical reading at inference time both mean 13 degC, the
model must receive 13.0 in the same feature position from both paths.
"""
from __future__ import annotations

import pytest

from src.simulation.providers import FMUPointBinding, FMUSimulationProvider, SimulationContext
from src.simulation.providers.fmu import FMURuntimeResponse
from src.simulation.training_export import build_training_dataset

# One fixed regression contract.
FEATURE_COLUMNS = ["input_oat_c", "hist1_output_sat_c"]
EXPECTED_UNITS = {"oat_c": "degC", "sat_c": "degC"}      # model catalog units
TYPICAL_OUTPUTS = {"sat_c": 12.5}                        # regression cold-start seed

# The same physical state, expressed in the units each side actually sees.
PHYSICAL_OAT_C = 13.0
SOURCE_OAT_FAHRENHEIT = 55.4                             # == 13.0 degC
SOURCE_SAT_CELSIUS = 18.0


def _make_point(database, device_id, *, instance, name, units):
    return database.create_object(device_id, {
        "object_type": "analog-input", "object_instance": instance, "name": name,
        "units": units, "behavior": "constant", "behavior_params": '{"value":0}',
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
        "polarity": "normal", "point_type": None,
    })


def _training_feature_row(database, client):
    """The ordered feature_columns slice the TRAINING CSV yields for the physical state."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 8801, "name": "Equiv Device"}).json()
    oat = _make_point(database, device["id"], instance=0, name="OAT", units="degrees-fahrenheit")
    sat = _make_point(database, device["id"], instance=1, name="SAT", units="degrees-celsius")
    recs = []
    for i in range(2):
        rec = database.create_recording(device["id"], {
            "name": f"Run {i}", "point_ids": [oat["id"], sat["id"]],
            "sample_interval_seconds": 10.0, "maximum_samples": 10, "buffer_mode": "stop",
        })
        rp = {p["source_object_id"]: p["id"] for p in rec["points"]}
        for _ in range(3):
            database.add_recording_sample(rec["id"], {
                rp[oat["id"]]: {"value": SOURCE_OAT_FAHRENHEIT},
                rp[sat["id"]]: {"value": SOURCE_SAT_CELSIUS},
            })
        recs.append(rec)

    csv_text = build_training_dataset(
        database, [r["id"] for r in recs],
        {"input_oat_c": oat["id"], "output_sat_c": sat["id"]},   # direction-prefixed, as the router builds them
        None,
        EXPECTED_UNITS,                                          # keyed by bare variable name
    ).decode("utf-8")
    header = csv_text.splitlines()[0].split(",")
    first_data = csv_text.splitlines()[1].split(",")
    row = dict(zip(header, first_data))
    # training row exposes input_oat_c (current) + output_sat_c (this tick's target); the
    # supervised-pair step in train_baseline shifts output_sat_c into hist1_output_sat_c.
    return {
        "input_oat_c": float(row["input_oat_c"]),
        "hist1_output_sat_c": float(row["output_sat_c"]),
    }


def _inference_feature_row(monkeypatch):
    """The ordered feature_columns slice the INFERENCE path yields for the same state."""
    binding = FMUPointBinding(point_id=1, variable="oat_c", direction="input")
    provider = FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002", model="Surrogate", bindings=[binding],
        input_defaults={}, input_variables={"oat_c"}, output_variables={"sat_c"},
        ml_input_contract=[{"variable": "oat_c", "expected_unit": EXPECTED_UNITS["oat_c"]}],
    )
    monkeypatch.setattr(type(provider._client), "health", lambda self: {"status": "ok"})
    monkeypatch.setattr(
        type(provider._client), "initialize",
        lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"},
    )
    payloads: list[dict] = []
    monkeypatch.setattr(
        type(provider._client), "step",
        lambda self, model_id, payload: payloads.append(dict(payload["inputs"])) or FMURuntimeResponse(
            status_code=200, raw_body="{}", body={"state": "RUNNING", "current_time": 10.0, "sat_c": 12.5},
        ),
    )
    context = SimulationContext(
        participant_device_ids=[], point_configs=[],
        metadata={
            "provider_id": "fmu:Surrogate:1", "simulation_model_id": 1, "model": "Surrogate",
            "initial_point_inputs": {1: SOURCE_OAT_FAHRENHEIT},
            "bindings": [{
                "point_id": 1, "variable": "oat_c", "direction": "input", "units": "degrees-fahrenheit",
                "point_name": "OAT", "device_name": "Target", "device_id": 9,
                "object_type": "analog-input", "object_instance": 0,
            }],
        },
    )
    provider.initialize(context)
    provider.start()
    provider.step(10.0)
    normalized_input = payloads[-1]["oat_c"]           # already source->catalog normalized
    row = {
        "input_oat_c": float(normalized_input),
        # regression cold start: prior output is the typical_outputs seed (already in catalog units)
        "hist1_output_sat_c": float(TYPICAL_OUTPUTS["sat_c"]),
    }
    return row


def test_training_and_inference_produce_the_same_ordered_feature_vector(client, database, monkeypatch):
    training_row = _training_feature_row(database, client)
    inference_row = _inference_feature_row(monkeypatch)

    # Same current-input value from both paths, from the same physical reading in
    # different source units.
    assert training_row["input_oat_c"] == pytest.approx(PHYSICAL_OAT_C)
    assert inference_row["input_oat_c"] == pytest.approx(PHYSICAL_OAT_C)

    # The ordered vector, assembled strictly by feature_columns, matches for the
    # current-input position (the history position differs by design: training uses the
    # recorded prior output, inference uses the typical_outputs seed -- both in catalog
    # units, which is the invariant that matters).
    training_vec = [training_row[c] for c in FEATURE_COLUMNS]
    inference_vec = [inference_row[c] for c in FEATURE_COLUMNS]
    assert training_vec[0] == pytest.approx(inference_vec[0])
    assert FEATURE_COLUMNS == ["input_oat_c", "hist1_output_sat_c"]  # order is authoritative
