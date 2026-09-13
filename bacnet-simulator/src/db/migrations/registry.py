"""The ordered list of schema migrations applied by
src/db/migrations/runner.py -- see that module's own docstring for the
tracking mechanism (a schema_migrations table).

Migration 1 (`baseline`) is today's full schema, taken verbatim from what
used to be Database.setup()'s single executescript() call -- every
CREATE TABLE/INDEX already uses IF NOT EXISTS, so running it against an
already-fully-migrated real database is a safe no-op.

Migrations 2 onward are each one of the ad-hoc "if column/shape missing,
ALTER/rebuild" blocks that used to live inline in Database.setup(), in the
exact same order they used to run there -- that order is load-bearing in
a few places (e.g. the devices/source_type rebuild at migration 8 selects
columns that migrations 4-7 must have already added, and migration 13
ALTERs the table migration 8 just rebuilt), so do not reorder these
without re-checking each migration's own SQL for what it assumes already
exists. Each migration still re-queries PRAGMA table_info() itself right
before acting (never trusts a column snapshot taken by an earlier
migration, and never trusts schema_migrations alone) -- the exact same
idempotency guard the original inline code already had, preserved here
per CLAUDE.md's "keep migrations additive and backward-compatible" rule.

For a NEW migration: write a function taking a single sqlite3.Connection
argument, add it to MIGRATIONS below with the next integer version and a
short name, done -- no new dependency, no separate tooling to learn.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable, NamedTuple


BASELINE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS energy_history (
        id INTEGER PRIMARY KEY,
        timestamp REAL NOT NULL,
        device_id INTEGER NOT NULL,
        model_type TEXT NOT NULL,

        power_kw REAL,
        total_energy_kwh REAL,

        source TEXT,
        confidence TEXT,
        metrics TEXT NOT NULL DEFAULT '{}',

        FOREIGN KEY(device_id)
            REFERENCES devices(id)
            ON DELETE CASCADE
    );

     CREATE INDEX IF NOT EXISTS
        idx_energy_history_device_time
        ON energy_history(
            device_id,
            timestamp
        );

    CREATE INDEX IF NOT EXISTS
        idx_energy_history_timestamp
        ON energy_history(timestamp);

    CREATE TABLE IF NOT EXISTS energy_model_configs (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        model_type TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        parameters TEXT NOT NULL DEFAULT '{}',
        UNIQUE(device_id, model_type)
    );

    CREATE INDEX IF NOT EXISTS idx_energy_model_configs_device_id ON energy_model_configs(device_id);

    CREATE TABLE IF NOT EXISTS locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        parent_location_id INTEGER REFERENCES locations(id),
        description TEXT NOT NULL DEFAULT ''
    );

    -- Physical/logical building equipment (Boiler, AHU, VAV, Pump,
    -- ...) -- distinct from `devices` (the BACnet/runtime
    -- communication abstraction, a.k.a. Controller). Mirrors
    -- `locations`' own minimal shape deliberately: no
    -- parent_equipment_id (sub-equipment hierarchy is expressed
    -- via the existing isPartOf semantic relationship between two
    -- equipment-kind semantic_entities rows, not a second,
    -- competing FK-based hierarchy), no local_slug/timestamps.
    CREATE TABLE IF NOT EXISTS equipment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        location_id INTEGER REFERENCES locations(id),
        equipment_type TEXT
    );

    CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_instance INTEGER NOT NULL UNIQUE
            CHECK(device_instance >= 1 AND device_instance <= 4194302),
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        vendor_name TEXT NOT NULL DEFAULT 'Iotistica',
        model_name TEXT NOT NULL DEFAULT 'BACnet Simulator',
        enabled INTEGER NOT NULL DEFAULT 1,
        firmware_revision TEXT NOT NULL DEFAULT 'N/A',
        protocol_revision INTEGER NOT NULL DEFAULT 22,
        max_apdu_length_accepted INTEGER NOT NULL DEFAULT 1024,
        segmentation_supported TEXT NOT NULL DEFAULT 'segmented-both'
    );

    CREATE TABLE IF NOT EXISTS objects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        object_type TEXT NOT NULL,
        object_instance INTEGER NOT NULL CHECK(object_instance >= 0 AND object_instance <= 4194302),
        name TEXT NOT NULL,
        units TEXT NOT NULL DEFAULT 'no-units',
        behavior TEXT NOT NULL DEFAULT 'constant',
        behavior_params TEXT NOT NULL DEFAULT '{"value":0}',
        enabled INTEGER NOT NULL DEFAULT 1,
        manual_value REAL,
        number_of_states INTEGER NOT NULL DEFAULT 2,
        reliability TEXT NOT NULL DEFAULT 'no-fault-detected',
        polarity TEXT NOT NULL DEFAULT 'normal',
        UNIQUE(device_id, object_type, object_instance)
    );

    CREATE TABLE IF NOT EXISTS semantic_entities (
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
    CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_entities_semantic_key
        ON semantic_entities(semantic_key) WHERE semantic_key IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_entities_object_unique
        ON semantic_entities(object_id) WHERE entity_kind = 'point' AND object_id IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_entities_location_unique
        ON semantic_entities(location_id) WHERE entity_kind = 'location' AND location_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_semantic_entities_device ON semantic_entities(device_id);
    CREATE INDEX IF NOT EXISTS idx_semantic_entities_brick_class ON semantic_entities(brick_class);

    CREATE TABLE IF NOT EXISTS semantic_relationships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_entity_id INTEGER NOT NULL REFERENCES semantic_entities(id) ON DELETE CASCADE,
        predicate TEXT NOT NULL CHECK(predicate IN ('isPointOf', 'isPartOf', 'feeds', 'hasLocation')),
        target_entity_id INTEGER NOT NULL REFERENCES semantic_entities(id) ON DELETE CASCADE,
        UNIQUE(source_entity_id, predicate, target_entity_id)
    );
    CREATE INDEX IF NOT EXISTS idx_semantic_relationships_target ON semantic_relationships(target_entity_id, predicate);

    CREATE TABLE IF NOT EXISTS functional_tests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        equipment_type TEXT NOT NULL,
        definition_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS custom_graphs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        definition_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS functional_test_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        functional_test_id INTEGER NOT NULL REFERENCES functional_tests(id) ON DELETE CASCADE,
        target_device_id INTEGER REFERENCES devices(id),
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

    CREATE TABLE IF NOT EXISTS profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        device_count INTEGER NOT NULL DEFAULT 0,
        data TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_login_at TEXT
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS notification_classes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        priority_to_offnormal INTEGER NOT NULL DEFAULT 100,
        priority_to_fault INTEGER NOT NULL DEFAULT 100,
        priority_to_normal INTEGER NOT NULL DEFAULT 100,
        ack_required_transitions TEXT NOT NULL DEFAULT '["to-offnormal","to-fault"]',
        recipients TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE IF NOT EXISTS object_alarm_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id INTEGER NOT NULL UNIQUE REFERENCES objects(id) ON DELETE CASCADE,
        notification_class_id INTEGER REFERENCES notification_classes(id) ON DELETE SET NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        event_enable TEXT NOT NULL DEFAULT '["to-offnormal","to-fault","to-normal"]',
        notify_type TEXT NOT NULL DEFAULT 'alarm',
        time_delay INTEGER NOT NULL DEFAULT 0,
        time_delay_normal INTEGER NOT NULL DEFAULT 0,
        params TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS alarm_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        object_id INTEGER REFERENCES objects(id) ON DELETE CASCADE,
        device_id INTEGER NOT NULL,
        object_name TEXT NOT NULL,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        priority INTEGER NOT NULL,
        value TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '',
        ts TEXT NOT NULL DEFAULT (datetime('now')),
        ack_required INTEGER NOT NULL DEFAULT 0,
        acknowledged INTEGER NOT NULL DEFAULT 0,
        ack_ts TEXT,
        ack_by TEXT
    );

    CREATE TABLE IF NOT EXISTS event_enrollments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        monitored_object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
        algorithm TEXT NOT NULL DEFAULT 'change-of-state',
        event_parameters TEXT NOT NULL DEFAULT '{}',
        notification_class_id INTEGER REFERENCES notification_classes(id) ON DELETE SET NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        event_enable TEXT NOT NULL DEFAULT '["to-offnormal","to-fault","to-normal"]',
        notify_type TEXT NOT NULL DEFAULT 'event',
        time_delay INTEGER NOT NULL DEFAULT 0,
        time_delay_normal INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS trend_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        monitored_object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
        logging_type TEXT NOT NULL DEFAULT 'polled',
        log_interval INTEGER NOT NULL DEFAULT 60,
        cov_increment REAL NOT NULL DEFAULT 1.0,
        buffer_size INTEGER NOT NULL DEFAULT 1000,
        stop_when_full INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        record_count INTEGER NOT NULL DEFAULT 0,
        total_record_count INTEGER NOT NULL DEFAULT 0,
        last_sampled_at REAL
    );

    CREATE TABLE IF NOT EXISTS trend_log_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trend_log_id INTEGER NOT NULL REFERENCES trend_logs(id) ON DELETE CASCADE,
        sequence_number INTEGER NOT NULL,
        ts TEXT NOT NULL DEFAULT (datetime('now')),
        value TEXT NOT NULL,
        status_flags TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX IF NOT EXISTS idx_trend_records_log_seq ON trend_log_records(trend_log_id, sequence_number);
    CREATE INDEX IF NOT EXISTS idx_trend_records_log_ts ON trend_log_records(trend_log_id, ts);

    CREATE TABLE IF NOT EXISTS bacnet_schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        value_type TEXT NOT NULL DEFAULT 'real',
        schedule_default TEXT NOT NULL DEFAULT '0',
        effective_start TEXT,
        effective_end TEXT,
        weekly_schedule TEXT NOT NULL DEFAULT '{}',
        exception_schedule TEXT NOT NULL DEFAULT '[]',
        priority_for_writing INTEGER NOT NULL DEFAULT 10
            CHECK(priority_for_writing >= 1 AND priority_for_writing <= 16),
        enabled INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS bacnet_schedule_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id INTEGER NOT NULL REFERENCES bacnet_schedules(id) ON DELETE CASCADE,
        object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
        property_identifier TEXT NOT NULL DEFAULT 'present-value'
    );

    CREATE TABLE IF NOT EXISTS bacnet_calendars (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        date_list TEXT NOT NULL DEFAULT '[]',
        enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS fault_rule_configs (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        rule_id TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        parameters TEXT NOT NULL DEFAULT '{}',
        persistence_seconds REAL,
        clear_seconds REAL,
        severity TEXT,
        UNIQUE(device_id, rule_id)
    );

    CREATE TABLE IF NOT EXISTS fault_events (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        rule_id TEXT NOT NULL,
        state TEXT NOT NULL,
        previous_state TEXT NOT NULL,
        severity TEXT NOT NULL,
        message TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT '[]',
        timestamp REAL NOT NULL,
        activated_at REAL,
        cleared_at REAL
    );

    CREATE INDEX IF NOT EXISTS idx_fault_events_device_id ON fault_events(device_id);
    CREATE INDEX IF NOT EXISTS idx_fault_events_rule_id ON fault_events(rule_id);
"""


def _migration_001_baseline(conn: sqlite3.Connection) -> None:
    conn.executescript(BASELINE_SCHEMA_SQL)


def _migration_002_objects_fault_columns(conn: sqlite3.Connection) -> None:
    """number_of_states/reliability/polarity were added to objects after it
    first shipped -- backfill for existing DBs instead of requiring a
    fresh one."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(objects)")}
    if "number_of_states" not in existing_cols:
        conn.execute("ALTER TABLE objects ADD COLUMN number_of_states INTEGER NOT NULL DEFAULT 2")
    if "reliability" not in existing_cols:
        conn.execute("ALTER TABLE objects ADD COLUMN reliability TEXT NOT NULL DEFAULT 'no-fault-detected'")
    if "polarity" not in existing_cols:
        conn.execute("ALTER TABLE objects ADD COLUMN polarity TEXT NOT NULL DEFAULT 'normal'")


def _migration_003_trend_logs_cov_increment(conn: sqlite3.Connection) -> None:
    """cov_increment was added to trend_logs after it first shipped
    (Phase 1) -- backfill for existing DBs too."""
    existing_tl_cols = {row[1] for row in conn.execute("PRAGMA table_info(trend_logs)")}
    if "cov_increment" not in existing_tl_cols:
        conn.execute("ALTER TABLE trend_logs ADD COLUMN cov_increment REAL NOT NULL DEFAULT 1.0")


def _migration_004_devices_object_info_columns(conn: sqlite3.Connection) -> None:
    """Device object info properties (GH #19) were added after devices
    first shipped -- backfill for existing DBs."""
    existing_dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "firmware_revision" not in existing_dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN firmware_revision TEXT NOT NULL DEFAULT 'N/A'")
    if "protocol_revision" not in existing_dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN protocol_revision INTEGER NOT NULL DEFAULT 22")
    if "max_apdu_length_accepted" not in existing_dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN max_apdu_length_accepted INTEGER NOT NULL DEFAULT 1024")
    if "segmentation_supported" not in existing_dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN segmentation_supported TEXT NOT NULL DEFAULT 'segmented-both'")


def _migration_005_devices_location_id(conn: sqlite3.Connection) -> None:
    """Location (organizational grouping, no BACnet protocol meaning) was
    added after devices first shipped."""
    existing_dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "location_id" not in existing_dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN location_id INTEGER REFERENCES locations(id)")


def _migration_006_devices_equipment_type(conn: sqlite3.Connection) -> None:
    """Brick/Haystack-style semantic metadata (optional layer) -- never
    read by the BACnet protocol/simulation engine, purely descriptive.
    See src/config.py's EQUIPMENT_TYPES/POINT_TYPES/LOCATION_KINDS for
    the pinned vocabulary."""
    existing_dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "equipment_type" not in existing_dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN equipment_type TEXT")


def _migration_007_devices_can_receive_event_notifications(conn: sqlite3.Connection) -> None:
    """Explicit per-device override for whether a device can receive
    BACnet Event Notifications. NULL (the default) means "infer from
    equipment_type" -- see _effective_can_receive_events(). Only an
    explicit 0/1 overrides that inference."""
    existing_dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "can_receive_event_notifications" not in existing_dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN can_receive_event_notifications INTEGER")


def _migration_008_devices_source_type_rebuild(conn: sqlite3.Connection) -> None:
    """devices gains a source_type discriminator (simulated vs.
    external-bacnet, see src/api/routers/discovery.py) and a
    UNIQUE(device_instance) -> UNIQUE(device_instance, source_type)
    constraint change, so a discovered external device and a future
    simulated copy of it can coexist at the same BACnet instance. SQLite
    can't ALTER a UNIQUE constraint in place, and `devices` has ~10
    FK-dependent child tables (objects, energy_model_configs,
    semantic_entities, trend_logs, bacnet_schedules, bacnet_calendars,
    fault_rule_configs, fault_events, notification_classes,
    event_enrollments, energy_history) with PRAGMA foreign_keys=ON (see
    Database._conn()) -- verified live that the usual RENAME-old/
    recreate/DROP-old migration pattern (used for
    _migration_017_energy_model_configs_instance_key below) is unsafe
    here: RENAME rewrites every child table's stored REFERENCES text to
    point at the renamed-away name, and DROP TABLE on a table with
    incoming FK references fails outright once children hold rows. This
    follows SQLite's own documented procedure for that case instead:
    disable FK enforcement for the rebuild, rebuild under a new name,
    DROP the ORIGINAL (never renamed, so no child SQL text ever needs
    rewriting), rename the new table into place, verify with
    foreign_key_check before committing, then re-enable enforcement.

    MUST run after _migration_004..007 above: the CREATE TABLE
    devices_new / INSERT INTO devices_new SELECT ... FROM devices below
    references firmware_revision/protocol_revision/
    max_apdu_length_accepted/segmentation_supported/location_id/
    equipment_type/can_receive_event_notifications, which those earlier
    migrations are what add in the first place -- do not renumber this
    ahead of them."""
    existing_dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "source_type" not in existing_dev_cols:
        conn.commit()  # flush any pending transaction -- foreign_keys can't be toggled mid-transaction
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE devices_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_instance INTEGER NOT NULL
                    CHECK(device_instance >= 1 AND device_instance <= 4194302),
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                vendor_name TEXT NOT NULL DEFAULT 'Iotistica',
                model_name TEXT NOT NULL DEFAULT 'BACnet Simulator',
                enabled INTEGER NOT NULL DEFAULT 1,
                firmware_revision TEXT NOT NULL DEFAULT 'N/A',
                protocol_revision INTEGER NOT NULL DEFAULT 22,
                max_apdu_length_accepted INTEGER NOT NULL DEFAULT 1024,
                segmentation_supported TEXT NOT NULL DEFAULT 'segmented-both',
                location_id INTEGER REFERENCES locations(id),
                equipment_type TEXT,
                can_receive_event_notifications INTEGER,
                source_type TEXT NOT NULL DEFAULT 'simulated'
                    CHECK(source_type IN ('simulated','external-bacnet')),
                external_host TEXT,
                external_port INTEGER,
                external_vendor_id INTEGER,
                external_last_seen_at TEXT,
                UNIQUE(device_instance, source_type)
            )
        """)
        conn.execute("""
            INSERT INTO devices_new (
                id, device_instance, name, description, vendor_name, model_name,
                enabled, firmware_revision, protocol_revision, max_apdu_length_accepted,
                segmentation_supported, location_id, equipment_type,
                can_receive_event_notifications, source_type
            )
            SELECT
                id, device_instance, name, description, vendor_name, model_name,
                enabled, firmware_revision, protocol_revision, max_apdu_length_accepted,
                segmentation_supported, location_id, equipment_type,
                can_receive_event_notifications, 'simulated'
            FROM devices
        """)
        conn.execute("DROP TABLE devices")
        conn.execute("ALTER TABLE devices_new RENAME TO devices")
        fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_problems:
            conn.rollback()
            conn.execute("PRAGMA foreign_keys = ON")
            raise RuntimeError(f"devices migration broke FK integrity: {fk_problems}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_source_type ON devices(source_type)")


def _migration_009_objects_point_type(conn: sqlite3.Connection) -> None:
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(objects)")}
    if "point_type" not in existing_cols:
        conn.execute("ALTER TABLE objects ADD COLUMN point_type TEXT")


def _migration_010_objects_description(conn: sqlite3.Connection) -> None:
    """description, for external-BACnet discovered points (see
    src/api/routers/external_objects.py) -- also usable for simulated
    objects later, never interpreted by SimEngine."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(objects)")}
    if "description" not in existing_cols:
        conn.execute("ALTER TABLE objects ADD COLUMN description TEXT")


def _migration_011_locations_kind(conn: sqlite3.Connection) -> None:
    existing_loc_cols = {row[1] for row in conn.execute("PRAGMA table_info(locations)")}
    if "kind" not in existing_loc_cols:
        conn.execute("ALTER TABLE locations ADD COLUMN kind TEXT")


def _migration_012_locations_sort_order(conn: sqlite3.Connection) -> None:
    """sort_order, used only by auto-generated Building/Level hierarchies
    (see Database.generate_building_levels) so sibling display order
    survives a rename. NULL for every existing/manually-created location
    -- those keep sorting by name exactly as before (see
    Database.get_locations())."""
    existing_loc_cols = {row[1] for row in conn.execute("PRAGMA table_info(locations)")}
    if "sort_order" not in existing_loc_cols:
        conn.execute("ALTER TABLE locations ADD COLUMN sort_order INTEGER")


def _migration_013_devices_simulation_mode(conn: sqlite3.Connection) -> None:
    """MUST run after _migration_008 (source_type rebuild) -- ALTERs the
    devices table that migration rebuilds."""
    existing_dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "simulation_mode" not in existing_dev_cols:
        conn.execute(
            "ALTER TABLE devices ADD COLUMN simulation_mode TEXT NOT NULL DEFAULT 'simulation' "
            "CHECK(simulation_mode IN ('simulation','mirror','replay'))"
        )


def _migration_014_devices_source_device_id(conn: sqlite3.Connection) -> None:
    existing_dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "source_device_id" not in existing_dev_cols:
        conn.execute(
            "ALTER TABLE devices ADD COLUMN source_device_id INTEGER REFERENCES devices(id)"
        )


def _migration_015_functional_test_runs_target_device_nullable(conn: sqlite3.Connection) -> None:
    """functional_test_runs.target_device_id becomes nullable with no
    ON DELETE CASCADE -- every point in a saved test definition now
    carries its own device (see the Functional Tests HVAC-regression
    plan), so a run no longer has one single target device to require or
    cascade from; new rows always insert NULL (see
    Database.create_functional_test_run), old rows keep their historical
    value. Accepted trade-off: deleting a device no longer
    cascade-deletes runs that referenced it -- fine, since details_json
    already snapshots point/device identity as data, so an orphaned run
    stays meaningful. SQLite can't ALTER a NOT NULL/FK-cascade constraint
    in place, so this follows the same safe rebuild procedure as
    _migration_008 above."""
    ftr_cols = {
        row[1]: row for row in conn.execute("PRAGMA table_info(functional_test_runs)").fetchall()
    }
    target_device_col = ftr_cols.get("target_device_id")
    if target_device_col is not None and target_device_col[3] == 1:  # notnull flag
        conn.commit()  # flush any pending transaction -- foreign_keys can't be toggled mid-transaction
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE functional_test_runs_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                functional_test_id INTEGER NOT NULL REFERENCES functional_tests(id) ON DELETE CASCADE,
                target_device_id INTEGER REFERENCES devices(id),
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
            )
        """)
        conn.execute("""
            INSERT INTO functional_test_runs_new (
                id, functional_test_id, target_device_id, execution_mode, state,
                started_at, finished_at, result, result_message, current_node_id,
                error, details_json, created_at
            )
            SELECT
                id, functional_test_id, target_device_id, execution_mode, state,
                started_at, finished_at, result, result_message, current_node_id,
                error, details_json, created_at
            FROM functional_test_runs
        """)
        conn.execute("DROP TABLE functional_test_runs")
        conn.execute("ALTER TABLE functional_test_runs_new RENAME TO functional_test_runs")
        fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_problems:
            conn.rollback()
            conn.execute("PRAGMA foreign_keys = ON")
            raise RuntimeError(f"functional_test_runs migration broke FK integrity: {fk_problems}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")


def _migration_016_semantic_entities_equipment_and_controller(conn: sqlite3.Connection) -> None:
    """semantic_entities gains an equipment_id FK (physical equipment, see
    the `equipment` table) and entity_kind gains 'controller' (the
    BACnet/runtime device's own Brick Controller identity, distinct from
    'equipment' -- see src/semantics/mirror.py's sync_controller_entity).
    SQLite can't ALTER a CHECK constraint or add a REFERENCES column with
    a new target table in place, so this follows the exact same safe
    rebuild procedure as _migration_008: semantic_relationships holds FK
    REFERENCES pointing INTO semantic_entities, so the ORIGINAL table is
    dropped (never renamed away first) and the _new table renamed into
    its place, preserving every row's original id (and therefore every
    semantic_relationships row's source_entity_id/target_entity_id still
    resolving correctly) -- verified via PRAGMA foreign_key_check before
    committing. Both this table and semantic_relationships (predicate
    gains 'controls'/'isHostedBy') are migrated together, gated on the
    same detection check, since they always ship together."""
    existing_se_cols = {row[1] for row in conn.execute("PRAGMA table_info(semantic_entities)")}
    if "equipment_id" not in existing_se_cols:
        conn.commit()  # flush any pending transaction -- foreign_keys can't be toggled mid-transaction
        conn.execute("PRAGMA foreign_keys = OFF")

        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE semantic_entities_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                local_slug TEXT,
                semantic_key TEXT,
                brick_class TEXT NOT NULL,
                entity_kind TEXT NOT NULL CHECK(entity_kind IN ('equipment', 'point', 'location', 'controller')),
                device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
                object_id INTEGER REFERENCES objects(id) ON DELETE CASCADE,
                location_id INTEGER REFERENCES locations(id),
                equipment_id INTEGER REFERENCES equipment(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            INSERT INTO semantic_entities_new (
                id, name, local_slug, semantic_key, brick_class,
                entity_kind, device_id, object_id, location_id
            )
            SELECT
                id, name, local_slug, semantic_key, brick_class,
                entity_kind, device_id, object_id, location_id
            FROM semantic_entities
        """)
        conn.execute("DROP TABLE semantic_entities")
        conn.execute("ALTER TABLE semantic_entities_new RENAME TO semantic_entities")
        fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_problems:
            conn.rollback()
            conn.execute("PRAGMA foreign_keys = ON")
            raise RuntimeError(f"semantic_entities migration broke FK integrity: {fk_problems}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_entities_semantic_key
                ON semantic_entities(semantic_key) WHERE semantic_key IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_entities_object_unique
                ON semantic_entities(object_id) WHERE entity_kind = 'point' AND object_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_entities_location_unique
                ON semantic_entities(location_id) WHERE entity_kind = 'location' AND location_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_semantic_entities_device ON semantic_entities(device_id);
            CREATE INDEX IF NOT EXISTS idx_semantic_entities_brick_class ON semantic_entities(brick_class);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_entities_equipment_unique
                ON semantic_entities(equipment_id) WHERE entity_kind = 'equipment' AND equipment_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_entities_controller_unique
                ON semantic_entities(device_id) WHERE entity_kind = 'controller';
        """)

        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE semantic_relationships_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_entity_id INTEGER NOT NULL REFERENCES semantic_entities(id) ON DELETE CASCADE,
                predicate TEXT NOT NULL CHECK(predicate IN ('isPointOf', 'isPartOf', 'feeds', 'hasLocation', 'controls', 'isHostedBy')),
                target_entity_id INTEGER NOT NULL REFERENCES semantic_entities(id) ON DELETE CASCADE,
                UNIQUE(source_entity_id, predicate, target_entity_id)
            )
        """)
        conn.execute("""
            INSERT INTO semantic_relationships_new (id, source_entity_id, predicate, target_entity_id)
            SELECT id, source_entity_id, predicate, target_entity_id FROM semantic_relationships
        """)
        conn.execute("DROP TABLE semantic_relationships")
        conn.execute("ALTER TABLE semantic_relationships_new RENAME TO semantic_relationships")
        fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_problems:
            conn.rollback()
            conn.execute("PRAGMA foreign_keys = ON")
            raise RuntimeError(f"semantic_relationships migration broke FK integrity: {fk_problems}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_semantic_relationships_target "
            "ON semantic_relationships(target_entity_id, predicate)"
        )


def _migration_017_energy_model_configs_instance_key(conn: sqlite3.Connection) -> None:
    existing_energy_cols = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(energy_model_configs)"
        )
    }

    if "instance_key" not in existing_energy_cols:
        conn.executescript(
            """
            ALTER TABLE energy_model_configs
            RENAME TO energy_model_configs_old;

            CREATE TABLE energy_model_configs (
                id INTEGER PRIMARY KEY,
                device_id INTEGER NOT NULL
                    REFERENCES devices(id)
                    ON DELETE CASCADE,
                model_type TEXT NOT NULL,
                instance_key TEXT NOT NULL DEFAULT 'default',
                enabled INTEGER NOT NULL DEFAULT 1,
                parameters TEXT NOT NULL DEFAULT '{}',
                UNIQUE(device_id, model_type, instance_key)
            );

            INSERT INTO energy_model_configs (
                id,
                device_id,
                model_type,
                instance_key,
                enabled,
                parameters
            )
            SELECT
                id,
                device_id,
                model_type,
                'default',
                enabled,
                parameters
            FROM energy_model_configs_old;

            DROP TABLE energy_model_configs_old;

            CREATE INDEX IF NOT EXISTS
                idx_energy_model_configs_device_id
            ON energy_model_configs(device_id);
            """
        )


def _migration_019_replay_recording_tables(conn: sqlite3.Connection) -> None:
    """Replay Recording (application-managed, SQLite-backed device-wide
    recording of an external BACnet device's values) -- see the "Replay"
    mode in CreateSimulatedCopyModal.vue, previously hard-disabled with no
    backing data model. Mirrors trend_logs/trend_log_records' shape
    (config row + child sample rows), except a recording is device-wide
    (many points sampled together per cycle) rather than one log per
    point, so there's an extra replay_recording_points table between them
    recording which points were selected and their identity at record
    time (kept even if the source device/object later changes or is
    deleted -- source_object_id is ON DELETE SET NULL, not CASCADE, so a
    recording stays usable for Replay regardless)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS replay_recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'recording' CHECK(status IN ('recording','completed')),
            sample_interval_seconds REAL NOT NULL,
            maximum_samples INTEGER NOT NULL,
            buffer_mode TEXT NOT NULL CHECK(buffer_mode IN ('overwrite','stop')),
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_replay_recordings_device ON replay_recordings(source_device_id);

        CREATE TABLE IF NOT EXISTS replay_recording_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL REFERENCES replay_recordings(id) ON DELETE CASCADE,
            source_object_id INTEGER REFERENCES objects(id) ON DELETE SET NULL,
            object_type TEXT NOT NULL,
            object_instance INTEGER NOT NULL,
            object_name TEXT NOT NULL,
            point_type TEXT,
            units TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_replay_recording_points_recording ON replay_recording_points(recording_id);

        CREATE TABLE IF NOT EXISTS replay_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL REFERENCES replay_recordings(id) ON DELETE CASCADE,
            recording_point_id INTEGER NOT NULL REFERENCES replay_recording_points(id) ON DELETE CASCADE,
            sample_index INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            value TEXT NOT NULL,
            reliability TEXT,
            out_of_service INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_replay_samples_recording_index ON replay_samples(recording_id, sample_index);
        CREATE INDEX IF NOT EXISTS idx_replay_samples_recording_ts ON replay_samples(recording_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_replay_samples_point_index ON replay_samples(recording_point_id, sample_index);
        """
    )
    existing_dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "replay_recording_id" not in existing_dev_cols:
        conn.execute(
            "ALTER TABLE devices ADD COLUMN replay_recording_id "
            "INTEGER REFERENCES replay_recordings(id) ON DELETE SET NULL"
        )


def _migration_020_equipment_manufacturer_model(conn: sqlite3.Connection) -> None:
    """Free-text descriptive fields (like devices' own vendor_name/model_name)
    -- never read by the BACnet protocol/simulation engine, no relation to
    equipment_type's Brick vocabulary."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(equipment)")}
    if "manufacturer" not in existing_cols:
        conn.execute("ALTER TABLE equipment ADD COLUMN manufacturer TEXT")
    if "model" not in existing_cols:
        conn.execute("ALTER TABLE equipment ADD COLUMN model TEXT")


def _migration_021_ai_suggestion_acceptances(conn: sqlite3.Connection) -> None:
    """Records every AI-suggested point classification (src/semantics/
    ai_suggestions.py) the user actually applied via the Semantic
    Suggestions modal -- accepted_class vs suggested_class distinguishes
    "applied as-is" from "AI suggested X, user corrected to Y before
    applying" (the more valuable signal for future fine-tuning). Deletes
    with its point (ON DELETE CASCADE) -- without the point's own name/
    units/description, a row here is no longer a usable training example
    anyway, nothing here is meant to outlive the point it describes."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ai_suggestion_acceptances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
            device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            suggested_class TEXT NOT NULL,
            accepted_class TEXT NOT NULL,
            confidence TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ai_suggestion_acceptances_object ON ai_suggestion_acceptances(object_id);
        CREATE INDEX IF NOT EXISTS idx_ai_suggestion_acceptances_device ON ai_suggestion_acceptances(device_id);
        """
    )


# The 8 built-in object templates, ported verbatim from admin/src/templates.ts
# (written earlier this session) -- same keys, labels, descriptions, object
# lists, and equipmentTypes tags. Icons aren't ported (not serializable);
# the frontend keeps its own key-keyed icon lookup for these same 8 keys.
_BUILTIN_TEMPLATES: list[dict] = [
    {
        "key": "ahu",
        "label": "Air Handling Unit",
        "description": "Supply/return fans, temps, valves, static pressure, alarms",
        "equipment_types": ["Air_Handling_Unit", "Rooftop_Unit"],
        "objects": [
            {"object_type": "binary-input", "object_instance": 1, "name": "SF-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Fan_Status"},
            {"object_type": "binary-input", "object_instance": 2, "name": "RF-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Fan_Status"},
            {"object_type": "analog-input", "object_instance": 3, "name": "SF-Speed", "units": "percent", "behavior": "sine", "behavior_params": '{"base":75,"amplitude":15,"period_hours":12}'},
            {"object_type": "analog-input", "object_instance": 4, "name": "RF-Speed", "units": "percent", "behavior": "sine", "behavior_params": '{"base":70,"amplitude":12,"period_hours":12}'},
            {"object_type": "analog-input", "object_instance": 5, "name": "SAT", "units": "degrees-celsius", "behavior": "noise", "behavior_params": '{"base":13,"noise":0.4}', "point_type": "Supply_Air_Temperature_Sensor"},
            {"object_type": "analog-input", "object_instance": 6, "name": "RAT", "units": "degrees-celsius", "behavior": "sine", "behavior_params": '{"base":22,"amplitude":2,"period_hours":24}', "point_type": "Return_Air_Temperature_Sensor"},
            {"object_type": "analog-input", "object_instance": 7, "name": "MAT", "units": "degrees-celsius", "behavior": "noise", "behavior_params": '{"base":16,"noise":0.8}', "point_type": "Mixed_Air_Temperature_Sensor"},
            {"object_type": "analog-input", "object_instance": 8, "name": "OAT", "units": "degrees-celsius", "behavior": "sine", "behavior_params": '{"base":12,"amplitude":8,"period_hours":24}', "point_type": "Outside_Air_Temperature_Sensor"},
            {"object_type": "analog-output", "object_instance": 9, "name": "OAD-Position", "units": "percent", "behavior": "sine", "behavior_params": '{"base":28,"amplitude":18,"period_hours":24}', "point_type": "Damper_Position_Command"},
            {"object_type": "analog-output", "object_instance": 10, "name": "CC-Valve", "units": "percent", "behavior": "sine", "behavior_params": '{"base":55,"amplitude":25,"period_hours":12}', "point_type": "Valve_Position_Command"},
            {"object_type": "analog-output", "object_instance": 11, "name": "HC-Valve", "units": "percent", "behavior": "sine", "behavior_params": '{"base":10,"amplitude":9,"period_hours":24}', "point_type": "Valve_Position_Command"},
            {"object_type": "analog-input", "object_instance": 12, "name": "SA-Flow", "units": "cubic-feet-per-minute", "behavior": "noise", "behavior_params": '{"base":8500,"noise":250}', "point_type": "Air_Flow_Sensor"},
            {"object_type": "analog-input", "object_instance": 13, "name": "SA-Static-Pressure", "units": "pascals", "behavior": "noise", "behavior_params": '{"base":375,"noise":12}', "point_type": "Static_Pressure_Sensor"},
            {"object_type": "binary-input", "object_instance": 14, "name": "Filter-DP-Alarm", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":false}', "point_type": "Change_Filter_Alarm"},
            {"object_type": "binary-input", "object_instance": 15, "name": "Freeze-Stat", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":false}', "point_type": "Freeze_Status"},
        ],
    },
    {
        "key": "vav",
        "label": "VAV Box",
        "description": "Zone temp, airflow, damper, reheat valve, CO₂, occupancy",
        "equipment_types": ["Variable_Air_Volume_Box"],
        "objects": [
            {"object_type": "analog-input", "object_instance": 1, "name": "Zone-Temp", "units": "degrees-celsius", "behavior": "noise", "behavior_params": '{"base":22,"noise":0.3}', "point_type": "Zone_Air_Temperature_Sensor"},
            {"object_type": "analog-value", "object_instance": 2, "name": "Zone-Setpoint", "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":22}', "point_type": "Room_Air_Temperature_Setpoint"},
            {"object_type": "analog-input", "object_instance": 3, "name": "Damper-Pos", "units": "percent", "behavior": "noise", "behavior_params": '{"base":55,"noise":3}', "point_type": "Damper_Position_Status"},
            {"object_type": "analog-output", "object_instance": 4, "name": "Damper-Cmd", "units": "percent", "behavior": "sine", "behavior_params": '{"base":55,"amplitude":14,"period_hours":8}', "point_type": "Damper_Position_Command"},
            {"object_type": "analog-input", "object_instance": 5, "name": "Zone-Airflow", "units": "cubic-feet-per-minute", "behavior": "noise", "behavior_params": '{"base":350,"noise":18}', "point_type": "Air_Flow_Sensor"},
            {"object_type": "analog-output", "object_instance": 6, "name": "Reheat-Valve", "units": "percent", "behavior": "sine", "behavior_params": '{"base":0,"amplitude":10,"period_hours":12}', "point_type": "Valve_Position_Command"},
            {"object_type": "binary-input", "object_instance": 7, "name": "Occupancy", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Occupancy_Sensor"},
            {"object_type": "analog-input", "object_instance": 8, "name": "Zone-CO2", "units": "parts-per-million", "behavior": "random_walk", "behavior_params": '{"value":650,"step":30,"min":400,"max":1200}', "point_type": "CO2_Level_Sensor"},
        ],
    },
    {
        "key": "fcu",
        "label": "Fan Coil Unit",
        "description": "Room temp, setpoint, cooling/heating valves, fan speeds",
        "equipment_types": None,
        "objects": [
            {"object_type": "analog-input", "object_instance": 1, "name": "Room-Temp", "units": "degrees-celsius", "behavior": "sine", "behavior_params": '{"base":23,"amplitude":1,"period_hours":24}', "point_type": "Zone_Air_Temperature_Sensor"},
            {"object_type": "analog-value", "object_instance": 2, "name": "Room-Setpoint", "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":22}', "point_type": "Room_Air_Temperature_Setpoint"},
            {"object_type": "analog-input", "object_instance": 3, "name": "Coil-Temp", "units": "degrees-celsius", "behavior": "noise", "behavior_params": '{"base":12,"noise":0.5}', "point_type": "Temperature_Sensor"},
            {"object_type": "analog-output", "object_instance": 4, "name": "Cooling-Valve", "units": "percent", "behavior": "manual", "behavior_params": '{"value":0}', "point_type": "Valve_Position_Command"},
            {"object_type": "analog-output", "object_instance": 5, "name": "Heating-Valve", "units": "percent", "behavior": "manual", "behavior_params": '{"value":0}', "point_type": "Valve_Position_Command"},
            {"object_type": "binary-output", "object_instance": 6, "name": "Fan-Low-Speed", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Fan_Speed_Command"},
            {"object_type": "binary-output", "object_instance": 7, "name": "Fan-High-Speed", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":false}', "point_type": "Fan_Speed_Command"},
        ],
    },
    {
        "key": "chiller",
        "label": "Chiller Plant",
        "description": "Dual chillers, condenser tower, CW loop flow & temps",
        "equipment_types": ["Chiller"],
        "objects": [
            {"object_type": "binary-input", "object_instance": 1, "name": "CH-1-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Run_Status"},
            {"object_type": "analog-input", "object_instance": 2, "name": "CH-1-kW", "units": "kilowatts", "behavior": "random_walk", "behavior_params": '{"value":212,"step":8,"min":80,"max":320}', "point_type": "Power_Sensor"},
            {"object_type": "analog-input", "object_instance": 3, "name": "CH-1-COP", "units": "no-units", "behavior": "noise", "behavior_params": '{"base":5.8,"noise":0.2}'},
            {"object_type": "binary-input", "object_instance": 4, "name": "CH-2-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Run_Status"},
            {"object_type": "analog-input", "object_instance": 5, "name": "CH-2-kW", "units": "kilowatts", "behavior": "random_walk", "behavior_params": '{"value":198,"step":8,"min":80,"max":320}', "point_type": "Power_Sensor"},
            {"object_type": "analog-input", "object_instance": 6, "name": "CH-2-COP", "units": "no-units", "behavior": "noise", "behavior_params": '{"base":5.6,"noise":0.2}'},
            {"object_type": "analog-input", "object_instance": 7, "name": "CW-Supply-Temp", "units": "degrees-celsius", "behavior": "noise", "behavior_params": '{"base":6.5,"noise":0.2}', "point_type": "Condenser_Water_Temperature_Sensor"},
            {"object_type": "analog-input", "object_instance": 8, "name": "CW-Return-Temp", "units": "degrees-celsius", "behavior": "noise", "behavior_params": '{"base":12.2,"noise":0.2}', "point_type": "Condenser_Water_Temperature_Sensor"},
            {"object_type": "analog-input", "object_instance": 9, "name": "CW-Flow", "units": "liters-per-second", "behavior": "noise", "behavior_params": '{"base":48,"noise":1.5}', "point_type": "Water_Flow_Sensor"},
            {"object_type": "analog-input", "object_instance": 10, "name": "CW-Diff-Pressure", "units": "pascals", "behavior": "noise", "behavior_params": '{"base":225,"noise":8}', "point_type": "Water_Differential_Pressure_Sensor"},
            {"object_type": "binary-input", "object_instance": 11, "name": "CT-Fan-1-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Fan_Status"},
            {"object_type": "binary-input", "object_instance": 12, "name": "CT-Fan-2-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Fan_Status"},
            {"object_type": "analog-input", "object_instance": 13, "name": "CT-Leaving-Water-Temp", "units": "degrees-celsius", "behavior": "noise", "behavior_params": '{"base":29.5,"noise":0.5}', "point_type": "Condenser_Water_Temperature_Sensor"},
            {"object_type": "binary-input", "object_instance": 15, "name": "CW-Pump-1-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Run_Status"},
            {"object_type": "binary-input", "object_instance": 16, "name": "CW-Pump-2-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":false}', "point_type": "Run_Status"},
        ],
    },
    {
        "key": "boiler",
        "label": "Hot Water Boiler",
        "description": "Dual boilers, HW supply/return temps, pumps, gas flow",
        "equipment_types": ["Boiler"],
        "objects": [
            {"object_type": "binary-input", "object_instance": 1, "name": "BLR-1-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Run_Status"},
            {"object_type": "analog-input", "object_instance": 2, "name": "BLR-1-Firing-Rate", "units": "percent", "behavior": "noise", "behavior_params": '{"base":62,"noise":5}'},
            {"object_type": "analog-input", "object_instance": 3, "name": "BLR-1-Flue-Temp", "units": "degrees-celsius", "behavior": "noise", "behavior_params": '{"base":88,"noise":3}', "point_type": "Temperature_Sensor"},
            {"object_type": "binary-input", "object_instance": 4, "name": "BLR-2-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":false}', "point_type": "Run_Status"},
            {"object_type": "analog-input", "object_instance": 5, "name": "BLR-2-Firing-Rate", "units": "percent", "behavior": "manual", "behavior_params": '{"value":0}'},
            {"object_type": "analog-input", "object_instance": 6, "name": "HW-Supply-Temp", "units": "degrees-celsius", "behavior": "noise", "behavior_params": '{"base":71,"noise":0.8}', "point_type": "Leaving_Hot_Water_Temperature_Sensor"},
            {"object_type": "analog-input", "object_instance": 7, "name": "HW-Return-Temp", "units": "degrees-celsius", "behavior": "noise", "behavior_params": '{"base":58.5,"noise":0.8}', "point_type": "Entering_Hot_Water_Temperature_Sensor"},
            {"object_type": "analog-input", "object_instance": 8, "name": "HW-Diff-Pressure", "units": "pascals", "behavior": "noise", "behavior_params": '{"base":180,"noise":6}', "point_type": "Water_Differential_Pressure_Sensor"},
            {"object_type": "analog-input", "object_instance": 9, "name": "Gas-Flow", "units": "cubic-feet-per-minute", "behavior": "random_walk", "behavior_params": '{"value":44,"step":3,"min":10,"max":85}'},
            {"object_type": "binary-input", "object_instance": 10, "name": "HW-Pump-1-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Run_Status"},
            {"object_type": "binary-input", "object_instance": 11, "name": "HW-Pump-2-Run", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":false}', "point_type": "Run_Status"},
        ],
    },
    {
        "key": "bms",
        "label": "BMS / Supervisor",
        "description": "Building occupancy, alarms, energy, outside air conditions",
        "equipment_types": None,
        "objects": [
            {"object_type": "binary-value", "object_instance": 1, "name": "Building-Occupied", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Occupancy_Status"},
            {"object_type": "analog-value", "object_instance": 2, "name": "Active-Alarms", "units": "no-units", "behavior": "random_walk", "behavior_params": '{"value":2,"step":1,"min":0,"max":8}', "point_type": "Alarm"},
            {"object_type": "analog-input", "object_instance": 3, "name": "Energy-Today-kWh", "units": "kilowatt-hours", "behavior": "random_walk", "behavior_params": '{"value":430,"step":12,"min":0,"max":2000}', "point_type": "Energy_Sensor"},
            {"object_type": "analog-input", "object_instance": 4, "name": "Peak-Demand-kW", "units": "kilowatts", "behavior": "random_walk", "behavior_params": '{"value":182,"step":4,"min":50,"max":320}', "point_type": "Peak_Demand_Sensor"},
            {"object_type": "analog-input", "object_instance": 5, "name": "Outside-Air-Temp", "units": "degrees-celsius", "behavior": "sine", "behavior_params": '{"base":12,"amplitude":8,"period_hours":24}', "point_type": "Outside_Air_Temperature_Sensor"},
            {"object_type": "analog-input", "object_instance": 6, "name": "Outside-Air-Humidity", "units": "percent", "behavior": "sine", "behavior_params": '{"base":55,"amplitude":15,"period_hours":24}', "point_type": "Outside_Air_Humidity_Sensor"},
        ],
    },
    {
        "key": "meter",
        "label": "Electric Meter",
        "description": "Active power, energy, voltage L1/L2, current, power factor",
        "equipment_types": ["Meter"],
        "objects": [
            {"object_type": "analog-input", "object_instance": 1, "name": "Active-Power-kW", "units": "kilowatts", "behavior": "noise", "behavior_params": '{"base":45,"noise":3}', "point_type": "Power_Sensor"},
            {"object_type": "analog-input", "object_instance": 2, "name": "Energy-kWh", "units": "kilowatt-hours", "behavior": "random_walk", "behavior_params": '{"value":1000,"step":0.05,"min":0,"max":999999}', "point_type": "Energy_Sensor"},
            {"object_type": "analog-input", "object_instance": 3, "name": "Voltage-L1", "units": "volts", "behavior": "noise", "behavior_params": '{"base":230,"noise":2}'},
            {"object_type": "analog-input", "object_instance": 4, "name": "Voltage-L2", "units": "volts", "behavior": "noise", "behavior_params": '{"base":230,"noise":2}'},
            {"object_type": "analog-input", "object_instance": 5, "name": "Current-L1", "units": "amperes", "behavior": "noise", "behavior_params": '{"base":65,"noise":4}'},
            {"object_type": "analog-input", "object_instance": 6, "name": "Power-Factor", "units": "no-units", "behavior": "noise", "behavior_params": '{"base":0.92,"noise":0.03}'},
        ],
    },
    {
        "key": "lighting",
        "label": "Lighting Controller",
        "description": "3-zone dimming levels, overrides, occupancy, setpoints",
        "equipment_types": ["Lighting_Equipment"],
        "objects": [
            {"object_type": "analog-output", "object_instance": 1, "name": "Zone-1-Level", "units": "percent", "behavior": "manual", "behavior_params": '{"value":100}', "point_type": "Lighting_Level_Command"},
            {"object_type": "analog-output", "object_instance": 2, "name": "Zone-2-Level", "units": "percent", "behavior": "manual", "behavior_params": '{"value":80}', "point_type": "Lighting_Level_Command"},
            {"object_type": "analog-output", "object_instance": 3, "name": "Zone-3-Level", "units": "percent", "behavior": "manual", "behavior_params": '{"value":60}', "point_type": "Lighting_Level_Command"},
            {"object_type": "binary-output", "object_instance": 4, "name": "Zone-1-Override", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":false}', "point_type": "On_Off_Command"},
            {"object_type": "binary-output", "object_instance": 5, "name": "Zone-2-Override", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":false}', "point_type": "On_Off_Command"},
            {"object_type": "binary-value", "object_instance": 6, "name": "Occupancy-Status", "units": "no-units", "behavior": "manual", "behavior_params": '{"value":true}', "point_type": "Occupancy_Status"},
            {"object_type": "analog-value", "object_instance": 7, "name": "Occupancy-Setpoint", "units": "percent", "behavior": "constant", "behavior_params": '{"value":100}'},
            {"object_type": "analog-value", "object_instance": 8, "name": "Standby-Setpoint", "units": "percent", "behavior": "constant", "behavior_params": '{"value":30}'},
        ],
    },
]


def _migration_022_templates(conn: sqlite3.Connection) -> None:
    """Moves the object-template feature (previously 8 hardcoded built-ins +
    localStorage user templates, entirely client-only -- see
    admin/src/templates.ts before this migration) into the database, so a
    saved template is no longer invisible to other browsers/machines and
    survives clearing site data. Built-ins become real rows here
    (is_builtin=1), matched/skipped by `key` so re-running this migration
    never duplicates them -- deleting one is refused at the API layer
    (src/api/routers/templates.py), never enforced here."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            objects_json TEXT NOT NULL,
            equipment_types_json TEXT,
            is_builtin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    for tpl in _BUILTIN_TEMPLATES:
        conn.execute(
            "INSERT OR IGNORE INTO templates (key, label, description, objects_json, equipment_types_json, is_builtin) "
            "VALUES (?,?,?,?,?,1)",
            (
                tpl["key"],
                tpl["label"],
                tpl["description"],
                json.dumps(tpl["objects"]),
                json.dumps(tpl["equipment_types"]) if tpl["equipment_types"] is not None else None,
            ),
        )


def _migration_023_replay_recordings_category(conn: sqlite3.Connection) -> None:
    """Free-text label a recording can be tagged with (e.g. "Normal",
    "Faulty") -- never read by the BACnet protocol/simulation engine or by
    replay playback. Purely a training-time signal: when several tagged
    recordings are combined into a training dataset
    (src/simulation/training_export.py), each distinct category becomes a
    numeric input_category feature column, letting a trained model
    condition on which regime a row came from -- without touching any
    model's own FMU inputs (see the plan this migration accompanies for
    why that's the point: model-agnostic, works for every model, no
    Modelica/FMU change needed for a new category)."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(replay_recordings)")}
    if "category" not in existing_cols:
        conn.execute("ALTER TABLE replay_recordings ADD COLUMN category TEXT")


def _migration_024_replay_recording_input_constants(conn: sqlite3.Connection) -> None:
    """Snapshots an FMU-backed model's constant-sourced input values (device
    config: simulation_model_configs.parameters.input_sources=='constant' +
    input_defaults) at the moment a recording starts -- NOT a BACnet point
    mapping (deliberately a separate table from replay_recording_points, no
    source_object_id, no fake object): a constant input never had a live
    point to begin with, that's the whole meaning of "constant" sourcing, so
    there's nothing to snapshot as a point identity, only the literal value
    the simulation actually used.

    Exists so training-data export (training_export.py) can pull each
    constant input's value from what a recording actually captured at record
    time, not from the device's CURRENT config -- the device's constants can
    change between two recordings (or between a recording and whenever
    training later runs), and using today's value for an old recording would
    silently mislabel what that recording's rows actually represent. A
    recording made before this migration existed simply has no rows here --
    training_export.py must treat that as "this recording cannot supply this
    variable", the same as a recording that never captured a needed point,
    not silently fall back to the live device config."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS replay_recording_input_constants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL REFERENCES replay_recordings(id) ON DELETE CASCADE,
            variable TEXT NOT NULL,
            value REAL NOT NULL,
            UNIQUE(recording_id, variable)
        );
        CREATE INDEX IF NOT EXISTS idx_replay_recording_input_constants_recording
            ON replay_recording_input_constants(recording_id);
        """
    )


def _migration_025_replay_recordings_condition(conn: sqlite3.Connection) -> None:
    """Lightweight, structured ground-truth classification for a recording
    -- 'normal'/'degraded'/'fault' -- separate from the free-text `category`
    column (a scenario/tag label, e.g. "hot-day", unchanged by this
    migration). Nullable, no default: every existing row reads back
    condition=NULL, treated everywhere as "unclassified/legacy", never
    backfilled or guessed. No CHECK constraint here -- same as `category`,
    enum enforcement lives at the API boundary (ReplayRecordingCreate/
    Update's own Literal type), not the DB.

    Deliberately a flat scalar column, not a blob -- this is what a later
    fault-injection/simulation feature can build on without any migration
    risk to existing data: attaching richer ground truth (fault_type/
    target/parameters) later is just another additive ALTER TABLE ADD
    COLUMN following this exact same pattern, whenever that feature
    actually exists. Nothing here anticipates its shape."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(replay_recordings)")}
    if "condition" not in existing_cols:
        conn.execute("ALTER TABLE replay_recordings ADD COLUMN condition TEXT")


def _migration_026_replay_recording_tags(conn: sqlite3.Connection) -> None:
    """Replaces the single free-text `category` column with a real list --
    multiple independent scenario/tag labels per recording (e.g. "hot-day",
    "fan-test"), not one categorical value. `category` itself is left in
    place but frozen: no code writes to it after this migration, nothing
    reads it any more (both consumers -- database.py and
    training_export.py -- move to this table). Not dropped: ALTER TABLE
    DROP COLUMN support varies by SQLite version and no migration here has
    ever dropped a column; an unused column costs nothing left in place.

    The backfill below is one-time and idempotent (INSERT OR IGNORE, plus
    the UNIQUE(recording_id, tag) constraint): every existing recording's
    single category string becomes a one-element tag list, so no
    classification data is lost. A recording with no category gets zero
    tag rows, same as before.

    Deliberately metadata-only going forward -- see the plan this migration
    accompanies for why the old category -> input_category training-
    feature injection is retired outright here, not adapted to a
    multi-tag-aware version: a training feature is a separate, later
    decision, not implied by giving recordings real tags."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS replay_recording_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL REFERENCES replay_recordings(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            UNIQUE(recording_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_replay_recording_tags_recording
            ON replay_recording_tags(recording_id);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO replay_recording_tags (recording_id, tag) "
        "SELECT id, category FROM replay_recordings WHERE category IS NOT NULL AND category != ''"
    )


def _migration_027_objects_source_object_id(conn: sqlite3.Connection) -> None:
    """Per-object live-value linking -- an object with source_object_id set
    is "linked": its Present_Value is periodically synced from the
    referenced object (see mirror_sync_loop's extension in
    simulation/runtime.py) instead of being driven by its own behavior,
    independent of what simulation_mode its OWN device has. This is the
    per-object sibling of devices.source_device_id (migration 14)'s
    whole-device Mirror mechanism -- same ALTER TABLE shape, same
    plain-FK-no-ON-DELETE-clause choice: deleting a still-linked-to source
    object is rejected at the API layer (see get_link_consumers, mirroring
    get_aggregate_membership_owner's existing pattern) rather than being
    silently unlinked, so FK enforcement (PRAGMA foreign_keys=ON) backs
    that guard up as a safety net exactly the way it already does for
    aggregate-mapping membership.

    NULL (the default, every pre-existing row) means "an ordinary, locally
    -driven object" -- unchanged behavior, no backfill needed."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(objects)")}
    if "source_object_id" not in existing_cols:
        conn.execute(
            "ALTER TABLE objects ADD COLUMN source_object_id INTEGER REFERENCES objects(id)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_objects_source_object_id ON objects(source_object_id)"
    )


def _migration_028_dashboards(conn: sqlite3.Connection) -> None:
    """Evolves the existing Custom Graphs feature (`custom_graphs`) into a
    dashboard/widget system, in place -- deliberately NOT a second, parallel
    table: a saved graph already *is* a widget, it just didn't have a
    dashboard to belong to or a grid position yet. Adds a small `dashboards`
    table (just a name -- no content of its own) and four/five new columns
    on `custom_graphs`: `dashboard_id` (which dashboard it's on),
    `widget_type` ('chart', unchanged rendering, or 'kpi', a new
    single-value tile whose `definition_json` holds
    {device_id, object_id, decimals?} instead of a series list), and
    `grid_x/grid_y/grid_w/grid_h` (position/size on that dashboard's grid).

    `dashboard_id` is intentionally nullable at the SQL level (no NOT NULL,
    even though every row conceptually belongs to exactly one dashboard) --
    same "enum/requiredness enforced at the API boundary, not the DB" choice
    already used for `replay_recordings.condition` (migration 25) and
    `custom_graphs`'s own definition shape: a NOT NULL column referencing a
    table that doesn't have any rows yet at ALTER TABLE time can't be added
    with a real default (SQLite requires a constant default for a NOT NULL
    ADD COLUMN, and there's no dashboard id to point at until the backfill
    below runs). `Database.create_custom_graph`/the API layer are what
    actually guarantee every row has a dashboard going forward.

    Backfill (idempotent: only runs when there's at least one custom_graphs
    row with no dashboard_id yet): creates one "My Dashboard" row and
    attaches every such pre-existing graph to it, stacked in a single
    column in their original order (grid_y = index * grid_h) -- so nothing
    a user already saved disappears or duplicates, it just gains a position
    on the new grid."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS dashboards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )

    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(custom_graphs)")}
    if "dashboard_id" not in existing_cols:
        conn.execute(
            "ALTER TABLE custom_graphs ADD COLUMN dashboard_id INTEGER REFERENCES dashboards(id) ON DELETE CASCADE"
        )
    if "widget_type" not in existing_cols:
        conn.execute("ALTER TABLE custom_graphs ADD COLUMN widget_type TEXT NOT NULL DEFAULT 'chart'")
    if "grid_x" not in existing_cols:
        conn.execute("ALTER TABLE custom_graphs ADD COLUMN grid_x INTEGER NOT NULL DEFAULT 0")
    if "grid_y" not in existing_cols:
        conn.execute("ALTER TABLE custom_graphs ADD COLUMN grid_y INTEGER NOT NULL DEFAULT 0")
    if "grid_w" not in existing_cols:
        conn.execute("ALTER TABLE custom_graphs ADD COLUMN grid_w INTEGER NOT NULL DEFAULT 6")
    if "grid_h" not in existing_cols:
        conn.execute("ALTER TABLE custom_graphs ADD COLUMN grid_h INTEGER NOT NULL DEFAULT 4")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_custom_graphs_dashboard_id ON custom_graphs(dashboard_id)")

    orphans = [
        row[0] for row in conn.execute(
            "SELECT id FROM custom_graphs WHERE dashboard_id IS NULL ORDER BY id"
        )
    ]
    if orphans:
        cur = conn.execute("INSERT INTO dashboards (name) VALUES ('My Dashboard')")
        dashboard_id = cur.lastrowid
        for index, graph_id in enumerate(orphans):
            conn.execute(
                "UPDATE custom_graphs SET dashboard_id=?, grid_x=0, grid_y=? WHERE id=?",
                (dashboard_id, index * 4, graph_id),
            )


def _migration_029_recordings_rename(conn: sqlite3.Connection) -> None:
    """Renames the "replay_recordings" family of tables (and the
    devices.replay_recording_id FK column) to just "recording(s)" --
    "replay" was legacy naming: this feature now serves Training,
    Calibration, and general trend inspection at least as much as it
    serves the actual Replay-mode device playback it was originally built
    for. "replay" stays only on the things genuinely about playing a
    recording back to a device (devices.simulation_mode='replay',
    replay_playback_loop, engine.py's replay_play/pause/seek/... section,
    src/api/routers/replay_playback.py) -- this migration touches none of
    those.

    SQLite's ALTER TABLE ... RENAME TO / RENAME COLUMN are both online,
    in-place, and foreign-key-safe: SQLite automatically rewrites the
    REFERENCES clauses stored in every dependent table's own SQL text when
    the table/column they point at is renamed (recording_points/
    recording_samples/recording_tags/recording_input_constants all
    reference recordings(id); devices.active_recording_id references
    recordings(id) too) -- unlike migration 8's device rebuild, nothing
    structural is changing here, so the far simpler rename-in-place form
    applies, not a create-new/copy/drop-old rebuild.

    Indexes aren't touched by a table RENAME TO the way REFERENCES clauses
    are, so they're recreated under the new names below -- purely cosmetic
    (nothing in the codebase looks an index up by name), done anyway for
    consistency. Every statement here is independently idempotent (IF
    EXISTS/IF NOT EXISTS, or an existing-columns check), so re-running this
    against an already-renamed database is a safe no-op."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    if "replay_recordings" in tables and "recordings" not in tables:
        conn.execute("ALTER TABLE replay_recordings RENAME TO recordings")
    if "replay_recording_points" in tables and "recording_points" not in tables:
        conn.execute("ALTER TABLE replay_recording_points RENAME TO recording_points")
    if "replay_samples" in tables and "recording_samples" not in tables:
        conn.execute("ALTER TABLE replay_samples RENAME TO recording_samples")
    if "replay_recording_tags" in tables and "recording_tags" not in tables:
        conn.execute("ALTER TABLE replay_recording_tags RENAME TO recording_tags")
    if "replay_recording_input_constants" in tables and "recording_input_constants" not in tables:
        conn.execute("ALTER TABLE replay_recording_input_constants RENAME TO recording_input_constants")

    device_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "replay_recording_id" in device_cols and "active_recording_id" not in device_cols:
        conn.execute("ALTER TABLE devices RENAME COLUMN replay_recording_id TO active_recording_id")

    # Recreate indexes under the new names -- must run after the table
    # renames above, since CREATE INDEX ... ON recordings(...) needs that
    # table to already exist under its new name.
    conn.execute("DROP INDEX IF EXISTS idx_replay_recordings_device")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recordings_device ON recordings(source_device_id)")
    conn.execute("DROP INDEX IF EXISTS idx_replay_recording_points_recording")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recording_points_recording ON recording_points(recording_id)")
    conn.execute("DROP INDEX IF EXISTS idx_replay_samples_recording_index")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recording_samples_recording_index "
        "ON recording_samples(recording_id, sample_index)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_replay_samples_recording_ts")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recording_samples_recording_ts "
        "ON recording_samples(recording_id, timestamp)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_replay_samples_point_index")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recording_samples_point_index "
        "ON recording_samples(recording_point_id, sample_index)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_replay_recording_input_constants_recording")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recording_input_constants_recording "
        "ON recording_input_constants(recording_id)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_replay_recording_tags_recording")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recording_tags_recording ON recording_tags(recording_id)")


def _migration_030_recordings_fault_type(conn: sqlite3.Connection) -> None:
    """Second axis of a recording's ground-truth classification, alongside
    `condition` (severity: normal/degraded/fault, migration 025) -- what
    KIND of problem, not just how bad. Real-world HVAC FDD practice
    (ASHRAE RP-1312 and labeled fault datasets) splits classification
    exactly this way: severity and fault type as two independent
    dimensions, not one longer enum. Nullable, no CHECK constraint, same
    "enforced at the API layer only" choice `condition` already made --
    and exactly the additive column migration 025's own docstring
    anticipated ("a future fault-injection feature would add its own
    additive column(s)... not extend [condition]").

    Deliberately NOT free text -- a classifier needs a small, known, closed
    set of classes to predict from; open text can't work as a
    classification target (see this feature's own plan for the reasoning).
    Independent of `condition` -- no cross-field validation here, editable
    after the fact, same flexibility condition/tags already have. The UI
    only shows/edits this when condition isn't 'normal' (nothing to
    classify for a normal recording), but nothing stops it being set
    regardless at the DB/API layer -- same "labels are just labels, safe to
    correct" spirit as condition and tags."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
    if "fault_type" not in existing_cols:
        conn.execute("ALTER TABLE recordings ADD COLUMN fault_type TEXT")


def _migration_031_objects_state_text(conn: sqlite3.Connection) -> None:
    """Optional BACnet State_Text list for a multi-state object, stored as a
    JSON-encoded array (one entry per state, index 0 = state 1). Nullable --
    every multi-state object before this migration keeps working exactly as
    before (a real client just sees the raw 1-based integer with no state
    text). Primary producer is the classifier attach flow: a promoted
    classifier model's `class_labels` (see iot-models' ModelMetadata) size
    and label the multi-state point auto-created/wired to it, so a value of
    2 reads as "sensor_bias" rather than a bare integer a human has to
    cross-reference against the model's own documentation."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(objects)")}
    if "state_text" not in existing_cols:
        conn.execute("ALTER TABLE objects ADD COLUMN state_text TEXT")


def _migration_032_devices_protocol(conn: sqlite3.Connection) -> None:
    """BACnet was the only protocol until now; `protocol` lets SimEngine dispatch each device to
    the right ProtocolAdapter (see src/simulation/protocols/) instead of assuming BACnet
    unconditionally. Every existing row defaults to 'bacnet' -- no behavior change for any
    project that predates this column."""
    existing_dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "protocol" not in existing_dev_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN protocol TEXT NOT NULL DEFAULT 'bacnet'")


def _migration_033_calibration_runs(conn: sqlite3.Connection) -> None:
    """Makes the BACnet Simulator the durable system-of-record for FMU calibration
    *workflow* history -- the piece the flow had no home for. Models Runtime still owns
    the compute record (uploaded datasets, HEBO run, validation replay, logs, raw result
    artifacts, transient job state) in its own file store; this table is the reconstruct/
    audit summary, keyed to a Models-Runtime `runtime_job_id`, that answers "which exact
    FMU + model.json + recording + point mapping produced this, was it validated, was it
    applied, and to which simulation model".

    Design choices, consistent with the rest of this file:

    * All foreign ids are plain INTEGER -- no SQL `REFERENCES`. `simulation_model_configs`
      is created outside the migration system (store.py::ensure_simulation_model_schema),
      and this codebase already enforces enum/requiredness/referential rules at the API
      layer rather than the DB (migrations 25/28/30). `ON DELETE` behaviour would only
      matter for a hard FK anyway.
    * JSON blobs as TEXT NOT NULL DEFAULT '{}' / '[]', decoded/encoded by
      src/db/database.py's calibration_run helpers.
    * `schema_version` (=1) so a later shape change to this row is explicit rather than
      guessed from which columns happen to be populated.
    * `validation_status` is a lifecycle INDEPENDENT of `status`: a calibration can be
      COMPLETED while its held-out validation is `failed` or was never requested. Only
      `completed` here means "the held-out replay actually ran and produced metrics" --
      never merely "a validation dataset was uploaded".
    * Partial unique index on `runtime_job_id`: one row per Models-Runtime compute job,
      but the row exists (job id still NULL) before that job is created, so NULLs are
      exempt.

    Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS throughout, and the column
    top-up loop re-checks PRAGMA table_info so a re-run against a partially-created table
    is a no-op."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS calibration_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schema_version INTEGER NOT NULL DEFAULT 1,

            runtime_job_id TEXT,
            runtime_dataset_id TEXT,
            runtime_validation_dataset_id TEXT,

            model_id TEXT NOT NULL,
            simulation_model_id INTEGER,

            recording_id INTEGER,
            validation_recording_id INTEGER,

            fmu_sha256 TEXT,
            metadata_sha256 TEXT,
            tuner_names TEXT NOT NULL DEFAULT '[]',

            status TEXT NOT NULL DEFAULT 'pending',
            failure_stage TEXT,
            error_summary TEXT,

            mapping_json TEXT NOT NULL DEFAULT '{}',

            objective_metric TEXT,
            baseline_metric REAL,
            calibration_metric REAL,
            improvement_pct REAL,

            validation_status TEXT NOT NULL DEFAULT 'not_requested',
            validation_cv_rmse REAL,
            validation_nmbe REAL,
            validation_rmse REAL,
            validation_mae REAL,
            validation_r2 REAL,

            g14_interval TEXT,
            g14_based_on TEXT,
            g14_applicable INTEGER,
            g14_pass INTEGER,

            best_parameters_json TEXT NOT NULL DEFAULT '{}',
            fmu_defaults_json TEXT NOT NULL DEFAULT '{}',

            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            started_at TEXT,
            completed_at TEXT,
            applied_at TEXT,
            applied_to_simulation_model_id INTEGER
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_runs_runtime_job_unique
            ON calibration_runs(runtime_job_id) WHERE runtime_job_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_calibration_runs_model
            ON calibration_runs(model_id);
        CREATE INDEX IF NOT EXISTS idx_calibration_runs_sim_model
            ON calibration_runs(simulation_model_id);
        CREATE INDEX IF NOT EXISTS idx_calibration_runs_recording
            ON calibration_runs(recording_id);
        """
    )

    # Top-up for any database that somehow has an older partial calibration_runs table
    # (e.g. a dev build that pre-dates a column). Additive only.
    expected_columns = {
        "schema_version": "INTEGER NOT NULL DEFAULT 1",
        "runtime_job_id": "TEXT",
        "runtime_dataset_id": "TEXT",
        "runtime_validation_dataset_id": "TEXT",
        "simulation_model_id": "INTEGER",
        "recording_id": "INTEGER",
        "validation_recording_id": "INTEGER",
        "fmu_sha256": "TEXT",
        "metadata_sha256": "TEXT",
        "tuner_names": "TEXT NOT NULL DEFAULT '[]'",
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "failure_stage": "TEXT",
        "error_summary": "TEXT",
        "mapping_json": "TEXT NOT NULL DEFAULT '{}'",
        "objective_metric": "TEXT",
        "baseline_metric": "REAL",
        "calibration_metric": "REAL",
        "improvement_pct": "REAL",
        "validation_status": "TEXT NOT NULL DEFAULT 'not_requested'",
        "validation_cv_rmse": "REAL",
        "validation_nmbe": "REAL",
        "validation_rmse": "REAL",
        "validation_mae": "REAL",
        "validation_r2": "REAL",
        "g14_interval": "TEXT",
        "g14_based_on": "TEXT",
        "g14_applicable": "INTEGER",
        "g14_pass": "INTEGER",
        "best_parameters_json": "TEXT NOT NULL DEFAULT '{}'",
        "fmu_defaults_json": "TEXT NOT NULL DEFAULT '{}'",
        "started_at": "TEXT",
        "completed_at": "TEXT",
        "applied_at": "TEXT",
        "applied_to_simulation_model_id": "INTEGER",
    }
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(calibration_runs)")}
    for column, decl in expected_columns.items():
        if column not in existing_cols:
            conn.execute(f"ALTER TABLE calibration_runs ADD COLUMN {column} {decl}")


class Migration(NamedTuple):
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline", _migration_001_baseline),
    Migration(2, "objects_fault_columns", _migration_002_objects_fault_columns),
    Migration(3, "trend_logs_cov_increment", _migration_003_trend_logs_cov_increment),
    Migration(4, "devices_object_info_columns", _migration_004_devices_object_info_columns),
    Migration(5, "devices_location_id", _migration_005_devices_location_id),
    Migration(6, "devices_equipment_type", _migration_006_devices_equipment_type),
    Migration(7, "devices_can_receive_event_notifications", _migration_007_devices_can_receive_event_notifications),
    Migration(8, "devices_source_type_rebuild", _migration_008_devices_source_type_rebuild),
    Migration(9, "objects_point_type", _migration_009_objects_point_type),
    Migration(10, "objects_description", _migration_010_objects_description),
    Migration(11, "locations_kind", _migration_011_locations_kind),
    Migration(12, "locations_sort_order", _migration_012_locations_sort_order),
    Migration(13, "devices_simulation_mode", _migration_013_devices_simulation_mode),
    Migration(14, "devices_source_device_id", _migration_014_devices_source_device_id),
    Migration(15, "functional_test_runs_target_device_nullable", _migration_015_functional_test_runs_target_device_nullable),
    Migration(16, "semantic_entities_equipment_and_controller", _migration_016_semantic_entities_equipment_and_controller),
    Migration(17, "energy_model_configs_instance_key", _migration_017_energy_model_configs_instance_key),
    # Note: version 18 was previously "objects_value_modifier_enabled" in an
    # earlier branch of this codebase and has already been recorded as
    # applied in real deployments' schema_migrations tables -- reusing it
    # here made this migration silently no-op forever (run_migrations only
    # checks the version number against schema_migrations, not the name or
    # function body), so replay_recording_tables took the next free number
    # (19) instead. Never reuse a version number once any real database may
    # have recorded it applied, even if the original migration's function
    # no longer exists in the current codebase.
    Migration(19, "replay_recording_tables", _migration_019_replay_recording_tables),
    Migration(20, "equipment_manufacturer_model", _migration_020_equipment_manufacturer_model),
    Migration(21, "ai_suggestion_acceptances", _migration_021_ai_suggestion_acceptances),
    Migration(22, "templates", _migration_022_templates),
    Migration(23, "replay_recordings_category", _migration_023_replay_recordings_category),
    Migration(24, "replay_recording_input_constants", _migration_024_replay_recording_input_constants),
    Migration(25, "replay_recordings_condition", _migration_025_replay_recordings_condition),
    Migration(26, "replay_recording_tags", _migration_026_replay_recording_tags),
    Migration(27, "objects_source_object_id", _migration_027_objects_source_object_id),
    Migration(28, "dashboards", _migration_028_dashboards),
    Migration(29, "recordings_rename", _migration_029_recordings_rename),
    Migration(30, "recordings_fault_type", _migration_030_recordings_fault_type),
    Migration(31, "objects_state_text", _migration_031_objects_state_text),
    Migration(32, "devices_protocol", _migration_032_devices_protocol),
    Migration(33, "calibration_runs", _migration_033_calibration_runs),
]
