"""Database.add_replay_sample -- maximum_samples/buffer_mode semantics.
"Maximum Samples refers to complete device snapshots, not individual
point-value rows": eviction/stop both operate on whole sample_index values,
never partial rows within a snapshot."""
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


def test_overwrite_oldest_evicts_whole_sample_index_at_cap(client, database):
    device = _make_device(client, instance=9901, name="Buffer Overwrite")
    p1 = _make_point(database, device["id"], instance=0, name="A")
    p2 = _make_point(database, device["id"], instance=1, name="B")
    recording = database.create_replay_recording(device["id"], {
        "name": "Overwrite Test",
        "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 3,
        "buffer_mode": "overwrite",
    })
    ids = [p["id"] for p in recording["points"]]

    for value in range(4):
        database.add_replay_sample(recording["id"], {
            ids[0]: {"value": value}, ids[1]: {"value": value * 10},
        })

    updated = database.get_replay_recording(recording["id"])
    assert updated["sample_count"] == 3
    assert updated["status"] == "recording"  # overwrite never auto-stops

    remaining_indexes = sorted({
        r["sample_index"]
        for r in database.get_replay_recording_samples(recording["id"], 1)
        + database.get_replay_recording_samples(recording["id"], 2)
        + database.get_replay_recording_samples(recording["id"], 3)
    })
    assert remaining_indexes == [1, 2, 3]
    # sample_index=0's rows are gone entirely -- both points, not just one.
    assert database.get_replay_recording_samples(recording["id"], 0) == []


def test_stop_mode_halts_at_cap_and_drops_the_overflow_sample(client, database):
    device = _make_device(client, instance=9902, name="Buffer Stop")
    p1 = _make_point(database, device["id"], instance=0, name="A")
    recording = database.create_replay_recording(device["id"], {
        "name": "Stop Test",
        "point_ids": [p1["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 2,
        "buffer_mode": "stop",
    })
    point_id = recording["points"][0]["id"]

    i0 = database.add_replay_sample(recording["id"], {point_id: {"value": 1}})
    i1 = database.add_replay_sample(recording["id"], {point_id: {"value": 2}})
    overflow = database.add_replay_sample(recording["id"], {point_id: {"value": 3}})

    assert (i0, i1) == (0, 1)
    assert overflow is None

    updated = database.get_replay_recording(recording["id"])
    assert updated["status"] == "completed"
    assert updated["sample_count"] == 2


def test_lowering_maximum_samples_trims_immediately_in_overwrite_mode(client, database):
    device = _make_device(client, instance=9904, name="Buffer Lower Cap Overwrite")
    p1 = _make_point(database, device["id"], instance=0, name="A")
    recording = database.create_replay_recording(device["id"], {
        "name": "Shrinking Cap",
        "point_ids": [p1["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 100,
        "buffer_mode": "overwrite",
    })
    point_id = recording["points"][0]["id"]
    for value in range(5):
        database.add_replay_sample(recording["id"], {point_id: {"value": value}})
    assert database.get_replay_recording(recording["id"])["sample_count"] == 5

    updated = database.update_replay_recording(recording["id"], {
        "name": "Shrinking Cap", "description": "",
        "sample_interval_seconds": 5.0, "maximum_samples": 2, "buffer_mode": "overwrite",
    })

    assert updated["sample_count"] == 2
    assert updated["status"] == "recording"
    remaining = sorted({
        r["sample_index"]
        for idx in range(5)
        for r in database.get_replay_recording_samples(recording["id"], idx)
    })
    assert remaining == [3, 4]  # oldest (0,1,2) trimmed, newest 2 kept


def test_lowering_maximum_samples_at_or_below_count_completes_stop_mode_immediately(client, database):
    device = _make_device(client, instance=9905, name="Buffer Lower Cap Stop")
    p1 = _make_point(database, device["id"], instance=0, name="A")
    recording = database.create_replay_recording(device["id"], {
        "name": "Shrinking Cap Stop",
        "point_ids": [p1["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 100,
        "buffer_mode": "stop",
    })
    point_id = recording["points"][0]["id"]
    for value in range(5):
        database.add_replay_sample(recording["id"], {point_id: {"value": value}})

    updated = database.update_replay_recording(recording["id"], {
        "name": "Shrinking Cap Stop", "description": "",
        "sample_interval_seconds": 5.0, "maximum_samples": 3, "buffer_mode": "stop",
    })

    assert updated["status"] == "completed"
    assert updated["ended_at"] is not None
    assert updated["sample_count"] == 5  # stop mode never deletes existing samples, just halts


def test_add_sample_on_already_completed_recording_is_a_no_op(client, database):
    device = _make_device(client, instance=9903, name="Buffer Completed")
    p1 = _make_point(database, device["id"], instance=0, name="A")
    recording = database.create_replay_recording(device["id"], {
        "name": "Already Done",
        "point_ids": [p1["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "stop",
    })
    database.stop_replay_recording(recording["id"])
    point_id = recording["points"][0]["id"]

    result = database.add_replay_sample(recording["id"], {point_id: {"value": 1}})

    assert result is None
    assert database.get_replay_recording(recording["id"])["sample_count"] == 0
