"""Structured Operand evaluation for Verify/Compare/Wait Until nodes -- no
eval(), no expression language. Mirrors the exact Operand shape the builder
already persists (kind: point | constant | variable, with an optional
numeric offset on variable; a `point` operand now carries a PointRef
{device_id, object_id} rather than a semantic point_type string), see
validation.py's own operand checks.

Forward-compat note (see plan §8): a future `kind: 'aggregate'` variant is
additive to this dispatch -- keep evaluate_operand/compare raising on
unknown kind/operator rather than silently falling through, so adding a new
branch later is the only change required, never a rewrite of this one."""
from __future__ import annotations

from typing import Any

from .validation import OPERATORS


class ExecutionError(Exception):
    """A controlled, expected test-execution failure (missing captured
    variable, unresolvable point at read time, etc.) -- always caught by the
    executor and turned into a run state="error", never an unhandled
    exception dump."""


async def evaluate_operand(operand: dict, variables: dict, runtime: Any) -> Any:
    kind = operand.get("kind")

    if kind == "point":
        return await runtime.read(operand["point"])

    if kind == "constant":
        return operand.get("value")

    if kind == "variable":
        name = operand.get("name")
        if name not in variables:
            raise ExecutionError(f"Variable \"{name}\" was never captured")
        value = variables[name]
        offset = operand.get("offset")
        if offset:
            return value + offset
        return value

    raise ExecutionError(f"Unknown operand kind: {kind!r}")


def compare(operator: str, left: Any, right: Any, tolerance: Any = None) -> bool:
    if operator not in OPERATORS:
        raise ExecutionError(f"Unknown operator: {operator!r}")

    if operator == "eq":
        return left == right
    if operator == "neq":
        return left != right
    if operator == "gt":
        return left > right
    if operator == "gte":
        return left >= right
    if operator == "lt":
        return left < right
    if operator == "lte":
        return left <= right
    if operator == "within_tolerance":
        if tolerance is None:
            raise ExecutionError("within_tolerance comparison requires a tolerance")
        return abs(left - right) <= tolerance

    raise ExecutionError(f"Unhandled operator: {operator!r}")
