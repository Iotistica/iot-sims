"""Bidirectional sync between Brick semantic_entities and the legacy flat
classification fields (devices.equipment_type, objects.point_type,
locations.kind) -- see src/semantics/mirror.py.

Brick is the semantic source of truth; the flat fields are compatibility
mirrors kept in lockstep automatically so a user assigns a classification
exactly once, through whichever UI surface is convenient (the Device/
Object/Location drawer, or the Semantic Model panel)."""
from __future__ import annotations


_DEVICE_INSTANCE_COUNTER = [800]


def _make_device(database, *, name="Mirror-Test-Device", equipment_type=None):
    """Goes through the real Database.create_device() (not raw SQL) so
    Direction 1 (sync_entity_from_flat_field) actually fires, matching
    what the Device drawer's save action does in production."""
    _DEVICE_INSTANCE_COUNTER[0] += 1
    device = database.create_device({
        "device_instance": _DEVICE_INSTANCE_COUNTER[0], "name": name, "description": "",
        "vendor_name": "x", "model_name": "y", "enabled": 1, "firmware_revision": "N/A",
        "protocol_revision": 22, "max_apdu_length_accepted": 1024, "segmentation_supported": "segmented-both",
        "location_id": None, "equipment_type": equipment_type, "can_receive_event_notifications": None,
    })
    return device["id"]


def _full_device_payload(device_id, database, **overrides):
    row = database.get_device(device_id)
    payload = dict(row)
    payload.update(overrides)
    return payload


# ── 1. point Brick class <-> objects.point_type ─────────────────────────

def test_creating_point_entity_updates_object_point_type(database):
    """Direction 2: Semantic panel creates a point entity -> objects.point_type mirrors it."""
    device_id = _make_device(database)
    obj = database.create_object(device_id, {
        "object_type": "analog-input", "object_instance": 1, "name": "Pt-1", "units": "no-units",
        "behavior": "manual", "behavior_params": '{"value":0}', "enabled": 1, "number_of_states": 2,
        "reliability": "no-fault-detected", "polarity": "normal", "point_type": None,
    })
    assert database.get_object(obj["id"])["point_type"] is None

    database.create_semantic_entity("Pt-1", "Power_Sensor", "point", object_id=obj["id"])
    assert database.get_object(obj["id"])["point_type"] == "Power_Sensor"


def test_updating_point_entity_updates_object_point_type(database):
    device_id = _make_device(database)
    obj = database.create_object(device_id, {
        "object_type": "analog-input", "object_instance": 1, "name": "Pt-1", "units": "no-units",
        "behavior": "manual", "behavior_params": '{"value":0}', "enabled": 1, "number_of_states": 2,
        "reliability": "no-fault-detected", "polarity": "normal", "point_type": "Power_Sensor",
    })
    entity = database.get_semantic_entities(object_id=obj["id"])[0]

    database.update_semantic_entity(entity["id"], "Pt-1", "Energy_Sensor", "point", object_id=obj["id"])
    assert database.get_object(obj["id"])["point_type"] == "Energy_Sensor"


def test_creating_object_with_point_type_creates_entity_and_updating_changes_it(database):
    """Direction 1: Object drawer sets point_type -> semantic entity mirrors it."""
    device_id = _make_device(database)
    obj = database.create_object(device_id, {
        "object_type": "analog-input", "object_instance": 1, "name": "Pt-1", "units": "no-units",
        "behavior": "manual", "behavior_params": '{"value":0}', "enabled": 1, "number_of_states": 2,
        "reliability": "no-fault-detected", "polarity": "normal", "point_type": "Power_Sensor",
    })
    entities = database.get_semantic_entities(object_id=obj["id"])
    assert len(entities) == 1 and entities[0]["brick_class"] == "Power_Sensor"

    payload = dict(obj)
    payload["point_type"] = "Energy_Sensor"
    database.update_object(obj["id"], payload)
    entities = database.get_semantic_entities(object_id=obj["id"])
    assert len(entities) == 1 and entities[0]["brick_class"] == "Energy_Sensor"

    payload["point_type"] = None
    database.update_object(obj["id"], payload)
    assert database.get_semantic_entities(object_id=obj["id"]) == []


# ── 2. top-level equipment Brick class <-> devices.equipment_type ───────

def test_creating_device_with_equipment_type_creates_direct_entity(database):
    device_id = _make_device(database, equipment_type="Chiller")
    entities = database.get_semantic_entities(device_id=device_id, entity_kind="equipment")
    assert len(entities) == 1 and entities[0]["brick_class"] == "Chiller"


def test_updating_device_equipment_type_updates_same_entity(database):
    device_id = _make_device(database, equipment_type="Chiller")
    entity_before = database.get_semantic_entities(device_id=device_id, entity_kind="equipment")[0]

    database.update_device(device_id, _full_device_payload(device_id, database, equipment_type="Boiler"))
    entities_after = database.get_semantic_entities(device_id=device_id, entity_kind="equipment")

    assert len(entities_after) == 1
    assert entities_after[0]["id"] == entity_before["id"]
    assert entities_after[0]["brick_class"] == "Boiler"


def test_clearing_device_equipment_type_deletes_direct_entity(database):
    device_id = _make_device(database, equipment_type="Chiller")
    assert database.get_semantic_entities(device_id=device_id, entity_kind="equipment") != []

    database.update_device(device_id, _full_device_payload(device_id, database, equipment_type=None))
    assert database.get_semantic_entities(device_id=device_id, entity_kind="equipment") == []


# ── 3. physical location Brick class <-> locations.kind ─────────────────

def test_creating_location_with_kind_creates_direct_entity(database):
    loc = database.create_location("Room 1", None, "", "Room")
    entities = database.get_semantic_entities(location_id=loc["id"])
    assert len(entities) == 1 and entities[0]["brick_class"] == "Room"


def test_updating_location_kind_updates_same_entity(database):
    loc = database.create_location("Room 1", None, "", "Room")
    entity_before = database.get_semantic_entities(location_id=loc["id"])[0]

    database.update_location(loc["id"], "Room 1", None, "", "Floor")
    entities_after = database.get_semantic_entities(location_id=loc["id"])

    assert len(entities_after) == 1
    assert entities_after[0]["id"] == entity_before["id"]
    assert entities_after[0]["brick_class"] == "Floor"


def test_creating_location_entity_via_panel_updates_locations_kind(database):
    """Direction 2: Semantic panel creates a location entity for a real
    locations row -> locations.kind mirrors it."""
    loc = database.create_location("Room 1", None, "")
    assert database.get_location(loc["id"])["kind"] is None

    database.create_semantic_entity("Room 1", "Room", "location", location_id=loc["id"])
    assert database.get_location(loc["id"])["kind"] == "Room"


def test_deleting_location_entity_clears_locations_kind(database):
    loc = database.create_location("Room 1", None, "", "Room")
    entity = database.get_semantic_entities(location_id=loc["id"])[0]

    database.delete_semantic_entity(entity["id"])
    assert database.get_location(loc["id"])["kind"] is None


# ── 4/9. sub-equipment must never overwrite devices.equipment_type ──────

def test_subequipment_does_not_overwrite_device_equipment_type(database):
    device_id = _make_device(database, equipment_type="Air_Handling_Unit")
    ahu_entity = database.get_semantic_entities(device_id=device_id, entity_kind="equipment")[0]

    fan = database.create_semantic_entity(
        "Supply Fan", "Supply_Fan", "equipment", device_id=device_id, local_slug="supply-fan",
    )
    database.create_semantic_relationship(fan["id"], "isPartOf", ahu_entity["id"])

    assert database.get_device(device_id)["equipment_type"] == "Air_Handling_Unit"


def test_creating_equipment_entity_via_panel_never_touches_flat_field(database):
    """Direction 2 is deliberately NOT implemented for entity_kind='equipment'
    at all -- creating ANY equipment entity via the generic Semantic panel
    CRUD (sub-equipment or otherwise) must never write devices.equipment_type."""
    device_id = _make_device(database)  # equipment_type starts NULL
    database.create_semantic_entity("Some Equipment", "Chiller", "equipment", device_id=device_id)
    assert database.get_device(device_id)["equipment_type"] is None


# ── 5. virtual Lighting_Zone must never overwrite device/location fields ─

def test_virtual_location_entity_does_not_touch_device_or_location_fields(database):
    device_id = _make_device(database, equipment_type="Lighting_Equipment")
    with database._conn() as conn:
        conn.execute("INSERT INTO locations (name, kind) VALUES ('Floor 1', 'Floor')")
        location_id = conn.execute("SELECT id FROM locations WHERE name='Floor 1'").fetchone()[0]
        conn.commit()

    # Virtual, device-hosted Lighting_Zone -- location_id is NULL by construction.
    database.create_semantic_entity(
        "Zone A", "Lighting_Zone", "location", device_id=device_id, local_slug="zone-a",
    )

    assert database.get_device(device_id)["equipment_type"] == "Lighting_Equipment"
    assert database.get_location(location_id)["kind"] == "Floor"


# ── 6. backfill must never overwrite an existing entity with a stale flat tag ─

def test_backfill_does_not_overwrite_existing_entity_with_stale_flat_tag(database):
    device_id = _make_device(database, equipment_type="Air_Handling_Unit")
    entity = database.get_semantic_entities(device_id=device_id, entity_kind="equipment")[0]

    # Simulate a stale flat tag written some other way (bypassing the mirror).
    with database._conn() as conn:
        conn.execute("UPDATE devices SET equipment_type='Pump' WHERE id=?", (device_id,))
        conn.commit()

    database.setup()  # re-runs backfill

    entity_after = database.get_semantic_entities(device_id=device_id, entity_kind="equipment")
    assert len(entity_after) == 1
    assert entity_after[0]["id"] == entity["id"]
    assert entity_after[0]["brick_class"] == "Air_Handling_Unit"  # NOT overwritten with the stale "Pump"


# ── 7. old projects with only flat tags still backfill correctly ────────

def test_old_data_with_only_flat_tags_still_backfills(database):
    """Simulates data that predates the mirror (e.g. restored from an old
    backup, or inserted directly) -- no semantic entity exists yet, only
    the flat tag. setup()'s backfill must still pick it up."""
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name, equipment_type) VALUES (901, 'Old-Chiller', 'Chiller')"
        )
        device_id = conn.execute("SELECT id FROM devices WHERE device_instance=901").fetchone()[0]
        conn.execute(
            "INSERT INTO objects (device_id, object_type, object_instance, name, point_type) VALUES (?,?,?,?,?)",
            (device_id, "analog-input", 1, "Old-Point", "Power_Sensor"),
        )
        conn.execute("INSERT INTO locations (name, kind) VALUES ('Old Room', 'Room')")
        location_id = conn.execute("SELECT id FROM locations WHERE name='Old Room'").fetchone()[0]
        conn.commit()

    assert database.get_semantic_entities(device_id=device_id) == []

    database.setup()

    device_entities = database.get_semantic_entities(device_id=device_id, entity_kind="equipment")
    assert len(device_entities) == 1 and device_entities[0]["brick_class"] == "Chiller"

    obj = database.get_objects(device_id)[0]
    point_entities = database.get_semantic_entities(object_id=obj["id"])
    assert len(point_entities) == 1 and point_entities[0]["brick_class"] == "Power_Sensor"

    location_entities = database.get_semantic_entities(location_id=location_id)
    assert len(location_entities) == 1 and location_entities[0]["brick_class"] == "Room"


# ── 8. existing Energy/FDD behavior remains unchanged during transition ─

def test_seeded_ahu_fans_still_resolve_correctly_with_mirror_active(seeded_database):
    """Sanity check that the bidirectional sync doesn't disturb the
    existing seed_default() semantic setup (Supply_Fan/Return_Fan
    isPartOf AHU-1) -- full Energy/FDD regression coverage already lives
    in test_ahu_fans.py / test_energy_ahu_integration.py / etc, all of
    which still pass unmodified alongside this change."""
    devices = seeded_database.get_devices()
    ahu1_id = next(d["id"] for d in devices if d["device_instance"] == 1003)
    ahu_entity = seeded_database.get_semantic_entities(
        device_id=ahu1_id, entity_kind="equipment", brick_class="Air_Handling_Unit",
    )[0]
    fans = seeded_database.get_related_entities(ahu_entity["id"], "isPartOf", direction="in")
    assert {f["brick_class"] for f in fans} == {"Supply_Fan", "Return_Fan"}
    # The AHU's own device row's equipment_type is untouched by its sub-equipment.
    assert seeded_database.get_device(ahu1_id)["equipment_type"] == "Air_Handling_Unit"
