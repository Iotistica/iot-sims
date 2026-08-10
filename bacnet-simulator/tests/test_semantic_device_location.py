"""The hasLocation relationship between a device's top-level equipment
entity and its assigned location's entity must be maintained automatically,
deterministically from devices.location_id -- the same "both sides
classified" pattern as isPointOf point membership, but device<->location
instead of point<->equipment. See src/semantics/mirror.py's
sync_device_location_relationship()/_sync_devices_for_location() and
src/semantics/backfill.py's backfill_device_location_relationships().

No BACnet write is exercised or possible here -- same lightweight test app
fixture as test_semantic_point_membership.py, no real BACnet engine
attached."""
from __future__ import annotations


def _create_location(client, name, kind=None, parent_location_id=None):
    resp = client.post("/locations", json={
        "name": name, "parent_location_id": parent_location_id, "description": "", "kind": kind,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _classify_location(client, location, kind):
    resp = client.put(f"/locations/{location['id']}", json={
        "name": location["name"], "parent_location_id": location["parent_location_id"],
        "description": location["description"], "kind": kind,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_boiler(client, location_id=None, equipment_type=None):
    resp = client.post("/devices", json={
        "device_instance": 1002, "name": "Boiler-1", "location_id": location_id, "equipment_type": equipment_type,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _update_device(client, device, **overrides):
    body = {"device_instance": device["device_instance"], "name": device["name"],
            "location_id": device.get("location_id"), "equipment_type": device.get("equipment_type")}
    body.update(overrides)
    resp = client.put(f"/devices/{device['id']}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _hasLocation_rows(database):
    return [r for r in database.get_semantic_relationships() if r["predicate"] == "hasLocation"]


def test_no_relationship_until_both_sides_classified(client, database):
    location = _create_location(client, "Basement")  # no kind yet
    device = _create_boiler(client, location_id=location["id"], equipment_type="Boiler")
    assert _hasLocation_rows(database) == []  # location isn't classified yet

    _classify_location(client, location, "Room")
    rels = _hasLocation_rows(database)
    assert len(rels) == 1


def test_relationship_appears_immediately_when_location_classified_first(client, database):
    location = _create_location(client, "Mechanical Room", kind="Room")
    device = _create_boiler(client, location_id=location["id"], equipment_type="Boiler")
    rels = _hasLocation_rows(database)
    assert len(rels) == 1


def test_resave_is_idempotent(client, database):
    location = _create_location(client, "Basement", kind="Room")
    device = _create_boiler(client, location_id=location["id"], equipment_type="Boiler")
    assert len(_hasLocation_rows(database)) == 1
    row_id = _hasLocation_rows(database)[0]["id"]

    device = _update_device(client, device, name="Boiler-1 renamed")  # unrelated edit
    rels = _hasLocation_rows(database)
    assert len(rels) == 1
    assert rels[0]["id"] == row_id  # untouched, not deleted+recreated


def test_moving_device_to_a_different_location_swaps_the_edge(client, database):
    loc_a = _create_location(client, "Basement", kind="Room")
    loc_b = _create_location(client, "Roof", kind="Room")
    device = _create_boiler(client, location_id=loc_a["id"], equipment_type="Boiler")
    assert _hasLocation_rows(database)[0]["target_entity_id"] is not None

    device = _update_device(client, device, location_id=loc_b["id"])
    rels = _hasLocation_rows(database)
    assert len(rels) == 1
    entities = {e["id"]: e for e in database.get_semantic_entities()}
    target_entity = entities[rels[0]["target_entity_id"]]
    assert target_entity["location_id"] == loc_b["id"]


def test_clearing_device_location_removes_relationship(client, database):
    location = _create_location(client, "Basement", kind="Room")
    device = _create_boiler(client, location_id=location["id"], equipment_type="Boiler")
    assert len(_hasLocation_rows(database)) == 1

    _update_device(client, device, location_id=None)
    assert _hasLocation_rows(database) == []


def test_clearing_location_kind_removes_relationship_via_cascade(client, database):
    location = _create_location(client, "Basement", kind="Room")
    device = _create_boiler(client, location_id=location["id"], equipment_type="Boiler")
    assert len(_hasLocation_rows(database)) == 1

    _classify_location(client, location, None)
    assert _hasLocation_rows(database) == []
    # the device's own equipment classification is untouched
    assert database.get_device(device["id"])["equipment_type"] == "Boiler"


def test_backfill_links_pre_existing_classified_data(client, database):
    location = _create_location(client, "Basement", kind="Room")
    device = _create_boiler(client, location_id=location["id"], equipment_type="Boiler")
    assert len(_hasLocation_rows(database)) == 1

    conn = database._conn()
    conn.execute("DELETE FROM semantic_relationships WHERE predicate='hasLocation'")
    conn.commit()
    assert _hasLocation_rows(database) == []

    from src.semantics.backfill import backfill_device_location_relationships
    backfill_device_location_relationships(conn)
    conn.commit()
    assert len(_hasLocation_rows(database)) == 1

    backfill_device_location_relationships(conn)  # idempotent
    conn.commit()
    assert len(_hasLocation_rows(database)) == 1


def test_relationship_visible_via_semantic_relationships_api(client):
    location = _create_location(client, "Basement", kind="Room")
    _create_boiler(client, location_id=location["id"], equipment_type="Boiler")

    resp = client.get("/semantic-relationships")
    assert resp.status_code == 200
    rels = [r for r in resp.json() if r["predicate"] == "hasLocation"]
    assert len(rels) == 1
