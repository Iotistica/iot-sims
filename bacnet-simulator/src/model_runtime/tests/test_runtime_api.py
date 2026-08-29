from fastapi.testclient import TestClient

from shared.runtime.models import diagnostics
from shared.runtime.models import manager as runtime_manager
from shared.runtime.app import app


def _model_id(client: TestClient, slug: str) -> str:
    """Resolves a model's current catalog GUID from its stable, human-
    readable slug (e.g. "SimpleVAVZone") via the live /models list --
    robust to the GUIDs being regenerated, unlike pinning a literal GUID
    constant in this file."""
    models = client.get("/models").json()["models"]
    return next(item["id"] for item in models if item["slug"] == slug)


def _catalog_model_by_slug(catalog: "runtime_manager.ModelCatalog", slug: str):
    """Same resolution as _model_id, for tests that talk to a ModelCatalog/
    FMUSessionManager directly instead of through the HTTP client."""
    return next(model for model in catalog._models.values() if model.slug == slug)


class FakeFMU:
    def simulate_options(self):
        return {}

    def simulate(self, start_time, final_time, input, options):
        names, trajectory = input
        values = dict(zip(names, trajectory[-1][1:]))
        heat_set = values.get("TRooHeaSet", 293.15) - 273.15
        cool_set = values.get("TRooCooSet", 296.15) - 273.15
        supply_temp = values.get("TSupAHU", values.get("TSupSet", 286.15)) - 273.15
        outdoor_temp = values.get("TOut", 303.15) - 273.15
        return_temp = values.get("TRet", 297.15) - 273.15
        fan = values.get("uFan", 0.8)
        chw_return_temp = values.get("TChwRet", 285.15) - 273.15

        zone_temp = (heat_set + cool_set) / 2.0
        mixed_temp = outdoor_temp * 0.3 + return_temp * 0.7
        return {
            "TRoo": [zone_temp + 273.15],
            "TSup": [supply_temp + 273.15],
            "VSup_flow": [max(0.01, fan)],
            "yDam": [0.4],
            "yDam_actual": [0.35],
            "yVal": [0.2],
            "yVal_actual": [0.15],
            "TMix": [mixed_temp + 273.15],
            "yFan": [fan],
            "yOutDam": [0.3],
            "yHeaVal": [0.25],
            "yCooVal": [0.1],
            "cooCapacityFactor": [1.0],
            "QCoolLoad": [12000.0],
            "QHeaLoad": [8000.0],
            "TChiWatRet": [chw_return_temp + 2.0 + 273.15],
            "VChiWat_flow": [0.0015],
            "PSupFan": [2500.0],
            "dpSup": [400.0],
            "dpSupSet": [400.0],
            "TChwSup": [min(chw_return_temp, 6.0) + 273.15],
            "dpChw": [225000.0],
            "PChi1": [100000.0],
            "PChi2": [90000.0],
            "COP1": [5.8],
            "COP2": [5.6],
            "QCoolDelivered": [500000.0],
            "plantPLR": [0.5],
        }

    def get_log(self):
        return []


def test_model_discovery_and_metadata():
    client = TestClient(app)

    models = client.get("/models")
    assert models.status_code == 200
    model_slugs = {item["slug"] for item in models.json()["models"]}
    assert {"SimpleVAVZone", "SimpleAHU", "SimpleChillerPlant"} <= model_slugs
    # ids are now opaque GUIDs, not the slugs themselves -- assert shape/
    # uniqueness rather than a fixed literal, since a GUID isn't stable
    # across catalog regeneration the way the old id strings were.
    model_ids = [item["id"] for item in models.json()["models"]]
    assert len(model_ids) == len(set(model_ids))
    assert all(item["id"] != item["slug"] for item in models.json()["models"])

    vav_id = _model_id(client, "SimpleVAVZone")
    metadata = client.get(f"/models/{vav_id}/metadata")
    assert metadata.status_code == 200
    body = metadata.json()
    assert body["id"] == vav_id
    assert body["slug"] == "SimpleVAVZone"
    assert body["warmup_seconds"] == 300.0
    assert any(item["name"] == "cooling_setpoint_c" for item in body["inputs"])


def test_vav_initialize_step(monkeypatch):
    monkeypatch.setattr(runtime_manager, "load_fmu_model", lambda path: FakeFMU())
    client = TestClient(app)
    vav_id = _model_id(client, "SimpleVAVZone")

    initialized = client.post(f"/models/{vav_id}/initialize", json={"inputs": {}})
    assert initialized.status_code == 200
    assert initialized.json()["state"] == "RUNNING"
    assert initialized.json()["current_time"] == 300.0
    assert initialized.json()["warmup_progress"] == 1.0
    state_after_initialize = client.get(
        f"/models/{vav_id}/sessions/{initialized.json()['session_id']}/state"
    )
    assert state_after_initialize.status_code == 200
    assert state_after_initialize.json()["outputs"] == {}

    stepped = client.post(
        f"/models/{vav_id}/step",
        json={
            "session_id": initialized.json()["session_id"],
            "time_step": 10.0,
            "inputs": {
                "heating_setpoint_c": 20.0,
                "cooling_setpoint_c": 24.0,
                "supply_air_temp_c": 13.0,
                "outdoor_temp_c": 30.0,
                "internal_gain_w": 1000.0
            }
        }
    )
    assert stepped.status_code == 200
    body = stepped.json()
    assert body["state"] == "RUNNING"
    assert body["current_time"] == 310.0
    assert body["zone_temp_c"] == 22.0
    assert body["damper_command_pct"] == 40.0


def test_ahu_initialize_step(monkeypatch):
    monkeypatch.setattr(runtime_manager, "load_fmu_model", lambda path: FakeFMU())
    client = TestClient(app)
    ahu_id = _model_id(client, "SimpleAHU")

    initialized = client.post(f"/models/{ahu_id}/initialize", json={"inputs": {}})
    assert initialized.status_code == 200
    assert initialized.json()["state"] == "RUNNING"
    assert initialized.json()["current_time"] == 300.0

    stepped = client.post(
        f"/models/{ahu_id}/step",
        json={
            "session_id": initialized.json()["session_id"],
            "time_step": 10.0,
            "inputs": {
                "supply_air_temp_setpoint_c": 12.0,
                "outdoor_air_temp_c": 30.0,
                "return_air_temp_c": 24.0,
                "fan_command_pct": 75.0
            }
        }
    )
    assert stepped.status_code == 200
    body = stepped.json()
    assert body["current_time"] == 310.0
    assert body["supply_air_temp_c"] == 12.0
    assert body["fan_command_pct"] == 75.0


def test_chiller_initialize_step(monkeypatch):
    monkeypatch.setattr(runtime_manager, "load_fmu_model", lambda path: FakeFMU())
    client = TestClient(app)
    chiller_id = _model_id(client, "SimpleChillerPlant")

    initialized = client.post(f"/models/{chiller_id}/initialize", json={"inputs": {}})
    assert initialized.status_code == 200
    assert initialized.json()["state"] == "RUNNING"
    assert initialized.json()["current_time"] == 300.0

    stepped = client.post(
        f"/models/{chiller_id}/step",
        json={
            "session_id": initialized.json()["session_id"],
            "time_step": 10.0,
            "inputs": {
                "chilled_water_return_temp_c": 12.0,
                "condenser_water_temp_c": 29.5,
                "chilled_water_flow_lps": 48.0,
                "chiller_1_run": 1.0,
                "chiller_2_run": 1.0,
                "chilled_water_pump_1_run": 1.0,
                "chilled_water_pump_2_run": 0.0,
                "cooling_tower_fan_1_run": 1.0,
                "cooling_tower_fan_2_run": 1.0
            }
        }
    )
    assert stepped.status_code == 200
    body = stepped.json()
    assert body["current_time"] == 310.0
    assert body["chilled_water_supply_temp_c"] == 6.0
    assert body["chiller_1_power_kw"] == 100.0
    assert body["cooling_delivered_kw"] == 500.0

    state = client.get(
        f"/models/{chiller_id}/sessions/{initialized.json()['session_id']}/state"
    )
    assert state.status_code == 200
    assert state.json()["fmu_inputs"]["chilled_water_flow_lps"]["value"] == 0.048


def test_independent_sessions_for_different_models(monkeypatch):
    monkeypatch.setattr(runtime_manager, "load_fmu_model", lambda path: FakeFMU())
    client = TestClient(app)
    vav_id = _model_id(client, "SimpleVAVZone")
    ahu_id = _model_id(client, "SimpleAHU")

    vav = client.post(f"/models/{vav_id}/initialize", json={"inputs": {}}).json()
    ahu = client.post(f"/models/{ahu_id}/initialize", json={"inputs": {}}).json()

    assert vav["session_id"] != ahu["session_id"]

    wrong_model_step = client.post(
        f"/models/{ahu_id}/step",
        json={"session_id": vav["session_id"], "time_step": 10.0, "inputs": {}}
    )
    assert wrong_model_step.status_code == 404

    vav_step = client.post(
        f"/models/{vav_id}/step",
        json={"session_id": vav["session_id"], "time_step": 10.0, "inputs": {}}
    )
    ahu_step = client.post(
        f"/models/{ahu_id}/step",
        json={"session_id": ahu["session_id"], "time_step": 20.0, "inputs": {}}
    )

    assert vav_step.status_code == 200
    assert ahu_step.status_code == 200
    assert vav_step.json()["current_time"] == 310.0
    assert ahu_step.json()["current_time"] == 320.0


def test_debug_state_contains_diagnostics(monkeypatch):
    monkeypatch.setattr(runtime_manager, "load_fmu_model", lambda path: FakeFMU())
    client = TestClient(app)
    vav_id = _model_id(client, "SimpleVAVZone")

    initialized = client.post(f"/models/{vav_id}/initialize", json={"inputs": {}})
    session_id = initialized.json()["session_id"]
    client.post(
        f"/models/{vav_id}/step",
        json={
            "session_id": session_id,
            "time_step": 5.0,
            "inputs": {
                "heating_setpoint_c": 20.0,
                "cooling_setpoint_c": 24.0,
                "supply_air_temp_c": 13.0,
                "outdoor_temp_c": 30.0,
                "internal_gain_w": 1000.0
            }
        }
    )

    state = client.get(f"/models/{vav_id}/sessions/{session_id}/state")
    assert state.status_code == 200
    body = state.json()
    assert body["state"] == "RUNNING"
    assert body["current_time"] == 305.0
    assert body["warmup_progress"] == 1.0
    assert body["api_inputs"]["heating_setpoint_c"] == 20.0
    assert body["fmu_inputs"]["heating_setpoint_c"]["value"] == 293.15
    assert body["outputs"]["zone_temp_c"] == 22.0
    assert body["diagnostics"]["mode"] == "DEADBAND"
    assert "solver" in body


def test_session_is_not_running_until_warmup_completes(monkeypatch):
    monkeypatch.setattr(runtime_manager, "load_fmu_model", lambda path: FakeFMU())
    manager = runtime_manager.FMUSessionManager(runtime_manager.ModelCatalog.from_environment())
    observed_states = []

    original_warmup = manager._warm_up_session

    def observed_warmup(session_id):
        with manager._lock:
            observed_states.append(manager._sessions[session_id].state.value)
        original_warmup(session_id)
        with manager._lock:
            observed_states.append(manager._sessions[session_id].state.value)

    monkeypatch.setattr(manager, "_warm_up_session", observed_warmup)

    vav_id = _catalog_model_by_slug(manager.catalog, "SimpleVAVZone").id
    response = manager.initialize(vav_id, {})

    assert observed_states == ["INITIALIZING", "RUNNING"]
    assert response["state"] == "RUNNING"
    assert response["warmup_progress"] == 1.0


def test_vav_diagnostic_warning_for_reheat_above_heating_setpoint():
    model = _catalog_model_by_slug(runtime_manager.ModelCatalog.from_environment(), "SimpleVAVZone")
    state = diagnostics.build_step_diagnostics(
        model=model,
        session_id="test-session",
        start_time=0.0,
        final_time=5.0,
        time_step=5.0,
        api_inputs={
            "heating_setpoint_c": 20.0,
            "cooling_setpoint_c": 25.0,
            "supply_air_temp_c": 13.0,
            "outdoor_temp_c": 30.0,
            "internal_gain_w": 1000.0,
        },
        fmu_inputs={},
        outputs={
            "zone_temp_c": 22.0,
            "supply_airflow_m3_s": 0.1,
            "discharge_air_temp_c": 30.0,
            "damper_command_pct": 10.0,
            "damper_position_pct": 10.0,
            "reheat_valve_command_pct": 100.0,
            "reheat_valve_position_pct": 100.0,
        },
        previous_outputs=None,
        elapsed_ms=1.0,
    )
    assert "reheat_active_above_heating_sp" in state["diagnostics"]["warnings"]


def test_diagnostics_failure_does_not_fail_step(monkeypatch):
    monkeypatch.setattr(runtime_manager, "load_fmu_model", lambda path: FakeFMU())

    def broken_diagnostics(**kwargs):
        raise RuntimeError("diagnostics exploded")

    monkeypatch.setattr(runtime_manager, "build_step_diagnostics", broken_diagnostics)
    client = TestClient(app)
    ahu_id = _model_id(client, "SimpleAHU")

    initialized = client.post(f"/models/{ahu_id}/initialize", json={"inputs": {}})
    stepped = client.post(
        f"/models/{ahu_id}/step",
        json={"session_id": initialized.json()["session_id"], "time_step": 10.0, "inputs": {}}
    )

    assert stepped.status_code == 200
    state = client.get(f"/models/{ahu_id}/sessions/{initialized.json()['session_id']}/state")
    assert state.status_code == 200
    assert "diagnostics_failed" in state.json()["diagnostics"]["warnings"]
