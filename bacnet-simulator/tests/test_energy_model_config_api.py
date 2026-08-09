"""API-level tests for configurable Energy Model setup: energy model config
CRUD (GET/POST /devices/{id}/energy-models, PUT/DELETE /energy/models/{id})
and its integration with EnergyEngine.evaluate_all()/GET /energy/equipment/
POST /energy/evaluate.

Builds its own FastAPI app (real Database + real EnergyEngine +
devices_router + energy_router) rather than using conftest.py's shared
test_app/client fixtures, which don't wire in energy_router or
app.state.energy_engine -- same self-contained-harness precedent as
tests/test_packet_capture_api_device_filter.py."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routers.devices import router as devices_router
from src.api.routers.energy import router as energy_router
from src.energy import EnergyEngine
from src.legacy import Database


class _FakeEngine:
    """async reload() for devices.py's schedule_engine_reload();
    get_device_point_values() for EnergyEngine's model evaluation -- reads
    each object's manual_value straight out of behavior_params, no live
    bacpypes3 objects needed for these config/validation-focused tests."""

    async def reload(self) -> None:
        pass

    def get_device_point_values(self, objects):
        values = {}
        for o in objects:
            point_type = o.get("point_type")
            if not point_type:
                continue
            try:
                params = json.loads(o.get("behavior_params") or "{}")
            except (TypeError, ValueError):
                params = {}
            values[point_type] = params.get("value", 0)
        return values


@pytest.fixture
def client_factory(tmp_path):
    def _make():
        db = Database(tmp_path / "test.db")
        db.setup()

        app = FastAPI()
        fake_engine = _FakeEngine()
        app.state.db = db
        app.state.engine = fake_engine
        app.state.device_names = {}
        app.state.energy_engine = EnergyEngine(
            database=db, simulation_engine=fake_engine, event_callback=None,
        )

        app.include_router(devices_router)
        app.include_router(energy_router)

        return TestClient(app), db

    return _make


def _create_device(client, *, instance=1001, name="Chiller-Plant"):
    resp = client.post("/devices", json={"device_instance": instance, "name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_create_chiller_config(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client)

    resp = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "instance_key": "default", "enabled": True,
        "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.8},
    })
    assert resp.status_code == 201, resp.text
    config = resp.json()
    assert config["model_type"] == "chiller"
    assert config["parameters"]["rated_capacity_kw"] == 3000

    listing = client.get(f"/devices/{device_id}/energy-models").json()
    assert len(listing) == 1


def test_update_chiller_config(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client)

    created = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "instance_key": "default", "enabled": True,
        "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.8},
    }).json()

    resp = client.put(f"/energy/models/{created['id']}", json={
        "model_type": "chiller", "instance_key": "default", "enabled": False,
        "parameters": {"rated_capacity_kw": 3500, "full_load_cop": 6.0},
    })
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["id"] == created["id"]
    assert updated["enabled"] is False
    assert updated["parameters"]["rated_capacity_kw"] == 3500

    listing = client.get(f"/devices/{device_id}/energy-models").json()
    assert len(listing) == 1  # updated in place, not duplicated


def test_delete_chiller_config(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client)
    created = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.8},
    }).json()

    resp = client.delete(f"/energy/models/{created['id']}")
    assert resp.status_code == 204

    listing = client.get(f"/devices/{device_id}/energy-models").json()
    assert listing == []


def test_duplicate_post_upserts_not_duplicates(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client)

    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.8},
    })
    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "parameters": {"rated_capacity_kw": 4000, "full_load_cop": 6.0},
    })

    listing = client.get(f"/devices/{device_id}/energy-models").json()
    assert len(listing) == 1
    assert listing[0]["parameters"]["rated_capacity_kw"] == 4000


def test_update_causing_key_collision_returns_409(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client)

    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "instance_key": "default",
        "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.8},
    })
    b = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "instance_key": "alt",
        "parameters": {"rated_capacity_kw": 1000, "full_load_cop": 5.0},
    }).json()

    resp = client.put(f"/energy/models/{b['id']}", json={
        "model_type": "chiller", "instance_key": "default", "enabled": True,
        "parameters": {"rated_capacity_kw": 1000, "full_load_cop": 5.0},
    })
    assert resp.status_code == 409


def test_unsupported_model_type_rejected(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client)

    resp = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "turbine", "parameters": {},
    })
    assert resp.status_code == 400


def test_invalid_chiller_parameters_rejected(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client)
    resp = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "parameters": {},  # no capacity at all
    })
    assert resp.status_code == 400


def test_invalid_boiler_parameters_rejected(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client, instance=1002, name="HW-Plant")
    resp = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "boiler", "parameters": {"thermal_efficiency": 1.5},
    })
    assert resp.status_code == 400


def test_invalid_ahu_parameters_rejected(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client, instance=1003, name="AHU-1")
    resp = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "ahu", "parameters": {},  # no fan power at all
    })
    assert resp.status_code == 400


def test_invalid_lighting_parameters_rejected(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client, instance=1501, name="DALI-GW-L1")
    resp = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "lighting", "parameters": {"rated_power_kw": 0},
    })
    assert resp.status_code == 400


def test_create_boiler_config(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client, instance=1002, name="HW-Plant")
    resp = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "boiler",
        "parameters": {"rated_thermal_capacity_kw": 4000.0, "thermal_efficiency": 0.92},
    })
    assert resp.status_code == 201, resp.text


def test_create_ahu_config(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client, instance=1003, name="AHU-1")
    resp = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "ahu",
        "parameters": {"supply_fan_rated_power_kw": 15.0, "return_fan_rated_power_kw": 11.0},
    })
    assert resp.status_code == 201, resp.text


def test_multiple_named_chiller_instances_coexist(client_factory):
    """Chiller/Boiler/AHU are NOT restricted to a single instance -- multiple
    named scenario-comparison configs (e.g. Baseline vs Efficient) on the
    same device must all be creatable and independently editable."""
    client, _db = client_factory()
    device_id = _create_device(client)

    baseline = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "instance_key": "Baseline",
        "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.0},
    })
    assert baseline.status_code == 201, baseline.text

    efficient = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "instance_key": "Efficient",
        "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 6.5},
    })
    assert efficient.status_code == 201, efficient.text

    degraded = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "instance_key": "Degraded",
        "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 3.8},
    })
    assert degraded.status_code == 201, degraded.text

    listing = client.get(f"/devices/{device_id}/energy-models").json()
    assert len(listing) == 3
    assert {c["instance_key"] for c in listing} == {"Baseline", "Efficient", "Degraded"}
    assert {c["parameters"]["full_load_cop"] for c in listing} == {5.0, 6.5, 3.8}


def test_multiple_boiler_instances_coexist(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client, instance=1002, name="HW-Plant")

    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "boiler", "instance_key": "Baseline",
        "parameters": {"rated_thermal_capacity_kw": 4000.0, "thermal_efficiency": 0.90},
    })
    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "boiler", "instance_key": "High Efficiency",
        "parameters": {"rated_thermal_capacity_kw": 4000.0, "thermal_efficiency": 0.97},
    })

    listing = client.get(f"/devices/{device_id}/energy-models").json()
    assert len(listing) == 2
    assert {c["instance_key"] for c in listing} == {"Baseline", "High Efficiency"}


def test_multiple_ahu_instances_coexist(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client, instance=1003, name="AHU-1")

    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "ahu", "instance_key": "Baseline",
        "parameters": {"supply_fan_rated_power_kw": 15.0, "return_fan_rated_power_kw": 11.0},
    })
    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "ahu", "instance_key": "Degraded",
        "parameters": {"supply_fan_rated_power_kw": 18.0, "return_fan_rated_power_kw": 13.0},
    })

    listing = client.get(f"/devices/{device_id}/energy-models").json()
    assert len(listing) == 2
    assert {c["instance_key"] for c in listing} == {"Baseline", "Degraded"}


def test_blank_model_name_rejected(client_factory):
    """Backend enforces a non-empty instance_key ("Model Name") itself --
    not relying only on frontend required-field validation."""
    client, _db = client_factory()
    device_id = _create_device(client)

    resp = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "instance_key": "",
        "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.8},
    })
    assert resp.status_code == 422  # Pydantic min_length=1 rejection


def test_multiple_enabled_instances_all_appear_in_evaluate_and_equipment(client_factory):
    """Existing evaluation behavior is unchanged by allowing multiple named
    instances: evaluate_all()/GET /energy/equipment return one entry per
    enabled config, with no dedup/collapse across instances of the same
    model_type -- aggregating them into a single building total is a
    frontend (UtilitiesDashboard.vue) concern, not something the backend
    does or should do here."""
    client, _db = client_factory()
    device_id = _create_device(client)

    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "instance_key": "Baseline",
        "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.0},
    })
    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "instance_key": "Efficient",
        "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 6.5},
    })

    evaluate_resp = client.post("/energy/evaluate?elapsed_seconds=5")
    assert evaluate_resp.status_code == 200
    results = [r for r in evaluate_resp.json() if r["device_id"] == device_id]
    assert len(results) == 2
    assert {r["instance_key"] for r in results} == {"Baseline", "Efficient"}

    equipment = client.get("/energy/equipment").json()
    equipment_for_device = [e for e in equipment if e["device_id"] == device_id]
    assert len(equipment_for_device) == 2


def test_lighting_multiple_instance_keys_coexist(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client, instance=1501, name="DALI-GW-L1")

    resp_a = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "lighting", "instance_key": "zone-a", "parameters": {"rated_power_kw": 3.0},
    })
    assert resp_a.status_code == 201, resp_a.text
    resp_b = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "lighting", "instance_key": "zone-b", "parameters": {"rated_power_kw": 2.5},
    })
    assert resp_b.status_code == 201, resp_b.text

    listing = client.get(f"/devices/{device_id}/energy-models").json()
    assert len(listing) == 2
    assert {c["instance_key"] for c in listing} == {"zone-a", "zone-b"}


def test_disabled_config_excluded_enabled_included(client_factory):
    client, _db = client_factory()
    device_id = _create_device(client)
    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "enabled": True,
        "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.8},
    })

    device_id_2 = _create_device(client, instance=1002, name="Chiller-2")
    client.post(f"/devices/{device_id_2}/energy-models", json={
        "model_type": "chiller", "enabled": False,
        "parameters": {"rated_capacity_kw": 1000, "full_load_cop": 5.0},
    })

    evaluate_resp = client.post("/energy/evaluate?elapsed_seconds=5")
    assert evaluate_resp.status_code == 200
    evaluated_ids = {r["device_id"] for r in evaluate_resp.json()}
    assert device_id in evaluated_ids
    assert device_id_2 not in evaluated_ids

    equipment = client.get("/energy/equipment").json()
    assert any(e["device_id"] == device_id for e in equipment)
    assert not any(e["device_id"] == device_id_2 for e in equipment)


def test_zero_configs_returns_empty_cleanly(client_factory):
    client, _db = client_factory()

    resp = client.get("/energy/equipment")
    assert resp.status_code == 200
    assert resp.json() == []

    resp2 = client.post("/energy/evaluate?elapsed_seconds=5")
    assert resp2.status_code == 200
    assert resp2.json() == []


def test_device_deletion_cascades_energy_configs(client_factory):
    client, db = client_factory()
    device_id = _create_device(client)
    client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.8},
    })

    resp = client.delete(f"/devices/{device_id}")
    assert resp.status_code == 204

    assert db.get_energy_model_configs(device_id) == []


def test_deleting_config_does_not_delete_device(client_factory):
    client, db = client_factory()
    device_id = _create_device(client)
    config = client.post(f"/devices/{device_id}/energy-models", json={
        "model_type": "chiller", "parameters": {"rated_capacity_kw": 3000, "full_load_cop": 5.8},
    }).json()

    resp = client.delete(f"/energy/models/{config['id']}")
    assert resp.status_code == 204

    assert db.get_device(device_id) is not None


def test_raw_sql_inserted_config_still_works(client_factory):
    """Exactly the user's manual workaround (bypassing the API entirely) --
    confirms the fixed Database methods don't require existing manually-
    seeded rows to be in some new shape."""
    client, db = client_factory()
    device_id = _create_device(client, instance=1501, name="DALI-GW-L1")

    with db._conn() as conn:
        conn.execute(
            "INSERT INTO energy_model_configs (device_id, model_type, instance_key, enabled, parameters) "
            "VALUES (?,?,?,?,?)",
            (device_id, "lighting", "zone-a", 1, json.dumps({"rated_power_kw": 3.0})),
        )
        conn.commit()

    listing = client.get(f"/devices/{device_id}/energy-models").json()
    assert len(listing) == 1
    assert listing[0]["instance_key"] == "zone-a"

    # /energy/equipment reflects EnergyEngine's last-evaluated cache, not a
    # live query against energy_model_configs -- evaluate first.
    evaluate_resp = client.post("/energy/evaluate?elapsed_seconds=5")
    assert evaluate_resp.status_code == 200, evaluate_resp.text

    equipment = client.get("/energy/equipment").json()
    assert any(e["device_id"] == device_id for e in equipment)
