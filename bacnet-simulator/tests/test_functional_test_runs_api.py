"""API-level tests for run creation/cancellation
(src/api/routers/functional_tests.py's /resolve+/runs, and
src/api/routers/functional_test_runs.py) against a real FastAPI TestClient
+ real SimEngine(database) -- no tick loop started, so a simulated WAIT
never completes on its own, making cancellation/concurrency deterministic
without needing real multi-second sleeps (poll interval is monkeypatched
down for speed). Both endpoints are now bodyless -- every point in a saved
definition already carries its own device, so there's no target_device_id
for the caller to supply, and the concurrency guard is test-scoped rather
than (test, device)-scoped."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routers.functional_test_runs import router as functional_test_runs_router
from src.api.routers.functional_tests import router as functional_tests_router
from src.functional_tests import runtime as runtime_module
from src.simulation.engine import SimEngine


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(runtime_module, "_WAIT_POLL_SECONDS", 0.02)


@pytest.fixture
def sim_app(database):
    app = FastAPI()
    app.state.db = database
    app.state.engine = SimEngine(database)
    app.include_router(functional_tests_router)
    app.include_router(functional_test_runs_router)
    return app


@pytest.fixture
def sim_client(sim_app):
    with TestClient(sim_app) as c:
        yield c


# References no points at all -- readiness trivially passes with an empty
# points list, so these tests don't need any device/object fixtures.
_SLOW_DEFINITION = {
    "version": 1,
    "nodes": [
        {"id": "start", "type": "start", "params": {}},
        {"id": "wait", "type": "wait", "params": {"seconds": 999999}},
        {"id": "end", "type": "end", "params": {"result": "pass"}},
    ],
    "edges": [
        {"source": "start", "target": "wait", "source_handle": None},
        {"source": "wait", "target": "end", "source_handle": None},
    ],
    "layout": {},
}


def _make_slow_test(database, equipment_type="Boiler"):
    return database.create_functional_test({
        "name": "Slow Test", "description": "", "equipment_type": equipment_type,
        "definition": _SLOW_DEFINITION,
    })


def _wait_for_state(sim_client, run_id, states, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = sim_client.get(f"/functional-test-runs/{run_id}").json()
        if run["state"] in states:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached {states}, last seen state={run['state']!r}")


def test_run_creation_ignores_a_browser_supplied_body(database, sim_client):
    test_row = _make_slow_test(database)

    resp = sim_client.post(
        f"/functional-tests/{test_row['id']}/runs",
        json={"definition": {"nodes": [], "edges": []}},
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]

    # Cleanup.
    sim_client.post(f"/functional-test-runs/{run_id}/cancel")
    _wait_for_state(sim_client, run_id, {"cancelled"})

    # The stored definition must be completely untouched by the injected
    # "definition" key in the run-creation request body.
    reloaded = sim_client.get(f"/functional-tests/{test_row['id']}").json()
    assert reloaded["definition"] == _SLOW_DEFINITION


def test_duplicate_concurrent_run_rejected_409(database, sim_client):
    test_row = _make_slow_test(database)

    resp1 = sim_client.post(f"/functional-tests/{test_row['id']}/runs")
    assert resp1.status_code == 201, resp1.text
    run_id = resp1.json()["id"]

    resp2 = sim_client.post(f"/functional-tests/{test_row['id']}/runs")
    assert resp2.status_code == 409

    sim_client.post(f"/functional-test-runs/{run_id}/cancel")
    _wait_for_state(sim_client, run_id, {"cancelled"})


def test_unrelated_tests_run_concurrently(database, sim_client):
    test_a = _make_slow_test(database)
    test_b = _make_slow_test(database)

    resp_a = sim_client.post(f"/functional-tests/{test_a['id']}/runs")
    resp_b = sim_client.post(f"/functional-tests/{test_b['id']}/runs")

    assert resp_a.status_code == 201, resp_a.text
    assert resp_b.status_code == 201, resp_b.text

    for run_id in (resp_a.json()["id"], resp_b.json()["id"]):
        sim_client.post(f"/functional-test-runs/{run_id}/cancel")
        _wait_for_state(sim_client, run_id, {"cancelled"})


def test_cancel_transitions_running_test_to_cancelled(database, sim_client, sim_app):
    test_row = _make_slow_test(database)

    resp = sim_client.post(f"/functional-tests/{test_row['id']}/runs")
    run_id = resp.json()["id"]

    _wait_for_state(sim_client, run_id, {"running"})

    cancel_resp = sim_client.post(f"/functional-test-runs/{run_id}/cancel")
    assert cancel_resp.status_code == 200

    final = _wait_for_state(sim_client, run_id, {"cancelled"})
    assert final["state"] == "cancelled"

    # No orphaned task left registered after the run settles.
    registry = getattr(sim_app.state, "functional_test_run_registry", {})
    assert run_id not in registry


def test_run_creation_rejects_when_a_referenced_point_is_missing(database, sim_client):
    definition = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "cap", "type": "capture", "params": {"point": {"device_id": 999, "object_id": 1}, "variable": "x"}},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [
            {"source": "start", "target": "cap", "source_handle": None},
            {"source": "cap", "target": "end", "source_handle": None},
        ],
        "layout": {},
    }
    test_row = database.create_functional_test({
        "name": "Broken Test", "description": "", "equipment_type": "Boiler", "definition": definition,
    })

    resp = sim_client.post(f"/functional-tests/{test_row['id']}/runs")

    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["points"][0]["status"] == "missing_device"


def test_resolve_is_bodyless_and_reports_readiness(database, sim_client):
    test_row = _make_slow_test(database)

    resp = sim_client.post(f"/functional-tests/{test_row['id']}/resolve")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"points": []}
