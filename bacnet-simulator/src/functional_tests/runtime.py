"""TestRuntime abstraction -- the GraphExecutor talks only to this
interface, never branching on execution_mode itself. Two implementations:
ExternalBacnetTestRuntime (reads through the existing, already-shared
_discovery_session()/read_present_values() path -- no second BACnet
Application/socket) and SimulationTestRuntime (reads through the existing
SimEngine.get_object_value()/elapsed_seconds/clock_state -- no second
simulation clock). Both are strictly READ-ONLY: neither ever calls
WriteProperty or mutates simulator state.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from .operands import ExecutionError

# Fixed, modest poll cadence for wait()'s internal loop -- matches the
# admin UI's own external-value polling cadence (ObjectsPanel.vue's 3s
# interval) closely enough, and is cheap for the in-process simulation read
# path too. Deliberately not configurable in this phase -- keep it small.
_WAIT_POLL_SECONDS = 1.0


class TestRuntime(ABC):
    def __init__(self, resolution_map: dict[str, dict]):
        # resolution_map: point_type -> resolved `objects` row (already
        # validated clean by pre-flight resolution before a runtime is ever
        # constructed -- resolve_point() below is a plain dict lookup, not
        # a fresh resolution).
        self._resolution_map = resolution_map

    def resolve_point(self, point_type: str) -> dict:
        obj = self._resolution_map.get(point_type)
        if obj is None:
            # Pre-flight resolution is supposed to guarantee every point
            # referenced by the graph is present in the map -- this is an
            # internal-consistency error, not a normal user-facing one.
            raise ExecutionError(f"Point {point_type!r} was not resolved before execution")
        return obj

    @abstractmethod
    async def read(self, point_type: str) -> Any: ...

    @abstractmethod
    def now(self) -> float: ...

    @abstractmethod
    async def wait(self, seconds: float, cancel_event: asyncio.Event) -> None: ...


class ExternalBacnetTestRuntime(TestRuntime):
    """READ ONLY. Every read opens the same short-lived, isolated discovery
    session every other external-BACnet read in this app already uses
    (src/api/routers/discovery.py's _discovery_session()) -- this is also
    what gives freshness-by-construction, since there is no per-object
    cache to go stale (see src/legacy.py's sync_external_objects docstring:
    present values are never persisted)."""

    def __init__(self, device: dict, resolution_map: dict[str, dict]):
        super().__init__(resolution_map)
        self._device = device

    async def read(self, point_type: str) -> Any:
        from ..api.routers.discovery import DiscoveryBindError, _discovery_session

        obj = self.resolve_point(point_type)
        points = [(obj["object_type"], obj["object_instance"])]

        try:
            async with _discovery_session() as discovery:
                values = await discovery.read_present_values(
                    self._device["external_host"], self._device["device_instance"], points,
                )
        except DiscoveryBindError as exc:
            raise ExecutionError(f"Could not reach the external device: {exc}") from exc

        return values.get((obj["object_type"], obj["object_instance"]))

    def now(self) -> float:
        return time.monotonic()

    async def wait(self, seconds: float, cancel_event: asyncio.Event) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if cancel_event.is_set():
                return
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=min(_WAIT_POLL_SECONDS, remaining))
            except asyncio.TimeoutError:
                continue
            return  # cancel_event fired


class SimulationTestRuntime(TestRuntime):
    """READ ONLY -- never calls anything that mutates simulator state
    (present_value, behavior, priority arrays). Reuses the simulator's own
    clock (engine.state.elapsed_seconds / engine.clock_state) rather than
    inventing a second one; since elapsed_seconds only advances while
    clock_state == "running", pausing the simulation automatically stalls
    both wait() and wait_until's timeout without any extra plumbing."""

    def __init__(self, engine: Any, resolution_map: dict[str, dict]):
        super().__init__(resolution_map)
        self._engine = engine

    async def read(self, point_type: str) -> Any:
        obj = self.resolve_point(point_type)
        return self._engine.get_object_value(obj["id"])

    def now(self) -> float:
        return self._engine.state.elapsed_seconds

    async def wait(self, seconds: float, cancel_event: asyncio.Event) -> None:
        start = self._engine.state.elapsed_seconds
        while True:
            if self._engine.state.elapsed_seconds - start >= seconds:
                return
            if cancel_event.is_set():
                return
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=_WAIT_POLL_SECONDS)
            except asyncio.TimeoutError:
                continue
            return  # cancel_event fired
