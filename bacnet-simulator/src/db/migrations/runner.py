"""Applies src/db/migrations/registry.py's MIGRATIONS list in order,
tracked in a schema_migrations table.

Every migration already carries its own idempotency guard (a PRAGMA
table_info() check before acting) inherited from the ad-hoc code this
replaced -- schema_migrations is a fast-path/audit-trail on top of that
existing safety net, not a replacement for it. That is what makes running
the full migration list against an already-fully-migrated real database
provably a no-op: the baseline schema is CREATE TABLE IF NOT EXISTS
throughout, and every later migration finds its column/shape already
present and skips its own DDL. There is no separate "adopt an existing
database" step -- the first run after upgrading an existing deployment
just backfills schema_migrations as a side effect of every migration
correctly detecting it has nothing to do.
"""
from __future__ import annotations

import sqlite3

from .registry import MIGRATIONS


def ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def run_migrations(conn: sqlite3.Connection) -> None:
    ensure_schema_migrations_table(conn)
    applied = {
        row[0] for row in conn.execute("SELECT version FROM schema_migrations")
    }
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (migration.version, migration.name),
        )
        conn.commit()
