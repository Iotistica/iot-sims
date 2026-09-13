"""POST /training/jobs -- classification (task_type="classification") branch. Sibling to
test_training_export_routes.py's own regression-path tests, same monkeypatch-the-relay-functions
pattern; covers the classifier-specific validation (classifier_target required, output variables
rejected from the feature mapping, >=2 classes / >=2 labeled recordings) and that a valid job
forwards task_type/classifier_target to create_training_job."""
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
        model_type="model-x", label="Model X", provider_type="fmu", description="",
        parameters=(),
        variables=(
            VariableDefinition(name="zone_temp_c", label="Zone Temp", direction="input", unit="°C"),
            VariableDefinition(name="supply_air_temp_c", label="SAT", direction="output", unit="°C"),
        ),
        factory=lambda parameters: None,
    )


def _two_labeled_recordings(client, database, *, conditions):
    device = _make_device(client, instance=9960, name="Classification Route Device")
    point = _make_point(database, device["id"], instance=0, name="Zone Temp")

    recordings = []
    for i, condition in enumerate(conditions):
        rec = database.create_recording(device["id"], {
            "name": f"Run {i}", "point_ids": [point["id"]],
            "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
            "condition": condition,
        })
        database.add_recording_sample(rec["id"], {
            next(p["id"] for p in rec["points"] if p["source_object_id"] == point["id"]): {"value": 21.0},
        })
        recordings.append(rec)
    return recordings, point["id"]


def _two_labeled_recordings_with_device(client, database, *, conditions, instance=9970):
    device = _make_device(client, instance=instance, name="Semantic Route Device")
    point = _make_point(database, device["id"], instance=0, name="Zone Temp")
    recordings = []
    for i, condition in enumerate(conditions):
        rec = database.create_recording(device["id"], {
            "name": f"Run {i}", "point_ids": [point["id"]],
            "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
            "condition": condition,
        })
        database.add_recording_sample(rec["id"], {
            next(p["id"] for p in rec["points"] if p["source_object_id"] == point["id"]): {"value": 21.0},
        })
        recordings.append(rec)
    return device, recordings, point["id"]


def _patch_relays(monkeypatch, *, capture: dict):
    monkeypatch.setattr(
        training_routes, "get_remote_model_definition", lambda settings, mid: _fake_model_definition(),
    )
    monkeypatch.setattr(
        training_routes, "upload_training_dataset",
        lambda settings, model_id, filename, file_obj: {"dataset_id": "ds-1", "csv": file_obj.read().decode("utf-8")},
    )

    def _fake_create_job(settings, model_id, dataset_id, *, history_window, test_fraction, seed, **extra):
        capture["kwargs"] = extra
        return {"job_id": "job-1", "model_id": model_id, "dataset_id": dataset_id, "status": "QUEUED"}

    monkeypatch.setattr(training_routes, "create_training_job", _fake_create_job)


def test_classification_job_requires_classifier_target(client, database):
    recordings, point_id = _two_labeled_recordings(client, database, conditions=["normal", "fault"])
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "task_type": "classification",
        "recording_ids": [r["id"] for r in recordings],
        "mapping": {"zone_temp_c": point_id},
    })
    assert res.status_code == 422


def test_classification_job_rejects_output_variable_in_mapping(client, database, monkeypatch):
    recordings, point_id = _two_labeled_recordings(client, database, conditions=["normal", "fault"])
    _patch_relays(monkeypatch, capture={})
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "task_type": "classification", "classifier_target": "condition",
        "recording_ids": [r["id"] for r in recordings],
        "mapping": {"supply_air_temp_c": point_id},
    })
    assert res.status_code == 400
    assert "output variable" in res.json()["detail"]


def test_classification_job_rejects_fewer_than_two_classes(client, database, monkeypatch):
    recordings, point_id = _two_labeled_recordings(client, database, conditions=["normal", "normal"])
    _patch_relays(monkeypatch, capture={})
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "task_type": "classification", "classifier_target": "condition",
        "recording_ids": [r["id"] for r in recordings],
        "mapping": {"zone_temp_c": point_id},
    })
    assert res.status_code == 400
    assert "2 distinct condition values" in res.json()["detail"]


def test_classification_job_rejects_unlabeled_recordings(client, database, monkeypatch):
    recordings, point_id = _two_labeled_recordings(client, database, conditions=["normal", None])
    _patch_relays(monkeypatch, capture={})
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "task_type": "classification", "classifier_target": "condition",
        "recording_ids": [r["id"] for r in recordings],
        "mapping": {"zone_temp_c": point_id},
    })
    assert res.status_code == 400


def test_classification_job_succeeds_and_forwards_task_type(client, database, monkeypatch):
    recordings, point_id = _two_labeled_recordings(client, database, conditions=["normal", "fault"])
    capture: dict = {}
    _patch_relays(monkeypatch, capture=capture)

    res = client.post("/training/jobs", json={
        "model_id": "model-x", "task_type": "classification", "classifier_target": "condition",
        "recording_ids": [r["id"] for r in recordings],
        "mapping": {"zone_temp_c": point_id},
        # The point isn't Brick-classified in this fixture -- an explicit choice is required
        # (see test_semantic_feature_contract.py for the semantic-resolution behavior itself;
        # this test only cares that the job gets created and task_type/classifier_target/
        # semantic_features/equipment_class all forward correctly).
        "feature_choices": {"zone_temp_c": {"kind": "device_specific"}},
    })
    assert res.status_code == 201, res.text
    kwargs = capture["kwargs"]
    assert kwargs["task_type"] == "classification"
    assert kwargs["classifier_target"] == "condition"
    assert kwargs["semantic_features"][0]["identity_kind"] == "device_specific"
    assert kwargs["semantic_features"][0]["source_object_id"] == point_id


def _two_regression_recordings(client, database):
    device = _make_device(client, instance=9961, name="Regression Route Device")
    p1 = _make_point(database, device["id"], instance=0, name="Zone Temp")
    p2 = _make_point(database, device["id"], instance=1, name="Supply Air Temp")
    recs = []
    for i in range(2):
        rec = database.create_recording(device["id"], {
            "name": f"Run {i}", "point_ids": [p1["id"], p2["id"]],
            "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
        })
        database.add_recording_sample(rec["id"], {
            next(p["id"] for p in rec["points"] if p["source_object_id"] == p1["id"]): {"value": 21.0},
            next(p["id"] for p in rec["points"] if p["source_object_id"] == p2["id"]): {"value": 13.0},
        })
        recs.append(rec)
    return recs, p1["id"], p2["id"]


def test_regression_job_blocks_on_undecided_input(client, database, monkeypatch):
    """Regression now builds the same semantic feature contract classification does -- an
    unclassified input with no feature_choices entry blocks, naming the point."""
    recs, p1, p2 = _two_regression_recordings(client, database)
    _patch_relays(monkeypatch, capture={})
    res = client.post("/training/jobs", json={
        "model_id": "model-x",
        "recording_ids": [r["id"] for r in recs],
        "mapping": {"zone_temp_c": p1, "supply_air_temp_c": p2},
    })
    assert res.status_code == 400
    assert "zone_temp_c" in res.json()["detail"] and "Brick" in res.json()["detail"]


def test_regression_job_forwards_contract_but_not_classifier_kwargs(client, database, monkeypatch):
    """A resolved regression job forwards the semantic contract + sampling cadence, but
    never task_type / classifier_target."""
    recs, p1, p2 = _two_regression_recordings(client, database)
    capture: dict = {}
    _patch_relays(monkeypatch, capture=capture)
    res = client.post("/training/jobs", json={
        "model_id": "model-x",
        "recording_ids": [r["id"] for r in recs],
        "mapping": {"zone_temp_c": p1, "supply_air_temp_c": p2},
        "feature_choices": {"zone_temp_c": {"kind": "device_specific"}},
    })
    assert res.status_code == 201, res.text
    kwargs = capture["kwargs"]
    assert "task_type" not in kwargs and "classifier_target" not in kwargs
    assert kwargs["semantic_features"][0]["identity_kind"] == "device_specific"
    assert kwargs["semantic_features"][0]["expected_unit"] == "°C"
    assert kwargs["sample_interval_seconds"] == 5.0


def test_classification_job_blocks_on_undecided_point_naming_it(client, database, monkeypatch):
    """The exact 'missing Brick coverage does not block users, missing a DECISION does' property
    from the route layer: an unclassified point with no feature_choices entry blocks, naming the
    point, not a generic 'not portable' message."""
    device, recordings, point_id = _two_labeled_recordings_with_device(client, database, conditions=["normal", "fault"])
    _patch_relays(monkeypatch, capture={})

    res = client.post("/training/jobs", json={
        "model_id": "model-x", "task_type": "classification", "classifier_target": "condition",
        "recording_ids": [r["id"] for r in recordings],
        "mapping": {"zone_temp_c": point_id},
    })
    assert res.status_code == 400
    assert "zone_temp_c" in res.json()["detail"]
    assert "Brick" in res.json()["detail"]


def test_classification_job_succeeds_with_brick_classified_point_and_no_choices(client, database, monkeypatch):
    """The primary, common-case UX: a fully Brick-mapped point resolves automatically, no
    feature_choices needed at all."""
    device, recordings, point_id = _two_labeled_recordings_with_device(client, database, conditions=["normal", "fault"])
    top = database.get_semantic_entities(device_id=device["id"], entity_kind="equipment")
    if not top:
        resp = client.put(f"/devices/{device['id']}", json={
            "protocol": "bacnet", "device_instance": device["device_instance"], "name": device["name"],
            "equipment_type": "Variable_Air_Volume_Box",
        })
        assert resp.status_code == 200, resp.text
    top_entity = next(
        e for e in database.get_semantic_entities(device_id=device["id"], entity_kind="equipment")
        if not database.get_related_entities(e["id"], "isPartOf", direction="out")
    )
    point_entity = database.create_semantic_entity(
        name="zone-temp-entity", brick_class="Zone_Air_Temperature_Sensor", entity_kind="point", object_id=point_id,
    )
    database.create_semantic_relationship(point_entity["id"], "isPointOf", top_entity["id"])

    capture: dict = {}
    _patch_relays(monkeypatch, capture=capture)
    res = client.post("/training/jobs", json={
        "model_id": "model-x", "task_type": "classification", "classifier_target": "condition",
        "recording_ids": [r["id"] for r in recordings],
        "mapping": {"zone_temp_c": point_id},
    })
    assert res.status_code == 201, res.text
    spec = capture["kwargs"]["semantic_features"][0]
    assert spec["identity_kind"] == "brick"
    assert spec["point_class"] == "Zone_Air_Temperature_Sensor"
    assert capture["kwargs"]["equipment_class"] == "Variable_Air_Volume_Box"


def test_classification_job_succeeds_with_explicit_custom_choice(client, database, monkeypatch):
    device, recordings, point_id = _two_labeled_recordings_with_device(client, database, conditions=["normal", "fault"])
    capture: dict = {}
    _patch_relays(monkeypatch, capture=capture)

    res = client.post("/training/jobs", json={
        "model_id": "model-x", "task_type": "classification", "classifier_target": "condition",
        "recording_ids": [r["id"] for r in recordings],
        "mapping": {"zone_temp_c": point_id},
        "feature_choices": {"zone_temp_c": {"kind": "custom", "custom_label": "OA_Damper_Position"}},
    })
    assert res.status_code == 201, res.text
    spec = capture["kwargs"]["semantic_features"][0]
    assert spec["identity_kind"] == "custom"
    assert spec["custom_label"] == "OA_Damper_Position"
