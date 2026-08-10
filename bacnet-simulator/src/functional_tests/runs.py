"""Run orchestration: pre-flight resolution (trust boundary -- always
re-resolved server-side, never trusts a frontend-supplied mapping),
concurrency guard, runtime selection, and the in-memory cancellation
registry backing the background execution task.

Two-step API, deliberately split so the DB-heavy part is a single
synchronous function (one asyncio.to_thread hop from the router) and the
task-spawning part runs directly on the event loop (asyncio.create_task
must not happen inside a worker thread):

    run_row, resolution_map = prepare_run(database, test, device)   # sync
    start_execution(app_state, database, run_row, ...)              # async-context, not itself awaited
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .executor import GraphExecutor
from .operands import ExecutionError
from .resolution import PointResolution, build_resolution, collect_point_types
from .runtime import ExternalBacnetTestRuntime, SimulationTestRuntime


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResolutionError(Exception):
    """Raised when one or more semantic points referenced by the test
    definition don't resolve cleanly against the target -- carries the
    full per-point-type resolution map so the router can return the same
    structured shape the /resolve preview endpoint already uses."""

    def __init__(self, resolution: dict[str, PointResolution]):
        self.resolution = resolution
        super().__init__("One or more required points did not resolve")


class ActiveRunExistsError(Exception):
    """Raised when this (functional_test_id, target_device_id) pair
    already has a pending/running run -- the one conservative concurrency
    guard requested; unrelated test/target pairs are never blocked."""

    def __init__(self, existing_run: dict):
        self.existing_run = existing_run
        super().__init__("A run for this test and target is already active")


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


def prepare_run(database: Any, test: dict, device: dict) -> tuple[dict, dict[str, dict]]:
    """Fully synchronous -- callable via a single asyncio.to_thread hop."""
    definition = test["definition"]
    point_types = collect_point_types(definition)
    resolution = build_resolution(database, device["id"], test["equipment_type"], point_types)

    unresolved = {pt: r for pt, r in resolution.items() if r.status != "resolved"}
    if unresolved:
        raise ResolutionError(resolution)

    existing = database.find_active_functional_test_run(test["id"], device["id"])
    if existing is not None:
        raise ActiveRunExistsError(existing)

    execution_mode = "external" if device.get("source_type") == "external-bacnet" else "simulation"
    run_row = database.create_functional_test_run({
        "functional_test_id": test["id"],
        "target_device_id": device["id"],
        "execution_mode": execution_mode,
    })

    resolution_map = {pt: r.object for pt, r in resolution.items()}
    return run_row, resolution_map


def start_execution(
    app_state: Any,
    database: Any,
    run_row: dict,
    device: dict,
    execution_mode: str,
    definition: dict,
    resolution_map: dict[str, dict],
) -> None:
    """Must be called from the event loop (not inside asyncio.to_thread) --
    creates the asyncio.Task that actually executes the graph."""
    if execution_mode == "external":
        runtime = ExternalBacnetTestRuntime(device, resolution_map)
    else:
        engine = getattr(app_state, "engine", None)
        if engine is None:
            raise ExecutionError("Simulation engine is unavailable")
        runtime = SimulationTestRuntime(engine, resolution_map)

    cancel_event = asyncio.Event()
    registry = _get_registry(app_state)
    run_id = run_row["id"]

    async def _task_body() -> None:
        try:
            await _run_and_finalize(database, run_id, runtime, definition, cancel_event)
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


async def _run_and_finalize(
    database: Any, run_id: int, runtime: Any, definition: dict, cancel_event: asyncio.Event,
) -> None:
    async def on_progress(node_id: str, entry: dict) -> None:
        await asyncio.to_thread(database.append_functional_test_run_detail, run_id, node_id, entry)

    await asyncio.to_thread(
        database.update_functional_test_run, run_id, state="running", started_at=_now_iso(),
    )

    executor = GraphExecutor(definition, runtime, cancel_event, on_progress=on_progress)

    try:
        result = await executor.run()
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
