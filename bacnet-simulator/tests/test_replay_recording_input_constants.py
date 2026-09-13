"""Snapshotting a device's constant-sourced FMU- or AI-backed inputs onto a
recording at record time -- migration 024 / recordings.py's
_constant_sourced_inputs / Database.add_recording_input_constants.

Deliberately a separate table from recording_points (see migration
024's own docstring): a constant-sourced input never had a BACnet point to
begin with, so there's nothing point-shaped to snapshot, only the literal
value the simulation actually used. This must be captured at record time,
not re-derived from the device's current config later -- the whole reason
this exists is that the device's config can change between recordings (or
between a recording and whenever training runs), and re-deriving live would
silently mislabel what an old recording's rows actually represent.
"""
from __future__ import annotations

import json

from src.api.routers import simulation as simulation_router
from src.simulation.models.registry import ModelDefinition, VariableDefinition


def _fmu_like_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="CONSTFMU", label="Const FMU", provider_type="fmu", description="",
        parameters=(),
        variables=(
            VariableDefinition("outdoor_air_temp_c", "OAT", "input", required=False),
            VariableDefinition("supply_air_temp_c", "SAT", "output", required=False),
        ),
        factory=lambda parameters: None, runtime_model="CONSTFMU",
    )


def _ai_like_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="CONSTAI", label="Const AI", provider_type="ai", description="",
        parameters=(),
        variables=(
            VariableDefinition("outdoor_air_temp_c", "OAT", "input", required=False),
            VariableDefinition("supply_air_temp_c", "SAT", "output", required=False),
        ),
        factory=lambda parameters: None, runtime_model="CONSTAI",
    )


def _create_fmu_config(client, device_id: int, *, input_defaults: dict) -> dict:
    input_sources = {name: "constant" for name in input_defaults}
    resp = client.post("/simulation/models", json={
        "name": "Const Test FMU", "provider_type": "fmu", "model_type": "CONSTFMU",
        "enabled": True, "created_from_device_id": device_id,
        "parameters": {"input_sources": input_sources, "input_defaults": input_defaults},
        "mappings": [], "aggregate_mappings": [],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_ai_config(client, device_id: int, *, input_defaults: dict) -> dict:
    input_sources = {name: "constant" for name in input_defaults}
    resp = client.post("/simulation/models", json={
        "name": "Const Test AI", "provider_type": "ai", "model_type": "CONSTAI",
        "enabled": True, "created_from_device_id": device_id,
        "parameters": {"input_sources": input_sources, "input_defaults": input_defaults},
        "mappings": [], "aggregate_mappings": [],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_new_recording_snapshots_constant_sourced_inputs_via_api(client, database, monkeypatch):
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _fmu_like_definition())
    monkeypatch.setattr(simulation_router, "reload_model", lambda *a, **k: {"enabled": True})
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9301, "name": "Const-Snapshot-Test"}).json()

    _create_fmu_config(client, device["id"], input_defaults={"outdoor_air_temp_c": 30.0})

    resp = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "AI-1", "description": "",
        "point_ids": None, "sample_interval_seconds": 5, "maximum_samples": 100, "buffer_mode": "stop",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["input_constants"] == [{"variable": "outdoor_air_temp_c", "value": 30.0}]


def test_recording_has_no_snapshot_when_device_has_no_active_model(client, database):
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9302, "name": "No-Model-Test"}).json()

    resp = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "AI-1", "description": "",
        "point_ids": None, "sample_interval_seconds": 5, "maximum_samples": 100, "buffer_mode": "stop",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["input_constants"] == []


def test_recording_has_no_snapshot_for_ai_backed_device_with_no_constants_configured(client, database):
    """An AI-backed model with nothing in parameters.input_sources/
    input_defaults (still the common case, e.g. every input point-mapped
    or unconfigured) -- nothing to snapshot, not an error."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9303, "name": "AI-Backed-Test"}).json()
    from src.simulation.models.store import ensure_simulation_model_schema
    ensure_simulation_model_schema(database)
    conn = database._conn()
    conn.execute(
        "INSERT INTO simulation_model_configs (name, provider_type, model_type, enabled, created_from_device_id, parameters) "
        "VALUES ('AI model', 'ai', 'some-ai-id', 1, ?, '{}')",
        (device["id"],),
    )
    conn.commit()
    conn.close()

    resp = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "AI-1", "description": "",
        "point_ids": None, "sample_interval_seconds": 5, "maximum_samples": 100, "buffer_mode": "stop",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["input_constants"] == []


def test_new_recording_snapshots_constant_sourced_inputs_for_ai_backed_device(client, database, monkeypatch):
    """AI models now support the same Constant/Point/Aggregate input
    sourcing FMU already had -- an AI device's constant inputs get
    snapshotted onto a new recording exactly like an FMU device's already
    did (see this module's own docstring for why that matters)."""
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _ai_like_definition())
    monkeypatch.setattr(simulation_router, "reload_model", lambda *a, **k: {"enabled": True})
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9306, "name": "AI-Const-Snapshot-Test"}).json()

    _create_ai_config(client, device["id"], input_defaults={"outdoor_air_temp_c": 30.0})

    resp = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "AI-1", "description": "",
        "point_ids": None, "sample_interval_seconds": 5, "maximum_samples": 100, "buffer_mode": "stop",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["input_constants"] == [{"variable": "outdoor_air_temp_c", "value": 30.0}]


def test_two_recordings_of_the_same_device_can_carry_different_constant_values(client, database, monkeypatch):
    """The device's constant changes between two recordings -- each
    recording keeps its OWN snapshotted value, not whatever the device is
    configured with by the time the second recording starts."""
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _fmu_like_definition())
    monkeypatch.setattr(simulation_router, "reload_model", lambda *a, **k: {"enabled": True})
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9304, "name": "Two-Snapshots-Test"}).json()

    config = _create_fmu_config(client, device["id"], input_defaults={"outdoor_air_temp_c": 30.0})
    rec1 = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "AI-1", "description": "",
        "point_ids": None, "sample_interval_seconds": 5, "maximum_samples": 100, "buffer_mode": "stop",
    }).json()

    # Change the device's own constant before the second recording.
    resp = client.put(f"/simulation/models/{config['id']}", json={
        "name": "Const Test FMU", "provider_type": "fmu", "model_type": "CONSTFMU",
        "enabled": True, "created_from_device_id": device["id"],
        "parameters": {
            "input_sources": {"outdoor_air_temp_c": "constant"},
            "input_defaults": {"outdoor_air_temp_c": 20.0},
        },
        "mappings": [], "aggregate_mappings": [],
    }, params={"apply": "true"})
    assert resp.status_code == 200, resp.text

    rec2 = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "AI-2", "description": "",
        "point_ids": None, "sample_interval_seconds": 5, "maximum_samples": 100, "buffer_mode": "stop",
    }).json()

    assert rec1["input_constants"] == [{"variable": "outdoor_air_temp_c", "value": 30.0}]
    assert rec2["input_constants"] == [{"variable": "outdoor_air_temp_c", "value": 20.0}]

    # And re-fetching rec1 later still reports its OWN original value, not
    # today's live config -- the whole point of snapshotting.
    refetched = client.get(f"/recordings/{rec1['id']}").json()
    assert refetched["input_constants"] == [{"variable": "outdoor_air_temp_c", "value": 30.0}]


def test_add_recording_input_constants_db_method(client, database):
    """Direct DB-layer coverage of the insert helper itself, independent of
    the API route."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9305, "name": "DB-Layer-Test"}).json()
    recording = database.create_recording(device["id"], {
        "name": "AI-1", "point_ids": [], "sample_interval_seconds": 5.0,
        "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert recording["input_constants"] == []

    database.add_recording_input_constants(recording["id"], {"a": 1.0, "b": 2.5})

    refetched = database.get_recording(recording["id"])
    assert sorted(refetched["input_constants"], key=lambda c: c["variable"]) == [
        {"variable": "a", "value": 1.0},
        {"variable": "b", "value": 2.5},
    ]

    # A no-op call (empty dict) doesn't error and doesn't add rows.
    database.add_recording_input_constants(recording["id"], {})
    assert len(database.get_recording(recording["id"])["input_constants"]) == 2
