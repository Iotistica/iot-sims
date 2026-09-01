"""Device/Equipment/Controller split: equipment CRUD API, the explicit
POST /devices/{id}/controller Controller-creation endpoint, and project
save/load round-tripping of both. Exercised through the `client` fixture
(a real FastAPI TestClient wired to a real temp-file Database) plus direct
Database calls for the pieces the client fixture's minimal router set
doesn't cover (project save/load has no dedicated router in the test app,
same pattern tests/test_project_roundtrip.py already uses)."""
from __future__ import annotations

import pytest


def _make_device(database, *, device_instance=301, name="AHU-1", equipment_type=None):
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name, equipment_type) VALUES (?,?,?)",
            (device_instance, name, equipment_type),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM devices WHERE device_instance=?", (device_instance,)
        ).fetchone()[0]


# ─── Equipment CRUD API ──────────────────────────────────────────────────────

def test_equipment_crud_round_trip(client):
    resp = client.post("/equipment", json={
        "name": "Boiler 1", "description": "", "equipment_type": "Boiler",
        "manufacturer": "Trane", "model": "Boiler-9000",
    })
    assert resp.status_code == 201, resp.text
    eq = resp.json()
    eq_id = eq["id"]
    assert eq["name"] == "Boiler 1"
    assert eq["equipment_type"] == "Boiler"
    assert eq["manufacturer"] == "Trane"
    assert eq["model"] == "Boiler-9000"

    resp = client.get(f"/equipment/{eq_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Boiler 1"
    assert resp.json()["manufacturer"] == "Trane"
    assert resp.json()["model"] == "Boiler-9000"

    resp = client.get("/equipment")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.put(f"/equipment/{eq_id}", json={
        "name": "Boiler 1 Renamed", "description": "", "equipment_type": "Boiler",
        "manufacturer": "Carrier", "model": "Boiler-X",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "Boiler 1 Renamed"
    assert resp.json()["manufacturer"] == "Carrier"
    assert resp.json()["model"] == "Boiler-X"

    resp = client.delete(f"/equipment/{eq_id}")
    assert resp.status_code == 204

    resp = client.get(f"/equipment/{eq_id}")
    assert resp.status_code == 404


def test_equipment_create_syncs_semantic_entity(client, database):
    resp = client.post("/equipment", json={"name": "Chiller 1", "description": "", "equipment_type": "Chiller"})
    assert resp.status_code == 201
    eq_id = resp.json()["id"]

    entities = database.get_semantic_entities(entity_kind="equipment")
    matching = [e for e in entities if e.get("equipment_id") == eq_id]
    assert len(matching) == 1
    assert matching[0]["brick_class"] == "Chiller"

    # Renaming/re-classifying through the Equipment API keeps the entity in
    # lockstep (Direction 1 -- see src/semantics/mirror.py).
    resp = client.put(f"/equipment/{eq_id}", json={"name": "Chiller 1", "description": "", "equipment_type": "Cooling_Tower"})
    assert resp.status_code == 200
    entities = database.get_semantic_entities(entity_kind="equipment")
    matching = [e for e in entities if e.get("equipment_id") == eq_id]
    assert len(matching) == 1
    assert matching[0]["brick_class"] == "Cooling_Tower"


def test_equipment_delete_clears_semantic_entity(client, database):
    resp = client.post("/equipment", json={"name": "Pump 1", "description": "", "equipment_type": "Pump"})
    eq_id = resp.json()["id"]
    assert len([e for e in database.get_semantic_entities(entity_kind="equipment") if e.get("equipment_id") == eq_id]) == 1

    resp = client.delete(f"/equipment/{eq_id}")
    assert resp.status_code == 204

    # ON DELETE CASCADE on semantic_entities.equipment_id removes the entity too.
    assert database.get_equipment(eq_id) is None
    remaining = [e for e in database.get_semantic_entities(entity_kind="equipment") if e.get("equipment_id") == eq_id]
    assert remaining == []


def test_equipment_invalid_semantic_type_rejected(client):
    resp = client.post("/equipment", json={"name": "Bad", "description": "", "equipment_type": "Not_A_Real_Class"})
    assert resp.status_code == 400


def test_equipment_delete_guards_location(client, database):
    loc = client.post("/locations", json={"name": "Mechanical Room", "description": ""}).json()
    client.post("/equipment", json={"name": "Boiler 1", "description": "", "location_id": loc["id"]})

    resp = client.delete(f"/locations/{loc['id']}")
    assert resp.status_code == 409


def test_equipment_kind_requires_exactly_one_of_device_id_equipment_id():
    from src.semantics.validation import validate_semantic_entity

    with pytest.raises(ValueError):
        validate_semantic_entity("equipment", "Boiler", device_id=None, object_id=None, location_id=None, equipment_id=None)

    with pytest.raises(ValueError):
        validate_semantic_entity("equipment", "Boiler", device_id=1, object_id=None, location_id=None, equipment_id=1)

    # Exactly one of the two -- both legal.
    validate_semantic_entity("equipment", "Boiler", device_id=1, object_id=None, location_id=None, equipment_id=None)
    validate_semantic_entity("equipment", "Boiler", device_id=None, object_id=None, location_id=None, equipment_id=1)


# ─── Controller endpoint ─────────────────────────────────────────────────────

def test_mark_device_as_controller_creates_entity(client, database):
    device_id = _make_device(database)

    resp = client.post(f"/devices/{device_id}/controller")
    assert resp.status_code == 200, resp.text
    entity = resp.json()
    assert entity["entity_kind"] == "controller"
    assert entity["brick_class"] == "Controller"
    assert entity["device_id"] == device_id

    entities = database.get_semantic_entities(device_id=device_id, entity_kind="controller")
    assert len(entities) == 1


def test_mark_device_as_controller_idempotent(client, database):
    device_id = _make_device(database, name="AHU-1")

    first = client.post(f"/devices/{device_id}/controller").json()
    second = client.post(f"/devices/{device_id}/controller").json()
    assert first["id"] == second["id"]

    entities = database.get_semantic_entities(device_id=device_id, entity_kind="controller")
    assert len(entities) == 1

    # Re-marking after a rename keeps the SAME entity's name in sync rather
    # than creating a second one.
    client.put(
        f"/devices/{device_id}",
        json={"device_instance": 301, "name": "AHU-1 Renamed", "equipment_type": None},
    )
    third = client.post(f"/devices/{device_id}/controller").json()
    assert third["id"] == first["id"]
    assert third["name"] == "AHU-1 Renamed"
    entities = database.get_semantic_entities(device_id=device_id, entity_kind="controller")
    assert len(entities) == 1


def test_mark_missing_device_as_controller_404(client):
    resp = client.post("/devices/999999/controller")
    assert resp.status_code == 404


def test_has_controller_entity_flag(client, database):
    device_id = _make_device(database)

    resp = client.get(f"/devices/{device_id}")
    assert not resp.json()["has_controller_entity"]

    client.post(f"/devices/{device_id}/controller")

    resp = client.get(f"/devices/{device_id}")
    assert resp.json()["has_controller_entity"]

    devices = client.get("/devices").json()
    dev = next(d for d in devices if d["id"] == device_id)
    assert dev["has_controller_entity"]


def test_create_and_update_device_never_create_controller_entity(client, database):
    """No backfill, no implicit creation -- entity_kind='controller' rows
    are only ever created by the one explicit POST .../controller call
    (see sync_controller_entity()'s docstring)."""
    resp = client.post(
        "/devices",
        json={"device_instance": 501, "name": "Legacy Device", "equipment_type": "Chiller"},
    )
    assert resp.status_code == 201
    device_id = resp.json()["id"]

    assert database.get_semantic_entities(device_id=device_id, entity_kind="controller") == []

    for _ in range(3):
        client.put(
            f"/devices/{device_id}",
            json={"device_instance": 501, "name": "Legacy Device Edited", "equipment_type": "Chiller"},
        )
    assert database.get_semantic_entities(device_id=device_id, entity_kind="controller") == []

    # A device with a legacy equipment_type still gets exactly its own
    # entity_kind='equipment' entity, exactly as before -- untouched by any
    # of the Controller machinery.
    equipment_entities = database.get_semantic_entities(device_id=device_id, entity_kind="equipment")
    assert len(equipment_entities) == 1


def test_sync_external_devices_never_creates_controller_entity(database):
    database.sync_external_devices([{
        "device_instance": 9001,
        "name": "ext_ahu_9001",
        "host": "172.22.0.99",
        "port": 47808,
        "metadata": {"objectName": "External-AHU"},
    }])
    ext_device = next(d for d in database.get_devices() if d["device_instance"] == 9001)
    assert ext_device["has_controller_entity"] == 0
    assert database.get_semantic_entities(device_id=ext_device["id"], entity_kind="controller") == []


def test_database_setup_never_backfills_controller_entities(seeded_database):
    """Opening/migrating an existing (seeded) database must never itself
    grant any device the Controller semantic role."""
    assert seeded_database.get_semantic_entities(entity_kind="controller") == []
    for dev in seeded_database.get_devices():
        assert dev["has_controller_entity"] == 0


# ─── Project save/load round-trip ────────────────────────────────────────────

def test_equipment_and_controller_survive_project_reload(database):
    loc = database.create_location("Mechanical Room", None, "")
    equipment = database.create_equipment("Boiler 1", "", loc["id"], "Boiler", "Trane", "Boiler-9000")
    device_id = _make_device(database, device_instance=701, name="Boiler-Controller")
    controller_entity = database.ensure_controller_entity(device_id)
    equipment_entity = next(
        e for e in database.get_semantic_entities(entity_kind="equipment")
        if e.get("equipment_id") == equipment["id"]
    )
    database.create_semantic_relationship(controller_entity["id"], "controls", equipment_entity["id"])

    project = database.save_project("Controller Round-trip", "")
    database.load_project(project["id"])

    new_equipment = database.get_equipment_list()
    assert len(new_equipment) == 1
    assert new_equipment[0]["id"] != equipment["id"]  # id was reassigned
    assert new_equipment[0]["name"] == "Boiler 1"
    assert new_equipment[0]["location_id"] is not None
    # manufacturer/model are plain descriptive columns with no id-remapping
    # concern (unlike location_id) -- restore_project's equipment-insert loop
    # must still carry them through, since it enumerates columns explicitly
    # rather than using SELECT * (see load_project's equipment restore loop).
    assert new_equipment[0]["manufacturer"] == "Trane"
    assert new_equipment[0]["model"] == "Boiler-9000"

    new_device = next(d for d in database.get_devices() if d["device_instance"] == 701)
    assert new_device["id"] != device_id
    assert new_device["has_controller_entity"]

    new_controller_entity = database.get_semantic_entities(device_id=new_device["id"], entity_kind="controller")[0]
    new_equipment_entity = next(
        e for e in database.get_semantic_entities(entity_kind="equipment")
        if e.get("equipment_id") == new_equipment[0]["id"]
    )

    related = database.get_related_entities(new_controller_entity["id"], "controls", direction="out")
    assert [r["id"] for r in related] == [new_equipment_entity["id"]]

    # semantic_key reflects the NEW ids, not the stale originals.
    assert f"equipment={new_equipment[0]['id']}" in new_equipment_entity["semantic_key"]
    assert f"device={new_device['id']}" in new_controller_entity["semantic_key"]


def test_clear_live_state_wipes_equipment(database):
    database.create_equipment("Boiler 1", "", None, "Boiler")
    assert len(database.get_equipment_list()) == 1

    database.clear_live_state()

    assert database.get_equipment_list() == []


# ─── Equipment panel backend surface (equipment_id filter, assignable-points) ─

def test_semantic_entities_equipment_id_filter(client, database):
    eq1 = client.post("/equipment", json={"name": "Boiler 1", "description": "", "equipment_type": "Boiler"}).json()
    eq2 = client.post("/equipment", json={"name": "Boiler 2", "description": "", "equipment_type": "Boiler"}).json()

    resp = client.get("/semantic-entities", params={"entity_kind": "equipment", "equipment_id": eq1["id"]})
    assert resp.status_code == 200
    matches = resp.json()
    assert len(matches) == 1
    assert matches[0]["equipment_id"] == eq1["id"]
    assert matches[0]["name"] == "Boiler 1"

    resp = client.get("/semantic-entities", params={"entity_kind": "equipment", "equipment_id": eq2["id"]})
    assert [e["equipment_id"] for e in resp.json()] == [eq2["id"]]


def test_assignable_points_empty_when_unclassified_equipment(client, database):
    """Equipment with no equipment_type has no backing semantic entity yet
    (sync_entity_from_flat_field only creates one when a Brick class is
    set) -- the endpoint must return [] rather than error."""
    eq = client.post("/equipment", json={"name": "Mystery Box", "description": ""}).json()

    resp = client.get(f"/equipment/{eq['id']}/assignable-points")
    assert resp.status_code == 200
    assert resp.json() == []


def test_assignable_points_empty_when_no_controller(client, database):
    eq = client.post("/equipment", json={"name": "Boiler 1", "description": "", "equipment_type": "Boiler"}).json()

    resp = client.get(f"/equipment/{eq['id']}/assignable-points")
    assert resp.status_code == 200
    assert resp.json() == []


def test_assignable_points_missing_equipment_404(client):
    resp = client.get("/equipment/999999/assignable-points")
    assert resp.status_code == 404


def test_assignable_points_lists_controller_objects_and_flags_existing_assignment(client, database):
    eq = client.post("/equipment", json={"name": "Chiller", "description": "", "equipment_type": "Chiller"}).json()
    other_eq = client.post("/equipment", json={"name": "Boiler", "description": "", "equipment_type": "Boiler"}).json()

    device_id = _make_device(database, device_instance=801, name="Chiller Controller")
    client.post(f"/devices/{device_id}/controller")

    with database._conn() as conn:
        conn.execute(
            "INSERT INTO objects (device_id, object_type, object_instance, name) VALUES (?,?,?,?)",
            (device_id, "analog-input", 1, "CT-Leaving-Water-Temp"),
        )
        conn.execute(
            "INSERT INTO objects (device_id, object_type, object_instance, name) VALUES (?,?,?,?)",
            (device_id, "analog-input", 2, "CW-Flow"),
        )
        conn.commit()
        obj1_id, obj2_id = (
            r[0] for r in conn.execute(
                "SELECT id FROM objects WHERE device_id=? ORDER BY object_instance", (device_id,)
            ).fetchall()
        )

    controller_entity = database.get_semantic_entities(device_id=device_id, entity_kind="controller")[0]
    equipment_entity = database.get_semantic_entities(entity_kind="equipment", equipment_id=eq["id"])[0]
    other_equipment_entity = database.get_semantic_entities(entity_kind="equipment", equipment_id=other_eq["id"])[0]

    # Not yet controlling -- no candidates.
    assert client.get(f"/equipment/{eq['id']}/assignable-points").json() == []

    database.create_semantic_relationship(controller_entity["id"], "controls", equipment_entity["id"])

    candidates = client.get(f"/equipment/{eq['id']}/assignable-points").json()
    assert {c["object_id"] for c in candidates} == {obj1_id, obj2_id}
    assert all(c["device_id"] == device_id for c in candidates)
    assert all(c["device_name"] == "Chiller Controller" for c in candidates)
    assert all(c["point_entity_id"] is None and c["current_assignment"] is None for c in candidates)

    # Classify + assign obj1 to a DIFFERENT equipment -- the endpoint must
    # surface that existing assignment rather than hiding it.
    point_entity = database.create_semantic_entity(
        "CT-Leaving-Water-Temp", "Temperature_Sensor", "point", object_id=obj1_id,
    )
    database.create_semantic_relationship(point_entity["id"], "isPointOf", other_equipment_entity["id"])

    candidates = {c["object_id"]: c for c in client.get(f"/equipment/{eq['id']}/assignable-points").json()}
    assert candidates[obj1_id]["point_entity_id"] == point_entity["id"]
    assert candidates[obj1_id]["current_assignment"] == {"entity_id": other_equipment_entity["id"], "name": "Boiler"}
    assert candidates[obj2_id]["point_entity_id"] is None
    assert candidates[obj2_id]["current_assignment"] is None
