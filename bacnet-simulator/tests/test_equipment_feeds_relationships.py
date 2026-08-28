"""Equipment-topology-specific coverage for the `feeds` predicate, exercised
through real Equipment/Location fixtures (rather than the bare device-only
fixtures test_semantic_api.py uses) -- these match what the Equipment
drawer's "Feeds / Serves" field and EquipmentPanel.vue's "Fed By" display
actually depend on. The underlying create/traverse/duplicate/self-reference
mechanics are already exercised generically in test_semantic_api.py; this
file proves they work correctly for the Equipment<->Equipment and
Equipment<->Location shapes this feature introduces."""
from __future__ import annotations


def _classified_equipment(client, name, equipment_type="Air_Handling_Unit"):
    resp = client.post("/equipment", json={"name": name, "equipment_type": equipment_type})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _equipment_entity(client, equipment_id):
    resp = client.get("/semantic-entities", params={"entity_kind": "equipment", "equipment_id": equipment_id})
    assert resp.status_code == 200
    entities = resp.json()
    assert len(entities) == 1
    return entities[0]


def test_feeds_relationship_persists_between_equipment(client):
    rtu = _classified_equipment(client, "RTU-1")
    vav = _classified_equipment(client, "VAV-1", equipment_type="Variable_Air_Volume_Box")

    rtu_entity = _equipment_entity(client, rtu["id"])
    vav_entity = _equipment_entity(client, vav["id"])

    resp = client.post(
        "/semantic-relationships",
        json={"source_entity_id": rtu_entity["id"], "predicate": "feeds", "target_entity_id": vav_entity["id"]},
    )
    assert resp.status_code == 201, resp.text

    listed = client.get(
        "/semantic-relationships",
        params={"source_entity_id": rtu_entity["id"], "predicate": "feeds"},
    )
    assert listed.status_code == 200
    assert [r["target_entity_id"] for r in listed.json()] == [vav_entity["id"]]


def test_feeds_inverse_lookup_fed_by(client):
    rtu = _classified_equipment(client, "RTU-1")
    vav = _classified_equipment(client, "VAV-1", equipment_type="Variable_Air_Volume_Box")
    rtu_entity = _equipment_entity(client, rtu["id"])
    vav_entity = _equipment_entity(client, vav["id"])

    client.post(
        "/semantic-relationships",
        json={"source_entity_id": rtu_entity["id"], "predicate": "feeds", "target_entity_id": vav_entity["id"]},
    )

    # "Fed By" is derived, never separately persisted: querying the
    # inverse direction on the target must surface the source.
    fed_by = client.get(
        f"/semantic-entities/{vav_entity['id']}/related",
        params={"predicate": "feeds", "direction": "in"},
    )
    assert fed_by.status_code == 200
    assert [e["id"] for e in fed_by.json()] == [rtu_entity["id"]]

    feeds = client.get(
        f"/semantic-entities/{rtu_entity['id']}/related",
        params={"predicate": "feeds", "direction": "out"},
    )
    assert feeds.status_code == 200
    assert [e["id"] for e in feeds.json()] == [vav_entity["id"]]


def test_feeds_relationship_equipment_to_location(client):
    vav = _classified_equipment(client, "VAV-1", equipment_type="Variable_Air_Volume_Box")
    zone = client.post("/locations", json={"name": "Zone 1", "kind": "Room"})
    assert zone.status_code == 201, zone.text
    zone = zone.json()

    vav_entity = _equipment_entity(client, vav["id"])
    zone_entity = client.get(
        "/semantic-entities", params={"entity_kind": "location", "location_id": zone["id"]}
    ).json()[0]

    resp = client.post(
        "/semantic-relationships",
        json={"source_entity_id": vav_entity["id"], "predicate": "feeds", "target_entity_id": zone_entity["id"]},
    )
    assert resp.status_code == 201, resp.text

    fed_by = client.get(
        f"/semantic-entities/{zone_entity['id']}/related",
        params={"predicate": "feeds", "direction": "in"},
    )
    assert [e["id"] for e in fed_by.json()] == [vav_entity["id"]]


def test_feeds_duplicate_relationship_is_idempotent(client):
    rtu = _classified_equipment(client, "RTU-1")
    vav = _classified_equipment(client, "VAV-1", equipment_type="Variable_Air_Volume_Box")
    rtu_entity = _equipment_entity(client, rtu["id"])
    vav_entity = _equipment_entity(client, vav["id"])

    body = {"source_entity_id": rtu_entity["id"], "predicate": "feeds", "target_entity_id": vav_entity["id"]}
    first = client.post("/semantic-relationships", json=body)
    second = client.post("/semantic-relationships", json=body)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    listed = client.get(
        "/semantic-relationships",
        params={"source_entity_id": rtu_entity["id"], "predicate": "feeds"},
    )
    assert len(listed.json()) == 1


def test_equipment_cannot_feed_itself(client):
    rtu = _classified_equipment(client, "RTU-1")
    rtu_entity = _equipment_entity(client, rtu["id"])

    resp = client.post(
        "/semantic-relationships",
        json={"source_entity_id": rtu_entity["id"], "predicate": "feeds", "target_entity_id": rtu_entity["id"]},
    )
    assert resp.status_code == 400


def test_deleting_target_equipment_removes_feeds_relationship(client):
    """Deleting the *fed* equipment (not the source) must not leave a
    dangling relationship row -- covers the FK ON DELETE CASCADE path from
    the Equipment side (Location-side cleanup is already covered by the
    existing location delete tests)."""
    rtu = _classified_equipment(client, "RTU-1")
    vav = _classified_equipment(client, "VAV-1", equipment_type="Variable_Air_Volume_Box")
    rtu_entity = _equipment_entity(client, rtu["id"])
    vav_entity = _equipment_entity(client, vav["id"])

    client.post(
        "/semantic-relationships",
        json={"source_entity_id": rtu_entity["id"], "predicate": "feeds", "target_entity_id": vav_entity["id"]},
    )

    resp = client.delete(f"/equipment/{vav['id']}")
    assert resp.status_code == 204

    remaining = client.get(
        "/semantic-relationships",
        params={"source_entity_id": rtu_entity["id"], "predicate": "feeds"},
    )
    assert remaining.json() == []
