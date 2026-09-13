"""/training/* routes -- thin glue, so these tests monkeypatch the relay
functions (remote_training.py/remote_catalog.py) rather than hitting a real
iot-models server, and monkeypatch suggest_mappings_for_model itself to test
only this router's own multi-recording coverage/filtering logic, not the
scoring engine it reuses (already covered by mapping/suggestions.py's own
tests). Mirrors test_calibration_routes.py's own structure/fixtures."""
from __future__ import annotations

import src.api.routers.training_export as training_routes
from src.simulation.models.registry import ModelDefinition, VariableDefinition


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


def _fake_model_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="model-x", label="Model X", provider_type="ai", description="",
        parameters=(),
        variables=(
            # default=18.5 -- exercises that the mapping-suggestions route
            # echoes a variable's own catalog default through untouched.
            VariableDefinition(name="outdoor_air_temp_c", label="OAT", direction="input", unit="°C", default=18.5),
            VariableDefinition(name="supply_air_temp_c", label="SAT", direction="output", unit="°C"),
        ),
        factory=lambda parameters: None,
    )


def _two_recordings_of_same_device(client, database):
    """Two separate recording sessions of the SAME device -- the realistic
    "combine several scenario runs" case, same source_object_ids in both."""
    device = _make_device(client, instance=9950, name="Training Route Device")
    p1 = _make_point(database, device["id"], instance=0, name="Outdoor Air Temp")
    p2 = _make_point(database, device["id"], instance=1, name="Supply Air Temp")

    rec1 = database.create_recording(device["id"], {
        "name": "Scenario A", "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    database.add_recording_sample(rec1["id"], {
        next(p["id"] for p in rec1["points"] if p["source_object_id"] == p1["id"]): {"value": 10.0},
        next(p["id"] for p in rec1["points"] if p["source_object_id"] == p2["id"]): {"value": 18.0},
    })

    rec2 = database.create_recording(device["id"], {
        "name": "Scenario B", "point_ids": [p1["id"], p2["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    database.add_recording_sample(rec2["id"], {
        next(p["id"] for p in rec2["points"] if p["source_object_id"] == p1["id"]): {"value": 20.0},
        next(p["id"] for p in rec2["points"] if p["source_object_id"] == p2["id"]): {"value": 22.0},
    })
    return rec1, rec2, p1["id"], p2["id"]


def _attach_model(database, device_id: int, model_type: str) -> int:
    from src.simulation.models.store import ensure_simulation_model_schema
    ensure_simulation_model_schema(database)
    conn = database._conn()
    cur = conn.execute(
        "INSERT INTO simulation_model_configs (name, provider_type, model_type, enabled, created_from_device_id) "
        "VALUES (?, 'ai', ?, 1, ?)",
        (f"model-for-{device_id}", model_type, device_id),
    )
    config_id = cur.lastrowid
    conn.commit()
    conn.close()
    return config_id


def _add_plain_mapping(database, config_id: int, variable: str, direction: str, point_id: int) -> None:
    conn = database._conn()
    conn.execute(
        "INSERT INTO simulation_model_mappings (model_config_id, variable, direction, point_id) VALUES (?, ?, ?, ?)",
        (config_id, variable, direction, point_id),
    )
    conn.commit()
    conn.close()


def _add_aggregate_mapping(database, config_id: int, variable: str, direction: str, member_point_ids: list[int]) -> None:
    conn = database._conn()
    cur = conn.execute(
        "INSERT INTO simulation_model_aggregate_mappings (model_config_id, variable, direction, operation) "
        "VALUES (?, ?, ?, 'max')",
        (config_id, variable, direction),
    )
    agg_id = cur.lastrowid
    for point_id in member_point_ids:
        conn.execute(
            "INSERT INTO simulation_model_aggregate_members (aggregate_mapping_id, point_id) VALUES (?, ?)",
            (agg_id, point_id),
        )
    conn.commit()
    conn.close()


def _completed_recording(client, database, *, instance: int, name: str) -> dict:
    device = _make_device(client, instance=instance, name=name)
    point = _make_point(database, device["id"], instance=0, name="Point")
    recording = database.create_recording(device["id"], {
        "name": f"{name} Recording", "point_ids": [point["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    database.add_recording_sample(recording["id"], {recording["points"][0]["id"]: {"value": 1.0}})
    database.stop_recording(recording["id"])
    return recording, device["id"]


# ── /training/recordings ───────────────────────────────────────────────────

def test_list_recordings_only_from_devices_running_the_same_model(client, database):
    """The bug this guards against, confirmed live: an RTU-model recording
    combined with an unrelated VAV-model recording produces near-zero point
    overlap, flagging almost every variable as missing from one side. This
    route must never surface that VAV recording as a training-eligible
    option for the RTU model at all."""
    rtu_rec, rtu_device_id = _completed_recording(client, database, instance=9960, name="RTU Controller")
    _attach_model(database, rtu_device_id, "rtu-model-id")

    vav_rec, vav_device_id = _completed_recording(client, database, instance=9961, name="VAV-2 Controller")
    _attach_model(database, vav_device_id, "vav-model-id")

    res = client.get("/training/recordings", params={"model_id": "rtu-model-id", "device_id": rtu_device_id})
    assert res.status_code == 200
    names = {r["name"] for r in res.json()}
    assert names == {rtu_rec["name"]}
    assert vav_rec["name"] not in names


def test_list_recordings_includes_every_device_running_the_same_model(client, database):
    """Two different devices both running the exact same model_type are a
    legitimate fleet -- both should be eligible for combining."""
    rec_a, device_a_id = _completed_recording(client, database, instance=9962, name="RTU-1")
    _attach_model(database, device_a_id, "rtu-model-id")

    rec_b, device_b_id = _completed_recording(client, database, instance=9963, name="RTU-2")
    _attach_model(database, device_b_id, "rtu-model-id")

    res = client.get("/training/recordings", params={"model_id": "rtu-model-id", "device_id": device_a_id})
    assert res.status_code == 200
    names = {r["name"] for r in res.json()}
    assert names == {rec_a["name"], rec_b["name"]}


def test_list_recordings_falls_back_to_calling_device_when_no_other_device_shares_the_model(client, database):
    """No other device runs this exact model -- falls back to just the
    calling device's own recordings, never every recording in the system."""
    own_rec, own_device_id = _completed_recording(client, database, instance=9964, name="Lonely RTU")
    _attach_model(database, own_device_id, "rtu-model-id")

    unrelated_rec, unrelated_device_id = _completed_recording(client, database, instance=9965, name="Unrelated Device")
    # Deliberately NOT attached to any model at all.

    res = client.get("/training/recordings", params={"model_id": "rtu-model-id", "device_id": own_device_id})
    assert res.status_code == 200
    names = {r["name"] for r in res.json()}
    assert names == {own_rec["name"]}
    assert unrelated_rec["name"] not in names


# ── /training/mapping-suggestions ─────────────────────────────────────────

def test_mapping_suggestions_404_for_unknown_recording(client, monkeypatch):
    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    res = client.get(
        "/training/mapping-suggestions",
        params={"recording_ids": "999999", "model_id": "model-x", "device_id": 0},
    )
    assert res.status_code == 404


def test_mapping_suggestions_400_for_empty_recording_ids(client):
    res = client.get(
        "/training/mapping-suggestions",
        params={"recording_ids": "", "model_id": "model-x", "device_id": 0},
    )
    assert res.status_code == 400


def test_mapping_suggestions_covers_every_variable_unmapped_by_default(client, database, monkeypatch):
    """Unlike calibration's own mapping-suggestions (inputs + one goal
    output only), every declared variable must be covered -- this model
    has one input and one output and no calibration block at all, which
    would 400 calibration's own route but must work fine here. Neither
    variable has been mapped in this device's Simulation Model config
    (there isn't one), so both come back unmapped -- there is no scoring
    engine here to guess a point instead (see this route's own docstring
    for why)."""
    rec1, rec2, _p1_id, _p2_id = _two_recordings_of_same_device(client, database)
    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )

    res = client.get(
        "/training/mapping-suggestions",
        params={
            "recording_ids": f"{rec1['id']},{rec2['id']}", "model_id": "model-x",
            "device_id": rec1["source_device_id"],
        },
    )
    assert res.status_code == 200
    body = res.json()
    names = {v["name"] for v in body["variables"]}
    assert names == {"outdoor_air_temp_c", "supply_air_temp_c"}
    for variable in body["variables"]:
        assert variable["mapped"] is False
        assert variable["object_id"] is None
        assert variable["point_name"] is None
        assert variable["missing_in_recordings"] == []

    oat_variable = next(v for v in body["variables"] if v["name"] == "outdoor_air_temp_c")
    # The variable's own catalog default is echoed through untouched.
    assert oat_variable["default"] == 18.5


def test_mapping_suggestions_uses_authoritative_mapping_from_device_config(client, database, monkeypatch):
    """A variable already mapped on the calling device's own active model
    config gets that exact point back, mapped=True -- the only way a
    variable ever becomes mapped here (see _authoritative_mapping)."""
    rec1, rec2, p1_id, p2_id = _two_recordings_of_same_device(client, database)
    device_id = rec1["source_device_id"]
    config_id = _attach_model(database, device_id, "model-x")
    _add_plain_mapping(database, config_id, "outdoor_air_temp_c", "input", p1_id)
    _add_plain_mapping(database, config_id, "supply_air_temp_c", "output", p2_id)

    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )

    res = client.get(
        "/training/mapping-suggestions",
        params={
            "recording_ids": f"{rec1['id']},{rec2['id']}", "model_id": "model-x", "device_id": device_id,
        },
    )
    assert res.status_code == 200
    body = res.json()
    oat_variable = next(v for v in body["variables"] if v["name"] == "outdoor_air_temp_c")
    assert oat_variable["mapped"] is True
    assert oat_variable["object_id"] == p1_id
    assert oat_variable["point_name"] == "Outdoor Air Temp"
    # Captured by both selected recordings -- nothing missing.
    assert oat_variable["missing_in_recordings"] == []

    sat_variable = next(v for v in body["variables"] if v["name"] == "supply_air_temp_c")
    assert sat_variable["mapped"] is True
    assert sat_variable["object_id"] == p2_id


def test_mapping_suggestions_reports_missing_in_recordings(client, database, monkeypatch):
    """An authoritatively-mapped variable whose point only exists in ONE of
    the selected recordings must be flagged as missing from the other --
    this is the coverage signal the UI needs to warn before submission that
    including this variable will silently drop the other recording's rows
    entirely."""
    device_a = _make_device(client, instance=9951, name="Device A")
    p_only_in_a = _make_point(database, device_a["id"], instance=0, name="Only In A")
    rec_a = database.create_recording(device_a["id"], {
        "name": "A", "point_ids": [p_only_in_a["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    database.add_recording_sample(rec_a["id"], {rec_a["points"][0]["id"]: {"value": 1.0}})

    device_b = _make_device(client, instance=9952, name="Device B")
    rec_b = database.create_recording(device_b["id"], {
        "name": "B", "point_ids": [],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })

    config_id = _attach_model(database, device_a["id"], "model-x")
    _add_plain_mapping(database, config_id, "outdoor_air_temp_c", "input", p_only_in_a["id"])

    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )

    res = client.get(
        "/training/mapping-suggestions",
        params={
            "recording_ids": f"{rec_a['id']},{rec_b['id']}", "model_id": "model-x",
            "device_id": device_a["id"],
        },
    )
    assert res.status_code == 200
    body = res.json()
    oat_variable = next(v for v in body["variables"] if v["name"] == "outdoor_air_temp_c")
    assert oat_variable["mapped"] is True
    assert oat_variable["object_id"] == p_only_in_a["id"]
    assert oat_variable["missing_in_recordings"] == [rec_b["id"]]

    sat_variable = next(v for v in body["variables"] if v["name"] == "supply_air_temp_c")
    assert sat_variable["mapped"] is False


def test_mapping_suggestions_aggregate_mapped_variable_comes_back_unmapped(client, database, monkeypatch):
    """An aggregate-mapped variable (weighted-average/min/max across
    several member points) has no single point whose raw value equals the
    model's own computed aggregate -- _authoritative_mapping deliberately
    excludes it (see its own docstring), and there is no scoring-engine
    fallback here any more to guess one instead, so it comes back
    unmapped, same as a variable with no mapping at all."""
    rec1, rec2, p1_id, p2_id = _two_recordings_of_same_device(client, database)
    device_id = rec1["source_device_id"]
    config_id = _attach_model(database, device_id, "model-x")
    _add_aggregate_mapping(database, config_id, "outdoor_air_temp_c", "input", [p1_id, p2_id])

    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )

    res = client.get(
        "/training/mapping-suggestions",
        params={
            "recording_ids": f"{rec1['id']},{rec2['id']}", "model_id": "model-x", "device_id": device_id,
        },
    )
    assert res.status_code == 200
    body = res.json()
    oat_variable = next(v for v in body["variables"] if v["name"] == "outdoor_air_temp_c")
    assert oat_variable["mapped"] is False
    assert oat_variable["object_id"] is None


# ── constant-sourced inputs (migration 024) ─────────────────────────────────

def test_mapping_suggestions_falls_back_to_recording_constant_when_no_live_point(client, database, monkeypatch):
    """No live point at all for this input -- both selected recordings
    snapshotted the same constant value at record time, so it comes back
    mapped via 'constant', not 'point'."""
    rec1, rec2, _p1_id, p2_id = _two_recordings_of_same_device(client, database)
    database.add_recording_input_constants(rec1["id"], {"outdoor_air_temp_c": 30.0})
    database.add_recording_input_constants(rec2["id"], {"outdoor_air_temp_c": 30.0})
    device_id = rec1["source_device_id"]
    config_id = _attach_model(database, device_id, "model-x")
    _add_plain_mapping(database, config_id, "supply_air_temp_c", "output", p2_id)

    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    res = client.get(
        "/training/mapping-suggestions",
        params={"recording_ids": f"{rec1['id']},{rec2['id']}", "model_id": "model-x", "device_id": device_id},
    )
    assert res.status_code == 200
    oat_variable = next(v for v in res.json()["variables"] if v["name"] == "outdoor_air_temp_c")
    assert oat_variable["mapped"] is True
    assert oat_variable["source"] == "constant"
    assert oat_variable["object_id"] is None
    assert oat_variable["constant_values"] == {str(rec1["id"]): 30.0, str(rec2["id"]): 30.0}
    assert oat_variable["missing_in_recordings"] == []


def test_mapping_suggestions_flags_recording_with_no_constant_snapshot(client, database, monkeypatch):
    """Only one of the two selected recordings has a snapshot for this
    input (predates migration 024, or wasn't constant-sourced at record
    time) -- still mapped=True (usable via the one that does), but the
    other recording is listed as missing it, same coverage signal a
    point-mapped variable gets."""
    rec1, rec2, _p1_id, p2_id = _two_recordings_of_same_device(client, database)
    database.add_recording_input_constants(rec1["id"], {"outdoor_air_temp_c": 30.0})
    # rec2 deliberately gets no snapshot -- simulates a pre-migration recording.
    device_id = rec1["source_device_id"]
    config_id = _attach_model(database, device_id, "model-x")
    _add_plain_mapping(database, config_id, "supply_air_temp_c", "output", p2_id)

    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    res = client.get(
        "/training/mapping-suggestions",
        params={"recording_ids": f"{rec1['id']},{rec2['id']}", "model_id": "model-x", "device_id": device_id},
    )
    assert res.status_code == 200
    oat_variable = next(v for v in res.json()["variables"] if v["name"] == "outdoor_air_temp_c")
    assert oat_variable["mapped"] is True
    assert oat_variable["source"] == "constant"
    assert oat_variable["missing_in_recordings"] == [rec2["id"]]


def test_mapping_suggestions_live_point_takes_priority_over_constant_snapshot(client, database, monkeypatch):
    """A variable that's BOTH point-mapped on the device's current config
    AND carries old constant-snapshot data (e.g. it used to be constant,
    now converted to a real point) -- the live point wins, exactly like
    _authoritative_mapping already prioritizes live config generally."""
    rec1, rec2, p1_id, p2_id = _two_recordings_of_same_device(client, database)
    database.add_recording_input_constants(rec1["id"], {"outdoor_air_temp_c": 30.0})
    database.add_recording_input_constants(rec2["id"], {"outdoor_air_temp_c": 30.0})
    device_id = rec1["source_device_id"]
    config_id = _attach_model(database, device_id, "model-x")
    _add_plain_mapping(database, config_id, "outdoor_air_temp_c", "input", p1_id)
    _add_plain_mapping(database, config_id, "supply_air_temp_c", "output", p2_id)

    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    res = client.get(
        "/training/mapping-suggestions",
        params={"recording_ids": f"{rec1['id']},{rec2['id']}", "model_id": "model-x", "device_id": device_id},
    )
    assert res.status_code == 200
    oat_variable = next(v for v in res.json()["variables"] if v["name"] == "outdoor_air_temp_c")
    assert oat_variable["source"] == "point"
    assert oat_variable["object_id"] == p1_id
    assert oat_variable["constant_values"] is None


# ── /training/jobs ─────────────────────────────────────────────────────────

def test_create_job_400_for_fewer_than_two_recordings(client, database):
    rec1, _rec2, p1_id, _p2_id = _two_recordings_of_same_device(client, database)
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "recording_ids": [rec1["id"]],
        "mapping": {"outdoor_air_temp_c": p1_id},
    })
    assert res.status_code == 400


def test_create_job_400_for_empty_mapping(client, database):
    rec1, rec2, _p1_id, _p2_id = _two_recordings_of_same_device(client, database)
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "recording_ids": [rec1["id"], rec2["id"]], "mapping": {},
    })
    assert res.status_code == 400


def test_create_job_404_for_unknown_recording(client):
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "recording_ids": [999998, 999999],
        "mapping": {"outdoor_air_temp_c": 1},
    })
    assert res.status_code == 404


def test_create_job_400_for_unknown_variable_name(client, database, monkeypatch):
    rec1, rec2, p1_id, _p2_id = _two_recordings_of_same_device(client, database)
    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "recording_ids": [rec1["id"], rec2["id"]],
        "mapping": {"not_a_real_variable": p1_id},
    })
    assert res.status_code == 400


def test_create_job_400_for_mapping_with_no_input_variables(client, database, monkeypatch):
    """A mapping built entirely from a device's own OUTPUT points (e.g.
    recordings made before this device's real inputs were ever captured)
    would produce a dataset with 0 input_* columns -- shared/train_baseline.
    py's own build_supervised_pairs rejects that deep inside the async
    training job; this route catches it up front instead, before spending a
    CSV export + upload on a dataset that's already known to be useless."""
    rec1, rec2, _p1_id, p2_id = _two_recordings_of_same_device(client, database)
    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "recording_ids": [rec1["id"], rec2["id"]],
        "mapping": {"supply_air_temp_c": p2_id},  # output only, no input
    })
    assert res.status_code == 400
    assert "0 input" in res.json()["detail"]


def test_create_job_400_for_mapping_with_no_output_variables(client, database, monkeypatch):
    rec1, rec2, p1_id, _p2_id = _two_recordings_of_same_device(client, database)
    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "recording_ids": [rec1["id"], rec2["id"]],
        "mapping": {"outdoor_air_temp_c": p1_id},  # input only, no output
    })
    assert res.status_code == 400
    assert "0 output" in res.json()["detail"]


def test_create_job_builds_prefixed_columns_uploads_then_creates_job(client, database, monkeypatch):
    rec1, rec2, p1_id, p2_id = _two_recordings_of_same_device(client, database)
    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )

    uploaded = {}
    def _fake_upload(settings, model_id, filename, file_obj):
        uploaded["model_id"] = model_id
        uploaded["filename"] = filename
        uploaded["csv"] = file_obj.read().decode("utf-8")
        return {"dataset_id": "ds-1"}
    created = {}
    def _fake_create_job(settings, model_id, dataset_id, *, history_window, test_fraction, seed, **_kw):
        created.update(model_id=model_id, dataset_id=dataset_id, history_window=history_window)
        return {"job_id": "job-1", "model_id": model_id, "dataset_id": dataset_id, "status": "QUEUED"}

    monkeypatch.setattr(training_routes, "upload_training_dataset", _fake_upload)
    monkeypatch.setattr(training_routes, "create_training_job", _fake_create_job)

    res = client.post("/training/jobs", json={
        "model_id": "model-x",
        "recording_ids": [rec1["id"], rec2["id"]],
        "mapping": {"outdoor_air_temp_c": p1_id, "supply_air_temp_c": p2_id},
        # Regression now resolves a semantic contract for its inputs too -- these test
        # points aren't Brick-classified, so an explicit choice is required.
        "feature_choices": {"outdoor_air_temp_c": {"kind": "device_specific"}},
        "history_window": 1,
    })

    assert res.status_code == 201
    assert res.json() == {
        "job_id": "job-1", "model_id": "model-x", "dataset_id": "ds-1", "status": "QUEUED",
    }
    # Direction-prefixed column names, resolved from the model's own
    # catalog metadata (input_/output_), not trusted from the request body.
    assert "input_outdoor_air_temp_c" in uploaded["csv"]
    assert "output_supply_air_temp_c" in uploaded["csv"]
    assert created["dataset_id"] == "ds-1"
    assert created["history_window"] == 1


def test_create_job_response_and_dataset_carry_no_tag_derived_column(client, database, monkeypatch):
    """Tags are metadata only (see training_export.py's own module
    docstring) -- two recordings with different `tags` must NOT produce an
    input_category (or any other tag-derived) CSV column, and the
    job-creation response must not carry a category_codes key at all. This
    is the retirement of the old single-`category` -> input_category
    behavior, not an adaptation of it to tags."""
    device = _make_device(client, instance=9953, name="Tags Route Device")
    p1 = _make_point(database, device["id"], instance=0, name="Outdoor Air Temp")
    p2 = _make_point(database, device["id"], instance=1, name="Supply Air Temp")

    rec1 = database.create_recording(device["id"], {
        "name": "Rec A", "point_ids": [p1["id"], p2["id"]], "tags": ["hot-day"],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    rp1 = {p["object_name"]: p["id"] for p in rec1["points"]}
    database.add_recording_sample(rec1["id"], {rp1["Outdoor Air Temp"]: {"value": 10.0}, rp1["Supply Air Temp"]: {"value": 18.0}})

    rec2 = database.create_recording(device["id"], {
        "name": "Rec B", "point_ids": [p1["id"], p2["id"]], "tags": ["fan-test"],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    rp2 = {p["object_name"]: p["id"] for p in rec2["points"]}
    database.add_recording_sample(rec2["id"], {rp2["Outdoor Air Temp"]: {"value": 99.0}, rp2["Supply Air Temp"]: {"value": 22.0}})

    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    uploaded = {}
    def _fake_upload(settings, model_id, filename, file_obj):
        uploaded["csv"] = file_obj.read().decode("utf-8")
        return {"dataset_id": "ds-1"}
    monkeypatch.setattr(training_routes, "upload_training_dataset", _fake_upload)
    monkeypatch.setattr(
        training_routes, "create_training_job",
        lambda settings, model_id, dataset_id, *, history_window, test_fraction, seed, **_kw: {
            "job_id": "job-1", "model_id": model_id, "dataset_id": dataset_id, "status": "QUEUED",
        },
    )

    res = client.post("/training/jobs", json={
        "model_id": "model-x",
        "recording_ids": [rec1["id"], rec2["id"]],
        "mapping": {"outdoor_air_temp_c": p1["id"], "supply_air_temp_c": p2["id"]},
        "feature_choices": {"outdoor_air_temp_c": {"kind": "device_specific"}},
    })

    assert res.status_code == 201
    assert "input_category" not in uploaded["csv"]
    assert "category_codes" not in res.json()


# ── constant-sourced inputs (migration 024) ─────────────────────────────────

def test_create_job_folds_in_constant_sourced_input_automatically(client, database, monkeypatch):
    """The caller's own mapping only has the output point-mapped (exactly
    what the frontend sends today -- mappingPayload only ever includes
    variables with a real object_id) -- the route must independently derive
    the constant-sourced input from the recordings' own snapshots and still
    succeed, without the caller ever naming it."""
    rec1, rec2, _p1_id, p2_id = _two_recordings_of_same_device(client, database)
    database.add_recording_input_constants(rec1["id"], {"outdoor_air_temp_c": 30.0})
    database.add_recording_input_constants(rec2["id"], {"outdoor_air_temp_c": 30.0})
    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    uploaded = {}
    def _fake_upload(settings, model_id, filename, file_obj):
        uploaded["csv"] = file_obj.read().decode("utf-8")
        return {"dataset_id": "ds-1"}
    monkeypatch.setattr(training_routes, "upload_training_dataset", _fake_upload)
    monkeypatch.setattr(
        training_routes, "create_training_job",
        lambda settings, model_id, dataset_id, *, history_window, test_fraction, seed, **_kw: {
            "job_id": "job-1", "model_id": model_id, "dataset_id": dataset_id, "status": "QUEUED",
        },
    )

    res = client.post("/training/jobs", json={
        "model_id": "model-x",
        "recording_ids": [rec1["id"], rec2["id"]],
        "mapping": {"supply_air_temp_c": p2_id},  # output only -- no input named at all
    })

    assert res.status_code == 201, res.text
    assert "input_outdoor_air_temp_c" in uploaded["csv"]
    rows = [line.split(",") for line in uploaded["csv"].strip().splitlines()]
    header = rows[0]
    col = header.index("input_outdoor_air_temp_c")
    # Same literal value on every data row -- that's what "constant" means.
    for row in rows[1:]:
        assert row[col] == "30.0"


def test_create_job_skips_recording_missing_constant_snapshot(client, database, monkeypatch):
    """Only rec1 snapshotted the constant -- rec2 must contribute zero rows
    (same "whole recording dropped" rule a missing point already gets),
    not silently use rec1's value for rec2's rows too."""
    rec1, rec2, _p1_id, p2_id = _two_recordings_of_same_device(client, database)
    database.add_recording_input_constants(rec1["id"], {"outdoor_air_temp_c": 30.0})
    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    uploaded = {}
    def _fake_upload(settings, model_id, filename, file_obj):
        uploaded["csv"] = file_obj.read().decode("utf-8")
        return {"dataset_id": "ds-1"}
    monkeypatch.setattr(training_routes, "upload_training_dataset", _fake_upload)
    monkeypatch.setattr(
        training_routes, "create_training_job",
        lambda settings, model_id, dataset_id, *, history_window, test_fraction, seed, **_kw: {
            "job_id": "job-1", "model_id": model_id, "dataset_id": dataset_id, "status": "QUEUED",
        },
    )

    res = client.post("/training/jobs", json={
        "model_id": "model-x",
        "recording_ids": [rec1["id"], rec2["id"]],
        "mapping": {"supply_air_temp_c": p2_id},
    })

    assert res.status_code == 201, res.text
    rows = [line.split(",") for line in uploaded["csv"].strip().splitlines()]
    trajectory_ids = {row[0] for row in rows[1:]}
    assert trajectory_ids == {str(rec1["id"])}


def test_create_job_ignores_constant_when_variable_already_point_mapped(client, database, monkeypatch):
    """A variable explicitly point-mapped by the caller must not also get a
    constant column injected -- the two are mutually exclusive per
    variable, and the point-mapped value is the one actually used."""
    rec1, rec2, p1_id, p2_id = _two_recordings_of_same_device(client, database)
    database.add_recording_input_constants(rec1["id"], {"outdoor_air_temp_c": 999.0})
    database.add_recording_input_constants(rec2["id"], {"outdoor_air_temp_c": 999.0})
    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    uploaded = {}
    def _fake_upload(settings, model_id, filename, file_obj):
        uploaded["csv"] = file_obj.read().decode("utf-8")
        return {"dataset_id": "ds-1"}
    monkeypatch.setattr(training_routes, "upload_training_dataset", _fake_upload)
    monkeypatch.setattr(
        training_routes, "create_training_job",
        lambda settings, model_id, dataset_id, *, history_window, test_fraction, seed, **_kw: {
            "job_id": "job-1", "model_id": model_id, "dataset_id": dataset_id, "status": "QUEUED",
        },
    )

    res = client.post("/training/jobs", json={
        "model_id": "model-x",
        "recording_ids": [rec1["id"], rec2["id"]],
        "mapping": {"outdoor_air_temp_c": p1_id, "supply_air_temp_c": p2_id},
        "feature_choices": {"outdoor_air_temp_c": {"kind": "device_specific"}},
    })

    assert res.status_code == 201, res.text
    header = uploaded["csv"].splitlines()[0].split(",")
    # Exactly one input_outdoor_air_temp_c column -- point-sourced, not
    # duplicated by the (ignored) constant snapshot.
    assert header.count("input_outdoor_air_temp_c") == 1
    rows = [line.split(",") for line in uploaded["csv"].strip().splitlines()[1:]]
    col = header.index("input_outdoor_air_temp_c")
    # Real recorded point values (10.0/20.0 from _two_recordings_of_same_device),
    # never the 999.0 constant snapshot.
    assert all(row[col] in ("10.0", "20.0") for row in rows)


# ── job history ─────────────────────────────────────────────────────────

def test_list_jobs_relays_sorted_newest_first(client, monkeypatch):
    monkeypatch.setattr(
        training_routes, "list_training_jobs",
        lambda settings, model_id: {"jobs": [
            {"job_id": "job-old", "model_id": model_id, "status": "COMPLETED", "created_at": 100},
            {"job_id": "job-new", "model_id": model_id, "status": "FAILED", "created_at": 300},
            {"job_id": "job-mid", "model_id": model_id, "status": "COMPLETED", "created_at": 200},
        ]},
    )
    res = client.get("/training/jobs", params={"model_id": "model-x"})
    assert res.status_code == 200
    assert [j["job_id"] for j in res.json()] == ["job-new", "job-mid", "job-old"]


# ── job status/results/cancel relays ───────────────────────────────────────

def test_get_job_relays(client, monkeypatch):
    monkeypatch.setattr(
        training_routes, "get_training_job",
        lambda settings, model_id, job_id: {"job_id": job_id, "model_id": model_id, "status": "RUNNING"},
    )
    res = client.get("/training/jobs/job-1", params={"model_id": "model-x"})
    assert res.status_code == 200
    assert res.json()["status"] == "RUNNING"


def test_get_results_relays(client, monkeypatch):
    monkeypatch.setattr(
        training_routes, "get_training_job_results",
        lambda settings, model_id, job_id: {"job_id": job_id, "mean_r2": 0.83},
    )
    res = client.get("/training/jobs/job-1/results", params={"model_id": "model-x"})
    assert res.status_code == 200
    assert res.json()["mean_r2"] == 0.83


def test_cancel_job_relays(client, monkeypatch):
    monkeypatch.setattr(
        training_routes, "cancel_training_job",
        lambda settings, model_id, job_id: {"job_id": job_id, "status": "CANCELLED"},
    )
    res = client.post("/training/jobs/job-1/cancel", params={"model_id": "model-x"})
    assert res.status_code == 200
    assert res.json()["status"] == "CANCELLED"


def test_promote_job_relays_with_optional_overrides(client, monkeypatch):
    captured = {}
    def _fake_promote(settings, model_id, job_id, *, slug=None, label=None):
        captured.update(model_id=model_id, job_id=job_id, slug=slug, label=label)
        return {"id": "new-model-id", "slug": slug or "auto-slug", "label": label or "auto-label", "runtime_type": "onnx"}
    monkeypatch.setattr(training_routes, "promote_training_job", _fake_promote)

    res = client.post(
        "/training/jobs/job-1/promote", params={"model_id": "model-x"},
        json={"slug": "My-Slug", "label": "My Label"},
    )
    assert res.status_code == 201
    assert res.json()["id"] == "new-model-id"
    assert captured == {"model_id": "model-x", "job_id": "job-1", "slug": "My-Slug", "label": "My Label"}


def test_promote_job_relays_with_no_overrides(client, monkeypatch):
    monkeypatch.setattr(
        training_routes, "promote_training_job",
        lambda settings, model_id, job_id, *, slug=None, label=None: {
            "id": "new-model-id", "slug": slug, "label": label, "runtime_type": "onnx",
        },
    )
    res = client.post("/training/jobs/job-1/promote", params={"model_id": "model-x"}, json={})
    assert res.status_code == 201
    body = res.json()
    assert body["slug"] is None
    assert body["label"] is None
