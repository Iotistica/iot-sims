"""HTTP-level tests for POST /devices/{id}/semantic-suggestions and the
apply flow (plain PUT /devices/{id} + PUT /devices/{id}/objects/{id},
reused unchanged) -- side-effect-free suggestion generation, existing
semantics winning over a new suggestion, and the new field-granular guard
that lets an external device's object be classified (point_type) without
opening up any other field."""
from __future__ import annotations

import pytest


def _create_simulated_ahu(client):
    device = client.post("/devices", json={"device_instance": 1003, "name": "AHU-1"}).json()
    sat = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
    }).json()
    rat = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 2, "name": "RAT", "units": "degrees-celsius",
    }).json()
    return device, [sat, rat]


@pytest.fixture
def external_ahu(database):
    devices = database.sync_external_devices([
        {"device_instance": 1003, "name": "AHU_1_1003", "host": "172.22.0.21", "port": 47808,
         "metadata": {"objectName": "AHU_1_1003"}},
    ])
    device_id = devices[0]["id"]
    objects = database.sync_external_objects(device_id, [
        {"object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius"},
        {"object_type": "analog-input", "object_instance": 2, "name": "RAT", "units": "degrees-celsius"},
    ])
    return {"device_id": device_id, "objects": objects}


def test_suggest_returns_equipment_and_point_suggestions(client):
    device, points = _create_simulated_ahu(client)
    resp = client.post(f"/devices/{device['id']}/semantic-suggestions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"]["suggested_class"] == "Air_Handling_Unit"
    names = {p["source_name"]: p["suggested_class"] for p in body["points"]}
    assert names["SAT"] == "Supply_Air_Temperature_Sensor"
    assert names["RAT"] == "Return_Air_Temperature_Sensor"


def test_suggest_causes_zero_db_mutation(client, database):
    device, points = _create_simulated_ahu(client)
    before_device = database.get_device(device["id"])
    before_objects = database.get_objects(device["id"])

    client.post(f"/devices/{device['id']}/semantic-suggestions")

    assert database.get_device(device["id"]) == before_device
    assert database.get_objects(device["id"]) == before_objects


def test_existing_classification_is_reported_not_overwritten(client, database):
    device, points = _create_simulated_ahu(client)
    client.put(f"/devices/{device['id']}", json={
        "device_instance": 1003, "name": "AHU-1", "equipment_type": "Boiler",
    })

    resp = client.post(f"/devices/{device['id']}/semantic-suggestions")
    body = resp.json()
    assert body["device"]["existing_class"] == "Boiler"
    assert body["device"]["suggested_class"] is None  # never a competing suggestion
    assert database.get_device(device["id"])["equipment_type"] == "Boiler"  # untouched


def test_simulated_device_apply_via_existing_endpoints_sets_point_type(client, database):
    device, points = _create_simulated_ahu(client)
    sat = points[0]

    resp = client.put(f"/devices/{device['id']}/objects/{sat['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
        "point_type": "Supply_Air_Temperature_Sensor",
    })
    assert resp.status_code == 200
    assert database.get_object(sat["id"])["point_type"] == "Supply_Air_Temperature_Sensor"
    assert database.get_object(sat["id"])["name"] == "SAT"  # never renamed


def test_external_device_suggestion_matches_simulated(client, external_ahu):
    resp = client.post(f"/devices/{external_ahu['device_id']}/semantic-suggestions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"]["suggested_class"] == "Air_Handling_Unit"
    names = {p["source_name"]: p["suggested_class"] for p in body["points"]}
    assert names["SAT"] == "Supply_Air_Temperature_Sensor"


def test_external_device_point_type_apply_succeeds(client, database, external_ahu):
    sat = external_ahu["objects"][0]
    resp = client.put(f"/devices/{external_ahu['device_id']}/objects/{sat['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
        "point_type": "Supply_Air_Temperature_Sensor",
    })
    assert resp.status_code == 200
    assert database.get_object(sat["id"])["point_type"] == "Supply_Air_Temperature_Sensor"
    assert database.get_object(sat["id"])["name"] == "SAT"


def test_external_device_other_object_fields_still_rejected(client, external_ahu):
    sat = external_ahu["objects"][0]
    resp = client.put(f"/devices/{external_ahu['device_id']}/objects/{sat['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "renamed", "units": "degrees-celsius",
        "point_type": "Supply_Air_Temperature_Sensor",
    })
    assert resp.status_code == 403


def test_external_device_equipment_type_apply_still_works(client, database, external_ahu):
    """Unchanged behavior -- confirms the object-level guard change didn't
    disturb the device-level guard, which already allowed equipment_type.
    Sends the full curated body (matching the real frontend apply flow)
    rather than relying on DeviceUpdate's schema defaults, which don't
    match what sync_external_devices() actually persisted (e.g.
    vendor_name defaults to "Iotistica" in the schema but "Unknown" here)
    and would otherwise trip the protected-field guard on an unrelated
    field."""
    existing = database.get_device(external_ahu["device_id"])
    resp = client.put(f"/devices/{external_ahu['device_id']}", json={
        "device_instance": existing["device_instance"], "name": existing["name"],
        "description": existing["description"], "vendor_name": existing["vendor_name"],
        "model_name": existing["model_name"], "enabled": existing["enabled"],
        "firmware_revision": existing["firmware_revision"], "protocol_revision": existing["protocol_revision"],
        "max_apdu_length_accepted": existing["max_apdu_length_accepted"],
        "segmentation_supported": existing["segmentation_supported"],
        "location_id": existing["location_id"], "equipment_type": "Air_Handling_Unit",
        "can_receive_event_notifications": existing["can_receive_event_notifications"],
    })
    assert resp.status_code == 200
    assert database.get_device(external_ahu["device_id"])["equipment_type"] == "Air_Handling_Unit"


def test_apply_is_idempotent(client, database):
    device, points = _create_simulated_ahu(client)
    sat = points[0]
    body = {
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
        "point_type": "Supply_Air_Temperature_Sensor",
    }
    client.put(f"/devices/{device['id']}/objects/{sat['id']}", json=body)
    client.put(f"/devices/{device['id']}/objects/{sat['id']}", json=body)

    entities = [
        dict(row) for row in database._conn().execute(
            "SELECT * FROM semantic_entities WHERE object_id=?", (sat["id"],)
        )
    ]
    assert len(entities) == 1  # re-applying the same class updates in place, never duplicates
