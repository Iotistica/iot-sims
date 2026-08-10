"""
BACnet/IP Simulator with REST + WebSocket management API.

Serves multiple virtual BACnet devices on UDP port 47808 and a management
API on HTTP port 47900. Device/object config is persisted in SQLite so it
survives container restarts and can be edited live via the Iotistica admin UI.
"""
import asyncio
import json
import math
import os
import random
import socket
import sqlite3
import time
import csv
import io
from abc import ABC, abstractmethod
from collections import deque, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

import bcrypt
import jwt
import psutil
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

# GH #15 refactor, pass 1 — constants/JWT-secret, Pydantic schemas, and the
# alarms/calendar/schedule/EDE helpers now live alongside this file in src/.
from .bacnet import alarms
from .bacnet import backup
from .bacnet import brick_export
from .bacnet import ede
from .bacnet import schedule as bacnet_schedule
from .bacnet import calendar as bacnet_calendar
from .core.config import (
    BACNET_PORT, BACNET_UNITS, BINARY_TYPES, BRICK_VERSION, COMMANDABLE_TYPES, DATA_DIR, DB_PATH,
    EQUIPMENT_TYPES, JWT_ALGORITHM, JWT_EXPIRE_HOURS, LOCATION_KINDS, MULTISTATE_TYPES,
    POINT_TYPES, SEMANTIC_PREDICATES, SIM_API_PORT, VALID_BEHAVIORS, VALID_OBJECT_TYPES,
    VALID_POLARITY, VALID_RELIABILITY, VALID_SEGMENTATION, _get_jwt_secret,
)
from .bacnet.schemas import (
    AckAlarmRequest, AlarmConfigSet, CalendarCreate, CalendarUpdate,
    Credentials, DeviceCreate, DeviceUpdate, EventEnrollmentCreate,
    EventEnrollmentUpdate, LocationCreate, LocationUpdate,
    NotificationClassCreate, NotificationClassUpdate,
    ObjectCreate, ObjectUpdate, PasswordReset, PriorityWrite, ProjectCreate,
    ProjectImport, ProjectUpdate, ScheduleCreate, ScheduleTargetSpec,
    ScheduleUpdate, SetValueRequest, SettingsPayload, TrendLogCreate,
    TrendLogUpdate,
)
from .semantics.backfill import (
    backfill_device_location_relationships,
    backfill_point_membership_relationships,
    backfill_semantic_entities,
    migrate_ahu_fan_aliases,
    upsert_semantic_entity,
    upsert_semantic_relationship,
)
from .semantics.keys import derive_semantic_key
from .semantics.mirror import sync_entity_from_flat_field, sync_flat_field_from_entity, sync_device_location_relationship
from .semantics.validation import validate_semantic_entity

from bacpypes3.local.device import DeviceObject
from bacpypes3.local.analog import AnalogInputObject, AnalogOutputObject, AnalogValueObject
from bacpypes3.local.binary import BinaryInputObject, BinaryOutputObject, BinaryValueObject
from bacpypes3.local.multistate import MultiStateInputObject, MultiStateOutputObject, MultiStateValueObject
from bacpypes3.local.object import Object as LocalObject
from bacpypes3.app import Application
from bacpypes3.primitivedata import Real, ObjectIdentifier, Unsigned, Boolean, Date, Time
from bacpypes3.constructeddata import SequenceOf, Any as BACnetAny
from bacpypes3.basetypes import (
    EngineeringUnits, BinaryPV, LoggingType, DeviceObjectPropertyReference,
    Reliability, LogRecord, LogRecordLogDatum, DateTime, StatusFlags,
    PriorityValue, Segmentation, Polarity,
)
from bacpypes3.local.cmd import Commandable
from bacpypes3.object import TrendLogObject as _TrendLogObjectSchema
from bacpypes3.debugging import bacpypes_debugging, ModuleLogger
from bacpypes3.errors import ExecutionError
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.apdu import (
    ReadPropertyACK,
    ReadRangeACK,
    SimpleAckPDU,
    ErrorPDU,
    RejectPDU,
    AbortPDU,
    ConfirmedEventNotificationRequest,
    UnconfirmedEventNotificationRequest,
)

from bacpypes3.ipv4 import IPv4DatagramServer

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bacnet-sim")

from .bacnet.packet_capture import CapturedPacket, PacketCapture

from .fault_detection import (FaultDetectionEngine,build_default_registry,)

from .energy import EnergyEngine
from .energy.registry import MODEL_TYPE_LABELS

from .api.routers.packet_capture import (
    router as packet_capture_router,
    resolve_packet_simulator_context,
)

from .api.routers.backups import (
    router as backups_router,
)

from .api.routers.locations import (
    router as locations_router,
)

from .api.routers.semantic import (
    router as semantic_router,
)

from .api.routers.calendars import (
    router as calendars_router,
)

from .api.routers.alarms import (
    router as alarms_router,
)

from .api.routers.trend_logs import (
    router as trend_logs_router,
)

from .api.routers.schedules import (
    router as schedules_router,
)


from .api.routers.devices import (
    router as devices_router,
)

from .api.routers.analytics import (
    router as analytics_router,
)

from .api.routers.auth import (
    router as auth_router,
)

from .api.routers.events import (
    router as events_router,
)

from .api.routers.exports import (
    router as exports_router,
)

from .api.routers.objects import (
    router as objects_router,
)

from .api.routers.projects import (
    router as projects_router,
)

from .api.routers.simulation import (
    router as simulation_router,
)

from .api.routers.websocket import (
    router as websocket_router,
)

from .api.routers.fault_detection import (
    router as fault_router,
)

from .api.routers.energy import router as energy_router

from .api.routers.discovery import (
    router as discovery_router,
)

from .api.routers.external_objects import (
    router as external_objects_router,
)

from .api.routers.semantic_suggestions import (
    router as semantic_suggestions_router,
)

from .api.routers.functional_tests import (
    router as functional_tests_router,
)

from .api.routers.functional_test_runs import (
    router as functional_test_runs_router,
)

from .api.guards import reject_external_device


_debug = 0
_log = ModuleLogger(globals())


# ─── Constants ────────────────────────────────────────────────────────────────
# Moved to bacnet_sim/config.py (GH #15 pass 1) — imported below.

# ─── Runtime settings ──────────────────────────────────────────────────────────
# Persisted key/value overrides for constants that used to be hardcoded. Values
# are stored as TEXT in the `settings` table; SETTINGS_SCHEMA casts them back.
SETTINGS_SCHEMA: dict[str, type] = {
    "tick_seconds": float,
    "device_log_maxlen": int,
    "global_log_maxlen": int,
    "metrics_errors_maxlen": int,
    "metrics_new_devices_maxlen": int,
    "metrics_duplicate_id_maxlen": int,
    "metrics_traffic_feed_maxlen": int,
    "object_history_maxlen": int,
    "trend_log_default_interval": int,
    "trend_log_default_buffer_size": int,
    "jwt_expire_hours": int,
}


def _default_settings() -> dict:
    return {
        "tick_seconds": 5.0,
        "device_log_maxlen": 300,
        "global_log_maxlen": 1000,
        "metrics_errors_maxlen": 200,
        "metrics_new_devices_maxlen": 200,
        "metrics_duplicate_id_maxlen": 100,
        "metrics_traffic_feed_maxlen": 500,
        "object_history_maxlen": 720,
        "trend_log_default_interval": 60,
        "trend_log_default_buffer_size": 1000,
        # Seeded from the env var so upgrading an existing deployment that
        # already sets JWT_EXPIRE_HOURS doesn't silently change on first read.
        "jwt_expire_hours": int(os.environ.get("JWT_EXPIRE_HOURS", "24")),
    }


# ─── Database ─────────────────────────────────────────────────────────────────

class Database:
    def __init__(self, path: Path):
        self.path = str(path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def setup(self) -> None:
        self.path_obj = Path(self.path)
        self.path_obj.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript("""
                
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

                CREATE TABLE IF NOT EXISTS functional_test_runs (
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

            """)
            # Additive migration: number_of_states was added to the objects
            # table after it first shipped — backfill it on existing DBs
            # instead of requiring a fresh one.
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(objects)")}
            if "number_of_states" not in existing_cols:
                conn.execute("ALTER TABLE objects ADD COLUMN number_of_states INTEGER NOT NULL DEFAULT 2")
            if "reliability" not in existing_cols:
                conn.execute("ALTER TABLE objects ADD COLUMN reliability TEXT NOT NULL DEFAULT 'no-fault-detected'")
            if "polarity" not in existing_cols:
                conn.execute("ALTER TABLE objects ADD COLUMN polarity TEXT NOT NULL DEFAULT 'normal'")
            # Additive migration: cov_increment was added to trend_logs after
            # it first shipped (Phase 1) — backfill for existing DBs too.
            existing_tl_cols = {row[1] for row in conn.execute("PRAGMA table_info(trend_logs)")}
            if "cov_increment" not in existing_tl_cols:
                conn.execute("ALTER TABLE trend_logs ADD COLUMN cov_increment REAL NOT NULL DEFAULT 1.0")
            # Additive migration: Device object info properties (GH #19) were
            # added after devices first shipped — backfill for existing DBs.
            existing_dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
            if "firmware_revision" not in existing_dev_cols:
                conn.execute("ALTER TABLE devices ADD COLUMN firmware_revision TEXT NOT NULL DEFAULT 'N/A'")
            if "protocol_revision" not in existing_dev_cols:
                conn.execute("ALTER TABLE devices ADD COLUMN protocol_revision INTEGER NOT NULL DEFAULT 22")
            if "max_apdu_length_accepted" not in existing_dev_cols:
                conn.execute("ALTER TABLE devices ADD COLUMN max_apdu_length_accepted INTEGER NOT NULL DEFAULT 1024")
            if "segmentation_supported" not in existing_dev_cols:
                conn.execute("ALTER TABLE devices ADD COLUMN segmentation_supported TEXT NOT NULL DEFAULT 'segmented-both'")
            # Additive migration: Location (organizational grouping, no BACnet
            # protocol meaning) was added after devices first shipped.
            if "location_id" not in existing_dev_cols:
                conn.execute("ALTER TABLE devices ADD COLUMN location_id INTEGER REFERENCES locations(id)")
            # Additive migration: Brick/Haystack-style semantic metadata
            # (optional layer) — never read by the BACnet protocol/simulation
            # engine, purely descriptive. See src/config.py's EQUIPMENT_TYPES/
            # POINT_TYPES/LOCATION_KINDS for the pinned vocabulary.
            if "equipment_type" not in existing_dev_cols:
                conn.execute("ALTER TABLE devices ADD COLUMN equipment_type TEXT")
            # Additive migration: explicit per-device override for whether a
            # device can receive BACnet Event Notifications. NULL (the
            # default) means "infer from equipment_type" — see
            # _effective_can_receive_events(). Only an explicit 0/1 overrides
            # that inference.
            if "can_receive_event_notifications" not in existing_dev_cols:
                conn.execute("ALTER TABLE devices ADD COLUMN can_receive_event_notifications INTEGER")
            # Schema migration: devices gains a source_type discriminator
            # (simulated vs. external-bacnet, see src/api/routers/discovery.py)
            # and a UNIQUE(device_instance) -> UNIQUE(device_instance,
            # source_type) constraint change, so a discovered external device
            # and a future simulated copy of it can coexist at the same BACnet
            # instance. SQLite can't ALTER a UNIQUE constraint in place, and
            # `devices` has ~10 FK-dependent child tables (objects,
            # energy_model_configs, semantic_entities, trend_logs,
            # bacnet_schedules, bacnet_calendars, fault_rule_configs,
            # fault_events, notification_classes, event_enrollments,
            # energy_history) with PRAGMA foreign_keys=ON (see _conn()) --
            # verified live that the usual RENAME-old/recreate/DROP-old
            # migration pattern (used elsewhere in this function for
            # energy_model_configs) is unsafe here: RENAME rewrites every
            # child table's stored REFERENCES text to point at the renamed-
            # away name, and DROP TABLE on a table with incoming FK
            # references fails outright once children hold rows. This
            # follows SQLite's own documented procedure for that case instead:
            # disable FK enforcement for the rebuild, rebuild under a new
            # name, DROP the ORIGINAL (never renamed, so no child SQL text
            # ever needs rewriting), rename the new table into place, verify
            # with foreign_key_check before committing, then re-enable
            # enforcement.
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
            if "point_type" not in existing_cols:
                conn.execute("ALTER TABLE objects ADD COLUMN point_type TEXT")
            # Additive migration: description, for external-BACnet discovered
            # points (see src/api/routers/external_objects.py) -- also usable
            # for simulated objects later, never interpreted by SimEngine.
            if "description" not in existing_cols:
                conn.execute("ALTER TABLE objects ADD COLUMN description TEXT")
            existing_loc_cols = {row[1] for row in conn.execute("PRAGMA table_info(locations)")}
            if "kind" not in existing_loc_cols:
                conn.execute("ALTER TABLE locations ADD COLUMN kind TEXT")

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

            backfill_semantic_entities(conn)
            # Must run AFTER backfill_semantic_entities() -- see
            # migrate_ahu_fan_aliases()'s own docstring for why (it needs
            # each AHU's own top-level equipment entity, which backfill is
            # what creates it from devices.equipment_type).
            migrate_ahu_fan_aliases(conn)
            # Also after backfill_semantic_entities() -- needs the point/
            # equipment entities that call just minted to link them.
            backfill_point_membership_relationships(conn)
            backfill_device_location_relationships(conn)
        log.info("Database ready at %s", self.path)

    def seed_default(self) -> None:
        """Populate with a 4-storey commercial office tower if DB is empty.

        Device layout
        -------------
        1000  BMS-Gateway        Honeywell WEBs-N4          (supervisory)
        1001  Chiller-Plant      Trane Tracer SC+            (basement – 2 chillers + CT)
        1002  HW-Plant           Honeywell Excel 500         (basement – 2 boilers)
        1003  AHU-1              Johnson Controls FEC26B     (floors 1-2)
        1004  AHU-2              Johnson Controls FEC26B     (floors 3-4)
        1005  Main-Meter         Honeywell WEBs-N4           (main electrical utility meter)
        1101  VAV-L1-01          Siemens RXB29.1             (floor 1 zone A – north)
        1102  VAV-L1-02          Siemens RXB29.1             (floor 1 zone B – south)
        1201  VAV-L2-01          Siemens RXB29.1             (floor 2 zone A – conference)
        1202  VAV-L2-02          Siemens RXB29.1             (floor 2 zone B – open plan)
        1301  VAV-L3-01          Siemens RXB29.1             (floor 3 zone A – exec suites)
        1302  VAV-L3-02          Siemens RXB29.1             (floor 3 zone B – server room)
        1401  VAV-L4-01          Siemens RXB29.1             (floor 4 zone A – open plan)
        1402  VAV-L4-02          Siemens RXB29.1             (floor 4 zone B – board room)
        1501  DALI-GW-L1         LOYTEC L-DALI/4             (floor 1 lighting – zones A/B)
        1502  DALI-GW-L2         LOYTEC L-DALI/4             (floor 2 lighting – zones A/B)
        1503  DALI-GW-L3         LOYTEC L-DALI/4             (floor 3 lighting – zones A/B)
        1504  DALI-GW-L4         LOYTEC L-DALI/4             (floor 4 lighting – zones A/B)

        DALI is its own low-level addressed bus for lamp ballasts/drivers
        (IEC 62386) — it isn't BACnet. What actually shows up on the BMS
        network is a DALI-to-BACnet gateway (LOYTEC L-DALI, WAGO, Helvar,
        Lutron Quantum, etc.) that bridges each DALI group/zone to a handful
        of BACnet points: dim level, on/off, scene, daylight sensor, and lamp/
        emergency-lighting fault status. Modeled here as one gateway per
        floor aggregating 2 zones, matching the VAV zone layout above.
        """
        HONEYWELL = "Honeywell International"
        JCI       = "Johnson Controls"
        SIEMENS   = "Siemens Building Technologies"
        TRANE     = "Trane Technologies"
        LOYTEC    = "LOYTEC electronics GmbH"

        with self._conn() as conn:
            if conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] > 0:
                return

            conn.executemany(
                "INSERT OR IGNORE INTO devices "
                "(device_instance, name, description, vendor_name, model_name, equipment_type) "
                "VALUES (?,?,?,?,?,?)",
                [
                    (1000, "BMS-Gateway",  "Building management system gateway – supervisory controller",  HONEYWELL, "WEBs-N4",   None),
                    (1001, "Chiller-Plant","Basement chiller plant – 2 × centrifugal chillers + cooling tower", TRANE, "Tracer SC+", "Chiller"),
                    (1002, "HW-Plant",     "Basement hot-water plant – 2 × condensing boilers",           HONEYWELL, "Excel 500", "Boiler"),
                    (1003, "AHU-1",        "Air handling unit 1 – serves floors 1 & 2",                   JCI,       "FEC26B",    "Air_Handling_Unit"),
                    (1004, "AHU-2",        "Air handling unit 2 – serves floors 3 & 4",                   JCI,       "FEC26B",    "Air_Handling_Unit"),
                    (1005, "Main-Meter",   "Main electrical utility meter",                                HONEYWELL, "WEBs-N4",   "Meter"),
                    (1101, "VAV-L1-01",    "Floor 1 VAV – Zone A north offices",                           SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1102, "VAV-L1-02",    "Floor 1 VAV – Zone B south offices",                           SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1201, "VAV-L2-01",    "Floor 2 VAV – Zone A conference rooms",                        SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1202, "VAV-L2-02",    "Floor 2 VAV – Zone B open-plan",                               SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1301, "VAV-L3-01",    "Floor 3 VAV – Zone A executive suites",                        SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1302, "VAV-L3-02",    "Floor 3 VAV – Zone B server room",                             SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1401, "VAV-L4-01",    "Floor 4 VAV – Zone A open-plan",                               SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1402, "VAV-L4-02",    "Floor 4 VAV – Zone B board room",                              SIEMENS,   "RXB29.1",   "Variable_Air_Volume_Box"),
                    (1501, "DALI-GW-L1",   "Floor 1 DALI-to-BACnet lighting gateway – zones A/B",         LOYTEC,    "L-DALI/4",  None),
                    (1502, "DALI-GW-L2",   "Floor 2 DALI-to-BACnet lighting gateway – zones A/B",         LOYTEC,    "L-DALI/4",  None),
                    (1503, "DALI-GW-L3",   "Floor 3 DALI-to-BACnet lighting gateway – zones A/B",         LOYTEC,    "L-DALI/4",  None),
                    (1504, "DALI-GW-L4",   "Floor 4 DALI-to-BACnet lighting gateway – zones A/B",         LOYTEC,    "L-DALI/4",  None),
                ],
            )

            def did(instance: int) -> int:
                return conn.execute(
                    "SELECT id FROM devices WHERE device_instance=?", (instance,)
                ).fetchone()[0]

            objects: list = []

            # ── BMS Gateway (1000) ─────────────────────────────────────────────
            bms = did(1000)
            objects += [
                (bms, "binary-value",  1, "Building-Occupied",   "no-units",        "manual",      '{"value":true}'),
                (bms, "analog-value",  2, "Active-Alarms",       "no-units",        "random_walk", '{"value":2,"step":1,"min":0,"max":8}'),
                (bms, "analog-input",  3, "Energy-Today-kWh",    "kilowatt-hours",  "random_walk", '{"value":430,"step":12,"min":0,"max":2000}', "Energy_Sensor"),
                (bms, "analog-input",  4, "Peak-Demand-kW",      "kilowatts",       "random_walk", '{"value":182,"step":4,"min":50,"max":320}', "Demand_Sensor"),
                (bms, "analog-input",  5, "Outside-Air-Temp",    "degrees-celsius", "sine",        '{"base":12.0,"amplitude":8.0,"period_hours":24}', "Outside_Air_Temperature_Sensor"),
                (bms, "analog-input",  6, "Outside-Air-Humidity","percent",         "sine",        '{"base":55.0,"amplitude":15.0,"period_hours":24}', "Outside_Air_Humidity_Sensor"),
            ]

            # ── Chiller Plant (1001) ───────────────────────────────────────────
            cp = did(1001)
            objects += [
                (cp, "binary-input",  1, "CH-1-Run",             "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (cp, "analog-input",  2, "CH-1-kW",              "kilowatts",       "random_walk", '{"value":212,"step":8,"min":80,"max":320}', "Power_Sensor"),
                (cp, "analog-input",  3, "CH-1-COP",             "no-units",        "noise",       '{"base":5.8,"noise":0.2}'),
                (cp, "binary-input",  4, "CH-2-Run",             "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (cp, "analog-input",  5, "CH-2-kW",              "kilowatts",       "random_walk", '{"value":198,"step":8,"min":80,"max":320}', "Power_Sensor"),
                (cp, "analog-input",  6, "CH-2-COP",             "no-units",        "noise",       '{"base":5.6,"noise":0.2}'),
                (cp, "analog-input",  7, "CW-Supply-Temp",       "degrees-celsius", "noise",       '{"base":6.5,"noise":0.2}', "Leaving_Chilled_Water_Temperature_Sensor"),
                (cp, "analog-input",  8, "CW-Return-Temp",       "degrees-celsius", "noise",       '{"base":12.2,"noise":0.2}', "Entering_Chilled_Water_Temperature_Sensor"),
                (cp, "analog-input",  9, "CW-Flow",              "liters-per-second","noise",      '{"base":48.0,"noise":1.5}', "Water_Flow_Sensor"),
                (cp, "analog-input", 10, "CW-Diff-Pressure",     "pascals",         "noise",       '{"base":225,"noise":8}', "Water_Differential_Pressure_Sensor"),
                (cp, "binary-input", 11, "CT-Fan-1-Run",         "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (cp, "binary-input", 12, "CT-Fan-2-Run",         "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (cp, "analog-input", 13, "CT-Leaving-Water-Temp","degrees-celsius", "noise",       '{"base":29.5,"noise":0.5}'),
                (cp, "analog-input", 14, "CT-Approach-Temp",     "degrees-celsius", "noise",       '{"base":3.2,"noise":0.3}'),
                (cp, "binary-input", 15, "CW-Pump-1-Run",        "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (cp, "binary-input", 16, "CW-Pump-2-Run",        "no-units",        "manual",      '{"value":false}', "Run_Status"),
            ]

            # ── Hot Water Plant (1002) ─────────────────────────────────────────
            hw = did(1002)
            objects += [
                (hw, "binary-input",  1, "BLR-1-Run",            "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (hw, "analog-input",  2, "BLR-1-Firing-Rate",    "percent",         "noise",       '{"base":62,"noise":5}'),
                (hw, "analog-input",  3, "BLR-1-Flue-Temp",      "degrees-celsius", "noise",       '{"base":88,"noise":3}'),
                (hw, "binary-input",  4, "BLR-2-Run",            "no-units",        "manual",      '{"value":false}', "Run_Status"),
                (hw, "analog-input",  5, "BLR-2-Firing-Rate",    "percent",         "manual",      '{"value":0}'),
                (hw, "analog-input",  6, "HW-Supply-Temp",       "degrees-celsius", "noise",       '{"base":71.0,"noise":0.8}', "Leaving_Hot_Water_Temperature_Sensor"),
                (hw, "analog-input",  7, "HW-Return-Temp",       "degrees-celsius", "noise",       '{"base":58.5,"noise":0.8}', "Entering_Hot_Water_Temperature_Sensor"),
                (hw, "analog-input",  8, "HW-Diff-Pressure",     "pascals",         "noise",       '{"base":180,"noise":6}', "Water_Differential_Pressure_Sensor"),
                (hw, "analog-input",  9, "Gas-Flow",             "cubic-feet-per-minute","random_walk",'{"value":44,"step":3,"min":10,"max":85}'),
                (hw, "binary-input", 10, "HW-Pump-1-Run",        "no-units",        "manual",      '{"value":true}', "Run_Status"),
                (hw, "binary-input", 11, "HW-Pump-2-Run",        "no-units",        "manual",      '{"value":false}', "Run_Status"),
            ]

            # ── Main Meter (1005) ──────────────────────────────────────────────
            meter = did(1005)
            objects += [
                (meter, "analog-input", 1, "Total-Energy-kWh", "kilowatt-hours", "random_walk", '{"value":48200,"step":25,"min":0,"max":500000}', "Energy_Sensor"),
                (meter, "analog-input", 2, "Total-Power-kW",   "kilowatts",      "random_walk", '{"value":210,"step":6,"min":50,"max":450}', "Power_Sensor"),
                (meter, "analog-input", 3, "Peak-Demand-kW",   "kilowatts",      "random_walk", '{"value":182,"step":4,"min":50,"max":320}', "Demand_Sensor"),
            ]

            # ── AHU-1  floors 1-2 (1003) ──────────────────────────────────────
            # SF-Run/RF-Run and SF-Speed/RF-Speed are tagged with the
            # canonical Fan_Status/Fan_Speed_Command point classes -- the
            # supply/return distinction comes from which Supply_Fan/
            # Return_Fan semantic equipment entity each isPointOf, set up
            # below, not from separate alias point-type names.
            ahu1 = did(1003)
            objects += [
                (ahu1, "binary-input",  1, "SF-Run",             "no-units",        "manual",      '{"value":true}', "Fan_Status"),
                (ahu1, "binary-input",  2, "RF-Run",             "no-units",        "manual",      '{"value":true}', "Fan_Status"),
                (ahu1, "analog-input",  3, "SF-Speed",           "percent",         "sine",        '{"base":75.0,"amplitude":15.0,"period_hours":12}', "Fan_Speed_Command"),
                (ahu1, "analog-input",  4, "RF-Speed",           "percent",         "sine",        '{"base":70.0,"amplitude":12.0,"period_hours":12}', "Fan_Speed_Command"),
                (ahu1, "analog-input",  5, "SAT",                "degrees-celsius", "noise",       '{"base":13.0,"noise":0.4}', "Supply_Air_Temperature_Sensor"),
                (ahu1, "analog-input",  6, "RAT",                "degrees-celsius", "sine",        '{"base":22.0,"amplitude":2.0,"period_hours":24}', "Return_Air_Temperature_Sensor"),
                (ahu1, "analog-input",  7, "MAT",                "degrees-celsius", "noise",       '{"base":16.0,"noise":0.8}', "Mixed_Air_Temperature_Sensor"),
                (ahu1, "analog-input",  8, "OAT",                "degrees-celsius", "sine",        '{"base":12.0,"amplitude":8.0,"period_hours":24}', "Outside_Air_Temperature_Sensor"),
                (ahu1, "analog-output", 9, "OAD-Position",       "percent",         "sine",        '{"base":28.0,"amplitude":18.0,"period_hours":24}', "Damper_Position_Command"),
                (ahu1, "analog-output",10, "CC-Valve",           "percent",         "sine",        '{"base":55.0,"amplitude":25.0,"period_hours":12}', "Valve_Position_Command"),
                (ahu1, "analog-output",11, "HC-Valve",           "percent",         "sine",        '{"base":10.0,"amplitude":9.0,"period_hours":24}', "Valve_Position_Command"),
                (ahu1, "analog-input", 12, "SA-Flow",            "cubic-feet-per-minute","noise",  '{"base":8500,"noise":250}', "Air_Flow_Sensor"),
                (ahu1, "analog-input", 13, "SA-Static-Pressure", "pascals",         "noise",       '{"base":375,"noise":12}', "Static_Pressure_Sensor"),
                (ahu1, "binary-input", 14, "Filter-DP-Alarm",    "no-units",        "manual",      '{"value":false}', "Change_Filter_Alarm"),
                (ahu1, "binary-input", 15, "Freeze-Stat",        "no-units",        "manual",      '{"value":false}', "Freeze_Status"),
            ]

            # ── AHU-2  floors 3-4 (1004) ──────────────────────────────────────
            ahu2 = did(1004)
            objects += [
                (ahu2, "binary-input",  1, "SF-Run",             "no-units",        "manual",      '{"value":true}', "Fan_Status"),
                (ahu2, "binary-input",  2, "RF-Run",             "no-units",        "manual",      '{"value":true}', "Fan_Status"),
                (ahu2, "analog-input",  3, "SF-Speed",           "percent",         "sine",        '{"base":70.0,"amplitude":18.0,"period_hours":12}', "Fan_Speed_Command"),
                (ahu2, "analog-input",  4, "RF-Speed",           "percent",         "sine",        '{"base":65.0,"amplitude":14.0,"period_hours":12}', "Fan_Speed_Command"),
                (ahu2, "analog-input",  5, "SAT",                "degrees-celsius", "noise",       '{"base":13.5,"noise":0.4}', "Supply_Air_Temperature_Sensor"),
                (ahu2, "analog-input",  6, "RAT",                "degrees-celsius", "sine",        '{"base":21.5,"amplitude":2.0,"period_hours":24}', "Return_Air_Temperature_Sensor"),
                (ahu2, "analog-input",  7, "MAT",                "degrees-celsius", "noise",       '{"base":15.5,"noise":0.8}', "Mixed_Air_Temperature_Sensor"),
                (ahu2, "analog-input",  8, "OAT",                "degrees-celsius", "sine",        '{"base":12.0,"amplitude":8.0,"period_hours":24}', "Outside_Air_Temperature_Sensor"),
                (ahu2, "analog-output", 9, "OAD-Position",       "percent",         "sine",        '{"base":25.0,"amplitude":16.0,"period_hours":24}', "Damper_Position_Command"),
                (ahu2, "analog-output",10, "CC-Valve",           "percent",         "sine",        '{"base":50.0,"amplitude":22.0,"period_hours":12}', "Valve_Position_Command"),
                (ahu2, "analog-output",11, "HC-Valve",           "percent",         "sine",        '{"base":12.0,"amplitude":9.0,"period_hours":24}', "Valve_Position_Command"),
                (ahu2, "analog-input", 12, "SA-Flow",            "cubic-feet-per-minute","noise",  '{"base":7800,"noise":220}', "Air_Flow_Sensor"),
                (ahu2, "analog-input", 13, "SA-Static-Pressure", "pascals",         "noise",       '{"base":360,"noise":12}', "Static_Pressure_Sensor"),
                (ahu2, "binary-input", 14, "Filter-DP-Alarm",    "no-units",        "manual",      '{"value":false}', "Change_Filter_Alarm"),
                (ahu2, "binary-input", 15, "Freeze-Stat",        "no-units",        "manual",      '{"value":false}', "Freeze_Status"),
            ]

            # ── VAV boxes ─────────────────────────────────────────────────────
            # (instance, zone_temp_base, cool_sp, heat_sp, damper_base, flow_base, reheat_base)
            vav_cfg = [
                (1101, 21.5, 23.0, 20.0, 68,  350, 12.0),   # L1 Zone A – north offices
                (1102, 22.0, 23.0, 20.5, 72,  320, 10.0),   # L1 Zone B – south offices
                (1201, 21.8, 23.0, 20.0, 65,  400, 14.0),   # L2 Zone A – conference rooms
                (1202, 22.2, 23.5, 20.5, 70,  370, 11.0),   # L2 Zone B – open plan
                (1301, 21.0, 22.5, 20.0, 60,  310, 16.0),   # L3 Zone A – exec suites
                (1302, 19.0, 20.5, 18.0, 90,  520, 5.0),    # L3 Zone B – server room (runs cold, high airflow)
                (1401, 22.0, 23.0, 20.5, 67,  360, 12.0),   # L4 Zone A – open plan
                (1402, 21.5, 23.0, 20.0, 63,  300, 18.0),   # L4 Zone B – board room
            ]
            for (inst, zt, csp, hsp, dmp, flow, rh) in vav_cfg:
                vd = did(inst)
                objects += [
                    (vd, "analog-input",  1, "Zone-Temp",         "degrees-celsius", "noise",  f'{{"base":{zt},"noise":0.3}}', "Zone_Air_Temperature_Sensor"),
                    (vd, "analog-value",  2, "Cooling-SP",        "degrees-celsius", "manual", f'{{"value":{csp}}}', "Cooling_Temperature_Setpoint"),
                    (vd, "analog-value",  3, "Heating-SP",        "degrees-celsius", "manual", f'{{"value":{hsp}}}', "Heating_Temperature_Setpoint"),
                    (vd, "analog-output", 4, "Damper-Cmd",        "percent",         "sine",   f'{{"base":{dmp},"amplitude":14.0,"period_hours":8}}', "Damper_Position_Command"),
                    (vd, "analog-input",  5, "Zone-Airflow",      "cubic-feet-per-minute","noise",f'{{"base":{flow},"noise":18}}', "Air_Flow_Sensor"),
                    (vd, "analog-output", 6, "Reheat-Valve",      "percent",         "sine",   f'{{"base":{rh},"amplitude":10.0,"period_hours":12}}', "Valve_Position_Command"),
                    (vd, "binary-input",  7, "Occupancy",         "no-units",        "manual", '{"value":true}', "Occupancy_Sensor"),
                    (vd, "analog-input",  8, "Zone-CO2",          "parts-per-million","random_walk",f'{{"value":650,"step":30,"min":400,"max":1200}}', "CO2_Level_Sensor"),
                ]

            # ── DALI-to-BACnet lighting gateways ─────────────────────────────
            # (instance, on_time, off_time) — occupied-hours schedule per floor.
            # Each gateway aggregates 2 zones (A/B), matching the VAV layout.
            dali_cfg = [
                (1501, "07:00", "19:00"),   # Floor 1
                (1502, "07:00", "19:00"),   # Floor 2
                (1503, "06:30", "20:00"),   # Floor 3 – executive suites, longer hours
                (1504, "07:00", "19:30"),   # Floor 4
            ]
            for (inst, on_time, off_time) in dali_cfg:
                gw = did(inst)
                objects += [
                    # Device-wide DALI Type-1 emergency lighting self-test status
                    (gw, "binary-value", 1, "Emergency-Test-OK", "no-units", "fault", json.dumps({
                        "base_behavior": "constant", "base_params": {"value": True},
                        "fault_type": "stuck", "fault_value": 0,
                        "mtbf_minutes": 10080, "fault_duration_seconds": 600,
                    })),
                ]
                next_inst = 2
                for zone in ("A", "B"):
                    objects += [
                        (gw, "binary-value", next_inst,     f"Zone-{zone}-Lights-On",  "no-units", "schedule", json.dumps({
                            "default": 0,
                            "blocks": [{"start": on_time, "value": 1}, {"start": off_time, "value": 0}],
                        }), "On_Off_Command"),
                        (gw, "analog-value", next_inst + 1, f"Zone-{zone}-Dim-Level",  "percent",  "sine",     json.dumps({
                            "base": 75.0, "amplitude": 15.0, "period_hours": 24,
                        }), "Lighting_Level_Command"),
                        (gw, "analog-value", next_inst + 2, f"Zone-{zone}-Scene",      "no-units", "manual",   json.dumps({
                            "value": 2,   # 1=Off 2=Occupied 3=Evening 4=Cleaning 5=Emergency
                        })),
                        (gw, "analog-input", next_inst + 3, f"Zone-{zone}-Daylight",   "luxes",    "sine",     json.dumps({
                            "base": 250.0, "amplitude": 240.0, "period_hours": 24,
                        })),
                        (gw, "binary-input", next_inst + 4, f"Zone-{zone}-Lamp-Fault", "no-units", "fault",    json.dumps({
                            "base_behavior": "constant", "base_params": {"value": False},
                            "fault_type": "stuck", "fault_value": 1,
                            "mtbf_minutes": 4320, "fault_duration_seconds": 1800,
                        })),
                        (gw, "analog-input", next_inst + 5, f"Zone-{zone}-Power",      "kilowatts","random_walk", json.dumps({
                            "value": 2.4, "step": 0.15, "min": 0.2, "max": 4.5,
                        }), "Power_Sensor"),
                    ]
                    next_inst += 6

            # Most tuples above are the original 7-element shape (no point_type
            # tag); only the curated subset tagged with an 8th element carries
            # one. Normalize before the bulk insert so both shapes work.
            objects = [(t + (None,)) if len(t) == 7 else t for t in objects]
            conn.executemany(
                "INSERT OR IGNORE INTO objects "
                "(device_id, object_type, object_instance, name, units, behavior, behavior_params, point_type) "
                "VALUES (?,?,?,?,?,?,?,?)",
                objects,
            )

            # Seed the default energy model for the Chiller-Plant device.
            #
            # Use the database ID resolved from device instance 1001 rather
            # than a hard-coded row ID, because SQLite row IDs can change
            # when the database is recreated or devices are imported.
            conn.execute(
                """
                INSERT INTO energy_model_configs (
                    device_id,
                    model_type,
                    enabled,
                    parameters
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(device_id, model_type, instance_key)
                DO UPDATE SET
                    enabled = excluded.enabled,
                    parameters = excluded.parameters
                """,
                (
                    cp,
                    "chiller",
                    1,
                    json.dumps(
                        {
                            "rated_capacity_kw": 3000.0,
                            "full_load_cop": 5.8,
                            "iplv_cop": 6.6,
                            "rated_electrical_power_kw": 520.0,
                        }
                    ),
                ),
            )

            # ── Brick Core semantic entities/relationships ──────────────────────
            # Demonstrates the canonical representation this migration exists
            # to enable: Supply_Fan/Return_Fan as their own sub-equipment
            # (isPartOf the AHU) each isPointOf-ing its own Fan_Status/
            # Fan_Speed_Command points, and Lighting_Zone as a location entity
            # directly hosting its own zone points -- instead of the
            # duplicate-Fan_Status collision the flat point_type tags above
            # still show (unavoidably, since two fans share one device) for
            # any caller that hasn't opted into semantic resolution.
            def oid(device_id: int, name: str) -> int:
                return conn.execute(
                    "SELECT id FROM objects WHERE device_id=? AND name=?",
                    (device_id, name),
                ).fetchone()[0]

            for ahu_device_id, ahu_name in ((ahu1, "AHU-1"), (ahu2, "AHU-2")):
                ahu_entity = upsert_semantic_entity(
                    conn, name=ahu_name, brick_class="Air_Handling_Unit",
                    entity_kind="equipment", device_id=ahu_device_id,
                )
                for brick_class, run_name, speed_name in (
                    ("Supply_Fan", "SF-Run", "SF-Speed"),
                    ("Return_Fan", "RF-Run", "RF-Speed"),
                ):
                    fan_entity = upsert_semantic_entity(
                        conn,
                        name=f"{ahu_name} {brick_class.replace('_', ' ')}",
                        brick_class=brick_class,
                        entity_kind="equipment",
                        device_id=ahu_device_id,
                        local_slug=brick_class.lower().replace("_", "-"),
                    )
                    upsert_semantic_relationship(conn, fan_entity["id"], "isPartOf", ahu_entity["id"])

                    for point_name, point_class in (
                        (run_name, "Fan_Status"),
                        (speed_name, "Fan_Speed_Command"),
                    ):
                        point_entity = upsert_semantic_entity(
                            conn, name=point_name, brick_class=point_class,
                            entity_kind="point", object_id=oid(ahu_device_id, point_name),
                        )
                        upsert_semantic_relationship(conn, point_entity["id"], "isPointOf", fan_entity["id"])

            for (dali_instance, _on_time, _off_time) in dali_cfg:
                gw = did(dali_instance)
                for zone in ("A", "B"):
                    zone_entity = upsert_semantic_entity(
                        conn,
                        name=f"Lighting Zone {zone} (device {dali_instance})",
                        brick_class="Lighting_Zone",
                        entity_kind="location",
                        device_id=gw,
                        local_slug=f"zone-{zone.lower()}",
                    )
                    for point_name, point_class in (
                        (f"Zone-{zone}-Power", "Power_Sensor"),
                        (f"Zone-{zone}-Lights-On", "On_Off_Command"),
                        (f"Zone-{zone}-Dim-Level", "Lighting_Level_Command"),
                    ):
                        point_entity = upsert_semantic_entity(
                            conn, name=point_name, brick_class=point_class,
                            entity_kind="point", object_id=oid(gw, point_name),
                        )
                        upsert_semantic_relationship(conn, point_entity["id"], "isPointOf", zone_entity["id"])

            # setup() already ran backfill_semantic_entities() once before
            # seed_default() populated any rows (fresh-install ordering:
            # setup() -> seed_default()), so that first pass found nothing
            # to backfill. Run it again now that the seeded equipment_type/
            # point_type/kind tags actually exist, so first boot doesn't
            # need a process restart before semantic entities show up.
            # (Also picks up every device/object/location NOT given explicit
            # semantic entities above, e.g. Chiller-Plant/HW-Plant/VAVs.)
            backfill_semantic_entities(conn)
            backfill_point_membership_relationships(conn)
            backfill_device_location_relationships(conn)

            conn.commit()
        log.info("Seeded 4-storey office tower: Honeywell/Trane/JCI/Siemens/LOYTEC – 18 devices, %d objects", len(objects))

        
    # ── Locations ────────────────────────────────────────────────────────────
    # Pure organizational grouping for the admin UI/sidebar tree — no BACnet
    # protocol representation, unlike devices/objects. Devices reference a
    # location via the nullable devices.location_id FK; NULL = top level.

    def get_locations(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM locations ORDER BY name")]

    def get_location(self, location_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
            return dict(r) if r else None

    def create_location(self, name: str, parent_location_id: Optional[int], description: str, kind: Optional[str] = None) -> dict:
        # `kind` is presented in the UI as the location's Brick class --
        # Brick is the source of truth, `locations.kind` is a compatibility
        # mirror kept in lockstep automatically (see src/semantics/mirror.py).
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO locations (name, parent_location_id, description, kind) VALUES (?,?,?,?)",
                (name, parent_location_id, description, kind),
            )
            sync_entity_from_flat_field(
                conn, entity_kind="location", name=name, brick_class=kind, location_id=cur.lastrowid,
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM locations WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_location(self, location_id: int, name: str, parent_location_id: Optional[int], description: str, kind: Optional[str] = None) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE locations SET name=?, parent_location_id=?, description=?, kind=? WHERE id=?",
                (name, parent_location_id, description, kind, location_id),
            )
            sync_entity_from_flat_field(
                conn, entity_kind="location", name=name, brick_class=kind, location_id=location_id,
            )
            conn.commit()
            r = conn.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
            return dict(r) if r else None

    def delete_location(self, location_id: int) -> bool:
        """Refuses to delete a non-empty location (sub-locations, devices,
        or semantic entities still reference it) — returns False rather
        than silently cascading."""
        with self._conn() as conn:
            has_sublocations = conn.execute(
                "SELECT 1 FROM locations WHERE parent_location_id=?", (location_id,)
            ).fetchone()
            has_devices = conn.execute(
                "SELECT 1 FROM devices WHERE location_id=?", (location_id,)
            ).fetchone()
            has_semantic_entities = conn.execute(
                "SELECT 1 FROM semantic_entities WHERE location_id=?", (location_id,)
            ).fetchone()
            if has_sublocations or has_devices or has_semantic_entities:
                return False
            cur = conn.execute("DELETE FROM locations WHERE id=?", (location_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Semantic entities/relationships (Brick Core) ────────────────────────
    # Thin CRUD/traversal methods matching the shape of the locations
    # methods above — see src/semantics/ for the multi-hop graph-walking
    # logic (SemanticResolver) built on top of these.

    def get_semantic_entities(
        self,
        *,
        device_id: Optional[int] = None,
        object_id: Optional[int] = None,
        location_id: Optional[int] = None,
        entity_kind: Optional[str] = None,
        brick_class: Optional[str] = None,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        for column, value in (
            ("device_id", device_id),
            ("object_id", object_id),
            ("location_id", location_id),
            ("entity_kind", entity_kind),
            ("brick_class", brick_class),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM semantic_entities{where} ORDER BY id", params
                )
            ]

    def get_semantic_entity(self, entity_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM semantic_entities WHERE id=?", (entity_id,)
            ).fetchone()
            return dict(r) if r else None

    def create_semantic_entity(
        self,
        name: str,
        brick_class: str,
        entity_kind: str,
        *,
        device_id: Optional[int] = None,
        object_id: Optional[int] = None,
        location_id: Optional[int] = None,
        local_slug: Optional[str] = None,
    ) -> dict:
        validate_semantic_entity(
            entity_kind, brick_class,
            device_id=device_id, object_id=object_id, location_id=location_id,
        )
        semantic_key = derive_semantic_key(
            entity_kind, brick_class,
            device_id=device_id, object_id=object_id, location_id=location_id,
            local_slug=local_slug,
        )
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO semantic_entities "
                "(name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id),
            )
            # Direction 2 (Semantic Model panel -> flat field): point/location
            # entities are unambiguous (DB-enforced 1:1 with object_id/
            # location_id), so a point/location entity created here keeps
            # objects.point_type/locations.kind in lockstep too -- a user
            # who classifies a point through this panel instead of the
            # Object drawer still sees it reflected there. Deliberately not
            # done for entity_kind='equipment' -- see src/semantics/mirror.py.
            sync_flat_field_from_entity(
                conn, entity_kind=entity_kind, brick_class=brick_class,
                object_id=object_id, location_id=location_id,
            )
            conn.commit()
            return dict(
                conn.execute(
                    "SELECT * FROM semantic_entities WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def update_semantic_entity(
        self,
        entity_id: int,
        name: str,
        brick_class: str,
        entity_kind: str,
        *,
        device_id: Optional[int] = None,
        object_id: Optional[int] = None,
        location_id: Optional[int] = None,
        local_slug: Optional[str] = None,
    ) -> Optional[dict]:
        validate_semantic_entity(
            entity_kind, brick_class,
            device_id=device_id, object_id=object_id, location_id=location_id,
        )
        semantic_key = derive_semantic_key(
            entity_kind, brick_class,
            device_id=device_id, object_id=object_id, location_id=location_id,
            local_slug=local_slug,
        )
        with self._conn() as conn:
            old = conn.execute(
                "SELECT * FROM semantic_entities WHERE id=?", (entity_id,)
            ).fetchone()
            old = dict(old) if old else None

            conn.execute(
                "UPDATE semantic_entities SET name=?, local_slug=?, semantic_key=?, "
                "brick_class=?, entity_kind=?, device_id=?, object_id=?, location_id=? "
                "WHERE id=?",
                (name, local_slug, semantic_key, brick_class, entity_kind,
                 device_id, object_id, location_id, entity_id),
            )

            # Direction 2, same as create_semantic_entity() above -- plus,
            # if this entity was re-linked away from a different object/
            # location (unusual, but the API allows it), clear THAT row's
            # flat field first so it doesn't keep pointing at a class this
            # entity no longer represents.
            if old is not None:
                if (old["entity_kind"] == "point" and old.get("object_id") is not None
                        and old["object_id"] != object_id):
                    sync_flat_field_from_entity(
                        conn, entity_kind="point", brick_class=None, object_id=old["object_id"],
                    )
                if (old["entity_kind"] == "location" and old.get("location_id") is not None
                        and old["location_id"] != location_id):
                    sync_flat_field_from_entity(
                        conn, entity_kind="location", brick_class=None, location_id=old["location_id"],
                    )
            sync_flat_field_from_entity(
                conn, entity_kind=entity_kind, brick_class=brick_class,
                object_id=object_id, location_id=location_id,
            )

            conn.commit()
            r = conn.execute(
                "SELECT * FROM semantic_entities WHERE id=?", (entity_id,)
            ).fetchone()
            return dict(r) if r else None

    def delete_semantic_entity(self, entity_id: int) -> bool:
        """Cascades to semantic_relationships referencing this entity (via
        the ON DELETE CASCADE FK) — a relationship pointing at a deleted
        entity has no independent meaning to protect, unlike locations'
        refuse-and-409 pattern. Direction 2 (see src/semantics/mirror.py):
        if this was a point/location entity, its flat field is cleared too
        -- deleting the Brick classification un-classifies the row
        everywhere, rather than leaving objects.point_type/locations.kind
        pointing at a class with no backing entity."""
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM semantic_entities WHERE id=?", (entity_id,)
            ).fetchone()
            cur = conn.execute("DELETE FROM semantic_entities WHERE id=?", (entity_id,))
            if existing is not None:
                existing = dict(existing)
                sync_flat_field_from_entity(
                    conn, entity_kind=existing["entity_kind"], brick_class=None,
                    object_id=existing.get("object_id"), location_id=existing.get("location_id"),
                )
            conn.commit()
            return cur.rowcount > 0

    def _get_or_create_semantic_entity(
        self,
        name: str,
        brick_class: str,
        entity_kind: str,
        *,
        device_id: Optional[int] = None,
        object_id: Optional[int] = None,
        location_id: Optional[int] = None,
        local_slug: Optional[str] = None,
    ) -> dict:
        """Shared upsert used by the get_or_create_*_entity() wrappers below.
        Keys off the derived semantic_key via an atomic SQLite UPSERT (see
        src/semantics/backfill.py's upsert_semantic_entity, which does the
        actual write) rather than an app-level SELECT-then-INSERT, so
        concurrent callers (or Database.setup()'s backfill running twice)
        can't race into a duplicate row."""
        if derive_semantic_key(
            entity_kind, brick_class,
            device_id=device_id, object_id=object_id, location_id=location_id,
            local_slug=local_slug,
        ) is None:
            raise ValueError(
                "get_or_create requires at least one of device_id/object_id/"
                "location_id so a semantic_key can be derived"
            )
        with self._conn() as conn:
            entity = upsert_semantic_entity(
                conn,
                name=name, brick_class=brick_class, entity_kind=entity_kind,
                device_id=device_id, object_id=object_id, location_id=location_id,
                local_slug=local_slug,
            )
            conn.commit()
            return entity

    def get_or_create_equipment_entity(
        self, *, device_id: int, name: str, brick_class: str, local_slug: Optional[str] = None,
    ) -> dict:
        return self._get_or_create_semantic_entity(
            name, brick_class, "equipment", device_id=device_id, local_slug=local_slug,
        )

    def get_or_create_point_entity(
        self, *, object_id: int, name: str, brick_class: str, local_slug: Optional[str] = None,
    ) -> dict:
        return self._get_or_create_semantic_entity(
            name, brick_class, "point", object_id=object_id, local_slug=local_slug,
        )

    def get_or_create_location_entity(
        self,
        *,
        name: str,
        brick_class: str,
        location_id: Optional[int] = None,
        device_id: Optional[int] = None,
        local_slug: Optional[str] = None,
    ) -> dict:
        """location_id (a real locations row) and device_id (a virtual,
        device-hosted zone like a Lighting_Zone) are mutually exclusive —
        see validate_semantic_entity()."""
        return self._get_or_create_semantic_entity(
            name, brick_class, "location",
            device_id=device_id, location_id=location_id, local_slug=local_slug,
        )

    def get_semantic_relationships(
        self,
        *,
        source_entity_id: Optional[int] = None,
        target_entity_id: Optional[int] = None,
        predicate: Optional[str] = None,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        for column, value in (
            ("source_entity_id", source_entity_id),
            ("target_entity_id", target_entity_id),
            ("predicate", predicate),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM semantic_relationships{where} ORDER BY id", params
                )
            ]

    def get_semantic_relationship(self, relationship_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM semantic_relationships WHERE id=?", (relationship_id,)
            ).fetchone()
            return dict(r) if r else None

    def create_semantic_relationship(
        self, source_entity_id: int, predicate: str, target_entity_id: int,
    ) -> dict:
        """Re-creating an identical relationship is a no-op returning the
        existing row, not an error — matches the UPSERT idempotency used
        for semantic_entities (see upsert_semantic_relationship)."""
        with self._conn() as conn:
            relationship = upsert_semantic_relationship(conn, source_entity_id, predicate, target_entity_id)
            conn.commit()
            return relationship

    def delete_semantic_relationship(self, relationship_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM semantic_relationships WHERE id=?", (relationship_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def get_related_entities(
        self, entity_id: int, predicate: str, direction: str = "out",
    ) -> list[dict]:
        """direction='out': entity_id is source_entity_id (what does X
        relate to). direction='in': entity_id is target_entity_id (what
        relates to X) — this is the direction 'get all points isPointOf
        entity X' actually needs, since points are the source of isPointOf
        edges."""
        if direction not in ("out", "in"):
            raise ValueError("direction must be 'out' or 'in'")
        with self._conn() as conn:
            if direction == "out":
                rows = conn.execute(
                    "SELECT se.* FROM semantic_relationships sr "
                    "JOIN semantic_entities se ON se.id = sr.target_entity_id "
                    "WHERE sr.source_entity_id=? AND sr.predicate=?",
                    (entity_id, predicate),
                )
            else:
                rows = conn.execute(
                    "SELECT se.* FROM semantic_relationships sr "
                    "JOIN semantic_entities se ON se.id = sr.source_entity_id "
                    "WHERE sr.target_entity_id=? AND sr.predicate=?",
                    (entity_id, predicate),
                )
            return [dict(r) for r in rows]

    def get_entity_points(
        self, entity_id: int, brick_class: Optional[str] = None,
    ) -> list[dict]:
        """Sugar for get_related_entities(entity_id, 'isPointOf',
        direction='in'), joined to objects for object_type/object_instance/
        name/units so callers get a ready-to-use point row."""
        with self._conn() as conn:
            query = (
                "SELECT se.*, o.object_type, o.object_instance, o.units, o.name AS object_name "
                "FROM semantic_relationships sr "
                "JOIN semantic_entities se ON se.id = sr.source_entity_id "
                "JOIN objects o ON o.id = se.object_id "
                "WHERE sr.target_entity_id=? AND sr.predicate='isPointOf'"
            )
            params: list[Any] = [entity_id]
            if brick_class is not None:
                query += " AND se.brick_class=?"
                params.append(brick_class)
            return [dict(r) for r in conn.execute(query, params)]

    def get_devices(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM devices ORDER BY device_instance")]

    def get_device(self, device_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            return dict(r) if r else None

    def sync_external_devices(self, discovered: list[dict]) -> list[dict]:
        """
        Reconciles a fresh discovery pass into the project's external-BACnet
        device inventory: updates the existing row if a device with this
        instance was already synced, inserts a new row if not. Never
        deletes -- a device missing from one Discover/Rediscover run may
        just be temporarily offline (see plan notes). (device_instance,
        source_type) is the reconciliation key, matching the table's own
        UNIQUE constraint -- this is the only place in the codebase that
        ever writes source_type='external-bacnet'.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            for d in discovered:
                meta = d.get("metadata") or {}
                conn.execute(
                    "INSERT INTO devices (device_instance, name, description, vendor_name, "
                    "model_name, enabled, source_type, external_host, external_port, "
                    "external_vendor_id, external_last_seen_at) "
                    "VALUES (:device_instance, :name, :description, :vendor_name, :model_name, "
                    "1, 'external-bacnet', :external_host, :external_port, "
                    ":external_vendor_id, :external_last_seen_at) "
                    "ON CONFLICT(device_instance, source_type) DO UPDATE SET "
                    "name=excluded.name, description=excluded.description, "
                    "vendor_name=excluded.vendor_name, model_name=excluded.model_name, "
                    "external_host=excluded.external_host, external_port=excluded.external_port, "
                    "external_vendor_id=excluded.external_vendor_id, "
                    "external_last_seen_at=excluded.external_last_seen_at",
                    {
                        "device_instance": d["device_instance"],
                        "name": meta.get("objectName") or d.get("name") or f"bacnet_device_{d['device_instance']}",
                        "description": meta.get("description") or "",
                        "vendor_name": meta.get("vendorName") or "Unknown",
                        "model_name": meta.get("modelName") or "Unknown",
                        "external_host": d.get("host"),
                        "external_port": d.get("port"),
                        "external_vendor_id": meta.get("vendorId"),
                        "external_last_seen_at": now,
                    },
                )
            conn.commit()
            return [dict(r) for r in conn.execute(
                "SELECT * FROM devices WHERE source_type='external-bacnet' ORDER BY device_instance"
            )]

    def create_device(self, data: dict) -> dict:
        # `equipment_type` is presented in the UI as the device's Brick
        # class -- Brick is the source of truth, `devices.equipment_type`
        # is a compatibility mirror kept in lockstep automatically (see
        # src/semantics/mirror.py). This only ever syncs the device's own
        # top-level equipment entity, never Supply_Fan/Return_Fan-style
        # sub-equipment sharing this device_id.
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO devices (device_instance, name, description, vendor_name, model_name, enabled, "
                "firmware_revision, protocol_revision, max_apdu_length_accepted, segmentation_supported, "
                "location_id, equipment_type, can_receive_event_notifications) "
                "VALUES (:device_instance, :name, :description, :vendor_name, :model_name, :enabled, "
                ":firmware_revision, :protocol_revision, :max_apdu_length_accepted, :segmentation_supported, "
                ":location_id, :equipment_type, :can_receive_event_notifications)",
                {
                    **data,
                    "location_id": data.get("location_id"),
                    "equipment_type": data.get("equipment_type"),
                    "can_receive_event_notifications": data.get("can_receive_event_notifications"),
                },
            )
            sync_entity_from_flat_field(
                conn, entity_kind="equipment", name=data.get("name") or "",
                brick_class=data.get("equipment_type"), device_id=cur.lastrowid,
            )
            sync_device_location_relationship(
                conn, device_id=cur.lastrowid, location_id=data.get("location_id"),
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM devices WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_device(self, device_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET device_instance=:device_instance, name=:name, "
                "description=:description, vendor_name=:vendor_name, model_name=:model_name, "
                "enabled=:enabled, firmware_revision=:firmware_revision, protocol_revision=:protocol_revision, "
                "max_apdu_length_accepted=:max_apdu_length_accepted, "
                "segmentation_supported=:segmentation_supported, location_id=:location_id, "
                "equipment_type=:equipment_type, "
                "can_receive_event_notifications=:can_receive_event_notifications WHERE id=:id",
                {
                    **data,
                    "location_id": data.get("location_id"),
                    "equipment_type": data.get("equipment_type"),
                    "can_receive_event_notifications": data.get("can_receive_event_notifications"),
                    "id": device_id,
                },
            )
            sync_entity_from_flat_field(
                conn, entity_kind="equipment", name=data.get("name") or "",
                brick_class=data.get("equipment_type"), device_id=device_id,
            )
            sync_device_location_relationship(
                conn, device_id=device_id, location_id=data.get("location_id"),
            )
            conn.commit()
            r = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            return dict(r) if r else None

    def delete_device(self, device_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_objects(self, device_id: int) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM objects WHERE device_id=? ORDER BY object_type, object_instance",
                (device_id,),
            )]

    def get_object(self, obj_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM objects WHERE id=?", (obj_id,)).fetchone()
            return dict(r) if r else None

    def touch_external_device_last_seen(self, device_id: int) -> None:
        """Bumps external_last_seen_at after a successful discover/refresh
        touch -- the minimal "seen/not seen" signal, no device-health
        subsystem."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET external_last_seen_at=? WHERE id=? AND source_type='external-bacnet'",
                (datetime.now(timezone.utc).isoformat(), device_id),
            )
            conn.commit()

    def sync_external_objects(self, device_id: int, points: list[dict]) -> list[dict]:
        """
        Upserts structural object identity for an external-BACnet device's
        discovered points, keyed on the existing UNIQUE(device_id,
        object_type, object_instance) constraint -- the same identity
        already used for simulated objects. Each `points` entry is a plain
        dict with object_type/object_instance/name/units/description.
        Deliberately never touches behavior/behavior_params/manual_value
        (simulation-only columns, left at their harmless defaults) or any
        live present-value -- present values are read fresh and returned in
        the API response only, never persisted here (see the discovery-vs-
        refresh split in src/api/routers/external_objects.py).
        """
        with self._conn() as conn:
            for p in points:
                conn.execute(
                    "INSERT INTO objects (device_id, object_type, object_instance, name, units, description) "
                    "VALUES (:device_id, :object_type, :object_instance, :name, :units, :description) "
                    "ON CONFLICT(device_id, object_type, object_instance) DO UPDATE SET "
                    "name=excluded.name, units=excluded.units, description=excluded.description",
                    {
                        "device_id": device_id,
                        "object_type": p["object_type"],
                        "object_instance": p["object_instance"],
                        "name": p.get("name") or f"{p['object_type']}_{p['object_instance']}",
                        "units": p.get("units") or "no-units",
                        "description": p.get("description"),
                    },
                )
            conn.commit()
            return [dict(r) for r in conn.execute(
                "SELECT * FROM objects WHERE device_id=? ORDER BY object_type, object_instance",
                (device_id,),
            )]

    def create_object(self, device_id: int, data: dict) -> dict:
        # `point_type` is presented in the UI as the point's Brick class --
        # Brick is the source of truth, `objects.point_type` is a
        # compatibility mirror kept in lockstep automatically (see
        # src/semantics/mirror.py). Unambiguous: at most one point entity
        # can ever reference a given object_id (DB-enforced).
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO objects (device_id, object_type, object_instance, name, units, behavior, behavior_params, enabled, number_of_states, reliability, polarity, point_type) "
                "VALUES (:device_id, :object_type, :object_instance, :name, :units, :behavior, :behavior_params, :enabled, :number_of_states, :reliability, :polarity, :point_type)",
                {**data, "device_id": device_id},
            )
            sync_entity_from_flat_field(
                conn, entity_kind="point", name=data.get("name") or "",
                brick_class=data.get("point_type"), object_id=cur.lastrowid,
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM objects WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_object(self, obj_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE objects SET object_type=:object_type, object_instance=:object_instance, "
                "name=:name, units=:units, behavior=:behavior, behavior_params=:behavior_params, "
                "enabled=:enabled, number_of_states=:number_of_states, reliability=:reliability, "
                "polarity=:polarity, point_type=:point_type WHERE id=:id",
                {**data, "id": obj_id},
            )
            sync_entity_from_flat_field(
                conn, entity_kind="point", name=data.get("name") or "",
                brick_class=data.get("point_type"), object_id=obj_id,
            )
            conn.commit()
            r = conn.execute("SELECT * FROM objects WHERE id=?", (obj_id,)).fetchone()
            return dict(r) if r else None

    def set_manual_value(self, obj_id: int, value: Any) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE objects SET manual_value=? WHERE id=?", (value, obj_id))
            conn.commit()

    def write_object(self, obj_id: int, value: Any) -> None:
        """Switch object to manual behavior and persist the written value."""
        params = json.dumps({"value": value})
        with self._conn() as conn:
            conn.execute(
                "UPDATE objects SET behavior='manual', behavior_params=?, manual_value=? WHERE id=?",
                (params, value, obj_id),
            )
            conn.commit()

    def delete_object(self, obj_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM objects WHERE id=?", (obj_id,))
            conn.commit()
            return cur.rowcount > 0

    def import_ede_objects(self, device_id: int, objects: list[dict]) -> int:
        """Upsert EDE rows into an existing device, keyed on (object_type,
        object_instance) — matches the objects table's UNIQUE constraint."""
        with self._conn() as conn:
            for obj in objects:
                conn.execute(
                    "INSERT INTO objects "
                    "(device_id, object_type, object_instance, name, units, behavior, behavior_params, enabled) "
                    "VALUES (:device_id, :object_type, :object_instance, :name, :units, :behavior, :behavior_params, :enabled) "
                    "ON CONFLICT(device_id, object_type, object_instance) DO UPDATE SET "
                    "name=excluded.name, units=excluded.units, behavior=excluded.behavior, "
                    "behavior_params=excluded.behavior_params, enabled=excluded.enabled",
                    {**obj, "device_id": device_id},
                )
            conn.commit()
            return len(objects)

    # ── Energy────────────────────────────────────────────

    def insert_energy_history_batch(
    self,
    rows: list[dict],
        ) -> None:
            if not rows:
                return

            with self._conn() as conn:
                conn.executemany(
                    """
                    INSERT INTO energy_history (
                        timestamp,
                        device_id,
                        model_type,
                        power_kw,
                        total_energy_kwh,
                        source,
                        confidence,
                        metrics
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            row["timestamp"],
                            row["device_id"],
                            row["model_type"],
                            row.get("power_kw"),
                            row.get("total_energy_kwh"),
                            row.get("source"),
                            row.get("confidence"),
                            row.get("metrics", "{}"),
                        )
                        for row in rows
                    ],
                )

                conn.commit()

    def delete_energy_history_before(
    self,
    cutoff_timestamp: float,
        ) -> int:
            with self._conn() as conn:
                cursor = conn.execute(
                    """
                    DELETE FROM energy_history
                    WHERE timestamp < ?
                    """,
                    (cutoff_timestamp,),
                )

                conn.commit()
                return cursor.rowcount

    def get_energy_history(
    self,
    *,
    start_timestamp: float,
    end_timestamp: float,
    device_id: int | None = None,
    model_type: str | None = None,
    ) -> list[dict]:
        sql = """
            SELECT
                id,
                timestamp,
                device_id,
                model_type,
                power_kw,
                total_energy_kwh,
                source,
                confidence,
                metrics
            FROM energy_history
            WHERE timestamp >= ?
            AND timestamp <= ?
        """

        params: list[object] = [
            start_timestamp,
            end_timestamp,
        ]

        if device_id is not None:
            sql += " AND device_id = ?"
            params.append(device_id)

        if model_type is not None:
            sql += " AND model_type = ?"
            params.append(model_type)

        sql += " ORDER BY timestamp ASC"

        with self._conn() as conn:
            rows = conn.execute(
                sql,
                params,
            ).fetchall()

            return [dict(row) for row in rows]
    
    def get_energy_model_config(self, device_id: int, model_type: str, instance_key: str = "default") -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM energy_model_configs WHERE device_id=? AND model_type=? AND instance_key=?",
                (device_id, model_type, instance_key),
            ).fetchone()
            return dict(row) if row else None

    def get_energy_model_configs(self, device_id: int) -> list[dict]:
        """Every config row for one device, any model_type/instance_key --
        used by the admin UI's per-device Energy Model list (unlike
        get_energy_model_config, which needs the full composite key)."""
        with self._conn() as conn:
            return [
                dict(row) for row in conn.execute(
                    "SELECT * FROM energy_model_configs WHERE device_id=? ORDER BY model_type, instance_key",
                    (device_id,),
                )
            ]

    def get_enabled_energy_model_configs(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM energy_model_configs WHERE enabled=1 ORDER BY device_id, model_type")]

    def upsert_energy_model_config(
        self, device_id: int, model_type: str, parameters: str, enabled: bool = True, instance_key: str = "default",
    ) -> dict:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO energy_model_configs (device_id, model_type, instance_key, enabled, parameters) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id, model_type, instance_key) DO UPDATE SET "
                "enabled=excluded.enabled, parameters=excluded.parameters",
                (device_id, model_type, instance_key, int(enabled), parameters),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM energy_model_configs WHERE device_id=? AND model_type=? AND instance_key=?",
                (device_id, model_type, instance_key),
            ).fetchone()
            if row is None:
                raise RuntimeError("Energy model configuration was not saved")
            return dict(row)

    def delete_energy_model_config(self, device_id: int, model_type: str, instance_key: str = "default") -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM energy_model_configs WHERE device_id=? AND model_type=? AND instance_key=?",
                (device_id, model_type, instance_key),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_energy_model_config_by_id(self, config_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM energy_model_configs WHERE id=?", (config_id,)).fetchone()
            return dict(row) if row else None

    def update_energy_model_config_by_id(
        self, config_id: int, *, model_type: str, instance_key: str, enabled: bool, parameters: str,
    ) -> dict | None:
        """Row-id-addressed update (used by PUT /energy/models/{id}) -- unlike
        upsert_energy_model_config, this can also change model_type/
        instance_key on an existing row, so a change that collides with a
        DIFFERENT existing row raises sqlite3.IntegrityError (caught by the
        router and turned into a 409, same pattern as semantic entities)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE energy_model_configs SET model_type=?, instance_key=?, enabled=?, parameters=? WHERE id=?",
                (model_type, instance_key, int(enabled), parameters, config_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM energy_model_configs WHERE id=?", (config_id,)).fetchone()
            return dict(row) if row else None

    def delete_energy_model_config_by_id(self, config_id: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute("DELETE FROM energy_model_configs WHERE id=?", (config_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ── Alarms: Notification Classes ────────────────────────────────────────────

    def get_notification_classes(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute(
                    "SELECT * FROM notification_classes WHERE device_id=? ORDER BY name", (device_id,)
                )
            else:
                rows = conn.execute("SELECT * FROM notification_classes ORDER BY device_id, name")
            return [dict(r) for r in rows]

    def get_notification_class(self, nc_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM notification_classes WHERE id=?", (nc_id,)).fetchone()
            return dict(r) if r else None

    def create_notification_class(self, device_id: int, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO notification_classes "
                "(device_id, name, priority_to_offnormal, priority_to_fault, priority_to_normal, "
                "ack_required_transitions, recipients) "
                "VALUES (:device_id, :name, :priority_to_offnormal, :priority_to_fault, :priority_to_normal, "
                ":ack_required_transitions, :recipients)",
                {**data, "device_id": device_id},
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT * FROM notification_classes WHERE id=?", (cur.lastrowid,)
            ).fetchone())

    def update_notification_class(self, nc_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE notification_classes SET name=:name, "
                "priority_to_offnormal=:priority_to_offnormal, priority_to_fault=:priority_to_fault, "
                "priority_to_normal=:priority_to_normal, ack_required_transitions=:ack_required_transitions, "
                "recipients=:recipients WHERE id=:id",
                {**data, "id": nc_id},
            )
            conn.commit()
            r = conn.execute("SELECT * FROM notification_classes WHERE id=?", (nc_id,)).fetchone()
            return dict(r) if r else None

    def delete_notification_class(self, nc_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM notification_classes WHERE id=?", (nc_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Alarms: per-object intrinsic reporting config ───────────────────────────

    def get_alarm_config(self, object_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM object_alarm_configs WHERE object_id=?", (object_id,)
            ).fetchone()
            return dict(r) if r else None

    def set_alarm_config(self, object_id: int, data: dict) -> dict:
        """Upsert the intrinsic-reporting config for one object."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO object_alarm_configs "
                "(object_id, notification_class_id, enabled, event_enable, notify_type, "
                "time_delay, time_delay_normal, params) "
                "VALUES (:object_id, :notification_class_id, :enabled, :event_enable, :notify_type, "
                ":time_delay, :time_delay_normal, :params) "
                "ON CONFLICT(object_id) DO UPDATE SET "
                "notification_class_id=excluded.notification_class_id, enabled=excluded.enabled, "
                "event_enable=excluded.event_enable, notify_type=excluded.notify_type, "
                "time_delay=excluded.time_delay, time_delay_normal=excluded.time_delay_normal, "
                "params=excluded.params",
                {**data, "object_id": object_id},
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT * FROM object_alarm_configs WHERE object_id=?", (object_id,)
            ).fetchone())

    def delete_alarm_config(self, object_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM object_alarm_configs WHERE object_id=?", (object_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_all_alarm_configs(self) -> list[dict]:
        """Every enabled intrinsic-reporting config, joined with its object row —
        used once per tick so the engine doesn't have to query per-object."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT oac.*, o.device_id AS obj_device_id, o.object_type, o.object_instance, "
                "o.name AS object_name "
                "FROM object_alarm_configs oac JOIN objects o ON o.id = oac.object_id "
                "WHERE oac.enabled = 1"
            )
            return [dict(r) for r in rows]

    # ── Alarms: event log ────────────────────────────────────────────────────────

    def log_alarm(self, entry: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO alarm_log "
                "(object_id, device_id, object_name, from_state, to_state, priority, value, "
                "message, ack_required) "
                "VALUES (:object_id, :device_id, :object_name, :from_state, :to_state, :priority, "
                ":value, :message, :ack_required)",
                entry,
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM alarm_log WHERE id=?", (cur.lastrowid,)).fetchone())

    def get_alarm_log(self, limit: int = 200, unacked_only: bool = False) -> list[dict]:
        with self._conn() as conn:
            query = "SELECT * FROM alarm_log"
            if unacked_only:
                query += " WHERE ack_required=1 AND acknowledged=0"
            query += " ORDER BY id DESC LIMIT ?"
            return [dict(r) for r in conn.execute(query, (limit,))]

    def ack_alarm(self, alarm_id: int, ack_by: str) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE alarm_log SET acknowledged=1, ack_ts=datetime('now'), ack_by=? WHERE id=?",
                (ack_by, alarm_id),
            )
            conn.commit()
            r = conn.execute("SELECT * FROM alarm_log WHERE id=?", (alarm_id,)).fetchone()
            return dict(r) if r else None

    # ── Alarms: Event Enrollments (Algorithmic Reporting) ───────────────────────

    def get_event_enrollments(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute(
                    "SELECT * FROM event_enrollments WHERE device_id=? ORDER BY name", (device_id,)
                )
            else:
                rows = conn.execute("SELECT * FROM event_enrollments ORDER BY device_id, name")
            return [dict(r) for r in rows]

    def get_event_enrollment(self, ee_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM event_enrollments WHERE id=?", (ee_id,)).fetchone()
            return dict(r) if r else None

    def create_event_enrollment(self, device_id: int, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO event_enrollments "
                "(device_id, name, monitored_object_id, algorithm, event_parameters, "
                "notification_class_id, enabled, event_enable, notify_type, time_delay, time_delay_normal) "
                "VALUES (:device_id, :name, :monitored_object_id, :algorithm, :event_parameters, "
                ":notification_class_id, :enabled, :event_enable, :notify_type, :time_delay, :time_delay_normal)",
                {**data, "device_id": device_id},
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT * FROM event_enrollments WHERE id=?", (cur.lastrowid,)
            ).fetchone())

    def update_event_enrollment(self, ee_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE event_enrollments SET name=:name, monitored_object_id=:monitored_object_id, "
                "algorithm=:algorithm, event_parameters=:event_parameters, "
                "notification_class_id=:notification_class_id, enabled=:enabled, "
                "event_enable=:event_enable, notify_type=:notify_type, time_delay=:time_delay, "
                "time_delay_normal=:time_delay_normal WHERE id=:id",
                {**data, "id": ee_id},
            )
            conn.commit()
            r = conn.execute("SELECT * FROM event_enrollments WHERE id=?", (ee_id,)).fetchone()
            return dict(r) if r else None

    def delete_event_enrollment(self, ee_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM event_enrollments WHERE id=?", (ee_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_all_event_enrollments(self) -> list[dict]:
        """Every enabled enrollment joined with its monitored object row —
        used once per tick, mirroring get_all_alarm_configs()."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ee.*, o.object_type, o.object_instance, o.name AS object_name, "
                "o.units AS object_units "
                "FROM event_enrollments ee JOIN objects o ON o.id = ee.monitored_object_id "
                "WHERE ee.enabled = 1"
            )
            return [dict(r) for r in rows]

    # ── Trend Logs ───────────────────────────────────────────────────────────────

    def get_trend_logs(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute("SELECT * FROM trend_logs WHERE device_id=? ORDER BY name", (device_id,))
            else:
                rows = conn.execute("SELECT * FROM trend_logs ORDER BY device_id, name")
            return [dict(r) for r in rows]

    def get_trend_log(self, tl_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM trend_logs WHERE id=?", (tl_id,)).fetchone()
            return dict(r) if r else None

    def create_trend_log(self, device_id: int, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO trend_logs "
                "(device_id, name, description, monitored_object_id, logging_type, log_interval, "
                "cov_increment, buffer_size, stop_when_full, enabled) "
                "VALUES (:device_id, :name, :description, :monitored_object_id, :logging_type, :log_interval, "
                ":cov_increment, :buffer_size, :stop_when_full, :enabled)",
                {**data, "device_id": device_id},
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM trend_logs WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_trend_log(self, tl_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE trend_logs SET name=:name, description=:description, "
                "monitored_object_id=:monitored_object_id, logging_type=:logging_type, "
                "log_interval=:log_interval, cov_increment=:cov_increment, buffer_size=:buffer_size, "
                "stop_when_full=:stop_when_full, enabled=:enabled WHERE id=:id",
                {**data, "id": tl_id},
            )
            conn.commit()
            r = conn.execute("SELECT * FROM trend_logs WHERE id=?", (tl_id,)).fetchone()
            return dict(r) if r else None

    def delete_trend_log(self, tl_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM trend_logs WHERE id=?", (tl_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_all_trend_logs(self) -> list[dict]:
        """Every enabled trend log joined with its monitored object row —
        used once per tick, mirroring get_all_alarm_configs()."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT tl.*, o.object_type, o.object_instance, o.name AS object_name "
                "FROM trend_logs tl JOIN objects o ON o.id = tl.monitored_object_id "
                "WHERE tl.enabled = 1"
            )
            return [dict(r) for r in rows]

    def add_trend_record(self, trend_log_id: int, value: Any, status_flags: str = "[]") -> Optional[int]:
        """Append a sample, enforcing the circular-buffer/stop-when-full
        semantics. Returns the new record's sequence number, or None if the
        buffer is full and stop_when_full is set."""
        with self._conn() as conn:
            cfg = conn.execute("SELECT * FROM trend_logs WHERE id=?", (trend_log_id,)).fetchone()
            if not cfg:
                return None
            if cfg["record_count"] >= cfg["buffer_size"]:
                if cfg["stop_when_full"]:
                    return None
                conn.execute(
                    "DELETE FROM trend_log_records WHERE id = ("
                    "SELECT id FROM trend_log_records WHERE trend_log_id=? ORDER BY sequence_number ASC LIMIT 1)",
                    (trend_log_id,),
                )
                new_count = cfg["record_count"]
            else:
                new_count = cfg["record_count"] + 1
            next_seq = cfg["total_record_count"] + 1
            conn.execute(
                "INSERT INTO trend_log_records (trend_log_id, sequence_number, value, status_flags) "
                "VALUES (?, ?, ?, ?)",
                (trend_log_id, next_seq, str(value), status_flags),
            )
            conn.execute(
                "UPDATE trend_logs SET record_count=?, total_record_count=?, last_sampled_at=? WHERE id=?",
                (new_count, next_seq, time.time(), trend_log_id),
            )
            conn.commit()
            return next_seq

    def get_trend_log_records(
        self, trend_log_id: int, from_ts: Optional[str] = None, to_ts: Optional[str] = None,
        start_sequence: Optional[int] = None, limit: int = 200, order: str = "asc",
    ) -> list[dict]:
        with self._conn() as conn:
            query = "SELECT * FROM trend_log_records WHERE trend_log_id=?"
            params: list[Any] = [trend_log_id]
            if from_ts:
                query += " AND ts >= ?"
                params.append(from_ts)
            if to_ts:
                query += " AND ts <= ?"
                params.append(to_ts)
            if start_sequence is not None:
                query += " AND sequence_number >= ?"
                params.append(start_sequence)
            query += f" ORDER BY sequence_number {'DESC' if order == 'desc' else 'ASC'} LIMIT ?"
            params.append(limit)
            return [dict(r) for r in conn.execute(query, params)]

    def clear_trend_log_records(self, trend_log_id: int) -> bool:
        """Clears the buffer but keeps total_record_count monotonic, matching
        real BACnet Trend Log clear-buffer semantics (sequence numbers never
        reuse, even across a clear)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM trend_log_records WHERE trend_log_id=?", (trend_log_id,))
            cur = conn.execute("UPDATE trend_logs SET record_count=0 WHERE id=?", (trend_log_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── BACnet Schedules ─────────────────────────────────────────────────────────
    # Note: unlike alarms/trend logs, schedules don't need per-tick evaluation —
    # bacnet_schedule.LocalScheduleObject (built on bacpypes3's own
    # ScheduleObject) self-schedules its own next transition via asyncio, so
    # the engine only needs to (re)construct them on start()/reload().

    def get_schedules(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute(
                    "SELECT * FROM bacnet_schedules WHERE device_id=? ORDER BY name", (device_id,)
                )
            else:
                rows = conn.execute("SELECT * FROM bacnet_schedules ORDER BY device_id, name")
            return [dict(r) for r in rows]

    def get_schedule(self, schedule_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM bacnet_schedules WHERE id=?", (schedule_id,)).fetchone()
            return dict(r) if r else None

    def get_schedule_targets(self, schedule_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT t.*, o.object_type, o.object_instance, o.name AS object_name "
                "FROM bacnet_schedule_targets t JOIN objects o ON o.id = t.object_id "
                "WHERE t.schedule_id=?",
                (schedule_id,),
            )
            return [dict(r) for r in rows]

    def _set_schedule_targets(self, conn: sqlite3.Connection, schedule_id: int, targets: list[dict]) -> None:
        conn.execute("DELETE FROM bacnet_schedule_targets WHERE schedule_id=?", (schedule_id,))
        for t in targets:
            conn.execute(
                "INSERT INTO bacnet_schedule_targets (schedule_id, object_id, property_identifier) "
                "VALUES (?, ?, ?)",
                (schedule_id, t["object_id"], t.get("property_identifier", "present-value")),
            )

    def create_schedule(self, device_id: int, data: dict, targets: list[dict]) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO bacnet_schedules "
                "(device_id, name, description, value_type, schedule_default, effective_start, "
                "effective_end, weekly_schedule, exception_schedule, priority_for_writing, enabled) "
                "VALUES (:device_id, :name, :description, :value_type, :schedule_default, :effective_start, "
                ":effective_end, :weekly_schedule, :exception_schedule, :priority_for_writing, :enabled)",
                {**data, "device_id": device_id},
            )
            schedule_id = cur.lastrowid
            self._set_schedule_targets(conn, schedule_id, targets)
            conn.commit()
            return dict(conn.execute("SELECT * FROM bacnet_schedules WHERE id=?", (schedule_id,)).fetchone())

    def update_schedule(self, schedule_id: int, data: dict, targets: list[dict]) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE bacnet_schedules SET name=:name, description=:description, "
                "value_type=:value_type, schedule_default=:schedule_default, "
                "effective_start=:effective_start, effective_end=:effective_end, "
                "weekly_schedule=:weekly_schedule, exception_schedule=:exception_schedule, "
                "priority_for_writing=:priority_for_writing, enabled=:enabled WHERE id=:id",
                {**data, "id": schedule_id},
            )
            self._set_schedule_targets(conn, schedule_id, targets)
            conn.commit()
            r = conn.execute("SELECT * FROM bacnet_schedules WHERE id=?", (schedule_id,)).fetchone()
            return dict(r) if r else None

    def set_schedule_enabled(self, schedule_id: int, enabled: bool) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE bacnet_schedules SET enabled=? WHERE id=?", (1 if enabled else 0, schedule_id)
            )
            conn.commit()
            r = conn.execute("SELECT * FROM bacnet_schedules WHERE id=?", (schedule_id,)).fetchone()
            return dict(r) if r else None

    def delete_schedule(self, schedule_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM bacnet_schedules WHERE id=?", (schedule_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── BACnet Calendars (GH #18) ────────────────────────────────────────────────
    # Referenced by name from a Schedule's exception_schedule JSON (see
    # bacnet_schedule.build_exception_schedule) rather than by a DB foreign key —
    # that keeps the reference portable across project save/load the same way
    # object_type+object_instance already does for schedule targets.

    def get_calendars(self, device_id: Optional[int] = None) -> list[dict]:
        with self._conn() as conn:
            if device_id is not None:
                rows = conn.execute(
                    "SELECT * FROM bacnet_calendars WHERE device_id=? ORDER BY name", (device_id,)
                )
            else:
                rows = conn.execute("SELECT * FROM bacnet_calendars ORDER BY device_id, name")
            return [dict(r) for r in rows]

    def get_calendar(self, calendar_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM bacnet_calendars WHERE id=?", (calendar_id,)).fetchone()
            return dict(r) if r else None

    def create_calendar(self, device_id: int, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO bacnet_calendars (device_id, name, description, date_list, enabled) "
                "VALUES (:device_id, :name, :description, :date_list, :enabled)",
                {**data, "device_id": device_id},
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM bacnet_calendars WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_calendar(self, calendar_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            conn.execute(
                "UPDATE bacnet_calendars SET name=:name, description=:description, "
                "date_list=:date_list, enabled=:enabled WHERE id=:id",
                {**data, "id": calendar_id},
            )
            conn.commit()
            r = conn.execute("SELECT * FROM bacnet_calendars WHERE id=?", (calendar_id,)).fetchone()
            return dict(r) if r else None

    def delete_calendar(self, calendar_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM bacnet_calendars WHERE id=?", (calendar_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Projects ──────────────────────────────────────────────────────────────
    # Persisted as "profiles" in the DB schema (table name unchanged, see
    # CREATE TABLE above) — only the Python-level naming was renamed to
    # "project" to match the admin UI ("New Project"/"Open Project"/"Save").

    def get_projects(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles ORDER BY created_at DESC"
            )]

    def get_project(self, project_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM profiles WHERE id=?", (project_id,)).fetchone()
            return dict(r) if r else None


    # ── Fault Detection  ──────────────────────────────────────────────────────────────
    def get_fault_rule_configs(
    self,
    device_id: int,
        ) -> list[dict]:
            with self._conn() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM fault_rule_configs
                    WHERE device_id = ?
                    ORDER BY rule_id
                    """,
                    (device_id,),
                )

                return [
                    dict(row)
                    for row in rows
                ]


    def upsert_fault_rule_config(
        self,
        device_id: int,
        rule_id: str,
        data: dict,
    ) -> dict:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO fault_rule_configs (
                    device_id,
                    rule_id,
                    enabled,
                    parameters,
                    persistence_seconds,
                    clear_seconds,
                    severity
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, rule_id)
                DO UPDATE SET
                    enabled = excluded.enabled,
                    parameters = excluded.parameters,
                    persistence_seconds =
                        excluded.persistence_seconds,
                    clear_seconds =
                        excluded.clear_seconds,
                    severity = excluded.severity
                """,
                (
                    device_id,
                    rule_id,
                    int(bool(data.get("enabled", True))),
                    data.get("parameters", "{}"),
                    data.get("persistence_seconds"),
                    data.get("clear_seconds"),
                    data.get("severity"),
                ),
            )

            conn.commit()

            row = conn.execute(
                """
                SELECT *
                FROM fault_rule_configs
                WHERE device_id = ?
                AND rule_id = ?
                """,
                (
                    device_id,
                    rule_id,
                ),
            ).fetchone()

            if row is None:
                raise RuntimeError(
                    "Fault rule configuration was not saved"
                )

            return dict(row)


    def record_fault_evaluation(
        self,
        data: dict,
    ) -> dict:
        with self._conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO fault_events (
                    device_id,
                    rule_id,
                    state,
                    previous_state,
                    severity,
                    message,
                    evidence,
                    timestamp,
                    activated_at,
                    cleared_at
                )
                VALUES (
                    :device_id,
                    :rule_id,
                    :state,
                    :previous_state,
                    :severity,
                    :message,
                    :evidence,
                    :timestamp,
                    :activated_at,
                    :cleared_at
                )
                """,
                data,
            )

            conn.commit()

            row = conn.execute(
                """
                SELECT *
                FROM fault_events
                WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()

            if row is None:
                raise RuntimeError(
                    "Fault event was not recorded"
                )

            return dict(row)


    def get_fault_events(
        self,
        *,
        device_id: int | None = None,
        active_only: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        query = """
            SELECT *
            FROM fault_events
        """

        clauses: list[str] = []
        values: list = []

        if device_id is not None:
            clauses.append(
                "device_id = ?"
            )
            values.append(device_id)

        if active_only:
            clauses.append(
                "state = 'active'"
            )

        if clauses:
            query += (
                " WHERE "
                + " AND ".join(clauses)
            )

        query += """
            ORDER BY id DESC
            LIMIT ?
        """

        values.append(limit)

        with self._conn() as conn:
            rows = conn.execute(
                query,
                values,
            )

            return [
                dict(row)
                for row in rows
            ]

    @staticmethod
    def _attach_trend_logs(conn: sqlite3.Connection, dev: dict) -> None:
        """Snapshot this device's trend logs for a project, replacing the
        live monitored_object_id with a portable (object_type, object_instance)
        reference — object ids are reassigned on load, so a raw id wouldn't
        survive the round trip."""
        trend_logs = [dict(r) for r in conn.execute(
            "SELECT * FROM trend_logs WHERE device_id=?", (dev["id"],)
        )]
        for tl in trend_logs:
            mon = conn.execute(
                "SELECT object_type, object_instance FROM objects WHERE id=?",
                (tl.pop("monitored_object_id"),),
            ).fetchone()
            tl["monitored_object_ref"] = (
                {"object_type": mon["object_type"], "object_instance": mon["object_instance"]}
                if mon else None
            )
            tl.pop("id", None)
            tl.pop("device_id", None)
            # Historical records aren't part of a project snapshot, only config.
            tl.pop("record_count", None)
            tl.pop("total_record_count", None)
            tl.pop("last_sampled_at", None)
        dev["trend_logs"] = trend_logs

    @staticmethod
    def _attach_schedules(conn: sqlite3.Connection, dev: dict) -> None:
        """Same portable-reference approach as _attach_trend_logs, but for
        each schedule's (potentially multiple) target object references."""
        schedules = [dict(r) for r in conn.execute(
            "SELECT * FROM bacnet_schedules WHERE device_id=?", (dev["id"],)
        )]
        for sched in schedules:
            targets = conn.execute(
                "SELECT object_id, property_identifier FROM bacnet_schedule_targets WHERE schedule_id=?",
                (sched["id"],),
            )
            portable_targets = []
            for t in targets:
                mon = conn.execute(
                    "SELECT object_type, object_instance FROM objects WHERE id=?", (t["object_id"],)
                ).fetchone()
                if mon:
                    portable_targets.append({
                        "object_type": mon["object_type"],
                        "object_instance": mon["object_instance"],
                        "property_identifier": t["property_identifier"],
                    })
            sched["targets"] = portable_targets
            sched.pop("id", None)
            sched.pop("device_id", None)
        dev["schedules"] = schedules

    @staticmethod
    def _attach_calendars(conn: sqlite3.Connection, dev: dict) -> None:
        """Calendars need no portable-reference handling of their own — a
        Schedule's calendarReference exceptions already store the calendar by
        name (see bacnet_schedule.build_exception_schedule), which survives
        the id reassignment on project load without any extra work here."""
        calendars = [dict(r) for r in conn.execute(
            "SELECT * FROM bacnet_calendars WHERE device_id=?", (dev["id"],)
        )]
        for cal in calendars:
            cal.pop("id", None)
            cal.pop("device_id", None)
        dev["calendars"] = calendars

    # ─── Functional tests ──────────────────────────────────────────────────
    # Project-level data, independent of any single device/object/location
    # (identified only by equipment_type, a plain validated string) --
    # included verbatim in save_project/load_project below, no id-remapping
    # needed since nothing references a functional_tests row and it
    # references nothing itself.

    def create_functional_test(self, data: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO functional_tests (name, description, equipment_type, definition_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (data["name"], data.get("description", ""), data["equipment_type"], json.dumps(data["definition"]), now, now),
            )
            conn.commit()
            return self._functional_test_row(conn, cur.lastrowid)

    def get_functional_tests(self) -> list[dict]:
        with self._conn() as conn:
            return [
                self._functional_test_dict(r)
                for r in conn.execute("SELECT * FROM functional_tests ORDER BY name")
            ]

    def get_functional_test(self, test_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM functional_tests WHERE id=?", (test_id,)).fetchone()
            return self._functional_test_dict(row) if row else None

    def update_functional_test(self, test_id: int, data: dict) -> Optional[dict]:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE functional_tests SET name=?, description=?, equipment_type=?, definition_json=?, updated_at=? WHERE id=?",
                (data["name"], data.get("description", ""), data["equipment_type"], json.dumps(data["definition"]),
                 datetime.now(timezone.utc).isoformat(), test_id),
            )
            if cur.rowcount == 0:
                conn.commit()
                return None
            conn.commit()
            return self._functional_test_row(conn, test_id)

    def delete_functional_test(self, test_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM functional_tests WHERE id=?", (test_id,))
            conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _functional_test_row(conn: sqlite3.Connection, test_id: int) -> dict:
        row = conn.execute("SELECT * FROM functional_tests WHERE id=?", (test_id,)).fetchone()
        return Database._functional_test_dict(row)

    @staticmethod
    def _functional_test_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["definition"] = json.loads(d.pop("definition_json"))
        return d

    # ── Functional test runs (execution state -- not project data, see
    # save_project/load_project below: deliberately not included in the
    # project JSON blob, and ON DELETE CASCADE on both FKs means a project
    # reload's DELETE FROM functional_tests / device wipe already cleans
    # these up with no extra code needed here). ──────────────────────────

    _FUNCTIONAL_TEST_RUN_UPDATABLE_FIELDS = {
        "state", "started_at", "finished_at", "result", "result_message",
        "current_node_id", "error",
    }

    def create_functional_test_run(self, data: dict) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO functional_test_runs (functional_test_id, target_device_id, execution_mode) "
                "VALUES (?,?,?)",
                (data["functional_test_id"], data["target_device_id"], data["execution_mode"]),
            )
            conn.commit()
            return self._functional_test_run_row(conn, cur.lastrowid)

    def get_functional_test_run(self, run_id: int) -> Optional[dict]:
        with self._conn() as conn:
            return self._functional_test_run_row(conn, run_id)

    def find_active_functional_test_run(self, functional_test_id: int, target_device_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM functional_test_runs WHERE functional_test_id=? AND target_device_id=? "
                "AND state IN ('pending','running') ORDER BY id DESC LIMIT 1",
                (functional_test_id, target_device_id),
            ).fetchone()
            return self._functional_test_run_dict(row) if row else None

    def update_functional_test_run(self, run_id: int, **fields) -> Optional[dict]:
        updates = {k: v for k, v in fields.items() if k in self._FUNCTIONAL_TEST_RUN_UPDATABLE_FIELDS}
        if not updates:
            with self._conn() as conn:
                return self._functional_test_run_row(conn, run_id)
        set_clause = ", ".join(f"{k}=?" for k in updates)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE functional_test_runs SET {set_clause} WHERE id=?",
                (*updates.values(), run_id),
            )
            conn.commit()
            return self._functional_test_run_row(conn, run_id)

    def append_functional_test_run_detail(self, run_id: int, node_id: str, entry: dict) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT details_json FROM functional_test_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                return
            details = json.loads(row["details_json"] or "[]")
            details.append(entry)
            conn.execute(
                "UPDATE functional_test_runs SET details_json=?, current_node_id=? WHERE id=?",
                (json.dumps(details), node_id, run_id),
            )
            conn.commit()

    @staticmethod
    def _functional_test_run_row(conn: sqlite3.Connection, run_id: int) -> Optional[dict]:
        row = conn.execute("SELECT * FROM functional_test_runs WHERE id=?", (run_id,)).fetchone()
        return Database._functional_test_run_dict(row) if row else None

    @staticmethod
    def _functional_test_run_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["details"] = json.loads(d.pop("details_json") or "[]")
        return d

    def save_project(
        self,
        name: str,
        description: str,
        source_type: str = "simulated",
        connection_config: Optional[dict] = None,
    ) -> dict:
        with self._conn() as conn:
            locations = [dict(r) for r in conn.execute("SELECT * FROM locations")]
            devices = [dict(r) for r in conn.execute(
                "SELECT * FROM devices ORDER BY device_instance"
            )]
            for dev in devices:
                dev["objects"] = [dict(r) for r in conn.execute(
                    "SELECT * FROM objects WHERE device_id=? ORDER BY object_type, object_instance",
                    (dev["id"],),
                )]
                self._attach_trend_logs(conn, dev)
                self._attach_schedules(conn, dev)
                self._attach_calendars(conn, dev)
            # Brick Core: stored verbatim, including the (soon-to-be-stale)
            # id/semantic_key values -- load_project() remaps ids and
            # recomputes semantic_key from the newly-assigned ones rather
            # than trusting what's stored here. See load_project()'s
            # comment for why semantic_key can't just be copied across.
            semantic_entities = [dict(r) for r in conn.execute("SELECT * FROM semantic_entities")]
            semantic_relationships = [dict(r) for r in conn.execute("SELECT * FROM semantic_relationships")]
            # Stored verbatim, including the (soon-to-be-stale) id/device_id
            # values -- load_project() remaps device_id through dev_id_map
            # the same way it already does for semantic_entities, rather
            # than trusting the stored id directly.
            energy_model_configs = [dict(r) for r in conn.execute("SELECT * FROM energy_model_configs")]
            # No id-remapping needed on restore -- functional_tests has no FK
            # to device/object/location at all (see create_functional_test's
            # own comment), so this is stored/restored verbatim like the
            # other blob fields but with none of their remap bookkeeping.
            functional_tests = [dict(r) for r in conn.execute("SELECT * FROM functional_tests")]
            data = json.dumps({
                "locations": locations,
                "devices": devices,
                "semantic_entities": semantic_entities,
                "semantic_relationships": semantic_relationships,
                "energy_model_configs": energy_model_configs,
                "functional_tests": functional_tests,
                "source_type": source_type,
                "connection_config": connection_config,
            })
            cur = conn.execute(
                "INSERT INTO profiles (name, description, device_count, data) VALUES (?,?,?,?)",
                (name, description, len(devices), data),
            )
            conn.commit()
            row = dict(conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles WHERE id=?",
                (cur.lastrowid,),
            ).fetchone())
            row["source_type"] = source_type
            row["connection_config"] = connection_config
            return row

    def update_project(self, project_id: int, name: str, description: str) -> bool:
        with self._conn() as conn:
            # source_type/connection_config aren't part of live device/location
            # state -- they only exist in this row's own stored data blob, so
            # they must be read back and re-embedded rather than dropped.
            existing = conn.execute(
                "SELECT data FROM profiles WHERE id=?", (project_id,)
            ).fetchone()
            existing_payload = json.loads(existing["data"]) if existing else {}
            source_type = existing_payload.get("source_type", "simulated")
            connection_config = existing_payload.get("connection_config")

            locations = [dict(r) for r in conn.execute("SELECT * FROM locations")]
            devices = [dict(r) for r in conn.execute(
                "SELECT * FROM devices ORDER BY device_instance"
            )]
            for dev in devices:
                dev["objects"] = [dict(r) for r in conn.execute(
                    "SELECT * FROM objects WHERE device_id=? ORDER BY object_type, object_instance",
                    (dev["id"],),
                )]
                self._attach_trend_logs(conn, dev)
                self._attach_schedules(conn, dev)
                self._attach_calendars(conn, dev)
            semantic_entities = [dict(r) for r in conn.execute("SELECT * FROM semantic_entities")]
            semantic_relationships = [dict(r) for r in conn.execute("SELECT * FROM semantic_relationships")]
            energy_model_configs = [dict(r) for r in conn.execute("SELECT * FROM energy_model_configs")]
            functional_tests = [dict(r) for r in conn.execute("SELECT * FROM functional_tests")]
            data = json.dumps({
                "locations": locations,
                "devices": devices,
                "semantic_entities": semantic_entities,
                "semantic_relationships": semantic_relationships,
                "energy_model_configs": energy_model_configs,
                "functional_tests": functional_tests,
                "source_type": source_type,
                "connection_config": connection_config,
            })
            cur = conn.execute(
                "UPDATE profiles SET name=?, description=?, device_count=?, data=? WHERE id=?",
                (name, description, len(devices), data, project_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def clear_live_state(self) -> None:
        """Wipes all live project state back to blank -- the exact same
        sequence load_project() uses to clear the slate before restoring a
        saved snapshot (see that method's own comment for why semantic
        tables must be cleared before devices/locations, not via cascade:
        semantic_entities.location_id has no ON DELETE CASCADE, so an
        unguarded location delete can be permanently blocked by a leftover
        semantic entity). Used by the "New Project" reset flow so it can't
        leave orphaned locations/semantic entities behind the way a pile of
        individual per-row API deletes could."""
        with self._conn() as conn:
            conn.execute("DELETE FROM semantic_relationships")
            conn.execute("DELETE FROM semantic_entities")
            conn.execute("DELETE FROM devices")
            conn.execute("DELETE FROM locations")
            conn.execute("DELETE FROM functional_tests")
            conn.commit()

    def load_project(self, project_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT data FROM profiles WHERE id=?", (project_id,)).fetchone()
            if not row:
                return None
            payload = json.loads(row["data"])
            # Wipe semantic tables BEFORE devices/locations, not after and
            # not via cascade: semantic_entities.location_id has no
            # ON DELETE CASCADE (locations deletion normally goes through
            # delete_location()'s guard), so an unguarded DELETE FROM
            # locations below would otherwise hit a foreign-key-constraint
            # failure if any leftover semantic_entities row still pointed
            # at one of those locations. This also guarantees no stale rows
            # survive to collide with the semantic_key values recomputed
            # below once ids are reassigned.
            conn.execute("DELETE FROM semantic_relationships")
            conn.execute("DELETE FROM semantic_entities")
            conn.execute("DELETE FROM devices")
            conn.execute("DELETE FROM locations")
            conn.execute("DELETE FROM functional_tests")
            conn.commit()

            # Restore locations parent-before-child, remapping old ids -> new
            # ids as we go (a fresh INSERT can't reuse the old autoincrement
            # ids) — same approach as objects/devices below, just one level
            # up since locations can nest into each other.
            location_id_map: dict[int, int] = {}
            remaining = list(payload.get("locations", []))
            while remaining:
                progressed = False
                still_remaining = []
                for loc in remaining:
                    old_id = loc["id"]
                    old_parent = loc.get("parent_location_id")
                    if old_parent is None or old_parent in location_id_map:
                        cur = conn.execute(
                            "INSERT INTO locations (name, parent_location_id, description, kind) VALUES (?,?,?,?)",
                            (loc["name"], location_id_map.get(old_parent) if old_parent is not None else None, loc.get("description", ""), loc.get("kind")),
                        )
                        location_id_map[old_id] = cur.lastrowid
                        progressed = True
                    else:
                        still_remaining.append(loc)
                if not progressed:
                    # Malformed/cyclic parent references in old data — bail
                    # rather than looping forever; leftover locations are
                    # simply not restored.
                    break
                remaining = still_remaining
            conn.commit()

            # Brick Core: semantic_entities.device_id/object_id can reference
            # ANY device/object in the project, not just "the current one" --
            # unlike obj_lookup below (kept as-is, per-device, keyed by
            # (object_type, object_instance) for trend_logs/schedules), these
            # two maps accumulate across the WHOLE devices loop so entities
            # can be restored afterward regardless of which device they
            # belong to.
            dev_id_map: dict[int, int] = {}
            global_obj_id_map: dict[int, int] = {}

            for dev in payload.get("devices", []):
                objects = dev.pop("objects", [])
                trend_logs = dev.pop("trend_logs", [])
                schedules = dev.pop("schedules", [])
                calendars = dev.pop("calendars", [])
                old_dev_id = dev.pop("id", None)
                old_location_id = dev.get("location_id")
                cur = conn.execute(
                    "INSERT INTO devices (device_instance, name, description, vendor_name, model_name, enabled, "
                    "firmware_revision, protocol_revision, max_apdu_length_accepted, segmentation_supported, "
                    "location_id, equipment_type, source_type, external_host, external_port, "
                    "external_vendor_id, external_last_seen_at) "
                    "VALUES (:device_instance, :name, :description, :vendor_name, :model_name, :enabled, "
                    ":firmware_revision, :protocol_revision, :max_apdu_length_accepted, :segmentation_supported, "
                    ":location_id, :equipment_type, :source_type, :external_host, :external_port, "
                    ":external_vendor_id, :external_last_seen_at)",
                    {
                        **dev,
                        "firmware_revision": dev.get("firmware_revision") or "N/A",
                        "protocol_revision": dev.get("protocol_revision") or 22,
                        "max_apdu_length_accepted": dev.get("max_apdu_length_accepted") or 1024,
                        "segmentation_supported": dev.get("segmentation_supported") or "segmented-both",
                        "location_id": location_id_map.get(old_location_id) if old_location_id is not None else None,
                        "equipment_type": dev.get("equipment_type"),
                        # Older saved projects (pre-dating this column) have no
                        # source_type at all -- default to 'simulated', same
                        # fallback SimEngine._simulated_enabled_devices() uses,
                        # so a device never silently becomes external (or vice
                        # versa, see the safety-boundary requirement) just by
                        # being saved/reloaded.
                        "source_type": dev.get("source_type") or "simulated",
                        "external_host": dev.get("external_host"),
                        "external_port": dev.get("external_port"),
                        "external_vendor_id": dev.get("external_vendor_id"),
                        "external_last_seen_at": dev.get("external_last_seen_at"),
                    },
                )
                dev_id = cur.lastrowid
                if old_dev_id is not None:
                    dev_id_map[old_dev_id] = dev_id
                obj_lookup: dict[tuple[str, int], int] = {}
                for obj in objects:
                    old_obj_id = obj.pop("id", None)
                    obj.pop("device_id", None)
                    obj_cur = conn.execute(
                        "INSERT OR IGNORE INTO objects "
                        "(device_id, object_type, object_instance, name, units, behavior, "
                        "behavior_params, enabled, manual_value, number_of_states, reliability, polarity, point_type, description) "
                        "VALUES (:device_id, :object_type, :object_instance, :name, :units, "
                        ":behavior, :behavior_params, :enabled, :manual_value, :number_of_states, :reliability, :polarity, :point_type, :description)",
                        {
                            **obj,
                            "device_id": dev_id,
                            "manual_value": obj.get("manual_value"),
                            "number_of_states": obj.get("number_of_states") or 2,
                            "reliability": obj.get("reliability") or "no-fault-detected",
                            "polarity": obj.get("polarity") or "normal",
                            "point_type": obj.get("point_type"),
                            "description": obj.get("description"),
                        },
                    )
                    obj_id = obj_cur.lastrowid if obj_cur.lastrowid else conn.execute(
                        "SELECT id FROM objects WHERE device_id=? AND object_type=? AND object_instance=?",
                        (dev_id, obj["object_type"], obj["object_instance"]),
                    ).fetchone()[0]
                    obj_lookup[(obj["object_type"], obj["object_instance"])] = obj_id
                    if old_obj_id is not None:
                        global_obj_id_map[old_obj_id] = obj_id

                for tl in trend_logs:
                    ref = tl.pop("monitored_object_ref", None)
                    if not ref:
                        continue
                    mon_id = obj_lookup.get((ref["object_type"], ref["object_instance"]))
                    if mon_id is None:
                        continue
                    conn.execute(
                        "INSERT INTO trend_logs "
                        "(device_id, name, description, monitored_object_id, logging_type, log_interval, "
                        "cov_increment, buffer_size, stop_when_full, enabled) "
                        "VALUES (:device_id, :name, :description, :monitored_object_id, :logging_type, "
                        ":log_interval, :cov_increment, :buffer_size, :stop_when_full, :enabled)",
                        {
                            "device_id": dev_id,
                            "name": tl.get("name", "Trend Log"),
                            "description": tl.get("description", ""),
                            "monitored_object_id": mon_id,
                            "logging_type": tl.get("logging_type", "polled"),
                            "log_interval": tl.get("log_interval", 60),
                            "cov_increment": tl.get("cov_increment", 1.0),
                            "buffer_size": tl.get("buffer_size", 1000),
                            "stop_when_full": tl.get("stop_when_full", 0),
                            "enabled": tl.get("enabled", 1),
                        },
                    )

                for sched in schedules:
                    portable_targets = sched.pop("targets", [])
                    sched_cur = conn.execute(
                        "INSERT INTO bacnet_schedules "
                        "(device_id, name, description, value_type, schedule_default, effective_start, "
                        "effective_end, weekly_schedule, exception_schedule, priority_for_writing, enabled) "
                        "VALUES (:device_id, :name, :description, :value_type, :schedule_default, "
                        ":effective_start, :effective_end, :weekly_schedule, :exception_schedule, "
                        ":priority_for_writing, :enabled)",
                        {
                            "device_id": dev_id,
                            "name": sched.get("name", "Schedule"),
                            "description": sched.get("description", ""),
                            "value_type": sched.get("value_type", "real"),
                            "schedule_default": sched.get("schedule_default", "0"),
                            "effective_start": sched.get("effective_start"),
                            "effective_end": sched.get("effective_end"),
                            "weekly_schedule": sched.get("weekly_schedule", "{}"),
                            "exception_schedule": sched.get("exception_schedule", "[]"),
                            "priority_for_writing": sched.get("priority_for_writing", 10),
                            "enabled": sched.get("enabled", 1),
                        },
                    )
                    schedule_id = sched_cur.lastrowid
                    for t in portable_targets:
                        target_id = obj_lookup.get((t["object_type"], t["object_instance"]))
                        if target_id is None:
                            continue
                        conn.execute(
                            "INSERT INTO bacnet_schedule_targets (schedule_id, object_id, property_identifier) "
                            "VALUES (?, ?, ?)",
                            (schedule_id, target_id, t.get("property_identifier", "present-value")),
                        )

                for cal in calendars:
                    conn.execute(
                        "INSERT INTO bacnet_calendars (device_id, name, description, date_list, enabled) "
                        "VALUES (:device_id, :name, :description, :date_list, :enabled)",
                        {
                            "device_id": dev_id,
                            "name": cal.get("name", "Calendar"),
                            "description": cal.get("description", ""),
                            "date_list": cal.get("date_list", "[]"),
                            "enabled": cal.get("enabled", 1),
                        },
                    )

            # Energy model configs reference a device only (no objects/
            # locations involved), so they just need dev_id_map, already
            # fully populated by the devices loop above. Skip silently if
            # the owning device wasn't restored, rather than fail the whole
            # import -- same tolerance as the relationship restore below.
            for cfg in payload.get("energy_model_configs", []):
                new_device_id = dev_id_map.get(cfg.get("device_id"))
                if new_device_id is None:
                    continue
                conn.execute(
                    "INSERT INTO energy_model_configs (device_id, model_type, instance_key, enabled, parameters) "
                    "VALUES (?,?,?,?,?)",
                    (
                        new_device_id,
                        cfg["model_type"],
                        cfg.get("instance_key") or "default",
                        cfg.get("enabled", 1),
                        cfg.get("parameters") or "{}",
                    ),
                )

            # No FK to device/object/location at all -- unlike everything
            # else restored above, nothing to remap, just re-insert verbatim.
            for ft in payload.get("functional_tests", []):
                conn.execute(
                    "INSERT INTO functional_tests (name, description, equipment_type, definition_json, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        ft["name"],
                        ft.get("description", ""),
                        ft["equipment_type"],
                        ft.get("definition_json") or "{}",
                        ft.get("created_at") or datetime.now(timezone.utc).isoformat(),
                        ft.get("updated_at") or datetime.now(timezone.utc).isoformat(),
                    ),
                )

            # Brick Core: restore entities AFTER locations/devices/objects
            # (their device_id/object_id/location_id FKs need the remaps
            # above to already exist), relationships LAST (they reference
            # entities by id, which only exist once this loop has run).
            #
            # semantic_key is RECOMPUTED from the newly-assigned ids, never
            # copied from the stored value -- a stored semantic_key embeds
            # the OLD surrogate device/object/location ids, which this same
            # load_project() call just reassigned above. local_slug never
            # references a surrogate id, so it survives verbatim and is
            # what makes recomputing the correct key possible.
            entity_id_map: dict[int, int] = {}
            for ent in payload.get("semantic_entities", []):
                old_entity_id = ent["id"]
                new_device_id = (
                    dev_id_map.get(ent.get("device_id"))
                    if ent.get("device_id") is not None else None
                )
                new_object_id = (
                    global_obj_id_map.get(ent.get("object_id"))
                    if ent.get("object_id") is not None else None
                )
                new_location_id = (
                    location_id_map.get(ent.get("location_id"))
                    if ent.get("location_id") is not None else None
                )
                computed_key = derive_semantic_key(
                    ent["entity_kind"], ent["brick_class"],
                    device_id=new_device_id, object_id=new_object_id, location_id=new_location_id,
                    local_slug=ent.get("local_slug"),
                )
                ent_cur = conn.execute(
                    "INSERT INTO semantic_entities "
                    "(name, local_slug, semantic_key, brick_class, entity_kind, device_id, object_id, location_id) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        ent["name"], ent.get("local_slug"), computed_key, ent["brick_class"], ent["entity_kind"],
                        new_device_id, new_object_id, new_location_id,
                    ),
                )
                entity_id_map[old_entity_id] = ent_cur.lastrowid

            for rel in payload.get("semantic_relationships", []):
                new_source_id = entity_id_map.get(rel["source_entity_id"])
                new_target_id = entity_id_map.get(rel["target_entity_id"])
                if new_source_id is None or new_target_id is None:
                    # Endpoint entity wasn't restorable -- skip this
                    # relationship rather than fail the whole import.
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO semantic_relationships (source_entity_id, predicate, target_entity_id) "
                    "VALUES (?,?,?)",
                    (new_source_id, rel["predicate"], new_target_id),
                )

            conn.commit()
            return {
                "source_type": payload.get("source_type", "simulated"),
                "connection_config": payload.get("connection_config"),
            }

    def import_project(self, name: str, description: str, data: dict) -> dict:
        with self._conn() as conn:
            device_count = len(data.get("devices", []))
            cur = conn.execute(
                "INSERT INTO profiles (name, description, device_count, data) VALUES (?,?,?,?)",
                (name, description, device_count, json.dumps(data)),
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles WHERE id=?",
                (cur.lastrowid,),
            ).fetchone())

    def delete_project(self, project_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM profiles WHERE id=?", (project_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Users ─────────────────────────────────────────────────────────────────

    def count_users(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def list_users(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, username, created_at, last_login_at FROM users ORDER BY created_at"
            )]

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(r) if r else None

    def get_user_by_username(self, username: str) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return dict(r) if r else None

    def create_user(self, username: str, password_hash: str) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?,?)",
                (username, password_hash),
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT id, username, created_at, last_login_at FROM users WHERE id=?",
                (cur.lastrowid,),
            ).fetchone())

    def update_user_password(self, user_id: int, password_hash: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id)
            )
            conn.commit()
            return cur.rowcount > 0

    def touch_last_login(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET last_login_at=datetime('now') WHERE id=?", (user_id,)
            )
            conn.commit()

    def delete_user(self, user_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Settings ──────────────────────────────────────────────────────────────

    def get_settings(self) -> dict:
        values = _default_settings()
        with self._conn() as conn:
            for row in conn.execute("SELECT key, value FROM settings"):
                key = row["key"]
                if key in SETTINGS_SCHEMA:
                    values[key] = SETTINGS_SCHEMA[key](row["value"])
        return values

    def save_settings(self, values: dict) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, str(v)) for k, v in values.items() if k in SETTINGS_SCHEMA],
            )
            conn.commit()


# ─── Auth ─────────────────────────────────────────────────────────────────────
# db/engine are set as module globals during app startup (see lifespan()) —
# by the time any request handler runs they're guaranteed to be assigned.

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: int, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": now,
        "exp": now + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


def user_from_token(token: str) -> Optional[dict]:
    """Decode a token and re-fetch the user row, so a deleted user's old
    token stops working immediately rather than staying valid until expiry."""
    payload = decode_access_token(token)
    if not payload:
        return None
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        return None
    user = db.get_user(user_id)
    if not user:
        return None
    return {"id": user["id"], "username": user["username"]}


def get_current_user(request: Request) -> dict:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = user_from_token(auth_header[7:].strip())
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


# Path prefixes reachable without a valid session — the login/setup flow
# itself, and the static admin SPA shell (the SPA then blocks on its own
# login screen until it has a token to call the real API with).
_PUBLIC_PATH_PREFIXES = ("/auth/", "/assets/")
_PUBLIC_PATHS = {"/", "/favicon.svg", "/bacnet-vendors.json"}


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PATH_PREFIXES)


# ─── Behaviors ────────────────────────────────────────────────────────────────

TICK_SECONDS = 5.0  # cadence of the engine tick loop; see tick_loop()/tick()
OBJECT_HISTORY_MAXLEN = 720  # per-object value-history ring buffer length; see tick()

@dataclass
class SimState:
    time_of_day: float = 12.0
    elapsed_seconds: float = 0.0


class Behavior(ABC):
    @abstractmethod
    def compute(self, state: SimState) -> Union[float, bool]:
        ...


class ConstantBehavior(Behavior):
    def __init__(self, params: dict):
        self.value = params.get("value", 0)

    def compute(self, state: SimState) -> Any:
        if isinstance(self.value, bool):
            return self.value
        return float(self.value)


class SineBehavior(Behavior):
    def __init__(self, params: dict):
        self.base = float(params.get("base", 20.0))
        self.amplitude = float(params.get("amplitude", 5.0))
        self.period_hours = float(params.get("period_hours", 24.0))
        self.phase_hours = float(params.get("phase_hours", 0.0))

    def compute(self, state: SimState) -> float:
        t = state.time_of_day + self.phase_hours
        return self.base + self.amplitude * math.sin(2 * math.pi * t / self.period_hours)


class NoiseBehavior(Behavior):
    def __init__(self, params: dict):
        self.base = float(params.get("base", 0.0))
        self.noise = float(params.get("noise", 1.0))

    def compute(self, state: SimState) -> float:
        return self.base + random.uniform(-self.noise, self.noise)


class RandomWalkBehavior(Behavior):
    def __init__(self, params: dict):
        self._value = float(params.get("value", 50.0))
        self.step = float(params.get("step", 1.0))
        self.min = float(params.get("min", 0.0))
        self.max = float(params.get("max", 100.0))

    def compute(self, state: SimState) -> float:
        self._value = max(self.min, min(self.max, self._value + random.uniform(-self.step, self.step)))
        return self._value


class ManualBehavior(Behavior):
    def __init__(self, params: dict, stored_value: Any = None):
        raw = params.get("value", stored_value)
        if raw is None:
            raw = 0
        if isinstance(raw, bool) or str(raw).lower() in ("true", "false"):
            self._value = raw if isinstance(raw, bool) else str(raw).lower() == "true"
        else:
            self._value = float(raw)

    def set(self, v: Any) -> None:
        self._value = v

    def compute(self, state: SimState) -> Any:
        return self._value


class DailyPatternBehavior(Behavior):
    """Returns different values based on time-of-day blocks (occupied/unoccupied
    scheduling). Not to be confused with a real BACnet Schedule object (see
    bacnet_schedule.py) — this is purely a value-simulation behavior, stored
    under the historical behavior name "schedule" for backward compatibility
    with existing projects/seed data, but renamed at the Python-class level
    now that real BACnet Schedule objects exist too."""

    @staticmethod
    def _parse_time(t: str) -> float:
        try:
            h, m = t.split(":")
            return int(h) + int(m) / 60.0
        except Exception:
            return 0.0

    def __init__(self, params: dict):
        self.default = float(params.get("default", 0))
        raw_blocks = params.get("blocks", [])
        self.blocks = sorted(
            [{"start": self._parse_time(b.get("start", "00:00")), "value": float(b.get("value", 0))}
             for b in raw_blocks if isinstance(b, dict)],
            key=lambda b: b["start"],
        )

    def compute(self, state: SimState) -> float:
        current = state.time_of_day % 24
        value = self.default
        for block in self.blocks:
            if current >= block["start"]:
                value = block["value"]
            else:
                break
        return value


class RampBehavior(Behavior):
    """Linearly ramps from one value to another over a fixed duration, optionally repeating."""

    def __init__(self, params: dict):
        self.from_val = float(params.get("from", 0))
        self.to_val = float(params.get("to", 100))
        self.duration_seconds = float(params.get("duration_minutes", 60)) * 60
        self.repeat = bool(params.get("repeat", True))

    def compute(self, state: SimState) -> float:
        if self.duration_seconds <= 0:
            return self.to_val
        if self.repeat:
            t = state.elapsed_seconds % self.duration_seconds
        else:
            t = min(state.elapsed_seconds, self.duration_seconds)
        frac = t / self.duration_seconds
        return self.from_val + (self.to_val - self.from_val) * frac


class FaultBehavior(Behavior):
    """Wraps a base behavior and randomly injects fault conditions (spike, stuck, offline)."""

    def __init__(self, params: dict):
        self._base_behavior_name = params.get("base_behavior", "constant")
        self._base_params = params.get("base_params", {"value": 0})
        self._inner: Optional[Behavior] = None
        self.fault_type = params.get("fault_type", "spike")
        self.fault_value = float(params.get("fault_value", 999))
        self.mtbf_minutes = float(params.get("mtbf_minutes", 60))
        self.fault_duration_seconds = float(params.get("fault_duration_seconds", 30))
        self._fault_active = False
        self._fault_end_elapsed: float = -1.0

    def compute(self, state: SimState) -> float:
        if self._inner is None:
            self._inner = make_behavior(self._base_behavior_name, json.dumps(self._base_params))

        if self._fault_active and state.elapsed_seconds > self._fault_end_elapsed:
            self._fault_active = False

        if not self._fault_active:
            # Ticks occur every TICK_SECONDS, not every second, so scale the
            # per-tick probability accordingly to make mtbf_minutes accurate.
            prob_per_tick = TICK_SECONDS / max(1.0, self.mtbf_minutes * 60.0)
            if random.random() < prob_per_tick:
                self._fault_active = True
                if self.fault_type == "spike":
                    self._fault_end_elapsed = state.elapsed_seconds
                else:
                    self._fault_end_elapsed = state.elapsed_seconds + self.fault_duration_seconds

        if self._fault_active:
            return 0.0 if self.fault_type == "offline" else self.fault_value

        return float(self._inner.compute(state))



def make_behavior(behavior: str, params_json: str, manual_value: Any = None) -> Behavior:
    try:
        params = json.loads(params_json) if params_json else {}
    except Exception:
        params = {}
    if behavior == "constant":
        return ConstantBehavior(params)
    if behavior == "sine":
        return SineBehavior(params)
    if behavior == "noise":
        return NoiseBehavior(params)
    if behavior == "random_walk":
        return RandomWalkBehavior(params)
    if behavior == "manual":
        return ManualBehavior(params, manual_value)
    if behavior == "schedule":
        return DailyPatternBehavior(params)
    if behavior == "ramp":
        return RampBehavior(params)
    if behavior == "fault":
        return FaultBehavior(params)
    return ConstantBehavior({"value": 0})

def get_device_log_entries(
    device_id: int,
    limit: int,
) -> list[dict]:
    entries = list(
        _device_logs.get(device_id, [])
    )
    return entries[-limit:]


def get_global_log_entries(
    limit: int,
) -> list[dict]:
    entries = list(_global_log)
    return entries[-limit:]

# ─── BACnet Application ───────────────────────────────────────────────────────

def _effective_can_receive_events(dev: dict) -> bool:
    """
    Whether this device can receive BACnet Event Notifications — real BACnet
    devices vary here (BIBBs like AE-N-I-B/AE-N-E-B vs. AE-N-A-only), and not
    every simulated device should behave as if it were a supervisory alarm
    sink. can_receive_event_notifications is an explicit per-device override
    (0/1); when unset (NULL), infer from equipment_type: devices tagged as a
    piece of physical HVAC/lighting equipment (AHU, VAV, Boiler, ...) are
    field-level devices and default to False, while untagged devices
    (workstations, BMS servers, gateways — this vocabulary has no equipment
    class for those) default to True.
    """
    override = dev.get("can_receive_event_notifications")
    if override is not None:
        return bool(override)
    return dev.get("equipment_type") is None


def _is_broadcast_address(destination) -> bool:
    # The *source* of a UDP packet is always the sender's own unicast return
    # address, whether the packet was sent broadcast or not — it never tells
    # you how the request was addressed. The destination does: bacpypes3's
    # IPv4 BVLL layer sets pduDestination to LocalBroadcast()/GlobalBroadcast()
    # only when the incoming LPDU was an Original-Broadcast-NPDU.
    if destination is None:
        return False
    return bool(getattr(destination, "is_localbroadcast", False) or getattr(destination, "is_globalbroadcast", False))


def _is_device_objid(objid) -> bool:
    if not isinstance(objid, tuple) or len(objid) != 2:
        return False
    t = objid[0]
    return t == "device" or (isinstance(t, int) and t == 8)


def _resolve_base_ip() -> str:
    iface = os.environ.get("BACPYPES_IFACE", "")
    if iface:
        ip = iface.split(":")[0].split("/")[0]
        # "0.0.0.0" here means "bind to all interfaces" — a valid bind
        # address but not a usable destination for self-directed traffic
        # (e.g. a device-type notification recipient resolving to our own
        # address). Fall through to hostname resolution for a real one.
        if ip and ip != "0.0.0.0":
            return ip
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        pass
    return "0.0.0.0"


# ─── Trend Log (BACnet wire exposure + ReadRange) ─────────────────────────────
# TrendLogObject has no bacpypes3.local implementation (unlike analog/binary/
# multi-state) — it's schema-only, so the "local" mixin here just makes it
# addressable/readable via the standard Object machinery. All the actual
# logging behavior (sampling, circular buffer) lives in SimEngine/Database;
# this class only carries the read-only, BACnet-wire-visible snapshot of it.
class LocalTrendLogObject(LocalObject, _TrendLogObjectSchema):
    pass


def _bacnet_datetime(ts: str) -> DateTime:
    """Parse a 'YYYY-MM-DD HH:MM:SS' SQLite timestamp into a BACnet DateTime."""
    d = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    return DateTime(
        date=Date((d.year - 1900, d.month, d.day, d.isoweekday())),
        time=Time((d.hour, d.minute, d.second, 0)),
    )


def _parse_trend_value(value_str: str, otype: str) -> Any:
    if otype in ("binary-input", "binary-output", "binary-value"):
        return value_str == "True"
    if otype in MULTISTATE_TYPES:
        return int(round(float(value_str)))
    return float(value_str)


def _build_log_record(record: dict, otype: str) -> LogRecord:
    value = _parse_trend_value(record["value"], otype)
    if otype in ("binary-input", "binary-output", "binary-value"):
        datum = LogRecordLogDatum(booleanValue=Boolean(value))
    elif otype in MULTISTATE_TYPES:
        datum = LogRecordLogDatum(unsignedValue=Unsigned(value))
    else:
        datum = LogRecordLogDatum(realValue=Real(value))
    return LogRecord(
        timestamp=_bacnet_datetime(record["ts"]),
        logDatum=datum,
        statusFlags=[0, 0, 0, 0],
    )


def _slice_trend_records(records: list[dict], range_: Any) -> tuple[list[dict], bool, bool]:
    """Apply a BACnet ReadRange Range choice (byPosition/bySequenceNumber/
    byTime) to an ascending-by-sequence-number list of records. Returns
    (selected, is_first, is_last) — is_first/is_last describe whether the
    selection includes the buffer's oldest/newest record (for resultFlags).
    No range at all (range_ is None) returns everything."""
    if not records:
        return [], True, True

    if range_ is None:
        return records, True, True

    def _apply(idx: int, count: int) -> list[dict]:
        if count >= 0:
            return records[idx: idx + count]
        end = idx + 1
        start = max(0, end + count)
        return records[start:end]

    selected: list[dict] = []
    if range_.byPosition is not None:
        idx = max(0, min(len(records) - 1, range_.byPosition.referenceIndex - 1))
        selected = _apply(idx, int(range_.byPosition.count))
    elif range_.bySequenceNumber is not None:
        ref_seq = range_.bySequenceNumber.referenceSequenceNumber
        idx = next((i for i, r in enumerate(records) if r["sequence_number"] >= ref_seq), len(records) - 1)
        selected = _apply(idx, int(range_.bySequenceNumber.count))
    elif range_.byTime is not None:
        ref_dt = range_.byTime.referenceTime
        ref_ts = f"{ref_dt.date[0] + 1900:04d}-{ref_dt.date[1]:02d}-{ref_dt.date[2]:02d} " \
                 f"{ref_dt.time[0]:02d}:{ref_dt.time[1]:02d}:{ref_dt.time[2]:02d}"
        idx = next((i for i, r in enumerate(records) if r["ts"] >= ref_ts), len(records) - 1)
        selected = _apply(idx, int(range_.byTime.count))
    else:
        selected = records

    is_first = bool(selected) and selected[0]["sequence_number"] == records[0]["sequence_number"]
    is_last = bool(selected) and selected[-1]["sequence_number"] == records[-1]["sequence_number"]
    return selected, is_first, is_last


@bacpypes_debugging
class SimApplication(Application):
    """Multi-device BACnet application — all virtual devices share one UDP socket."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._virtual_devices: dict[int, DeviceObject] = {}
        self._virtual_object_lists: dict[int, list] = {}
        self._sim_engine: Any = None  # set by SimEngine after construction
        self._own_ip: Optional[str] = None  # set by SimEngine.start(), for I-Am loopback filtering
        self._i_am_listeners: list[Callable[[Any], None]] = []

    def add_i_am_listener(self, listener: Callable[[Any], None]) -> None:
        """Register a callback invoked with every inbound I-Am APDU, after
        this class's own duplicate-device-ID handling. Used by
        src/bacnet/client/transport.py's targeted-discovery I-Am collector.
        Purely additive -- never replaces do_IAmRequest itself."""
        self._i_am_listeners.append(listener)

    def remove_i_am_listener(self, listener: Callable[[Any], None]) -> None:
        if listener in self._i_am_listeners:
            self._i_am_listeners.remove(listener)

    def get_object_id(self, objid):
        obj = super().get_object_id(objid)
        if obj is not None:
            return obj
        if _is_device_objid(objid):
            return self._virtual_devices.get(int(objid[1]))
        return None

    async def do_WhoIsRequest(self, apdu) -> None:
        low = apdu.deviceInstanceRangeLowLimit
        high = apdu.deviceInstanceRangeHighLimit
        source = apdu.pduSource
        is_unicast = not _is_broadcast_address(getattr(apdu, "pduDestination", None))

        metrics.requests_total += 1
        metrics.requests_by_service["WhoIs"] += 1
        metrics.discovery_total += 1
        if is_unicast:
            metrics.requests_unicast += 1
        else:
            metrics.requests_broadcast += 1
        now = time.time()
        src_str = str(source)
        metrics.clients_seen[src_str] = now
        metrics.recent_requests.append({
            "ts": now, "service": "WhoIs", "source": src_str,
            "broadcast": not is_unicast, "device": None, "ok": True,
        })

        saved = self.device_object
        try:
            for did, dev_obj in self._virtual_devices.items():
                in_range = (low is None and high is None) or (
                    low is not None and high is not None and low <= did <= high
                )
                if not in_range:
                    continue
                self.device_object = dev_obj
                if is_unicast:
                    self.i_am(address=source)
                else:
                    self.i_am()
        finally:
            self.device_object = saved

    async def do_IAmRequest(self, apdu) -> None:
        # Unconfirmed and previously unhandled — Application has no default
        # do_IAmRequest, so incoming I-Am from other devices on the network
        # was silently dropped before this override existed (indication()
        # only raises UnrecognizedService for confirmed requests with no
        # handler; unconfirmed ones with no handler just return, app.py
        # ~878-881). This hook is purely additive — no existing behavior
        # changes by adding it.
        try:
            instance = int(apdu.iAmDeviceIdentifier[1])
        except Exception:
            return
        source = apdu.pduSource
        src_str = str(source)
        now = time.time()

        metrics.discovery_total += 1
        metrics.clients_seen[src_str] = now
        is_new = instance not in metrics.iam_seen
        metrics.iam_seen[instance] = now
        if is_new:
            metrics.new_devices_timeline.append({"ts": now, "device_instance": instance, "source": src_str})

        # Flag a real collision: someone other than us claiming one of our
        # own virtual devices' instance numbers. Loopback of our own I_am
        # broadcasts (if the OS reflects them back) is filtered by IP.
        own_ip = (self._own_ip or "").split(":")[0]
        source_ip = src_str.split(":")[0]
        if instance in self._virtual_devices and source_ip and source_ip != own_ip:
            metrics.duplicate_id_events.append({
                "ts": now, "device_instance": instance, "source": src_str,
            })

        # Restore BACpypes3's own I-Am processing (WhoIsIAmServices's
        # _who_is_futures matching, used by Application.who_is() callers) --
        # previously unreachable since this override never chained to it.
        try:
            await super().do_IAmRequest(apdu)
        except Exception:
            log.exception("super().do_IAmRequest failed for %r", apdu)

        # Notify any registered temporary listeners (targeted-discovery I-Am
        # collectors, see src/bacnet/client/transport.py) -- purely additive.
        for listener in list(self._i_am_listeners):
            try:
                listener(apdu)
            except Exception:
                log.exception("I-Am listener failed for %r", apdu)

    def _log_received_event_notification(self, apdu) -> None:
        """Parses a real, wire-received APDU and defers to the shared logger.
        Kept only for genuine external (address-type) recipients — see
        _log_event_notification_received's docstring for why device-type
        recipients no longer go through this path at all."""
        try:
            sender_instance = int(apdu.initiatingDeviceIdentifier[1])
        except Exception:
            sender_instance = None
        message_text = str(getattr(apdu, "messageText", "") or "")
        from_state = str(getattr(apdu, "fromState", "?"))
        to_state = str(getattr(apdu, "toState", "?"))
        _log_event_notification_received(sender_instance, None, message_text, from_state, to_state)

    async def do_UnconfirmedEventNotificationRequest(self, apdu) -> None:
        # Unconfirmed and previously unhandled — same situation do_IAmRequest
        # documents above: Application has no default handler, so this was
        # silently dropped before this override existed. Purely additive.
        #
        # Exceptions here would otherwise be swallowed by Application.
        # indication()'s broad except block, which logs via bacpypes3's own
        # debug channel (off by default) rather than the standard logging
        # module — so a bug in this handler would be silent even with normal
        # log levels turned up. Log explicitly instead of trusting that path.
        try:
            self._log_received_event_notification(apdu)
        except Exception:
            log.exception("do_UnconfirmedEventNotificationRequest failed on %r", apdu)

    async def do_ConfirmedEventNotificationRequest(self, apdu) -> None:
        # Same as above, but must ack — Application has no default handler
        # for this service either, so without this the *sender's* confirmed
        # wait (alarms.send_event_notification's asyncio.wait_for) would
        # error out instead of completing.
        try:
            self._log_received_event_notification(apdu)
        except Exception:
            log.exception("do_ConfirmedEventNotificationRequest failed on %r", apdu)
        await self.response(SimpleAckPDU(context=apdu))

    async def do_ReadPropertyRequest(self, apdu) -> None:
        objid = apdu.objectIdentifier

        # Stamp pending context here (cheap: dict write + counter increments,
        # no scans/allocation) — SimApplication.response() pops this to
        # attribute latency + success/error once the outcome is known,
        # whether this method answers directly or delegates to super().
        pending_key = (str(apdu.pduSource), apdu.apduInvokeID)
        metrics.pending[pending_key] = {
            "service": "ReadProperty", "objid": str(objid), "started": time.monotonic(),
        }
        metrics.requests_total += 1
        metrics.requests_by_service["ReadProperty"] += 1
        metrics.reads_total += 1
        metrics.requests_unicast += 1  # ReadProperty is always confirmed/unicast by protocol
        metrics.clients_seen[str(apdu.pduSource)] = time.time()

        if _is_device_objid(objid):
            did = int(objid[1])
            virtual = self._virtual_devices.get(did)
            if virtual:
                prop = apdu.propertyIdentifier
                try:
                    prop_code = int(prop)
                except Exception:
                    prop_code = getattr(prop, "value", prop)
                if prop_code == 76:
                    cls = DeviceObject._elements.get("objectList")
                    raw = self._virtual_object_lists.get(did, [])
                    value = cls([ObjectIdentifier(o) for o in raw])
                elif prop_code == 77:
                    value = virtual.objectName
                elif prop_code == 121:
                    value = virtual.vendorName
                elif prop_code == 70:
                    value = virtual.modelName
                elif prop_code == 28:
                    value = virtual.description
                else:
                    await super().do_ReadPropertyRequest(apdu)
                    return
                resp = ReadPropertyACK(
                    objectIdentifier=objid,
                    propertyIdentifier=prop,
                    propertyArrayIndex=apdu.propertyArrayIndex,
                    propertyValue=value,
                    context=apdu,
                )
                await self.response(resp)
                return
        await super().do_ReadPropertyRequest(apdu)

    async def do_WritePropertyRequest(self, apdu) -> None:
        pending_key = (str(apdu.pduSource), apdu.apduInvokeID)
        metrics.pending[pending_key] = {
            "service": "WriteProperty", "objid": str(apdu.objectIdentifier), "started": time.monotonic(),
        }
        metrics.requests_total += 1
        metrics.requests_by_service["WriteProperty"] += 1
        metrics.writes_total += 1
        metrics.requests_unicast += 1  # WriteProperty is always confirmed/unicast by protocol
        metrics.clients_seen[str(apdu.pduSource)] = time.time()

        if self._sim_engine is None:
            await super().do_WritePropertyRequest(apdu)
            return

        # Only intercept present-value writes (property identifier 85)
        prop = apdu.propertyIdentifier
        try:
            prop_code = int(prop)
        except Exception:
            prop_code = getattr(prop, "value", None)
        if prop_code != 85:
            await super().do_WritePropertyRequest(apdu)
            return

        # Find the bacpypes3 object
        obj = self.get_object_id(apdu.objectIdentifier)
        if obj is None:
            await super().do_WritePropertyRequest(apdu)
            return

        # Resolve to DB id by object identity
        db_id = self._sim_engine.db_id_for_bacnet_object(obj)
        if db_id is None:
            await super().do_WritePropertyRequest(apdu)
            return

        obj_row = await asyncio.to_thread(self._sim_engine.db.get_object, db_id)
        if not obj_row:
            await super().do_WritePropertyRequest(apdu)
            return

        otype = obj_row["object_type"]
        WRITABLE = {
            "analog-output", "analog-value", "binary-output", "binary-value",
            "multi-state-output", "multi-state-value",
        }
        if otype not in WRITABLE:
            await super().do_WritePropertyRequest(apdu)
            return

        # Extract the written value
        try:
            if "analog" in otype:
                value: Any = float(apdu.propertyValue.cast_out(Real))
            elif otype in MULTISTATE_TYPES:
                value = int(apdu.propertyValue.cast_out(Unsigned))
            else:
                bpv = apdu.propertyValue.cast_out(BinaryPV)
                value = (str(bpv) == "active")
        except Exception as e:
            log.warning("WriteProperty decode error on %s: %s", apdu.objectIdentifier, e)
            metrics.errors_by_type["error:property.invalidDataType"] += 1
            await super().do_WritePropertyRequest(apdu)
            return

        # Persist to DB and update in-memory sim
        await self._sim_engine.write_object(db_id, value, source=str(apdu.pduSource))
        await self.response(SimpleAckPDU(context=apdu))

    async def do_ReadRangeRequest(self, apdu) -> None:
        """Serve BACnet ReadRange for Trend Log objects' Log_Buffer property
        (by position, by sequence number, by time, or the whole buffer if
        no range is given) — see _slice_trend_records(). Every other
        object/property falls through to bacpypes3's own handling, which is
        unimplemented (raises NotImplementedError), same as before this
        override existed."""
        pending_key = (str(apdu.pduSource), apdu.apduInvokeID)
        metrics.pending[pending_key] = {
            "service": "ReadRange", "objid": str(apdu.objectIdentifier), "started": time.monotonic(),
        }
        metrics.requests_total += 1
        metrics.requests_by_service["ReadRange"] += 1
        metrics.reads_total += 1
        metrics.requests_unicast += 1
        metrics.clients_seen[str(apdu.pduSource)] = time.time()

        if self._sim_engine is None:
            await super().do_ReadRangeRequest(apdu)
            return

        objid = apdu.objectIdentifier
        tl_id = None
        for tlid, bobj in self._sim_engine._trend_log_objects.items():
            if bobj.objectIdentifier == objid:
                tl_id = tlid
                break
        if tl_id is None:
            await super().do_ReadRangeRequest(apdu)
            return

        prop = apdu.propertyIdentifier
        try:
            prop_code = int(prop)
        except Exception:
            prop_code = getattr(prop, "value", None)
        if prop_code != 131:  # log-buffer
            raise ExecutionError(errorClass="property", errorCode="unknownProperty")

        tl_cfg = await asyncio.to_thread(self._sim_engine.db.get_trend_log, tl_id)
        if not tl_cfg or not tl_cfg["enabled"]:
            raise ExecutionError(errorClass="object", errorCode="unknownObject")

        monitored = await asyncio.to_thread(self._sim_engine.db.get_object, tl_cfg["monitored_object_id"])
        otype = monitored["object_type"] if monitored else "analog-input"

        all_records = await asyncio.to_thread(
            self._sim_engine.db.get_trend_log_records, tl_id, limit=tl_cfg["buffer_size"], order="asc"
        )
        try:
            selected, is_first, is_last = _slice_trend_records(all_records, apdu.range)
        except Exception as e:
            raise ExecutionError(errorClass="property", errorCode="invalidArrayIndex") from e

        log_records = [_build_log_record(r, otype) for r in selected]
        item_data = BACnetAny(SequenceOf(LogRecord)(log_records))

        resp = ReadRangeACK(
            objectIdentifier=objid,
            propertyIdentifier=prop,
            propertyArrayIndex=apdu.propertyArrayIndex,
            resultFlags=[is_first, is_last, not is_last],
            itemCount=len(log_records),
            itemData=item_data,
            firstSequenceNumber=selected[0]["sequence_number"] if selected else 1,
            context=apdu,
        )
        await self.response(resp)

    async def response(self, apdu) -> None:  # type: ignore[override]
        # Every outcome — success, reject, abort, or protocol error — passes
        # through here before being sent on the wire, regardless of whether
        # it originated in our own do_*Request code above or fell through to
        # bacpypes3's own internal object/property validation inside
        # super().do_*Request(). See Application.indication() (bacpypes3
        # app.py): it catches RejectException/AbortException/ExecutionError
        # from the do_*Request call and turns each into exactly the PDU
        # types checked below, always via self.response(...) — so this is
        # the one stable place to observe every request's real outcome
        # without touching indication()'s own dispatch logic.
        pending_key = (str(apdu.pduDestination), apdu.apduInvokeID) if getattr(apdu, "pduDestination", None) else None
        ctx = metrics.pending.pop(pending_key, None) if pending_key else None

        now = time.time()
        latency_ms = (time.monotonic() - ctx["started"]) * 1000 if ctx else None
        if latency_ms is not None:
            metrics.latencies_ms.append(latency_ms)

        objid_key = ctx["objid"] if ctx else None
        service = ctx["service"] if ctx else None
        ok = True
        error_label = None

        if isinstance(apdu, RejectPDU):
            ok = False
            error_label = f"reject:{apdu.apduAbortRejectReason}"
        elif isinstance(apdu, AbortPDU):
            ok = False
            error_label = f"abort:{apdu.apduAbortRejectReason}"
        elif isinstance(apdu, ErrorPDU):
            ok = False
            err_class = getattr(apdu, "errorClass", "unknown")
            err_code = getattr(apdu, "errorCode", "unknown")
            error_label = f"error:{err_class}.{err_code}"

        if error_label:
            metrics.errors_by_type[error_label] += 1
            metrics.recent_errors.append({
                "ts": now, "type": error_label, "service": service, "object": objid_key,
            })
        elif objid_key and service == "ReadProperty":
            metrics.object_reads[objid_key] += 1
        elif objid_key and service == "WriteProperty":
            metrics.object_writes[objid_key] += 1

        if service:
            metrics.recent_requests.append({
                "ts": now, "service": service, "object": objid_key, "ok": ok,
                "latency_ms": latency_ms,
            })

        await super().response(apdu)


def _apply_reliability(bacnet_obj: Any, reliability_str: str) -> None:
    """Force a specific Reliability value (GH #16) on a constructed analog/
    binary/multi-state object, for testing client-side fault handling. Also
    sets the statusFlags.fault bit, matching what real BACnet clients
    actually key off of — Reliability alone is often not surfaced in a
    client's UI, but the fault status bit almost always is."""
    try:
        reliability = Reliability(reliability_str)
    except Exception:
        reliability = Reliability("no-fault-detected")
    bacnet_obj.reliability = reliability
    fault = 0 if str(reliability) == "no-fault-detected" else 1
    bacnet_obj.statusFlags = StatusFlags([0, fault, 0, 0])


def _apply_polarity(bacnet_obj: Any, polarity_str: str) -> None:
    """Set Polarity (GH #19) on a constructed Binary Input/Output object.
    Binary Value has no polarity property in bacpypes3's schema (matching
    real BACnet spec — only physically-wired points have one), so this is
    only ever called for binary-input/binary-output."""
    try:
        bacnet_obj.polarity = Polarity(polarity_str)
    except Exception:
        bacnet_obj.polarity = Polarity("normal")


def normalize_present_value(object_type: str, val: Any) -> Any:
    """Canonicalizes a Behavior.compute() result before it's stored/served/
    logged anywhere (the tick loop's SimEngine._objects processing calls
    this immediately after compute(), before _update_value(), before
    _prev_values[obj_id] = val, before the /sim/state snapshot, trend logs,
    or alarm/enrollment evaluation see it).

    Behavior.compute() is typed float|bool and different Behavior
    subclasses disagree on which they return for the same logical binary
    point -- ManualBehavior keeps bool only for literal JSON true/false
    (numeric input is coerced to float), while DailyPatternBehavior and
    FaultBehavior always return float, even when wrapping a boolean
    ConstantBehavior. Present_Value for binary-input/output/value is
    BACnetBinaryPV (an ENUMERATED{inactive,active}, not a float or a
    general integer -- see ASHRAE 135's object type tables), so the
    simulator's canonical internal/API representation is a plain bool
    (True=active). Normalizing once, here, is what lets every downstream
    consumer (API JSON, the /sim/state snapshot, the Vue UI) treat
    "is this point binary" as the single source of truth for how to
    display/interpret a value, instead of branching on whatever Python/JS
    runtime type a particular Behavior happened to produce."""
    if object_type in BINARY_TYPES:
        return bool(val)
    return val


# ─── Sim Engine ───────────────────────────────────────────────────────────────

def _force_close_bacnet_transports(app: Any) -> None:
    """
    Defensive cleanup for what app.close() can't handle: if a link-layer UDP
    endpoint's creation task hadn't finished binding yet when close() ran (it
    can raise AttributeError trying to close a transport that was never set
    -- see bacpypes3's ipv4/__init__.py IPv4DatagramServer.close()), close()
    bails out without ever touching that task. BACpypes3's own endpoint
    setup (retrying_create_datagram_endpoint) then keeps it retrying forever
    in the background, fully detached from this now-abandoned Application --
    if it eventually succeeds, it binds a real socket nothing else will ever
    release, permanently occupying this simulator's own BACnet port even
    though self.app has already been reset to None. Reaches into each link
    layer's server and either cancels the task (still pending) or closes the
    transport it already produced (finished just after close() gave up).
    """
    for link_layer in getattr(app, "link_layers", {}).values():
        server = getattr(link_layer, "server", None)
        for task in getattr(server, "_transport_tasks", None) or []:
            if not task.done():
                task.cancel()
                continue
            try:
                transport, _protocol = task.result()
            except Exception:
                continue
            try:
                transport.close()
            except Exception:
                pass


class SimEngine:
    """Manages the running BACnet application and the simulation tick loop."""

    def __init__(self, db: Database):
        self.db = db
        self.state = SimState()
        self.app: Optional[SimApplication] = None
        self.network_port: Optional[NetworkPortObject] = None
        # object DB id → (bacpypes3 object, Behavior)
        self._objects: dict[int, tuple[Any, Behavior]] = {}
        # device instance → slot index (for physical instance offset)
        self._device_slots: dict[int, int] = {}
        # device instance → device row, rebuilt on every start()/reload() from
        # the same DB fetch as _device_slots -- lets packet-capture streaming
        # resolve device identity in O(1) without a per-packet DB query.
        self._devices_by_instance: dict[int, dict] = {}
        self._reload_event = asyncio.Event()
        # Guards reload() against overlapping runs. Every device/object CRUD route
        # fires reload() via asyncio.create_task() (fire-and-forget), and start()
        # does hundreds of awaited DB calls for a large project — plenty of time
        # for a second reload() to start before the first finishes. Without this
        # lock, two reloads race on self.app/_objects/_device_slots and the loser
        # can leave a stale DeviceObject (e.g. an old instance number for a device
        # that was since renumbered) registered in the winner's _virtual_devices,
        # where it keeps answering Who-Is broadcasts indefinitely even though the
        # DB (source of truth) has already moved on.
        self._reload_lock = asyncio.Lock()
        self._current_values: dict = {}  # for API
        # object DB id → last logged value (for change detection)
        self._prev_values: dict[int, Any] = {}  # kept for history only
        # object DB id → rolling 1-hour history (720 ticks × 5 s), never persisted
        self._history: dict[int, deque] = {}
        # object DB id → intrinsic-reporting runtime state (not persisted — a
        # restart starts every object back at "normal", an acceptable
        # simulator simplification; see alarms.py)
        self._alarm_runtime: dict[int, alarms.AlarmRuntime] = {}
        # event_enrollment DB id → algorithmic-reporting runtime state (same
        # not-persisted simplification as _alarm_runtime above)
        self._enrollment_runtime: dict[int, alarms.AlarmRuntime] = {}
        # trend_log DB id → last value actually recorded, for COV-triggered
        # logging (not persisted — same simplification as the above)
        self._trend_log_last_value: dict[int, Any] = {}
        # trend_log DB id → live bacpypes3 TrendLogObject, once exposed on
        # the BACnet wire (see _create_trend_log_objects())
        self._trend_log_objects: dict[int, Any] = {}
        # schedule DB id → live bacnet_schedule.LocalScheduleObject. Unlike
        # the above, these don't need a runtime cache in tick() — bacpypes3's
        # ScheduleObject self-schedules its own next transition.
        self._schedule_objects: dict[int, Any] = {}
        # calendar DB id → live bacnet_calendar.LocalCalendarObject (GH #18).
        # presentValue has no self-scheduling hook like ScheduleObject does,
        # so tick() refreshes it directly — see _refresh_calendar_present_values.
        self._calendar_objects: dict[int, Any] = {}
        # simulation clock: whether tick() advances time / recomputes values.
        # Independent of self.app (the BACnet stack) — objects stay reachable
        # and hold their last value while paused/stopped.
        # One of "running" / "paused" / "stopped" — "paused" freezes values in
        # place, "stopped" additionally rewinds elapsed time/history to zero.
        # Starts running on process boot (historical default, pre-dates these
        # controls); loading/switching a project explicitly stops it instead
        # (see load_project()) so a freshly loaded project doesn't silently
        # start ticking.
        self.clock_state: str = "running"

    def pause(self) -> None:
        self.clock_state = "paused"

    def resume(self) -> None:
        self.clock_state = "running"

    def reset(self) -> None:
        """Stop the clock and rewind simulated time/history back to the start."""
        self.clock_state = "stopped"
        self.state.elapsed_seconds = 0.0
        self.state.time_of_day = 12.0
        self._history.clear()
        for _, behavior in self._objects.values():
            if isinstance(behavior, FaultBehavior):
                behavior._fault_active = False
                behavior._fault_end_elapsed = -1.0

    @staticmethod
    def _simulated_enabled_devices(devices: list[dict]) -> list[dict]:
        """The ONLY devices SimEngine ever turns into live virtual BACnet
        DeviceObjects. External BACnet devices (source_type ==
        'external-bacnet') must NEVER appear here -- they belong to a real
        physical device the simulator only reads from, never impersonates.
        Every downstream effect (virtual-device registration, object/
        trend-log/schedule/calendar creation, and the I-Am announcement
        loop) cascades from this one list -- see start(). A device dict
        with no source_type key at all (pre-migration shape) defaults to
        'simulated' for backward compatibility."""
        return [
            d for d in devices
            if d["enabled"] and d.get("source_type", "simulated") == "simulated"
        ]

    async def start(self) -> None:
        devices = await asyncio.to_thread(self.db.get_devices)
        self._devices_by_instance = {d["device_instance"]: d for d in devices}
        enabled = self._simulated_enabled_devices(devices)
        if not enabled:
            log.info("No enabled devices — BACnet stack idle")
            self.app = None
            return

        base_ip = _resolve_base_ip()

        install_bacpypes_packet_capture_hooks(
            local_ip=base_ip,
            local_port=BACNET_PORT,
            get_clock_state=lambda: self.clock_state,
        )
        
        loop = asyncio.get_running_loop()
        orig = loop.get_exception_handler()

        def _exc_handler(loop, ctx):
            exc = ctx.get("exception")
            if isinstance(exc, RuntimeError) and str(exc) == "no broadcast":
                return
            if orig:
                orig(loop, ctx)
            else:
                loop.default_exception_handler(ctx)

        loop.set_exception_handler(_exc_handler)

        primary = enabled[0]
        bind_addr = f"{base_ip}:{BACNET_PORT}"

        primary_dev_obj = self._make_device_object(primary)
        self.network_port = NetworkPortObject(
            bind_addr,
            objectIdentifier=("network-port", 1),
            objectName="NetworkPort-1",
        )

        self.app = SimApplication.from_object_list([primary_dev_obj, self.network_port])
        self.app._sim_engine = self
        self.app._own_ip = base_ip  # for filtering our own I-Am loopback in duplicate-ID detection
        await asyncio.sleep(0.3)

        self.app._virtual_devices[primary["device_instance"]] = primary_dev_obj
        self._device_slots = {d["device_instance"]: i for i, d in enumerate(enabled)}

        log.info("BACnet socket bound to %s", bind_addr)

        for idx, dev in enumerate(enabled):
            slot = idx
            if idx == 0:
                dev_obj = primary_dev_obj
            else:
                dev_obj = self._make_device_object(dev)
                self.app._virtual_devices[dev["device_instance"]] = dev_obj

            objects = await asyncio.to_thread(self.db.get_objects, dev["id"])
            bacnet_ids = [dev_obj.objectIdentifier]
            if idx == 0:
                bacnet_ids.append(self.network_port.objectIdentifier)

            for obj_row in objects:
                if not obj_row["enabled"]:
                    continue
                bacnet_obj, behavior = self._create_object(obj_row, slot, dev["name"])
                try:
                    self.app.add_object(bacnet_obj)
                except RuntimeError:
                    log.exception(
                        "Failed to add object %r on device %r (name/identifier collision?) — skipping",
                        obj_row["name"], dev["name"],
                    )
                    continue
                self._objects[obj_row["id"]] = (bacnet_obj, behavior)
                bacnet_ids.append(bacnet_obj.objectIdentifier)

            # Calendars (GH #18) must be built before Schedules below, since a
            # Schedule's exceptionSchedule may reference one by name.
            calendars = await asyncio.to_thread(self.db.get_calendars, dev["id"])
            calendar_phys_by_name: dict[str, int] = {}
            for cal_idx, cal in enumerate(calendars):
                if not cal["enabled"]:
                    continue
                try:
                    entries = json.loads(cal["date_list"] or "[]")
                    phys = slot * 1000 + cal_idx + 1
                    cal_bacnet_obj = bacnet_calendar.LocalCalendarObject(
                        objectIdentifier=("calendar", phys),
                        objectName=f"{dev['name']}.{cal['name']}",
                        description=cal.get("description", ""),
                        presentValue=Boolean(bacnet_calendar.today_in_date_list(entries)),
                        dateList=bacnet_calendar.build_date_list(entries),
                    )
                    self.app.add_object(cal_bacnet_obj)
                    self._calendar_objects[cal["id"]] = cal_bacnet_obj
                    calendar_phys_by_name[cal["name"]] = phys
                    bacnet_ids.append(cal_bacnet_obj.objectIdentifier)
                except Exception:
                    log.exception("Failed to build calendar %r on device %r — skipping", cal["name"], dev["name"])

            trend_logs = await asyncio.to_thread(self.db.get_trend_logs, dev["id"])
            for tl_idx, tl in enumerate(trend_logs):
                monitored = self._objects.get(tl["monitored_object_id"])
                if monitored is None:
                    continue  # monitored object disabled/missing — skip exposing on the wire
                monitored_objid = monitored[0].objectIdentifier
                records = await asyncio.to_thread(
                    self.db.get_trend_log_records, tl["id"], limit=tl["buffer_size"], order="asc"
                )
                log_buffer = SequenceOf(LogRecord)(
                    [_build_log_record(r, monitored_objid[0]) for r in records]
                )
                try:
                    tl_bacnet_obj = LocalTrendLogObject(
                        objectIdentifier=("trend-log", slot * 1000 + tl_idx + 1),
                        objectName=f"{dev['name']}.{tl['name']}",
                        description=tl.get("description", ""),
                        enable=Boolean(bool(tl["enabled"])),
                        stopWhenFull=Boolean(bool(tl["stop_when_full"])),
                        bufferSize=Unsigned(tl["buffer_size"]),
                        logBuffer=log_buffer,
                        recordCount=Unsigned(tl["record_count"]),
                        totalRecordCount=Unsigned(tl["total_record_count"]),
                        loggingType=LoggingType(tl["logging_type"]),
                        statusFlags=[0, 0, 0, 0],
                        reliability=Reliability("no-fault-detected"),
                        logDeviceObjectProperty=DeviceObjectPropertyReference(
                            objectIdentifier=monitored_objid,
                            propertyIdentifier="present-value",
                        ),
                        logInterval=Unsigned(tl.get("log_interval") or 0),
                    )
                    self.app.add_object(tl_bacnet_obj)
                    self._trend_log_objects[tl["id"]] = tl_bacnet_obj
                    bacnet_ids.append(tl_bacnet_obj.objectIdentifier)
                except Exception:
                    log.exception("Failed to build trend log %r on device %r — skipping", tl["name"], dev["name"])

            schedules = await asyncio.to_thread(self.db.get_schedules, dev["id"])
            for sched_idx, sched in enumerate(schedules):
                if not sched["enabled"]:
                    continue  # same convention as disabled regular objects: not built at all
                targets = await asyncio.to_thread(self.db.get_schedule_targets, sched["id"])
                obj_prop_refs = []
                for t in targets:
                    target_entry = self._objects.get(t["object_id"])
                    if target_entry is None:
                        continue  # target object disabled/missing — skip that reference
                    obj_prop_refs.append(DeviceObjectPropertyReference(
                        objectIdentifier=target_entry[0].objectIdentifier,
                        propertyIdentifier=t.get("property_identifier", "present-value"),
                    ))
                try:
                    value_type = sched.get("value_type", "real")
                    default_raw = json.loads(sched["schedule_default"] or "0")
                    sched_bacnet_obj = bacnet_schedule.LocalScheduleObject(
                        objectIdentifier=("schedule", slot * 1000 + sched_idx + 1),
                        objectName=f"{dev['name']}.{sched['name']}",
                        description=sched.get("description", ""),
                        presentValue=bacnet_schedule.default_value(value_type, default_raw),
                        effectivePeriod=bacnet_schedule.build_effective_period(
                            sched.get("effective_start"), sched.get("effective_end")
                        ),
                        weeklySchedule=bacnet_schedule.build_weekly_schedule(
                            json.loads(sched["weekly_schedule"] or "{}"), value_type
                        ),
                        exceptionSchedule=bacnet_schedule.build_exception_schedule(
                            json.loads(sched["exception_schedule"] or "[]"), value_type, calendar_phys_by_name
                        ),
                        scheduleDefault=bacnet_schedule.default_value(value_type, default_raw),
                        listOfObjectPropertyReferences=SequenceOf(DeviceObjectPropertyReference)(obj_prop_refs),
                        priorityForWriting=Unsigned(sched["priority_for_writing"]),
                    )
                    sched_bacnet_obj._value_type = value_type
                    self.app.add_object(sched_bacnet_obj)
                    self._schedule_objects[sched["id"]] = sched_bacnet_obj
                    bacnet_ids.append(sched_bacnet_obj.objectIdentifier)
                except Exception:
                    log.exception("Failed to build schedule %r on device %r — skipping", sched["name"], dev["name"])

            dev_obj.objectList = bacnet_ids
            self.app._virtual_object_lists[dev["device_instance"]] = bacnet_ids
            log.info("Device %d (%s): %d objects", dev["device_instance"], dev["name"], len(objects))

        # Announce all devices
        saved = self.app.device_object
        try:
            for dev_obj in self.app._virtual_devices.values():
                self.app.device_object = dev_obj
                self.app.i_am()
        finally:
            self.app.device_object = saved

    def _make_device_object(self, dev: dict) -> DeviceObject:
        try:
            segmentation = Segmentation(dev.get("segmentation_supported") or "segmented-both")
        except Exception:
            segmentation = Segmentation("segmented-both")
        return DeviceObject(
            objectIdentifier=f"device,{dev['device_instance']}",
            objectName=dev["name"],
            vendorIdentifier=999,
            description=dev.get("description", ""),
            modelName=dev.get("model_name", "BACnet Simulator"),
            vendorName=dev.get("vendor_name", "Iotistica"),
            applicationSoftwareVersion="3.0",
            location=dev["name"],
            firmwareRevision=dev.get("firmware_revision") or "N/A",
            protocolRevision=Unsigned(dev.get("protocol_revision") or 22),
            maxApduLengthAccepted=Unsigned(dev.get("max_apdu_length_accepted") or 1024),
            segmentationSupported=segmentation,
        )

    def _create_object(self, obj_row: dict, slot: int, device_name: str = "") -> tuple[Any, Behavior]:


        phys = slot * 1000 + obj_row["object_instance"]
        behavior = make_behavior(
            obj_row["behavior"],
            obj_row["behavior_params"],
            obj_row.get("manual_value"),
        )
        val = behavior.compute(self.state)
        otype = obj_row["object_type"]
        # BACnet requires globally unique object names within a single application,
        # even across virtual devices — prefix with device name to guarantee uniqueness.
        obj_name = f"{device_name}.{obj_row['name']}" if device_name else obj_row["name"]

        _ANALOG_CLS = {
            "analog-input":  AnalogInputObject,
            "analog-output": AnalogOutputObject,
            "analog-value":  AnalogValueObject,
        }
        _BINARY_CLS = {
            "binary-input":  BinaryInputObject,
            "binary-output": BinaryOutputObject,
            "binary-value":  BinaryValueObject,
        }
        if otype in _ANALOG_CLS:
            units_str = obj_row.get("units") or "no-units"
            try:
                units = EngineeringUnits(units_str)
            except Exception:
                units = EngineeringUnits("no-units")
            bacnet_obj = _ANALOG_CLS[otype](
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                presentValue=Real(float(val)),
                units=units,
            )
            _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
        elif otype == "binary-output":
            # Pass presentValue= in the constructor so Commandable.__init__ can set
            # relinquishDefault from it (line 87 in bacpypes3/local/cmd.py).
            # _Object.__init__ sets it directly via super().__setattr__, bypassing
            # Commandable.__setattr__, so priorityArray is not accessed before it exists.
            # The tick loop later writes via Commandable.__setattr__ → priorityArray[15]
            # → recalculating() which keeps presentValue up-to-date for ReadProperty.
            active = bool(val) if not isinstance(val, bool) else val
            bacnet_obj = BinaryOutputObject(
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                presentValue=BinaryPV("active" if active else "inactive"),
            )
            _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
            _apply_polarity(bacnet_obj, obj_row.get("polarity") or "normal")
        elif otype in MULTISTATE_TYPES:
            _MULTISTATE_CLS = {
                "multi-state-input":  MultiStateInputObject,
                "multi-state-output": MultiStateOutputObject,
                "multi-state-value":  MultiStateValueObject,
            }
            n_states = max(1, int(obj_row.get("number_of_states") or 2))
            state = max(1, min(n_states, round(float(val))))
            # Same reasoning as binary-output above — multi-state-output is
            # Commandable too, so presentValue must be passed at construction.
            bacnet_obj = _MULTISTATE_CLS[otype](
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                presentValue=Unsigned(state),
                numberOfStates=Unsigned(n_states),
            )
            _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
        else:
            active = bool(val) if not isinstance(val, bool) else val
            cls = _BINARY_CLS.get(otype, BinaryInputObject)
            bacnet_obj = cls(
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                presentValue=BinaryPV("active" if active else "inactive"),
            )
            _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
            if otype == "binary-input":
                _apply_polarity(bacnet_obj, obj_row.get("polarity") or "normal")
        return bacnet_obj, behavior
    
    def get_object_value(self, object_id: int):
        """Live per-object value, refreshed every tick (self._prev_values,
        set at the top of the tick loop -- see the `self._prev_values[obj_id]
        = val` line right before each object's snapshot entry is built).

        NOTE ON HISTORY: this used to read self._current_values instead,
        which looks plausible (the name suggests "current value") but is
        WRONG -- _current_values is only ever assigned wholesale as either
        `{}` or `{"devices": [...], "tick": ...}` (the /sim/state snapshot
        cache for the API/WebSocket, see get_state()), never as a per-object
        {object_id: value} dict. `_current_values.get(object_id)` therefore
        always returned None once the sim had ticked at least once.

        This class used to ALSO define a second get_object_value() later in
        the class body that correctly read _prev_values -- Python's last-
        definition-wins for duplicate method names meant that second
        definition silently shadowed this one, so every caller
        (get_device_point_values(), the Energy Engine, SemanticResolver,
        CommissioningPointResolver) was actually getting the CORRECT
        _prev_values-based behavior by accident of definition order. An
        earlier fix here mistakenly resolved the naming collision by keeping
        THIS (broken, _current_values-based) definition as canonical and
        renaming the working one out of the way -- which fixed the naming
        collision but broke real value resolution in the process. Both
        definitions are now consolidated into this one, correct,
        _prev_values-based implementation."""
        return self._prev_values.get(object_id)

    def get_device_point_values(self, objects: list[dict]) -> dict[str, object]:
        values: dict[str, object] = {}
        for obj in objects:
            point_type = obj.get("point_type")
            if point_type:
                values[str(point_type)] = self.get_object_value(obj["id"])
        return values
    def get_devices_by_instance(self) -> dict[int, dict]:
        return self._devices_by_instance

    def resolve_wire_object(
        self,
        object_type: str,
        physical_instance: int,
    ) -> Optional[dict]:
        """Resolve a wire-visible BACnet object back to its simulator row.

        Regular simulator objects are stored in ``self._objects`` as:
            database object id -> (live BACpypes object, Behavior)

        Matching the actual live ``objectIdentifier`` is safer than trying to
        reverse the slot-offset formula because it also stays correct after
        reloads and for any future changes to instance allocation.
        """
        normalized_type = str(object_type).strip().lower().replace('_', '-')

        for object_db_id, (bacnet_obj, behavior) in self._objects.items():
            try:
                identifier = bacnet_obj.objectIdentifier
                wire_type = str(identifier[0]).strip().lower().replace('_', '-')
                wire_instance = int(identifier[1])
            except Exception:
                continue

            if (
                wire_type != normalized_type
                or wire_instance != int(physical_instance)
            ):
                continue

            obj_row = self.db.get_object(object_db_id)
            if obj_row is None:
                return None

            device = self.db.get_device(int(obj_row['device_id']))
            if device is None:
                return None

            # Same fix as get_object_value() above: _current_values is only
            # ever the whole /sim/state snapshot, never per-object -- use
            # _prev_values, which IS refreshed per-object every tick.
            current_value = self._prev_values.get(object_db_id)
            if isinstance(current_value, dict):
                current_value = current_value.get('value')

            return {
                'device_id': int(device['id']),
                'device_instance': int(device['device_instance']),
                'device_name': str(device['name']),
                'object_id': int(obj_row['id']),
                'object_name': str(obj_row['name']),
                'object_type': str(obj_row['object_type']),
                'object_instance': int(obj_row['object_instance']),
                'wire_object_identifier': (
                    f"{wire_type}:{wire_instance}"
                ),
                'local_object_identifier': (
                    f"{obj_row['object_type']}:{obj_row['object_instance']}"
                ),
                'current_value': current_value,
                'units': obj_row.get('units'),
                'behavior': obj_row.get('behavior'),
                'point_type': obj_row.get('point_type'),
            }

        return None

    def _update_value(self, bacnet_obj: Any, otype: str, val: Any) -> None:
        if otype in ("analog-input", "analog-output", "analog-value"):
            bacnet_obj.presentValue = Real(float(val))
        elif otype == "binary-output":
            active = bool(val) if not isinstance(val, bool) else val
            bacnet_obj.presentValue = BinaryPV("active" if active else "inactive")  # triggers recalculating() via priorityArray
        elif otype in MULTISTATE_TYPES:
            n_states = int(bacnet_obj.numberOfStates)
            state = max(1, min(n_states, round(float(val))))
            bacnet_obj.presentValue = Unsigned(state)
        else:
            active = bool(val) if not isinstance(val, bool) else val
            bacnet_obj.presentValue = BinaryPV("active" if active else "inactive")

    async def tick(self) -> None:
        """Advance sim state and update all object values."""
        if self.clock_state != "running":
            return

        self.state.elapsed_seconds += TICK_SECONDS
        self.state.time_of_day = (self.state.time_of_day + TICK_SECONDS / 3600) % 24

        snapshot: dict[int, dict] = {}
        devices = await asyncio.to_thread(self.db.get_devices)
        dev_map = {d["id"]: d for d in devices}
        device_capabilities = {d["device_instance"]: _effective_can_receive_events(d) for d in devices}

        alarm_configs = {c["object_id"]: c for c in await asyncio.to_thread(self.db.get_all_alarm_configs)}
        event_enrollments = await asyncio.to_thread(self.db.get_all_event_enrollments)
        enrollments_by_object: dict[int, list[dict]] = {}
        for ee in event_enrollments:
            enrollments_by_object.setdefault(ee["monitored_object_id"], []).append(ee)
        notification_classes = (
            {nc["id"]: nc for nc in await asyncio.to_thread(self.db.get_notification_classes)}
            if alarm_configs or event_enrollments else {}
        )
        trend_logs = await asyncio.to_thread(self.db.get_all_trend_logs)
        trend_logs_by_object: dict[int, list[dict]] = {}
        for tl in trend_logs:
            trend_logs_by_object.setdefault(tl["monitored_object_id"], []).append(tl)
        now = time.time()

        for obj_id, (bacnet_obj, behavior) in self._objects.items():
            obj_row = await asyncio.to_thread(self.db.get_object, obj_id)
            if not obj_row:
                continue
            dev = dev_map.get(obj_row["device_id"])
            if not dev:
                continue
            # Rebuild behavior if it changed, carrying stateful internals across ticks
            new_b = make_behavior(obj_row["behavior"], obj_row["behavior_params"], obj_row.get("manual_value"))
            if isinstance(new_b, ManualBehavior) and isinstance(behavior, ManualBehavior):
                new_b.set(behavior._value)
            elif obj_row.get("manual_value") is not None and isinstance(new_b, ManualBehavior):
                new_b.set(obj_row["manual_value"])
            if isinstance(new_b, FaultBehavior) and isinstance(behavior, FaultBehavior):
                new_b._fault_active = behavior._fault_active
                new_b._fault_end_elapsed = behavior._fault_end_elapsed
                new_b._inner = behavior._inner
            if isinstance(new_b, RandomWalkBehavior) and isinstance(behavior, RandomWalkBehavior):
                new_b._value = behavior._value
            self._objects[obj_id] = (bacnet_obj, new_b)
            val = normalize_present_value(obj_row["object_type"], new_b.compute(self.state))
            self._update_value(bacnet_obj, obj_row["object_type"], val)

            cfg = alarm_configs.get(obj_id)
            if cfg is not None:
                await self._evaluate_alarm(obj_id, obj_row, dev, val, cfg, notification_classes, device_capabilities)

            for enrollment in enrollments_by_object.get(obj_id, []):
                await self._evaluate_enrollment(enrollment, obj_row, dev, val, notification_classes, device_capabilities)

            for tl in trend_logs_by_object.get(obj_id, []):
                if tl["logging_type"] == "polled":
                    if now - (tl["last_sampled_at"] or 0) >= tl["log_interval"]:
                        await self._sample_trend_log(tl["id"], val)
                elif tl["logging_type"] == "cov":
                    last = self._trend_log_last_value.get(tl["id"])
                    if last is None or self._trend_log_value_changed(val, last, tl["cov_increment"]):
                        await self._sample_trend_log(tl["id"], val)
                        self._trend_log_last_value[tl["id"]] = val

            self._prev_values[obj_id] = val

            # Append to rolling history (never persisted)
            hist = self._history.setdefault(obj_id, deque(maxlen=OBJECT_HISTORY_MAXLEN))
            hist.append((time.time(), 1.0 if val is True else 0.0 if val is False else float(val)))

            did = dev["device_instance"]
            if did not in snapshot:
                snapshot[did] = {"device_instance": did, "name": dev["name"], "objects": []}
            snapshot[did]["objects"].append({
                "id": obj_id,
                "name": obj_row["name"],
                "object_type": obj_row["object_type"],
                "object_instance": obj_row["object_instance"],
                "value": val,
                "units": obj_row.get("units", ""),
                "behavior": obj_row["behavior"],
            })

        self._current_values = {"devices": list(snapshot.values()), "tick": self.state.elapsed_seconds}

        # Calendar objects (GH #18) have no self-scheduling hook like Schedule
        # does, so refresh presentValue here — cheap, and only cosmetic for a
        # direct ReadProperty since Schedule's own calendarReference
        # resolution reads dateList directly, not presentValue.
        for cal_id, cal_bacnet_obj in self._calendar_objects.items():
            cal_row = await asyncio.to_thread(self.db.get_calendar, cal_id)
            if not cal_row:
                continue
            try:
                entries = json.loads(cal_row["date_list"] or "[]")
                cal_bacnet_obj.presentValue = Boolean(bacnet_calendar.today_in_date_list(entries))
            except Exception:
                log.exception("Failed to refresh calendar %r presentValue", cal_row.get("name"))

    async def _evaluate_alarm(
        self, obj_id: int, obj_row: dict, dev: dict, val: Any, cfg: dict, notification_classes: dict[int, dict],
        device_capabilities: dict[int, bool],
    ) -> None:
        """Advance one object's intrinsic-reporting state machine and, on a
        confirmed transition, log it and (best-effort) notify the object's
        Notification Class recipients. See alarms.py for the algorithm."""
        runtime = self._alarm_runtime.setdefault(obj_id, alarms.AlarmRuntime())
        try:
            params = json.loads(cfg["params"] or "{}")
        except (TypeError, ValueError):
            params = {}
        transition = alarms.evaluate(
            obj_row["object_type"], val, params, runtime,
            self.state.elapsed_seconds, cfg["time_delay"], cfg["time_delay_normal"],
        )
        if transition is None:
            return
        from_state, to_state = transition

        tname = alarms.transition_name(to_state)
        try:
            event_enable = json.loads(cfg["event_enable"] or "[]")
        except (TypeError, ValueError):
            event_enable = []
        if tname not in event_enable:
            return

        nc = notification_classes.get(cfg["notification_class_id"])
        priority = 100
        ack_required = False
        if nc is not None:
            priority = {
                "to-offnormal": nc["priority_to_offnormal"],
                "to-fault": nc["priority_to_fault"],
                "to-normal": nc["priority_to_normal"],
            }.get(tname, 100)
            try:
                ack_list = json.loads(nc["ack_required_transitions"] or "[]")
            except (TypeError, ValueError):
                ack_list = []
            ack_required = tname in ack_list

        detail = alarms.describe_transition(obj_row["object_type"], val, params, from_state, to_state, obj_row.get("units", ""))
        message = f"{obj_row['name']} transitioned {from_state} → {to_state}: {detail}"
        await asyncio.to_thread(self.db.log_alarm, {
            "object_id": obj_id,
            "device_id": dev["id"],
            "object_name": obj_row["name"],
            "from_state": from_state,
            "to_state": to_state,
            "priority": priority,
            "value": str(val),
            "message": message,
            "ack_required": 1 if ack_required else 0,
        })
        log_level = "info" if to_state == "normal" else "error" if to_state == "fault" else "warn"
        _log_event(dev["id"], log_level, f"Alarm: {message}")

        if nc is not None and self.app is not None:
            asyncio.create_task(alarms.send_event_notification(
                self.app, dev["device_instance"], obj_row, nc,
                from_state, to_state, priority, ack_required,
                device_capabilities=device_capabilities,
                log_fn=lambda level, msg: _log_event(dev["id"], level, msg),
                on_local_delivery=_log_event_notification_received,
            ))

    async def _evaluate_enrollment(
        self, enrollment: dict, obj_row: dict, dev: dict, val: Any, notification_classes: dict[int, dict],
        device_capabilities: dict[int, bool],
    ) -> None:
        """Same shape as _evaluate_alarm(), but for an Event Enrollment
        watching obj_row's present-value independently of obj_row's own
        alarm config — see alarms.evaluate_enrollment()."""
        runtime = self._enrollment_runtime.setdefault(enrollment["id"], alarms.AlarmRuntime())
        try:
            params = json.loads(enrollment["event_parameters"] or "{}")
        except (TypeError, ValueError):
            params = {}
        transition = alarms.evaluate_enrollment(
            enrollment["algorithm"], obj_row["object_type"], val, params, runtime,
            self.state.elapsed_seconds, enrollment["time_delay"], enrollment["time_delay_normal"],
        )
        if transition is None:
            return
        from_state, to_state = transition

        tname = alarms.transition_name(to_state)
        try:
            event_enable = json.loads(enrollment["event_enable"] or "[]")
        except (TypeError, ValueError):
            event_enable = []
        if tname not in event_enable:
            return

        nc = notification_classes.get(enrollment["notification_class_id"])
        priority = 100
        ack_required = False
        if nc is not None:
            priority = {
                "to-offnormal": nc["priority_to_offnormal"],
                "to-fault": nc["priority_to_fault"],
                "to-normal": nc["priority_to_normal"],
            }.get(tname, 100)
            try:
                ack_list = json.loads(nc["ack_required_transitions"] or "[]")
            except (TypeError, ValueError):
                ack_list = []
            ack_required = tname in ack_list

        detail = alarms.describe_transition(obj_row["object_type"], val, params, from_state, to_state, obj_row.get("units", ""))
        message = f"[{enrollment['name']}] {obj_row['name']} transitioned {from_state} → {to_state}: {detail}"

        await asyncio.to_thread(self.db.log_alarm, {
            "object_id": obj_row["id"],
            "device_id": dev["id"],
            "object_name": f"{enrollment['name']} ({obj_row['name']})",
            "from_state": from_state,
            "to_state": to_state,
            "priority": priority,
            "value": str(val),
            "message": message,
            "ack_required": 1 if ack_required else 0,
        })
        log_level = "info" if to_state == "normal" else "error" if to_state == "fault" else "warn"
        _log_event(dev["id"], log_level, f"Alarm: {message}")

        if nc is not None and self.app is not None:
            asyncio.create_task(alarms.send_event_notification(
                self.app, dev["device_instance"], obj_row, nc,
                from_state, to_state, priority, ack_required,
                device_capabilities=device_capabilities,
                log_fn=lambda level, msg: _log_event(dev["id"], level, msg),
                on_local_delivery=_log_event_notification_received,
            ))

    async def reload(self) -> None:
        """Rebuild the BACnet stack from DB (called after config changes)."""
        async with self._reload_lock:
            log.info("Reloading BACnet stack...")
            if self.app:
                for (bacnet_obj, _) in list(self._objects.values()):
                    try:
                        self.app.delete_object(bacnet_obj)
                    except Exception:
                        pass
                for bacnet_obj in list(self._trend_log_objects.values()):
                    try:
                        self.app.delete_object(bacnet_obj)
                    except Exception:
                        pass
                for bacnet_obj in list(self._schedule_objects.values()):
                    if getattr(bacnet_obj, "_interpret_schedule_handle", None):
                        bacnet_obj._interpret_schedule_handle.cancel()
                    try:
                        self.app.delete_object(bacnet_obj)
                    except Exception:
                        pass
                for bacnet_obj in list(self._calendar_objects.values()):
                    try:
                        self.app.delete_object(bacnet_obj)
                    except Exception:
                        pass
                self._objects.clear()
                self._trend_log_objects.clear()
                self._schedule_objects.clear()
                self._calendar_objects.clear()
                self._prev_values.clear()
                self._history.clear()
                self._alarm_runtime.clear()
                self._enrollment_runtime.clear()
                self._trend_log_last_value.clear()
                self._current_values = {}
                # Explicitly close the bacpypes3 socket before dropping the reference.
                # BinaryOutputObject↔PriorityArray form a circular reference that delays
                # GC, keeping the UDP socket bound to port 47808 and preventing re-bind.
                try:
                    await self.app.close()
                except Exception:
                    pass
                finally:
                    # Always sweep for any endpoint task close() didn't get
                    # to, so a failure here can never leak a live socket
                    # bind past this reload (see the helper's docstring).
                    try:
                        _force_close_bacnet_transports(self.app)
                    except Exception:
                        pass
                self.app = None
            await self.start()
            log.info("Reload complete")

    async def stop(self) -> None:
        """Cleanly shut down the BACnet stack."""
        if self.app:
            for (bacnet_obj, _) in list(self._objects.values()):
                try:
                    self.app.delete_object(bacnet_obj)
                except Exception:
                    pass
            for bacnet_obj in list(self._trend_log_objects.values()):
                try:
                    self.app.delete_object(bacnet_obj)
                except Exception:
                    pass
            for bacnet_obj in list(self._schedule_objects.values()):
                if getattr(bacnet_obj, "_interpret_schedule_handle", None):
                    bacnet_obj._interpret_schedule_handle.cancel()
                try:
                    self.app.delete_object(bacnet_obj)
                except Exception:
                    pass
            for bacnet_obj in list(self._calendar_objects.values()):
                try:
                    self.app.delete_object(bacnet_obj)
                except Exception:
                    pass
            self._objects.clear()
            self._trend_log_objects.clear()
            self._schedule_objects.clear()
            self._calendar_objects.clear()
            try:
                await self.app.close()
            except Exception:
                pass
            finally:
                try:
                    _force_close_bacnet_transports(self.app)
                except Exception:
                    pass
            self.app = None
        log.info("BACnet stack stopped")

    async def add_object_hot(self, device_instance: int, obj_row: dict) -> None:
        """Hot-add a single object to the running BACnet app without full reload."""
        if not self.app:
            return
        slot = self._device_slots.get(device_instance, 0)
        dev_obj = self.app._virtual_devices.get(device_instance)
        dev_name = str(dev_obj.objectName) if dev_obj else ""
        bacnet_obj, behavior = self._create_object(obj_row, slot, dev_name)
        self.app.add_object(bacnet_obj)
        self._objects[obj_row["id"]] = (bacnet_obj, behavior)
        if dev_obj:
            existing = list(self.app._virtual_object_lists.get(device_instance, []))
            existing.append(bacnet_obj.objectIdentifier)
            dev_obj.objectList = existing
            self.app._virtual_object_lists[device_instance] = existing

    def set_manual_value(self, obj_id: int, value: Any) -> bool:
        if obj_id not in self._objects:
            return False
        bacnet_obj, behavior = self._objects[obj_id]
        if isinstance(behavior, ManualBehavior):
            behavior.set(value)
        else:
            new_b = ManualBehavior({"value": value})
            self._objects[obj_id] = (bacnet_obj, new_b)
        obj_row = self.db.get_object(obj_id)
        if obj_row:
            self._update_value(bacnet_obj, obj_row["object_type"], value)
        return True

    async def write_object(self, obj_id: int, value: Any, source: Optional[str] = None) -> bool:
        """Handle a BACnet WriteProperty — switches the object to manual, persists, updates live.

        Unlike the REST "Set" endpoint (set_object_value, which logs its own
        "Manual override" activity-log entry), this is the path a genuine
        external BACnet client's WriteProperty request takes — it was
        previously silent in the per-device Activity Log (still counted in
        the analytics traffic metrics, just not human-audit-visible), so a
        real external write left no record of what was written or by whom.
        `source` is the requesting client's address (apdu.pduSource), when
        the caller has one, for a real audit trail of who wrote what.
        """
        if obj_id not in self._objects:
            return False
        bacnet_obj, _ = self._objects[obj_id]
        obj_row = await asyncio.to_thread(self.db.get_object, obj_id)
        if not obj_row:
            return False
        await asyncio.to_thread(self.db.write_object, obj_id, value)
        new_b = ManualBehavior({"value": value})
        self._objects[obj_id] = (bacnet_obj, new_b)
        self._update_value(bacnet_obj, obj_row["object_type"], value)
        val_str = str(value) + (f" {obj_row['units']}" if obj_row.get("units") and obj_row["units"] != "no-units" else "")
        source_suffix = f" (from {source})" if source else ""
        _log_event(obj_row["device_id"], "info", f"External write: {obj_row['name']} → {val_str}{source_suffix}")
        return True

    @staticmethod
    def _priority_value_out(pv: PriorityValue) -> Any:
        """Decode a PriorityValue slot to a plain Python value, or None if null."""
        if pv._choice == "null":
            return None
        raw = getattr(pv, pv._choice)
        return str(raw) == "active" if pv._choice == "enumerated" else raw

    def get_priority_array(self, obj_id: int) -> Optional[dict]:
        """Read all 16 priority-array slots + relinquish default (GH #17).
        Returns None for object types with no real priority array — i.e.
        everything except the three Commandable *-output types."""
        if obj_id not in self._objects:
            return None
        bacnet_obj, _ = self._objects[obj_id]
        if not isinstance(bacnet_obj, Commandable):
            return None
        cp = bacnet_obj.currentCommandPriority
        rd = bacnet_obj.relinquishDefault
        relinquish_default = str(rd) == "active" if isinstance(rd, BinaryPV) else \
            (float(rd) if isinstance(rd, Real) else int(rd))
        return {
            "priority_array": [self._priority_value_out(pv) for pv in bacnet_obj.priorityArray],
            "relinquish_default": relinquish_default,
            "current_command_priority": int(cp.unsigned) if cp.unsigned is not None else None,
        }

    async def write_priority(self, obj_id: int, priority: int, value: Any) -> bool:
        """Write (or, if value is None, relinquish) a specific priority-array
        slot on a Commandable object (GH #17) — this is a direct priority-array
        write, distinct from write_object()'s "the sim value" (priority 16)."""
        if obj_id not in self._objects:
            return False
        bacnet_obj, _ = self._objects[obj_id]
        if not isinstance(bacnet_obj, Commandable):
            return False
        if not (1 <= priority <= 16):
            return False
        obj_row = await asyncio.to_thread(self.db.get_object, obj_id)
        if not obj_row:
            return False
        otype = obj_row["object_type"]
        if value is None:
            pv = PriorityValue(null=())
        elif otype == "analog-output":
            pv = PriorityValue(real=float(value))
        elif otype == "multi-state-output":
            pv = PriorityValue(unsigned=int(value))
        else:
            active = bool(value) if not isinstance(value, bool) else value
            pv = PriorityValue(enumerated=BinaryPV("active" if active else "inactive"))
        bacnet_obj.priorityArray[priority - 1] = pv
        return True

    def get_state(self) -> dict:
        return self._current_values

    def db_id_for_bacnet_object(self, bacnet_obj: Any) -> Optional[int]:
        """Reverse lookup: given a live bacpypes3 object, find the DB row id
        that owns it. Used by both incoming WriteProperty requests and
        Schedule objects' present_value_changed() (see bacnet_schedule.py)."""
        for did, (bobj, _) in self._objects.items():
            if bobj is bacnet_obj:
                return did
        return None

    @staticmethod
    def _trend_log_value_changed(value: Any, last: Any, cov_increment: float) -> bool:
        if isinstance(value, bool) or isinstance(last, bool):
            return bool(value) != bool(last)
        try:
            return abs(float(value) - float(last)) >= cov_increment
        except (TypeError, ValueError):
            return value != last

    async def _sample_trend_log(self, tl_id: int, val: Any) -> Optional[int]:
        """Append a record and, if this trend log is exposed on the BACnet
        wire, refresh its recordCount/totalRecordCount so a ReadProperty
        reflects the latest buffer state without waiting for a reload().
        Returns the new sequence number, or None if the buffer was full
        with stop_when_full set."""
        seq = await asyncio.to_thread(self.db.add_trend_record, tl_id, val)
        if seq is None:
            return None
        bacnet_obj = self._trend_log_objects.get(tl_id)
        if bacnet_obj is not None:
            cfg = await asyncio.to_thread(self.db.get_trend_log, tl_id)
            if cfg:
                bacnet_obj.recordCount = Unsigned(cfg["record_count"])
                bacnet_obj.totalRecordCount = Unsigned(cfg["total_record_count"])
        return seq

    def refresh_trend_log_buffer_empty(self, tl_id: int) -> None:
        """Reflect a cleared record buffer on the BACnet-wire object, if any."""
        bacnet_obj = self._trend_log_objects.get(tl_id)
        if bacnet_obj is not None:
            bacnet_obj.logBuffer = SequenceOf(LogRecord)([])
            bacnet_obj.recordCount = Unsigned(0)


# ─── FastAPI models ───────────────────────────────────────────────────────────
# Moved to bacnet_sim/schemas.py (GH #15 pass 1) -- imported at the top of this file.


# ─── Globals (shared between FastAPI and engine) ──────────────────────────────

db: Database = None  # type: ignore
engine: SimEngine = None  # type: ignore
ws_clients: list[WebSocket] = []
fault_detection_engine: FaultDetectionEngine | None = None
energy_engine : EnergyEngine | None = None

packet_capture = PacketCapture(
    max_packets=10_000,
    max_payload_bytes=65_535,
)
packet_stream_ws_clients: list[WebSocket] = []

# ─── BACpypes3 packet-capture transport hooks ─────────────────────────────────

_bacpypes_capture_hooks_installed = False


def _bacpypes_address_tuple(
    address: Any,
    *,
    fallback_ip: str,
    fallback_port: int,
) -> tuple[str, int]:
    """
    Convert a BACpypes3 IPv4Address-like object into (IP, port).

    BACpypes3 versions may expose the tuple under slightly different
    attributes, so this deliberately uses defensive fallbacks.
    """
    if address is None:
        return fallback_ip, fallback_port

    for attribute in ("addrTuple", "addr_tuple"):
        value = getattr(address, attribute, None)

        if (
            isinstance(value, tuple)
            and len(value) >= 2
        ):
            return str(value[0]), int(value[1])

    text = str(address)

    # BACpypes3 may stringify an address as "10.0.0.60:47808".
    if ":" in text:
        host, possible_port = text.rsplit(":", 1)

        try:
            return host, int(possible_port)
        except ValueError:
            pass

    return text or fallback_ip, fallback_port


def install_bacpypes_packet_capture_hooks(
    *,
    local_ip: str,
    local_port: int,
    get_clock_state: Callable[[], str],
) -> None:
    """
    Install one process-wide hook around BACpypes3's IPv4 UDP transport.

    indication()   = outbound toward the UDP socket
    confirmation() = inbound from the UDP socket

    While clock_state != "running" (i.e. "paused" or "stopped"), ALL
    outbound traffic is suppressed here — this is the single choke point
    every outbound byte passes through regardless of what generated it
    (Who-Is/I-Am response, ReadProperty/WriteProperty ACK, COV
    notification, ...), so it's the simplest, safest place to make both
    "Pause" and "Stop" mean "the simulator stops responding" without
    tearing down and rebinding the UDP socket itself (which Start would
    then have to safely reconstruct — this way Start/Pause/Stop just
    toggle a check, the transport is never touched). Pause and Stop still
    differ elsewhere (Stop rewinds elapsed_seconds/time_of_day to 0,
    Pause leaves them exactly where they were so Resume picks up without
    losing simulated time) — this suppression is the one thing they now
    share.

    Suppressed packets are neither sent nor recorded by packet capture —
    they never happened, from the network's point of view. Inbound
    traffic (confirmation()) is NOT gated here: a real, paused/stopped
    controller still receives whatever other devices broadcast at it
    (e.g. Who-Is), it just doesn't answer -- see the "Stop"/"Pause"
    behavior decided for this simulator.
    """
    global _bacpypes_capture_hooks_installed

    if _bacpypes_capture_hooks_installed:
        return

    original_indication = IPv4DatagramServer.indication
    original_confirmation = IPv4DatagramServer.confirmation

    async def captured_indication(
        transport_self: IPv4DatagramServer,
        pdu: Any,
    ) -> None:
        if get_clock_state() != "running":
            return

        try:
            payload = bytes(getattr(pdu, "pduData", b""))

            destination = _bacpypes_address_tuple(
                getattr(pdu, "pduDestination", None),
                fallback_ip="255.255.255.255",
                fallback_port=local_port,
            )

            source = _bacpypes_address_tuple(
                getattr(pdu, "pduSource", None),
                fallback_ip=local_ip,
                fallback_port=local_port,
            )

            if payload:
                packet_capture.record_outbound(
                    payload,
                    source=source,
                    destination=destination,
                )
        except Exception:
            # Capture must never interrupt BACnet communication.
            log.exception(
                "Failed to record outbound BACnet/IP packet"
            )

        await original_indication(transport_self, pdu)

    async def captured_confirmation(
        transport_self: IPv4DatagramServer,
        pdu: Any,
    ) -> None:
        try:
            payload = bytes(getattr(pdu, "pduData", b""))

            source = _bacpypes_address_tuple(
                getattr(pdu, "pduSource", None),
                fallback_ip="0.0.0.0",
                fallback_port=local_port,
            )

            destination = _bacpypes_address_tuple(
                getattr(pdu, "pduDestination", None),
                fallback_ip=local_ip,
                fallback_port=local_port,
            )

            if payload:
                packet_capture.record_inbound(
                    payload,
                    source=source,
                    destination=destination,
                )
        except Exception:
            log.exception(
                "Failed to record inbound BACnet/IP packet"
            )

        await original_confirmation(transport_self, pdu)

    IPv4DatagramServer.indication = captured_indication
    IPv4DatagramServer.confirmation = captured_confirmation

    _bacpypes_capture_hooks_installed = True

    log.info(
        "Installed BACpypes3 packet-capture hooks for %s:%s",
        local_ip,
        local_port,
    )

# ─── Per-device event log ─────────────────────────────────────────────────────
_device_logs: dict[int, deque] = {}
_global_log: deque = deque(maxlen=1000)
_device_names: dict[int, str] = {}
_MAX_LOG = 300





# ─── Analytics metrics store ───────────────────────────────────────────────────
# In-memory only (never persisted), same pattern as _global_log above — reset
# on process restart. Plain dict/deque mutations only, no locks needed (single
# asyncio event loop) and no per-request DB writes, so this stays cheap enough
# to not affect simulator performance.

class Metrics:
    def __init__(self) -> None:
        self.start_time = time.time()
        # (str(pduSource), invoke_id) -> {device, object, service, started}
        # stamped at request-start, popped in SimApplication.response()
        self.pending: dict[tuple, dict] = {}

        self.requests_total = 0
        self.requests_by_service: dict[str, int] = defaultdict(int)
        self.requests_by_device: dict[int, int] = defaultdict(int)
        self.requests_broadcast = 0
        self.requests_unicast = 0
        self.reads_total = 0
        self.writes_total = 0

        # "reject:<reason>" / "abort:<reason>" / "error:<class>.<code>"
        self.errors_by_type: dict[str, int] = defaultdict(int)
        self.recent_errors: deque = deque(maxlen=200)

        self.object_reads: dict[int, int] = defaultdict(int)
        self.object_writes: dict[int, int] = defaultdict(int)

        self.discovery_total = 0
        self.iam_seen: dict[int, float] = {}          # device_instance -> last-seen ts
        self.new_devices_timeline: deque = deque(maxlen=200)
        self.duplicate_id_events: deque = deque(maxlen=100)

        self.recent_requests: deque = deque(maxlen=500)   # live traffic feed
        self.latencies_ms: deque = deque(maxlen=500)
        self.clients_seen: dict[str, float] = {}            # source addr -> last-seen ts


metrics = Metrics()
metrics_ws_clients: list[WebSocket] = []


def _apply_settings_live(values: dict) -> None:
    """Push a settings dict into the module globals/buffers that actually
    drive behavior, so a save takes effect immediately — no restart. Safe to
    call repeatedly (e.g. once at startup, then again on every PUT /settings).
    Resizing a deque via deque(old, maxlen=new) keeps only the newest `new`
    items, matching normal ring-buffer truncation semantics."""
    global TICK_SECONDS, JWT_EXPIRE_HOURS, OBJECT_HISTORY_MAXLEN, _MAX_LOG, _global_log

    TICK_SECONDS = values["tick_seconds"]
    JWT_EXPIRE_HOURS = values["jwt_expire_hours"]
    OBJECT_HISTORY_MAXLEN = values["object_history_maxlen"]

    _MAX_LOG = values["device_log_maxlen"]
    for device_id in list(_device_logs):
        _device_logs[device_id] = deque(_device_logs[device_id], maxlen=_MAX_LOG)
    _global_log = deque(_global_log, maxlen=values["global_log_maxlen"])

    metrics.recent_errors = deque(metrics.recent_errors, maxlen=values["metrics_errors_maxlen"])
    metrics.new_devices_timeline = deque(metrics.new_devices_timeline, maxlen=values["metrics_new_devices_maxlen"])
    metrics.duplicate_id_events = deque(metrics.duplicate_id_events, maxlen=values["metrics_duplicate_id_maxlen"])
    metrics.recent_requests = deque(metrics.recent_requests, maxlen=values["metrics_traffic_feed_maxlen"])
    metrics.latencies_ms = deque(metrics.latencies_ms, maxlen=values["metrics_traffic_feed_maxlen"])

    if engine is not None:
        for obj_id in list(engine._history):
            engine._history[obj_id] = deque(engine._history[obj_id], maxlen=OBJECT_HISTORY_MAXLEN)


def _log_event(device_id: Optional[int], level: str, message: str) -> None:
    """
    device_id=None records a simulator-level event not attributable to any
    one virtual device — e.g. an incoming Event Notification, which (since
    every virtual device shares one socket/address) carries the *sender's*
    device identifier but nothing about which of our devices it was
    addressed to. That's a real BACnet Event Notification limitation, not
    something to paper over with a guess.
    """
    entry = {
        "ts": time.time(),
        "level": level,
        "device_id": device_id,
        "device_name": _device_names.get(device_id, f"#{device_id}") if device_id is not None else "Simulator",
        "message": message,
    }
    if device_id is not None:
        if device_id not in _device_logs:
            _device_logs[device_id] = deque(maxlen=_MAX_LOG)
        _device_logs[device_id].append(entry)
    _global_log.append(entry)


def _log_event_notification_received(
    sender_instance: Optional[int],
    recipient_instance: Optional[int],
    message_text: str,
    from_state: str,
    to_state: str,
) -> None:
    """
    Records that an Event Notification "arrived" — closes the loop on
    alarms.send_event_notification(). For device-type recipients this is
    called directly in-process (see send_event_notification's
    on_local_delivery callback) rather than from a real received APDU:
    bacpypes3's IPv4 transport (ipv4/__init__.py, IPv4DatagramServer.
    confirmation()) silently drops any inbound packet whose source address
    equals our own bound address, treating it as a reflected broadcast — so
    a genuine network round-trip can never reach Application.indication()
    for a notification addressed to one of our own devices (every virtual
    device here shares one socket/address, so that's always the case for a
    device-type recipient). Simulating receipt directly sidesteps a real,
    structural bacpypes3 limitation rather than fighting it — and, since
    it's in-process, we actually know the recipient here, unlike a genuine
    received APDU (which only ever carries the *sender's* device identifier;
    recipient_instance is None when called from that path, e.g. an external
    address-type sender).

    Logged at the simulator level (device_id=None) rather than attributed to
    the recipient device: it's still a real BACnet Event Notification
    limitation that a genuinely external client would face — the recipient
    is only ever a network address on the wire, this simulator just happens
    to have out-of-band knowledge of it for its own device-type recipients.
    """
    sender = f"device {sender_instance}" if sender_instance is not None else "an unknown device"
    recipient = f" to device {recipient_instance}" if recipient_instance is not None else ""
    level = "info" if to_state == "normal" else "error" if to_state == "fault" else "warn"
    msg = f"Received Event Notification from {sender}{recipient}: {message_text} ({from_state} -> {to_state})"
    log.info(msg)
    _log_event(None, level, msg)


# ─── WebSocket broadcaster ────────────────────────────────────────────────────

async def broadcast_state() -> None:
    if not ws_clients:
        return

    data = json.dumps(
        engine.get_state()
    )

    dead_clients: list[WebSocket] = []

    for websocket in list(ws_clients):
        try:
            await websocket.send_text(data)
        except Exception:
            dead_clients.append(websocket)

    for websocket in dead_clients:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


# ─── Packet capture stream broadcaster ─────────────────────────────────────────

async def broadcast_captured_packet(packet: CapturedPacket) -> None:
    if not packet_stream_ws_clients:
        return

    try:
        payload = packet.to_dict(include_hex=True)

        # Cheap, in-memory device-only association (I-Am / directed Who-Is /
        # device-object) -- see plan notes. The no-op resolver skips the
        # expensive per-object DB/O(N) path entirely for live packets;
        # ordinary point-level traffic stays unassociated until the next
        # REST fetch.
        resolve_packet_simulator_context(
            payload,
            devices_by_instance=(
                engine.get_devices_by_instance() if engine else {}
            ),
            resolve_object=lambda *_args, **_kwargs: None,
        )

        data = json.dumps(payload)
    except Exception as exc:
        # Never let a malformed/unexpected packet break the capture path --
        # matches _record()'s own invariant -- but don't fail silently either.
        log.debug("packet-capture stream: failed to prepare packet: %s", exc)
        return

    dead_clients: list[WebSocket] = []

    for websocket in list(packet_stream_ws_clients):
        try:
            await websocket.send_text(data)
        except Exception:
            dead_clients.append(websocket)

    for websocket in dead_clients:
        if websocket in packet_stream_ws_clients:
            packet_stream_ws_clients.remove(websocket)


def _on_packet_captured(packet: CapturedPacket) -> None:
    if packet_stream_ws_clients:  # skip task creation with nobody listening
        asyncio.create_task(broadcast_captured_packet(packet))


packet_capture.set_packet_listener(_on_packet_captured)


# ─── Analytics aggregation ─────────────────────────────────────────────────────
# Per-request instrumentation (above, in SimApplication) only does cheap O(1)
# counter/dict updates. All cross-referencing and sorting — which is more
# expensive but still bounded (object counts are small; deques capped at
# <=500) — happens here instead, once per metrics tick rather than once per
# BACnet request, so it can't add per-request latency to the simulator.

_PROCESS = psutil.Process()


def _object_to_device_map() -> dict[str, int]:
    """Reverse of engine.app._virtual_object_lists (device -> [objid]),
    rebuilt each tick rather than cached, since it's cheap and always in sync
    with the current device set (no invalidation-on-reload bookkeeping
    needed)."""
    mapping: dict[str, int] = {}
    if engine is None or engine.app is None:
        return mapping
    for did, objids in engine.app._virtual_object_lists.items():
        for objid in objids:
            mapping[str(ObjectIdentifier(objid))] = did
    return mapping


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * pct))
    return s[idx]


async def build_metrics_snapshot() -> dict:
    now = time.time()
    devices = await asyncio.to_thread(db.get_devices) if db is not None else []
    obj_to_device = _object_to_device_map()

    # Overview
    recent_1s = [r for r in metrics.recent_requests if now - r["ts"] <= 1.0]
    active_clients = [addr for addr, ts in metrics.clients_seen.items() if now - ts <= 30.0]
    online_devices = sum(1 for d in devices if d.get("enabled"))
    active_alarms = sum(
        1 for dev in engine.get_state().get("devices", []) if engine is not None
        for o in dev["objects"] if o.get("behavior") == "fault"
    ) if engine is not None else 0

    # Traffic
    device_activity: dict[int, int] = defaultdict(int)
    for objid_key, count in list(metrics.object_reads.items()) + list(metrics.object_writes.items()):
        did = obj_to_device.get(objid_key)
        if did is not None:
            device_activity[did] += count
    top_devices = sorted(device_activity.items(), key=lambda kv: kv[1], reverse=True)[:10]
    device_names = {d["device_instance"]: d["name"] for d in devices}

    # Object analytics
    all_objids = set(obj_to_device.keys())
    accessed_objids = set(metrics.object_reads.keys()) | set(metrics.object_writes.keys())
    unused_objects = len(all_objids - accessed_objids)
    top_objects = sorted(
        ((k, metrics.object_reads.get(k, 0) + metrics.object_writes.get(k, 0)) for k in accessed_objids),
        key=lambda kv: kv[1], reverse=True,
    )[:15]

    # Performance
    lat = list(metrics.latencies_ms)
    error_count_recent = sum(1 for e in metrics.recent_errors if now - e["ts"] <= 60.0)
    total_count_recent = sum(1 for r in metrics.recent_requests if now - r["ts"] <= 60.0)

    return {
        "ts": now,
        "overview": {
            "total_devices": len(devices),
            "online_devices": online_devices,
            "offline_devices": len(devices) - online_devices,
            "active_clients": len(active_clients),
            "requests_per_sec": len(recent_1s),
            "avg_response_time_ms": round(sum(lat) / len(lat), 2) if lat else 0.0,
            "active_alarms": active_alarms,
        },
        "traffic": {
            "requests_total": metrics.requests_total,
            "reads_total": metrics.reads_total,
            "writes_total": metrics.writes_total,
            "requests_by_service": dict(metrics.requests_by_service),
            "broadcast": metrics.requests_broadcast,
            "unicast": metrics.requests_unicast,
            "top_devices": [
                {"device_instance": did, "name": device_names.get(did, f"#{did}"), "count": c}
                for did, c in top_devices
            ],
            "recent_requests": list(metrics.recent_requests)[-100:],
        },
        "devices": {
            "list": [
                {
                    "id": d["id"],
                    "device_instance": d["device_instance"],
                    "name": d["name"],
                    "enabled": bool(d.get("enabled")),
                    "object_count": len(engine.app._virtual_object_lists.get(d["device_instance"], [])) if engine and engine.app else 0,
                    "activity": device_activity.get(d["device_instance"], 0),
                }
                for d in devices
            ],
            "uptime_seconds": engine.state.elapsed_seconds if engine else 0,
        },
        "objects": {
            "total": len(all_objids),
            "unused": unused_objects,
            "top_accessed": [{"object": k, "count": c} for k, c in top_objects],
            "reads_total": sum(metrics.object_reads.values()),
            "writes_total": sum(metrics.object_writes.values()),
        },
        "performance": {
            "avg_response_time_ms": round(sum(lat) / len(lat), 2) if lat else 0.0,
            "p95_response_time_ms": round(_percentile(lat, 0.95), 2),
            "throughput_per_sec": len(recent_1s),
            "concurrent_clients": len(active_clients),
            "cpu_percent": _PROCESS.cpu_percent(interval=None),
            "memory_mb": round(_PROCESS.memory_info().rss / (1024 * 1024), 1),
            "error_rate_percent": round(100 * error_count_recent / total_count_recent, 2) if total_count_recent else 0.0,
        },
        "errors": {
            "total": sum(metrics.errors_by_type.values()),
            "by_type": dict(metrics.errors_by_type),
            "duplicate_device_ids": list(metrics.duplicate_id_events)[-20:],
            "recent": list(metrics.recent_errors)[-50:],
        },
        "discovery": {
            "who_is_total": metrics.discovery_total,
            "devices_seen": len(metrics.iam_seen),
            "new_devices_timeline": list(metrics.new_devices_timeline)[-50:],
        },
    }


async def broadcast_metrics() -> None:
    if not metrics_ws_clients:
        return
    snapshot = await build_metrics_snapshot()
    data = json.dumps(snapshot)
    dead = []
    for ws in metrics_ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        metrics_ws_clients.remove(ws)


# ─── Background tasks ─────────────────────────────────────────────────────────

async def tick_loop(fault_detection_engine: FaultDetectionEngine | None,
    energy_engine: EnergyEngine | None,) -> None:
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await engine.tick()

            # engine.tick() already no-ops while paused/stopped (present
            # values stay frozen) -- fault/energy evaluation must respect
            # the same gate, otherwise energy totals keep accumulating
            # kWh for elapsed time that, per the frozen clock, never
            # actually passed.
            if engine.clock_state == "running":
                if fault_detection_engine is not None:
                    await fault_detection_engine.evaluate_all()

                if energy_engine is not None:
                    await energy_engine.evaluate_all(
                        elapsed_seconds=TICK_SECONDS,
                    )

            await broadcast_state()
            state = engine.get_state()
            for dev in state.get("devices", []):
                vals = "  ".join(
                    f"{o['name']}={o['value']:.2f}" if isinstance(o["value"], float) else f"{o['name']}={o['value']}"
                    for o in dev["objects"]
                )
                log.info("[%s]  %s", dev["name"], vals)
        except Exception as e:
            log.error("Tick error: %s", e)


async def metrics_loop() -> None:
    # Deliberately independent of TICK_SECONDS/tick_loop — device-value
    # simulation and analytics refresh are different concerns with different
    # natural cadences (5s vs 1s), and coupling them would mean either
    # slowing down analytics or speeding up (and adding load to) the actual
    # device simulation just to serve the dashboard.
    while True:
        await asyncio.sleep(1.0)
        try:
            await broadcast_metrics()
        except Exception as e:
            log.error("Metrics tick error: %s", e)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, engine
    db = Database(DB_PATH)
    await asyncio.to_thread(db.setup)
    await asyncio.to_thread(db.seed_default)
    for d in db.get_devices():
        _device_names[d["id"]] = d["name"]
    engine = SimEngine(db)

     # Expose shared runtime objects to extracted routers.
    app.state.db = db
    app.state.engine = engine
    app.state.packet_capture = packet_capture
 

    app.state.get_current_user = get_current_user
    app.state.log_event = _log_event

    app.state.device_names = _device_names
    app.state.effective_can_receive_events = (
        _effective_can_receive_events
    )

    app.state.build_metrics_snapshot = (
    build_metrics_snapshot
    )

    app.state.user_from_token = user_from_token
    app.state.hash_password = hash_password
    app.state.verify_password = verify_password
    app.state.create_access_token = create_access_token

    app.state.device_logs = _device_logs
    app.state.global_log = _global_log
    app.state.get_device_logs = (
    get_device_log_entries
    )
    app.state.get_global_logs = (
        get_global_log_entries
    )

    app.state.ws_clients = ws_clients
    app.state.metrics_ws_clients = metrics_ws_clients
    app.state.packet_stream_ws_clients = packet_stream_ws_clients


    fault_detection_engine = FaultDetectionEngine(
        database=db,
        simulation_engine=engine,
        registry=build_default_registry(),
        event_callback=_log_event,
    )

    energy_engine = EnergyEngine(
        database=db,
        simulation_engine=engine,
        event_callback=_log_event,
        history_interval_seconds=60.0,
        history_retention_days=7,
        audit_log_interval_seconds=60.0,
    )

    app.state.energy_engine = energy_engine

    app.state.fault_detection_engine = fault_detection_engine


    

    _apply_settings_live(await asyncio.to_thread(db.get_settings))
    await engine.start()
    tick_task = asyncio.create_task(tick_loop(fault_detection_engine,energy_engine))
    metrics_task = asyncio.create_task(metrics_loop())
    log.info("BACnet Simulator API ready on port %d", SIM_API_PORT)
    yield
    log.info("Shutting down")
    tick_task.cancel()
    metrics_task.cancel()
    try:
        await tick_task
    except asyncio.CancelledError:
        pass
    await engine.stop()


# ─── FastAPI app ──────────────────────────────────────────────────────────────

api = FastAPI(title="BACnet Simulator", lifespan=lifespan)

api.include_router(packet_capture_router)
api.include_router(backups_router)
api.include_router(locations_router)
api.include_router(semantic_router)
api.include_router(calendars_router)
api.include_router(alarms_router)
api.include_router(trend_logs_router)
api.include_router(schedules_router)
api.include_router(devices_router)
api.include_router(analytics_router)
api.include_router(auth_router)
api.include_router(exports_router)
api.include_router(events_router)
api.include_router(objects_router)
api.include_router(projects_router)
api.include_router(simulation_router)
api.include_router(websocket_router)
api.include_router(fault_router)
api.include_router(energy_router)
api.include_router(discovery_router)
api.include_router(external_objects_router)
api.include_router(semantic_suggestions_router)
api.include_router(functional_tests_router)
api.include_router(functional_test_runs_router)

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@api.middleware("http")
async def auth_gate(request: Request, call_next):
    """Require a valid bearer token for everything except the login/setup
    flow and the static admin SPA shell (see _is_public_path)."""
    if request.method == "OPTIONS" or _is_public_path(request.url.path):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer ") or not user_from_token(auth_header[7:].strip()):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return await call_next(request)


ADMIN_DIST = Path(__file__).parent.parent / "admin" / "dist"
ADMIN_PUBLIC = Path(__file__).parent.parent / "admin" / "public"


@api.get("/", include_in_schema=False)
async def admin_root():
    f = ADMIN_DIST / "index.html"
    if f.exists():
        return FileResponse(str(f), media_type="text/html")
    return {"message": "Admin not built. Run: cd admin && npm ci && npm run build"}


@api.get("/favicon.svg", include_in_schema=False)
async def admin_favicon():
    f = ADMIN_PUBLIC / "favicon.svg"
    if f.exists():
        return FileResponse(str(f), media_type="image/svg+xml")
    f = ADMIN_DIST / "favicon.svg"
    if f.exists():
        return FileResponse(str(f), media_type="image/svg+xml")
    from fastapi import HTTPException
    raise HTTPException(status_code=404)


@api.get("/bacnet-vendors.json", include_in_schema=False)
async def bacnet_vendors():
    f = ADMIN_DIST / "bacnet-vendors.json"
    if f.exists():
        return FileResponse(str(f), media_type="application/json")
    return JSONResponse({"vendors": []})



@api.get("/meta")
async def meta():
    # All virtual devices share this simulator's single BACnet/IP socket —
    # there's no per-device address, so notification recipients that target
    # one of our own devices all resolve to this same network address.
    own_ip = engine.app._own_ip if engine and engine.app else None
    return {
        "object_types": sorted(VALID_OBJECT_TYPES),
        "behaviors": sorted(VALID_BEHAVIORS),
        "units": BACNET_UNITS,
        "reliability_options": sorted(VALID_RELIABILITY),
        "polarity_options": sorted(VALID_POLARITY),
        "segmentation_options": sorted(VALID_SEGMENTATION),
        "brick_version": BRICK_VERSION,
        "equipment_types": [{"value": k, "label": v} for k, v in sorted(EQUIPMENT_TYPES.items())],
        "point_types": [{"value": k, "label": v} for k, v in sorted(POINT_TYPES.items())],
        "location_kinds": [{"value": k, "label": v} for k, v in sorted(LOCATION_KINDS.items())],
        "semantic_predicates": [{"value": k, "label": v} for k, v in sorted(SEMANTIC_PREDICATES.items())],
        "energy_model_types": [{"value": k, "label": v} for k, v in MODEL_TYPE_LABELS.items()],
        "network_address": f"{own_ip}:{BACNET_PORT}" if own_ip and own_ip != "0.0.0.0" else None,
    }


@api.get("/settings")
async def get_settings():
    return await asyncio.to_thread(db.get_settings)


@api.put("/settings")
async def update_settings(body: SettingsPayload):
    await asyncio.to_thread(db.save_settings, body.model_dump())
    _apply_settings_live(body.model_dump())
    return body



@api.post("/devices/{device_id}/import/ede")
async def import_device_ede(device_id: int, file: UploadFile = File(...)):
    device = await asyncio.to_thread(db.get_device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    reject_external_device(device)
    text = (await file.read()).decode("utf-8", errors="replace")
    rows = ede.parse_ede_rows(text)
    instances = sorted({row["device_instance"] for row in rows})
    if len(instances) > 1:
        raise HTTPException(
            400,
            f"This EDE file covers {len(instances)} devices (instances {instances}) — "
            "importing it into a single device would merge them and could overwrite "
            "points that collide by object type/instance. Use the project-level EDE "
            "import instead so each device is created separately.",
        )
    objects = [
        {k: v for k, v in row.items() if k != "device_instance"}
        for row in rows
    ]
    count = await asyncio.to_thread(db.import_ede_objects, device_id, objects)
    asyncio.create_task(engine.reload())
    return {"ok": True, "objects_imported": count}


@api.post("/profiles/import/ede", status_code=201)
async def import_project_ede(
    name: str = Form(...),
    description: str = Form(""),
    device_name: str = Form(""),
    file: UploadFile = File(...),
):
    text = (await file.read()).decode("utf-8", errors="replace")
    rows = ede.parse_ede_rows(text)
    if not rows:
        raise HTTPException(400, "No valid EDE rows found in file")
    data = ede.rows_to_devices(rows, device_name)
    return await asyncio.to_thread(db.import_project, name, description, data)



# ── Admin static assets (Vite build output) ──
# Must be mounted after all API routes so API paths take precedence.
_assets_dir = ADMIN_DIST / "assets"
if _assets_dir.exists():
    api.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="admin-assets")


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main():
    config = uvicorn.Config(api, host="0.0.0.0", port=SIM_API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
