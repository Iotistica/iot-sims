"""API-level tests for the Custom Graphs router
(src/api/routers/custom_graphs.py) + its definition validator
(validate_custom_graph_definition in src/bacnet/schemas.py), exercised
through the `client` fixture.

Mirrors tests/test_functional_tests_api.py's structure. Unlike a
functional test's definition, a custom graph's series entries are opaque
device_id/object_id references (resolved live against /points by the
frontend, never validated against the DB here) -- see
validate_custom_graph_definition's own comment for why. So these tests
don't need real devices/objects, just structurally valid definitions.
"""
from __future__ import annotations

import pytest


def _definition(**overrides):
    definition = {
        "version": 1,
        "series": [
            {"device_id": 1, "object_id": 10, "color": "#1890ff", "axis": "left", "visible": True},
            {"device_id": 2, "object_id": 20, "color": "#52c41a", "axis": "right", "visible": True},
        ],
        "time_range": "live",
    }
    definition.update(overrides)
    return definition


def _payload(**overrides):
    payload = {
        "name": "RTU vs. Zone Temps",
        "definition": _definition(),
    }
    payload.update(overrides)
    return payload


def test_create_get_update_delete_custom_graph(client):
    resp = client.post("/custom-graphs", json=_payload())
    assert resp.status_code == 201, resp.text
    graph = resp.json()
    assert graph["name"] == "RTU vs. Zone Temps"
    assert len(graph["definition"]["series"]) == 2
    assert graph["definition"]["series"][0]["device_id"] == 1
    graph_id = graph["id"]

    resp = client.get(f"/custom-graphs/{graph_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "RTU vs. Zone Temps"

    resp = client.get("/custom-graphs")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.put(
        f"/custom-graphs/{graph_id}",
        json=_payload(name="RTU vs. Zone Temps (Renamed)"),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "RTU vs. Zone Temps (Renamed)"

    resp = client.delete(f"/custom-graphs/{graph_id}")
    assert resp.status_code == 204

    resp = client.get(f"/custom-graphs/{graph_id}")
    assert resp.status_code == 404


def test_rename_via_update_preserves_definition(client):
    """SavedGraphsView.vue's Rename action resubmits the SAME definition
    with only the name changed -- confirm that round-trips exactly."""
    created = client.post("/custom-graphs", json=_payload()).json()

    resp = client.put(
        f"/custom-graphs/{created['id']}",
        json={"name": "Renamed Only", "definition": created["definition"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed Only"
    assert body["definition"] == created["definition"]


def test_get_missing_custom_graph_404(client):
    resp = client.get("/custom-graphs/999999")
    assert resp.status_code == 404


def test_update_missing_custom_graph_404(client):
    resp = client.put("/custom-graphs/999999", json=_payload())
    assert resp.status_code == 404


def test_delete_missing_custom_graph_404(client):
    resp = client.delete("/custom-graphs/999999")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "mutate,description",
    [
        (lambda d: d.update(series=[]), "empty series list"),
        (lambda d: d["series"][0].pop("device_id"), "missing device_id"),
        (lambda d: d["series"][0].__setitem__("device_id", "1"), "device_id not an integer"),
        (lambda d: d["series"][0].__setitem__("axis", "top"), "invalid axis"),
        (lambda d: d["series"][0].pop("color"), "missing color"),
        (lambda d: d["series"][0].__setitem__("color", ""), "empty color"),
        (lambda d: d["series"][0].__setitem__("visible", "yes"), "visible not a boolean"),
        (lambda d: d.update(time_range="1h"), "unsupported time_range (only 'live' exists this iteration)"),
        (
            lambda d: d["series"].append({"device_id": 1, "object_id": 10, "color": "#000", "axis": "left", "visible": True}),
            "duplicate (device_id, object_id) pair",
        ),
    ],
)
def test_invalid_definition_rejected(client, mutate, description):
    definition = _definition()
    mutate(definition)
    resp = client.post("/custom-graphs", json=_payload(definition=definition))
    assert resp.status_code == 400, f"{description}: expected 400, got {resp.status_code}: {resp.text}"


def test_empty_name_rejected(client):
    resp = client.post("/custom-graphs", json=_payload(name=""))
    assert resp.status_code == 422


def test_multiple_saved_graphs_listed_independently(client):
    client.post("/custom-graphs", json=_payload(name="Graph A"))
    client.post("/custom-graphs", json=_payload(name="Graph B"))

    resp = client.get("/custom-graphs")
    assert resp.status_code == 200
    names = {g["name"] for g in resp.json()}
    assert names == {"Graph A", "Graph B"}
