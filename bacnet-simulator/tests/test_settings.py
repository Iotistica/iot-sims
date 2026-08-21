"""Tests for the global Settings mechanism backing the admin UI's new
"Simulation" tab (SettingsView.vue): Database.get_settings()/save_settings()
(load/persistence), SettingsPayload (validation), and _apply_settings_live()
(live application).

src/application.py's GET/PUT /settings routes are defined directly on the
`api` FastAPI instance (not via a router module) and read the
src/dependencies.py globals `db`/`engine` that only lifespan() wires up at
real app startup -- tests/conftest.py's lightweight test_app fixture (a bare
FastAPI() with a hand-picked router list, see its own docstring) does not
include them. So, matching this project's own precedent of testing below
the HTTP layer when the HTTP layer itself isn't practically constructible
in isolation, this file exercises the exact same three layers those two
routes call into: Database persistence, Pydantic validation, and the
live-apply function.

Six settings moved to the new Simulation tab this pass (tick_seconds,
object_history_maxlen, trend_log_default_interval,
trend_log_default_buffer_size, fmu_runtime_url, fmu_runtime_timeout_s) --
no new settings, no schema/validation change, no simulation architecture
change. "Live application" differs by field: tick_seconds and
object_history_maxlen are cached in module globals refreshed by
_apply_settings_live() (see that function's own docstring); the other four
are read fresh from Database.get_settings() at point of use (trend log
creation, FMU provider construction) with no caching layer to go stale --
proven here by the persistence round-trip tests plus a direct read of
those call sites, not a separate live-apply test (there is no module
global for them to update).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

import src.dependencies as dependencies
from src.bacnet.schemas import SettingsPayload
from src.db.database import _default_settings


def _valid_payload_dict() -> dict:
    return SettingsPayload().model_dump()


# ─── Load: defaults ──────────────────────────────────────────────────────

def test_get_settings_returns_defaults_on_empty_database(database):
    values = database.get_settings()
    assert values["tick_seconds"] == 5.0
    assert values["object_history_maxlen"] == 720
    assert values["trend_log_default_interval"] == 60
    assert values["trend_log_default_buffer_size"] == 1000
    assert values["fmu_runtime_url"] == "http://localhost:8002"
    assert values["fmu_runtime_timeout_s"] == 20.0


# ─── Validation (SettingsPayload, what PUT /settings enforces) ──────────

@pytest.mark.parametrize("field,bad_value", [
    ("tick_seconds", 0.0),                    # below ge=0.1
    ("tick_seconds", 3601),                   # above le=3600
    ("object_history_maxlen", 9),             # below ge=10
    ("object_history_maxlen", 100001),        # above le=100000
    ("trend_log_default_interval", 0),        # below ge=1
    ("trend_log_default_buffer_size", 0),     # below ge=1
    ("trend_log_default_buffer_size", 100001),# above le=100000
    ("fmu_runtime_url", ""),                  # below min_length=1
    ("fmu_runtime_timeout_s", 0.5),           # below ge=1.0
    ("fmu_runtime_timeout_s", 121),           # above le=120
])
def test_settings_payload_rejects_out_of_range_values(field, bad_value):
    payload = _valid_payload_dict()
    payload[field] = bad_value
    with pytest.raises(ValidationError):
        SettingsPayload(**payload)


def test_settings_payload_accepts_boundary_values():
    low = _valid_payload_dict()
    low.update(
        tick_seconds=0.1, object_history_maxlen=10,
        trend_log_default_interval=1, trend_log_default_buffer_size=1,
        fmu_runtime_timeout_s=1.0,
    )
    SettingsPayload(**low)  # must not raise

    high = _valid_payload_dict()
    high.update(
        tick_seconds=3600, object_history_maxlen=100000,
        trend_log_default_buffer_size=100000, fmu_runtime_timeout_s=120,
    )
    SettingsPayload(**high)  # must not raise


# ─── Save + persistence round-trip ───────────────────────────────────────

def test_save_settings_persists_and_round_trips(database):
    database.save_settings({
        "tick_seconds": 2.5,
        "object_history_maxlen": 500,
        "trend_log_default_interval": 30,
        "trend_log_default_buffer_size": 2000,
        "fmu_runtime_url": "http://fmu-runtime:9000",
        "fmu_runtime_timeout_s": 45.0,
    })
    reloaded = database.get_settings()
    assert reloaded["tick_seconds"] == 2.5
    assert reloaded["object_history_maxlen"] == 500
    assert reloaded["trend_log_default_interval"] == 30
    assert reloaded["trend_log_default_buffer_size"] == 2000
    assert reloaded["fmu_runtime_url"] == "http://fmu-runtime:9000"
    assert reloaded["fmu_runtime_timeout_s"] == 45.0


def test_save_settings_does_not_clobber_other_fields(database):
    """SettingsView.vue's Simulation tab PUTs the full 13-field form (see
    its own save handler), but the DB layer itself is column-partial --
    proven here so a future caller that only saves a subset (as this
    project's own SettingsPayload docstring flags as a real risk for a
    naive partial PUT) can trust the persistence layer isn't what would
    cause data loss."""
    database.save_settings({"jwt_expire_hours": 48})
    database.save_settings({"tick_seconds": 1.0})
    values = database.get_settings()
    assert values["tick_seconds"] == 1.0
    assert values["jwt_expire_hours"] == 48  # not reset by the second save


def test_persisted_settings_survive_a_new_database_instance(tmp_path):
    """Persistence survives a fresh Database object against the same
    file -- not just re-reading the same in-memory instance/connection."""
    from src.db import Database
    db_path = tmp_path / "settings_persist.db"
    first = Database(db_path)
    first.setup()
    first.save_settings({"fmu_runtime_url": "http://persisted:8002"})

    second = Database(db_path)
    values = second.get_settings()
    assert values["fmu_runtime_url"] == "http://persisted:8002"


# ─── Live application ─────────────────────────────────────────────────────

class _FakeSimEngineForHistory:
    """Minimal stand-in for the one thing _apply_settings_live touches on
    the module-global `engine`: its _history dict of per-object deques."""
    def __init__(self, history: dict):
        self._history = history


def test_apply_settings_live_updates_tick_and_history_globals(monkeypatch):
    monkeypatch.setattr(dependencies, "engine", None)
    values = _default_settings()
    values["tick_seconds"] = 1.5
    values["object_history_maxlen"] = 42
    dependencies._apply_settings_live(values)
    try:
        assert dependencies.TICK_SECONDS == 1.5
        assert dependencies.OBJECT_HISTORY_MAXLEN == 42
    finally:
        # Restore module globals other tests in the same process may rely
        # on their documented defaults for -- _apply_settings_live mutates
        # process-wide state, not anything scoped to a fixture.
        dependencies._apply_settings_live(_default_settings())


def test_apply_settings_live_resizes_existing_object_history_buffers(monkeypatch):
    """The concrete "shrinking a value truncates the in-memory buffer to
    the newest entries immediately" behavior the Buffers & Retention tab's
    own hint text already describes -- proven here for
    object_history_maxlen specifically, which moved to the new Simulation
    tab this pass but keeps the exact same live-apply behavior (untouched
    backend, per the task's own "keep the change focused" instruction)."""
    from collections import deque
    history = {1: deque([(float(i), float(i)) for i in range(10)], maxlen=720)}
    fake_engine = _FakeSimEngineForHistory(history)
    monkeypatch.setattr(dependencies, "engine", fake_engine)

    values = _default_settings()
    values["object_history_maxlen"] = 3
    dependencies._apply_settings_live(values)
    try:
        resized = fake_engine._history[1]
        assert resized.maxlen == 3
        assert len(resized) == 3
        # Newest entries kept, oldest dropped -- deque(old, maxlen=new)
        # truncation semantics, matching the function's own docstring.
        assert [v for _, v in resized] == [7.0, 8.0, 9.0]
    finally:
        dependencies._apply_settings_live(_default_settings())
