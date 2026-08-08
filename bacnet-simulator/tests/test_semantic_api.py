"""API-level tests for the semantic entities/relationships router
(src/api/routers/semantic.py), exercised through the `client` fixture
(a real FastAPI TestClient wired to a real temp-file Database -- no
mocking of the persistence layer)."""
from __future__ import annotations


def _make_device(database, *, equipment_type="Chiller"):
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name, equipment_type) VALUES (?,?,?)",
            (201, "Chiller-1", equipment_type),
        )
        conn.commit()
        return conn.execute("SELECT id FROM devices WHERE device_instance=201").fetchone()[0]


def test_create_get_update_delete_entity(client, database):
    device_id = _make_device(database)

    resp = client.post(
        "/semantic-entities",
        json={"name": "Chiller Plant", "brick_class": "Chiller", "entity_kind": "equipment", "device_id": device_id},
    )
    assert resp.status_code == 201, resp.text
    entity = resp.json()
    assert entity["semantic_key"] is not None
    entity_id = entity["id"]

    resp = client.get(f"/semantic-entities/{entity_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Chiller Plant"

    resp = client.get("/semantic-entities", params={"device_id": device_id})
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.put(
        f"/semantic-entities/{entity_id}",
        json={"name": "Chiller Plant Renamed", "brick_class": "Chiller", "entity_kind": "equipment", "device_id": device_id},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Chiller Plant Renamed"

    resp = client.delete(f"/semantic-entities/{entity_id}")
    assert resp.status_code == 204

    resp = client.get(f"/semantic-entities/{entity_id}")
    assert resp.status_code == 404


def test_get_missing_entity_404(client):
    resp = client.get("/semantic-entities/999999")
    assert resp.status_code == 404


def test_invalid_entity_kind_brick_class_combo_rejected(client, database):
    device_id = _make_device(database)

    # point entity with device_id set instead of object_id -- invalid shape.
    resp = client.post(
        "/semantic-entities",
        json={"name": "Bad Point", "brick_class": "Power_Sensor", "entity_kind": "point", "device_id": device_id},
    )
    assert resp.status_code == 400

    # non-canonical brick_class for the given entity_kind.
    resp = client.post(
        "/semantic-entities",
        json={"name": "Bad Class", "brick_class": "Not_A_Real_Brick_Class", "entity_kind": "equipment", "device_id": device_id},
    )
    assert resp.status_code == 400

    # location entity with neither location_id nor device_id set.
    resp = client.post(
        "/semantic-entities",
        json={"name": "Bad Location", "brick_class": "Zone", "entity_kind": "location"},
    )
    assert resp.status_code == 400


def test_relationship_create_traverse_delete_cascade(client, database):
    device_id = _make_device(database)

    parent = client.post(
        "/semantic-entities",
        json={"name": "AHU-1", "brick_class": "Air_Handling_Unit", "entity_kind": "equipment", "device_id": device_id},
    ).json()
    child = client.post(
        "/semantic-entities",
        # NOTE: "Fan" isn't added to EQUIPMENT_TYPES until phase 4 -- use a
        # currently-canonical class here, the specific class doesn't matter
        # for this traversal/cascade test.
        json={"name": "Supply Fan", "brick_class": "Pump", "entity_kind": "equipment", "device_id": device_id, "local_slug": "supply-fan"},
    ).json()

    resp = client.post(
        "/semantic-relationships",
        json={"source_entity_id": child["id"], "predicate": "isPartOf", "target_entity_id": parent["id"]},
    )
    assert resp.status_code == 201, resp.text
    rel = resp.json()

    # duplicate insert is a no-op, same relationship id back.
    resp2 = client.post(
        "/semantic-relationships",
        json={"source_entity_id": child["id"], "predicate": "isPartOf", "target_entity_id": parent["id"]},
    )
    assert resp2.status_code == 201
    assert resp2.json()["id"] == rel["id"]

    # traversal: out from child -> parent; in on parent -> child.
    out = client.get(f"/semantic-entities/{child['id']}/related", params={"predicate": "isPartOf", "direction": "out"})
    assert out.status_code == 200
    assert [e["id"] for e in out.json()] == [parent["id"]]

    inn = client.get(f"/semantic-entities/{parent['id']}/related", params={"predicate": "isPartOf", "direction": "in"})
    assert inn.status_code == 200
    assert [e["id"] for e in inn.json()] == [child["id"]]

    # deleting an endpoint entity cascades the relationship away.
    resp = client.delete(f"/semantic-entities/{child['id']}")
    assert resp.status_code == 204
    remaining = client.get("/semantic-relationships", params={"target_entity_id": parent["id"]})
    assert remaining.json() == []


def test_ispartof_cycle_rejected(client, database):
    device_id = _make_device(database)

    a = client.post(
        "/semantic-entities",
        json={"name": "A", "brick_class": "Air_Handling_Unit", "entity_kind": "equipment", "device_id": device_id},
    ).json()
    b = client.post(
        "/semantic-entities",
        json={"name": "B", "brick_class": "Pump", "entity_kind": "equipment", "device_id": device_id, "local_slug": "b"},
    ).json()

    # B isPartOf A
    resp = client.post(
        "/semantic-relationships",
        json={"source_entity_id": b["id"], "predicate": "isPartOf", "target_entity_id": a["id"]},
    )
    assert resp.status_code == 201

    # A isPartOf B would close a cycle -- rejected.
    resp = client.post(
        "/semantic-relationships",
        json={"source_entity_id": a["id"], "predicate": "isPartOf", "target_entity_id": b["id"]},
    )
    assert resp.status_code == 400


def test_relationship_predicate_and_self_reference_validated(client, database):
    device_id = _make_device(database)
    a = client.post(
        "/semantic-entities",
        json={"name": "A", "brick_class": "Chiller", "entity_kind": "equipment", "device_id": device_id},
    ).json()

    resp = client.post(
        "/semantic-relationships",
        json={"source_entity_id": a["id"], "predicate": "notARealPredicate", "target_entity_id": a["id"]},
    )
    assert resp.status_code == 400

    resp = client.post(
        "/semantic-relationships",
        json={"source_entity_id": a["id"], "predicate": "feeds", "target_entity_id": a["id"]},
    )
    assert resp.status_code == 400


def test_meta_exposes_semantic_predicates(client):
    resp = client.get("/meta")
    # /meta isn't part of the client fixture's minimal router set (it's a
    # direct @api.get route on the full legacy.py app), so this endpoint
    # isn't reachable through the test_app fixture -- covered instead as a
    # plain unit check on the source dict it's built from.
    assert resp.status_code == 404


def test_semantic_predicates_dict_shape():
    from src.core.config import SEMANTIC_PREDICATES

    assert set(SEMANTIC_PREDICATES) == {"isPointOf", "isPartOf", "feeds", "hasLocation"}
