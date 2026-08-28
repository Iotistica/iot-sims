"""Database.get_replay_recording_all_samples -- the long-format bulk read
used by the calibration dataset builder (see calibration_export.py).
Covers grouping/ordering/decoding only; get_replay_recording_samples
(per-index) and the write path (add_replay_sample) are already covered
elsewhere."""
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


def test_all_samples_grouped_ordered_and_decoded(client, database):
    device = _make_device(client, instance=9901, name="All Samples Device")
    p1 = _make_point(database, device["id"], instance=0, name="Zone Temp")
    p2 = _make_point(database, device["id"], instance=1, name="Airflow")

    recording = database.create_replay_recording(device["id"], {
        "name": "All Samples Test",
        "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 100,
        "buffer_mode": "overwrite",
    })
    point_ids = {p["object_name"]: p["id"] for p in recording["points"]}

    database.add_replay_sample(recording["id"], {
        point_ids["Zone Temp"]: {"value": 21.0},
        point_ids["Airflow"]: {"value": 1.5},
    })
    database.add_replay_sample(recording["id"], {
        point_ids["Zone Temp"]: {"value": 21.5},
        point_ids["Airflow"]: {"value": 1.6},
    })

    rows = database.get_replay_recording_all_samples(recording["id"])

    assert len(rows) == 4
    assert [r["sample_index"] for r in rows] == [0, 0, 1, 1]
    # Decoded back to real types, not the raw JSON-text column value.
    assert {r["value"] for r in rows} == {21.0, 1.5, 21.5, 1.6}
    # Object identity carried through the join, not just the point id.
    zone_temp_rows = [r for r in rows if r["recording_point_id"] == point_ids["Zone Temp"]]
    assert all(r["object_name"] == "Zone Temp" for r in zone_temp_rows)
    assert all(r["units"] == "degrees-celsius" for r in zone_temp_rows)
