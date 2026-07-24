"""
SQLite persistence + auth for the OPC UA Simulator's management API.

Schema and auth logic mirror the sibling BACnet simulator
(bacnet-simulator/bacnet_simulator.py's Database class and auth functions)
adapted for a Device -> Tag hierarchy instead of BACnet's Device -> Object.
"""
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt
import jwt

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "opcua_sim.db"

VALID_DATA_TYPES = {"Boolean", "Double", "Int32", "String"}
VALID_BEHAVIORS = {"constant", "sine", "noise", "random_walk", "manual", "schedule", "ramp", "fault"}

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "24"))
_JWT_SECRET_FILE = DATA_DIR / ".jwt_secret"
_jwt_secret_cache: Optional[str] = None


def _get_jwt_secret() -> str:
    """Resolve the JWT signing secret: env override, else a persisted random
    value in DATA_DIR so tokens survive process restarts."""
    global _jwt_secret_cache
    if _jwt_secret_cache is not None:
        return _jwt_secret_cache
    env_secret = os.environ.get("JWT_SECRET")
    if env_secret:
        _jwt_secret_cache = env_secret
        return _jwt_secret_cache
    _JWT_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _JWT_SECRET_FILE.exists():
        _jwt_secret_cache = _JWT_SECRET_FILE.read_text().strip()
    else:
        _jwt_secret_cache = secrets.token_hex(32)
        _JWT_SECRET_FILE.write_text(_jwt_secret_cache)
    return _jwt_secret_cache


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "device"


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
        path_obj = Path(self.path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    manufacturer TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    data_type TEXT NOT NULL DEFAULT 'Double',
                    writable INTEGER NOT NULL DEFAULT 0,
                    unit TEXT NOT NULL DEFAULT '',
                    behavior TEXT NOT NULL DEFAULT 'constant',
                    behavior_params TEXT NOT NULL DEFAULT '{"value":0}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    manual_value REAL,
                    UNIQUE(device_id, name)
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

                CREATE TABLE IF NOT EXISTS nodeset_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_filename TEXT NOT NULL,
                    device_ids TEXT NOT NULL DEFAULT '[]',
                    device_count INTEGER NOT NULL DEFAULT 0,
                    tag_count INTEGER NOT NULL DEFAULT 0,
                    warning_count INTEGER NOT NULL DEFAULT 0,
                    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)

    # ── Devices ──────────────────────────────────────────────────────────────

    def get_devices(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM devices ORDER BY name")]

    def get_device(self, device_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            return dict(r) if r else None

    def _unique_key(self, conn: sqlite3.Connection, name: str, exclude_id: Optional[int] = None) -> str:
        base = _slugify(name)
        key = base
        suffix = 2
        while True:
            row = conn.execute(
                "SELECT id FROM devices WHERE key=?" + (" AND id != ?" if exclude_id else ""),
                (key, exclude_id) if exclude_id else (key,),
            ).fetchone()
            if not row:
                return key
            key = f"{base}-{suffix}"
            suffix += 1

    def create_device(self, name: str, description: str = "", manufacturer: str = "",
                       model: str = "", enabled: bool = True) -> dict:
        with self._conn() as conn:
            key = self._unique_key(conn, name)
            cur = conn.execute(
                "INSERT INTO devices (key, name, description, manufacturer, model, enabled) "
                "VALUES (?,?,?,?,?,?)",
                (key, name, description, manufacturer, model, 1 if enabled else 0),
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM devices WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_device(self, device_id: int, name: str, description: str = "",
                       manufacturer: str = "", model: str = "", enabled: bool = True) -> Optional[dict]:
        with self._conn() as conn:
            existing = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if not existing:
                return None
            key = existing["key"] if existing["name"] == name else self._unique_key(conn, name, exclude_id=device_id)
            conn.execute(
                "UPDATE devices SET key=?, name=?, description=?, manufacturer=?, model=?, enabled=? WHERE id=?",
                (key, name, description, manufacturer, model, 1 if enabled else 0, device_id),
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone())

    def delete_device(self, device_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── Tags ─────────────────────────────────────────────────────────────────

    def get_tags(self, device_id: int) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM tags WHERE device_id=? ORDER BY name", (device_id,)
            )]

    def get_tag(self, device_id: int, tag_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM tags WHERE id=? AND device_id=?", (tag_id, device_id)
            ).fetchone()
            return dict(r) if r else None

    def create_tag(self, device_id: int, name: str, data_type: str, writable: bool,
                    unit: str, behavior: str, behavior_params: str, enabled: bool) -> dict:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO tags (device_id, name, data_type, writable, unit, behavior, "
                "behavior_params, enabled) VALUES (?,?,?,?,?,?,?,?)",
                (device_id, name, data_type, 1 if writable else 0, unit, behavior,
                 behavior_params, 1 if enabled else 0),
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM tags WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_tag(self, device_id: int, tag_id: int, name: str, data_type: str, writable: bool,
                    unit: str, behavior: str, behavior_params: str, enabled: bool) -> Optional[dict]:
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id FROM tags WHERE id=? AND device_id=?", (tag_id, device_id)
            ).fetchone()
            if not existing:
                return None
            conn.execute(
                "UPDATE tags SET name=?, data_type=?, writable=?, unit=?, behavior=?, "
                "behavior_params=?, enabled=? WHERE id=?",
                (name, data_type, 1 if writable else 0, unit, behavior, behavior_params,
                 1 if enabled else 0, tag_id),
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone())

    def delete_tag(self, device_id: int, tag_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM tags WHERE id=? AND device_id=?", (tag_id, device_id))
            conn.commit()
            return cur.rowcount > 0

    def set_manual_value(self, device_id: int, tag_id: int, value) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tags SET manual_value=? WHERE id=? AND device_id=?", (value, tag_id, device_id)
            )
            conn.commit()
            return cur.rowcount > 0

    # ── Profiles ─────────────────────────────────────────────────────────────

    def get_profiles(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles ORDER BY created_at DESC"
            )]

    def get_profile(self, profile_id: int) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
            return dict(r) if r else None

    def _snapshot_data(self, conn: sqlite3.Connection) -> tuple[str, int]:
        import json
        devices = [dict(r) for r in conn.execute("SELECT * FROM devices")]
        for d in devices:
            d["tags"] = [dict(r) for r in conn.execute(
                "SELECT * FROM tags WHERE device_id=?", (d["id"],)
            )]
        return json.dumps({"devices": devices}), len(devices)

    def save_profile(self, name: str, description: str = "") -> dict:
        with self._conn() as conn:
            data, count = self._snapshot_data(conn)
            cur = conn.execute(
                "INSERT INTO profiles (name, description, device_count, data) VALUES (?,?,?,?)",
                (name, description, count, data),
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles WHERE id=?",
                (cur.lastrowid,),
            ).fetchone())

    def update_profile(self, profile_id: int, name: str, description: str) -> bool:
        with self._conn() as conn:
            data, count = self._snapshot_data(conn)
            cur = conn.execute(
                "UPDATE profiles SET name=?, description=?, device_count=?, data=? WHERE id=?",
                (name, description, count, data, profile_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_profile(self, profile_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
            conn.commit()
            return cur.rowcount > 0

    def import_profile(self, name: str, description: str, data: dict) -> dict:
        import json
        device_count = len(data.get("devices", []))
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO profiles (name, description, device_count, data) VALUES (?,?,?,?)",
                (name, description, device_count, json.dumps(data)),
            )
            conn.commit()
            return dict(conn.execute(
                "SELECT id, name, description, created_at, device_count FROM profiles WHERE id=?",
                (cur.lastrowid,),
            ).fetchone())

    def replace_live_state(self, data: dict) -> None:
        """Destructively replace all devices/tags with a profile's snapshot."""
        with self._conn() as conn:
            conn.execute("DELETE FROM devices")
            for d in data.get("devices", []):
                key = self._unique_key(conn, d["name"])
                cur = conn.execute(
                    "INSERT INTO devices (key, name, description, manufacturer, model, enabled) "
                    "VALUES (?,?,?,?,?,?)",
                    (key, d["name"], d.get("description", ""), d.get("manufacturer", ""),
                     d.get("model", ""), d.get("enabled", 1)),
                )
                device_id = cur.lastrowid
                for t in d.get("tags", []):
                    conn.execute(
                        "INSERT INTO tags (device_id, name, data_type, writable, unit, behavior, "
                        "behavior_params, enabled, manual_value) VALUES (?,?,?,?,?,?,?,?,?)",
                        (device_id, t["name"], t.get("data_type", "Double"), t.get("writable", 0),
                         t.get("unit", ""), t.get("behavior", "constant"),
                         t.get("behavior_params", '{"value":0}'), t.get("enabled", 1),
                         t.get("manual_value")),
                    )
            conn.commit()

    # ── NodeSet imports ──────────────────────────────────────────────────────

    def create_nodeset_import(self, source_filename: str, device_ids: list[int],
                               device_count: int, tag_count: int, warning_count: int) -> int:
        import json
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO nodeset_imports (source_filename, device_ids, device_count, "
                "tag_count, warning_count) VALUES (?,?,?,?,?)",
                (source_filename, json.dumps(device_ids), device_count, tag_count, warning_count),
            )
            conn.commit()
            return cur.lastrowid

    def get_nodeset_imports(self) -> list[dict]:
        import json
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM nodeset_imports ORDER BY imported_at DESC"
            )]
        for r in rows:
            r["device_ids"] = json.loads(r["device_ids"])
        return rows

    def get_nodeset_import(self, import_id: int) -> Optional[dict]:
        import json
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM nodeset_imports WHERE id=?", (import_id,)).fetchone()
        if not r:
            return None
        row = dict(r)
        row["device_ids"] = json.loads(row["device_ids"])
        return row

    def delete_nodeset_import(self, import_id: int) -> Optional[list[int]]:
        """Deletes only the devices this import batch created (cascades to
        their tags via the FK). Devices already removed independently since
        the import are skipped, not treated as an error. Returns the device
        ids actually deleted, or None if the import batch itself doesn't exist."""
        row = self.get_nodeset_import(import_id)
        if row is None:
            return None
        with self._conn() as conn:
            deleted = []
            for device_id in row["device_ids"]:
                cur = conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
                if cur.rowcount > 0:
                    deleted.append(device_id)
            conn.execute("DELETE FROM nodeset_imports WHERE id=?", (import_id,))
            conn.commit()
            return deleted

    # ── Users ────────────────────────────────────────────────────────────────

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


# ─── Auth ─────────────────────────────────────────────────────────────────────

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


def user_from_token(db: Database, token: str) -> Optional[dict]:
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
