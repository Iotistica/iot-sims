"""Tests for the two TestRuntime implementations
(src/functional_tests/runtime.py). SimulationTestRuntime is exercised
against a real SimEngine(database) -- no socket needed, same precedent as
tests/test_sim_engine_object_value.py (poke _prev_values/state.elapsed_seconds/
clock_state directly). ExternalBacnetTestRuntime is exercised with
_discovery_session monkeypatched to a fake, no real network involved,
proving it goes through the shared discovery session rather than building
its own BACnet Application."""
from __future__ import annotations

import asyncio
import contextlib

from src.functional_tests import runtime as runtime_module
from src.functional_tests.runtime import ExternalBacnetTestRuntime, SimulationTestRuntime
from src.legacy import SimEngine


# ─── SimulationTestRuntime ───────────────────────────────────────────────

async def test_simulation_runtime_reads_actual_current_simulated_value(database):
    engine = SimEngine(database)
    engine._current_values = {"devices": [], "tick": 0.0}
    engine._prev_values[42] = 58.2

    runtime = SimulationTestRuntime(engine, resolution_map={"T": {"id": 42, "name": "obj"}})

    assert await runtime.read("T") == 58.2


async def test_simulation_runtime_wait_honors_pause(database, monkeypatch):
    monkeypatch.setattr(runtime_module, "_WAIT_POLL_SECONDS", 0.02)

    engine = SimEngine(database)
    engine.clock_state = "paused"

    runtime = SimulationTestRuntime(engine, {})
    cancel_event = asyncio.Event()

    task = asyncio.create_task(runtime.wait(5, cancel_event))
    await asyncio.sleep(0.1)
    # elapsed_seconds never advances while paused -- wait() must still be
    # running, not have silently fallen back to a wall-clock timeout.
    assert not task.done()

    cancel_event.set()
    await asyncio.wait_for(task, timeout=2)


async def test_simulation_runtime_wait_completes_once_clock_advances(database, monkeypatch):
    monkeypatch.setattr(runtime_module, "_WAIT_POLL_SECONDS", 0.02)

    engine = SimEngine(database)
    engine.clock_state = "running"

    runtime = SimulationTestRuntime(engine, {})
    cancel_event = asyncio.Event()

    task = asyncio.create_task(runtime.wait(2, cancel_event))
    await asyncio.sleep(0.05)
    assert not task.done()

    engine.state.elapsed_seconds += 2
    await asyncio.wait_for(task, timeout=2)
    assert task.done()


# ─── ExternalBacnetTestRuntime ───────────────────────────────────────────

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


async def test_external_runtime_reads_through_the_shared_discovery_session(monkeypatch):
    fake = _install_fake_discovery_session(monkeypatch, {("analog-input", 1): 61.1})

    device = {"external_host": "10.0.0.5", "device_instance": 99}
    resolution_map = {"T": {"object_type": "analog-input", "object_instance": 1}}
    runtime = ExternalBacnetTestRuntime(device, resolution_map)

    value = await runtime.read("T")

    assert value == 61.1
    assert fake.calls == [("10.0.0.5", 99, [("analog-input", 1)])]
