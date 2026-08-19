"""Pure-function tests for GraphExecutor (src/functional_tests/executor.py)
-- no BACnet, no real time (mostly), no DB. FakeTestRuntime gives full
control over values/clock/cancellation so waits resolve instantly and
cancellation is deterministic rather than racy. Points are concrete
PointRef dicts ({device_id, object_id}) -- see PT() below -- matching the
Functional Tests HVAC-regression schema (no more semantic point_type
strings)."""
from __future__ import annotations

import asyncio
import copy

import pytest

from src.functional_tests.executor import DEFAULT_SET_PRIORITY, GraphExecutor
from src.functional_tests.operands import ExecutionError


def PT(object_id: int, device_id: int = 1) -> dict:
    return {"device_id": device_id, "object_id": object_id}


def _pt_key(ref: dict) -> tuple:
    return (ref["device_id"], ref["object_id"])


class FakeTestRuntime:
    """Duck-types TestRuntime's interface (read/write/snapshot_for_restore/
    now/wait) -- not a subclass, since the real TestRuntime is constructed
    from (engine, database, point_cache) and this needs full test control
    instead."""

    def __init__(self, values=None, on_wait=None, on_read=None):
        self.values = dict(values or {})
        self._clock = 0.0
        self.on_wait = on_wait
        self.on_read = on_read
        self.read_calls: list[tuple] = []
        self.write_calls: list[tuple] = []

    async def read(self, point_ref):
        key = _pt_key(point_ref)
        self.read_calls.append(key)
        if self.on_read is not None:
            self.on_read(self)
        return self.values.get(key)

    async def write(self, point_ref, value, priority=None):
        key = _pt_key(point_ref)
        self.write_calls.append((key, value, priority))
        self.values[key] = value

    async def snapshot_for_restore(self, point_ref):
        return {"commandable": True}

    def now(self) -> float:
        return self._clock

    async def wait(self, seconds: float, cancel_event: asyncio.Event) -> None:
        if self.on_wait is not None:
            self.on_wait(self, cancel_event)
        if cancel_event.is_set():
            return
        self._clock += seconds


def _def(nodes, edges):
    return {"version": 1, "nodes": nodes, "edges": edges, "layout": {}}


def _start_end(result="pass"):
    return _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "end", "type": "end", "params": {"result": result}},
        ],
        edges=[{"source": "start", "target": "end", "source_handle": None}],
    )


# ─── 1. Start -> End(pass) ──────────────────────────────────────────────

async def test_start_to_end_pass():
    executor = GraphExecutor(_start_end("pass"), FakeTestRuntime(), asyncio.Event())
    result = await executor.run()
    assert result.state == "passed"
    assert result.result == "pass"


async def test_start_to_end_fail_and_inconclusive():
    for result_value, expected_state in (("fail", "failed"), ("inconclusive", "inconclusive")):
        executor = GraphExecutor(_start_end(result_value), FakeTestRuntime(), asyncio.Event())
        result = await executor.run()
        assert result.state == expected_state
        assert result.result == result_value


# ─── 2. Wait advances runtime time ──────────────────────────────────────

async def test_wait_advances_runtime_time():
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "wait", "type": "wait", "params": {"seconds": 30}},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        edges=[
            {"source": "start", "target": "wait", "source_handle": None},
            {"source": "wait", "target": "end", "source_handle": None},
        ],
    )
    runtime = FakeTestRuntime()
    executor = GraphExecutor(definition, runtime, asyncio.Event())
    result = await executor.run()
    assert result.state == "passed"
    assert runtime.now() == 30


# ─── 3 & 4. Wait Until succeeds / times out ─────────────────────────────

def _wait_until_def(timeout_seconds=60):
    return _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "wu", "type": "wait_until", "params": {
                "point": PT(1), "operator": "eq", "value": {"kind": "constant", "value": True},
                "timeout_seconds": timeout_seconds,
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        edges=[
            {"source": "start", "target": "wu", "source_handle": None},
            {"source": "wu", "target": "end", "source_handle": None},
        ],
    )


async def test_wait_until_succeeds():
    runtime = FakeTestRuntime(values={_pt_key(PT(1)): True})
    executor = GraphExecutor(_wait_until_def(), runtime, asyncio.Event())
    result = await executor.run()
    assert result.state == "passed"


async def test_wait_until_times_out():
    # Value never satisfies eq True -- runtime.now() must cross the
    # timeout via wait_until's own polling (asyncio.wait_for(..., timeout=1.0)
    # against an unset cancel_event just times out and loops, so this
    # advances real wall-clock slightly; timeout_seconds=0 makes it fail on
    # the very first check without needing to actually wait a full second).
    runtime = FakeTestRuntime(values={_pt_key(PT(1)): False})
    executor = GraphExecutor(_wait_until_def(timeout_seconds=0), runtime, asyncio.Event())
    result = await executor.run()
    assert result.state == "failed"
    assert result.result == "fail"
    assert "timed out" in result.message


# ─── Wait Until: value can reference a captured baseline ────────────────

async def test_wait_until_value_from_captured_variable():
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "cap", "type": "capture", "params": {"point": PT(4), "variable": "baseline"}},
            {"id": "wu", "type": "wait_until", "params": {
                "point": PT(5), "operator": "lt",
                "value": {"kind": "variable", "name": "baseline", "offset": -1},
                "timeout_seconds": 600,
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        edges=[
            {"source": "start", "target": "cap", "source_handle": None},
            {"source": "cap", "target": "wu", "source_handle": None},
            {"source": "wu", "target": "end", "source_handle": None},
        ],
    )
    # baseline captured = 100 -> threshold = 100 - 1 = 99; PT(5) reads 90 -> 90 < 99 -> pass immediately.
    runtime = FakeTestRuntime(values={_pt_key(PT(4)): 100, _pt_key(PT(5)): 90})
    executor = GraphExecutor(definition, runtime, asyncio.Event())
    result = await executor.run()
    assert result.state == "passed"
    assert executor.variables["baseline"] == 100


# ─── Wait Until: stable_for_seconds requires the condition to hold ──────

async def test_wait_until_stable_for_requires_condition_to_hold():
    # Read #1: condition already true, but stable_for hasn't elapsed yet
    # (first_true_at == now == 0) -- must NOT pass yet. Read #2: clock has
    # advanced past stable_for_seconds while still true -- must pass then.
    calls = {"n": 0}

    def _advance(runtime):
        calls["n"] += 1
        runtime.values[_pt_key(PT(1))] = True
        if calls["n"] >= 2:
            runtime._clock = 10.0

    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "wu", "type": "wait_until", "params": {
                "point": PT(1), "operator": "eq", "value": {"kind": "constant", "value": True},
                "stable_for_seconds": 5, "timeout_seconds": 600,
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        edges=[
            {"source": "start", "target": "wu", "source_handle": None},
            {"source": "wu", "target": "end", "source_handle": None},
        ],
    )
    runtime = FakeTestRuntime(on_read=_advance)
    executor = GraphExecutor(definition, runtime, asyncio.Event())
    result = await executor.run()
    assert result.state == "passed"
    assert calls["n"] == 2  # would be 1 if stability wasn't enforced


# ─── 5. Capture stores current value ────────────────────────────────────

async def test_capture_stores_current_value():
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "cap", "type": "capture", "params": {"point": PT(2), "variable": "lwt"}},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        edges=[
            {"source": "start", "target": "cap", "source_handle": None},
            {"source": "cap", "target": "end", "source_handle": None},
        ],
    )
    runtime = FakeTestRuntime(values={_pt_key(PT(2)): 58.2})
    executor = GraphExecutor(definition, runtime, asyncio.Event())
    await executor.run()
    assert executor.variables["lwt"] == 58.2


# ─── 6 & 7. Verify takes pass/fail edge ─────────────────────────────────

def _verify_def():
    return _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "v", "type": "verify", "params": {
                "left": {"kind": "constant", "value": 10},
                "operator": "gt",
                "right": {"kind": "constant", "value": 5},
            }},
            {"id": "end-pass", "type": "end", "params": {"result": "pass"}},
            {"id": "end-fail", "type": "end", "params": {"result": "fail"}},
        ],
        edges=[
            {"source": "start", "target": "v", "source_handle": None},
            {"source": "v", "target": "end-pass", "source_handle": "pass"},
            {"source": "v", "target": "end-fail", "source_handle": "fail"},
        ],
    )


async def test_verify_takes_pass_edge():
    executor = GraphExecutor(_verify_def(), FakeTestRuntime(), asyncio.Event())
    result = await executor.run()
    assert result.result == "pass"


async def test_verify_takes_fail_edge():
    definition = _verify_def()
    # Flip the comparison so it evaluates false -> should follow "fail" edge.
    definition["nodes"][1]["params"]["left"]["value"] = 1
    executor = GraphExecutor(definition, FakeTestRuntime(), asyncio.Event())
    result = await executor.run()
    assert result.result == "fail"


# ─── 8. Compare supports all six legacy operators ───────────────────────

@pytest.mark.parametrize("operator,left,right,expected", [
    ("eq", 5, 5, "pass"),
    ("neq", 5, 6, "pass"),
    ("gt", 6, 5, "pass"),
    ("gte", 5, 5, "pass"),
    ("lt", 4, 5, "pass"),
    ("lte", 5, 5, "pass"),
    ("eq", 5, 6, "fail"),
])
async def test_compare_operators(operator, left, right, expected):
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "c", "type": "compare", "params": {
                "left": {"kind": "constant", "value": left},
                "operator": operator,
                "right": {"kind": "constant", "value": right},
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        edges=[
            {"source": "start", "target": "c", "source_handle": None},
            {"source": "c", "target": "end", "source_handle": None},
        ],
    )
    executor = GraphExecutor(definition, FakeTestRuntime(), asyncio.Event())
    result = await executor.run()
    assert result.state == "passed"  # compare doesn't branch -- always continues


# ─── within_tolerance operator ───────────────────────────────────────────

@pytest.mark.parametrize("left,right,tolerance,expected", [
    (10.0, 10.4, 0.5, "pass"),
    (10.0, 11.0, 0.5, "fail"),
])
async def test_within_tolerance_operator(left, right, tolerance, expected):
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "v", "type": "verify", "params": {
                "left": {"kind": "constant", "value": left},
                "operator": "within_tolerance",
                "right": {"kind": "constant", "value": right},
                "tolerance": tolerance,
            }},
            {"id": "end-pass", "type": "end", "params": {"result": "pass"}},
            {"id": "end-fail", "type": "end", "params": {"result": "fail"}},
        ],
        edges=[
            {"source": "start", "target": "v", "source_handle": None},
            {"source": "v", "target": "end-pass", "source_handle": "pass"},
            {"source": "v", "target": "end-fail", "source_handle": "fail"},
        ],
    )
    executor = GraphExecutor(definition, FakeTestRuntime(), asyncio.Event())
    result = await executor.run()
    assert result.result == expected


# ─── Set: writes via runtime.write() and records to executor.writes ─────

async def test_set_writes_and_records():
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "set", "type": "set", "params": {"point": PT(3), "value": "OFF", "priority": 8}},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        edges=[
            {"source": "start", "target": "set", "source_handle": None},
            {"source": "set", "target": "end", "source_handle": None},
        ],
    )
    runtime = FakeTestRuntime()
    executor = GraphExecutor(definition, runtime, asyncio.Event())
    result = await executor.run()
    assert result.state == "passed"
    assert runtime.write_calls == [(_pt_key(PT(3)), "OFF", 8)]
    assert len(executor.writes) == 1
    assert executor.writes[0]["point"] == PT(3)
    assert executor.writes[0]["priority"] == 8
    assert executor.writes[0]["commandable"] is True


async def test_set_defaults_priority_when_unset():
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "set", "type": "set", "params": {"point": PT(3), "value": "OFF"}},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        edges=[
            {"source": "start", "target": "set", "source_handle": None},
            {"source": "set", "target": "end", "source_handle": None},
        ],
    )
    runtime = FakeTestRuntime()
    executor = GraphExecutor(definition, runtime, asyncio.Event())
    await executor.run()
    assert executor.writes[0]["priority"] == DEFAULT_SET_PRIORITY


# ─── 9. Variable + offset evaluates correctly ───────────────────────────

async def test_variable_with_offset():
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "cap", "type": "capture", "params": {"point": PT(6), "variable": "lwt"}},
            {"id": "v", "type": "verify", "params": {
                "left": {"kind": "constant", "value": 62},
                "operator": "gt",
                "right": {"kind": "variable", "name": "lwt", "offset": 2},
            }},
            {"id": "end-pass", "type": "end", "params": {"result": "pass"}},
            {"id": "end-fail", "type": "end", "params": {"result": "fail"}},
        ],
        edges=[
            {"source": "start", "target": "cap", "source_handle": None},
            {"source": "cap", "target": "v", "source_handle": None},
            {"source": "v", "target": "end-pass", "source_handle": "pass"},
            {"source": "v", "target": "end-fail", "source_handle": "fail"},
        ],
    )
    runtime = FakeTestRuntime(values={_pt_key(PT(6)): 58})  # 62 > 58 + 2 == 60 -> pass
    executor = GraphExecutor(definition, runtime, asyncio.Event())
    result = await executor.run()
    assert result.result == "pass"


# ─── 10. Missing captured variable gives controlled error ───────────────

async def test_missing_variable_gives_controlled_error():
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "v", "type": "verify", "params": {
                "left": {"kind": "variable", "name": "never_captured"},
                "operator": "eq",
                "right": {"kind": "constant", "value": 1},
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        edges=[
            {"source": "start", "target": "v", "source_handle": None},
            {"source": "v", "target": "end", "source_handle": "pass"},
        ],
    )
    executor = GraphExecutor(definition, FakeTestRuntime(), asyncio.Event())
    with pytest.raises(ExecutionError, match="never_captured"):
        await executor.run()


# ─── 13 & 14. Cancel interrupts Wait / Wait Until ───────────────────────

async def test_cancel_interrupts_wait():
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "wait", "type": "wait", "params": {"seconds": 300}},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        edges=[
            {"source": "start", "target": "wait", "source_handle": None},
            {"source": "wait", "target": "end", "source_handle": None},
        ],
    )

    def _cancel_during_wait(runtime, cancel_event):
        cancel_event.set()

    runtime = FakeTestRuntime(on_wait=_cancel_during_wait)
    cancel_event = asyncio.Event()
    executor = GraphExecutor(definition, runtime, cancel_event)
    result = await executor.run()
    assert result.state == "cancelled"


async def test_cancel_interrupts_wait_until():
    def _cancel_on_first_read(runtime):
        runtime.values[_pt_key(PT(1))] = False
        runtime._cancel_event_ref.set()

    cancel_event = asyncio.Event()
    runtime = FakeTestRuntime(values={_pt_key(PT(1)): False})
    runtime._cancel_event_ref = cancel_event  # test-only wiring for the on_read hook
    runtime.on_read = _cancel_on_first_read

    executor = GraphExecutor(_wait_until_def(timeout_seconds=600), runtime, cancel_event)
    result = await executor.run()
    assert result.state == "cancelled"


# ─── 15. Malformed definition cannot execute ────────────────────────────

async def test_no_start_node_refuses_to_execute():
    definition = _def(nodes=[{"id": "end", "type": "end", "params": {"result": "pass"}}], edges=[])
    executor = GraphExecutor(definition, FakeTestRuntime(), asyncio.Event())
    with pytest.raises(ExecutionError, match="exactly one start node"):
        await executor.run()


async def test_two_start_nodes_refuses_to_execute():
    definition = _def(
        nodes=[
            {"id": "start1", "type": "start", "params": {}},
            {"id": "start2", "type": "start", "params": {}},
        ],
        edges=[],
    )
    executor = GraphExecutor(definition, FakeTestRuntime(), asyncio.Event())
    with pytest.raises(ExecutionError, match="exactly one start node"):
        await executor.run()


async def test_cyclic_graph_hits_step_cap_instead_of_hanging():
    definition = _def(
        nodes=[
            {"id": "start", "type": "start", "params": {}},
            {"id": "wait", "type": "wait", "params": {"seconds": 0}},
        ],
        edges=[
            {"source": "start", "target": "wait", "source_handle": None},
            {"source": "wait", "target": "wait", "source_handle": None},  # self-loop
        ],
    )
    executor = GraphExecutor(definition, FakeTestRuntime(), asyncio.Event())
    with pytest.raises(ExecutionError, match="maximum"):
        await executor.run()


# ─── 19. Running a test never mutates its FunctionalTestDefinition ──────

async def test_run_never_mutates_definition():
    definition = _verify_def()
    snapshot = copy.deepcopy(definition)
    executor = GraphExecutor(definition, FakeTestRuntime(), asyncio.Event())
    await executor.run()
    assert definition == snapshot
