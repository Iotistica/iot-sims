"""Every simulator write/mutation endpoint must reject an external-BACnet
device's inventory row -- this enforces requirement 3's safety boundary at
the HTTP layer (backend, not just the UI hiding controls), and requirement
9's "read-only means read-only" for object-level actions. DELETE
/devices/{id} is the deliberate exception (see src/api/guards.py) --
removing the inventory record performs no BACnet action.

PUT /devices/{id} is a second, narrower exception (added alongside the
device/object UX unification): project-local fields (name, description,
location_id, equipment_type, can_receive_event_notifications) may change
freely -- only fields that mirror the real physical device or simulator
ownership (device_instance, vendor_name, model_name, firmware_revision,
protocol_revision, max_apdu_length_accepted, segmentation_supported,
enabled) stay rejected. See reject_external_source_mutation() in
src/api/guards.py."""
from __future__ import annotations

import pytest


@pytest.fixture
def external_device(database):
    devices = database.sync_external_devices([
        {"device_instance": 5000, "name": "ext", "host": "172.22.0.50", "port": 47808,
         "metadata": {"objectName": "External-AHU", "vendorName": "Acme"}},
    ])
    device_id = devices[0]["id"]
    objects = database.sync_external_objects(device_id, [
        {"object_type": "analog-output", "object_instance": 1, "name": "CoolValve", "units": "percent"},
    ])
    return {"device_id": device_id, "object_id": objects[0]["id"]}


# Matches exactly what sync_external_devices()/the devices table's own
# column defaults produce for the `external_device` fixture above -- a PUT
# with this body unchanged is a true no-op on every field.
def _unchanged_body(**overrides):
    body = {
        "device_instance": 5000,
        "name": "External-AHU",
        "description": "",
        "vendor_name": "Acme",
        "model_name": "Unknown",
        "enabled": 1,
        "firmware_revision": "N/A",
        "protocol_revision": 22,
        "max_apdu_length_accepted": 1024,
        "segmentation_supported": "segmented-both",
    }
    body.update(overrides)
    return body


def test_get_device_allowed(client, external_device):
    resp = client.get(f"/devices/{external_device['device_id']}")
    assert resp.status_code == 200
    assert resp.json()["source_type"] == "external-bacnet"


def test_get_objects_allowed(client, external_device):
    resp = client.get(f"/devices/{external_device['device_id']}/objects")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_update_device_instance_rejected(client, external_device):
    resp = client.put(
        f"/devices/{external_device['device_id']}",
        json=_unchanged_body(device_instance=5001),
    )
    assert resp.status_code == 403


def test_update_device_vendor_rejected(client, external_device):
    resp = client.put(
        f"/devices/{external_device['device_id']}",
        json=_unchanged_body(vendor_name="Someone Else"),
    )
    assert resp.status_code == 403


def test_update_device_enabled_rejected(client, external_device):
    resp = client.put(
        f"/devices/{external_device['device_id']}",
        json=_unchanged_body(enabled=0),
    )
    assert resp.status_code == 403


def test_update_device_location_allowed(client, external_device, database):
    location = database.create_location("Basement", None, "")
    resp = client.put(
        f"/devices/{external_device['device_id']}",
        json=_unchanged_body(location_id=location["id"]),
    )
    assert resp.status_code == 200
    assert resp.json()["location_id"] == location["id"]


def test_update_device_name_and_description_allowed(client, external_device):
    resp = client.put(
        f"/devices/{external_device['device_id']}",
        json=_unchanged_body(name="Renamed for display", description="Rooftop AHU"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed for display"
    assert body["description"] == "Rooftop AHU"


def test_delete_device_allowed(client, external_device):
    """Deliberate exception: removing the project-inventory record performs
    no BACnet action, so it's allowed for external devices too."""
    resp = client.delete(f"/devices/{external_device['device_id']}")
    assert resp.status_code == 204
    assert client.get(f"/devices/{external_device['device_id']}").status_code == 404


def test_delete_device_cascades_to_its_objects(client, external_device, database):
    client.delete(f"/devices/{external_device['device_id']}")
    assert database.get_object(external_device["object_id"]) is None


def test_create_energy_model_rejected(client, external_device):
    resp = client.post(
        f"/devices/{external_device['device_id']}/energy-models",
        json={"model_type": "chiller", "parameters": {}},
    )
    assert resp.status_code == 403


def test_create_object_rejected(client, external_device):
    resp = client.post(
        f"/devices/{external_device['device_id']}/objects",
        json={"object_type": "analog-input", "object_instance": 99, "name": "new-point"},
    )
    assert resp.status_code == 403


def test_update_object_rejected(client, external_device):
    resp = client.put(
        f"/devices/{external_device['device_id']}/objects/{external_device['object_id']}",
        json={"object_type": "analog-output", "object_instance": 1, "name": "renamed"},
    )
    assert resp.status_code == 403


def test_delete_object_rejected(client, external_device):
    resp = client.delete(
        f"/devices/{external_device['device_id']}/objects/{external_device['object_id']}",
    )
    assert resp.status_code == 403


def test_set_value_rejected(client, external_device):
    resp = client.post(
        f"/devices/{external_device['device_id']}/objects/{external_device['object_id']}/value",
        json={"value": 42.0},
    )
    assert resp.status_code == 403


def test_write_priority_array_rejected(client, external_device):
    resp = client.put(
        f"/devices/{external_device['device_id']}/objects/{external_device['object_id']}/priority-array/1",
        json={"value": 50.0},
    )
    assert resp.status_code == 403


# import_device_ede (POST /devices/{id}/import/ede) is defined directly on
# src/legacy.py's monolithic `api` app, not a separate router -- it isn't
# reachable through the lightweight test_app fixture (which deliberately
# avoids legacy.py's full lifespan(), see conftest.py's docstring), so it
# isn't covered here. Its guard call is the same one-line
# reject_external_device(device) pattern as every route above; verified
# live in the Docker acceptance pass instead.
