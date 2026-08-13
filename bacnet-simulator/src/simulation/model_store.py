from __future__ import annotations

import json
import sqlite3
from typing import Any


def ensure_simulation_model_schema(database: Any) -> None:
    """
    Additive persistence for simulation-model instances.

    This helper keeps the first implementation independent from legacy.py.
    Long term, these CREATE TABLE statements can be moved into Database.setup().
    """
    with database._conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS simulation_model_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                model_type TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                parameters TEXT NOT NULL DEFAULT '{}',
                created_from_device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_sim_model_configs_provider
                ON simulation_model_configs(provider_type, model_type);

            CREATE INDEX IF NOT EXISTS idx_sim_model_configs_created_from_device
                ON simulation_model_configs(created_from_device_id);

            CREATE TABLE IF NOT EXISTS simulation_model_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_config_id INTEGER NOT NULL
                    REFERENCES simulation_model_configs(id) ON DELETE CASCADE,
                variable TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('input', 'output')),
                point_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
                UNIQUE(model_config_id, variable, direction)
            );

            CREATE INDEX IF NOT EXISTS idx_sim_model_mappings_model
                ON simulation_model_mappings(model_config_id);

            CREATE INDEX IF NOT EXISTS idx_sim_model_mappings_point
                ON simulation_model_mappings(point_id);

            -- One explicit simulation-model output owner per point.
            -- Built-in is fallback ownership and is not stored here.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sim_model_output_owner
                ON simulation_model_mappings(point_id)
                WHERE direction='output';
            """
        )
        conn.commit()


def _decode_config(row: Any) -> dict[str, Any]:
    result = dict(row)
    try:
        result["parameters"] = json.loads(result.get("parameters") or "{}")
    except (TypeError, json.JSONDecodeError):
        result["parameters"] = {}
    result["enabled"] = bool(result.get("enabled"))
    return result


def _load_mappings(conn: sqlite3.Connection, model_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            m.id,
            m.model_config_id,
            m.variable,
            m.direction,
            m.point_id,
            o.device_id,
            o.name AS point_name,
            o.object_type,
            o.object_instance,
            o.units,
            o.point_type,
            d.name AS device_name
        FROM simulation_model_mappings m
        JOIN objects o ON o.id = m.point_id
        JOIN devices d ON d.id = o.device_id
        WHERE m.model_config_id=?
        ORDER BY
            CASE m.direction WHEN 'input' THEN 0 ELSE 1 END,
            m.variable
        """,
        (model_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_simulation_model(database: Any, model_id: int) -> dict | None:
    ensure_simulation_model_schema(database)
    with database._conn() as conn:
        row = conn.execute(
            "SELECT * FROM simulation_model_configs WHERE id=?",
            (model_id,),
        ).fetchone()
        if row is None:
            return None
        result = _decode_config(row)
        result["mappings"] = _load_mappings(conn, model_id)
        return result


def list_simulation_models(
    database: Any,
    *,
    created_from_device_id: int | None = None,
) -> list[dict]:
    ensure_simulation_model_schema(database)
    with database._conn() as conn:
        if created_from_device_id is None:
            rows = conn.execute(
                "SELECT * FROM simulation_model_configs ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM simulation_model_configs
                WHERE created_from_device_id=?
                ORDER BY id
                """,
                (created_from_device_id,),
            ).fetchall()

        result: list[dict] = []
        for row in rows:
            item = _decode_config(row)
            item["mappings"] = _load_mappings(conn, int(item["id"]))
            result.append(item)
        return result


def list_enabled_simulation_models(database: Any) -> list[dict]:
    ensure_simulation_model_schema(database)
    return [
        model
        for model in list_simulation_models(database)
        if model["enabled"]
    ]


def _replace_mappings(
    conn: sqlite3.Connection,
    model_id: int,
    mappings: list[dict],
) -> None:
    conn.execute(
        "DELETE FROM simulation_model_mappings WHERE model_config_id=?",
        (model_id,),
    )

    for mapping in mappings:
        conn.execute(
            """
            INSERT INTO simulation_model_mappings
                (model_config_id, variable, direction, point_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                model_id,
                str(mapping["variable"]),
                str(mapping["direction"]),
                int(mapping["point_id"]),
            ),
        )


def create_simulation_model(
    database: Any,
    *,
    name: str,
    provider_type: str,
    model_type: str,
    enabled: bool,
    parameters: dict,
    created_from_device_id: int | None,
    mappings: list[dict],
) -> dict:
    ensure_simulation_model_schema(database)
    with database._conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO simulation_model_configs
                (name, provider_type, model_type, enabled, parameters, created_from_device_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                provider_type,
                model_type,
                int(enabled),
                json.dumps(parameters),
                created_from_device_id,
            ),
        )
        model_id = int(cur.lastrowid)
        _replace_mappings(conn, model_id, mappings)
        conn.commit()

    result = get_simulation_model(database, model_id)
    if result is None:
        raise RuntimeError("Simulation model was not saved")
    return result


def update_simulation_model(
    database: Any,
    model_id: int,
    *,
    name: str,
    provider_type: str,
    model_type: str,
    enabled: bool,
    parameters: dict,
    created_from_device_id: int | None,
    mappings: list[dict],
) -> dict | None:
    ensure_simulation_model_schema(database)
    with database._conn() as conn:
        existing = conn.execute(
            "SELECT id FROM simulation_model_configs WHERE id=?",
            (model_id,),
        ).fetchone()
        if existing is None:
            return None

        conn.execute(
            """
            UPDATE simulation_model_configs
            SET name=?,
                provider_type=?,
                model_type=?,
                enabled=?,
                parameters=?,
                created_from_device_id=?,
                updated_at=datetime('now')
            WHERE id=?
            """,
            (
                name,
                provider_type,
                model_type,
                int(enabled),
                json.dumps(parameters),
                created_from_device_id,
                model_id,
            ),
        )
        _replace_mappings(conn, model_id, mappings)
        conn.commit()

    return get_simulation_model(database, model_id)


def delete_simulation_model(database: Any, model_id: int) -> bool:
    ensure_simulation_model_schema(database)
    with database._conn() as conn:
        cur = conn.execute(
            "DELETE FROM simulation_model_configs WHERE id=?",
            (model_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def get_explicit_output_owner(
    database: Any,
    point_id: int,
    *,
    excluding_model_id: int | None = None,
) -> dict | None:
    ensure_simulation_model_schema(database)
    sql = """
        SELECT c.id, c.name, c.provider_type, c.model_type
        FROM simulation_model_mappings m
        JOIN simulation_model_configs c ON c.id=m.model_config_id
        WHERE m.direction='output' AND m.point_id=?
    """
    params: list[Any] = [point_id]
    if excluding_model_id is not None:
        sql += " AND c.id<>?"
        params.append(excluding_model_id)

    with database._conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
