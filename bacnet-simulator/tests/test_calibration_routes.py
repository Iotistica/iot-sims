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

    still_recording = database.create_recording(device_a["id"], {
        "name": "Still Recording", "point_ids": [pa["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })

    completed_empty = database.create_recording(device_a["id"], {
        "name": "Completed Empty", "point_ids": [pa["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    database.stop_recording(completed_empty["id"])

    completed_with_data = database.create_recording(device_b["id"], {
        "name": "Completed With Data", "point_ids": [pb["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    point_id = completed_with_data["points"][0]["id"]
    database.add_recording_sample(completed_with_data["id"], {point_id: {"value": 1.0}})
    database.stop_recording(completed_with_data["id"])

    res = client.get("/calibration/recordings")
    assert res.status_code == 200
    names = {r["name"] for r in res.json()}

    assert names == {"Completed With Data"}
    only = res.json()[0]
    assert only["device_name"] == "Device B"
    assert only["sample_count"] == 1


def _completed_recording_with_data(client, database, *, instance: int, name: str, equipment_type: str | None = None) -> dict:
    device = client.post("/devices", json={
        "protocol": "bacnet", "device_instance": instance, "name": name, "equipment_type": equipment_type,
    }).json()
    point = _make_point(database, device["id"], instance=0, name="Point")
    recording = database.create_recording(device["id"], {
        "name": f"{name} Recording", "point_ids": [point["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    point_id = recording["points"][0]["id"]
    database.add_recording_sample(recording["id"], {point_id: {"value": 1.0}})
    database.stop_recording(recording["id"])
    return recording


def test_list_recordings_model_id_no_longer_narrows(client, database):
    """The old equipment-type narrowing depended on mapping_hints.preferred_equipment_types,
    which was removed in the semantic-contract clean break. model_id is accepted but inert
    -- every completed recording is returned regardless."""
    _completed_recording_with_data(client, database, instance=9940, name="Rooftop 1", equipment_type="Rooftop_Unit")
    _completed_recording_with_data(client, database, instance=9941, name="Boiler Plant", equipment_type="Boiler")

    unfiltered = {r["name"] for r in client.get("/calibration/recordings").json()}
    with_model = {r["name"] for r in client.get("/calibration/recordings", params={"model_id": "rtu-model"}).json()}

    assert unfiltered == with_model == {"Rooftop 1 Recording", "Boiler Plant Recording"}


# ── /calibration/mapping-suggestions ─────────────────────────────────────

def _canned_metadata() -> dict:
    return {
        "id": "model-x",
        "inputs": [{"name": "outdoor_air_temp_c", "unit": "degC", "required": True}],
        "outputs": [{"name": "supply_air_temp_c", "unit": "degC"}],
        "fmu_sha256": "f" * 64,
        "metadata_sha256": "e" * 64,
        "calibration": {
            "enabled": True,
            "goal": {"output": "supply_air_temp_c", "metric": "cv_rmse"},
            "tuners": [{"parameter": "COP_nominal", "min": 2.0, "max": 5.0}],
        },
    }


def _seed_recording_with_two_of_three_points(client, database):
    # A throwaway device+recording created first deliberately desyncs the
    # objects.id and recording_points.id autoincrement sequences --
    # in a fresh test DB they'd otherwise happen to start in lockstep (both
    # at 1), which would silently mask a regression back to conflating the
    # two id spaces (see the "no data rows" bug this test guards against).
    throwaway_device = _make_device(client, instance=9929, name="Throwaway Device")
    throwaway_point_a = _make_point(database, throwaway_device["id"], instance=0, name="Throwaway Point A")
    _make_point(database, throwaway_device["id"], instance=1, name="Throwaway Point B")  # objects.id consumed, not recorded
    database.create_recording(throwaway_device["id"], {
        # Only one of the two throwaway objects is selected -- so
        # objects.id and recording_points.id advance by different
        # amounts here, guaranteeing they're desynced for every point
        # created below.
        "name": "Throwaway Recording", "point_ids": [throwaway_point_a["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })

    device = _make_device(client, instance=9930, name="Mapping Device")
    p1 = _make_point(database, device["id"], instance=0, name="Recorded Point 1")
    p2 = _make_point(database, device["id"], instance=1, name="Recorded Point 2")
    _make_point(database, device["id"], instance=2, name="Unrecorded Point")  # not in recording

    recording = database.create_recording(device["id"], {
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
    # No semantic contract on the canned metadata -> semantics don't bind, and the
    # name-based mechanism is exposed SEPARATELY (never as `suggested_point_id`).
    assert goal_variable["suggested_point_id"] is None
    assert goal_variable["resolution"]["quality"] == "unresolved"
    assert goal_variable["name_match"]["point_id"] == expected_recording_point_id
    assert goal_variable["name_match"]["confidence"] == "low"
    assert goal_variable["name_match"]["mechanism"] == "name"
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
    database.add_recording_sample(recording["id"], {recording_point_id: {"value": 42.0}})

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
    goal = next(v for v in body["variables"] if v["name"] == "supply_air_temp_c")
    assert goal["suggested_point_id"] is None  # no semantic contract on canned metadata
    goal_suggested_id = goal["name_match"]["point_id"]  # the name mechanism's pick

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
    assert goal_variable["suggested_point_id"] is None
    assert goal_variable["name_match"]["point_id"] == expected_id
    assert goal_variable["name_match"]["confidence"] == "low"


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
    assert setpoint_variable["suggested_point_id"] is None
    assert setpoint_variable["name_match"]["point_id"] == expected_id
    assert setpoint_variable["name_match"]["confidence"] == "low"


# ── /calibration/mapping-suggestions: provider-neutral semantic tier ──────

def _semantic_metadata() -> dict:
    """Canned metadata carrying provider-neutral `semantic` blocks (layer 1)."""
    return {
        "id": "model-x",
        "inputs": [
            {"name": "outdoor_air_temp_c", "unit": "degC", "required": True,
             "semantic": {"id": "outdoor_air_temperature", "role": "sensor", "scope": "any"}},
        ],
        "outputs": [
            {"name": "supply_air_temp_c", "unit": "degC",
             "semantic": {"id": "supply_air_temperature", "role": "sensor"}},
        ],
        "calibration": {"enabled": True, "goal": {"output": "supply_air_temp_c"}, "tuners": []},
    }


def _classify_point(database, object_id: int, device_id: int, brick_class: str) -> None:
    top = next(
        e for e in database.get_semantic_entities(device_id=device_id, entity_kind="equipment")
        if not database.get_related_entities(e["id"], "isPartOf", direction="out")
    )
    point_entity = database.create_semantic_entity(
        name=f"entity-{object_id}", brick_class=brick_class, entity_kind="point", object_id=object_id,
    )
    database.create_semantic_relationship(point_entity["id"], "isPointOf", top["id"])


def _rtu_with_recording(client, database, *, point_specs):
    """point_specs: list of (object_name, brick_class|None). Returns (recording, {name: recording_point_id})."""
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": 9940, "name": "RTU-1"}).json()
    assert client.put(f"/devices/{device['id']}", json={
        "protocol": "bacnet", "device_instance": 9940, "name": "RTU-1", "equipment_type": "Rooftop_Unit",
    }).status_code == 200

    object_ids = []
    for i, (name, brick_class) in enumerate(point_specs):
        obj = _make_point(database, device["id"], instance=i, name=name)
        object_ids.append(obj["id"])
        if brick_class:
            _classify_point(database, obj["id"], device["id"], brick_class)

    recording = database.create_recording(device["id"], {
        "name": "Semantic Test", "point_ids": object_ids,
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    rp_by_object = {p["source_object_id"]: p["id"] for p in recording["points"]}
    return recording, {spec[0]: rp_by_object[oid] for spec, oid in zip(point_specs, object_ids)}


def test_mapping_suggestions_resolves_semantically_despite_opaque_point_name(client, database, monkeypatch):
    monkeypatch.setattr(calibration_routes, "fetch_remote_metadata", lambda s, m: _semantic_metadata())
    recording, rp = _rtu_with_recording(client, database, point_specs=[
        ("XYZ_42", "Supply_Air_Temperature_Sensor"),   # opaque name, correct classification
        ("weird_oat", "Outside_Air_Temperature_Sensor"),
    ])

    body = client.get("/calibration/mapping-suggestions", params={
        "recording_id": recording["id"], "model_id": "model-x",
    }).json()

    goal = next(v for v in body["variables"] if v["name"] == "supply_air_temp_c")
    assert goal["suggested_point_id"] == rp["XYZ_42"]
    assert goal["confidence"] == "high"
    assert goal["resolution"] == {"provider": "brick", "quality": "exact"}

    oat = next(v for v in body["variables"] if v["name"] == "outdoor_air_temp_c")
    assert oat["suggested_point_id"] == rp["weird_oat"]
    assert oat["resolution"]["quality"] == "exact"

    # the neutral boundary: no representation-specific identity leaks into the response
    assert "point_class" not in client.get(
        "/calibration/mapping-suggestions",
        params={"recording_id": recording["id"], "model_id": "model-x"},
    ).text


def test_mapping_suggestions_semantic_ambiguity_reports_no_suggestion(client, database, monkeypatch):
    monkeypatch.setattr(calibration_routes, "fetch_remote_metadata", lambda s, m: _semantic_metadata())
    recording, rp = _rtu_with_recording(client, database, point_specs=[
        ("sat_a", "Supply_Air_Temperature_Sensor"),
        ("sat_b", "Supply_Air_Temperature_Sensor"),   # two identical -> ambiguous
        ("oat", "Outside_Air_Temperature_Sensor"),
    ])

    body = client.get("/calibration/mapping-suggestions", params={
        "recording_id": recording["id"], "model_id": "model-x",
    }).json()

    goal = next(v for v in body["variables"] if v["name"] == "supply_air_temp_c")
    assert goal["suggested_point_id"] is None
    assert goal["confidence"] == "none"
    assert goal["resolution"]["quality"] == "ambiguous"
    assert {a["point_id"] for a in goal["alternatives"]} == {rp["sat_a"], rp["sat_b"]}


def test_unresolved_semantic_never_auto_binds_name_match_is_separate(client, database, monkeypatch):
    """Clean break: a failed semantic resolution does NOT silently fall through to the
    name scorer for `suggested_point_id`. The name mechanism is offered separately, and
    a name-based guess is never labelled a semantic match."""
    monkeypatch.setattr(calibration_routes, "fetch_remote_metadata", lambda s, m: _semantic_metadata())
    recording, rp = _rtu_with_recording(client, database, point_specs=[
        ("Supply Air Temp", None),   # not classified -> semantic resolution unresolved
        ("OAT", None),
    ])

    def _fake_suggest(variable, device_id, database_arg):
        target = "Supply Air Temp" if variable.name == "supply_air_temp_c" else "OAT"
        oid = next(p["source_object_id"] for p in recording["points"] if p["object_name"] == target)
        return MappingSuggestion(
            variable=variable.name, direction=variable.direction, suggested_point_id=oid,
            suggested_point_name=target, confidence="high", score=0.9, reasons=["name match"],
        )
    monkeypatch.setattr(calibration_routes, "suggest_mapping_for_variable", _fake_suggest)

    body = client.get("/calibration/mapping-suggestions", params={
        "recording_id": recording["id"], "model_id": "model-x",
    }).json()
    goal = next(v for v in body["variables"] if v["name"] == "supply_air_temp_c")
    assert goal["suggested_point_id"] is None
    assert goal["resolution"]["quality"] == "unresolved"
    assert goal["name_match"]["point_id"] == rp["Supply Air Temp"]
    assert goal["name_match"]["mechanism"] == "name"
    # a name match is never dressed up as a semantic reason
    assert not any("Semantic" in r for r in goal["reasons"])


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


def _patch_job_relays(monkeypatch, *, upload=None, create_job=None, results=None, get_job=None):
    monkeypatch.setattr(calibration_routes, "fetch_remote_metadata", lambda s, m: _canned_metadata())
    calls: dict = {"uploads": [], "create_job": None}

    def _default_upload(settings, model_id, filename, file_obj):
        calls["uploads"].append({"filename": filename, "csv": file_obj.read().decode("utf-8")})
        return {"dataset_id": f"ds-{len(calls['uploads'])}"}

    def _default_create_job(settings, model_id, dataset_id, configuration=None, *,
                            validation_dataset_id=None, experiment_id=None):
        calls["create_job"] = {
            "dataset_id": dataset_id,
            "validation_dataset_id": validation_dataset_id,
            "experiment_id": experiment_id,
        }
        return {"job_id": "job-1", "model_id": model_id, "status": "QUEUED"}

    monkeypatch.setattr(calibration_routes, "upload_calibration_dataset", upload or _default_upload)
    monkeypatch.setattr(calibration_routes, "create_calibration_job", create_job or _default_create_job)
    if results is not None:
        monkeypatch.setattr(calibration_routes, "get_calibration_results", results)
    if get_job is not None:
        monkeypatch.setattr(calibration_routes, "get_calibration_job", get_job)
    return calls


def test_create_job_creates_run_row_with_provenance_and_revision_pin(client, database, monkeypatch):
    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    database.add_recording_sample(recording["id"], {p1_id: {"value": 12.3}})
    calls = _patch_job_relays(monkeypatch)

    res = client.post("/calibration/jobs", json={
        "recording_id": recording["id"],
        "model_id": "model-x",
        "mapping": {"outdoor_air_temp_c": p1_id},
        "simulation_model_id": 42,
    })
    assert res.status_code == 201
    body = res.json()
    assert body["job_id"] == "job-1"
    run_id = body["calibration_run_id"]

    assert "outdoor_air_temp_c" in calls["uploads"][0]["csv"]
    assert calls["create_job"]["dataset_id"] == "ds-1"
    assert calls["create_job"]["validation_dataset_id"] is None
    assert calls["create_job"]["experiment_id"] == str(run_id)

    run = database.get_calibration_run(run_id)
    assert run["status"] == "queued"
    assert run["runtime_job_id"] == "job-1"
    assert run["runtime_dataset_id"] == "ds-1"
    assert run["simulation_model_id"] == 42
    assert run["recording_id"] == recording["id"]
    assert run["fmu_sha256"] == "f" * 64
    assert run["metadata_sha256"] == "e" * 64
    assert run["tuner_names"] == ["COP_nominal"]
    assert run["objective_metric"] == "cv_rmse"
    assert run["validation_status"] == "not_requested"
    prov = run["mapping_json"]["outdoor_air_temp_c"]
    assert prov["recording_point_id"] == p1_id
    assert prov["semantic_resolution"]["provider"]  # a provider always answered
    assert prov["selection_source"] in ("semantic_match", "user_override")


def test_create_job_with_validation_recording_uploads_second_dataset(client, database, monkeypatch):
    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    database.add_recording_sample(recording["id"], {p1_id: {"value": 12.3}})

    # A second recording of the SAME device/point -> _remap_points_to_recording can match
    # it by source_object_id.
    device_id = recording["source_device_id"]
    src_point = next(p for p in database.get_recording(recording["id"])["points"] if p["id"] == p1_id)
    val_recording = database.create_recording(device_id, {
        "name": "Validation Rec", "point_ids": [src_point["source_object_id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    val_point_id = database.get_recording(val_recording["id"])["points"][0]["id"]
    database.add_recording_sample(val_recording["id"], {val_point_id: {"value": 11.0}})

    calls = _patch_job_relays(monkeypatch)

    res = client.post("/calibration/jobs", json={
        "recording_id": recording["id"],
        "model_id": "model-x",
        "mapping": {"outdoor_air_temp_c": p1_id},
        "validation_recording_id": val_recording["id"],
    })
    assert res.status_code == 201
    run = database.get_calibration_run(res.json()["calibration_run_id"])

    assert len(calls["uploads"]) == 2
    assert calls["create_job"]["validation_dataset_id"] == "ds-2"
    assert run["runtime_validation_dataset_id"] == "ds-2"
    assert run["validation_recording_id"] == val_recording["id"]
    assert run["validation_status"] == "pending"


def test_create_job_rejects_validation_recording_equal_to_calibration(client, database, monkeypatch):
    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    _patch_job_relays(monkeypatch)
    res = client.post("/calibration/jobs", json={
        "recording_id": recording["id"], "model_id": "model-x",
        "mapping": {"outdoor_air_temp_c": p1_id},
        "validation_recording_id": recording["id"],
    })
    assert res.status_code == 400


def test_create_job_dataset_upload_failure_marks_run_failed(client, database, monkeypatch):
    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    database.add_recording_sample(recording["id"], {p1_id: {"value": 12.3}})

    def _boom_upload(settings, model_id, filename, file_obj):
        raise RuntimeError("runtime down")

    _patch_job_relays(monkeypatch, upload=_boom_upload)
    res = client.post("/calibration/jobs", json={
        "recording_id": recording["id"], "model_id": "model-x",
        "mapping": {"outdoor_air_temp_c": p1_id},
    })
    assert res.status_code == 502
    runs = database.list_calibration_runs(model_id="model-x")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["failure_stage"] == "dataset_upload"
    assert "runtime down" in runs[0]["error_summary"]


def test_get_job_syncs_completed_run(client, database, monkeypatch):
    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    database.add_recording_sample(recording["id"], {p1_id: {"value": 12.3}})
    _patch_job_relays(monkeypatch)
    run_id = client.post("/calibration/jobs", json={
        "recording_id": recording["id"], "model_id": "model-x",
        "mapping": {"outdoor_air_temp_c": p1_id},
    }).json()["calibration_run_id"]

    monkeypatch.setattr(
        calibration_routes, "get_calibration_job",
        lambda s, m, j: {"job_id": j, "model_id": m, "status": "COMPLETED"},
    )
    monkeypatch.setattr(
        calibration_routes, "get_calibration_results",
        lambda s, m, j: {
            "objective": {"baseline": 25.0, "best": 12.5, "improvement_pct": 50.0},
            "best_parameters": {"COP_nominal": 3.9},
            "fmu_defaults": {"COP_nominal": 3.5},
            "validation_metrics": None,
            "ashrae_g14": {"applicable": False, "pass": None, "based_on": None,
                           "reason": "held_out_validation_not_available"},
        },
    )

    res = client.get("/calibration/jobs/job-1", params={"model_id": "model-x"})
    assert res.status_code == 200
    assert res.json()["calibration_run_id"] == run_id

    run = database.get_calibration_run(run_id)
    assert run["status"] == "completed"
    assert run["baseline_metric"] == 25.0
    assert run["calibration_metric"] == 12.5
    assert run["improvement_pct"] == 50.0
    assert run["best_parameters_json"] == {"COP_nominal": 3.9}
    assert run["fmu_defaults_json"] == {"COP_nominal": 3.5}
    assert run["g14_based_on"] is None
    assert run["g14_applicable"] == 0
    assert run["validation_status"] == "not_requested"
    assert run["completed_at"] is not None

    # A second poll is idempotent -- the terminal row isn't re-synced.
    monkeypatch.setattr(
        calibration_routes, "get_calibration_results",
        lambda s, m, j: (_ for _ in ()).throw(AssertionError("should not refetch")),
    )
    assert client.get("/calibration/jobs/job-1", params={"model_id": "model-x"}).status_code == 200


def test_get_job_syncs_failed_run(client, database, monkeypatch):
    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    database.add_recording_sample(recording["id"], {p1_id: {"value": 12.3}})
    _patch_job_relays(monkeypatch)
    run_id = client.post("/calibration/jobs", json={
        "recording_id": recording["id"], "model_id": "model-x",
        "mapping": {"outdoor_air_temp_c": p1_id},
    }).json()["calibration_run_id"]

    monkeypatch.setattr(
        calibration_routes, "get_calibration_job",
        lambda s, m, j: {"job_id": j, "model_id": m, "status": "FAILED", "error": "HEBO blew up"},
    )
    client.get("/calibration/jobs/job-1", params={"model_id": "model-x"})

    run = database.get_calibration_run(run_id)
    assert run["status"] == "failed"
    assert run["failure_stage"] == "calibrating"
    assert run["error_summary"] == "HEBO blew up"


def test_calibration_runs_list_and_detail(client, database, monkeypatch):
    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    database.add_recording_sample(recording["id"], {p1_id: {"value": 12.3}})
    _patch_job_relays(monkeypatch)
    run_id = client.post("/calibration/jobs", json={
        "recording_id": recording["id"], "model_id": "model-x",
        "mapping": {"outdoor_air_temp_c": p1_id}, "simulation_model_id": 7,
    }).json()["calibration_run_id"]

    listed = client.get("/calibration/runs", params={"model_id": "model-x"})
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [run_id]
    assert client.get("/calibration/runs", params={"simulation_model_id": 7}).json()[0]["id"] == run_id
    assert client.get("/calibration/runs", params={"model_id": "nope"}).json() == []

    detail = client.get(f"/calibration/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["runtime_job_id"] == "job-1"
    assert client.get("/calibration/runs/999999").status_code == 404


# ── /calibration/runs/{id}/apply ─────────────────────────────────────────

def _completed_run_and_model(client, database, monkeypatch, *, best_params):
    from src.simulation.models import store

    recording, p1_id, _ = _seed_recording_with_two_of_three_points(client, database)
    database.add_recording_sample(recording["id"], {p1_id: {"value": 12.3}})
    _patch_job_relays(monkeypatch)
    run_id = client.post("/calibration/jobs", json={
        "recording_id": recording["id"], "model_id": "model-x",
        "mapping": {"outdoor_air_temp_c": p1_id},
    }).json()["calibration_run_id"]
    database.update_calibration_run(run_id, status="completed", best_parameters_json=best_params)

    model = store.create_simulation_model(
        database, name="RTU cfg", provider_type="fmu", model_type="model-x",
        enabled=False, parameters={"parameter_overrides": {"COP_nominal": 3.0}},
        created_from_device_id=None, mappings=[],
    )
    reloaded: list[int] = []
    monkeypatch.setattr(calibration_routes, "reload_model",
                        lambda db, eng, mid, **kw: reloaded.append(mid))
    return run_id, model["id"], reloaded


def test_apply_run_merges_overrides_reloads_and_records_applied_state(client, database, monkeypatch):
    run_id, model_id, reloaded = _completed_run_and_model(
        client, database, monkeypatch, best_params={"COP_nominal": 3.9, "kSAT": 0.25},
    )

    res = client.post(f"/calibration/runs/{run_id}/apply", json={"simulation_model_id": model_id})
    assert res.status_code == 200

    from src.simulation.models import store
    cfg = store.get_simulation_model(database, model_id)
    assert cfg["parameters"]["parameter_overrides"] == {"COP_nominal": 3.9, "kSAT": 0.25}
    assert reloaded == [model_id]

    run = database.get_calibration_run(run_id)
    assert run["applied_at"] is not None
    assert run["applied_to_simulation_model_id"] == model_id


def test_apply_run_409_when_not_completed(client, database, monkeypatch):
    run_id, model_id, _ = _completed_run_and_model(client, database, monkeypatch, best_params={"x": 1.0})
    database.update_calibration_run(run_id, status="running")
    res = client.post(f"/calibration/runs/{run_id}/apply", json={"simulation_model_id": model_id})
    assert res.status_code == 409


def test_apply_run_404_for_unknown_run_or_model(client, database, monkeypatch):
    run_id, _, _ = _completed_run_and_model(client, database, monkeypatch, best_params={"x": 1.0})
    assert client.post("/calibration/runs/999999/apply", json={"simulation_model_id": 1}).status_code == 404
    assert client.post(f"/calibration/runs/{run_id}/apply", json={"simulation_model_id": 999999}).status_code == 404


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
