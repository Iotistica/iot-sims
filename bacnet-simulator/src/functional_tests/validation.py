"""Structural validation for a FunctionalTest's `definition` JSON.

Boundary (see plan): this module only answers "is this a well-formed
FunctionalTestDefinition?" -- unsupported node types, malformed params,
dangling edge references, unknown point_type/equipment_type vocabulary.
It deliberately does NOT do graph-quality analysis (exactly-one-Start,
reachability, unused captured variables, terminal-path existence) --
that stays in the frontend validator (admin/src/functionalTestValidation.ts)
so it isn't built twice.
"""
from __future__ import annotations

import re
from typing import Any

from ..core.config import POINT_TYPES

NODE_TYPES = {"start", "wait", "wait_until", "capture", "verify", "compare", "end"}
OPERATORS = {"eq", "neq", "gt", "gte", "lt", "lte"}
END_RESULTS = {"pass", "fail", "inconclusive"}
SOURCE_HANDLES = {None, "pass", "fail"}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_functional_test_definition(definition: Any) -> None:
    """Raises ValueError with a human-readable message on the first
    structural problem found."""
    if not isinstance(definition, dict):
        raise ValueError("definition must be an object")

    nodes = definition.get("nodes")
    edges = definition.get("edges")
    if not isinstance(nodes, list):
        raise ValueError("definition.nodes must be a list")
    if not isinstance(edges, list):
        raise ValueError("definition.edges must be a list")

    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("each node must be an object")

        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("each node must have a non-empty string id")
        if node_id in node_ids:
            raise ValueError(f"duplicate node id: {node_id!r}")
        node_ids.add(node_id)

        node_type = node.get("type")
        if node_type not in NODE_TYPES:
            raise ValueError(f"node {node_id!r}: unsupported type {node_type!r}")

        params = node.get("params")
        if not isinstance(params, dict):
            raise ValueError(f"node {node_id!r}: params must be an object")

        _validate_params(node_id, node_type, params)

    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("each edge must be an object")

        source = edge.get("source")
        target = edge.get("target")
        if source not in node_ids:
            raise ValueError(f"edge references unknown source node: {source!r}")
        if target not in node_ids:
            raise ValueError(f"edge references unknown target node: {target!r}")

        source_handle = edge.get("source_handle")
        if source_handle not in SOURCE_HANDLES:
            raise ValueError(f"edge has invalid source_handle: {source_handle!r}")


def _validate_params(node_id: str, node_type: str, params: dict) -> None:
    if node_type == "start":
        return

    if node_type == "end":
        result = params.get("result")
        if result not in END_RESULTS:
            raise ValueError(
                f"node {node_id!r}: end.result must be one of {sorted(END_RESULTS)}"
            )
        message = params.get("message")
        if message is not None and not isinstance(message, str):
            raise ValueError(f"node {node_id!r}: end.message must be a string")
        return

    if node_type == "wait":
        seconds = params.get("seconds")
        if not _is_number(seconds) or seconds < 0:
            raise ValueError(f"node {node_id!r}: wait.seconds must be a non-negative number")
        return

    if node_type == "wait_until":
        _validate_point_type(node_id, "wait_until", params.get("point_type"))
        _validate_operator(node_id, "wait_until", params.get("operator"))
        if "value" not in params:
            raise ValueError(f"node {node_id!r}: wait_until.value is required")
        timeout_seconds = params.get("timeout_seconds")
        if not _is_number(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError(
                f"node {node_id!r}: wait_until.timeout_seconds must be a positive number"
            )
        return

    if node_type == "capture":
        _validate_point_type(node_id, "capture", params.get("point_type"))
        variable = params.get("variable")
        if not isinstance(variable, str) or not IDENTIFIER_RE.match(variable):
            raise ValueError(f"node {node_id!r}: capture.variable must be a valid identifier")
        return

    if node_type in ("verify", "compare"):
        _validate_operator(node_id, node_type, params.get("operator"))
        _validate_operand(node_id, node_type, "left", params.get("left"))
        _validate_operand(node_id, node_type, "right", params.get("right"))
        return


def _validate_operator(node_id: str, node_type: str, operator: Any) -> None:
    if operator not in OPERATORS:
        raise ValueError(
            f"node {node_id!r}: {node_type}.operator must be one of {sorted(OPERATORS)}"
        )


def _validate_point_type(node_id: str, node_type: str, point_type: Any) -> None:
    if not isinstance(point_type, str) or point_type not in POINT_TYPES:
        raise ValueError(
            f"node {node_id!r}: {node_type}.point_type {point_type!r} is not a recognized point class"
        )


def _validate_operand(node_id: str, node_type: str, side: str, operand: Any) -> None:
    if not isinstance(operand, dict):
        raise ValueError(f"node {node_id!r}: {node_type}.{side} must be an object")

    kind = operand.get("kind")
    if kind == "point":
        _validate_point_type(node_id, f"{node_type}.{side}", operand.get("point_type"))
    elif kind == "constant":
        if "value" not in operand:
            raise ValueError(f"node {node_id!r}: {node_type}.{side} constant operand missing value")
    elif kind == "variable":
        name = operand.get("name")
        if not isinstance(name, str) or not IDENTIFIER_RE.match(name):
            raise ValueError(
                f"node {node_id!r}: {node_type}.{side} variable operand has invalid name"
            )
        offset = operand.get("offset")
        if offset is not None and not _is_number(offset):
            raise ValueError(f"node {node_id!r}: {node_type}.{side} variable offset must be a number")
    else:
        raise ValueError(
            f"node {node_id!r}: {node_type}.{side} operand kind must be point/constant/variable"
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
