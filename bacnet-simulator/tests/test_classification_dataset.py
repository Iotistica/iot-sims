"""build_classification_dataset / recording_class_distribution -- the classifier counterpart to
build_training_dataset (test_training_export.py), sibling module-level tests following the exact
same fixture pattern: a recording is one trajectory_id, samples come from
database.add_recording_sample."""
from __future__ import annotations

import csv
import io

from src.simulation.training_export import build_classification_dataset, recording_class_distribution


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


def _make_recording(database, device_id, point_id, *, name, condition, fault_type=None, values):
    rec = database.create_recording(device_id, {
        "name": name, "point_ids": [point_id],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
        "condition": condition, "fault_type": fault_type,
    })
    rp = {p["object_name"]: p["id"] for p in rec["points"]}
    point_name = next(iter(rp))
    for value in values:
        database.add_recording_sample(rec["id"], {rp[point_name]: {"value": value}})
    return rec


def test_label_broadcast_per_trajectory_and_ids_preserved(client, database):
    dev = _make_device(client, instance=9930, name="Classifier Export Device")
    point = _make_point(database, dev["id"], instance=0, name="Zone Temp")

    rec1 = _make_recording(
        database, dev["id"], point["id"], name="Normal Run", condition="normal", values=[21.0, 21.2],
    )
    rec2 = _make_recording(
        database, dev["id"], point["id"], name="Fault Run", condition="fault",
        fault_type="sensor_bias", values=[27.0],
    )

    mapping = {"input_zone_temp_c": point["id"]}
    csv_bytes = build_classification_dataset(database, [rec1["id"], rec2["id"]], mapping, "condition")
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows

    assert header == ["trajectory_id", "step_index", "sim_time_s", "input_zone_temp_c", "label"]
    assert [row[0] for row in data_rows] == [str(rec1["id"]), str(rec1["id"]), str(rec2["id"])]
    assert [row[1] for row in data_rows] == ["0", "1", "0"]
    # Label is the recording's own condition, broadcast across every row.
    assert [row[-1] for row in data_rows] == ["normal", "normal", "fault"]


def test_normal_recording_contributes_a_synthetic_normal_label_for_fault_type_target(client, database):
    """The exact gap reported live: a Normal recording never has fault_type set (nothing to
    classify at labeling time for Normal), so without this it could never be selected when
    training a fault-type classifier -- making it impossible to mix e.g. 1 Normal + 1 Fault
    recording. A Normal recording must contribute the synthetic "normal" label instead of being
    silently excluded."""
    dev = _make_device(client, instance=9935, name="Mixed Normal Fault Device")
    point = _make_point(database, dev["id"], instance=0, name="Zone Temp")

    normal_rec = _make_recording(
        database, dev["id"], point["id"], name="Normal Run", condition="normal", values=[21.0, 21.1],
    )
    fault_rec = _make_recording(
        database, dev["id"], point["id"], name="Fault Run",
        condition="fault", fault_type="sensor_stuck", values=[27.0],
    )

    dist = recording_class_distribution(database, [normal_rec["id"], fault_rec["id"]], "fault_type")
    assert dist == {
        "normal": {"trajectories": 1, "samples": 2},
        "sensor_stuck": {"trajectories": 1, "samples": 1},
    }

    mapping = {"input_zone_temp_c": point["id"]}
    csv_bytes = build_classification_dataset(
        database, [normal_rec["id"], fault_rec["id"]], mapping, "fault_type",
    )
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows
    assert header[-1] == "label"
    labels_by_trajectory = {row[0]: row[-1] for row in data_rows}
    assert labels_by_trajectory[str(normal_rec["id"])] == "normal"
    assert labels_by_trajectory[str(fault_rec["id"])] == "sensor_stuck"
    # recordings.fault_type itself is untouched -- still None for the Normal recording, never
    # backfilled with the synthetic label.
    assert database.get_recording(normal_rec["id"])["fault_type"] is None


def test_fault_type_label_used_when_that_is_the_chosen_target(client, database):
    dev = _make_device(client, instance=9931, name="Fault Type Export Device")
    point = _make_point(database, dev["id"], instance=0, name="Zone Temp")

    rec = _make_recording(
        database, dev["id"], point["id"], name="Stuck Sensor Run",
        condition="fault", fault_type="sensor_stuck", values=[21.0],
    )

    mapping = {"input_zone_temp_c": point["id"]}
    csv_bytes = build_classification_dataset(database, [rec["id"], rec["id"]], mapping, "fault_type")
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows

    assert header[-1] == "label"
    assert all(row[-1] == "sensor_stuck" for row in data_rows)


def test_recording_with_null_target_value_is_excluded(client, database):
    """A recording with no condition set at all (condition=None) contributes
    NO rows when classifying by condition -- never silently defaulted to
    some placeholder label."""
    dev = _make_device(client, instance=9932, name="Unlabeled Export Device")
    point = _make_point(database, dev["id"], instance=0, name="Zone Temp")

    labeled = _make_recording(
        database, dev["id"], point["id"], name="Labeled", condition="normal", values=[21.0],
    )
    unlabeled = _make_recording(
        database, dev["id"], point["id"], name="Unlabeled", condition=None, values=[99.0],
    )

    mapping = {"input_zone_temp_c": point["id"]}
    csv_bytes = build_classification_dataset(
        database, [labeled["id"], unlabeled["id"]], mapping, "condition",
    )
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows

    assert len(data_rows) == 1
    assert data_rows[0][0] == str(labeled["id"])
    assert "99.0" not in csv_bytes.decode("utf-8")


def test_class_distribution_counts_recordings_and_samples_separately(client, database):
    dev = _make_device(client, instance=9933, name="Distribution Device")
    point = _make_point(database, dev["id"], instance=0, name="Zone Temp")

    rec1 = _make_recording(
        database, dev["id"], point["id"], name="Normal 1", condition="normal", values=[21.0] * 50,
    )
    rec2 = _make_recording(
        database, dev["id"], point["id"], name="Normal 2", condition="normal", values=[21.0] * 30,
    )
    rec3 = _make_recording(
        database, dev["id"], point["id"], name="Fault 1", condition="fault", values=[27.0] * 5,
    )
    unlabeled = _make_recording(
        database, dev["id"], point["id"], name="Unlabeled", condition=None, values=[0.0] * 10,
    )

    dist = recording_class_distribution(
        database, [rec1["id"], rec2["id"], rec3["id"], unlabeled["id"]], "condition",
    )
    assert dist["normal"] == {"trajectories": 2, "samples": 80}
    assert dist["fault"] == {"trajectories": 1, "samples": 5}
    assert "None" not in dist and len(dist) == 2


def test_recording_created_via_real_http_request_flows_correctly_into_training(client, database):
    """End-to-end verification that a recording created the exact way RecordingsDrawer.vue's
    save() actually does it (POST /devices/{id}/recordings with condition/fault_type/tags
    already set, before Start Recording is even clicked -- see the real UI flow) is picked up
    correctly by BOTH training functions, not just the direct-DB-call fixtures the other tests
    in this file use. Closes the loop: UI payload shape -> HTTP layer -> schema validation ->
    Database.create_recording -> recording_class_distribution/build_classification_dataset."""
    dev = _make_device(client, instance=9934, name="HTTP Verify Device")
    point = _make_point(database, dev["id"], instance=0, name="Zone Temp")

    created = client.post(f"/devices/{dev['id']}/recordings", json={
        "name": "RTU-1 check file", "condition": "fault", "fault_type": "sensor_bias",
        "tags": ["commissioning"],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "stop",
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["condition"] == "fault"
    assert body["fault_type"] == "sensor_bias"
    assert body["tags"] == ["commissioning"]

    recording_id = body["id"]
    rp = {p["object_name"]: p["id"] for p in database.get_recording(recording_id)["points"]}
    database.add_recording_sample(recording_id, {rp["Zone Temp"]: {"value": 13.26}})
    database.add_recording_sample(recording_id, {rp["Zone Temp"]: {"value": 13.25}})

    # recording_class_distribution sees exactly the condition/fault_type set via the real POST.
    dist = recording_class_distribution(database, [recording_id], "condition")
    assert dist == {"fault": {"trajectories": 1, "samples": 2}}
    dist_by_fault = recording_class_distribution(database, [recording_id], "fault_type")
    assert dist_by_fault == {"sensor_bias": {"trajectories": 1, "samples": 2}}

    # build_classification_dataset's own `label` column carries the same value through.
    mapping = {"input_zone_temp_c": point["id"]}
    csv_bytes = build_classification_dataset(database, [recording_id], mapping, "condition")
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows
    assert header[-1] == "label"
    assert all(row[-1] == "fault" for row in data_rows)
    assert len(data_rows) == 2
