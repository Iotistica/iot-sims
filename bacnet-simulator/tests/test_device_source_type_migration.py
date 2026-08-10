"""Schema migration tests for devices.source_type + external_* columns and
the UNIQUE(device_instance) -> UNIQUE(device_instance, source_type) change.

The migration rebuilds `devices` under PRAGMA foreign_keys=OFF (SQLite
can't ALTER a UNIQUE constraint in place, and devices has ~10 FK-dependent
child tables) -- these tests target exactly the failure modes that a naive
RENAME-based rebuild would hit (see src/legacy.py's migration comment)."""
from __future__ import annotations

import sqlite3

import pytest


def test_devices_table_has_new_columns(database):
    cols = {row[1] for row in database._conn().execute("PRAGMA table_info(devices)")}
    assert "source_type" in cols
    assert "external_host" in cols
    assert "external_port" in cols
    assert "external_vendor_id" in cols
    assert "external_last_seen_at" in cols


def test_objects_table_has_description_column(database):
    cols = {row[1] for row in database._conn().execute("PRAGMA table_info(objects)")}
    assert "description" in cols


def test_existing_devices_default_to_simulated(seeded_database):
    devices = seeded_database.get_devices()
    assert len(devices) > 0
    assert all(d["source_type"] == "simulated" for d in devices)


def test_foreign_key_integrity_after_seed(seeded_database):
    fk_problems = seeded_database._conn().execute("PRAGMA foreign_key_check").fetchall()
    assert fk_problems == []


def test_setup_is_idempotent(database):
    # Simulates an app restart against an already-migrated DB.
    database.setup()
    database.setup()
    cols = {row[1] for row in database._conn().execute("PRAGMA table_info(devices)")}
    assert "source_type" in cols


def test_device_instance_unique_within_source_type(database):
    conn = database._conn()
    conn.execute(
        "INSERT INTO devices (device_instance, name, source_type) VALUES (1003, 'a', 'simulated')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO devices (device_instance, name, source_type) VALUES (1003, 'b', 'simulated')"
        )


def test_device_instance_can_coexist_across_source_types(database):
    conn = database._conn()
    conn.execute(
        "INSERT INTO devices (device_instance, name, source_type) VALUES (1003, 'sim', 'simulated')"
    )
    conn.execute(
        "INSERT INTO devices (device_instance, name, source_type) VALUES (1003, 'ext', 'external-bacnet')"
    )
    conn.commit()
    rows = conn.execute("SELECT name, source_type FROM devices WHERE device_instance=1003").fetchall()
    assert {(r["name"], r["source_type"]) for r in rows} == {
        ("sim", "simulated"), ("ext", "external-bacnet"),
    }
