"""objects.state_text (migration 031) -- JSON-encoded persistence round-trip through
Database.create_object/update_object/get_object/get_objects. Devices are created via the real
HTTP client (devices_router imports fine), but objects go through database.create_object()
directly rather than POST /devices/{id}/objects -- this Windows dev environment can't import
objects_router (external_objects.py -> discovery.py -> `import fcntl`), same established
convention as test_object_linking.py."""
from __future__ import annotations


def _object_data(**overrides) -> dict:
    data = {
        "object_type": "multi-state-value",
        "object_instance": 1,
        "name": "Detection-Point",
        "units": "no-units",
        "behavior": "constant",
        "behavior_params": '{"value": 1}',
        "enabled": 1,
        "number_of_states": 3,
        "reliability": "no-fault-detected",
        "polarity": "normal",
        "point_type": None,
        "state_text": None,
    }
    data.update(overrides)
    return data


def test_create_object_persists_and_decodes_state_text(client, database):
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 7401, "name": "Classifier-Device"}).json()
    class_labels = ["normal", "sensor_bias", "sensor_stuck"]

    created = database.create_object(device["id"], _object_data(state_text=class_labels))
    assert created["state_text"] == class_labels

    fetched = database.get_object(created["id"])
    assert fetched["state_text"] == class_labels


def test_create_object_without_state_text_decodes_to_none(client, database):
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 7402, "name": "Plain-Device"}).json()
    created = database.create_object(device["id"], _object_data())
    assert created["state_text"] is None
    assert database.get_object(created["id"])["state_text"] is None


def test_update_object_replaces_state_text(client, database):
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 7403, "name": "Classifier-Device"}).json()
    created = database.create_object(device["id"], _object_data(state_text=["a", "b", "c"]))

    updated = database.update_object(created["id"], _object_data(state_text=["x", "y", "z"]))
    assert updated["state_text"] == ["x", "y", "z"]


def test_update_object_omitting_state_text_clears_it(client, database):
    """Full-replace semantics, same as every other ObjectUpdate field
    (e.g. behavior_params) -- an update that doesn't resend state_text
    clears it, it isn't preserved from the previous row. The frontend's
    own ObjectDrawer.vue round-trips the existing value specifically to
    avoid this happening from an unrelated edit."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 7404, "name": "Classifier-Device"}).json()
    created = database.create_object(device["id"], _object_data(state_text=["a", "b", "c"]))

    updated = database.update_object(created["id"], _object_data(state_text=None))
    assert updated["state_text"] is None


def test_get_objects_decodes_state_text_for_every_row(client, database):
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 7405, "name": "Mixed-Device"}).json()
    database.create_object(device["id"], _object_data(object_instance=1, state_text=["a", "b", "c"]))
    database.create_object(device["id"], _object_data(object_instance=2, state_text=None))

    rows = {r["object_instance"]: r for r in database.get_objects(device["id"])}
    assert rows[1]["state_text"] == ["a", "b", "c"]
    assert rows[2]["state_text"] is None
