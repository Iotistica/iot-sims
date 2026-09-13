"""The provider-neutral semantic signal in the Auto-Map scorer
(src/simulation/mapping/suggestions.py). Two differently / opaquely named points with the
same semantic identity must both out-score name similarity -- meaning, not the vendor
point name, is authoritative.

Uses Database.create_object + explicit classification directly (this dev environment can't
import objects_router -- see test_semantic_feature_contract.py's header for why)."""
from __future__ import annotations

from src.semantics.contract import SemanticContract
from src.simulation.mapping.suggestions import suggest_mapping_for_variable
from src.simulation.models.registry import VariableDefinition


def _make_device(client, *, instance, name, equipment_type=None):
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
        "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal",
        "point_type": None,
    })


def _classify(database, object_id, device_id, brick_class):
    top = next(
        e for e in database.get_semantic_entities(device_id=device_id, entity_kind="equipment")
        if not database.get_related_entities(e["id"], "isPartOf", direction="out")
    )
    entity = database.create_semantic_entity(
        name=f"e-{object_id}", brick_class=brick_class, entity_kind="point", object_id=object_id,
    )
    database.create_semantic_relationship(entity["id"], "isPointOf", top["id"])


_SAT_VARIABLE = VariableDefinition(
    "supply_air_temp_c", "Supply Air Temperature", "input", unit="°C",
    semantic=SemanticContract(id="supply_air_temperature", role="sensor"),
)


def test_opaque_named_point_wins_on_semantic_identity(client, database):
    rtu = _make_device(client, instance=7100, name="RTU-A", equipment_type="Rooftop_Unit")
    # a point whose NAME has zero useful tokens, but is classified correctly
    opaque = _make_point(database, rtu["id"], instance=1, name="AI_07")
    _classify(database, opaque["id"], rtu["id"], "Supply_Air_Temperature_Sensor")
    # a decoy whose name screams "supply air temp" but is a different class
    decoy = _make_point(database, rtu["id"], instance=2, name="Supply Air Temperature Reading")
    _classify(database, decoy["id"], rtu["id"], "Return_Air_Temperature_Sensor")

    suggestion = suggest_mapping_for_variable(_SAT_VARIABLE, rtu["id"], database)

    assert suggestion.suggested_point_id == opaque["id"]
    assert suggestion.confidence in ("high", "medium")
    assert any("Semantic identity match" in r for r in suggestion.reasons)


def test_two_differently_named_points_same_identity_are_interchangeable(client, database):
    rtu = _make_device(client, instance=7101, name="RTU-B", equipment_type="Rooftop_Unit")
    a = _make_point(database, rtu["id"], instance=1, name="SAT")
    b = _make_point(database, rtu["id"], instance=2, name="AHU1_SA_TEMP")
    _classify(database, a["id"], rtu["id"], "Supply_Air_Temperature_Sensor")
    _classify(database, b["id"], rtu["id"], "Supply_Air_Temperature_Sensor")

    suggestion = suggest_mapping_for_variable(_SAT_VARIABLE, rtu["id"], database)
    # both are semantically exact -> ambiguous -> engine surfaces both, picks neither
    # confidently (score-tied). Either both are candidates, or one is the pick and the
    # other is an alternative -- what matters is both are present and both are semantic.
    ids = {suggestion.suggested_point_id, *(alt.point_id for alt in suggestion.alternatives)}
    assert {a["id"], b["id"]} <= ids


def test_no_semantic_contract_is_byte_identical_to_today(client, database):
    rtu = _make_device(client, instance=7102, name="RTU-C", equipment_type="Rooftop_Unit")
    p = _make_point(database, rtu["id"], instance=1, name="SAT")
    _classify(database, p["id"], rtu["id"], "Supply_Air_Temperature_Sensor")

    no_semantic = VariableDefinition(
        "supply_air_temp_c", "Supply Air Temperature", "input", unit="°C",
    )
    suggestion = suggest_mapping_for_variable(no_semantic, rtu["id"], database)
    assert not any("Semantic identity" in r for r in suggestion.reasons)
    # a classified point still surfaces (name/unit), just never as a "semantic" match
    assert suggestion.suggested_point_id == p["id"]
