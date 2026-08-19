"""TestRuntime -- a single point-aware runtime the GraphExecutor talks to.
Replaces the old SimulationTestRuntime/ExternalBacnetTestRuntime split: since
every point reference is now a concrete PointRef ({device_id, object_id})
carrying its own device, a single run can touch both simulated and
external-BACnet points, so the runtime routes per-point rather than the
executor branching on a single run-wide execution_mode.

Reads: routed per-point by the referenced device's source_type -- external
reads go through the existing, already-shared _discovery_session()/
read_present_values() path (no second BACnet Application/socket); simulated
reads go through the existing SimEngine.get_object_value() (no second
simulation clock).

Writes: simulation-only. External BACnet WriteProperty is hard-blocked
everywhere else in this codebase by design (see src/bacnet/client/
README.md, ReadOnlyBACnetError) -- write() raises ExecutionError for any
point on an external-bacnet device, matching that constraint rather than
adding a second one. Commandable object types (analog-output, binary-
output, multi-state-output) write through the existing BACnet priority-array
mechanism; everything else writes through the existing manual-value
override path -- both are the exact same write paths the REST API already
exposes (PUT .../priority-array/{priority}, POST .../value), just called
in-process instead of over HTTP.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from ..core.config import COMMANDABLE_TYPES
from .operands import ExecutionError

# Fixed, modest poll cadence for wait()'s internal loop -- matches the
# admin UI's own external-value polling cadence (ObjectsPanel.vue's 3s
# interval) closely enough, and is cheap for the in-process simulation read
# path too. Deliberately not configurable in this phase -- keep it small.
_WAIT_POLL_SECONDS = 1.0


class TestRuntime:
    # Not a pytest test class despite the name -- this is the production
    # runtime GraphExecutor talks to.
    __test__ = False

    def __init__(self, engine: Any, database: Any, point_cache: dict[tuple[int, int], dict]):
        # point_cache: (device_id, object_id) -> {"object": <objects row>,
        # "device": <devices row>} -- built once by
        # readiness.build_point_cache before a runtime is ever constructed,
        # so every point a run references is guaranteed present here (see
        # _resolve()'s internal-consistency check below, not a fresh lookup).
        self._engine = engine
        self._database = database
        self._point_cache = point_cache
        # Wall-clock only when every referenced device is external (matches
        # today's pure external-commissioning behavior); any run touching a
        # simulated device -- including every run with a Set node, since
        # writes are simulation-only -- is governed by the sim's own clock,
        # so pausing the simulation automatically stalls waits/timeouts with
        # no extra plumbing. An empty point_cache (e.g. a Wait-only test
        # with no point references at all) defaults to the sim clock too.
        self._use_wall_clock = bool(point_cache) and all(
            entry["device"].get("source_type") == "external-bacnet"
            for entry in point_cache.values()
        )

    def _resolve(self, point_ref: dict) -> dict:
        key = (point_ref["device_id"], point_ref["object_id"])
        entry = self._point_cache.get(key)
        if entry is None:
            # Pre-flight readiness is supposed to guarantee every point
            # referenced by the graph is present in the cache -- this is an
            # internal-consistency error, not a normal user-facing one.
            raise ExecutionError(f"Point {point_ref!r} was not resolved before execution")
        return entry

    async def read(self, point_ref: dict) -> Any:
        entry = self._resolve(point_ref)
        obj = entry["object"]
        device = entry["device"]

        if device.get("source_type") == "external-bacnet":
            from ..api.routers.discovery import DiscoveryBindError, _discovery_session

            points = [(obj["object_type"], obj["object_instance"])]
            try:
                async with _discovery_session() as discovery:
                    values = await discovery.read_present_values(
                        device["external_host"], device["device_instance"], points,
                    )
            except DiscoveryBindError as exc:
                raise ExecutionError(f"Could not reach the external device: {exc}") from exc
            return values.get((obj["object_type"], obj["object_instance"]))

        return self._engine.get_object_value(obj["id"])

    async def write(self, point_ref: dict, value: Any, priority: Optional[int] = None) -> None:
        entry = self._resolve(point_ref)
        obj = entry["object"]
        device = entry["device"]

        if device.get("source_type") == "external-bacnet":
            raise ExecutionError("Set is only supported against simulated devices")

        if obj["object_type"] in COMMANDABLE_TYPES:
            ok = await self._engine.write_priority(obj["id"], priority, value)
            if not ok:
                raise ExecutionError(f"Could not write point {point_ref!r} (not live, or invalid priority)")
        else:
            await asyncio.to_thread(self._database.set_manual_value, obj["id"], value)
            ok = self._engine.set_manual_value(obj["id"], value)
            if not ok:
                raise ExecutionError(f"Could not write point {point_ref!r} (not live)")

    async def snapshot_for_restore(self, point_ref: dict) -> dict:
        """Captures whatever state write() is about to overwrite, so
        runs.restore_writes() can put it back exactly afterward. Commandable
        objects need no value snapshot -- BACnet's own priority-array
        relinquish (write(point, None, priority=<same priority>)) IS the
        restore, since it just releases that slot back to whatever the
        next-lower-priority/relinquish-default value already was. Others get
        the full pre-run object row (already cached in point_cache, built
        before any Set node ever runs -- no fresh read needed) so restore
        can put back not just the value but the original behavior/
        behavior_params if it wasn't already a plain manual override."""
        entry = self._resolve(point_ref)
        obj = entry["object"]
        if obj["object_type"] in COMMANDABLE_TYPES:
            return {"commandable": True}
        return {"commandable": False, "object_row": dict(obj)}

    def now(self) -> float:
        if self._use_wall_clock:
            return time.monotonic()
        return self._engine.state.elapsed_seconds

    async def wait(self, seconds: float, cancel_event: asyncio.Event) -> None:
        if self._use_wall_clock:
            deadline = time.monotonic() + seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or cancel_event.is_set():
                    return
                try:
                    await asyncio.wait_for(cancel_event.wait(), timeout=min(_WAIT_POLL_SECONDS, remaining))
                except asyncio.TimeoutError:
                    continue
                return  # cancel_event fired

        start = self._engine.state.elapsed_seconds
        while True:
            if self._engine.state.elapsed_seconds - start >= seconds or cancel_event.is_set():
                return
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=_WAIT_POLL_SECONDS)
            except asyncio.TimeoutError:
                continue
            return  # cancel_event fired
