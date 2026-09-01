from __future__ import annotations

import json
import sqlite3
from typing import Any

from .remote_catalog import normalize_remote_model_id


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
                conversion TEXT,
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

            -- An FMU input driven by an operation ("max", "min", or
            -- "weighted_average") over several BACnet points' live values,
            -- rather than exactly one point or a constant. Kept as separate
            -- tables (rather than widening simulation_model_mappings.point_id
            -- to hold a list) since point_id there is NOT NULL and singular
            -- -- changing that would need SQLite's risky rename/rebuild
            -- migration path. `operation` has no CHECK constraint: its
            -- vocabulary is enforced at the Pydantic layer so adding
            -- avg/sum later needs no schema change.
            CREATE TABLE IF NOT EXISTS simulation_model_aggregate_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_config_id INTEGER NOT NULL
                    REFERENCES simulation_model_configs(id) ON DELETE CASCADE,
                variable TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction = 'input'),
                operation TEXT NOT NULL,
                UNIQUE(model_config_id, variable, direction)
            );

            CREATE INDEX IF NOT EXISTS idx_sim_model_agg_mappings_model
                ON simulation_model_aggregate_mappings(model_config_id);

            -- point_id is ON DELETE RESTRICT (not CASCADE, unlike ordinary
            -- simulation_model_mappings.point_id): silently dropping one
            -- member out of several would let MAX(A,B,C) quietly become
            -- MAX(A,B) -- still a plausible-looking value with nothing
            -- signaling that a configured source vanished. RESTRICT makes
            -- that state unreachable: deleting a point (or a device that
            -- owns one) that's still an aggregate member fails outright.
            -- See src/api/routers/objects.py::delete_object and
            -- src/api/routers/devices.py::delete_device for the
            -- corresponding clean-error translation. weight_point_id
            -- (added for "weighted_average", see below) is under the same
            -- RESTRICT policy for the identical reason -- a weight point
            -- vanishing out from under a live weighted_average would
            -- silently change which pairs participate.
            CREATE TABLE IF NOT EXISTS simulation_model_aggregate_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aggregate_mapping_id INTEGER NOT NULL
                    REFERENCES simulation_model_aggregate_mappings(id) ON DELETE CASCADE,
                point_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE RESTRICT,
                UNIQUE(aggregate_mapping_id, point_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sim_model_agg_members_mapping
                ON simulation_model_aggregate_members(aggregate_mapping_id);

            CREATE INDEX IF NOT EXISTS idx_sim_model_agg_members_point
                ON simulation_model_aggregate_members(point_id);

            -- Mirrors an already-resolved model INPUT value (Point,
            -- Aggregate, or Constant -- whichever currently sources
            -- `variable`) onto a second BACnet point's Present Value. Kept
            -- as its own table rather than a third simulation_model_mappings
            -- direction (e.g. 'expose'): that table's direction column,
            -- point-scoped output-owner index, and every "direction=='input'
            -- vs 'output'" check throughout runtime.py/fmu.py would
            -- all need auditing for a value that is simultaneously read-
            -- adjacent (keyed off an input variable) and write-adjacent
            -- (claims output ownership of a point) -- a separate table needs
            -- none of that. point_id is ON DELETE CASCADE (unlike an
            -- aggregate member's point_id): an exposure is a convenience
            -- mirror, not a required source -- losing the target point just
            -- means nothing gets mirrored anymore, the same as deleting an
            -- ordinary output mapping's point today.
            CREATE TABLE IF NOT EXISTS simulation_model_input_exposures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_config_id INTEGER NOT NULL
                    REFERENCES simulation_model_configs(id) ON DELETE CASCADE,
                variable TEXT NOT NULL,
                point_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
                UNIQUE(model_config_id, variable)
            );

            CREATE INDEX IF NOT EXISTS idx_sim_model_input_exposures_model
                ON simulation_model_input_exposures(model_config_id);

            -- One explicit exposure writer per point -- same "one writer"
            -- invariant idx_sim_model_output_owner enforces for plain
            -- output mappings, just in this table's own domain. A point
            -- being simultaneously an output-mapping target AND an
            -- exposure target is a separate, cross-table conflict checked
            -- at the API validation layer (get_explicit_output_owner +
            -- get_explicit_exposure_owner), since SQLite can't express a
            -- uniqueness constraint spanning two tables.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sim_model_input_exposure_owner
                ON simulation_model_input_exposures(point_id);
            """
        )

        # Additive migration: weight_point_id was added to
        # simulation_model_aggregate_members after the table first shipped,
        # for the "weighted_average" aggregate operation -- one weight point
        # per value-point member row, NULL for "max" (and any future
        # non-weighted operation). Backfill for existing DBs instead of
        # requiring a fresh one, same pattern legacy.py's own additive
        # migrations use (PRAGMA table_info check + conditional ALTER
        # TABLE). A plain ALTER TABLE ADD COLUMN with a REFERENCES clause is
        # safe here -- devices.location_id already does the same thing.
        existing_member_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(simulation_model_aggregate_members)")
        }
        if "weight_point_id" not in existing_member_cols:
            conn.execute(
                "ALTER TABLE simulation_model_aggregate_members "
                "ADD COLUMN weight_point_id INTEGER REFERENCES objects(id) ON DELETE RESTRICT"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sim_model_agg_members_weight_point "
                "ON simulation_model_aggregate_members(weight_point_id)"
            )

        # Additive migration: conversion was added to simulation_model_mappings
        # after the table first shipped -- a named value conversion (see
        # mapping/conversions.CONVERSIONS) applied to an FMU output at the
        # mapping boundary, e.g. 'zero_based_to_multistate' for a zero-based
        # FMU state output mapped onto this simulator's strictly 1-based
        # multi-state Present_Value. NULL (no conversion) for every existing
        # row -- same backfill pattern as weight_point_id above.
        existing_mapping_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(simulation_model_mappings)")
        }
        if "conversion" not in existing_mapping_cols:
            conn.execute(
                "ALTER TABLE simulation_model_mappings ADD COLUMN conversion TEXT"
            )

        conn.commit()


def _decode_config(row: Any) -> dict[str, Any]:
    result = dict(row)
    if result.get("provider_type") == "fmu":
        result["model_type"] = normalize_remote_model_id(str(result.get("model_type") or ""))
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
            m.conversion,
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


def _load_aggregate_mappings(conn: sqlite3.Connection, model_id: int) -> list[dict]:
    """Loads aggregate mapping headers + member points and assembles each
    into exactly the dict shape runtime._build_fmu_provider already
    expects (point_ids plural + point_metadata) -- see _is_aggregate_row's
    "point_ids" in mapping discriminator, which is what lets the merged
    list returned by get_simulation_model/list_simulation_models "just
    work" with the runtime layer untouched.

    weight_point_id is LEFT JOINed (it's nullable -- only "weighted_average"
    rows populate it) and surfaced two ways: a "weight_point_ids" list
    positionally parallel to "point_ids" (None where a member has no
    weight), and its metadata folded into the same "point_metadata" dict
    value points already use (keyed by weight_point_id, so a lookup by
    point_id works identically for a value point or a weight point)."""
    agg_rows = conn.execute(
        """
        SELECT id, model_config_id, variable, direction, operation
        FROM simulation_model_aggregate_mappings
        WHERE model_config_id=?
        ORDER BY variable
        """,
        (model_id,),
    ).fetchall()

    result: list[dict] = []
    for agg in agg_rows:
        member_rows = conn.execute(
            """
            SELECT
                am.point_id,
                o.name AS point_name,
                o.device_id,
                o.object_type,
                o.object_instance,
                o.units,
                o.point_type,
                d.name AS device_name,
                am.weight_point_id,
                wo.name AS weight_point_name,
                wo.device_id AS weight_device_id,
                wo.object_type AS weight_object_type,
                wo.object_instance AS weight_object_instance,
                wo.units AS weight_units,
                wo.point_type AS weight_point_type,
                wd.name AS weight_device_name
            FROM simulation_model_aggregate_members am
            JOIN objects o ON o.id = am.point_id
            JOIN devices d ON d.id = o.device_id
            LEFT JOIN objects wo ON wo.id = am.weight_point_id
            LEFT JOIN devices wd ON wd.id = wo.device_id
            WHERE am.aggregate_mapping_id=?
            ORDER BY am.id
            """,
            (agg["id"],),
        ).fetchall()

        point_ids = [int(m["point_id"]) for m in member_rows]
        weight_point_ids = [
            int(m["weight_point_id"]) if m["weight_point_id"] is not None else None
            for m in member_rows
        ]
        point_metadata = {
            int(m["point_id"]): {
                "point_name": m["point_name"],
                "device_name": m["device_name"],
                "device_id": m["device_id"],
                "object_type": m["object_type"],
                "object_instance": m["object_instance"],
                "units": m["units"],
                "point_type": m["point_type"],
            }
            for m in member_rows
        }
        for m in member_rows:
            if m["weight_point_id"] is not None:
                point_metadata[int(m["weight_point_id"])] = {
                    "point_name": m["weight_point_name"],
                    "device_name": m["weight_device_name"],
                    "device_id": m["weight_device_id"],
                    "object_type": m["weight_object_type"],
                    "object_instance": m["weight_object_instance"],
                    "units": m["weight_units"],
                    "point_type": m["weight_point_type"],
                }
        result.append({
            "id": agg["id"],
            "model_config_id": agg["model_config_id"],
            "variable": agg["variable"],
            "direction": agg["direction"],
            "operation": agg["operation"],
            "point_ids": point_ids,
            "weight_point_ids": weight_point_ids,
            "point_metadata": point_metadata,
        })
    return result


def _load_input_exposures(conn: sqlite3.Connection, model_id: int) -> list[dict]:
    """Loads {variable, point_id} exposure rows plus the target point's
    display metadata -- same shape/columns _load_mappings already returns
    for a plain mapping, so the runtime/API/frontend layers can treat an
    exposure's point info identically to any other point reference."""
    rows = conn.execute(
        """
        SELECT
            e.id,
            e.model_config_id,
            e.variable,
            e.point_id,
            o.device_id,
            o.name AS point_name,
            o.object_type,
            o.object_instance,
            o.units,
            o.point_type,
            d.name AS device_name
        FROM simulation_model_input_exposures e
        JOIN objects o ON o.id = e.point_id
        JOIN devices d ON d.id = o.device_id
        WHERE e.model_config_id=?
        ORDER BY e.variable
        """,
        (model_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _bulk_load_mappings(conn: sqlite3.Connection, model_ids: list[int]) -> dict[int, list[dict]]:
    """Batched equivalent of calling _load_mappings once per id in
    model_ids -- one query via a WHERE model_config_id IN (...) instead of
    one query per model. Ordering (input before output, then by variable)
    is preserved by sorting the single result set the same way the
    original per-model ORDER BY did, then grouping by model_config_id."""
    result: dict[int, list[dict]] = {mid: [] for mid in model_ids}
    if not model_ids:
        return result
    placeholders = ",".join("?" * len(model_ids))
    rows = conn.execute(
        f"""
        SELECT
            m.id,
            m.model_config_id,
            m.variable,
            m.direction,
            m.point_id,
            m.conversion,
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
        WHERE m.model_config_id IN ({placeholders})
        ORDER BY
            m.model_config_id,
            CASE m.direction WHEN 'input' THEN 0 ELSE 1 END,
            m.variable
        """,
        model_ids,
    ).fetchall()
    for row in rows:
        result[row["model_config_id"]].append(dict(row))
    return result


def _bulk_load_aggregate_mappings(conn: sqlite3.Connection, model_ids: list[int]) -> dict[int, list[dict]]:
    """Batched equivalent of calling _load_aggregate_mappings once per id
    in model_ids. Same two-level shape (aggregate header -> member points)
    as the original, just fetched via two IN (...) queries total instead
    of 1 + N (one per aggregate header, previously)."""
    result: dict[int, list[dict]] = {mid: [] for mid in model_ids}
    if not model_ids:
        return result
    placeholders = ",".join("?" * len(model_ids))
    agg_rows = conn.execute(
        f"""
        SELECT id, model_config_id, variable, direction, operation
        FROM simulation_model_aggregate_mappings
        WHERE model_config_id IN ({placeholders})
        ORDER BY model_config_id, variable
        """,
        model_ids,
    ).fetchall()
    if not agg_rows:
        return result

    agg_ids = [int(agg["id"]) for agg in agg_rows]
    agg_placeholders = ",".join("?" * len(agg_ids))
    member_rows = conn.execute(
        f"""
        SELECT
            am.aggregate_mapping_id,
            am.point_id,
            o.name AS point_name,
            o.device_id,
            o.object_type,
            o.object_instance,
            o.units,
            o.point_type,
            d.name AS device_name,
            am.weight_point_id,
            wo.name AS weight_point_name,
            wo.device_id AS weight_device_id,
            wo.object_type AS weight_object_type,
            wo.object_instance AS weight_object_instance,
            wo.units AS weight_units,
            wo.point_type AS weight_point_type,
            wd.name AS weight_device_name
        FROM simulation_model_aggregate_members am
        JOIN objects o ON o.id = am.point_id
        JOIN devices d ON d.id = o.device_id
        LEFT JOIN objects wo ON wo.id = am.weight_point_id
        LEFT JOIN devices wd ON wd.id = wo.device_id
        WHERE am.aggregate_mapping_id IN ({agg_placeholders})
        ORDER BY am.aggregate_mapping_id, am.id
        """,
        agg_ids,
    ).fetchall()
    members_by_agg: dict[int, list[Any]] = {aid: [] for aid in agg_ids}
    for m in member_rows:
        members_by_agg[m["aggregate_mapping_id"]].append(m)

    for agg in agg_rows:
        agg_id = int(agg["id"])
        agg_members = members_by_agg[agg_id]
        point_ids = [int(m["point_id"]) for m in agg_members]
        weight_point_ids = [
            int(m["weight_point_id"]) if m["weight_point_id"] is not None else None
            for m in agg_members
        ]
        point_metadata = {
            int(m["point_id"]): {
                "point_name": m["point_name"],
                "device_name": m["device_name"],
                "device_id": m["device_id"],
                "object_type": m["object_type"],
                "object_instance": m["object_instance"],
                "units": m["units"],
                "point_type": m["point_type"],
            }
            for m in agg_members
        }
        for m in agg_members:
            if m["weight_point_id"] is not None:
                point_metadata[int(m["weight_point_id"])] = {
                    "point_name": m["weight_point_name"],
                    "device_name": m["weight_device_name"],
                    "device_id": m["weight_device_id"],
                    "object_type": m["weight_object_type"],
                    "object_instance": m["weight_object_instance"],
                    "units": m["weight_units"],
                    "point_type": m["weight_point_type"],
                }
        result[agg["model_config_id"]].append({
            "id": agg["id"],
            "model_config_id": agg["model_config_id"],
            "variable": agg["variable"],
            "direction": agg["direction"],
            "operation": agg["operation"],
            "point_ids": point_ids,
            "weight_point_ids": weight_point_ids,
            "point_metadata": point_metadata,
        })
    return result


def _bulk_load_input_exposures(conn: sqlite3.Connection, model_ids: list[int]) -> dict[int, list[dict]]:
    """Batched equivalent of calling _load_input_exposures once per id in
    model_ids."""
    result: dict[int, list[dict]] = {mid: [] for mid in model_ids}
    if not model_ids:
        return result
    placeholders = ",".join("?" * len(model_ids))
    rows = conn.execute(
        f"""
        SELECT
            e.id,
            e.model_config_id,
            e.variable,
            e.point_id,
            o.device_id,
            o.name AS point_name,
            o.object_type,
            o.object_instance,
            o.units,
            o.point_type,
            d.name AS device_name
        FROM simulation_model_input_exposures e
        JOIN objects o ON o.id = e.point_id
        JOIN devices d ON d.id = o.device_id
        WHERE e.model_config_id IN ({placeholders})
        ORDER BY e.model_config_id, e.variable
        """,
        model_ids,
    ).fetchall()
    for row in rows:
        result[row["model_config_id"]].append(dict(row))
    return result


def list_all_simulation_models(conn: sqlite3.Connection) -> list[dict]:
    """Same shape as list_simulation_models() (every simulation model, with
    mappings/aggregate_mappings assembled) but takes an already-open
    connection directly instead of a Database wrapper -- for a caller
    (legacy.py's save_project/update_project) that needs this data
    gathered within its OWN already-open transaction rather than a second,
    separately-opened connection. Includes every model regardless of
    provider_type (including legacy 'system' rows), unlike
    list_simulation_models's own default -- a full-project snapshot must
    be complete, not filtered for UI display.

    Uses the _bulk_load_* helpers (3 queries total, or 4 if any model has
    aggregate mappings) instead of _load_mappings/_load_aggregate_mappings/
    _load_input_exposures called once per model (3 queries per model,
    previously) -- save_project/update_project call this on every save, so
    a project with dozens of simulation models was issuing 100+ sequential
    queries just for this step."""
    rows = conn.execute(
        "SELECT * FROM simulation_model_configs ORDER BY id"
    ).fetchall()
    model_ids = [int(row["id"]) for row in rows]
    mappings_by_id = _bulk_load_mappings(conn, model_ids)
    aggregate_mappings_by_id = _bulk_load_aggregate_mappings(conn, model_ids)
    input_exposures_by_id = _bulk_load_input_exposures(conn, model_ids)

    result: list[dict] = []
    for row in rows:
        item = _decode_config(row)
        model_id = int(item["id"])
        item["mappings"] = mappings_by_id[model_id] + aggregate_mappings_by_id[model_id]
        item["input_exposures"] = input_exposures_by_id[model_id]
        result.append(item)
    return result


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
        result["mappings"] = _load_mappings(conn, model_id) + _load_aggregate_mappings(conn, model_id)
        result["input_exposures"] = _load_input_exposures(conn, model_id)
        return result


def list_simulation_models(
    database: Any,
    *,
    created_from_device_id: int | None = None,
    include_legacy_system: bool = False,
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
            if (
                not include_legacy_system
                and item.get("provider_type") == "system"
            ):
                continue
            item["mappings"] = _load_mappings(conn, int(item["id"])) + _load_aggregate_mappings(conn, int(item["id"]))
            item["input_exposures"] = _load_input_exposures(conn, int(item["id"]))
            result.append(item)
        return result


def list_enabled_simulation_models(database: Any) -> list[dict]:
    ensure_simulation_model_schema(database)
    return [
        model
        for model in list_simulation_models(database)
        if model["enabled"]
    ]


# provider_type values register_model_config (runtime.py) can still
# load, plus 'system' -- the legacy provider_type list_simulation_models
# already filters out of every normal listing (include_legacy_system) and
# reconcile_enabled_models never attempts to register. Anything else is a
# provider whose implementation is gone from the codebase (e.g. 'weather',
# removed in 47a1a46 in favor of the FMU-based Weather.mo model) with no
# code path left that can ever load it again.
_REGISTRABLE_OR_LEGACY_PROVIDER_TYPES = ("fmu", "learned", "system")


def purge_unsupported_simulation_models(database: Any) -> list[dict[str, Any]]:
    """
    Deletes any simulation_model_configs row whose provider_type names a
    provider no longer implemented anywhere in this codebase.

    Without this, such a row survives every restart untouched:
    reconcile_enabled_models keeps trying (and failing) to register it,
    logging the same "FMU registration failed (Unsupported provider type:
    ...)" event forever, AND the Simulation Model drawer keeps hydrating
    from it every time it's opened for that device, showing settings for a
    provider that no longer exists and can't be edited or applied. Deleting
    the row (ON DELETE CASCADE takes its mappings/aggregate rows/exposures
    with it) is the correct fix, not a fallback/reconstruction -- there is
    no current schema this data could be migrated into automatically, and
    the device already falls back to built-in behavior for a point with no
    surviving output mapping, same as any other removed simulation model.
    """
    ensure_simulation_model_schema(database)
    placeholders = ",".join("?" for _ in _REGISTRABLE_OR_LEGACY_PROVIDER_TYPES)
    with database._conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, name, provider_type, created_from_device_id
            FROM simulation_model_configs
            WHERE provider_type NOT IN ({placeholders})
            """,
            _REGISTRABLE_OR_LEGACY_PROVIDER_TYPES,
        ).fetchall()
        if rows:
            conn.execute(
                f"DELETE FROM simulation_model_configs WHERE provider_type NOT IN ({placeholders})",
                _REGISTRABLE_OR_LEGACY_PROVIDER_TYPES,
            )
            conn.commit()
    return [dict(row) for row in rows]


def get_active_simulation_models_by_device(database: Any) -> dict[int, dict]:
    """
    Return the enabled explicit simulation model configured for each
    device.

    Keyed by created_from_device_id -- the device a model was configured
    for -- not by whether any of its outputs happen to be mapped to a
    point yet. A model with an FMU selected and running but zero output
    mappings still owns/drives its device (e.g. still consuming inputs,
    still worth showing as active); requiring a mapping made the badge
    disappear for exactly that case. If a device is driven by more than
    one model, keep the first model for the label and expose the count
    so the UI can make that ambiguity visible later if needed.
    """
    ensure_simulation_model_schema(database)
    with database._conn() as conn:
        rows = conn.execute(
            """
            SELECT
                created_from_device_id AS device_id,
                id,
                name,
                provider_type,
                model_type
            FROM simulation_model_configs
            WHERE enabled=1
                AND provider_type<>'system'
                AND created_from_device_id IS NOT NULL
            ORDER BY created_from_device_id, id
            """
        ).fetchall()

    result: dict[int, dict] = {}
    for row in rows:
        device_id = int(row["device_id"])
        model_id = int(row["id"])

        if device_id not in result:
            result[device_id] = {
                "id": model_id,
                "name": row["name"],
                "provider_type": row["provider_type"],
                "model_type": row["model_type"],
                "model_count": 1,
            }
        else:
            result[device_id]["model_count"] += 1
    return result


def get_devices_with_disabled_simulation_model(database: Any) -> set[int]:
    """
    Return device ids whose explicit simulation model exists but is
    currently disabled (stopped).

    Used only to distinguish, in the admin UI's device badge, "device has
    no explicit simulation model at all" from "device's simulation model
    is configured but not running" -- devices with an *enabled* model are
    already covered by get_active_simulation_models_by_device. Keyed by
    created_from_device_id, same reasoning as that function -- a stopped
    model with no output mappings yet should still read as "stopped", not
    silently look like "no model at all".
    """
    ensure_simulation_model_schema(database)
    with database._conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT created_from_device_id AS device_id
            FROM simulation_model_configs
            WHERE enabled=0
                AND provider_type<>'system'
                AND created_from_device_id IS NOT NULL
            """
        ).fetchall()
    return {int(row["device_id"]) for row in rows}


def get_output_owners_by_point(
    database: Any,
    point_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    *,
    excluding_model_id: int | None = None,
) -> dict[int, dict]:
    """Return explicit simulation-model output ownership by point id."""
    ensure_simulation_model_schema(database)
    sql = """
        SELECT
            m.point_id,
            c.id,
            c.name,
            c.provider_type,
            c.model_type,
            m.variable
        FROM simulation_model_mappings m
        JOIN simulation_model_configs c ON c.id=m.model_config_id
        WHERE c.enabled=1 AND m.direction='output'
            AND c.provider_type<>'system'
    """
    params: list[Any] = []
    ids = [int(point_id) for point_id in (point_ids or [])]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        sql += f" AND m.point_id IN ({placeholders})"
        params.extend(ids)
    if excluding_model_id is not None:
        sql += " AND c.id<>?"
        params.append(int(excluding_model_id))

    with database._conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return {
            int(row["point_id"]): {
                "id": int(row["id"]),
                "name": row["name"],
                "provider_type": row["provider_type"],
                "model_type": row["model_type"],
                "variable": row["variable"],
            }
            for row in rows
        }


def get_exposure_owners_by_point(
    database: Any,
    point_ids: list[int] | set[int] | tuple[int, ...] | None = None,
    *,
    excluding_model_id: int | None = None,
) -> dict[int, dict]:
    """Return explicit simulation-model input-exposure ownership by point
    id. Mirrors get_output_owners_by_point's shape/filters exactly -- used
    alongside it so a point driven by a mirrored INPUT value (rather than a
    plain OUTPUT mapping) is still surfaced as FMU/Learned-driven in the
    UI's Behavior column, instead of falling through to that point's raw,
    stale `behavior` field."""
    ensure_simulation_model_schema(database)
    sql = """
        SELECT
            e.point_id,
            c.id,
            c.name,
            c.provider_type,
            c.model_type,
            e.variable
        FROM simulation_model_input_exposures e
        JOIN simulation_model_configs c ON c.id=e.model_config_id
        WHERE c.enabled=1
            AND c.provider_type<>'system'
    """
    params: list[Any] = []
    ids = [int(point_id) for point_id in (point_ids or [])]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        sql += f" AND e.point_id IN ({placeholders})"
        params.extend(ids)
    if excluding_model_id is not None:
        sql += " AND c.id<>?"
        params.append(int(excluding_model_id))

    with database._conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return {
            int(row["point_id"]): {
                "id": int(row["id"]),
                "name": row["name"],
                "provider_type": row["provider_type"],
                "model_type": row["model_type"],
                "variable": row["variable"],
            }
            for row in rows
        }


def reconcile_provider_owned_raw_behavior(database: Any) -> None:
    """Relabel every currently provider (FMU/learned model)-owned point
    still showing the legacy `behavior='constant'` default to the clearer
    'raw' (see VALID_BEHAVIORS in src/core/config.py). The two mean exactly
    the same thing for a provider-owned point -- a pure passthrough of the
    live value; see SimEngine._apply_fmu_behavior -- so this only changes
    what's displayed in the admin UI, never runtime behavior.

    Called unconditionally from Database.setup() on every app boot -- NOT a
    one-time schema_migrations-tracked migration. That's deliberate: a
    point can become provider-owned at any time after the app first
    started (a user maps a new output on an existing or new simulation
    model), and a one-time historical fixup can never catch those going
    forward. Re-running this on every boot means the very next restart
    after mapping a new point relabels it too, with no per-mapping-
    creation-code-path hook needed. Uses the exact same ownership filters
    as get_output_owners_by_point/get_exposure_owners_by_point above, so a
    point this leaves alone is never one that's actually provider-owned
    today.

    Deliberately does NOT call ensure_simulation_model_schema() -- that
    would unconditionally CREATE the simulation_model_* tables on every
    boot, even for a database that has never used a simulation model at
    all, breaking the fresh-install-vs-migrated-upgrade schema symmetry
    tests/test_db_migrations.py checks. Checking sqlite_master first keeps
    "these tables only ever get created on first real use" intact; a
    database that HAS used simulation models already has them, and one
    that hasn't has nothing for this function to reconcile anyway.
    """
    with database._conn() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('simulation_model_mappings','simulation_model_configs','simulation_model_input_exposures')"
            )
        }
        if len(tables) < 3:
            return
        conn.execute(
            """
            UPDATE objects SET behavior='raw'
            WHERE behavior='constant' AND id IN (
                SELECT m.point_id FROM simulation_model_mappings m
                JOIN simulation_model_configs c ON c.id=m.model_config_id
                WHERE c.enabled=1 AND m.direction='output' AND c.provider_type<>'system'
                UNION
                SELECT e.point_id FROM simulation_model_input_exposures e
                JOIN simulation_model_configs c ON c.id=e.model_config_id
                WHERE c.enabled=1 AND c.provider_type<>'system'
            )
            """
        )
        conn.commit()


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
                (model_config_id, variable, direction, point_id, conversion)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                model_id,
                str(mapping["variable"]),
                str(mapping["direction"]),
                int(mapping["point_id"]),
                str(mapping["conversion"]) if mapping.get("conversion") else None,
            ),
        )


def _replace_aggregate_mappings(
    conn: sqlite3.Connection,
    model_id: int,
    aggregate_mappings: list[dict],
) -> None:
    """DELETE-then-reinsert, same pattern as _replace_mappings. Deleting the
    header rows cascades member rows via ON DELETE CASCADE (the header/member
    relationship, unlike a member's own point_id, is safe to cascade -- it's
    "this aggregate no longer exists", not "one source silently vanished")."""
    conn.execute(
        "DELETE FROM simulation_model_aggregate_mappings WHERE model_config_id=?",
        (model_id,),
    )

    for aggregate in aggregate_mappings:
        cur = conn.execute(
            """
            INSERT INTO simulation_model_aggregate_mappings
                (model_config_id, variable, direction, operation)
            VALUES (?, ?, ?, ?)
            """,
            (
                model_id,
                str(aggregate["variable"]),
                str(aggregate["direction"]),
                str(aggregate["operation"]),
            ),
        )
        aggregate_mapping_id = int(cur.lastrowid)
        # weight_point_ids is positionally parallel to point_ids (None for
        # "max"/non-weighted rows) -- absent entirely on a row that predates
        # this field, hence the fallback to an all-None list of the same
        # length rather than requiring every caller to supply it.
        point_ids = aggregate["point_ids"]
        weight_point_ids = aggregate.get("weight_point_ids") or [None] * len(point_ids)
        for point_id, weight_point_id in zip(point_ids, weight_point_ids):
            conn.execute(
                """
                INSERT INTO simulation_model_aggregate_members
                    (aggregate_mapping_id, point_id, weight_point_id)
                VALUES (?, ?, ?)
                """,
                (
                    aggregate_mapping_id,
                    int(point_id),
                    int(weight_point_id) if weight_point_id is not None else None,
                ),
            )


def _replace_input_exposures(
    conn: sqlite3.Connection,
    model_id: int,
    input_exposures: list[dict],
) -> None:
    """DELETE-then-reinsert, same pattern as _replace_mappings/
    _replace_aggregate_mappings."""
    conn.execute(
        "DELETE FROM simulation_model_input_exposures WHERE model_config_id=?",
        (model_id,),
    )

    for exposure in input_exposures:
        conn.execute(
            """
            INSERT INTO simulation_model_input_exposures
                (model_config_id, variable, point_id)
            VALUES (?, ?, ?)
            """,
            (
                model_id,
                str(exposure["variable"]),
                int(exposure["point_id"]),
            ),
        )


def insert_simulation_model(
    conn: sqlite3.Connection,
    *,
    name: str,
    provider_type: str,
    model_type: str,
    enabled: bool,
    parameters: dict,
    created_from_device_id: int | None,
    mappings: list[dict],
    aggregate_mappings: list[dict] | None = None,
    input_exposures: list[dict] | None = None,
) -> int:
    """Inserts one simulation_model_configs row + its mappings/aggregate
    mappings using an already-open connection -- the shared core of
    create_simulation_model() (below, which opens its own connection for
    the ordinary API-request path) and legacy.py's load_project() (which
    must reuse its own already-open transaction: the device/object rows a
    project restore just inserted aren't visible to a second, separately-
    opened connection until that transaction commits, and
    created_from_device_id/point_id are foreign keys into exactly those
    rows). Returns the new model's id; does not commit -- the caller
    decides transaction boundaries."""
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
    _replace_aggregate_mappings(conn, model_id, aggregate_mappings or [])
    _replace_input_exposures(conn, model_id, input_exposures or [])
    return model_id


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
    aggregate_mappings: list[dict] | None = None,
    input_exposures: list[dict] | None = None,
) -> dict:
    ensure_simulation_model_schema(database)
    with database._conn() as conn:
        model_id = insert_simulation_model(
            conn,
            name=name,
            provider_type=provider_type,
            model_type=model_type,
            enabled=enabled,
            parameters=parameters,
            created_from_device_id=created_from_device_id,
            mappings=mappings,
            aggregate_mappings=aggregate_mappings,
            input_exposures=input_exposures,
        )
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
    aggregate_mappings: list[dict] | None = None,
    input_exposures: list[dict] | None = None,
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
        _replace_aggregate_mappings(conn, model_id, aggregate_mappings or [])
        _replace_input_exposures(conn, model_id, input_exposures or [])
        conn.commit()

    return get_simulation_model(database, model_id)


def set_simulation_model_enabled(database: Any, model_id: int, enabled: bool) -> dict | None:
    """Flips ONLY the enabled column -- unlike update_simulation_model, never
    touches simulation_model_mappings/simulation_model_aggregate_mappings/
    simulation_model_input_exposures, so toggling this can never perturb a
    model's saved configuration (the dedicated ON/OFF control in the
    Simulation Model drawer's footer relies on this: "disabled" must mean
    "not participating in SimEngine", never "mappings reset/lost"). Pairs
    with runtime.reload_model, which already implements the correct
    runtime lifecycle for either direction (unregister always, re-register
    only if enabled) -- see src/api/routers/simulation.py's
    PUT .../enabled route, which calls this then reload_model()."""
    ensure_simulation_model_schema(database)
    with database._conn() as conn:
        cur = conn.execute(
            "UPDATE simulation_model_configs SET enabled=?, updated_at=datetime('now') WHERE id=?",
            (int(enabled), model_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
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
            AND c.provider_type<>'system'
    """
    params: list[Any] = [point_id]
    if excluding_model_id is not None:
        sql += " AND c.id<>?"
        params.append(excluding_model_id)

    with database._conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def get_explicit_exposure_owner(
    database: Any,
    point_id: int,
    *,
    excluding_model_id: int | None = None,
) -> dict | None:
    """Mirrors get_explicit_output_owner's shape, for the separate
    simulation_model_input_exposures table -- used at the API validation
    layer to reject a point being claimed by both an ordinary output
    mapping and an input exposure (or by two exposures), since SQLite can't
    express a uniqueness constraint spanning two tables."""
    ensure_simulation_model_schema(database)
    sql = """
        SELECT c.id, c.name, c.provider_type, c.model_type, e.variable
        FROM simulation_model_input_exposures e
        JOIN simulation_model_configs c ON c.id=e.model_config_id
        WHERE e.point_id=?
    """
    params: list[Any] = [point_id]
    if excluding_model_id is not None:
        sql += " AND c.id<>?"
        params.append(excluding_model_id)

    with database._conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def get_aggregate_membership_owner(database: Any, point_id: int) -> dict | None:
    """Whether `point_id` is currently a member of any aggregate FMU input
    mapping -- as a value point OR as a weighted_average weight point --
    used by objects.py::delete_object to reject deleting a point out from
    under an aggregate with a clear, actionable error instead of letting
    the ON DELETE RESTRICT constraint surface as a raw
    sqlite3.IntegrityError. Mirrors get_explicit_output_owner's shape."""
    ensure_simulation_model_schema(database)
    sql = """
        SELECT c.id AS model_id, c.name AS model_name, c.provider_type, c.model_type, am.variable
        FROM simulation_model_aggregate_members mem
        JOIN simulation_model_aggregate_mappings am ON am.id = mem.aggregate_mapping_id
        JOIN simulation_model_configs c ON c.id = am.model_config_id
        WHERE mem.point_id=? OR mem.weight_point_id=?
    """
    with database._conn() as conn:
        row = conn.execute(sql, [point_id, point_id]).fetchone()
        return dict(row) if row else None
