"""The isPointOf membership relationship between a point's semantic entity
and its device's top-level equipment entity must be maintained automatically
-- purely from objects.device_id, never inferred -- the moment both sides
are classified, through ANY write path (here: the ordinary device/object
PUT routes, which Suggest Semantics' Apply Selected also goes through
unchanged). See src/semantics/mirror.py's _sync_point_membership() and
src/semantics/backfill.py's backfill_point_membership_relationships().

No BACnet write is exercised or possible here -- the lightweight test app
fixture (conftest.py) has no real BACnet engine attached at all, only
_FakeEngine; every operation below is the same PUT /devices/.../objects/...
already proven elsewhere in this session to never touch the BACnet client."""
from __future__ import annotations

from src.bacnet.brick_export import build_brick_graph


def _create_boiler_with_objects(client):
    device = client.post("/devices", json={"device_instance": 1002, "name": "Boiler-1"}).json()
    o1 = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "BLR-Firing-Rate",
    }).json()
    o2 = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 2, "name": "BLR-Supply-Temp",
    }).json()
    return device, o1, o2


def _classify_device(client, device, equipment_type):
    resp = client.put(f"/devices/{device['id']}", json={
        "device_instance": device["device_instance"], "name": device["name"], "equipment_type": equipment_type,
    })
    assert resp.status_code == 200, resp.text


def _classify_object(client, device_id, obj, point_type):
    resp = client.put(f"/devices/{device_id}/objects/{obj['id']}", json={
        "object_type": obj["object_type"], "object_instance": obj["object_instance"], "name": obj["name"],
        "point_type": point_type,
    })
    assert resp.status_code == 200, resp.text


def _isPointOf_rows(database):
    return [r for r in database.get_semantic_relationships() if r["predicate"] == "isPointOf"]


def test_membership_lifecycle(client, database):
    device, o1, o2 = _create_boiler_with_objects(client)

    # 1/2/3 -- classify device, classify one object -> exactly one relationship
    _classify_device(client, device, "Boiler")
    _classify_object(client, device["id"], o1, "Natural_Gas_Flow_Sensor")
    rels = _isPointOf_rows(database)
    assert len(rels) == 1
    equipment_entity_id = rels[0]["target_entity_id"]
    point1_entity_id = rels[0]["source_entity_id"]

    # 4 -- classify second object -> second relationship
    _classify_object(client, device["id"], o2, "Leaving_Hot_Water_Temperature_Sensor")
    rels = _isPointOf_rows(database)
    assert len(rels) == 2
    assert {r["target_entity_id"] for r in rels} == {equipment_entity_id}

    # 5 -- reapply the same classes -> no duplicates
    _classify_device(client, device, "Boiler")
    _classify_object(client, device["id"], o1, "Natural_Gas_Flow_Sensor")
    assert len(_isPointOf_rows(database)) == 2

    # 6 -- change point_type -> relationship stays tied to the same entities
    _classify_object(client, device["id"], o1, "Natural_Gas_Flow_Sensor")  # no-op reclassify first
    before = next(r for r in _isPointOf_rows(database) if r["source_entity_id"] == point1_entity_id)
    _classify_object(client, device["id"], o1, "Water_Flow_Sensor")
    after = next(r for r in _isPointOf_rows(database) if r["source_entity_id"] == point1_entity_id)
    assert before["id"] == after["id"]
    assert after["target_entity_id"] == equipment_entity_id

    # 7 -- clear point_type -> relationship disappears
    _classify_object(client, device["id"], o1, None)
    rels = _isPointOf_rows(database)
    assert len(rels) == 1
    assert point1_entity_id not in {r["source_entity_id"] for r in rels}

    # 8 -- reassign point_type -> relationship returns
    _classify_object(client, device["id"], o1, "Water_Flow_Sensor")
    assert len(_isPointOf_rows(database)) == 2

    # 9 -- clear device equipment_type -> ALL its points' relationships disappear
    _classify_device(client, device, None)
    assert _isPointOf_rows(database) == []
    # entities for the still-classified points/objects themselves survive --
    # only the relationship (which cascaded via the now-deleted equipment
    # entity) is gone, not the points' own classification.
    assert database.get_object(o1["id"])["point_type"] == "Water_Flow_Sensor"
    assert database.get_object(o2["id"])["point_type"] == "Leaving_Hot_Water_Temperature_Sensor"

    # 10 -- restore equipment_type -> relationships for still-classified points return
    _classify_device(client, device, "Boiler")
    rels = _isPointOf_rows(database)
    assert len(rels) == 2


def test_backfill_links_pre_existing_classified_data(client, database):
    """Simulates data classified before this feature existed: entities
    present (as the ongoing mirror sync would have made them), but no
    relationship row -- exactly what a pre-upgrade database looks like."""
    device, o1, _o2 = _create_boiler_with_objects(client)
    _classify_device(client, device, "Boiler")
    _classify_object(client, device["id"], o1, "Natural_Gas_Flow_Sensor")
    assert len(_isPointOf_rows(database)) == 1

    # Simulate "pre-fix" state: entities exist, relationship doesn't.
    conn = database._conn()
    conn.execute("DELETE FROM semantic_relationships")
    conn.commit()
    assert _isPointOf_rows(database) == []

    from src.semantics.backfill import backfill_point_membership_relationships
    backfill_point_membership_relationships(conn)
    conn.commit()

    rels = _isPointOf_rows(database)
    assert len(rels) == 1

    # Idempotent -- re-running never duplicates.
    backfill_point_membership_relationships(conn)
    conn.commit()
    assert len(_isPointOf_rows(database)) == 1


def test_relationship_visible_via_semantic_relationships_api(client):
    device, o1, _o2 = _create_boiler_with_objects(client)
    _classify_device(client, device, "Boiler")
    _classify_object(client, device["id"], o1, "Natural_Gas_Flow_Sensor")

    resp = client.get("/semantic-relationships")
    assert resp.status_code == 200
    rels = [r for r in resp.json() if r["predicate"] == "isPointOf"]
    assert len(rels) == 1


def test_brick_export_includes_point_equipment_membership(client, database):
    device, o1, _o2 = _create_boiler_with_objects(client)
    _classify_device(client, device, "Boiler")
    _classify_object(client, device["id"], o1, "Natural_Gas_Flow_Sensor")

    dev_row = database.get_device(device["id"])
    dev_row["objects"] = database.get_objects(device["id"])
    entities = database.get_semantic_entities()
    relationships = database.get_semantic_relationships()

    graph, warnings = build_brick_graph([dev_row], entities=entities, relationships=relationships)
    ttl = graph.serialize(format="turtle")
    assert "isPointOf" in ttl or "hasPoint" in ttl
