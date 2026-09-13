"""src/semantics/feature_contract.py -- the Brick-portable classifier feature identity resolver.
Devices are created via the real HTTP client (devices_router imports fine); objects and
sub-equipment go through Database.create_object/create_semantic_entity/create_semantic_relationship
directly rather than POST /devices/{id}/objects -- this Windows dev environment can't import
objects_router in isolation (external_objects.py -> discovery.py -> `import fcntl`), same
established convention as test_object_linking.py/test_classifier_state_text.py."""
from __future__ import annotations

from src.semantics.feature_contract import (
    ResolvedBrickPoint,
    build_semantic_feature_contract,
    compute_portability,
    match_brick_identity_in_candidates,
    resolve_point_semantic_identity,
    resolve_semantic_contract_against_device,
)
from src.semantics.resolution import ResolutionCandidate


def _make_device(client, *, instance: int, name: str, equipment_type: str | None = None) -> dict:
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": instance, "name": name}).json()
    if equipment_type:
        resp = client.put(f"/devices/{device['id']}", json={
            "protocol": "bacnet", "device_instance": instance, "name": name, "equipment_type": equipment_type,
        })
        assert resp.status_code == 200, resp.text
        device = resp.json()
    return device


def _make_point(database, device_id: int, *, instance: int, name: str, units: str = "percent") -> dict:
    return database.create_object(device_id, {
        "object_type": "analog-input", "object_instance": instance, "name": name, "units": units,
        "behavior": "constant", "behavior_params": '{"value":0}', "enabled": 1,
        "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal",
        "point_type": None,
    })


def _classify_point_directly(database, object_id: int, point_class: str) -> dict:
    """Classifies a point WITHOUT the automatic device-level isPointOf auto-link (see
    create_object's own sync -- that always links to the device's top-level equipment) --
    used when the test wants to attach the point to a specific sub-equipment entity instead."""
    return database.create_semantic_entity(
        name=f"entity-for-{object_id}", brick_class=point_class, entity_kind="point", object_id=object_id,
    )


def _make_sub_equipment(database, device_id: int, *, brick_class: str, parent_entity_id: int) -> dict:
    entity = database.create_semantic_entity(
        name=brick_class, brick_class=brick_class, entity_kind="equipment", device_id=device_id,
    )
    database.create_semantic_relationship(entity["id"], "isPartOf", parent_entity_id)
    return entity


def _top_level_equipment_entity(database, device_id: int) -> dict:
    """Sub-equipment (Supply_Fan/Return_Fan) shares the SAME device_id as the device's own
    top-level equipment entity (see src/semantics/mirror.py's find_direct_equipment_entity) --
    distinguished only by NOT being the source of an outgoing isPartOf edge, same filter
    SemanticResolver.get_equipment_entity uses."""
    for entity in database.get_semantic_entities(device_id=device_id, entity_kind="equipment"):
        if not database.get_related_entities(entity["id"], "isPartOf", direction="out"):
            return entity
    raise AssertionError("no top-level equipment entity found")


def test_resolve_point_semantic_identity_top_level_point(client, database):
    device = _make_device(client, instance=8801, name="VAV-1", equipment_type="Variable_Air_Volume_Box")
    point = _make_point(database, device["id"], instance=1, name="Zone-Temp", units="degrees-celsius")
    top = _top_level_equipment_entity(database, device["id"])
    point_entity = _classify_point_directly(database, point["id"], "Zone_Air_Temperature_Sensor")
    database.create_semantic_relationship(point_entity["id"], "isPointOf", top["id"])

    resolved = resolve_point_semantic_identity(database, point["id"])
    assert resolved is not None
    assert resolved.point_class == "Zone_Air_Temperature_Sensor"
    assert resolved.equipment_path == ()
    assert resolved.unit == "degrees-celsius"


def test_resolve_point_semantic_identity_sub_equipment_point(client, database):
    device = _make_device(client, instance=8802, name="AHU-1", equipment_type="Air_Handling_Unit")
    top = _top_level_equipment_entity(database, device["id"])
    supply_fan = _make_sub_equipment(database, device["id"], brick_class="Supply_Fan", parent_entity_id=top["id"])

    point = _make_point(database, device["id"], instance=1, name="Supply-Fan-Speed")
    point_entity = _classify_point_directly(database, point["id"], "Fan_Speed_Command")
    database.create_semantic_relationship(point_entity["id"], "isPointOf", supply_fan["id"])

    resolved = resolve_point_semantic_identity(database, point["id"])
    assert resolved is not None
    assert resolved.point_class == "Fan_Speed_Command"
    assert resolved.equipment_path == ("Supply_Fan",)


def test_resolve_point_semantic_identity_unclassified_point_is_none(client, database):
    device = _make_device(client, instance=8803, name="Unclassified-Device")
    point = _make_point(database, device["id"], instance=1, name="Raw-Point")
    assert resolve_point_semantic_identity(database, point["id"]) is None


def test_supply_and_return_fan_speed_commands_are_distinct(client, database):
    """The graph-context disambiguation this whole module exists for: two points sharing the
    identical point_class must resolve to non-colliding identities when they belong to different
    sub-equipment."""
    device = _make_device(client, instance=8804, name="AHU-2", equipment_type="Air_Handling_Unit")
    top = _top_level_equipment_entity(database, device["id"])
    supply_fan = _make_sub_equipment(database, device["id"], brick_class="Supply_Fan", parent_entity_id=top["id"])
    return_fan = _make_sub_equipment(database, device["id"], brick_class="Return_Fan", parent_entity_id=top["id"])

    supply_point = _make_point(database, device["id"], instance=1, name="Supply-Fan-Speed")
    supply_entity = _classify_point_directly(database, supply_point["id"], "Fan_Speed_Command")
    database.create_semantic_relationship(supply_entity["id"], "isPointOf", supply_fan["id"])

    return_point = _make_point(database, device["id"], instance=2, name="Return-Fan-Speed")
    return_entity = _classify_point_directly(database, return_point["id"], "Fan_Speed_Command")
    database.create_semantic_relationship(return_entity["id"], "isPointOf", return_fan["id"])

    supply_resolved = resolve_point_semantic_identity(database, supply_point["id"])
    return_resolved = resolve_point_semantic_identity(database, return_point["id"])
    assert supply_resolved.equipment_path == ("Supply_Fan",)
    assert return_resolved.equipment_path == ("Return_Fan",)
    assert (supply_resolved.point_class, supply_resolved.equipment_path) != \
           (return_resolved.point_class, return_resolved.equipment_path)


def test_match_brick_identity_in_candidates_tiers(client, database):
    device = _make_device(client, instance=8810, name="AHU-M", equipment_type="Air_Handling_Unit")
    top = _top_level_equipment_entity(database, device["id"])
    supply_fan = _make_sub_equipment(database, device["id"], brick_class="Supply_Fan", parent_entity_id=top["id"])

    # a live, fully-classified Supply_Fan/Fan_Speed_Command point
    live = _make_point(database, device["id"], instance=1, name="opaque_1")
    live_entity = _classify_point_directly(database, live["id"], "Fan_Speed_Command")
    database.create_semantic_relationship(live_entity["id"], "isPointOf", supply_fan["id"])

    live_cand = ResolutionCandidate(id=101, source_object_id=live["id"])
    snap_cand = ResolutionCandidate(id=102, representation_snapshot={"brick.point_class": "Fan_Speed_Command"})
    wrong_cand = ResolutionCandidate(id=103, representation_snapshot={"brick.point_class": "Fan_Status"})

    # path-requiring target: only the live-verified path counts as "exact"
    target = ResolvedBrickPoint("Fan_Speed_Command", ("Supply_Fan",), None)
    got = match_brick_identity_in_candidates(database, [live_cand, snap_cand, wrong_cand], target)
    assert got == {101: "exact", 102: "class_only", 103: "unresolved"}

    # no-path target: class match is enough, but only a live walk is "exact"
    target2 = ResolvedBrickPoint("Fan_Speed_Command", (), None)
    got2 = match_brick_identity_in_candidates(database, [live_cand, snap_cand], target2)
    assert got2 == {101: "exact", 102: "class_only"}


class _FullyMappedAhu:
    """Shared fixture: an AHU with a Zone Temp Sensor on the top-level equipment plus a
    Supply_Fan/Return_Fan pair -- used by both contract-building and binding tests below."""

    def __init__(self, client, database, *, instance: int, name: str):
        self.device = _make_device(client, instance=instance, name=name, equipment_type="Air_Handling_Unit")
        top = _top_level_equipment_entity(database, self.device["id"])
        supply_fan = _make_sub_equipment(database, self.device["id"], brick_class="Supply_Fan", parent_entity_id=top["id"])
        return_fan = _make_sub_equipment(database, self.device["id"], brick_class="Return_Fan", parent_entity_id=top["id"])

        self.zone_temp = _make_point(database, self.device["id"], instance=1, name="Zone-Temp", units="degrees-celsius")
        zt_entity = _classify_point_directly(database, self.zone_temp["id"], "Zone_Air_Temperature_Sensor")
        database.create_semantic_relationship(zt_entity["id"], "isPointOf", top["id"])

        self.supply_speed = _make_point(database, self.device["id"], instance=2, name="Supply-Fan-Speed")
        sp_entity = _classify_point_directly(database, self.supply_speed["id"], "Fan_Speed_Command")
        database.create_semantic_relationship(sp_entity["id"], "isPointOf", supply_fan["id"])

        self.unclassified = _make_point(database, self.device["id"], instance=3, name="Raw-Signal")


def test_build_contract_all_brick_resolves_without_any_choices(client, database):
    fixture = _FullyMappedAhu(client, database, instance=8810, name="AHU-3")
    mapping = {"zone_temp_c": fixture.zone_temp["id"], "supply_fan_speed_pct": fixture.supply_speed["id"]}

    result = build_semantic_feature_contract(database, mapping)

    assert result.ok
    assert result.undecided == []
    assert result.specs["zone_temp_c"]["identity_kind"] == "brick"
    assert result.specs["zone_temp_c"]["point_class"] == "Zone_Air_Temperature_Sensor"
    assert result.specs["supply_fan_speed_pct"]["equipment_path"] == ["Supply_Fan"]


def test_unresolvable_point_without_a_choice_is_undecided_not_blocked_by_default(client, database):
    """The core 'missing Brick coverage does not block users' property: an unclassified point with
    NO explicit feature_choices entry is reported as undecided (the caller can act on that), not
    silently defaulted and not a hard failure of the whole function."""
    fixture = _FullyMappedAhu(client, database, instance=8811, name="AHU-4")
    mapping = {"raw_signal": fixture.unclassified["id"]}

    result = build_semantic_feature_contract(database, mapping)

    assert not result.ok
    assert result.undecided == ["raw_signal"]
    assert "raw_signal" not in result.specs


def test_explicit_custom_choice_resolves_an_unclassified_point(client, database):
    fixture = _FullyMappedAhu(client, database, instance=8812, name="AHU-5")
    mapping = {"raw_signal": fixture.unclassified["id"]}

    result = build_semantic_feature_contract(
        database, mapping, feature_choices={"raw_signal": {"kind": "custom", "custom_label": "Zone_CO2_Level"}},
    )

    assert result.ok
    assert result.undecided == []
    spec = result.specs["raw_signal"]
    assert spec["identity_kind"] == "custom"
    assert spec["custom_label"] == "Zone_CO2_Level"
    assert spec["source_object_id"] == fixture.unclassified["id"]
    assert spec["source_device_id"] == fixture.device["id"]
    assert spec["bacnet_object_type"] == "analog-input"


def test_explicit_device_specific_choice_resolves_an_unclassified_point(client, database):
    fixture = _FullyMappedAhu(client, database, instance=8813, name="AHU-6")
    mapping = {"raw_signal": fixture.unclassified["id"]}

    result = build_semantic_feature_contract(
        database, mapping, feature_choices={"raw_signal": {"kind": "device_specific"}},
    )

    assert result.ok
    spec = result.specs["raw_signal"]
    assert spec["identity_kind"] == "device_specific"
    assert spec["source_object_id"] == fixture.unclassified["id"]
    assert "custom_label" not in spec


def test_duplicate_brick_identity_across_two_variables_is_ambiguous(client, database):
    fixture = _FullyMappedAhu(client, database, instance=8814, name="AHU-7")
    # Map the SAME supply-fan-speed point under two different variable names.
    mapping = {"var_a": fixture.supply_speed["id"], "var_b": fixture.supply_speed["id"]}

    result = build_semantic_feature_contract(database, mapping)

    assert not result.ok
    assert result.ambiguous_brick_pairs == [("var_a", "var_b")]


def test_duplicate_custom_label_across_two_variables_is_rejected(client, database):
    fixture = _FullyMappedAhu(client, database, instance=8815, name="AHU-8")
    other_point = database.create_object(fixture.device["id"], {
        "object_type": "analog-input", "object_instance": 9, "name": "Other-Raw", "units": "no-units",
        "behavior": "constant", "behavior_params": '{"value":0}', "enabled": 1,
        "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal", "point_type": None,
    })
    mapping = {"var_a": fixture.unclassified["id"], "var_b": other_point["id"]}

    result = build_semantic_feature_contract(
        database, mapping,
        feature_choices={
            "var_a": {"kind": "custom", "custom_label": "Same_Label"},
            "var_b": {"kind": "custom", "custom_label": "Same_Label"},
        },
    )

    assert not result.ok
    assert result.duplicate_custom_label_pairs == [("var_a", "var_b")]


def test_portability_all_brick_is_portable(client, database):
    fixture = _FullyMappedAhu(client, database, instance=8816, name="AHU-9")
    mapping = {"zone_temp_c": fixture.zone_temp["id"], "supply_fan_speed_pct": fixture.supply_speed["id"]}
    result = build_semantic_feature_contract(database, mapping)
    assert compute_portability(list(result.specs.values())) == "portable"


def test_portability_one_custom_feature_is_partial(client, database):
    fixture = _FullyMappedAhu(client, database, instance=8817, name="AHU-10")
    mapping = {"zone_temp_c": fixture.zone_temp["id"], "raw_signal": fixture.unclassified["id"]}
    result = build_semantic_feature_contract(
        database, mapping, feature_choices={"raw_signal": {"kind": "custom", "custom_label": "X"}},
    )
    assert compute_portability(list(result.specs.values())) == "partial"


def test_portability_any_device_specific_feature_dominates(client, database):
    fixture = _FullyMappedAhu(client, database, instance=8818, name="AHU-11")
    mapping = {
        "zone_temp_c": fixture.zone_temp["id"],
        "raw_signal_custom": fixture.unclassified["id"],
    }
    other_point = database.create_object(fixture.device["id"], {
        "object_type": "analog-input", "object_instance": 10, "name": "Other-Raw-2", "units": "no-units",
        "behavior": "constant", "behavior_params": '{"value":0}', "enabled": 1,
        "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal", "point_type": None,
    })
    mapping["raw_signal_devspecific"] = other_point["id"]
    result = build_semantic_feature_contract(
        database, mapping,
        feature_choices={
            "raw_signal_custom": {"kind": "custom", "custom_label": "X"},
            "raw_signal_devspecific": {"kind": "device_specific"},
        },
    )
    assert compute_portability(list(result.specs.values())) == "device_specific"


def test_binding_brick_feature_resolves_against_a_different_device_with_matching_classification(client, database):
    """The core portability property: the exact same semantic_features contract, resolved against
    TWO different devices with different object_ids but equivalent Brick classification, produces
    the same feature_name -> matched but different underlying object_id."""
    source = _FullyMappedAhu(client, database, instance=8820, name="AHU-Training")
    target = _FullyMappedAhu(client, database, instance=8821, name="AHU-Target")
    assert source.zone_temp["id"] != target.zone_temp["id"]

    mapping = {"zone_temp_c": source.zone_temp["id"], "supply_fan_speed_pct": source.supply_speed["id"]}
    contract = build_semantic_feature_contract(database, mapping)
    semantic_features = list(contract.specs.values())

    binding = resolve_semantic_contract_against_device(
        database, target.device["id"], semantic_features, training_device_id=source.device["id"],
    )

    assert binding.ok
    zone_feature_name = contract.specs["zone_temp_c"]["feature_name"]
    fan_feature_name = contract.specs["supply_fan_speed_pct"]["feature_name"]
    assert binding.bound[zone_feature_name] == target.zone_temp["id"]
    assert binding.bound[fan_feature_name] == target.supply_speed["id"]
    # Genuinely a different object on a different device -- not the training device's own id.
    assert binding.bound[zone_feature_name] != source.zone_temp["id"]


def test_binding_missing_brick_requirement_on_target_device(client, database):
    source = _FullyMappedAhu(client, database, instance=8822, name="AHU-Training-2")
    bare_target = _make_device(client, instance=8823, name="Bare-VAV", equipment_type="Variable_Air_Volume_Box")

    mapping = {"supply_fan_speed_pct": source.supply_speed["id"]}
    contract = build_semantic_feature_contract(database, mapping)

    binding = resolve_semantic_contract_against_device(
        database, bare_target["id"], list(contract.specs.values()), training_device_id=source.device["id"],
    )
    assert not binding.ok
    feature_name = contract.specs["supply_fan_speed_pct"]["feature_name"]
    assert feature_name in binding.missing


def test_binding_ambiguous_brick_requirement_on_target_device(client, database):
    """Two candidate points on the target satisfy the same requirement with no way to
    disambiguate -- must be reported ambiguous, never silently resolved to the first match."""
    source = _FullyMappedAhu(client, database, instance=8824, name="AHU-Training-3")
    target = _FullyMappedAhu(client, database, instance=8825, name="AHU-Target-2")
    top = _top_level_equipment_entity(database, target.device["id"])

    # A second, duplicate Zone_Air_Temperature_Sensor directly on the target's top-level equipment.
    duplicate_point = database.create_object(target.device["id"], {
        "object_type": "analog-input", "object_instance": 20, "name": "Zone-Temp-2", "units": "degrees-celsius",
        "behavior": "constant", "behavior_params": '{"value":0}', "enabled": 1,
        "number_of_states": 2, "reliability": "no-fault-detected", "polarity": "normal", "point_type": None,
    })
    dup_entity = _classify_point_directly(database, duplicate_point["id"], "Zone_Air_Temperature_Sensor")
    database.create_semantic_relationship(dup_entity["id"], "isPointOf", top["id"])

    mapping = {"zone_temp_c": source.zone_temp["id"]}
    contract = build_semantic_feature_contract(database, mapping)

    binding = resolve_semantic_contract_against_device(
        database, target.device["id"], list(contract.specs.values()), training_device_id=source.device["id"],
    )
    assert not binding.ok
    feature_name = contract.specs["zone_temp_c"]["feature_name"]
    assert feature_name in binding.ambiguous
    assert len(binding.ambiguous[feature_name]) == 2


def test_binding_custom_and_device_specific_features_never_resolve_on_a_different_device(client, database):
    source = _FullyMappedAhu(client, database, instance=8826, name="AHU-Training-4")
    target = _FullyMappedAhu(client, database, instance=8827, name="AHU-Target-3")

    mapping = {"raw_signal": source.unclassified["id"]}
    contract = build_semantic_feature_contract(
        database, mapping, feature_choices={"raw_signal": {"kind": "custom", "custom_label": "X"}},
    )

    binding = resolve_semantic_contract_against_device(
        database, target.device["id"], list(contract.specs.values()), training_device_id=source.device["id"],
    )
    assert not binding.ok
    feature_name = contract.specs["raw_signal"]["feature_name"]
    assert feature_name in binding.missing
    assert "not portable" in binding.missing[feature_name]


def test_binding_custom_and_device_specific_features_resolve_on_the_training_device_itself(client, database):
    source = _FullyMappedAhu(client, database, instance=8828, name="AHU-Training-5")
    mapping = {"raw_signal": source.unclassified["id"]}
    contract = build_semantic_feature_contract(
        database, mapping, feature_choices={"raw_signal": {"kind": "device_specific"}},
    )

    binding = resolve_semantic_contract_against_device(
        database, source.device["id"], list(contract.specs.values()), training_device_id=source.device["id"],
    )
    assert binding.ok
    feature_name = contract.specs["raw_signal"]["feature_name"]
    assert binding.bound[feature_name] == source.unclassified["id"]
