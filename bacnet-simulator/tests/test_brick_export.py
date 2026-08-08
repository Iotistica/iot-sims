"""build_brick_graph()'s Brick Core relationship emission: isPartOf/hasPart,
feeds/isFedBy, hasLocation/isLocationOf triples with correct inverses, and
URI reuse (point entities reuse object URIs, location entities backed by a
real locations row reuse the location's URI, top-level equipment reuses
the device URI, sub-equipment/virtual locations mint a fresh entity URI).
Existing per-device output must stay byte-for-byte unchanged when
entities/relationships aren't passed at all."""
from __future__ import annotations

from src.bacnet import brick_export


def _ahu1_data(seeded_database):
    devices = seeded_database.get_devices()
    ahu1 = next(d for d in devices if d["device_instance"] == 1003)
    data = dict(ahu1)
    data["objects"] = seeded_database.get_objects(ahu1["id"])
    return data


def _semantic_data_for(database, device_data):
    entities = list(database.get_semantic_entities(device_id=device_data["id"]))
    for obj in device_data["objects"]:
        entities.extend(database.get_semantic_entities(object_id=obj["id"]))
    entity_ids = {e["id"] for e in entities}
    relationships = [
        r for r in database.get_semantic_relationships()
        if r["source_entity_id"] in entity_ids and r["target_entity_id"] in entity_ids
    ]
    return entities, relationships


def test_ispartof_and_inverse_emitted_for_seeded_ahu_fans(seeded_database):
    device_data = _ahu1_data(seeded_database)
    entities, relationships = _semantic_data_for(seeded_database, device_data)

    graph, warnings = brick_export.build_brick_graph(
        [device_data], entities=entities, relationships=relationships,
    )

    supply_fan = next(e for e in entities if e["brick_class"] == "Supply_Fan")
    ahu_entity = next(e for e in entities if e["brick_class"] == "Air_Handling_Unit")

    supply_fan_uri = brick_export._entity_uri(supply_fan["id"], None)
    ahu_uri = brick_export._device_uri(ahu_entity["device_id"], None)

    assert (supply_fan_uri, brick_export.BRICK.isPartOf, ahu_uri) in graph
    assert (ahu_uri, brick_export.BRICK.hasPart, supply_fan_uri) in graph


def test_point_entities_reuse_object_uri_not_duplicate_node(seeded_database):
    device_data = _ahu1_data(seeded_database)
    entities, relationships = _semantic_data_for(seeded_database, device_data)

    graph, warnings = brick_export.build_brick_graph(
        [device_data], entities=entities, relationships=relationships,
    )

    sf_run_object = next(o for o in device_data["objects"] if o["name"] == "SF-Run")
    sf_run_entity = next(e for e in entities if e.get("object_id") == sf_run_object["id"])

    object_uri = brick_export._object_uri(sf_run_object["id"], None)
    # No separate node was minted for the point entity -- it's the same
    # URI the per-object loop already typed and gave a hasExternalReference.
    supply_fan = next(e for e in entities if e["brick_class"] == "Supply_Fan")
    supply_fan_uri = brick_export._entity_uri(supply_fan["id"], None)
    assert (object_uri, brick_export.BRICK.isPointOf, supply_fan_uri) in graph
    assert (object_uri, brick_export.REF.hasExternalReference, None) in graph


def test_location_entity_with_real_location_row_reuses_location_uri(database):
    with database._conn() as conn:
        conn.execute("INSERT INTO locations (name, kind) VALUES ('Room 101', 'Room')")
        location_id = conn.execute("SELECT id FROM locations WHERE name='Room 101'").fetchone()[0]
        conn.commit()

    entity = database.create_semantic_entity(
        "Room 101", "Room", "location", location_id=location_id,
    )

    graph, warnings = brick_export.build_brick_graph([], entities=[entity], relationships=[])

    location_uri = brick_export._location_uri(location_id, None)
    entity_uri = brick_export._entity_uri(entity["id"], None)

    assert (location_uri, brick_export.RDF.type, brick_export.BRICK.Room) in graph
    assert (entity_uri, brick_export.RDF.type, brick_export.BRICK.Room) not in graph


def test_feeds_and_haslocation_emitted_with_inverses(database):
    with database._conn() as conn:
        conn.execute("INSERT INTO devices (device_instance, name) VALUES (601, 'Chiller-Plant')")
        conn.execute("INSERT INTO devices (device_instance, name) VALUES (602, 'AHU-1')")
        plant_device_id = conn.execute("SELECT id FROM devices WHERE device_instance=601").fetchone()[0]
        ahu_device_id = conn.execute("SELECT id FROM devices WHERE device_instance=602").fetchone()[0]
        conn.execute("INSERT INTO locations (name, kind) VALUES ('Room 101', 'Room')")
        location_id = conn.execute("SELECT id FROM locations WHERE name='Room 101'").fetchone()[0]
        conn.commit()

    plant = database.create_semantic_entity("Chiller-Plant", "Chiller", "equipment", device_id=plant_device_id)
    ahu = database.create_semantic_entity("AHU-1", "Air_Handling_Unit", "equipment", device_id=ahu_device_id)
    database.create_semantic_relationship(plant["id"], "feeds", ahu["id"])

    room = database.create_semantic_entity("Room 101", "Room", "location", location_id=location_id)
    database.create_semantic_relationship(ahu["id"], "hasLocation", room["id"])

    entities = [plant, ahu, room]
    relationships = database.get_semantic_relationships()

    graph, warnings = brick_export.build_brick_graph([], entities=entities, relationships=relationships)

    plant_uri = brick_export._device_uri(plant_device_id, None)
    ahu_uri = brick_export._device_uri(ahu_device_id, None)
    room_uri = brick_export._location_uri(location_id, None)

    assert (plant_uri, brick_export.BRICK.feeds, ahu_uri) in graph
    assert (ahu_uri, brick_export.BRICK.isFedBy, plant_uri) in graph
    assert (ahu_uri, brick_export.BRICK.hasLocation, room_uri) in graph
    assert (room_uri, brick_export.BRICK.isLocationOf, ahu_uri) in graph


def test_existing_per_device_output_unchanged_without_entities(seeded_database):
    """Regression guard: entities/relationships default to empty -- zero
    behavior change for callers that don't pass them (e.g. before phase 8
    wires up project-level exports)."""
    device_data = _ahu1_data(seeded_database)

    # NOTE: can't compare graph_with_none/graph_with_empty via raw triple-set
    # equality -- each object's ref:BACnetReference blank node (BNode()) gets
    # a fresh random identity on every build_brick_graph() call, even for
    # logically identical input, so two separately-built graphs never compare
    # equal by triple set alone. TTL content + warnings are what's actually
    # observable/stable, and both must show the same "no Brick Core data"
    # shape regardless of whether entities/relationships were omitted
    # (defaulting to None) or passed explicitly as empty lists.
    graph_with_none, warnings_with_none = brick_export.build_brick_graph([device_data])
    graph_with_empty, warnings_with_empty = brick_export.build_brick_graph(
        [device_data], entities=[], relationships=[],
    )

    assert warnings_with_none == warnings_with_empty

    ttl = brick_export.graph_to_ttl(graph_with_none, warnings_with_none)
    ttl_empty = brick_export.graph_to_ttl(graph_with_empty, warnings_with_empty)
    for content in (ttl, ttl_empty):
        assert "brick:isPartOf" not in content
        assert "brick:Supply_Fan" not in content
        assert "brick:hasPoint" in content
        assert "brick:isPointOf" in content
