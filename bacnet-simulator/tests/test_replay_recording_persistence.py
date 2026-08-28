"""Database.*_replay_recording* -- basic create/read persistence for the
Replay Recording feature (device-wide, SQLite-backed recording of an
external BACnet device, later played back through a cloned simulated
device). Covers the repository layer only, not the sampling loop or API."""
from __future__ import annotations


def _make_device(client, *, instance: int, name: str) -> dict:
    return client.post("/devices", json={"device_instance": instance, "name": name}).json()


def _make_point(database, device_id: int, *, instance: int, name: str) -> dict:
    # Goes through Database.create_object directly rather than
    # POST /devices/{id}/objects -- this test file only needs a real
    # `objects` row to reference, and exercising the HTTP layer isn't the
    # point of a repository-level test.
    return database.create_object(device_id, {
        "object_type": "analog-input",
        "object_instance": instance,
        "name": name,
        "units": "degrees-celsius",
        "behavior": "constant",
        "behavior_params": '{"value":0}',
        "enabled": 1,
        "number_of_states": 2,
        "reliability": "no-fault-detected",
        "polarity": "normal",
        "point_type": None,
    })


def test_create_replay_recording_starts_immediately_and_snapshots_selected_points(client, database):
    device = _make_device(client, instance=9701, name="Replay Persistence A")
    p1 = _make_point(database, device["id"], instance=0, name="Zone Temp")
    p2 = _make_point(database, device["id"], instance=1, name="Airflow")

    recording = database.create_replay_recording(device["id"], {
        "name": "Test Recording",
        "description": "for persistence test",
        "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 100,
        "buffer_mode": "overwrite",
    })

    assert recording["status"] == "recording"
    assert recording["ended_at"] is None
    assert recording["point_count"] == 2
    assert recording["sample_count"] == 0
    assert {p["object_name"] for p in recording["points"]} == {"Zone Temp", "Airflow"}


def test_create_replay_recording_all_points_when_point_ids_omitted(client, database):
    device = _make_device(client, instance=9702, name="Replay Persistence B")
    _make_point(database, device["id"], instance=0, name="Zone Temp")
    _make_point(database, device["id"], instance=1, name="Airflow")

    recording = database.create_replay_recording(device["id"], {
        "name": "All Points Recording",
        "sample_interval_seconds": 5.0,
        "maximum_samples": 100,
        "buffer_mode": "stop",
    })

    assert recording["point_count"] == 2


def test_stop_replay_recording_sets_completed_and_ended_at(client, database):
    device = _make_device(client, instance=9703, name="Replay Persistence C")
    _make_point(database, device["id"], instance=0, name="Zone Temp")

    recording = database.create_replay_recording(device["id"], {
        "name": "Stoppable",
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "stop",
    })

    stopped = database.stop_replay_recording(recording["id"])

    assert stopped["status"] == "completed"
    assert stopped["ended_at"] is not None


def test_delete_replay_recording_cascades_points_and_samples(client, database):
    device = _make_device(client, instance=9704, name="Replay Persistence D")
    p1 = _make_point(database, device["id"], instance=0, name="Zone Temp")

    recording = database.create_replay_recording(device["id"], {
        "name": "Deletable",
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "stop",
    })
    point_id = recording["points"][0]["id"]
    database.add_replay_sample(recording["id"], {point_id: {"value": 21.5}})

    assert database.delete_replay_recording(recording["id"]) is True
    assert database.get_replay_recording(recording["id"]) is None
