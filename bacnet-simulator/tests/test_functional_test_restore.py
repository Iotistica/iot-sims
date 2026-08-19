"""End-to-end coverage of the run orchestration's restore guarantee
(src/functional_tests/runs.py's _run_and_finalize/restore_writes) -- this is
the highest-risk area of the whole HVAC-regression feature: a Set node must
NEVER leave the simulator modified after a run, regardless of whether the
run passed, failed, was cancelled, or hit an internal error.

Uses the real database fixture (so functional_test_runs persistence,
build_point_cache, and non-commandable restore's database.update_object are
all exercised for real) with a fake `engine` (tracks write_priority/
set_manual_value/reload calls and a plain object_values dict) rather than a
live BACnet Application -- consistent with this test suite's existing
convention of poking/faking SimEngine's surface for unit-level engine
interaction (see test_functional_test_runtimes.py) instead of booting a
full BACnet stack just to prove orchestration logic."""
from __future__ import annotations

import asyncio

from src.functional_tests.readiness import build_point_cache, collect_point_refs
from src.functional_tests.runs import _run_and_finalize
from src.functional_tests.runtime import TestRuntime


class _FakeClockState:
    elapsed_seconds = 0.0


class _FakeEngine:
    def __init__(self):
        self.write_priority_calls: list[tuple] = []
        self.set_manual_value_calls: list[tuple] = []
        self.reload_calls = 0
        self.object_values: dict[int, object] = {}
        # TestRuntime.now() reads engine.state.elapsed_seconds when any
        # referenced device is simulated (see runtime.py) -- only exercised
        # by tests with a wait/wait_until node (the cancellation scenario).
        self.state = _FakeClockState()

    async def write_priority(self, obj_id, priority, value):
        self.write_priority_calls.append((obj_id, priority, value))
        self.object_values[obj_id] = value
        return True

    def set_manual_value(self, obj_id, value):
        self.set_manual_value_calls.append((obj_id, value))
        self.object_values[obj_id] = value
        return True

    def get_object_value(self, obj_id):
        return self.object_values.get(obj_id)

    async def reload(self):
        self.reload_calls += 1


def _make_device(database, name="Chiller Plant", instance=9001):
    with database._conn() as conn:
        conn.execute("INSERT INTO devices (device_instance, name) VALUES (?,?)", (instance, name))
        conn.commit()
        return conn.execute("SELECT id FROM devices WHERE device_instance=?", (instance,)).fetchone()[0]


def _make_object(database, device_id, name, object_instance, object_type, behavior=None, behavior_params=None):
    fields = {"device_id": device_id, "object_type": object_type, "object_instance": object_instance, "name": name}
    if behavior is not None:
        fields["behavior"] = behavior
    if behavior_params is not None:
        fields["behavior_params"] = behavior_params
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    with database._conn() as conn:
        conn.execute(f"INSERT INTO objects ({columns}) VALUES ({placeholders})", tuple(fields.values()))
        conn.commit()
        return conn.execute(
            "SELECT id FROM objects WHERE device_id=? AND name=?", (device_id, name)
        ).fetchone()[0]


def _prepare_run(database, definition):
    test_row = database.create_functional_test({
        "name": "Restore Test", "description": "", "equipment_type": "Chiller", "definition": definition,
    })
    run_row = database.create_functional_test_run({
        "functional_test_id": test_row["id"], "execution_mode": "simulation",
    })
    point_cache = build_point_cache(database, collect_point_refs(definition))
    return run_row, point_cache


def _set_and_end(point, result="pass", priority=8, value=False):
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "set", "type": "set", "params": {"point": point, "value": value, "priority": priority}},
            {"id": "end", "type": "end", "params": {"result": result}},
        ],
        "edges": [
            {"source": "start", "target": "set", "source_handle": None},
            {"source": "set", "target": "end", "source_handle": None},
        ],
    }


# ─── (a) Passing run: restore relinquishes the exact priority slot used ──

async def test_restore_relinquishes_priority_after_passing_run(database):
    device_id = _make_device(database)
    obj_id = _make_object(database, device_id, "CH-2-Run", 1, "binary-output")
    point = {"device_id": device_id, "object_id": obj_id}
    definition = _set_and_end(point)

    run_row, point_cache = _prepare_run(database, definition)
    engine = _FakeEngine()
    runtime = TestRuntime(engine, database, point_cache)

    await _run_and_finalize(database, engine, run_row["id"], runtime, definition, asyncio.Event(), point_cache, "Restore Test", None)

    final = database.get_functional_test_run(run_row["id"])
    assert final["state"] == "passed"
    # Set wrote False at priority 8; restore must relinquish that exact
    # slot (value=None at the same priority) -- net effect: no override
    # left behind, same as if the test never ran.
    assert engine.write_priority_calls == [(obj_id, 8, False), (obj_id, 8, None)]
    assert engine.object_values[obj_id] is None

    restore_entries = [d for d in final["details"] if d["type"] == "restore"]
    assert len(restore_entries) == 1
    assert restore_entries[0]["outcome"] == "ok"
    assert restore_entries[0]["action"] == "relinquished"


# ─── (b) Failed run: restore puts back the original non-manual behavior ──

async def test_restore_restores_original_behavior_after_failed_run(database):
    device_id = _make_device(database)
    obj_id = _make_object(
        database, device_id, "Zone-SP", 1, "analog-value",
        behavior="sine", behavior_params='{"amplitude": 1, "period": 60, "offset": 72}',
    )
    point = {"device_id": device_id, "object_id": obj_id}
    definition = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "set", "type": "set", "params": {"point": point, "value": 999}},
            {"id": "v", "type": "verify", "params": {
                "left": {"kind": "constant", "value": 1}, "operator": "eq", "right": {"kind": "constant", "value": 2},
            }},
            {"id": "end-pass", "type": "end", "params": {"result": "pass"}},
            {"id": "end-fail", "type": "end", "params": {"result": "fail"}},
        ],
        "edges": [
            {"source": "start", "target": "set", "source_handle": None},
            {"source": "set", "target": "v", "source_handle": None},
            {"source": "v", "target": "end-pass", "source_handle": "pass"},
            {"source": "v", "target": "end-fail", "source_handle": "fail"},
        ],
    }

    run_row, point_cache = _prepare_run(database, definition)
    engine = _FakeEngine()
    runtime = TestRuntime(engine, database, point_cache)

    await _run_and_finalize(database, engine, run_row["id"], runtime, definition, asyncio.Event(), point_cache, "Restore Test", None)

    final = database.get_functional_test_run(run_row["id"])
    assert final["state"] == "failed"  # the verify was designed to fail

    restored = database.get_object(obj_id)
    assert restored["behavior"] == "sine"
    assert restored["behavior_params"] == '{"amplitude": 1, "period": 60, "offset": 72}'
    assert engine.reload_calls == 1

    restore_entries = [d for d in final["details"] if d["type"] == "restore"]
    assert restore_entries[0]["action"] == "restored_behavior"


# ─── (c) Cancelled mid-run: restore still fires ──────────────────────────

async def test_restore_fires_after_cancellation(database):
    device_id = _make_device(database)
    obj_id = _make_object(database, device_id, "CH-2-Run", 1, "binary-output")
    point = {"device_id": device_id, "object_id": obj_id}
    definition = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "set", "type": "set", "params": {"point": point, "value": False, "priority": 8}},
            {"id": "wu", "type": "wait_until", "params": {
                "point": point, "operator": "eq", "value": {"kind": "constant", "value": True},
                "timeout_seconds": 600,
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [
            {"source": "start", "target": "set", "source_handle": None},
            {"source": "set", "target": "wu", "source_handle": None},
            {"source": "wu", "target": "end", "source_handle": None},
        ],
    }

    run_row, point_cache = _prepare_run(database, definition)
    engine = _FakeEngine()
    runtime = TestRuntime(engine, database, point_cache)
    cancel_event = asyncio.Event()

    # Set has to actually execute (and the run enter Wait Until's poll
    # loop) before cancellation, so this can't just pre-set cancel_event --
    # let the run start on a background task and cancel it shortly after.
    task = asyncio.create_task(
        _run_and_finalize(database, engine, run_row["id"], runtime, definition, cancel_event, point_cache, "Restore Test", None)
    )
    await asyncio.sleep(0.05)
    cancel_event.set()
    await asyncio.wait_for(task, timeout=2)

    final = database.get_functional_test_run(run_row["id"])
    assert final["state"] == "cancelled"
    assert engine.write_priority_calls[-1] == (obj_id, 8, None)
    assert engine.object_values[obj_id] is None


# ─── (d) Internal ExecutionError after a Set: restore still fires ───────

async def test_restore_fires_after_internal_execution_error(database):
    device_id = _make_device(database)
    obj_id = _make_object(database, device_id, "CH-2-Run", 1, "binary-output")
    point = {"device_id": device_id, "object_id": obj_id}
    definition = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "set", "type": "set", "params": {"point": point, "value": False, "priority": 8}},
            {"id": "v", "type": "verify", "params": {
                "left": {"kind": "variable", "name": "never_captured"},
                "operator": "eq",
                "right": {"kind": "constant", "value": 1},
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [
            {"source": "start", "target": "set", "source_handle": None},
            {"source": "set", "target": "v", "source_handle": None},
            {"source": "v", "target": "end", "source_handle": "pass"},
        ],
    }

    run_row, point_cache = _prepare_run(database, definition)
    engine = _FakeEngine()
    runtime = TestRuntime(engine, database, point_cache)

    await _run_and_finalize(database, engine, run_row["id"], runtime, definition, asyncio.Event(), point_cache, "Restore Test", None)

    final = database.get_functional_test_run(run_row["id"])
    assert final["state"] == "error"
    assert "never_captured" in (final["error"] or "")
    assert engine.write_priority_calls[-1] == (obj_id, 8, None)
    assert engine.object_values[obj_id] is None
