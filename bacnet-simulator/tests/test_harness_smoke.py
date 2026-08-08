"""Proves the pytest harness itself works before later phases build real
Brick Core tests on top of it: fresh Database.setup()/seed_default() run
cleanly, backfill_semantic_entities() is idempotent across repeated
setup() calls, and the test_app/client fixtures can serve a real request
through a real router without booting the full BACnet/IP application.
"""
from __future__ import annotations

from src.semantics.backfill import backfill_semantic_entities
from src.semantics.keys import derive_semantic_key
from src.semantics.validation import validate_semantic_entity


def test_setup_creates_semantic_tables(database):
    entities = database.get_semantic_entities()
    relationships = database.get_semantic_relationships()
    assert entities == []
    assert relationships == []


def test_setup_is_idempotent(database):
    database.setup()
    database.setup()
    assert database.get_semantic_entities() == []


def test_seed_default_backfills_semantic_entities(seeded_database):
    devices = seeded_database.get_devices()
    assert len(devices) == 18

    entities = seeded_database.get_semantic_entities()
    assert entities, "seed_default() should leave backfilled semantic entities behind"

    by_kind = {}
    for e in entities:
        by_kind[e["entity_kind"]] = by_kind.get(e["entity_kind"], 0) + 1
    assert by_kind["equipment"] > 0
    assert by_kind["point"] > 0


def test_backfill_idempotent_across_repeated_setup(seeded_database):
    first = len(seeded_database.get_semantic_entities())
    seeded_database.setup()
    second = len(seeded_database.get_semantic_entities())
    seeded_database.setup()
    third = len(seeded_database.get_semantic_entities())
    assert first == second == third


def test_client_fixture_serves_a_real_router(client):
    response = client.get("/locations")
    assert response.status_code == 200
    assert response.json() == []


def test_semantics_package_importable_independently():
    # These modules must not require the full legacy.py app (fastapi
    # app object, bacpypes3 stack, etc.) to import -- only src.core.config.
    assert derive_semantic_key("point", "Power_Sensor", object_id=1) is not None
    validate_semantic_entity(
        "point", "Power_Sensor", device_id=None, object_id=1, location_id=None
    )
