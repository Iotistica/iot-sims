"""Run orchestration: pre-flight readiness (trust boundary -- always
re-checked server-side, never trusts a frontend-supplied point list),
concurrency guard, runtime construction, the in-memory cancellation
registry backing the background execution task, guaranteed restore of any
simulator state a Set node wrote, and activity-log entries for the run and
each point-touching step (reusing the same device Activity Log every manual
override/external write already logs to -- see src/legacy.py's
_log_event()/app.state.log_event -- so "why did this point change?" always
has an answer, whether it was a person or a Functional Test run).

Two-step API, deliberately split so the DB-heavy part is a single
synchronous function (one asyncio.to_thread hop from the router) and the
task-spawning part runs directly on the event loop (asyncio.create_task
must not happen inside a worker thread):

    run_row, point_cache = prepare_run(database, test)     # sync
    start_execution(app_state, database, run_row, ...)     # async-context, not itself awaited

Every point in a saved definition already carries its own device (see plan)
-- there is no more per-run "target device" to select, so a run's readiness
check and concurrency guard are both test-scoped, not device-scoped.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .executor import GraphExecutor
from .operands import ExecutionError
from .readiness import PointReadiness, build_point_cache, check_readiness, collect_point_refs
from .runtime import TestRuntime

LogFn = Callable[[Optional[int], str, str], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReadinessError(Exception):
    """Raised when one or more points referenced by the test definition
    aren't ready to run (missing device/object, or a Set node targeting a
    non-simulated device) -- carries the full readiness list so the router
    can return the same structured shape the /resolve preview already
    uses."""

    def __init__(self, readiness: list[PointReadiness]):
        self.readiness = readiness
        super().__init__("One or more required points are not ready")


class ActiveRunExistsError(Exception):
    """Raised when this functional test already has a pending/running run
    -- the one conservative concurrency guard requested; unrelated tests are
    never blocked."""

    def __init__(self, existing_run: dict):
        self.existing_run = existing_run
        super().__init__("A run for this test is already active")


@dataclass
class _RunHandle:
    task: "asyncio.Task[None]"
    cancel_event: asyncio.Event


def _get_registry(app_state: Any) -> dict[int, _RunHandle]:
    registry = getattr(app_state, "functional_test_run_registry", None)
    if registry is None:
        registry = {}
        app_state.functional_test_run_registry = registry
    return registry


def prepare_run(database: Any, test: dict) -> tuple[dict, dict[tuple[int, int], dict]]:
    """Fully synchronous -- callable via a single asyncio.to_thread hop."""
    definition = test["definition"]

    readiness = check_readiness(database, definition)
    if any(r.status != "ok" for r in readiness):
        raise ReadinessError(readiness)

    existing = database.find_active_functional_test_run(test["id"])
    if existing is not None:
        raise ActiveRunExistsError(existing)

    point_refs = collect_point_refs(definition)
    point_cache = build_point_cache(database, point_refs)

    execution_mode = "simulation" if any(
        entry["device"].get("source_type") != "external-bacnet" for entry in point_cache.values()
    ) else "external"
    # An empty point_cache (no point references at all -- e.g. a bare
    # Start -> Wait -> End test) defaults to "simulation", matching
    # TestRuntime's own wall-clock-vs-sim-clock default for the same case.
    if not point_cache:
        execution_mode = "simulation"

    run_row = database.create_functional_test_run({
        "functional_test_id": test["id"],
        "execution_mode": execution_mode,
    })

    return run_row, point_cache


def start_execution(
    app_state: Any,
    database: Any,
    run_row: dict,
    test_name: str,
    definition: dict,
    point_cache: dict[tuple[int, int], dict],
) -> None:
    """Must be called from the event loop (not inside asyncio.to_thread) --
    creates the asyncio.Task that actually executes the graph."""
    engine = getattr(app_state, "engine", None)
    if engine is None:
        raise ExecutionError("Simulation engine is unavailable")

    runtime = TestRuntime(engine, database, point_cache)
    log_event: Optional[LogFn] = getattr(app_state, "log_event", None)

    cancel_event = asyncio.Event()
    registry = _get_registry(app_state)
    run_id = run_row["id"]

    async def _task_body() -> None:
        try:
            await _run_and_finalize(
                database, engine, run_id, runtime, definition, cancel_event, point_cache, test_name, log_event,
            )
        finally:
            registry.pop(run_id, None)

    task = asyncio.create_task(_task_body())
    registry[run_id] = _RunHandle(task=task, cancel_event=cancel_event)


def cancel_run(app_state: Any, run_id: int) -> bool:
    registry = _get_registry(app_state)
    handle = registry.get(run_id)
    if handle is None:
        return False
    handle.cancel_event.set()
    return True


def _point_name(point_cache: dict[tuple[int, int], dict], point_ref: Any) -> Optional[str]:
    if not isinstance(point_ref, dict):
        return None
    cached = point_cache.get((point_ref.get("device_id"), point_ref.get("object_id")))
    return cached["object"]["name"] if cached else None


def _log_detail(point_cache: dict, log_event: Optional[LogFn], test_name: str, entry: dict) -> None:
    """Logs one executed node's outcome to the Activity Log of every device
    a point it touched belongs to -- e.g. a Set/Capture/Wait Until's own
    `point`, or either side of a Verify/Compare's point-kind operand.
    Node types that touch no point (Wait, a Verify comparing two
    constants) are intentionally not logged here -- the run-level
    started/finished entries already cover overall activity, and there's
    nothing device-scoped to attribute a bare Wait to."""
    if log_event is None:
        return

    node_type = entry.get("type") or ""
    message = entry.get("message") or ""

    device_ids: set[int] = set()
    primary_point = entry.get("point")
    point_name = _point_name(point_cache, primary_point)
    if isinstance(primary_point, dict) and isinstance(primary_point.get("device_id"), int):
        device_ids.add(primary_point["device_id"])

    for side in ("left", "right"):
        operand = entry.get(side)
        if isinstance(operand, dict) and operand.get("kind") == "point":
            op_point = operand.get("point")
            if isinstance(op_point, dict) and isinstance(op_point.get("device_id"), int):
                device_ids.add(op_point["device_id"])

    if not device_ids:
        return

    label = f"{node_type.upper()} {point_name}" if point_name else node_type.upper()
    text = f'Functional Test "{test_name}": {label} — {message}' if message else f'Functional Test "{test_name}": {label}'
    level = "warn" if entry.get("outcome") in ("fail", "error") else "info"

    for device_id in device_ids:
        log_event(device_id, level, text)


async def _run_and_finalize(
    database: Any, engine: Any, run_id: int, runtime: TestRuntime, definition: dict, cancel_event: asyncio.Event,
    point_cache: dict[tuple[int, int], dict], test_name: str, log_event: Optional[LogFn],
) -> None:
    async def on_progress(node_id: str, entry: dict) -> None:
        await asyncio.to_thread(database.append_functional_test_run_detail, run_id, node_id, entry)
        _log_detail(point_cache, log_event, test_name, entry)

    if log_event is not None:
        log_event(None, "info", f'Functional Test "{test_name}" started')

    await asyncio.to_thread(
        database.update_functional_test_run, run_id, state="running", started_at=_now_iso(),
    )

    executor = GraphExecutor(definition, runtime, cancel_event, on_progress=on_progress)
    final_state = "error"

    try:
        try:
            result = await executor.run()
            final_state = result.state
            await asyncio.to_thread(
                database.update_functional_test_run, run_id,
                state=result.state, result=result.result, result_message=result.message,
                finished_at=_now_iso(),
            )
        except ExecutionError as exc:
            await asyncio.to_thread(
                database.update_functional_test_run, run_id,
                state="error", error=str(exc), finished_at=_now_iso(),
            )
        except Exception as exc:  # pragma: no cover -- safety net against an unexpected bug
            await asyncio.to_thread(
                database.update_functional_test_run, run_id,
                state="error", error=f"internal error: {exc}", finished_at=_now_iso(),
            )
    finally:
        # Runs on every exit path -- pass, fail, cancel, or any of the
        # exception branches above -- so a Set node never leaves the
        # simulator modified, regardless of how the run ended.
        await restore_writes(database, engine, runtime, run_id, executor.writes, on_progress)
        if log_event is not None:
            log_event(None, "info", f'Functional Test "{test_name}" finished: {final_state}')


async def restore_writes(
    database: Any, engine: Any, runtime: TestRuntime, run_id: int, writes: list[dict],
    on_progress: Any,
) -> None:
    """Undoes every Set node's write, most-recent-first. Best-effort: one
    write's restore failing doesn't block the rest -- each outcome is
    recorded as its own synthetic "restore" detail entry so a partial
    restore is visible in the Run dialog rather than silently swallowed."""
    for entry in reversed(writes):
        point = entry["point"]
        started_at = _now_iso()
        try:
            if entry.get("commandable"):
                # BACnet's own relinquish -- releases the exact priority
                # slot this write used, reverting to whatever the next-
                # lower-priority/relinquish-default value already was.
                await runtime.write(point, None, entry["priority"])
                action = "relinquished"
            else:
                object_row = entry["object_row"]
                if object_row.get("behavior") == "manual":
                    await asyncio.to_thread(database.set_manual_value, object_row["id"], object_row.get("manual_value"))
                    engine.set_manual_value(object_row["id"], object_row.get("manual_value"))
                    action = "restored_manual"
                else:
                    await asyncio.to_thread(database.update_object, object_row["id"], object_row)
                    await engine.reload()
                    action = "restored_behavior"
            detail = {
                "node_id": "__restore__", "type": "restore", "outcome": "ok",
                "message": f"restored point after run", "started_at": started_at, "finished_at": _now_iso(),
                "point": point, "action": action,
            }
        except Exception as exc:  # pragma: no cover -- best-effort, never blocks the other restores
            detail = {
                "node_id": "__restore__", "type": "restore", "outcome": "error",
                "message": f"failed to restore point: {exc}", "started_at": started_at, "finished_at": _now_iso(),
                "point": point, "action": None,
            }
        if on_progress is not None:
            await on_progress("__restore__", detail)
