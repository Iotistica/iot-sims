"""build_calibration_dataset -- pivots a recording's long-format samples
into the wide CSV iot-models' calibration API expects (one column per
mapped variable + a shared timestamp column, no unit conversion)."""
from __future__ import annotations

import csv
import io

from src.simulation.calibration_export import build_calibration_dataset


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


def _seeded_recording(client, database, *, instance: int):
    device = _make_device(client, instance=instance, name=f"Calib Export {instance}")
    p1 = _make_point(database, device["id"], instance=0, name="Outdoor Air Temp")
    p2 = _make_point(database, device["id"], instance=1, name="Supply Air Temp")

    recording = database.create_replay_recording(device["id"], {
        "name": "Export Test",
        "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 100,
        "buffer_mode": "overwrite",
    })
    point_ids = {p["object_name"]: p["id"] for p in recording["points"]}

    database.add_replay_sample(recording["id"], {
        point_ids["Outdoor Air Temp"]: {"value": 10.0},
        point_ids["Supply Air Temp"]: {"value": 18.0},
    })
    database.add_replay_sample(recording["id"], {
        point_ids["Outdoor Air Temp"]: {"value": 11.0},
        point_ids["Supply Air Temp"]: {"value": 18.5},
    })
    return recording, point_ids


def test_pivots_to_wide_csv_with_mapped_column_names(client, database):
    recording, point_ids = _seeded_recording(client, database, instance=9910)

    csv_bytes = build_calibration_dataset(database, recording["id"], {
        "outdoor_air_temp_c": point_ids["Outdoor Air Temp"],
        "supply_air_temp_c": point_ids["Supply Air Temp"],
    })

    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows

    assert header == ["timestamp", "outdoor_air_temp_c", "supply_air_temp_c"]
    assert len(data_rows) == 2
    assert data_rows[0][1:] == ["10.0", "18.0"]
    assert data_rows[1][1:] == ["11.0", "18.5"]
    # Real ISO-8601 timestamps, not blank/placeholder.
    assert all(row[0] for row in data_rows)


def test_column_order_follows_mapping_insertion_order(client, database):
    recording, point_ids = _seeded_recording(client, database, instance=9911)

    csv_bytes = build_calibration_dataset(database, recording["id"], {
        "supply_air_temp_c": point_ids["Supply Air Temp"],
        "outdoor_air_temp_c": point_ids["Outdoor Air Temp"],
    })

    header = next(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    assert header == ["timestamp", "supply_air_temp_c", "outdoor_air_temp_c"]


def test_unmapped_points_are_simply_excluded(client, database):
    recording, point_ids = _seeded_recording(client, database, instance=9912)

    csv_bytes = build_calibration_dataset(database, recording["id"], {
        "supply_air_temp_c": point_ids["Supply Air Temp"],
    })

    header = next(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    assert header == ["timestamp", "supply_air_temp_c"]
