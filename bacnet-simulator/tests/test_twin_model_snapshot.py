from src.simulation.engine import SimEngine


def _object_row(object_id: int, name: str, instance: int) -> dict:
    return {
        "id": object_id,
        "name": name,
        "object_type": "analog-value",
        "object_instance": instance,
        "units": "degrees-celsius",
        "behavior": "constant",
    }


def test_twin_snapshot_includes_fmu_input_shadow_model_values() -> None:
    engine = SimEngine.__new__(SimEngine)
    engine._point_output_owner = {
        6001: "fmu:SimpleVAVZone:1016",
    }
    engine._provider_diagnostics = {
        "fmu:SimpleVAVZone:1016": {"runtime_state": "RUNNING"},
    }
    engine._model_input_shadow_values = {
        6002: (23.0, "RUNNING"),
        6003: (20.0, "RUNNING"),
    }

    provider_outputs = {
        6001: 22.13,
    }
    snapshot_objects = [
        engine._twin_snapshot_payload(
            6001,
            _object_row(6001, "Zone-Temp", 1),
            21.73,
            provider_outputs,
        ),
        engine._twin_snapshot_payload(
            6002,
            _object_row(6002, "Cooling-SP", 2),
            23.0,
            provider_outputs,
        ),
        engine._twin_snapshot_payload(
            6003,
            _object_row(6003, "Heating-SP", 3),
            20.0,
            provider_outputs,
        ),
    ]

    snapshot = {
        payload["id"]: payload
        for payload in snapshot_objects
        if payload is not None
    }

    assert snapshot[6001]["model_value"] == 22.13
    assert snapshot[6002]["model_value"] == 23.0
    assert snapshot[6003]["model_value"] == 20.0
    assert snapshot[6001]["model_state"] == "RUNNING"
    assert snapshot[6002]["model_state"] == "RUNNING"
    assert snapshot[6003]["model_state"] == "RUNNING"
