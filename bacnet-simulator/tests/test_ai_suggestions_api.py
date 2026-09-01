"""HTTP-level tests for POST /devices/{id}/semantic-suggestions/points/{id}/ai
-- monkeypatches the Azure client construction and suggest_point_via_ai()
directly (no real network/credentials), confirming: the route wires a
successful AI result through correctly (source='ai'); a simulated Azure
failure produces a generic, credential-free error and touches no data; an
out-of-vocabulary suggested_class from a misbehaving model is rejected
server-side rather than passed through -- the one authoritative vocabulary
check this feature relies on."""
from __future__ import annotations

import pytest

from src.api.routers import semantic_suggestions as ss_module
from src.semantics.ai_suggestions import AiPointSuggestion


def _create_simulated_ahu(client):
    device = client.post("/devices", json={"device_instance": 1003, "name": "AHU-1"}).json()
    sat = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius",
    }).json()
    temp1 = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input", "object_instance": 2, "name": "TEMP1",
    }).json()
    return device, [sat, temp1]


class _FakeAzureClient:
    """Stands in for the built LLM client -- construction never touches
    real env vars/network; .parse() is never called directly since the
    tests below patch suggest_point_via_ai() itself, one level up."""
    def __init__(self, *a, **kw):
        pass


@pytest.fixture
def no_real_azure_client(monkeypatch):
    monkeypatch.setattr(ss_module, "build_llm_client", lambda settings: _FakeAzureClient())


def test_ai_suggestion_success_marks_source_ai(client, database, no_real_azure_client, monkeypatch):
    device, (sat, temp1) = _create_simulated_ahu(client)
    monkeypatch.setattr(
        ss_module, "suggest_point_via_ai",
        lambda *a, **kw: AiPointSuggestion(suggested_class="Temperature_Sensor", confidence="medium", reason="generic temperature point"),
    )

    before = database.get_objects(device["id"])
    resp = client.post(f"/devices/{device['id']}/semantic-suggestions/points/{temp1['id']}/ai")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "ai"
    assert body["suggested_class"] == "Temperature_Sensor"
    assert body["confidence"] == "medium"
    assert body["reasons"] == ["generic temperature point"]

    # Still side-effect-free -- an AI suggestion is exactly as inert as a
    # rule-based one until Apply Selected.
    assert database.get_objects(device["id"]) == before


def test_ai_failure_returns_generic_error_and_no_mutation(client, database, no_real_azure_client, monkeypatch):
    device, (sat, temp1) = _create_simulated_ahu(client)

    def _raise(*a, **kw):
        raise RuntimeError(f"Azure OpenAI request failed -- endpoint=https://secret.example.com key=abc123")

    monkeypatch.setattr(ss_module, "suggest_point_via_ai", _raise)

    before = database.get_objects(device["id"])
    resp = client.post(f"/devices/{device['id']}/semantic-suggestions/points/{temp1['id']}/ai")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail == "AI suggestion is unavailable. Check the AI provider configuration in Settings."
    # The real exception (which could carry endpoint/key detail) must never
    # reach the response body.
    assert "secret.example.com" not in detail
    assert "abc123" not in detail

    assert database.get_objects(device["id"]) == before


def test_out_of_vocabulary_class_is_rejected_server_side(client, no_real_azure_client, monkeypatch):
    device, (sat, temp1) = _create_simulated_ahu(client)
    monkeypatch.setattr(
        ss_module, "suggest_point_via_ai",
        lambda *a, **kw: AiPointSuggestion(suggested_class="Not_A_Real_Brick_Class", confidence="high", reason="hallucinated"),
    )

    resp = client.post(f"/devices/{device['id']}/semantic-suggestions/points/{temp1['id']}/ai")
    assert resp.status_code == 200
    body = resp.json()
    # Rejected class collapses to no suggestion -- never passed through,
    # confidence forced to "none" alongside it rather than showing a
    # confident tag next to an empty class.
    assert body["suggested_class"] is None
    assert body["confidence"] == "none"


def test_ai_endpoint_404s_for_unknown_object(client, no_real_azure_client):
    device = client.post("/devices", json={"device_instance": 1004, "name": "AHU-2"}).json()
    resp = client.post(f"/devices/{device['id']}/semantic-suggestions/points/999999/ai")
    assert resp.status_code == 404


# ─── POST .../points/{id}/ai-accept -- recording an applied AI suggestion ────

def test_ai_accept_records_suggestion(client, database):
    device, (sat, temp1) = _create_simulated_ahu(client)
    resp = client.post(
        f"/devices/{device['id']}/semantic-suggestions/points/{temp1['id']}/ai-accept",
        json={"suggested_class": "Temperature_Sensor", "accepted_class": "Temperature_Sensor", "confidence": "medium", "reason": "generic temperature point"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["object_id"] == temp1["id"]
    assert body["device_id"] == device["id"]
    assert body["suggested_class"] == "Temperature_Sensor"
    assert body["accepted_class"] == "Temperature_Sensor"
    assert body["confidence"] == "medium"
    assert body["reason"] == "generic temperature point"

    with database._conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM ai_suggestion_acceptances")]
    assert len(rows) == 1
    assert rows[0]["object_id"] == temp1["id"]


def test_ai_accept_records_override_distinctly(client, database):
    """The user changed the picker before applying -- accepted_class differs
    from suggested_class, both are still recorded (not collapsed to one)."""
    device, (sat, temp1) = _create_simulated_ahu(client)
    resp = client.post(
        f"/devices/{device['id']}/semantic-suggestions/points/{temp1['id']}/ai-accept",
        json={"suggested_class": "Temperature_Sensor", "accepted_class": "Humidity_Sensor", "confidence": "low", "reason": "weak match"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["suggested_class"] == "Temperature_Sensor"
    assert body["accepted_class"] == "Humidity_Sensor"


def test_ai_accept_404s_for_unknown_object(client):
    device = client.post("/devices", json={"device_instance": 1005, "name": "AHU-3"}).json()
    resp = client.post(
        f"/devices/{device['id']}/semantic-suggestions/points/999999/ai-accept",
        json={"suggested_class": "Temperature_Sensor", "accepted_class": "Temperature_Sensor", "confidence": "medium", "reason": ""},
    )
    assert resp.status_code == 404


def test_ai_accept_404s_for_unknown_device(client):
    resp = client.post(
        "/devices/999999/semantic-suggestions/points/1/ai-accept",
        json={"suggested_class": "Temperature_Sensor", "accepted_class": "Temperature_Sensor", "confidence": "medium", "reason": ""},
    )
    assert resp.status_code == 404
