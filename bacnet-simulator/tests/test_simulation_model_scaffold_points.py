"""POST /simulation/models/scaffold-points -- the drawer's "Create a point on
this controller for each output" checkbox. Creates one BACnet object per
declared model output that has no same-named point yet; additive only."""
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
                               semantic=SemanticContract(id="start_cmd", role="command")),
            VariableDefinition("active_power_kw", "Active Power", "output", unit="kW",
                               semantic=SemanticContract(id="active_power_kw", role="sensor")),
            VariableDefinition("engine_speed_rpm", "Engine Speed", "output", unit="rpm",
                               semantic=SemanticContract(id="engine_speed_rpm", role="sensor")),
            VariableDefinition("reactive_power_kvar", "Reactive Power", "output", unit="kvar",
                               semantic=SemanticContract(id="reactive_power_kvar", role="sensor")),
            VariableDefinition("running", "Running", "output", unit="boolean",
                               semantic=SemanticContract(id="running", role="status")),
        ),
        factory=lambda parameters: None,
        runtime_model="DieselGenset",
    )


def _patch(monkeypatch):
    definition = _genset_definition()
    monkeypatch.setattr(
        simulation_router, "_runtime_definition", lambda _db, _mt: definition
    )


def test_scaffold_creates_one_point_per_output_with_mapped_units(client, database, monkeypatch):
    _patch(monkeypatch)
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 3301, "name": "Genset Controller"}).json()

    res = client.post("/simulation/models/scaffold-points", json={
        "model_type": "DieselGenset",
        "created_from_device_id": device["id"],
    })
    assert res.status_code == 200, res.text
    body = res.json()

    by_var = {c["variable"]: c for c in body["created"]}
    assert set(by_var) == {"active_power_kw", "engine_speed_rpm", "reactive_power_kvar", "running"}
    # inputs are never scaffolded by default
    assert "start_cmd" not in by_var

    assert by_var["active_power_kw"]["object_type"] == "analog-value"
    assert by_var["active_power_kw"]["units"] == "kilowatts"
    assert by_var["engine_speed_rpm"]["units"] == "revolutions-per-minute"
    assert by_var["reactive_power_kvar"]["units"] == "kilovolt-amperes-reactive"
    # boolean/status output -> binary-value, no unit
    assert by_var["running"]["object_type"] == "binary-value"
    assert by_var["running"]["units"] == "no-units"

    objects = database.get_objects(device["id"])
    assert sorted(o["name"] for o in objects) == [
        "Active Power", "Engine Speed", "Reactive Power", "Running",
    ]


def test_scaffold_is_idempotent_by_name(client, database, monkeypatch):
    _patch(monkeypatch)
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 3302, "name": "GC2"}).json()

    first = client.post("/simulation/models/scaffold-points", json={
        "model_type": "DieselGenset", "created_from_device_id": device["id"],
    }).json()
    assert len(first["created"]) == 4

    second = client.post("/simulation/models/scaffold-points", json={
        "model_type": "DieselGenset", "created_from_device_id": device["id"],
    }).json()
    assert second["created"] == []
    assert {s["variable"] for s in second["skipped"]} == {
        "active_power_kw", "engine_speed_rpm", "reactive_power_kvar", "running",
    }
    assert len(database.get_objects(device["id"])) == 4


def test_scaffold_rejects_missing_device(client, monkeypatch):
    _patch(monkeypatch)
    res = client.post("/simulation/models/scaffold-points", json={
        "model_type": "DieselGenset", "created_from_device_id": 999999,
    })
    assert res.status_code == 422
