"""Database.has_replayable_recording -- backs "Replay availability is based
on whether the source device has at least one completed, non-empty
recording" in the Create Simulation dialog."""
from __future__ import annotations


def _make_device(client, *, instance: int, name: str) -> dict:
    return client.post("/devices", json={"device_instance": instance, "name": name}).json()


def _make_point(database, device_id: int, *, instance: int, name: str) -> dict:
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


def test_no_recordings_means_not_available(client, database):
    device = _make_device(client, instance=9601, name="Availability None")
    assert database.has_replayable_recording(device["id"]) is False


def test_still_recording_is_not_available(client, database):
    device = _make_device(client, instance=9602, name="Availability InProgress")
    p1 = _make_point(database, device["id"], instance=0, name="A")
    recording = database.create_replay_recording(device["id"], {
        "name": "In Progress",
        "point_ids": [p1["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "stop",
    })
    database.add_replay_sample(recording["id"], {recording["points"][0]["id"]: {"value": 1}})

    assert database.has_replayable_recording(device["id"]) is False


def test_completed_but_empty_is_not_available(client, database):
    device = _make_device(client, instance=9603, name="Availability EmptyCompleted")
    p1 = _make_point(database, device["id"], instance=0, name="A")
    recording = database.create_replay_recording(device["id"], {
        "name": "Empty",
        "point_ids": [p1["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "stop",
    })
    database.stop_replay_recording(recording["id"])

    assert database.has_replayable_recording(device["id"]) is False


def test_completed_and_non_empty_is_available(client, database):
    device = _make_device(client, instance=9604, name="Availability Ready")
    p1 = _make_point(database, device["id"], instance=0, name="A")
    recording = database.create_replay_recording(device["id"], {
        "name": "Ready",
        "point_ids": [p1["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "stop",
    })
    database.add_replay_sample(recording["id"], {recording["points"][0]["id"]: {"value": 1}})
    database.stop_replay_recording(recording["id"])

    assert database.has_replayable_recording(device["id"]) is True
