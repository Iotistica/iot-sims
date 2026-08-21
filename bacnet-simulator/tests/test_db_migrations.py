"""Tests for src/db/migrations/ -- the schema_migrations-tracked migration
system that replaced Database.setup()'s old inline executescript() + ~15
ad-hoc "if column missing, ALTER TABLE" blocks.

Before this file, NOTHING exercised any migration's ALTER-TABLE branch:
tests/conftest.py's `database`/`seeded_database` fixtures always create a
brand-new SQLite file and call Database.setup() exactly once, so every
migration's own column-presence check already finds the column (since the
baseline schema's CREATE TABLE already includes it) and the ALTER branch
never fires. This file closes that gap directly: it builds representative
"old schema" fixture databases with plain sqlite3 (bypassing
Database.setup()/run_migrations() entirely for construction), narrower
than today's baseline in exactly the ways each numbered migration expects
to find, then runs run_migrations() against them and checks the result.

Also proves the idempotency guarantee the whole system depends on
(see runner.py's own docstring): running the full migration list twice is
a no-op the second time, and running it against an ALREADY-fully-migrated
database (i.e. what a real dev/production DB looks like today) is
similarly a no-op -- there is no separate "adopt an old database" step.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.db.database import Database
from src.db.migrations.registry import MIGRATIONS
from src.db.migrations.runner import run_migrations


# ─── A representative "old schema" DB, narrower than today's baseline in
# exactly the ways migrations 2-17 expect to find, plus the two full-table-
# rebuild targets (functional_test_runs, energy_model_configs) in their
# pre-migration shape. Tables NOT listed here (equipment, custom_graphs,
# profiles, users, settings, notification_classes, trend_log_records,
# bacnet_schedules/targets, bacnet_calendars, fault_rule_configs,
# fault_events, object_alarm_configs, alarm_log, event_enrollments) simply
# didn't exist in an old database either -- migration 1 (baseline) creates
# them fresh via CREATE TABLE IF NOT EXISTS, same as it would for a
# brand-new database.
_OLD_SCHEMA_SQL = """
    CREATE TABLE locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        parent_location_id INTEGER REFERENCES locations(id),
        description TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_instance INTEGER NOT NULL UNIQUE
            CHECK(device_instance >= 1 AND device_instance <= 4194302),
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        vendor_name TEXT NOT NULL DEFAULT 'Iotistica',
        model_name TEXT NOT NULL DEFAULT 'BACnet Simulator',
        enabled INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE objects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        object_type TEXT NOT NULL,
        object_instance INTEGER NOT NULL,
        name TEXT NOT NULL,
        units TEXT NOT NULL DEFAULT 'no-units',
        behavior TEXT NOT NULL DEFAULT 'constant',
        behavior_params TEXT NOT NULL DEFAULT '{"value":0}',
        enabled INTEGER NOT NULL DEFAULT 1,
        manual_value REAL,
        UNIQUE(device_id, object_type, object_instance)
    );

    CREATE TABLE trend_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        monitored_object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
        logging_type TEXT NOT NULL DEFAULT 'polled',
        log_interval INTEGER NOT NULL DEFAULT 60,
        buffer_size INTEGER NOT NULL DEFAULT 1000,
        stop_when_full INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        record_count INTEGER NOT NULL DEFAULT 0,
        total_record_count INTEGER NOT NULL DEFAULT 0,
        last_sampled_at REAL
    );

    CREATE TABLE functional_tests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        equipment_type TEXT NOT NULL,
        definition_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- Pre-migration shape: target_device_id NOT NULL WITH ON DELETE CASCADE
    -- (migration 15's whole reason to exist).
    CREATE TABLE functional_test_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        functional_test_id INTEGER NOT NULL REFERENCES functional_tests(id) ON DELETE CASCADE,
        target_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        execution_mode TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        started_at TEXT,
        finished_at TEXT,
        result TEXT,
        result_message TEXT,
        current_node_id TEXT,
        error TEXT,
        details_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- Pre-migration shape: no equipment_id, entity_kind CHECK missing
    -- 'controller' (migration 16's whole reason to exist).
    CREATE TABLE semantic_entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        local_slug TEXT,
        semantic_key TEXT,
        brick_class TEXT NOT NULL,
        entity_kind TEXT NOT NULL CHECK(entity_kind IN ('equipment', 'point', 'location')),
        device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
        object_id INTEGER REFERENCES objects(id) ON DELETE CASCADE,
        location_id INTEGER REFERENCES locations(id)
    );

    CREATE TABLE semantic_relationships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_entity_id INTEGER NOT NULL REFERENCES semantic_entities(id) ON DELETE CASCADE,
        predicate TEXT NOT NULL CHECK(predicate IN ('isPointOf', 'isPartOf', 'feeds', 'hasLocation')),
        target_entity_id INTEGER NOT NULL REFERENCES semantic_entities(id) ON DELETE CASCADE,
        UNIQUE(source_entity_id, predicate, target_entity_id)
    );

    -- Pre-migration shape: no instance_key (migration 17's whole reason to
    -- exist).
    CREATE TABLE energy_model_configs (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        model_type TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        parameters TEXT NOT NULL DEFAULT '{}',
        UNIQUE(device_id, model_type)
    );
"""


def _make_old_schema_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_OLD_SCHEMA_SQL)
    conn.commit()
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _all_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in rows}


# ─── Migrating a representative old schema up to current ────────────────

def test_migrations_bring_old_schema_up_to_current(tmp_path):
    old_db_path = tmp_path / "old_schema.db"
    conn = _make_old_schema_db(old_db_path)

    run_migrations(conn)

    # Every migration recorded, in order, none skipped/duplicated.
    rows = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [m.version for m in MIGRATIONS]
    assert len(rows) == len(MIGRATIONS)

    # New columns migrations 2-14 add are all present.
    assert {"number_of_states", "reliability", "polarity"} <= _table_columns(conn, "objects")
    assert "cov_increment" in _table_columns(conn, "trend_logs")
    dev_cols = _table_columns(conn, "devices")
    assert {
        "firmware_revision", "protocol_revision", "max_apdu_length_accepted",
        "segmentation_supported", "location_id", "equipment_type",
        "can_receive_event_notifications", "source_type", "simulation_mode",
        "source_device_id",
    } <= dev_cols
    assert {"point_type", "description"} <= _table_columns(conn, "objects")
    assert {"kind", "sort_order"} <= _table_columns(conn, "locations")

    # Migration 8's devices rebuild: existing rows survive, new UNIQUE
    # constraint shape in place, source_type defaulted correctly.
    conn.execute(
        "INSERT INTO devices (device_instance, name) VALUES (1234, 'Old Device')"
    )
    row = conn.execute(
        "SELECT source_type FROM devices WHERE device_instance=1234"
    ).fetchone()
    assert row["source_type"] == "simulated"

    # Migration 15: functional_test_runs.target_device_id is now nullable.
    ftr_cols = {row[1]: row for row in conn.execute("PRAGMA table_info(functional_test_runs)")}
    assert ftr_cols["target_device_id"][3] == 0  # notnull flag cleared

    # Migration 16: semantic_entities gained equipment_id and 'controller'.
    assert "equipment_id" in _table_columns(conn, "semantic_entities")
    conn.execute(
        "INSERT INTO semantic_entities (name, brick_class, entity_kind, device_id) "
        "VALUES ('Test Controller', 'Controller', 'controller', 1)"
    )  # must not raise -- 'controller' is now a legal entity_kind

    # Migration 16 also rebuilt semantic_relationships with the wider
    # predicate CHECK ('controls'/'isHostedBy').
    entity_id = conn.execute("SELECT id FROM semantic_entities WHERE entity_kind='controller'").fetchone()[0]
    conn.execute(
        "INSERT INTO semantic_entities (name, brick_class, entity_kind, device_id) "
        "VALUES ('Test Equip', 'Equipment', 'equipment', 1)"
    )
    equip_entity_id = conn.execute("SELECT id FROM semantic_entities WHERE entity_kind='equipment'").fetchone()[0]
    conn.execute(
        "INSERT INTO semantic_relationships (source_entity_id, predicate, target_entity_id) "
        "VALUES (?, 'controls', ?)",
        (entity_id, equip_entity_id),
    )  # must not raise

    # Migration 17: energy_model_configs gained instance_key.
    assert "instance_key" in _table_columns(conn, "energy_model_configs")

    # Baseline-only tables (never existed in the old DB) were created fresh.
    for table in ("equipment", "custom_graphs", "profiles", "users", "settings"):
        assert table in _all_tables(conn)

    conn.close()


def test_migrations_are_idempotent_on_second_run(tmp_path):
    conn = _make_old_schema_db(tmp_path / "old_schema.db")
    run_migrations(conn)
    first_rows = conn.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version").fetchall()

    run_migrations(conn)  # must not raise, must not touch anything
    second_rows = conn.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version").fetchall()

    assert first_rows == second_rows
    conn.close()


# ─── The de-risking claim: a no-op against an already-migrated DB ────────

def test_migrations_are_a_no_op_against_an_already_migrated_database(tmp_path):
    """The concrete claim the whole system depends on: running the full
    migration list against a database that already has today's final
    schema (i.e. any real dev/production DB, or one built the normal way
    via Database.setup()) does nothing and raises nothing -- see
    runner.py's own docstring. No separate "adopt an existing database"
    step exists or is needed."""
    db = Database(tmp_path / "current.db")
    db.setup()

    with db._conn() as conn:
        before_tables = _all_tables(conn)
        before_dev_cols = _table_columns(conn, "devices")
        migration_count_before = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]

        run_migrations(conn)  # second, redundant call -- must be a no-op

        after_tables = _all_tables(conn)
        after_dev_cols = _table_columns(conn, "devices")
        migration_count_after = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]

    assert before_tables == after_tables
    assert before_dev_cols == after_dev_cols
    assert migration_count_before == migration_count_after == len(MIGRATIONS)


def test_database_setup_runs_migrations_and_is_idempotent(tmp_path):
    """Database.setup() itself (not run_migrations() directly) is what
    real callers use -- confirm it wires the migration system in
    correctly and that calling it twice against the same file is safe
    (mirrors the old executescript()-based setup()'s own idempotency,
    since Database.setup() is called on every process start)."""
    db_path = tmp_path / "setup_twice.db"
    first = Database(db_path)
    first.setup()

    second = Database(db_path)
    second.setup()  # must not raise

    with second._conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert count == len(MIGRATIONS)


def test_fresh_database_setup_matches_migrated_old_schema(tmp_path):
    """A brand-new Database().setup() and an old-schema DB brought current
    via run_migrations() must land on the identical table/column shape --
    the two paths a real deployment can take (fresh install vs. upgrade)
    must converge."""
    fresh = Database(tmp_path / "fresh.db")
    fresh.setup()
    with fresh._conn() as fresh_conn:
        fresh_tables = _all_tables(fresh_conn)
        fresh_device_cols = _table_columns(fresh_conn, "devices")
        fresh_object_cols = _table_columns(fresh_conn, "objects")

    migrated_conn = _make_old_schema_db(tmp_path / "migrated.db")
    run_migrations(migrated_conn)
    migrated_tables = _all_tables(migrated_conn)
    migrated_device_cols = _table_columns(migrated_conn, "devices")
    migrated_object_cols = _table_columns(migrated_conn, "objects")
    migrated_conn.close()

    assert fresh_tables == migrated_tables
    assert fresh_device_cols == migrated_device_cols
    assert fresh_object_cols == migrated_object_cols


# ─── Migration ordering sanity ───────────────────────────────────────────

def test_migration_versions_are_sequential_starting_at_one():
    versions = [m.version for m in MIGRATIONS]
    assert versions == list(range(1, len(MIGRATIONS) + 1))


def test_migration_names_are_unique():
    names = [m.name for m in MIGRATIONS]
    assert len(names) == len(set(names))
