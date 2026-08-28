"""HTTP layer for the Replay Recording router (src/api/routers/replay_recordings.py)
-- thin, so this only checks the wiring and the "external devices only"
guard; the actual persistence/buffer/grouping logic is covered directly
against Database in the other test_replay_*.py files."""
from __future__ import annotations


def _make_external_device(database, *, instance: int, name: str) -> dict:
    return database.sync_external_devices([{
        "device_instance": instance,
        "name": name,
        "host": "127.0.0.1",
        "port": 47808,
        "metadata": {},
    }])[0]


def _make_simulated_device(client, *, instance: int, name: str) -> dict:
    return client.post("/devices", json={"device_instance": instance, "name": name}).json()


def test_create_recording_allowed_for_simulated_device(client):
    # Recordings are a general-purpose, device-agnostic feature -- a
    # simulated device's points are sampled directly from its own live
    # in-process values (see runtime._replay_recording_sample_once's
    # non-external branch) rather than over the wire, but creation is
    # otherwise identical, so this must succeed exactly like an external
    # device's does.
    device = _make_simulated_device(client, instance=9501, name="Not External")

    res = client.post(f"/devices/{device['id']}/replay-recordings", json={
        "name": "Should Work",
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    })

    assert res.status_code == 201
    assert res.json()["source_device_id"] == device["id"]


def test_create_start_stop_delete_recording_via_api(client, database):
    device = _make_external_device(database, instance=9502, name="External Device")
    point = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 0, "name": "A",
        "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":0}',
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
        "polarity": "normal", "point_type": None,
    })

    created = client.post(f"/devices/{device['id']}/replay-recordings", json={
        "name": "API Recording",
        "point_ids": [point["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    }).json()
    assert created["status"] == "recording"
    assert created["point_count"] == 1

    listed = client.get(f"/devices/{device['id']}/replay-recordings").json()
    assert len(listed) == 1

    updated = client.put(f"/replay-recordings/{created['id']}", json={
        "name": "Renamed Recording",
        "sample_interval_seconds": 10.0,
        "maximum_samples": 500,
        "buffer_mode": "stop",
    }).json()
    assert updated["name"] == "Renamed Recording"
    assert updated["sample_interval_seconds"] == 10.0
    assert updated["maximum_samples"] == 500
    assert updated["buffer_mode"] == "stop"
    assert updated["status"] == "recording"  # editing doesn't touch status
    assert updated["point_count"] == 1  # points untouched by edit

    stopped = client.post(f"/replay-recordings/{created['id']}/stop").json()
    assert stopped["status"] == "completed"

    res = client.delete(f"/replay-recordings/{created['id']}")
    assert res.status_code == 204
    assert client.get(f"/replay-recordings/{created['id']}").status_code == 404
