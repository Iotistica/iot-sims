"""GraphExecutor -- walks a saved FunctionalTestDefinition node by node
against a TestRuntime. One implementation shared by both external and
simulated targets; no `if execution_mode == ...` branches live here, only
in how the TestRuntime itself was constructed (see runs.py).

Execution preconditions (checked here, not a re-implementation of the
frontend's full graph-quality validator -- see validation.py's own
docstring for that boundary): exactly one `start` node, and a resolvable,
unambiguous outgoing edge at every non-terminal step. Violating either
aborts immediately via ExecutionError rather than partially executing --
"never guess" extended to graph traversal itself.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from .operands import ExecutionError, compare, evaluate_operand
from .runtime import TestRuntime

# Guards against a cyclic graph (e.g. a Verify "fail" edge looping back to
# an earlier Wait) running forever in a background task.
MAX_STEPS = 500

_END_RESULT_TO_STATE = {"pass": "passed", "fail": "failed", "inconclusive": "inconclusive"}

OnProgress = Optional[Callable[[str, dict], Awaitable[None]]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExecutionResult:
    state: str  # "passed" | "failed" | "inconclusive" | "cancelled"
    result: Optional[str] = None
    message: Optional[str] = None


@dataclass
class _NodeOutcome:
    handle: Optional[str] = None
    terminate: Optional[ExecutionResult] = None
    cancelled: bool = False


class GraphExecutor:
    def __init__(
        self,
        definition: dict,
        runtime: TestRuntime,
        cancel_event: asyncio.Event,
        on_progress: OnProgress = None,
    ):
        self._nodes_by_id: dict[str, dict] = {n["id"]: n for n in definition.get("nodes", [])}
        self._edges: list[dict] = definition.get("edges", [])
        self._runtime = runtime
        self._cancel_event = cancel_event
        self._on_progress = on_progress
        self.variables: dict[str, Any] = {}

    async def run(self) -> ExecutionResult:
        start_nodes = [n for n in self._nodes_by_id.values() if n.get("type") == "start"]
        if len(start_nodes) != 1:
            raise ExecutionError(
                f"Cannot execute: expected exactly one start node, found {len(start_nodes)}"
            )

        node = start_nodes[0]
        steps = 0

        while True:
            if self._cancel_event.is_set():
                return ExecutionResult(state="cancelled")

            steps += 1
            if steps > MAX_STEPS:
                raise ExecutionError(
                    f"Execution exceeded the maximum of {MAX_STEPS} steps (possible cycle in the graph)"
                )

            outcome = await self._execute_node(node)

            if outcome.cancelled or self._cancel_event.is_set():
                return ExecutionResult(state="cancelled")

            if outcome.terminate is not None:
                return outcome.terminate

            node = self._next_node(node, outcome.handle)

    def _next_node(self, node: dict, handle: Optional[str]) -> dict:
        candidates = [
            e for e in self._edges
            if e.get("source") == node["id"] and e.get("source_handle") == handle
        ]
        if len(candidates) != 1:
            raise ExecutionError(
                f"Node {node['id']!r} does not have exactly one outgoing edge for handle {handle!r}"
            )
        target_id = candidates[0].get("target")
        target = self._nodes_by_id.get(target_id)
        if target is None:
            raise ExecutionError(f"Edge from {node['id']!r} references a missing node {target_id!r}")
        return target

    async def _execute_node(self, node: dict) -> _NodeOutcome:
        node_id = node["id"]
        node_type = node.get("type")
        params = node.get("params") or {}
        started_at = _now_iso()

        handler = getattr(self, f"_handle_{node_type}", None)
        if handler is None:
            raise ExecutionError(f"Unsupported node type: {node_type!r}")

        outcome, detail_outcome, message = await handler(node_id, params)

        if not outcome.cancelled:
            await self._record(node_id, node_type, detail_outcome, message, started_at)

        return outcome

    async def _record(
        self, node_id: str, node_type: str, outcome: str, message: Optional[str], started_at: str,
    ) -> None:
        entry = {
            "node_id": node_id,
            "type": node_type,
            "outcome": outcome,
            "message": message,
            "started_at": started_at,
            "finished_at": _now_iso(),
        }
        if self._on_progress is not None:
            await self._on_progress(node_id, entry)

    # ── Node handlers ────────────────────────────────────────────────────
    # Each returns (outcome, detail_outcome, message).

    async def _handle_start(self, node_id, params):
        return _NodeOutcome(handle=None), "ok", None

    async def _handle_wait(self, node_id, params):
        seconds = params["seconds"]
        await self._runtime.wait(seconds, self._cancel_event)
        if self._cancel_event.is_set():
            return _NodeOutcome(cancelled=True), "ok", None
        return _NodeOutcome(handle=None), "ok", f"waited {seconds} seconds"

    async def _handle_wait_until(self, node_id, params):
        point_type = params["point_type"]
        operator = params["operator"]
        value = params["value"]
        timeout_seconds = params["timeout_seconds"]

        start_time = self._runtime.now()
        last_observed = None

        while True:
            if self._cancel_event.is_set():
                return _NodeOutcome(cancelled=True), "ok", None

            last_observed = await self._runtime.read(point_type)
            if compare(operator, last_observed, value):
                elapsed = self._runtime.now() - start_time
                message = f"observed {last_observed!r} after {elapsed:.1f}s"
                return _NodeOutcome(handle=None), "ok", message

            if self._runtime.now() - start_time >= timeout_seconds:
                message = f"timed out after {timeout_seconds}s (last observed {last_observed!r})"
                return (
                    _NodeOutcome(terminate=ExecutionResult(state="failed", result="fail", message=message)),
                    "fail",
                    message,
                )

            try:
                await asyncio.wait_for(self._cancel_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            return _NodeOutcome(cancelled=True), "ok", None

    async def _handle_capture(self, node_id, params):
        point_type = params["point_type"]
        variable = params["variable"]
        value = await self._runtime.read(point_type)
        self.variables[variable] = value
        return _NodeOutcome(handle=None), "ok", f"captured {value!r} as {variable}"

    async def _handle_verify(self, node_id, params):
        left = await evaluate_operand(params["left"], self.variables, self._runtime)
        right = await evaluate_operand(params["right"], self.variables, self._runtime)
        passed = compare(params["operator"], left, right)
        message = f"observed {left!r} {params['operator']} {right!r} -> {'PASS' if passed else 'FAIL'}"
        return _NodeOutcome(handle="pass" if passed else "fail"), ("pass" if passed else "fail"), message

    async def _handle_compare(self, node_id, params):
        left = await evaluate_operand(params["left"], self.variables, self._runtime)
        right = await evaluate_operand(params["right"], self.variables, self._runtime)
        passed = compare(params["operator"], left, right)
        message = f"observed {left!r} {params['operator']} {right!r} -> {'PASS' if passed else 'FAIL'}"
        return _NodeOutcome(handle=None), ("pass" if passed else "fail"), message

    async def _handle_end(self, node_id, params):
        result = params.get("result", "inconclusive")
        message = params.get("message")
        return (
            _NodeOutcome(terminate=ExecutionResult(state=_END_RESULT_TO_STATE[result], result=result, message=message)),
            result,
            message,
        )
