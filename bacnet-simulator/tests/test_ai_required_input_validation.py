"""Regression tests for AI-backed Simulation Model validation, now unified
with FMU's own `_validate_mapping_contract` path.

Background: an AI-backed Simulation Model could once be saved/enabled with
zero required input mappings -- _validate_mapping_contract returned
immediately for provider_type == "ai" before any check existed, silently
skipping the same "required mappings are missing" guard FMU already had.
That's exactly how a real device (Zone 2 Controller) ended up attached to an
AI model with 0 usable inputs -- the gap wasn't caught at save time, only
much later as "0 inputs" on the (unrelated, correctly-designed) retraining
eligibility screen.

A follow-up fix added a narrow `_validate_ai_required_inputs`, specifically
because AI had no Constant/Aggregate input-sourcing UI at all yet -- but now
that AI has gained the exact same Constant/Point/Aggregate sourcing FMU has
(see the plan this file accompanies), that narrow validator is gone, and AI
runs through the SAME shared contract FMU already relies on: self-loop
prevention, output-ownership, aggregate-source validation, input-exposure
validation, and the required-input check (including its constant-sourcing
discharge). These tests exercise that shared path from the AI side.

One real bug surfaced and fixed while unifying this: _normalized_parameters'
own fallback backfills an unmapped, catalog-default-less input as "constant"
sourced with input_defaults[name] = None -- the required-input discharge
used to check only key *membership* in input_defaults, which would wrongly
treat that None as "satisfied". It now checks the value is not None.
"""
from __future__ import annotations

from src.api.routers import simulation as simulation_router
from src.simulation.models.registry import ModelDefinition, VariableDefinition


def _ai_like_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="TESTAI",
        label="Test AI Model",
        provider_type="ai",
        description="",
        parameters=(),
        variables=(
            # required, no manifest default -- the model cannot run without
            # a live value for this, so it must be mapped (or explicitly
            # given a constant value in the payload itself).
            VariableDefinition("required_input", "Required Input", "input", required=True),
            # required, but has a manifest default -- the runtime falls
            # back to it when unmapped, so this must stay save-able unmapped
            # (the dead-end case: an FMU input that was constant-sourced,
            # whose value carried into the AI manifest as its own default).
            VariableDefinition("defaulted_input", "Defaulted Input", "input", default=5.0, required=True),
            # not required at all -- always optional to map.
            VariableDefinition("optional_input", "Optional Input", "input", required=False),
            VariableDefinition("out1", "Output 1", "output", required=True),
        ),
        factory=lambda parameters: None,
        runtime_model="TESTAI",
    )


def _make_device_and_points(client, database, *, instance: int, count: int) -> tuple[dict, list[dict]]:
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": instance, "name": "AI-Test"}).json()
    # Created directly via the database fixture, not POST /devices/{id}/objects
    # -- objects_router fails to import in this test env (a pre-existing,
    # unrelated environment issue -- see tests/conftest.py's _routers(),
    # which silently drops it on ImportError), matching the same "166
    # pre-existing failures" baseline this session already established.
    points = [
        database.create_object(device["id"], {
            "object_type": "analog-value", "object_instance": i + 1, "name": f"Point-{i + 1}",
            "units": "percent", "behavior": "constant", "behavior_params": '{"value":0}',
            "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
            "polarity": "normal", "point_type": None,
        })
        for i in range(count)
    ]
    return device, points


def _ai_payload(
    device_id: int, *, mappings: list[dict], enabled: bool = True,
    parameters: dict | None = None, aggregate_mappings: list[dict] | None = None,
) -> dict:
    return {
        "name": "Test AI Model",
        "provider_type": "ai",
        "model_type": "TESTAI",
        "enabled": enabled,
        "created_from_device_id": device_id,
        "parameters": parameters or {},
        "mappings": mappings,
        "aggregate_mappings": aggregate_mappings or [],
    }


def test_enabled_ai_model_rejects_unmapped_required_no_default_input(client, database, monkeypatch):
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _ai_like_definition())
    device, points = _make_device_and_points(client, database, instance=9001, count=1)

    payload = _ai_payload(device["id"], mappings=[
        {"variable": "out1", "direction": "output", "point_id": points[0]["id"]},
    ])
    resp = client.post("/simulation/models", json=payload)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["message"] == "Required model mappings are missing"
    assert {m["variable"] for m in detail["missing"]} == {"required_input"}


def test_enabled_ai_model_succeeds_once_required_input_is_mapped(client, database, monkeypatch):
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _ai_like_definition())
    # Save-time validation is the only thing under test here -- activation
    # (reload_model -> register_model_config -> a real HTTP call to
    # iot-models) is a separate concern with no live runtime in this test
    # environment, same reason the FMU self-loop tests in
    # test_self_referential_fmu_mapping.py use enabled=False throughout.
    # This test needs enabled=True specifically to prove the mapping check
    # itself passes, so activation is stubbed instead of sidestepped.
    monkeypatch.setattr(simulation_router, "reload_model", lambda *a, **k: {"enabled": True})
    device, points = _make_device_and_points(client, database, instance=9002, count=2)

    payload = _ai_payload(device["id"], mappings=[
        {"variable": "required_input", "direction": "input", "point_id": points[0]["id"]},
        {"variable": "out1", "direction": "output", "point_id": points[1]["id"]},
    ])
    resp = client.post("/simulation/models", json=payload)

    assert resp.status_code == 201, resp.text


def test_enabled_ai_model_does_not_require_a_defaulted_input_to_be_mapped(client, database, monkeypatch):
    """The dead-end case: defaulted_input is `required=True` in the manifest
    but carries its own catalog `default` -- _normalized_parameters backfills
    it as constant-sourced with that real value, so it must stay save-able
    unmapped, or a converted FMU-constant input with a surviving manifest
    default would become permanently unsaveable."""
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _ai_like_definition())
    monkeypatch.setattr(simulation_router, "reload_model", lambda *a, **k: {"enabled": True})
    device, points = _make_device_and_points(client, database, instance=9003, count=2)

    payload = _ai_payload(device["id"], mappings=[
        {"variable": "required_input", "direction": "input", "point_id": points[0]["id"]},
        {"variable": "out1", "direction": "output", "point_id": points[1]["id"]},
        # defaulted_input and optional_input both deliberately absent.
    ])
    resp = client.post("/simulation/models", json=payload)

    assert resp.status_code == 201, resp.text
    # _normalized_parameters actually did the backfill -- not just "didn't
    # error", the catalog's own default (5.0) is now a real, visible,
    # editable constant value, not an invisible fallback.
    assert resp.json()["parameters"]["input_defaults"]["defaulted_input"] == 5.0
    assert resp.json()["parameters"]["input_sources"]["defaulted_input"] == "constant"


def test_disabled_ai_model_draft_may_omit_required_input_mapping(client, database, monkeypatch):
    """Disabled configs are drafts -- same rule the FMU path already
    follows: don't demand the full runtime contract until activation."""
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _ai_like_definition())
    device, points = _make_device_and_points(client, database, instance=9004, count=1)

    payload = _ai_payload(device["id"], mappings=[
        {"variable": "out1", "direction": "output", "point_id": points[0]["id"]},
    ], enabled=False)
    resp = client.post("/simulation/models", json=payload)

    assert resp.status_code == 201, resp.text


# ── AI now shares FMU's full Constant/Point/Aggregate contract ──────────────

def test_enabled_ai_model_succeeds_via_explicit_constant_sourcing(client, database, monkeypatch):
    """A required input satisfied by an explicit, user-entered constant
    value (parameters.input_sources/input_defaults) -- not the model's own
    catalog default, a real per-save override -- must be save-able with no
    mapping at all, the exact capability this change adds to AI."""
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _ai_like_definition())
    monkeypatch.setattr(simulation_router, "reload_model", lambda *a, **k: {"enabled": True})
    device, points = _make_device_and_points(client, database, instance=9005, count=1)

    payload = _ai_payload(
        device["id"],
        mappings=[{"variable": "out1", "direction": "output", "point_id": points[0]["id"]}],
        parameters={
            "input_sources": {"required_input": "constant"},
            "input_defaults": {"required_input": 42.0},
        },
    )
    resp = client.post("/simulation/models", json=payload)

    assert resp.status_code == 201, resp.text
    assert resp.json()["parameters"]["input_defaults"]["required_input"] == 42.0


def test_enabled_ai_model_rejects_input_output_self_loop(client, database, monkeypatch):
    """The self-loop check (a point can't be both a model input source and
    output target) never ran for AI at all before this change -- confirm it
    now does, the same protection FMU already had."""
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _ai_like_definition())
    device, points = _make_device_and_points(client, database, instance=9006, count=1)

    payload = _ai_payload(device["id"], mappings=[
        {"variable": "required_input", "direction": "input", "point_id": points[0]["id"]},
        {"variable": "out1", "direction": "output", "point_id": points[0]["id"]},
    ], parameters={"input_sources": {"required_input": "point"}})
    resp = client.post("/simulation/models", json=payload)

    assert resp.status_code == 422, resp.text
    assert "both a model input source and a model output target" in resp.json()["detail"]


def test_enabled_ai_model_aggregate_mapping_satisfies_required_input(client, database, monkeypatch):
    """An Aggregate-sourced required input (e.g. max across several points)
    persists and satisfies the required-input check, matching FMU."""
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _ai_like_definition())
    monkeypatch.setattr(simulation_router, "reload_model", lambda *a, **k: {"enabled": True})
    device, points = _make_device_and_points(client, database, instance=9007, count=3)

    payload = _ai_payload(
        device["id"],
        mappings=[{"variable": "out1", "direction": "output", "point_id": points[0]["id"]}],
        aggregate_mappings=[{
            "variable": "required_input", "direction": "input", "operation": "max",
            "point_ids": [points[1]["id"], points[2]["id"]], "weight_point_ids": None,
        }],
        parameters={"input_sources": {"required_input": "aggregate"}},
    )
    resp = client.post("/simulation/models", json=payload)

    assert resp.status_code == 201, resp.text
    # get_simulation_model merges plain + aggregate mappings into one
    # `mappings` list in the response (see store.py's own load) -- assert
    # on content, not count, since "mappings" isn't just the plain rows.
    aggregate_rows = [m for m in resp.json()["mappings"] if "point_ids" in m]
    assert aggregate_rows and aggregate_rows[0]["variable"] == "required_input"
    assert set(aggregate_rows[0]["point_ids"]) == {points[1]["id"], points[2]["id"]}
