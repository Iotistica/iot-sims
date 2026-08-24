"""HTTP-level tests for POST /simulation/models/mapping-suggestions and
POST /simulation/models/mapping-suggestions/ai -- the Auto Map endpoints
backing the Simulation Model drawer. Side-effect-free suggestion
generation (mirrors tests/test_semantic_suggestions_api.py's conventions),
plus the AI fallback's shortlist-only guarantee.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.simulation.mapping_ai_suggestions import AiMappingSuggestion

# Real catalog GUID for the remote runtime's SimpleVAVZone model -- these
# tests reach the actual FMU runtime (see get_runtime_settings's
# fmu_runtime_url default), so model_type must be an id that runtime's
# catalog genuinely has. Used to be the pre-GUID id "simple_vav_zone_fmu",
# which only resolved via LEGACY_MODEL_IDS's alias table; now that that
# table is gone (removed once nothing live still needed the old-id ->
# GUID upgrade), tests must use the real id directly like every other
# caller already does.
SIMPLE_VAV_ZONE_MODEL_TYPE = "b76aae8c-ecfe-44f2-b053-8b005c8ae2ed"


def _set_equipment_type(client, device: dict, equipment_type: str) -> dict:
    resp = client.put(f"/devices/{device['id']}", json={
        "device_instance": device["device_instance"], "name": device["name"],
        "description": device.get("description", ""), "vendor_name": device.get("vendor_name", "Iotistica"),
        "model_name": device.get("model_name", "BACnet Simulator"), "enabled": device.get("enabled", True),
        "firmware_revision": device.get("firmware_revision", "N/A"),
        "protocol_revision": device.get("protocol_revision", 22),
        "max_apdu_length_accepted": device.get("max_apdu_length_accepted", 1024),
        "segmentation_supported": device.get("segmentation_supported", "segmented-both"),
        "location_id": device.get("location_id"), "equipment_type": equipment_type,
        "can_receive_event_notifications": device.get("can_receive_event_notifications"),
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _equipment_entity_id(database, device_id: int) -> int:
    entities = database.get_semantic_entities(device_id=device_id, entity_kind="equipment")
    assert entities
    return entities[0]["id"]


def _make_ahu_vav_topology(client, database):
    ahu = client.post("/devices", json={"device_instance": 3001, "name": "AHU-API"}).json()
    ahu = _set_equipment_type(client, ahu, "Air_Handling_Unit")
    ahu_sat = client.post(f"/devices/{ahu['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
    }).json()
    client.put(f"/devices/{ahu['id']}/objects/{ahu_sat['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
        "point_type": "Supply_Air_Temperature_Sensor",
    })

    vav = client.post("/devices", json={"device_instance": 3002, "name": "VAV-API"}).json()
    vav = _set_equipment_type(client, vav, "Variable_Air_Volume_Box")

    client.post("/semantic-relationships", json={
        "source_entity_id": _equipment_entity_id(database, ahu["id"]),
        "predicate": "feeds",
        "target_entity_id": _equipment_entity_id(database, vav["id"]),
    })
    return ahu, ahu_sat, vav


def test_mapping_suggestions_returns_entry_per_variable(client):
    device = client.post("/devices", json={"device_instance": 3003, "name": "VAV-Plain"}).json()

    resp = client.post("/simulation/models/mapping-suggestions", json={
        "model_type": SIMPLE_VAV_ZONE_MODEL_TYPE, "provider_type": "fmu",
        "created_from_device_id": device["id"],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "variables" in body
    assert len(body["variables"]) > 0
    for entry in body["variables"]:
        assert "variable" in entry and "confidence" in entry and "equipment_scope_used" in entry


def test_mapping_suggestions_causes_zero_db_mutation(client, database):
    device = client.post("/devices", json={"device_instance": 3004, "name": "VAV-ReadOnly"}).json()
    before_device = database.get_device(device["id"])
    before_objects = database.get_objects(device["id"])

    client.post("/simulation/models/mapping-suggestions", json={
        "model_type": "simple_vav_zone_fmu", "provider_type": "fmu", "created_from_device_id": device["id"],
    })

    assert database.get_device(device["id"]) == before_device
    assert database.get_objects(device["id"]) == before_objects


def test_mapping_suggestions_unknown_model_type_404(client):
    resp = client.post("/simulation/models/mapping-suggestions", json={
        "model_type": "does-not-exist", "provider_type": "fmu", "created_from_device_id": None,
    })
    assert resp.status_code == 404


def test_topology_suggestion_via_api(client, database):
    ahu, ahu_sat, vav = _make_ahu_vav_topology(client, database)

    resp = client.post("/simulation/models/mapping-suggestions", json={
        "model_type": "simple_vav_zone_fmu", "provider_type": "fmu", "created_from_device_id": vav["id"],
    })
    assert resp.status_code == 200
    body = resp.json()
    entry = next(v for v in body["variables"] if v["variable"] == "supply_air_temp_c")

    assert entry["suggested_point_id"] == ahu_sat["id"]
    assert entry["equipment_scope_used"] == "upstream"
    assert entry["related_equipment_name"] == "AHU-API"
    assert any("Upstream" in r for r in entry["reasons"])


def test_mapping_suggestion_ai_success_uses_relationship_aware_shortlist(client, database):
    ahu, ahu_sat, vav = _make_ahu_vav_topology(client, database)

    captured = {}

    def _fake_suggest(client_arg, **kwargs):
        captured["candidates"] = kwargs["candidates"]
        captured["relationship_context"] = kwargs["relationship_context"]
        return AiMappingSuggestion(point_id=ahu_sat["id"], confidence="high", reason="Matches upstream AHU SAT.")

    with patch("src.api.routers.simulation.AzureStructuredClient") as mock_client_cls, \
         patch("src.api.routers.simulation.suggest_point_for_variable_via_ai", side_effect=_fake_suggest):
        mock_client_cls.return_value = object()
        resp = client.post("/simulation/models/mapping-suggestions/ai", json={
            "model_type": "simple_vav_zone_fmu", "variable": "supply_air_temp_c", "created_from_device_id": vav["id"],
        })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "ai"
    assert body["suggested_point_id"] == ahu_sat["id"]
    assert body["confidence"] == "high"
    assert any(c.id == ahu_sat["id"] for c in captured["candidates"])
    assert captured["relationship_context"] is not None


def test_mapping_suggestion_ai_rejects_out_of_shortlist_point(client, database):
    ahu, ahu_sat, vav = _make_ahu_vav_topology(client, database)

    def _fake_suggest(client_arg, **kwargs):
        return AiMappingSuggestion(point_id=999999, confidence="high", reason="Hallucinated point.")

    with patch("src.api.routers.simulation.AzureStructuredClient") as mock_client_cls, \
         patch("src.api.routers.simulation.suggest_point_for_variable_via_ai", side_effect=_fake_suggest):
        mock_client_cls.return_value = object()
        resp = client.post("/simulation/models/mapping-suggestions/ai", json={
            "model_type": "simple_vav_zone_fmu", "variable": "supply_air_temp_c", "created_from_device_id": vav["id"],
        })

    assert resp.status_code == 200
    body = resp.json()
    assert body["suggested_point_id"] is None
    assert body["confidence"] == "none"


def test_mapping_suggestion_ai_azure_failure_returns_502(client, database):
    ahu, ahu_sat, vav = _make_ahu_vav_topology(client, database)

    with patch("src.api.routers.simulation.AzureStructuredClient", side_effect=RuntimeError("no config")):
        resp = client.post("/simulation/models/mapping-suggestions/ai", json={
            "model_type": "simple_vav_zone_fmu", "variable": "supply_air_temp_c", "created_from_device_id": vav["id"],
        })

    assert resp.status_code == 502


def test_mapping_suggestion_ai_without_topology_still_returns_valid_shortlist(client, database):
    vav = client.post("/devices", json={"device_instance": 3005, "name": "VAV-NoTopology"}).json()
    vav = _set_equipment_type(client, vav, "Variable_Air_Volume_Box")
    local_sat = client.post(f"/devices/{vav['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
    }).json()

    captured = {}

    def _fake_suggest(client_arg, **kwargs):
        captured["candidates"] = kwargs["candidates"]
        return AiMappingSuggestion(point_id=local_sat["id"], confidence="medium", reason="Only point available.")

    with patch("src.api.routers.simulation.AzureStructuredClient") as mock_client_cls, \
         patch("src.api.routers.simulation.suggest_point_for_variable_via_ai", side_effect=_fake_suggest):
        mock_client_cls.return_value = object()
        resp = client.post("/simulation/models/mapping-suggestions/ai", json={
            "model_type": "simple_vav_zone_fmu", "variable": "supply_air_temp_c", "created_from_device_id": vav["id"],
        })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["equipment_scope_used"] == "fallback"
    assert any(c.id == local_sat["id"] for c in captured["candidates"])
    assert body["suggested_point_id"] == local_sat["id"]


def test_mapping_suggestion_ai_unknown_variable_400(client):
    device = client.post("/devices", json={"device_instance": 3006, "name": "VAV-BadVar"}).json()
    resp = client.post("/simulation/models/mapping-suggestions/ai", json={
        "model_type": SIMPLE_VAV_ZONE_MODEL_TYPE, "variable": "not_a_real_variable", "created_from_device_id": device["id"],
    })
    assert resp.status_code == 400


def test_mapping_suggestion_ai_unknown_model_type_404(client):
    resp = client.post("/simulation/models/mapping-suggestions/ai", json={
        "model_type": "does-not-exist", "variable": "x", "created_from_device_id": None,
    })
    assert resp.status_code == 404
