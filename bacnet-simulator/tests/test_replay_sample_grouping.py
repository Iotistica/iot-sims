"""Database.add_replay_sample -- "all point values captured during the same
polling cycle use the same sample_index and timestamp" (the core grouping
requirement distinguishing a Replay Recording snapshot from independent
per-point trend-log-style rows)."""
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


def _make_recording(client, database, device_id: int, point_ids: list[int]) -> dict:
    return database.create_replay_recording(device_id, {
        "name": "Grouping Test",
        "point_ids": point_ids,
        "sample_interval_seconds": 5.0,
        "maximum_samples": 100,
        "buffer_mode": "overwrite",
    })


def test_one_polling_cycle_shares_sample_index_and_timestamp(client, database):
    device = _make_device(client, instance=9801, name="Grouping A")
    p1 = _make_point(database, device["id"], instance=0, name="Zone Temperature")
    p2 = _make_point(database, device["id"], instance=1, name="Airflow")
    p3 = _make_point(database, device["id"], instance=2, name="Damper Position")

    recording = _make_recording(client, database, device["id"], [p1["id"], p2["id"], p3["id"]])
    ids = {p["object_name"]: p["id"] for p in recording["points"]}

    index = database.add_replay_sample(recording["id"], {
        ids["Zone Temperature"]: {"value": 22.5},
        ids["Airflow"]: {"value": 425},
        ids["Damper Position"]: {"value": 0.47},
    })

    rows = database.get_replay_recording_samples(recording["id"], index)
    assert len(rows) == 3
    assert len({r["sample_index"] for r in rows}) == 1
    assert len({r["timestamp"] for r in rows}) == 1


def test_successive_polling_cycles_get_increasing_sample_index(client, database):
    device = _make_device(client, instance=9802, name="Grouping B")
    p1 = _make_point(database, device["id"], instance=0, name="Zone Temperature")
    recording = _make_recording(client, database, device["id"], [p1["id"]])
    point_id = recording["points"][0]["id"]

    i0 = database.add_replay_sample(recording["id"], {point_id: {"value": 20.0}})
    i1 = database.add_replay_sample(recording["id"], {point_id: {"value": 20.5}})
    i2 = database.add_replay_sample(recording["id"], {point_id: {"value": 21.0}})

    assert (i0, i1, i2) == (0, 1, 2)
