"""API-level tests for the Functional Tests router
(src/api/routers/functional_tests.py) + its structural definition
validator (src/functional_tests/validation.py), exercised through the
`client` fixture.

Per the approved plan, the backend validates that a definition is a
well-formed FunctionalTestDefinition (node types, params shapes, operand
shapes, edge references, duplicate ids) but deliberately does NOT do
graph-quality analysis (exactly-one-Start, reachability, unused capture
variables) -- that's the frontend validator's job. The last test in this
file asserts that boundary explicitly: a structurally valid but
graph-incomplete definition (two Start nodes) is accepted by the API."""
from __future__ import annotations

import pytest


def _minimal_definition():
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [
            {"source": "start", "target": "end", "source_handle": None},
        ],
        "layout": {"start": {"x": 0, "y": 0}, "end": {"x": 0, "y": 120}},
    }


def _payload(**overrides):
    payload = {
        "name": "Boiler Heating Response",
        "description": "",
        "equipment_type": "Boiler",
        "definition": _minimal_definition(),
    }
    payload.update(overrides)
    return payload


def test_create_get_update_delete_functional_test(client):
    resp = client.post("/functional-tests", json=_payload())
    assert resp.status_code == 201, resp.text
    test = resp.json()
    assert test["name"] == "Boiler Heating Response"
    assert test["equipment_type"] == "Boiler"
    assert test["definition"]["nodes"][0]["type"] == "start"
    assert "status" not in test
    test_id = test["id"]

    resp = client.get(f"/functional-tests/{test_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Boiler Heating Response"

    resp = client.get("/functional-tests")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.put(
        f"/functional-tests/{test_id}",
        json=_payload(name="Boiler Heating Response (Renamed)"),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Boiler Heating Response (Renamed)"

    resp = client.delete(f"/functional-tests/{test_id}")
    assert resp.status_code == 204

    resp = client.get(f"/functional-tests/{test_id}")
    assert resp.status_code == 404


def test_get_missing_functional_test_404(client):
    resp = client.get("/functional-tests/999999")
    assert resp.status_code == 404


def test_update_missing_functional_test_404(client):
    resp = client.put("/functional-tests/999999", json=_payload())
    assert resp.status_code == 404


def test_delete_missing_functional_test_404(client):
    resp = client.delete("/functional-tests/999999")
    assert resp.status_code == 404


def test_invalid_equipment_type_rejected(client):
    resp = client.post("/functional-tests", json=_payload(equipment_type="Not_A_Real_Class"))
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "mutate,description",
    [
        (lambda d: d["nodes"].append({"id": "x", "type": "loop", "params": {}}), "unsupported node type"),
        (lambda d: d["nodes"].append({"id": "w", "type": "wait", "params": {"seconds": "five"}}), "malformed wait params"),
        (
            lambda d: d["nodes"].append(
                {"id": "wu", "type": "wait_until", "params": {
                    "point_type": "Run_Status", "operator": "roughly", "value": True, "timeout_seconds": 60,
                }}
            ),
            "invalid operator",
        ),
        (
            lambda d: d["nodes"].append({"id": "e2", "type": "end", "params": {"result": "maybe"}}),
            "invalid End result",
        ),
        (
            lambda d: d["nodes"].append(
                {"id": "c", "type": "capture", "params": {"point_type": "Not_A_Point_Type", "variable": "x"}}
            ),
            "unknown point_type",
        ),
        (
            lambda d: d["nodes"].append(
                {"id": "v", "type": "verify", "params": {
                    "left": {"kind": "point"},
                    "operator": "eq",
                    "right": {"kind": "constant", "value": 1},
                }}
            ),
            "malformed Operand (point missing point_type)",
        ),
        (
            lambda d: d["edges"].append({"source": "start", "target": "nonexistent", "source_handle": None}),
            "edge references nonexistent node",
        ),
        (
            lambda d: d["edges"].append({"source": "start", "target": "end", "source_handle": "maybe"}),
            "invalid source_handle",
        ),
        (
            lambda d: d["nodes"].append({"id": "start", "type": "wait", "params": {"seconds": 1}}),
            "duplicate node id",
        ),
    ],
)
def test_structurally_invalid_definitions_rejected(client, mutate, description):
    definition = _minimal_definition()
    mutate(definition)
    resp = client.post("/functional-tests", json=_payload(definition=definition))
    assert resp.status_code == 400, f"{description} should be rejected: {resp.text}"


def test_graph_quality_issues_are_not_backend_concerns(client):
    """Two Start nodes is a graph-quality problem (frontend's job per the
    drawn boundary), not a structural one -- the backend must accept it."""
    definition = _minimal_definition()
    definition["nodes"].append({"id": "start2", "type": "start", "params": {}})
    resp = client.post("/functional-tests", json=_payload(definition=definition))
    assert resp.status_code == 201, resp.text
