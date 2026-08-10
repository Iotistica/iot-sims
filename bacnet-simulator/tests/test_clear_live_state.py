"""Database.clear_live_state() -- the "New Project" reset's wipe, reusing
load_project()'s own proven sequence. Regression target: a naive per-row
delete loop (devices then locations, no semantic tables) leaves a location
permanently stuck whenever a leftover semantic entity still points at it --
delete_location() refuses rather than cascading, and nothing ever clears
that semantic entity on its own."""
from __future__ import annotations


def test_clear_live_state_wipes_devices_and_locations(seeded_database):
    # seed_default() only seeds devices/objects, never locations -- create
    # one directly so this test still exercises the "and locations" half of
    # its own name/regression target.
    seeded_database.create_location("Building A", None, "")

    assert seeded_database.get_devices()
    assert seeded_database.get_locations()

    seeded_database.clear_live_state()

    assert seeded_database.get_devices() == []
    assert seeded_database.get_locations() == []


def test_clear_live_state_resolves_semantic_entity_blocked_location(database):
    """Reproduces the exact bug: a semantic entity referencing a leaf
    location blocks delete_location() forever -- a per-row delete loop that
    never touches semantic_entities can't make progress no matter how many
    passes it retries."""
    conn = database._conn()
    conn.execute("INSERT INTO locations (name) VALUES ('Building A')")
    building_id = conn.execute("SELECT id FROM locations WHERE name='Building A'").fetchone()[0]
    conn.execute(
        "INSERT INTO locations (name, parent_location_id) VALUES ('Basement', ?)", (building_id,)
    )
    basement_id = conn.execute("SELECT id FROM locations WHERE name='Basement'").fetchone()[0]
    conn.execute(
        "INSERT INTO semantic_entities (name, brick_class, entity_kind, location_id) "
        "VALUES ('Basement Zone', 'Location', 'location', ?)", (basement_id,)
    )
    conn.commit()

    # Confirm this really is the blocked scenario before the fix runs.
    assert database.delete_location(basement_id) is False
    assert database.delete_location(building_id) is False
    assert len(database.get_locations()) == 2

    database.clear_live_state()

    assert database.get_locations() == []
    assert conn.execute("SELECT COUNT(*) FROM semantic_entities").fetchone()[0] == 0


def test_clear_live_state_is_idempotent_on_empty_db(database):
    database.clear_live_state()
    database.clear_live_state()
    assert database.get_devices() == []
    assert database.get_locations() == []
