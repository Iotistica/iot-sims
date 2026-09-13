"""GET /training/models/{model_id}/semantic-compatibility -- the attach-time "Equipment
Compatibility" check. Monkeypatches fetch_remote_metadata (no real iot-models server), same
pattern as test_classification_job_routes.py. Devices/points via client.post("/devices")/
database.create_object (objects_router unavailable in isolation, see that file's own docstring)."""
from __future__ import annotations

import src.api.routers.training_export as training_routes


def _make_device(client, *, instance: int, name: str, equipment_type: str | None = None) -> dict:
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": instance, "name": name}).json()
    if equipment_type:
        resp = client.put(f"/devices/{device['id']}", json={
            "protocol": "bacnet", "device_instance": instance, "name": name, "equipment_type": equipment_type,
        })
        assert resp.status_code == 200, resp.text
        device = resp.json()
    return device


def _make_point(database, device_id, *, instance, name, units="degrees-celsius"):
    return database.create_object(device_id, {
        "object_type": "analog-input", "object_instance": instance, "name": name, "units": units,
        "behavior": "constant", "behavior_params": '{"value":0}', "enabled": 1,
        "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal", "point_type": None,
    })


def _top_level_entity(database, device_id):
    for e in database.get_semantic_entities(device_id=device_id, entity_kind="equipment"):
        if not database.get_related_entities(e["id"], "isPartOf", direction="out"):
            return e
    raise AssertionError("no top-level equipment entity")


def _classify_point(database, object_id, point_class, owner_entity_id):
    entity = database.create_semantic_entity(
        name=f"entity-{object_id}", brick_class=point_class, entity_kind="point", object_id=object_id,
    )
    database.create_semantic_relationship(entity["id"], "isPointOf", owner_entity_id)
    return entity


def _patch_metadata(monkeypatch, metadata: dict):
    monkeypatch.setattr(
        training_routes, "fetch_remote_metadata", lambda settings, model_id: metadata,
    )


def test_brick_feature_resolves_against_the_target_device(client, database, monkeypatch):
    device = _make_device(client, instance=9980, name="Compat-VAV", equipment_type="Variable_Air_Volume_Box")
    top = _top_level_entity(database, device["id"])
    point = _make_point(database, device["id"], instance=1, name="Zone-Temp")
    _classify_point(database, point["id"], "Zone_Air_Temperature_Sensor", top["id"])

    _patch_metadata(monkeypatch, {
        "portability": "portable",
        "semantic_features": [
            {"feature_name": "zone_air_temperature_sensor", "identity_kind": "brick",
             "point_class": "Zone_Air_Temperature_Sensor", "equipment_path": []},
        ],
    })

    res = client.get(f"/training/models/model-x/semantic-compatibility?device_id={device['id']}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["bound"]["zone_air_temperature_sensor"] == point["id"]
    assert body["missing"] == {}


def test_missing_brick_requirement_blocks(client, database, monkeypatch):
    device = _make_device(client, instance=9981, name="Bare-Device")
    _patch_metadata(monkeypatch, {
        "portability": "portable",
        "semantic_features": [
            {"feature_name": "zone_air_temperature_sensor", "identity_kind": "brick",
             "point_class": "Zone_Air_Temperature_Sensor", "equipment_path": []},
        ],
    })

    res = client.get(f"/training/models/model-x/semantic-compatibility?device_id={device['id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert "zone_air_temperature_sensor" in body["missing"]


def test_convertible_unit_is_reported_but_does_not_block_readiness(client, database, monkeypatch):
    device = _make_device(client, instance=9990, name="Compat-VAV-F", equipment_type="Variable_Air_Volume_Box")
    top = _top_level_entity(database, device["id"])
    point = _make_point(database, device["id"], instance=1, name="Zone-Temp-F", units="degrees-fahrenheit")
    _classify_point(database, point["id"], "Zone_Air_Temperature_Sensor", top["id"])
    _patch_metadata(monkeypatch, {
        "portability": "portable",
        "ml_input_contract": [
            {"variable": "zone_temp_c", "feature_name": "zone_air_temperature_sensor",
             "identity_kind": "brick", "point_class": "Zone_Air_Temperature_Sensor",
             "equipment_path": [], "expected_unit": "degC"},
        ],
    })
    body = client.get(f"/training/models/model-x/semantic-compatibility?device_id={device['id']}").json()
    assert body["ok"] is True  # a required degF -> degC conversion does not block
    assert body["unit_conversions"] == [
        {"feature": "zone_air_temperature_sensor", "source_unit": "degrees-fahrenheit", "expected_unit": "degC"},
    ]
    assert body["unit_incompatible"] == [] and body["unit_unknown"] == []


def test_incompatible_unit_blocks_readiness(client, database, monkeypatch):
    device = _make_device(client, instance=9991, name="Compat-VAV-Pct", equipment_type="Variable_Air_Volume_Box")
    top = _top_level_entity(database, device["id"])
    point = _make_point(database, device["id"], instance=1, name="Damper-Pct", units="percent")
    _classify_point(database, point["id"], "Zone_Air_Temperature_Sensor", top["id"])
    _patch_metadata(monkeypatch, {
        "portability": "portable",
        "ml_input_contract": [
            {"variable": "zone_temp_c", "feature_name": "zone_air_temperature_sensor",
             "identity_kind": "brick", "point_class": "Zone_Air_Temperature_Sensor",
             "equipment_path": [], "expected_unit": "degC"},
        ],
    })
    body = client.get(f"/training/models/model-x/semantic-compatibility?device_id={device['id']}").json()
    assert body["ok"] is False
    assert body["unit_incompatible"][0]["feature"] == "zone_air_temperature_sensor"


def test_device_specific_feature_resolves_only_on_training_device(client, database, monkeypatch):
    training_device = _make_device(client, instance=9982, name="Training-Device")
    other_device = _make_device(client, instance=9983, name="Other-Device")
    point = _make_point(database, training_device["id"], instance=1, name="Raw-Point")

    metadata = {
        "portability": "device_specific",
        "semantic_features": [
            {"feature_name": "raw_point", "identity_kind": "device_specific",
             "source_device_id": training_device["id"], "source_object_id": point["id"]},
        ],
    }
    _patch_metadata(monkeypatch, metadata)

    ok_res = client.get(f"/training/models/model-x/semantic-compatibility?device_id={training_device['id']}")
    assert ok_res.json()["ok"] is True
    assert ok_res.json()["bound"]["raw_point"] == point["id"]

    blocked_res = client.get(f"/training/models/model-x/semantic-compatibility?device_id={other_device['id']}")
    assert blocked_res.json()["ok"] is False
    assert "not portable" in blocked_res.json()["missing"]["raw_point"]
