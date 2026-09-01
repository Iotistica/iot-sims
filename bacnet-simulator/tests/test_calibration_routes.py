"""/calibration/* routes -- thin glue, so these tests monkeypatch the
relay functions (remote_calibration.py/remote_catalog.py) rather than
hitting a real iot-models server, and monkeypatch
suggest_mapping_for_variable itself to test only this router's own
recording-scoped filtering, not the scoring engine it reuses (already
covered by mapping/suggestions.py's own tests)."""
from __future__ import annotations

import src.api.routers.calibration as calibration_routes
from src.simulation.mapping.suggestions import MappingAlternative, MappingSuggestion


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


# ── /calibration/models ──────────────────────────────────────────────────

def test_list_models_relays_catalog(client, monkeypatch):
    canned = [{"id": "m1", "label": "Model One", "calibration_enabled": True}]
    monkeypatch.setattr(calibration_routes, "fetch_remote_catalog", lambda settings: canned)

    res = client.get("/calibration/models")

    assert res.status_code == 200
    assert res.json() == canned


def test_list_models_502s_on_relay_failure(client, monkeypatch):
    def _boom(settings):
        raise RuntimeError("FMU model runtime cannot be reached at http://localhost:8002")
    monkeypatch.setattr(calibration_routes, "fetch_remote_catalog", _boom)

    res = client.get("/calibration/models")

    assert res.status_code == 502
    assert "runtime_error" in res.json()["detail"]


# ── /calibration/recordings ──────────────────────────────────────────────

def test_list_recordings_only_completed_nonempty_across_devices(client, database):
    device_a = _make_device(client, instance=9920, name="Device A")
    device_b = _make_device(client, instance=9921, name="Device B")
    pa = _make_point(database, device_a["id"], instance=0, name="Point A")
    pb = _make_point(database, device_b["id"], instance=0, name="Point B")

    still_recording = database.create_replay_recording(device_a["id"], {
        "name": "Still Recording", "point_ids": [pa["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })

    completed_empty = database.create_replay_recording(device_a["id"], {
        "name": "Completed Empty", "point_ids": [pa["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    database.stop_replay_recording(completed_empty["id"])

    completed_with_data = database.create_replay_recording(device_b["id"], {
        "name": "Completed With Data", "point_ids": [pb["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    point_id = completed_with_data["points"][0]["id"]
    database.add_replay_sample(completed_with_data["id"], {point_id: {"value": 1.0}})
    database.stop_replay_recording(completed_with_data["id"])

    res = client.get("/calibration/recordings")
    assert res.status_code == 200
    names = {r["name"] for r in res.json()}

    assert names == {"Completed With Data"}
    only = res.json()[0]
    assert only["device_name"] == "Device B"
    assert only["sample_count"] == 1


def _completed_recording_with_data(client, database, *, instance: int, name: str, equipment_type: str | None = None) -> dict:
    device = client.post("/devices", json={
        "device_instance": instance, "name": name, "equipment_type": equipment_type,
    }).json()
    point = _make_point(database, device["id"], instance=0, name="Point")
    recording = database.create_replay_recording(device["id"], {
        "name": f"{name} Recording", "point_ids": [point["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    point_id = recording["points"][0]["id"]
    database.add_replay_sample(recording["id"], {point_id: {"value": 1.0}})
    database.stop_replay_recording(recording["id"])
    return recording


def test_list_recordings_filters_by_model_equipment_type(client, database, monkeypatch):
    rtu_recording = _completed_recording_with_data(
        client, database, instance=9940, name="Rooftop 1", equipment_type="Rooftop_Unit",
    )
    _completed_recording_with_data(
        client, database, instance=9941, name="Boiler Plant", equipment_type="Boiler",
    )

    monkeypatch.setattr(
        calibration_routes, "fetch_remote_metadata",
        lambda settings, mid: {
            "inputs": [{"name": "x", "mapping_hints": {"preferred_equipment_types": ["Rooftop_Unit"]}}],
            "outputs": [],
        },
    )

    res = client.get("/calibration/recordings", params={"model_id": "rtu-model"})

    assert res.status_code == 200
    names = {r["name"] for r in res.json()}
    assert names == {rtu_recording["name"]}


def test_list_recordings_falls_back_to_all_when_no_device_matches(client, database, monkeypatch):
    _completed_recording_with_data(
        client, database, instance=9942, name="Boiler Only", equipment_type="Boiler",
    )

    monkeypatch.setattr(
        calibration_routes, "fetch_remote_metadata",
        lambda settings, mid: {
            "inputs": [{"name": "x", "mapping_hints": {"preferred_equipment_types": ["Rooftop_Unit"]}}],
            "outputs": [],
        },
    )

    res = client.get("/calibration/recordings", params={"model_id": "rtu-model"})

    assert res.status_code == 200
    assert {r["name"] for r in res.json()} == {"Boiler Only Recording"}


def test_list_recordings_unfiltered_when_model_declares_no_equipment_hints(client, database, monkeypatch):
    _completed_recording_with_data(client, database, instance=9943, name="Anything")

    monkeypatch.setattr(
        calibration_routes, "fetch_remote_metadata",
        lambda settings, mid: {"inputs": [{"name": "x", "mapping_hints": {}}], "outputs": []},
    )

    res = client.get("/calibration/recordings", params={"model_id": "no-hints-model"})

    assert res.status_code == 200
    assert {r["name"] for r in res.json()} == {"Anything Recording"}


# ── /calibration/mapping-suggestions ─────────────────────────────────────

def _canned_metadata() -> dict:
    return {
        "id": "model-x",
        "inputs": [{"name": "outdoor_air_temp_c", "unit": "degC", "required": True}],
        "outputs": [{"name": "supply_air_temp_c", "unit": "degC"}],
        "calibration": {"enabled": True, "goal": {"output": "supply_air_temp_c"}, "tuners": []},
    }


def _seed_recording_with_two_of_three_points(client, database):
    # A throwaway device+recording created first deliberately desyncs the
    # objects.id and replay_recording_points.id autoincrement sequences --
    # in a fresh test DB they'd otherwise happen to start in lockstep (both
    # at 1), which would silently mask a regression back to conflating the
    # two id spaces (see the "no data rows" bug this test guards against).
    throwaway_device = _make_device(client, instance=9929, name="Throwaway Device")
    throwaway_point_a = _make_point(database, throwaway_device["id"], instance=0, name="Throwaway Point A")
    _make_point(database, throwaway_device["id"], instance=1, name="Throwaway Point B")  # objects.id consumed, not recorded
    database.create_replay_recording(throwaway_device["id"], {
        # Only one of the two throwaway objects is selected -- so
        # objects.id and replay_recording_points.id advance by different
        # amounts here, guaranteeing they're desynced for every point
        # created below.
        "name": "Throwaway Recording", "point_ids": [throwaway_point_a["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })

    device = _make_device(client, instance=9930, name="Mapping Device")
    p1 = _make_point(database, device["id"], instance=0, name="Recorded Point 1")
    p2 = _make_point(database, device["id"], instance=1, name="Recorded Point 2")
    _make_point(database, device["id"], instance=2, name="Unrecorded Point")  # not in recording

    recording = database.create_replay_recording(device["id"], {
        "name": "Mapping Test", "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    return recording, p1["id"], p2["id"]


def test_mapping_suggestions_404_for_unknown_recording(client, monkeypatch):
    monkeypatch.setattr(calibration_routes, "fetch_remote_metadata", lambda settings, mid: _canned_metadata())

    res = client.get("/calibration/mapping-suggestions", params={"recording_id": 999999, "model_id": "model-x"})

    assert res.status_code == 404


def test_mapping_suggestions_400_when_model_has_no_calibration_block(client, database, monkeypatch):
    recording, _, _ = _seed_recording_with_two_of_three_points(client, database)
    monkeypatch.setattr(
        calibration_routes, "fetch_remote_metadata",
        lambda settings, mid: {"id": "no-calib", "inputs": [], "outputs": []},
    )

    res = client.get(
        "/calibration/mapping-suggestions",
        params={"recording_id": recording["id"], "model_id": "no-calib"},
    )

    assert res.status_code == 400


def test_mapping_suggestions_falls_back_to_in_recording_alternative(client, database, monkeypatch):
    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    monkeypatch.setattr(calibration_routes, "fetch_remote_metadata", lambda settings, mid: _canned_metadata())

    # The (fake) scorer's top pick is a point that exists on the device but
    # was never included in this recording -- the route must not offer it,
    # and must fall back to the best alternative that IS in the recording.
    def _fake_suggest(variable, device_id, database_arg):
        return MappingSuggestion(
            variable=variable.name,
            direction=variable.direction,
            suggested_point_id=424242,  # not in the recording
            suggested_point_name="Unrecorded Point",
            confidence="high",
            score=0.9,
            reasons=["exact name match"],
            alternatives=[
                MappingAlternative(point_id=p1_id, point_name="Recorded Point 1", score=0.6, reasons=["unit match"]),
            ],
        )
    monkeypatch.setattr(calibration_routes, "suggest_mapping_for_variable", _fake_suggest)

    res = client.get(
        "/calibration/mapping-suggestions",
        params={"recording_id": recording["id"], "model_id": "model-x"},
    )

    assert res.status_code == 200
    body = res.json()
    goal_variable = next(v for v in body["variables"] if v["name"] == "supply_air_temp_c")
    # The response must speak in recording_point_id terms, not objects.id
    # (p1_id) -- see the module docstring on why these ids deliberately
    # diverge in this test's fixture.
    expected_recording_point_id = next(
        p["id"] for p in recording["points"] if p["source_object_id"] == p1_id
    )
    assert expected_recording_point_id != p1_id
    assert goal_variable["suggested_point_id"] == expected_recording_point_id
    assert goal_variable["confidence"] == "low"
    # Every offered candidate point actually belongs to this recording, and
    # is itself already in recording_point_id terms.
    offered_ids = {p["id"] for p in body["points"]}
    assert offered_ids == {p["id"] for p in recording["points"]}
    assert 424242 not in offered_ids
    # object_type is carried through -- the frontend needs it to steer a
    # unit:"boolean" variable's point picker toward binary points only.
    assert all(p["object_type"] == "analog-input" for p in body["points"])


def test_mapping_suggestions_points_and_dataset_use_matching_id_space(client, database, monkeypatch):
    """End-to-end regression for the id-space bug: the point id a mapping
    suggestion hands back must be exactly the id build_calibration_dataset
    needs (recording_point_id), or the produced CSV silently has zero data
    rows (every sample lookup misses)."""
    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    recording_point_id = next(p["id"] for p in recording["points"] if p["source_object_id"] == p1_id)
    database.add_replay_sample(recording["id"], {recording_point_id: {"value": 42.0}})

    monkeypatch.setattr(calibration_routes, "fetch_remote_metadata", lambda settings, mid: _canned_metadata())

    def _fake_suggest(variable, device_id, database_arg):
        return MappingSuggestion(
            variable=variable.name, direction=variable.direction,
            suggested_point_id=p1_id, suggested_point_name="Recorded Point 1",
            confidence="high", score=0.9, reasons=[],
        )
    monkeypatch.setattr(calibration_routes, "suggest_mapping_for_variable", _fake_suggest)

    body = client.get(
        "/calibration/mapping-suggestions",
        params={"recording_id": recording["id"], "model_id": "model-x"},
    ).json()
    goal_suggested_id = next(v for v in body["variables"] if v["name"] == "supply_air_temp_c")["suggested_point_id"]

    from src.simulation.calibration_export import build_calibration_dataset
    import csv, io  # noqa: E401

    csv_bytes = build_calibration_dataset(database, recording["id"], {"supply_air_temp_c": goal_suggested_id})
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8"))))

    assert len(rows) == 2  # header + exactly the one sample seeded above
    assert rows[1][1] == "42.0"


def test_mapping_suggestions_rejects_setpoint_actual_value_mismatch(client, database, monkeypatch):
    """Confirmed live: the shared scoring engine can rank a setpoint point
    above the actual-value point for a plain (non-setpoint) variable, and
    vice versa, since it only sees name/token overlap -- "Setpoint" is just
    another matching word to it. A variable and its suggested point
    disagreeing on setpoint-vs-actual-value is never an acceptable
    suggestion for calibration (it would fit the model against its own
    setpoint as if it were the measured output), so the route must skip a
    mismatched top pick and fall back to a same-kind alternative instead."""
    recording, setpoint_point_id, actual_value_point_id = _seed_recording_with_two_of_three_points(
        client, database,
    )
    monkeypatch.setattr(calibration_routes, "fetch_remote_metadata", lambda settings, mid: _canned_metadata())

    def _fake_suggest(variable, device_id, database_arg):
        # Top pick disagrees with the variable on setpoint-vs-actual-value;
        # the (correct) alternative agrees.
        return MappingSuggestion(
            variable=variable.name, direction=variable.direction,
            suggested_point_id=setpoint_point_id, suggested_point_name="Supply-Air-Temp-Setpoint",
            confidence="high", score=0.9, reasons=["name match"],
            alternatives=[
                MappingAlternative(
                    point_id=actual_value_point_id, point_name="Supply-Air-Temp",
                    score=0.7, reasons=["name match"],
                ),
            ],
        )
    monkeypatch.setattr(calibration_routes, "suggest_mapping_for_variable", _fake_suggest)

    body = client.get(
        "/calibration/mapping-suggestions",
        params={"recording_id": recording["id"], "model_id": "model-x"},
    ).json()

    # supply_air_temp_c (the goal) is NOT a setpoint -- must not accept the
    # setpoint-named top pick, must fall back to the actual-value alternative.
    goal_variable = next(v for v in body["variables"] if v["name"] == "supply_air_temp_c")
    expected_id = next(p["id"] for p in recording["points"] if p["source_object_id"] == actual_value_point_id)
    assert goal_variable["suggested_point_id"] == expected_id
    assert goal_variable["confidence"] == "low"


def test_mapping_suggestions_setpoint_variable_prefers_setpoint_point(client, database, monkeypatch):
    """The mirror-image case: a genuinely setpoint-named variable must not
    accept a plain actual-value top pick either."""
    recording, actual_value_point_id, setpoint_point_id = _seed_recording_with_two_of_three_points(
        client, database,
    )
    monkeypatch.setattr(
        calibration_routes, "fetch_remote_metadata",
        lambda settings, mid: {
            "inputs": [{"name": "supply_air_temp_setpoint_c", "unit": "degC"}],
            "outputs": [{"name": "supply_air_temp_c", "unit": "degC"}],
            "calibration": {"enabled": True, "goal": {"output": "supply_air_temp_c"}, "tuners": []},
        },
    )

    def _fake_suggest(variable, device_id, database_arg):
        return MappingSuggestion(
            variable=variable.name, direction=variable.direction,
            suggested_point_id=actual_value_point_id, suggested_point_name="Supply-Air-Temp",
            confidence="high", score=0.9, reasons=["name match"],
            alternatives=[
                MappingAlternative(
                    point_id=setpoint_point_id, point_name="Supply-Air-Temp-Setpoint",
                    score=0.6, reasons=["name match"],
                ),
            ],
        )
    monkeypatch.setattr(calibration_routes, "suggest_mapping_for_variable", _fake_suggest)

    body = client.get(
        "/calibration/mapping-suggestions",
        params={"recording_id": recording["id"], "model_id": "model-x"},
    ).json()

    setpoint_variable = next(v for v in body["variables"] if v["name"] == "supply_air_temp_setpoint_c")
    expected_id = next(p["id"] for p in recording["points"] if p["source_object_id"] == setpoint_point_id)
    assert setpoint_variable["suggested_point_id"] == expected_id
    assert setpoint_variable["confidence"] == "low"


# ── /calibration/jobs ─────────────────────────────────────────────────────

def test_create_job_404_for_unknown_recording(client):
    res = client.post("/calibration/jobs", json={
        "recording_id": 999999, "model_id": "model-x", "mapping": {"x": 1},
    })
    assert res.status_code == 404


def test_create_job_400_for_empty_mapping(client, database):
    recording, _, _ = _seed_recording_with_two_of_three_points(client, database)
    res = client.post("/calibration/jobs", json={
        "recording_id": recording["id"], "model_id": "model-x", "mapping": {},
    })
    assert res.status_code == 400


def test_create_job_uploads_dataset_then_creates_job(client, database, monkeypatch):
    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    database.add_replay_sample(recording["id"], {p1_id: {"value": 12.3}})

    uploaded = {}
    def _fake_upload(settings, model_id, filename, file_obj):
        uploaded["model_id"] = model_id
        uploaded["filename"] = filename
        uploaded["csv"] = file_obj.read().decode("utf-8")
        return {"dataset_id": "ds-1"}
    created = {}
    def _fake_create_job(settings, model_id, dataset_id, configuration=None):
        created["model_id"] = model_id
        created["dataset_id"] = dataset_id
        return {"job_id": "job-1", "model_id": model_id, "status": "QUEUED"}

    monkeypatch.setattr(calibration_routes, "upload_calibration_dataset", _fake_upload)
    monkeypatch.setattr(calibration_routes, "create_calibration_job", _fake_create_job)

    res = client.post("/calibration/jobs", json={
        "recording_id": recording["id"],
        "model_id": "model-x",
        "mapping": {"outdoor_air_temp_c": p1_id},
    })

    assert res.status_code == 201
    assert res.json() == {"job_id": "job-1", "model_id": "model-x", "status": "QUEUED"}
    assert uploaded["model_id"] == "model-x"
    assert "outdoor_air_temp_c" in uploaded["csv"]
    assert created["dataset_id"] == "ds-1"


# ── job status/results/cancel relays ─────────────────────────────────────

def test_get_job_relays(client, monkeypatch):
    monkeypatch.setattr(
        calibration_routes, "get_calibration_job",
        lambda settings, model_id, job_id: {"job_id": job_id, "model_id": model_id, "status": "RUNNING"},
    )
    res = client.get("/calibration/jobs/job-1", params={"model_id": "model-x"})
    assert res.status_code == 200
    assert res.json()["status"] == "RUNNING"


def test_get_results_relays(client, monkeypatch):
    monkeypatch.setattr(
        calibration_routes, "get_calibration_results",
        lambda settings, model_id, job_id: {"job_id": job_id, "objective": {"best": 0.1}},
    )
    res = client.get("/calibration/jobs/job-1/results", params={"model_id": "model-x"})
    assert res.status_code == 200
    assert res.json()["objective"]["best"] == 0.1


def test_cancel_job_relays(client, monkeypatch):
    monkeypatch.setattr(
        calibration_routes, "cancel_calibration_job",
        lambda settings, model_id, job_id: {"job_id": job_id, "status": "CANCELLED"},
    )
    res = client.post("/calibration/jobs/job-1/cancel", params={"model_id": "model-x"})
    assert res.status_code == 200
    assert res.json()["status"] == "CANCELLED"
