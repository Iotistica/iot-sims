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
]
