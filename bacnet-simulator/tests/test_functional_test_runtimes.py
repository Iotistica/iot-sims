"""Tests for TestRuntime (src/functional_tests/runtime.py) -- one runtime
shared by every run, routing per-point by the referenced device's
source_type rather than branching on a single run-wide execution_mode (see
its own module docstring, and the plan this replaced the old
SimulationTestRuntime/ExternalBacnetTestRuntime split for). Reads against a
simulated device are exercised against a real SimEngine(database) -- no
socket needed, same precedent as tests/test_sim_engine_object_value.py
(poke _prev_values/state.elapsed_seconds/clock_state directly). Reads
against an external device go through _discovery_session monkeypatched to
a fake, no real network involved. Writes are exercised against a fake
engine/database stub (no real BACnet object stack needed to prove
routing/guard behavior -- write_priority/set_manual_value's own BACnet-
level behavior is covered by tests/test_objects_api.py and friends)."""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from src.functional_tests import runtime as runtime_module
from src.functional_tests.operands import ExecutionError
from src.functional_tests.runtime import TestRuntime
from src.legacy import SimEngine

SIMULATED_DEVICE = {"source_type": "simulated"}
EXTERNAL_DEVICE = {"source_type": "external-bacnet", "external_host": "10.0.0.5", "device_instance": 99}


class _FakeEngine:
    def __init__(self):
        self.write_priority_calls: list[tuple] = []
        self.set_manual_value_calls: list[tuple] = []

    async def write_priority(self, obj_id, priority, value):
        self.write_priority_calls.append((obj_id, priority, value))
        return True

    def set_manual_value(self, obj_id, value):
        self.set_manual_value_calls.append((obj_id, value))
        return True


class _FakeDatabase:
    def __init__(self):
        self.set_manual_value_calls: list[tuple] = []

    def set_manual_value(self, obj_id, value):
        self.set_manual_value_calls.append((obj_id, value))


class _FakeDiscovery:
    def __init__(self, values: dict):
        self._values = values
        self.calls: list[tuple] = []

    async def read_present_values(self, host, device_instance, points, **kwargs):
        self.calls.append((host, device_instance, points))
        return self._values


def _install_fake_discovery_session(monkeypatch, values: dict) -> _FakeDiscovery:
    fake = _FakeDiscovery(values)

    @contextlib.asynccontextmanager
    async def _fake_session(*args, **kwargs):
        yield fake

    monkeypatch.setattr("src.api.routers.discovery._discovery_session", _fake_session)
    return fake


# ─── read() routes per-point ─────────────────────────────────────────────

async def test_runtime_reads_simulated_point_through_engine(database):
    engine = SimEngine(database)
    engine._current_values = {"devices": [], "tick": 0.0}
    engine._prev_values[42] = 58.2

    point_cache = {
        (1, 42): {"object": {"id": 42, "object_type": "analog-input"}, "device": SIMULATED_DEVICE},
    }
    runtime = TestRuntime(engine, database, point_cache)

    assert await runtime.read({"device_id": 1, "object_id": 42}) == 58.2


async def test_runtime_reads_external_point_through_shared_discovery_session(monkeypatch):
    fake = _install_fake_discovery_session(monkeypatch, {("analog-input", 1): 61.1})

    point_cache = {
        (2, 7): {
            "object": {"id": 7, "object_type": "analog-input", "object_instance": 1},
            "device": EXTERNAL_DEVICE,
        },
    }
    runtime = TestRuntime(engine=None, database=None, point_cache=point_cache)

    value = await runtime.read({"device_id": 2, "object_id": 7})

    assert value == 61.1
    assert fake.calls == [("10.0.0.5", 99, [("analog-input", 1)])]


# ─── write() is simulation-only, and routes by commandable-ness ─────────

async def test_runtime_write_raises_for_external_device():
    point_cache = {
        (2, 7): {"object": {"id": 7, "object_type": "binary-output"}, "device": EXTERNAL_DEVICE},
    }
    runtime = TestRuntime(engine=None, database=None, point_cache=point_cache)

    with pytest.raises(ExecutionError, match="simulated"):
        await runtime.write({"device_id": 2, "object_id": 7}, False, priority=8)


async def test_runtime_write_uses_priority_array_for_commandable_object():
    engine = _FakeEngine()
    point_cache = {
        (1, 10): {"object": {"id": 10, "object_type": "binary-output"}, "device": SIMULATED_DEVICE},
    }
    runtime = TestRuntime(engine, _FakeDatabase(), point_cache)

    await runtime.write({"device_id": 1, "object_id": 10}, False, priority=8)

    assert engine.write_priority_calls == [(10, 8, False)]


async def test_runtime_write_uses_manual_value_for_non_commandable_object():
    engine = _FakeEngine()
    database = _FakeDatabase()
    point_cache = {
        (1, 11): {"object": {"id": 11, "object_type": "analog-value"}, "device": SIMULATED_DEVICE},
    }
    runtime = TestRuntime(engine, database, point_cache)

    await runtime.write({"device_id": 1, "object_id": 11}, 72.5, priority=8)

    assert database.set_manual_value_calls == [(11, 72.5)]
    assert engine.set_manual_value_calls == [(11, 72.5)]


# ─── Clock selection: wall clock only when every device is external ─────

def test_runtime_uses_wall_clock_when_all_devices_external():
    point_cache = {
        (2, 7): {"object": {"id": 7, "object_type": "analog-input"}, "device": EXTERNAL_DEVICE},
    }
    runtime = TestRuntime(engine=None, database=None, point_cache=point_cache)
    assert runtime._use_wall_clock is True


def test_runtime_uses_sim_clock_when_any_device_simulated(database):
    engine = SimEngine(database)
    point_cache = {
        (1, 10): {"object": {"id": 10, "object_type": "binary-output"}, "device": SIMULATED_DEVICE},
        (2, 7): {"object": {"id": 7, "object_type": "analog-input"}, "device": EXTERNAL_DEVICE},
    }
    runtime = TestRuntime(engine, database, point_cache)
    assert runtime._use_wall_clock is False
    assert runtime.now() == engine.state.elapsed_seconds


def test_runtime_uses_sim_clock_when_point_cache_empty(database):
    engine = SimEngine(database)
    runtime = TestRuntime(engine, database, {})
    assert runtime._use_wall_clock is False


async def test_runtime_wait_honors_pause(database, monkeypatch):
    monkeypatch.setattr(runtime_module, "_WAIT_POLL_SECONDS", 0.02)

    engine = SimEngine(database)
    engine.clock_state = "paused"
    runtime = TestRuntime(engine, database, {})
    cancel_event = asyncio.Event()

    task = asyncio.create_task(runtime.wait(5, cancel_event))
    await asyncio.sleep(0.1)
    # elapsed_seconds never advances while paused -- wait() must still be
    # running, not have silently fallen back to a wall-clock timeout.
    assert not task.done()

    cancel_event.set()
    await asyncio.wait_for(task, timeout=2)


async def test_runtime_wait_completes_once_clock_advances(database, monkeypatch):
    monkeypatch.setattr(runtime_module, "_WAIT_POLL_SECONDS", 0.02)

    engine = SimEngine(database)
    engine.clock_state = "running"
    runtime = TestRuntime(engine, database, {})
    cancel_event = asyncio.Event()

    task = asyncio.create_task(runtime.wait(2, cancel_event))
    await asyncio.sleep(0.05)
    assert not task.done()

    engine.state.elapsed_seconds += 2
    await asyncio.wait_for(task, timeout=2)
    assert task.done()
