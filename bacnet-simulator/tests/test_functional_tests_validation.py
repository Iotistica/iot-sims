"""Unit tests for structural definition validation
(src/functional_tests/validation.py), calling validate_functional_test_definition
directly rather than through the API -- covers the schema additions (Set
node, within_tolerance operator + tolerance, wait_until's operand-based
value, stable_for_seconds) that test_functional_tests_api.py's parametrized
cases don't each spell out individually."""
from __future__ import annotations

import pytest

from src.functional_tests.validation import validate_functional_test_definition


def _def(*extra_nodes, extra_edges=None):
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            *extra_nodes,
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [
            {"source": "start", "target": extra_nodes[0]["id"] if extra_nodes else "end", "source_handle": None},
            *(extra_edges or []),
            {"source": (extra_nodes[-1]["id"] if extra_nodes else "start"), "target": "end", "source_handle": None},
        ],
    }


POINT = {"device_id": 1, "object_id": 1}


def test_set_node_valid_shape_accepted():
    definition = _def({"id": "s", "type": "set", "params": {"point": POINT, "value": "OFF", "priority": 8}})
    validate_functional_test_definition(definition)  # must not raise


def test_set_node_without_priority_accepted():
    definition = _def({"id": "s", "type": "set", "params": {"point": POINT, "value": "OFF"}})
    validate_functional_test_definition(definition)  # must not raise


@pytest.mark.parametrize("params,description", [
    ({"value": "OFF"}, "missing point"),
    ({"point": POINT}, "missing value"),
    ({"point": POINT, "value": "OFF", "priority": 0}, "priority below range"),
    ({"point": POINT, "value": "OFF", "priority": 17}, "priority above range"),
    ({"point": {"object_id": 1}, "value": "OFF"}, "point missing device_id"),
])
def test_set_node_invalid_shapes_rejected(params, description):
    definition = _def({"id": "s", "type": "set", "params": params})
    with pytest.raises(ValueError):
        validate_functional_test_definition(definition)


@pytest.mark.parametrize("node_type", ["verify", "compare"])
def test_within_tolerance_requires_tolerance(node_type):
    definition = _def({
        "id": "n", "type": node_type, "params": {
            "left": {"kind": "constant", "value": 1},
            "operator": "within_tolerance",
            "right": {"kind": "constant", "value": 1},
        },
    })
    with pytest.raises(ValueError, match="tolerance"):
        validate_functional_test_definition(definition)


@pytest.mark.parametrize("node_type", ["verify", "compare"])
def test_within_tolerance_with_tolerance_accepted(node_type):
    definition = _def({
        "id": "n", "type": node_type, "params": {
            "left": {"kind": "constant", "value": 1},
            "operator": "within_tolerance",
            "right": {"kind": "constant", "value": 1},
            "tolerance": 0.5,
        },
    })
    validate_functional_test_definition(definition)  # must not raise


def test_wait_until_within_tolerance_requires_tolerance():
    definition = _def({
        "id": "wu", "type": "wait_until", "params": {
            "point": POINT, "operator": "within_tolerance",
            "value": {"kind": "constant", "value": 13}, "timeout_seconds": 60,
        },
    })
    with pytest.raises(ValueError, match="tolerance"):
        validate_functional_test_definition(definition)


def test_wait_until_value_must_be_a_well_formed_operand():
    definition = _def({
        "id": "wu", "type": "wait_until", "params": {
            "point": POINT, "operator": "eq",
            "value": {"kind": "not_a_real_kind"}, "timeout_seconds": 60,
        },
    })
    with pytest.raises(ValueError):
        validate_functional_test_definition(definition)


def test_wait_until_value_can_be_a_variable_operand():
    definition = _def({
        "id": "wu", "type": "wait_until", "params": {
            "point": POINT, "operator": "lt",
            "value": {"kind": "variable", "name": "baseline", "offset": -1},
            "timeout_seconds": 60,
        },
    })
    validate_functional_test_definition(definition)  # must not raise


def test_wait_until_negative_stable_for_seconds_rejected():
    definition = _def({
        "id": "wu", "type": "wait_until", "params": {
            "point": POINT, "operator": "eq",
            "value": {"kind": "constant", "value": True},
            "stable_for_seconds": -5, "timeout_seconds": 60,
        },
    })
    with pytest.raises(ValueError, match="stable_for_seconds"):
        validate_functional_test_definition(definition)


def test_wait_until_non_negative_stable_for_seconds_accepted():
    definition = _def({
        "id": "wu", "type": "wait_until", "params": {
            "point": POINT, "operator": "eq",
            "value": {"kind": "constant", "value": True},
            "stable_for_seconds": 30, "timeout_seconds": 60,
        },
    })
    validate_functional_test_definition(definition)  # must not raise
