"""Regression test for the ongoing reconciliation step
(model_store.reconcile_provider_owned_raw_behavior) that relabels an
already-provider-owned point's `behavior='constant'` to `'raw'`.

"constant" and "raw" are 100% functionally identical for a provider-owned
point (both are a pure passthrough of the live FMU/model value -- see
SimEngine._apply_fmu_behavior), so this only changes what's displayed in
the admin UI, never runtime behavior.

This runs on every Database.setup() call (every app boot), NOT as a
one-time schema_migrations-tracked migration -- a point can become
provider-owned at any time after the app first started, and a historical
one-time fixup could never catch those. Test setup uses its own connection
to insert fixture rows, closing it before invoking the reconciliation
function so the two never hold the file-backed SQLite DB open at the same
time (Database._conn() opens a brand-new connection every call).
"""
from __future__ import annotations

from src.simulation.model_store import ensure_simulation_model_schema, reconcile_provider_owned_raw_behavior


def _make_device_and_point(database, *, instance: int, behavior: str = "constant"):
    conn = database._conn()
    conn.execute("INSERT INTO devices (device_instance, name) VALUES (?, ?)", (instance, f"Device-{instance}"))
    device_id = conn.execute("SELECT id FROM devices WHERE device_instance=?", (instance,)).fetchone()[0]
    conn.execute(
        "INSERT INTO objects (device_id, object_type, object_instance, name, behavior, behavior_params) "
        "VALUES (?, 'analog-input', 1, ?, ?, '{\"value\": 22}')",
        (device_id, f"Point-{instance}", behavior),
    )
    point_id = conn.execute(
        "SELECT id FROM objects WHERE device_id=? AND object_instance=1", (device_id,)
    ).fetchone()[0]
    conn.commit()
    conn.close()
    return device_id, point_id


def _make_output_mapping(database, point_id: int, *, enabled: bool = True, provider_type: str = "fmu"):
    ensure_simulation_model_schema(database)
    conn = database._conn()
    conn.execute(
        "INSERT INTO simulation_model_configs (name, provider_type, model_type, enabled) VALUES (?, ?, 'RTU', ?)",
        (f"model-for-{point_id}", provider_type, 1 if enabled else 0),
    )
    model_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO simulation_model_mappings (model_config_id, variable, direction, point_id) "
        "VALUES (?, 'some_variable', 'output', ?)",
        (model_id, point_id),
    )
    conn.commit()
    conn.close()
    return model_id


def _make_input_exposure(database, point_id: int, *, enabled: bool = True):
    ensure_simulation_model_schema(database)
    conn = database._conn()
    conn.execute(
        "INSERT INTO simulation_model_configs (name, provider_type, model_type, enabled) VALUES (?, 'learned', 'RTU', ?)",
        (f"exposure-model-for-{point_id}", 1 if enabled else 0),
    )
    model_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO simulation_model_input_exposures (model_config_id, variable, point_id) "
        "VALUES (?, 'some_input', ?)",
        (model_id, point_id),
    )
    conn.commit()
    conn.close()
    return model_id


def _behavior_of(database, point_id: int) -> str:
    conn = database._conn()
    try:
        return conn.execute("SELECT behavior FROM objects WHERE id=?", (point_id,)).fetchone()[0]
    finally:
        conn.close()


def test_output_owned_constant_point_becomes_raw(database):
    _device_id, point_id = _make_device_and_point(database, instance=7001)
    _make_output_mapping(database, point_id)

    reconcile_provider_owned_raw_behavior(database)

    assert _behavior_of(database, point_id) == "raw"


def test_input_exposure_owned_constant_point_becomes_raw(database):
    _device_id, point_id = _make_device_and_point(database, instance=7002)
    _make_input_exposure(database, point_id)

    reconcile_provider_owned_raw_behavior(database)

    assert _behavior_of(database, point_id) == "raw"


def test_disabled_model_output_is_not_touched(database):
    """A DISABLED model's output mapping doesn't make the point provider-
    owned today (get_output_owners_by_point filters enabled=1) -- the
    reconciliation must use the exact same filter, or it would relabel a
    point that's actually still behavior-driven."""
    _device_id, point_id = _make_device_and_point(database, instance=7003)
    _make_output_mapping(database, point_id, enabled=False)

    reconcile_provider_owned_raw_behavior(database)

    assert _behavior_of(database, point_id) == "constant"


def test_system_provider_type_is_not_touched(database):
    _device_id, point_id = _make_device_and_point(database, instance=7004)
    _make_output_mapping(database, point_id, provider_type="system")

    reconcile_provider_owned_raw_behavior(database)

    assert _behavior_of(database, point_id) == "constant"


def test_non_constant_behavior_is_left_alone(database):
    """Only the exact legacy default ('constant') is relabeled -- a point
    someone already explicitly configured with sine/noise/etc. keeps that
    choice untouched."""
    _device_id, point_id = _make_device_and_point(database, instance=7005, behavior="sine")
    _make_output_mapping(database, point_id)

    reconcile_provider_owned_raw_behavior(database)

    assert _behavior_of(database, point_id) == "sine"


def test_plain_non_provider_point_is_not_touched(database):
    _device_id, point_id = _make_device_and_point(database, instance=7006)

    reconcile_provider_owned_raw_behavior(database)

    assert _behavior_of(database, point_id) == "constant"


def test_reconcile_is_idempotent(database):
    _device_id, point_id = _make_device_and_point(database, instance=7008)
    _make_output_mapping(database, point_id)

    reconcile_provider_owned_raw_behavior(database)
    reconcile_provider_owned_raw_behavior(database)

    assert _behavior_of(database, point_id) == "raw"


def test_reconcile_catches_a_mapping_created_after_a_previous_run(database):
    """The whole point of running this on every boot instead of as a
    one-time migration: a mapping created AFTER an earlier reconciliation
    already ran must still get caught on the next one."""
    _device_id, point_id_early = _make_device_and_point(database, instance=7009)
    _make_output_mapping(database, point_id_early)
    reconcile_provider_owned_raw_behavior(database)
    assert _behavior_of(database, point_id_early) == "raw"

    # A second point is only mapped AFTER that first reconciliation run.
    _device_id2, point_id_late = _make_device_and_point(database, instance=7010)
    _make_output_mapping(database, point_id_late)
    assert _behavior_of(database, point_id_late) == "constant"  # not yet reconciled

    reconcile_provider_owned_raw_behavior(database)
    assert _behavior_of(database, point_id_late) == "raw"


def test_setup_reconciles_provider_owned_points(database):
    """Database.setup() itself calls this on every boot -- exercise it
    through the real entry point, not just the function directly."""
    _device_id, point_id = _make_device_and_point(database, instance=7011)
    _make_output_mapping(database, point_id)
    assert _behavior_of(database, point_id) == "constant"

    database.setup()

    assert _behavior_of(database, point_id) == "raw"
