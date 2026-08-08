"""Verifies seed_default() sets up the canonical Brick Core representation
for AHU supply/return fans and DALI lighting zones -- the live example
the Brick Core migration is built and tested against (see CLAUDE.md,
"Semantic model / Brick")."""
from __future__ import annotations


def test_ahu_fans_are_separate_subequipment_with_own_points(seeded_database):
    devices = seeded_database.get_devices()
    ahu1_id = next(d["id"] for d in devices if d["device_instance"] == 1003)

    ahu_entity = seeded_database.get_semantic_entities(
        device_id=ahu1_id, entity_kind="equipment", brick_class="Air_Handling_Unit"
    )[0]

    fans = seeded_database.get_related_entities(ahu_entity["id"], "isPartOf", direction="in")
    assert {f["brick_class"] for f in fans} == {"Supply_Fan", "Return_Fan"}

    for fan in fans:
        points = seeded_database.get_entity_points(fan["id"])
        assert {p["brick_class"] for p in points} == {"Fan_Status", "Fan_Speed_Command"}


def test_dali_zones_are_location_entities_with_own_points(seeded_database):
    devices = seeded_database.get_devices()
    gw1_id = next(d["id"] for d in devices if d["device_instance"] == 1501)

    zones = seeded_database.get_semantic_entities(
        device_id=gw1_id, entity_kind="location", brick_class="Lighting_Zone"
    )
    assert {z["local_slug"] for z in zones} == {"zone-a", "zone-b"}
    # Virtual location: device-hosted (device_id set), not backed by a
    # locations table row.
    assert all(z["location_id"] is None for z in zones)

    for zone in zones:
        points = seeded_database.get_entity_points(zone["id"])
        assert {p["brick_class"] for p in points} == {
            "Power_Sensor", "On_Off_Command", "Lighting_Level_Command",
        }


def test_flat_point_type_still_collides_for_unmigrated_callers(seeded_database):
    """The whole reason this migration exists: the legacy flat point_type
    tag still can't distinguish supply vs return fan on its own -- that's
    exactly what the semantic entities/relationships above are for."""
    devices = seeded_database.get_devices()
    ahu1_id = next(d["id"] for d in devices if d["device_instance"] == 1003)

    objects = seeded_database.get_objects(ahu1_id)
    fan_status_objects = [o for o in objects if o["point_type"] == "Fan_Status"]
    assert {o["name"] for o in fan_status_objects} == {"SF-Run", "RF-Run"}


def test_seed_semantic_setup_idempotent_across_setup_calls(seeded_database):
    devices = seeded_database.get_devices()
    ahu1_id = next(d["id"] for d in devices if d["device_instance"] == 1003)
    ahu_entity = seeded_database.get_semantic_entities(
        device_id=ahu1_id, entity_kind="equipment", brick_class="Air_Handling_Unit"
    )[0]
    before = len(seeded_database.get_related_entities(ahu_entity["id"], "isPartOf", direction="in"))

    seeded_database.setup()

    after = len(seeded_database.get_related_entities(ahu_entity["id"], "isPartOf", direction="in"))
    assert before == after == 2
