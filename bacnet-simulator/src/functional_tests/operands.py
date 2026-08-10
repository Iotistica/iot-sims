"""Structured Operand evaluation for Verify/Compare nodes -- no eval(),
no expression language. Mirrors the exact Operand shape the builder already
persists (kind: point | constant | variable, with an optional numeric
offset on variable), see validation.py's own operand checks."""
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
        return await runtime.read(operand["point_type"])

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


def compare(operator: str, left: Any, right: Any) -> bool:
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

    raise ExecutionError(f"Unhandled operator: {operator!r}")
