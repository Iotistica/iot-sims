"""Pure-engine tests for src/simulation/mapping_suggestions.py -- the
deterministic Auto Map scoring/candidate-discovery engine used by the
Simulation Model drawer's "Auto Map" action. Uses the `client`/`database`
fixtures only to build device/object/semantic-relationship fixtures
(exactly like tests/test_semantic_suggestions_api.py); the functions under
test are called directly, not through HTTP.
"""
from __future__ import annotations

from src.simulation.mapping_suggestions import (
    build_shortlist,
    confidence_for,
    discover_candidates,
    suggest_mapping_for_variable,
)
from src.simulation.models.registry import (
    MappingHints,
    ModelDefinition,
    VariableDefinition,
)

# MODEL_REGISTRY (a static local catalog) was removed 2026-08-25 once the FMU catalog moved to
# the model runtime's own dynamic metadata (see remote_catalog.py's definition_from_metadata) --
# nothing populates a local registry by this key anymore. This engine only exercises the pure
# scoring/candidate-discovery functions in mapping_suggestions.py, which take VariableDefinition
# objects directly, so a local fixture (matching SimpleVAVZone's real supply_air_temp_c/
# zone_temp_c inputs) is all these tests actually need -- no live catalog lookup required.
VAV_DEFINITION = ModelDefinition(
    model_type="simple_vav_zone_fmu",
    label="VAV",
    provider_type="fmu",
    description="VAV terminal FMU with external zone-temperature input, damper control, airflow, and hot-water reheat.",
    parameters=(),
    variables=(
        VariableDefinition(
            "supply_air_temp_c", "Supply Air Temperature", "input",
            unit="°C",
            suggested_point_types=("Supply_Air_Temperature_Sensor",),
            # A VAV's supply air physically comes from its upstream AHU --
            # test_topology_upstream_preference/test_missing_topology_graceful_fallback
            # both exercise this (upstream-preferring, with graceful fallback when no
            # `feeds` relationship exists), unlike zone_temp_c below (self-scope).
            mapping_hints=MappingHints(equipment_scope="upstream"),
        ),
        # zone_temp_c is a model OUTPUT (the VAV's computed zone temperature), not an input --
        # confirmed against the real SimpleVAVZone.fmu (test_vav_initialize_step checks it as
        # a step response output). Direction matters here specifically: output-ownership
        # exclusion (get_output_owners_by_point) only engages for direction="output" variables.
        VariableDefinition(
            "zone_temp_c", "Zone Temperature", "output",
            unit="°C",
            suggested_point_types=("Zone_Air_Temperature_Sensor",),
        ),
    ),
    factory=lambda parameters: None,
)
SAT_INPUT = next(v for v in VAV_DEFINITION.variables if v.name == "supply_air_temp_c")


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
    assert entities, f"no equipment semantic entity for device {device_id}"
    return entities[0]["id"]


def _make_ahu(client, instance: int, name: str) -> dict:
    device = client.post("/devices", json={"device_instance": instance, "name": name}).json()
    device = _set_equipment_type(client, device, "Air_Handling_Unit")
    client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
    })
    return device


def _make_vav(client, instance: int, name: str) -> dict:
    device = client.post("/devices", json={"device_instance": instance, "name": name}).json()
    device = _set_equipment_type(client, device, "Variable_Air_Volume_Box")
    return device


def test_brick_semantics_outranks_name_similarity_only(client, database):
    ahu = _make_ahu(client, 2001, "AHU-1")
    client.post(f"/devices/{ahu['id']}/objects", json={
        # No Brick class, but its name ("SupplyTemp") is at least as
        # name-similar to the variable as the classified "SAT" point.
        "object_type": "analog-input", "object_instance": 2, "name": "SupplyTemp", "units": "degrees-celsius",
    })
    sat = client.get(f"/devices/{ahu['id']}/objects").json()[0]
    client.put(f"/devices/{ahu['id']}/objects/{sat['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
        "point_type": "Supply_Air_Temperature_Sensor",
    })

    suggestion = suggest_mapping_for_variable(SAT_INPUT, ahu["id"], database)
    assert suggestion.suggested_point_id == sat["id"]
    assert any(r.startswith("Brick:") for r in suggestion.reasons)


def test_unit_conflict_lowers_score(client, database):
    ahu = _make_ahu(client, 2002, "AHU-2")
    sat = client.get(f"/devices/{ahu['id']}/objects").json()[0]
    client.put(f"/devices/{ahu['id']}/objects/{sat['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-fahrenheit",
        "point_type": "Supply_Air_Temperature_Sensor",
    })
    matched_units = suggest_mapping_for_variable(SAT_INPUT, ahu["id"], database)

    # Same point, but now with an incompatible (non-temperature) unit --
    # score must be strictly lower than the matched-unit case above.
    client.put(f"/devices/{ahu['id']}/objects/{sat['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "percent",
        "point_type": "Supply_Air_Temperature_Sensor",
    })
    conflicting_units = suggest_mapping_for_variable(SAT_INPUT, ahu["id"], database)

    assert conflicting_units.score < matched_units.score


def test_topology_upstream_preference(client, database):
    ahu = _make_ahu(client, 2003, "AHU-3")
    vav = _make_vav(client, 2004, "VAV-3")

    # VAV also has a local, equally-named "SAT" point -- without topology
    # awareness this would tie with the AHU's point.
    local_sat = client.post(f"/devices/{vav['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
    }).json()

    ahu_sat = client.get(f"/devices/{ahu['id']}/objects").json()[0]
    client.put(f"/devices/{ahu['id']}/objects/{ahu_sat['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
        "point_type": "Supply_Air_Temperature_Sensor",
    })
    client.put(f"/devices/{vav['id']}/objects/{local_sat['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
        "point_type": "Supply_Air_Temperature_Sensor",
    })

    ahu_entity_id = _equipment_entity_id(database, ahu["id"])
    vav_entity_id = _equipment_entity_id(database, vav["id"])
    resp = client.post("/semantic-relationships", json={
        "source_entity_id": ahu_entity_id, "predicate": "feeds", "target_entity_id": vav_entity_id,
    })
    assert resp.status_code == 201, resp.text

    suggestion = suggest_mapping_for_variable(SAT_INPUT, vav["id"], database)
    assert suggestion.equipment_scope_used == "upstream"
    assert suggestion.suggested_point_id == ahu_sat["id"]
    assert suggestion.related_equipment_name == "AHU-3"
    assert any("Upstream" in r for r in suggestion.reasons)


def test_topology_correct_equipment_wins_over_unrelated_same_class(client, database):
    serving_ahu = _make_ahu(client, 2005, "AHU-Serving")
    other_ahu = _make_ahu(client, 2006, "AHU-Unrelated")
    vav = _make_vav(client, 2007, "VAV-4")

    serving_sat = client.get(f"/devices/{serving_ahu['id']}/objects").json()[0]
    other_sat = client.get(f"/devices/{other_ahu['id']}/objects").json()[0]
    for device, point in ((serving_ahu, serving_sat), (other_ahu, other_sat)):
        client.put(f"/devices/{device['id']}/objects/{point['id']}", json={
            "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
            "point_type": "Supply_Air_Temperature_Sensor",
        })

    serving_entity_id = _equipment_entity_id(database, serving_ahu["id"])
    vav_entity_id = _equipment_entity_id(database, vav["id"])
    client.post("/semantic-relationships", json={
        "source_entity_id": serving_entity_id, "predicate": "feeds", "target_entity_id": vav_entity_id,
    })

    suggestion = suggest_mapping_for_variable(SAT_INPUT, vav["id"], database)
    assert suggestion.suggested_point_id == serving_sat["id"]
    assert suggestion.suggested_point_id != other_sat["id"]


def test_self_scope_preference(client, database):
    device_a = client.post("/devices", json={"device_instance": 2008, "name": "VAV-A"}).json()
    device_b = client.post("/devices", json={"device_instance": 2009, "name": "VAV-B"}).json()

    local_point = client.post(f"/devices/{device_a['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "ZoneTemp", "units": "degrees-celsius",
    }).json()
    client.put(f"/devices/{device_a['id']}/objects/{local_point['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "ZoneTemp", "units": "degrees-celsius",
        "point_type": "Zone_Air_Temperature_Sensor",
    })
    # Identical class/name on an unrelated device -- self-scope must not
    # prefer it just because scores tie without location awareness.
    other_point = client.post(f"/devices/{device_b['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "ZoneTemp", "units": "degrees-celsius",
    }).json()
    client.put(f"/devices/{device_b['id']}/objects/{other_point['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "ZoneTemp", "units": "degrees-celsius",
        "point_type": "Zone_Air_Temperature_Sensor",
    })

    setpoint_variable = next(v for v in VAV_DEFINITION.variables if v.name == "zone_temp_c")
    assert setpoint_variable.mapping_hints is None  # defaults to self scope

    suggestion = suggest_mapping_for_variable(setpoint_variable, device_a["id"], database)
    assert suggestion.equipment_scope_used == "self"
    assert suggestion.suggested_point_id == local_point["id"]


def test_missing_topology_graceful_fallback(client, database):
    vav = _make_vav(client, 2010, "VAV-Isolated")
    fleet_sat = client.post(f"/devices/{vav['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
    }).json()
    client.put(f"/devices/{vav['id']}/objects/{fleet_sat['id']}", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
        "point_type": "Supply_Air_Temperature_Sensor",
    })

    # No `feeds` relationship exists anywhere -- upstream resolution must
    # gracefully fall back to device/fleet-wide scoring, not return none.
    candidates, scope_used, related_name = discover_candidates(SAT_INPUT, vav["id"], database)
    assert scope_used == "fallback"
    assert related_name is None
    assert candidates

    suggestion = suggest_mapping_for_variable(SAT_INPUT, vav["id"], database)
    assert suggestion.equipment_scope_used == "fallback"
    assert suggestion.suggested_point_id == fleet_sat["id"]
    assert suggestion.confidence != "none"


def test_output_ownership_excluded(client, database):
    from src.simulation.model_store import create_simulation_model, ensure_simulation_model_schema

    vav = _make_vav(client, 2011, "VAV-5")
    zone_temp = client.post(f"/devices/{vav['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "ZoneTemp", "units": "degrees-celsius",
    }).json()
    zone_temp_variable = next(v for v in VAV_DEFINITION.variables if v.name == "zone_temp_c")

    ensure_simulation_model_schema(database)
    create_simulation_model(
        database, name="Owner Model", provider_type="fmu", model_type="simple_vav_zone_fmu", enabled=True,
        parameters={"runtime_url": "http://localhost:8000", "model": "SimpleVAVZone"}, created_from_device_id=vav["id"],
        mappings=[{"variable": "zone_temp_c", "direction": "output", "point_id": zone_temp["id"]}],
    )

    suggestion = suggest_mapping_for_variable(zone_temp_variable, vav["id"], database)
    assert suggestion.suggested_point_id != zone_temp["id"]


def test_confidence_thresholds_match_semantics_engine():
    assert confidence_for(0.80) == "high"
    assert confidence_for(0.55) == "medium"
    assert confidence_for(0.30) == "low"
    assert confidence_for(0.29) == "none"


def test_build_shortlist_returns_existing_points_only(client, database):
    ahu = _make_ahu(client, 2012, "AHU-Shortlist")
    candidates, scope_used, related_name = build_shortlist(SAT_INPUT, ahu["id"], database, limit=8)
    assert len(candidates) <= 8
    assert all(isinstance(c.id, int) for c in candidates)


def test_mapping_hints_default_to_self_scope():
    definition = VariableDefinition("x", "X", "input")
    assert definition.mapping_hints is None

    hints = MappingHints()
    assert hints.equipment_scope == "self"
    assert hints.preferred_equipment_types == ()
    assert hints.relationship is None
