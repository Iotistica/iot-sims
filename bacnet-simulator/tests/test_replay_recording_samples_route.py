"""GET /recordings/{id}/samples -- CSV export of a recording's raw
samples, pivoted wide (checking a randomized-Behavior point actually
varied sample to sample), distinct from the calibration flow's own
long-format bulk read / narrower mapping-driven CSV."""
from __future__ import annotations

import csv
import io


def _make_device(client, *, instance: int, name: str) -> dict:
    return client.post("/devices", json={"protocol": "bacnet", "device_instance": instance, "name": name}).json()


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


def test_samples_export_404_for_unknown_recording(client):
    res = client.get("/recordings/999999/samples")
    assert res.status_code == 404


def test_samples_export_pivots_wide_ordered_with_headers_and_attachment(client, database):
    device = _make_device(client, instance=9960, name="Samples Route Device")
    p1 = _make_point(database, device["id"], instance=0, name="Supply-Air-Temp")
    p2 = _make_point(database, device["id"], instance=1, name="Fan-Command")

    recording = database.create_recording(device["id"], {
        "name": "Samples Route Test", "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    point_ids = {p["object_name"]: p["id"] for p in recording["points"]}

    database.add_recording_sample(recording["id"], {
        point_ids["Supply-Air-Temp"]: {"value": 12.08},
        point_ids["Fan-Command"]: {"value": 80.0},
    })
    database.add_recording_sample(recording["id"], {
        point_ids["Supply-Air-Temp"]: {"value": 13.94},
        point_ids["Fan-Command"]: {"value": 80.0},
    })

    res = client.get(f"/recordings/{recording['id']}/samples")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="Samples_Route_Test.csv"' == res.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(res.text)))
    header, *data_rows = rows
    assert header == [
        "timestamp", "Condition", "Fault Type", "Tags",
        "Supply-Air-Temp (degrees-celsius)", "Fan-Command (degrees-celsius)",
    ]
    assert len(data_rows) == 2
    # Unlabeled recording -- Condition/Fault Type/Tags are blank, not omitted.
    assert data_rows[0][1:4] == ["", "", ""]
    assert data_rows[0][4:] == ["12.08", "80.0"]
    assert data_rows[1][4:] == ["13.94", "80.0"]  # genuinely varied, not stuck at one value


def test_samples_export_includes_condition_fault_type_and_tags_on_every_row(client, database):
    """The exact gap this test guards against: a recording made under Condition=Fault,
    Fault Type=Sensor Bias must carry that label in its raw export -- otherwise a human
    downloading/archiving/sharing the CSV has no way to tell what it represents."""
    device = _make_device(client, instance=9962, name="Labeled Samples Device")
    p1 = _make_point(database, device["id"], instance=0, name="Zone-Temp")
    recording = database.create_recording(device["id"], {
        "name": "Labeled Test", "point_ids": [p1["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
        "condition": "fault", "fault_type": "sensor_bias", "tags": ["hot-day", "fan-test"],
    })
    point_ids = {p["object_name"]: p["id"] for p in recording["points"]}
    database.add_recording_sample(recording["id"], {point_ids["Zone-Temp"]: {"value": 27.0}})

    res = client.get(f"/recordings/{recording['id']}/samples")
    rows = list(csv.reader(io.StringIO(res.text)))
    header, *data_rows = rows
    assert header[:4] == ["timestamp", "Condition", "Fault Type", "Tags"]
    assert data_rows[0][1:3] == ["fault", "sensor_bias"]
    assert set(data_rows[0][3].split("; ")) == {"hot-day", "fan-test"}


def test_samples_export_missing_sample_for_a_point_is_a_blank_cell(client, database):
    device = _make_device(client, instance=9961, name="Sparse Device")
    p1 = _make_point(database, device["id"], instance=0, name="Point A")
    p2 = _make_point(database, device["id"], instance=1, name="Point B")
    recording = database.create_recording(device["id"], {
        "name": "Sparse Test", "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    point_ids = {p["object_name"]: p["id"] for p in recording["points"]}
    # Only Point A gets a value this cycle -- Point B has no sample at all
    # for sample_index 0.
    database.add_recording_sample(recording["id"], {point_ids["Point A"]: {"value": 1.0}})

    res = client.get(f"/recordings/{recording['id']}/samples")
    rows = list(csv.reader(io.StringIO(res.text)))

    assert rows[1] == [rows[1][0], "", "", "", "1.0", ""]
