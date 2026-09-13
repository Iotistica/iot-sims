"""build_training_dataset -- combines one or more recordings' long-format
samples into the wide, multi-trajectory CSV iot-models' training-job API
expects (trajectory_id/step_index/sim_time_s/<mapped columns>, one
recording = one trajectory_id)."""
from __future__ import annotations

import csv
import io

from src.simulation.training_export import build_training_dataset


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


def test_combines_two_recordings_of_the_same_device_into_one_csv_with_distinct_trajectory_ids(client, database):
    """The realistic case: two separate recording sessions of the SAME
    device (e.g. two different scripted scenario runs) -- both share the
    same source_object_ids, so one mapping (keyed by source_object_id)
    correctly resolves against each recording's own points independently,
    and both contribute their own rows under their own trajectory_id."""
    dev = _make_device(client, instance=9920, name="Training Export Device")
    p1 = _make_point(database, dev["id"], instance=0, name="Outdoor Air Temp")
    p2 = _make_point(database, dev["id"], instance=1, name="Supply Air Temp")

    rec1 = database.create_recording(dev["id"], {
        "name": "Scenario A", "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp1 = {p["object_name"]: p["id"] for p in rec1["points"]}
    database.add_recording_sample(rec1["id"], {rp1["Outdoor Air Temp"]: {"value": 10.0}, rp1["Supply Air Temp"]: {"value": 18.0}})
    database.add_recording_sample(rec1["id"], {rp1["Outdoor Air Temp"]: {"value": 11.0}, rp1["Supply Air Temp"]: {"value": 18.5}})

    rec2 = database.create_recording(dev["id"], {
        "name": "Scenario B", "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 10.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp2 = {p["object_name"]: p["id"] for p in rec2["points"]}
    database.add_recording_sample(rec2["id"], {rp2["Outdoor Air Temp"]: {"value": 20.0}, rp2["Supply Air Temp"]: {"value": 22.0}})

    mapping = {"input_outdoor_air_temp_c": p1["id"], "output_supply_air_temp_c": p2["id"]}
    csv_bytes = build_training_dataset(database, [rec1["id"], rec2["id"]], mapping)
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows

    assert header == ["trajectory_id", "step_index", "sim_time_s", "input_outdoor_air_temp_c", "output_supply_air_temp_c"]
    assert [row[0] for row in data_rows] == [str(rec1["id"]), str(rec1["id"]), str(rec2["id"])]
    assert [row[1] for row in data_rows] == ["0", "1", "0"]
    assert [row[2] for row in data_rows] == ["0.0", "5.0", "0.0"]
    assert data_rows[0][3:] == ["10.0", "18.0"]
    assert data_rows[1][3:] == ["11.0", "18.5"]


def test_constant_mapping_writes_a_repeated_value_column_per_recording(client, database):
    """A constant-sourced input (no point at all) still gets a real
    input_* column -- filled with whatever value THAT recording itself
    snapshotted, repeated on every one of its own rows, distinct from
    another recording's own (possibly different) snapshotted value."""
    dev = _make_device(client, instance=9921, name="Constant Export Device")
    p2 = _make_point(database, dev["id"], instance=1, name="Supply Air Temp")

    rec1 = database.create_recording(dev["id"], {
        "name": "Scenario A", "point_ids": [p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp1 = {p["object_name"]: p["id"] for p in rec1["points"]}
    database.add_recording_sample(rec1["id"], {rp1["Supply Air Temp"]: {"value": 18.0}})
    database.add_recording_sample(rec1["id"], {rp1["Supply Air Temp"]: {"value": 18.5}})
    database.add_recording_input_constants(rec1["id"], {"outdoor_air_temp_c": 30.0})

    rec2 = database.create_recording(dev["id"], {
        "name": "Scenario B", "point_ids": [p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp2 = {p["object_name"]: p["id"] for p in rec2["points"]}
    database.add_recording_sample(rec2["id"], {rp2["Supply Air Temp"]: {"value": 22.0}})
    database.add_recording_input_constants(rec2["id"], {"outdoor_air_temp_c": 20.0})

    mapping = {"output_supply_air_temp_c": p2["id"]}
    constant_mapping = {"input_outdoor_air_temp_c": {rec1["id"]: 30.0, rec2["id"]: 20.0}}
    csv_bytes = build_training_dataset(database, [rec1["id"], rec2["id"]], mapping, constant_mapping)
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows

    # mapping's own keys come first, constant_mapping's fill in after --
    # see build_training_dataset's own variable_names construction.
    assert header == ["trajectory_id", "step_index", "sim_time_s", "output_supply_air_temp_c", "input_outdoor_air_temp_c"]
    const_col = header.index("input_outdoor_air_temp_c")
    # rec1's rows carry rec1's own snapshot (30.0), rec2's carry rec2's own
    # (20.0) -- never mixed, never today's live device value.
    assert [row[const_col] for row in data_rows if row[0] == str(rec1["id"])] == ["30.0", "30.0"]
    assert [row[const_col] for row in data_rows if row[0] == str(rec2["id"])] == ["20.0"]


def test_recording_missing_its_constant_snapshot_contributes_no_rows(client, database):
    """rec2 never snapshotted this variable (predates the feature, or it
    wasn't constant-sourced at record time) -- dropped entirely, same rule
    a recording missing a mapped POINT already gets, not silently given
    rec1's value or a blank."""
    dev = _make_device(client, instance=9922, name="Missing Constant Device")
    p2 = _make_point(database, dev["id"], instance=1, name="Supply Air Temp")

    rec1 = database.create_recording(dev["id"], {
        "name": "Scenario A", "point_ids": [p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp1 = {p["object_name"]: p["id"] for p in rec1["points"]}
    database.add_recording_sample(rec1["id"], {rp1["Supply Air Temp"]: {"value": 18.0}})
    database.add_recording_input_constants(rec1["id"], {"outdoor_air_temp_c": 30.0})

    rec2 = database.create_recording(dev["id"], {
        "name": "Scenario B (pre-migration)", "point_ids": [p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp2 = {p["object_name"]: p["id"] for p in rec2["points"]}
    database.add_recording_sample(rec2["id"], {rp2["Supply Air Temp"]: {"value": 22.0}})
    # rec2 deliberately gets no add_recording_input_constants call.

    mapping = {"output_supply_air_temp_c": p2["id"]}
    constant_mapping = {"input_outdoor_air_temp_c": {rec1["id"]: 30.0}}  # no entry for rec2
    csv_bytes = build_training_dataset(database, [rec1["id"], rec2["id"]], mapping, constant_mapping)
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    _header, *data_rows = rows

    assert [row[0] for row in data_rows] == [str(rec1["id"])]


def test_recording_missing_a_mapped_variable_contributes_no_rows(client, database):
    dev1 = _make_device(client, instance=9922, name="Full Mapping")
    p1 = _make_point(database, dev1["id"], instance=0, name="Outdoor Air Temp")
    p2 = _make_point(database, dev1["id"], instance=1, name="Supply Air Temp")
    rec_full = database.create_recording(dev1["id"], {
        "name": "Full", "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp_full = {p["object_name"]: p["id"] for p in rec_full["points"]}
    database.add_recording_sample(rec_full["id"], {rp_full["Outdoor Air Temp"]: {"value": 10.0}, rp_full["Supply Air Temp"]: {"value": 18.0}})

    dev2 = _make_device(client, instance=9923, name="Partial Mapping")
    p3 = _make_point(database, dev2["id"], instance=0, name="Outdoor Air Temp")
    # Note: no "Supply Air Temp" point recorded on this second device at all.
    rec_partial = database.create_recording(dev2["id"], {
        "name": "Partial", "point_ids": [p3["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp_partial = {p["object_name"]: p["id"] for p in rec_partial["points"]}
    database.add_recording_sample(rec_partial["id"], {rp_partial["Outdoor Air Temp"]: {"value": 99.0}})

    mapping = {"input_outdoor_air_temp_c": p1["id"], "output_supply_air_temp_c": p2["id"]}
    csv_bytes = build_training_dataset(database, [rec_full["id"], rec_partial["id"]], mapping)
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows
    # rec_partial has no point for output_supply_air_temp_c's source_object_id
    # (p2["id"]) at all -- contributes zero rows, only rec_full appears.
    assert [row[0] for row in data_rows] == [str(rec_full["id"])]


def test_single_recording_missing_one_sample_drops_only_that_row(client, database):
    dev = _make_device(client, instance=9924, name="Sparse Sample")
    p1 = _make_point(database, dev["id"], instance=0, name="Outdoor Air Temp")
    p2 = _make_point(database, dev["id"], instance=1, name="Supply Air Temp")
    recording = database.create_recording(dev["id"], {
        "name": "Sparse", "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp = {p["object_name"]: p["id"] for p in recording["points"]}
    # sample_index 0: both points present.
    database.add_recording_sample(recording["id"], {rp["Outdoor Air Temp"]: {"value": 10.0}, rp["Supply Air Temp"]: {"value": 18.0}})
    # sample_index 1: only one point sampled this cycle (e.g. a read
    # failure for the other point) -- add_recording_sample's own `values`
    # dict simply omits the missing point rather than writing a null.
    database.add_recording_sample(recording["id"], {rp["Outdoor Air Temp"]: {"value": 11.0}})

    mapping = {"input_outdoor_air_temp_c": p1["id"], "output_supply_air_temp_c": p2["id"]}
    csv_bytes = build_training_dataset(database, [recording["id"]], mapping)
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows
    assert len(data_rows) == 1
    assert data_rows[0][1] == "0"
    assert data_rows[0][3:] == ["10.0", "18.0"]


def test_sim_time_s_derived_from_sample_index_times_interval_not_real_timestamp(client, database):
    dev = _make_device(client, instance=9925, name="Time Axis")
    p1 = _make_point(database, dev["id"], instance=0, name="Outdoor Air Temp")
    recording = database.create_recording(dev["id"], {
        "name": "Time", "point_ids": [p1["id"]],
        "sample_interval_seconds": 15.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp = {p["object_name"]: p["id"] for p in recording["points"]}
    for value in (1.0, 2.0, 3.0):
        database.add_recording_sample(recording["id"], {rp["Outdoor Air Temp"]: {"value": value}})

    mapping = {"input_outdoor_air_temp_c": p1["id"]}
    csv_bytes = build_training_dataset(database, [recording["id"]], mapping)
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    _, *data_rows = rows
    assert [row[2] for row in data_rows] == ["0.0", "15.0", "30.0"]


def test_recordings_with_different_tags_get_no_input_category_column(client, database):
    """Tags are metadata only -- recordings tagged with different scenario
    labels must NOT produce an input_category (or any other tag-derived)
    column. This retires the old single-`category` -> input_category
    training-feature injection outright rather than adapting it to tags;
    see training_export.py's own module docstring."""
    dev = _make_device(client, instance=9926, name="Tags No Feature Device")
    p1 = _make_point(database, dev["id"], instance=0, name="Outdoor Air Temp")

    rec1 = database.create_recording(dev["id"], {
        "name": "Run A", "point_ids": [p1["id"]], "tags": ["hot-day"],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp1 = {p["object_name"]: p["id"] for p in rec1["points"]}
    database.add_recording_sample(rec1["id"], {rp1["Outdoor Air Temp"]: {"value": 10.0}})

    rec2 = database.create_recording(dev["id"], {
        "name": "Run B", "point_ids": [p1["id"]], "tags": ["fan-test", "cooling"],
        "sample_interval_seconds": 5.0, "maximum_samples": 100, "buffer_mode": "overwrite",
    })
    rp2 = {p["object_name"]: p["id"] for p in rec2["points"]}
    database.add_recording_sample(rec2["id"], {rp2["Outdoor Air Temp"]: {"value": 11.0}})

    mapping = {"input_outdoor_air_temp_c": p1["id"]}
    csv_bytes = build_training_dataset(database, [rec1["id"], rec2["id"]], mapping)
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))
    header, *data_rows = rows

    assert header == ["trajectory_id", "step_index", "sim_time_s", "input_outdoor_air_temp_c"]
    assert all(len(row) == len(header) for row in data_rows)
