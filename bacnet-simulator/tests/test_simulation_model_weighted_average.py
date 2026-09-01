"""Weighted Average FMU aggregate input mappings -- the second Aggregate
operation alongside "max" (see tests/test_simulation_model_aggregate_inputs.py
and tests/test_simulation_model_aggregate_persistence.py for that one).

    weighted_average = sum(value[i] * weight[i]) / sum(weight[i])

Primary use case: RTU return-air temperature = weighted average of zone
temperatures, weighted by each zone's VAV airflow.

Same two-layer split as the existing aggregate test suite:
  - Provider-level: construct FMUSimulationProvider + FMUAggregateInput
    directly, fake only the FMURuntimeClient HTTP methods, no DB.
  - DB/API-level: real device/object creation via the `client` fixture,
    following test_simulation_model_aggregate_persistence.py's exact
    conventions (_patch_definition, _MinimalSimEngine, enabled=False
    drafts throughout).

Unlike "max" -- which fails the WHOLE aggregate the instant any one member
is missing/non-numeric (see FMUAggregateStepError's docstring) --
weighted_average tolerates a bad INDIVIDUAL pair (missing/non-numeric
value, missing/non-numeric/negative weight): that pair is simply excluded
from the sum, the same way a real BAS drops a faulted sensor from a trend
rather than refusing to compute anything.

When every valid weight is itself 0 (e.g. RTU return_air_temp_c weighted
by VAV airflow, and every VAV reads 0 airflow because the upstream RTU
hasn't established duct pressure yet -- a real startup deadlock: RTU can't
initialize without a weight, but can't produce duct pressure without
initializing), it falls back to a plain arithmetic mean of whichever
values ARE valid rather than failing outright. It only fails (the same
FMUInputResolutionError/FMUAggregateStepError behavior "max" uses) when
there is no valid VALUE anywhere in the pair set -- i.e. nothing left to
even average.
"""
from __future__ import annotations

import math

import pytest

from src.api.routers import simulation as simulation_router
from src.simulation.models import runtime as model_runtime
from src.simulation.models.store import get_simulation_model
from src.simulation.models.registry import ModelDefinition, VariableDefinition
from src.simulation.providers import FMUAggregateInput, FMUPointBinding, SimulationContext
from src.simulation.providers.fmu import (
    FMUAggregateStepError,
    FMUInputResolutionError,
    FMURuntimeResponse,
    FMUSimulationProvider,
)
from src.simulation.providers.base import ProviderStatus


# ═══════════════════════════════════════════════════════════════════════════
# Provider-level tests
# ═══════════════════════════════════════════════════════════════════════════

def _fake_health(self):
    return {"status": "ok"}


def _member_binding_metadata(point_id: int, name: str, variable: str) -> dict:
    return {
        "point_id": point_id,
        "variable": variable,
        "direction": "input",
        "point_name": name,
        "device_name": f"Zone-{point_id}",
        "device_id": point_id,
        "object_type": "analog-input",
        "object_instance": 1,
        "units": "degC",
    }


def _weighted_average_context(
    initial_point_inputs: dict,
    *,
    value_ids=(101, 102, 103),
    weight_ids=(201, 202, 203),
    variable="t_return_air_c",
) -> SimulationContext:
    bindings = [
        _member_binding_metadata(pid, f"Zone{i + 1}-Temp", variable)
        for i, pid in enumerate(value_ids)
    ] + [
        _member_binding_metadata(wid, f"VAV{i + 1}-Airflow", variable)
        for i, wid in enumerate(weight_ids)
    ]
    return SimulationContext(
        participant_device_ids=[],
        point_configs=[],
        metadata={
            "provider_id": "fmu:SimpleAHU:8",
            "simulation_model_id": 8,
            "model": "SimpleAHU",
            "initial_point_inputs": initial_point_inputs,
            "bindings": bindings,
        },
    )


def _provider(*, bindings=None, aggregate_inputs=None) -> FMUSimulationProvider:
    return FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="SimpleAHU",
        bindings=bindings or [],
        aggregate_inputs=aggregate_inputs or [],
        input_defaults={},
        input_variables={"t_return_air_c", "supply_air_temp_c"},
        output_variables={"zone_temp_c"},
    )


def _capturing_initialize(captured: dict):
    def _fake_initialize(self, model_id, inputs=None):
        captured["inputs"] = dict(inputs or {})
        return {"session_id": "s1", "state": "RUNNING"}
    return _fake_initialize


# ─── 1. Correct weighted-average calculation ────────────────────────────────

def test_weighted_average_calculation_different_weights(monkeypatch):
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    # Zone temps 20/22/24 C, weights (airflow) 1.0/2.0/1.0 m3/s.
    context = _weighted_average_context({101: 20.0, 102: 22.0, 103: 24.0, 201: 1.0, 202: 2.0, 203: 1.0})
    provider.initialize(context)

    expected = (20.0 * 1.0 + 22.0 * 2.0 + 24.0 * 1.0) / (1.0 + 2.0 + 1.0)
    assert captured["inputs"]["t_return_air_c"] == pytest.approx(expected)
    assert expected == pytest.approx(22.0)  # sanity-check the hand-computed expectation itself


# ─── 2. Equal weights reduces to a plain arithmetic mean ────────────────────

def test_weighted_average_equal_weights_is_plain_mean(monkeypatch):
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    context = _weighted_average_context({101: 18.0, 102: 21.0, 103: 27.0, 201: 5.0, 202: 5.0, 203: 5.0})
    provider.initialize(context)

    assert captured["inputs"]["t_return_air_c"] == pytest.approx((18.0 + 21.0 + 27.0) / 3.0)


# ─── 3. Zero weight is valid: included, contributes nothing ────────────────

def test_weighted_average_zero_weight_pair_contributes_nothing(monkeypatch):
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    # Zone 3's VAV is off (airflow=0) -- a wildly different temp there
    # (99C) must not skew the result at all.
    context = _weighted_average_context({101: 20.0, 102: 24.0, 103: 99.0, 201: 1.0, 202: 1.0, 203: 0.0})
    provider.initialize(context)

    assert captured["inputs"]["t_return_air_c"] == pytest.approx((20.0 + 24.0) / 2.0)


# ─── 4. All weights zero -> plain-average startup fallback, not NaN/Inf ────
# Regression coverage for the real RTU/VAV startup deadlock this fallback
# fixes: RTU's return_air_temp_c weighted average needs VAV airflow as its
# weight, but VAV airflow depends on RTU's own duct-pressure output, which
# RTU can't produce until it initializes. Before this fallback, all-zero
# weights (VAVs reporting 0 airflow because RTU hasn't started) hard-failed
# initialize()/step() forever -- a genuine deadlock, not a data-quality
# problem. See _resolve_weighted_average's docstring in providers/fmu.py.

def test_weighted_average_all_zero_weights_falls_back_to_plain_average_on_initialize(monkeypatch):
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    context = _weighted_average_context({101: 20.0, 102: 22.0, 103: 24.0, 201: 0.0, 202: 0.0, 203: 0.0})
    provider.initialize(context)

    assert captured["inputs"]["t_return_air_c"] == pytest.approx((20.0 + 22.0 + 24.0) / 3.0)
    assert not math.isnan(captured["inputs"]["t_return_air_c"])


def test_weighted_average_all_zero_weights_falls_back_to_plain_average_on_step(monkeypatch):
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    output_binding = FMUPointBinding(point_id=999, variable="zone_temp_c", direction="output")
    provider = _provider(aggregate_inputs=[agg], bindings=[output_binding])
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})

    captured_payloads: list[dict] = []

    def _fake_step(self, model_id, payload):
        captured_payloads.append(dict(payload["inputs"]))
        return FMURuntimeResponse(status_code=200, raw_body="{}", body={"state": "RUNNING", "current_time": 5.0, "zone_temp_c": 22.0})

    monkeypatch.setattr(type(provider._client), "step", _fake_step)

    context = _weighted_average_context({101: 20.0, 102: 22.0, 103: 24.0, 201: 1.0, 202: 1.0, 203: 1.0})
    provider.initialize(context)
    provider.start()

    # All VAVs go to 0 airflow mid-run (e.g. RTU's own duct-pressure output
    # dropped out) -- must keep running on the plain-average fallback, not
    # error out.
    provider.set_inputs({201: 0.0, 202: 0.0, 203: 0.0})
    provider.step(5.0)

    assert provider.get_status() == ProviderStatus.RUNNING
    assert captured_payloads[-1]["t_return_air_c"] == pytest.approx((20.0 + 22.0 + 24.0) / 3.0)

    # Airflow returns -> transitions straight back to the real weighted
    # average on the very next step, no separate state to reset.
    provider.set_inputs({201: 2.0, 202: 1.0, 203: 1.0})
    provider.step(5.0)
    expected = (20.0 * 2.0 + 22.0 * 1.0 + 24.0 * 1.0) / (2.0 + 1.0 + 1.0)
    assert captured_payloads[-1]["t_return_air_c"] == pytest.approx(expected)


# ─── 5. Invalid/missing individual pairs are ignored, not fatal ────────────

def test_weighted_average_ignores_missing_value_pair(monkeypatch):
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    # Point 102 (zone 2 temperature) has no live value at all.
    context = _weighted_average_context({101: 20.0, 103: 24.0, 201: 1.0, 202: 1.0, 203: 1.0})
    provider.initialize(context)

    assert captured["inputs"]["t_return_air_c"] == pytest.approx((20.0 + 24.0) / 2.0)


def test_weighted_average_ignores_non_numeric_value_pair(monkeypatch):
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    context = _weighted_average_context({101: 20.0, 102: "fault", 103: 24.0, 201: 1.0, 202: 1.0, 203: 1.0})
    provider.initialize(context)

    assert captured["inputs"]["t_return_air_c"] == pytest.approx((20.0 + 24.0) / 2.0)


def test_weighted_average_ignores_missing_weight_pair(monkeypatch):
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    # Point 202 (zone 2's VAV airflow) has no live value at all.
    context = _weighted_average_context({101: 20.0, 102: 30.0, 103: 24.0, 201: 1.0, 203: 1.0})
    provider.initialize(context)

    assert captured["inputs"]["t_return_air_c"] == pytest.approx((20.0 + 24.0) / 2.0)


def test_weighted_average_ignores_negative_weight_pair(monkeypatch):
    """Do not allow negative weights: a negative weight excludes its pair,
    the same as a missing value/weight -- it is never clamped to 0 and
    never allowed to participate (which would subtract from the sum)."""
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    context = _weighted_average_context({101: 20.0, 102: 999.0, 103: 24.0, 201: 1.0, 202: -5.0, 203: 1.0})
    provider.initialize(context)

    assert captured["inputs"]["t_return_air_c"] == pytest.approx((20.0 + 24.0) / 2.0)


def test_weighted_average_all_pairs_invalid_aborts(monkeypatch):
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    monkeypatch.setattr(provider._client, "health", lambda: (_ for _ in ()).throw(AssertionError("must not contact runtime")))

    # No live values for anything.
    context = _weighted_average_context({})

    with pytest.raises(FMUInputResolutionError) as excinfo:
        provider.initialize(context)
    assert "total valid weight is 0" in str(excinfo.value)


# ─── 6. Dynamic re-resolution across steps ──────────────────────────────────

def test_weighted_average_step_recomputes_when_values_change(monkeypatch):
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    output_binding = FMUPointBinding(point_id=999, variable="zone_temp_c", direction="output")
    provider = _provider(aggregate_inputs=[agg], bindings=[output_binding])
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", lambda self, model_id, inputs=None: {"session_id": "s1", "state": "RUNNING"})

    captured_payloads: list[dict] = []

    def _fake_step(self, model_id, payload):
        captured_payloads.append(dict(payload["inputs"]))
        return FMURuntimeResponse(status_code=200, raw_body="{}", body={"state": "RUNNING", "current_time": 5.0, "zone_temp_c": 22.0})

    monkeypatch.setattr(type(provider._client), "step", _fake_step)

    context = _weighted_average_context({101: 20.0, 102: 22.0, 103: 24.0, 201: 1.0, 202: 1.0, 203: 1.0})
    provider.initialize(context)
    provider.start()

    provider.step(5.0)
    assert captured_payloads[-1]["t_return_air_c"] == pytest.approx(22.0)

    # Zone 1 heats up and its VAV opens further.
    provider.set_inputs({101: 30.0, 201: 3.0})
    provider.step(5.0)
    expected = (30.0 * 3.0 + 22.0 * 1.0 + 24.0 * 1.0) / (3.0 + 1.0 + 1.0)
    assert captured_payloads[-1]["t_return_air_c"] == pytest.approx(expected)


# ─── 7. validate() structural checks ────────────────────────────────────────

def test_validate_rejects_weighted_average_with_length_mismatch():
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202),
    )
    provider = _provider(aggregate_inputs=[agg])
    result = provider.validate()
    assert not result.valid
    assert any("exactly one weight point per value point" in e for e in result.errors)


def test_validate_rejects_weighted_average_with_missing_weight_for_one_point():
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, None, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    result = provider.validate()
    assert not result.valid
    assert any("missing a weight point" in e and "102" in e for e in result.errors)


def test_validate_accepts_well_formed_weighted_average():
    agg = FMUAggregateInput(
        variable="t_return_air_c", operation="weighted_average",
        point_ids=(101, 102, 103), weight_point_ids=(201, 202, 203),
    )
    provider = _provider(aggregate_inputs=[agg])
    result = provider.validate()
    assert result.valid, result.errors


# ─── 8. Existing Maximum aggregate behavior is unaffected (regression) ─────

def test_max_aggregate_calculation_unaffected_by_weighted_average_support(monkeypatch):
    agg = FMUAggregateInput(variable="u_vav_dam_max", operation="max", point_ids=(101, 102, 103))
    provider = FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="SimpleAHU",
        bindings=[],
        aggregate_inputs=[agg],
        input_defaults={},
        input_variables={"u_vav_dam_max"},
        output_variables=set(),
    )
    captured: dict = {}
    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _capturing_initialize(captured))

    context = SimulationContext(
        participant_device_ids=[], point_configs=[],
        metadata={
            "initial_point_inputs": {101: 62.0, 102: 81.0, 103: 74.0},
            "bindings": [
                {"point_id": pid, "variable": "u_vav_dam_max", "direction": "input"}
                for pid in (101, 102, 103)
            ],
        },
    )
    provider.initialize(context)
    assert captured["inputs"]["u_vav_dam_max"] == 81.0
    assert agg.weight_point_ids == ()


# ═══════════════════════════════════════════════════════════════════════════
# DB/API-level tests (persistence, validation, wiring)
# ═══════════════════════════════════════════════════════════════════════════

WA_VARIABLE = "return_air_temp_c"


class _MinimalSimEngine:
    """Copied from test_simulation_model_aggregate_persistence.py -- see its
    own docstring for why this fuller-but-still-minimal fake is needed for
    every test in this file (all go through the live HTTP endpoints)."""
    async def reload(self) -> None:
        pass

    async def add_object_hot(self, device_instance: int, obj: dict) -> None:
        pass

    def get_simulation_providers(self):
        return {}

    def unregister_simulation_provider(self, runtime_id: str) -> bool:
        return False


@pytest.fixture
def client(client):
    client.app.state.engine = _MinimalSimEngine()
    return client


def _fake_ahu_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="SimpleAHU",
        label="Simple AHU",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(
            VariableDefinition(WA_VARIABLE, "Return Air Temperature", "input"),
        ),
        factory=lambda parameters: None,
        runtime_model="SimpleAHU",
    )


def _patch_definition(monkeypatch) -> ModelDefinition:
    definition = _fake_ahu_definition()
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _model_type: definition)
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _settings, _model_type: definition)
    return definition


def _make_device_with_value_and_weight_points(client, *, count=3, instance=4001):
    """Creates one device with `count` "zone temperature" (value) points and
    `count` "VAV airflow" (weight) points -- the primary HVAC use case from
    the task spec (RTU return-air temp = weighted avg of zone temps by VAV
    airflow)."""
    device = client.post("/devices", json={"device_instance": instance, "name": "RTU-Test"}).json()
    value_points = [
        client.post(f"/devices/{device['id']}/objects", json={
            "object_type": "analog-input", "object_instance": i + 1,
            "name": f"Zone{i + 1}-Temp", "units": "degC",
        }).json()
        for i in range(count)
    ]
    weight_points = [
        client.post(f"/devices/{device['id']}/objects", json={
            "object_type": "analog-input", "object_instance": 100 + i + 1,
            "name": f"VAV{i + 1}-Airflow", "units": "cfm",
        }).json()
        for i in range(count)
    ]
    return device, value_points, weight_points


def _weighted_average_payload(
    device_id: int, point_ids: list[int], weight_point_ids: list[int] | None, *, enabled: bool = False,
) -> dict:
    aggregate: dict = {
        "variable": WA_VARIABLE, "direction": "input",
        "operation": "weighted_average", "point_ids": point_ids,
    }
    if weight_point_ids is not None:
        aggregate["weight_point_ids"] = weight_point_ids
    return {
        "name": "RTU-Test Simple AHU",
        "provider_type": "fmu",
        "model_type": "SimpleAHU",
        "enabled": enabled,
        "created_from_device_id": device_id,
        "parameters": {"input_sources": {WA_VARIABLE: "aggregate"}},
        "mappings": [],
        "aggregate_mappings": [aggregate],
    }


def _max_payload(device_id: int, point_ids: list[int], *, enabled: bool = False) -> dict:
    return {
        "name": "RTU-Test Simple AHU",
        "provider_type": "fmu",
        "model_type": "SimpleAHU",
        "enabled": enabled,
        "created_from_device_id": device_id,
        "parameters": {"input_sources": {WA_VARIABLE: "aggregate"}},
        "mappings": [],
        "aggregate_mappings": [
            {"variable": WA_VARIABLE, "direction": "input", "operation": "max", "point_ids": point_ids},
        ],
    }


def _aggregate_db_rows(database, model_id: int) -> tuple[list, list]:
    with database._conn() as conn:
        headers = conn.execute(
            "SELECT * FROM simulation_model_aggregate_mappings WHERE model_config_id=?",
            (model_id,),
        ).fetchall()
        members = []
        for h in headers:
            members.extend(conn.execute(
                "SELECT * FROM simulation_model_aggregate_members WHERE aggregate_mapping_id=?",
                (h["id"],),
            ).fetchall())
        return headers, members


# ─── 9-10. Create + reload (serialization/deserialization) ─────────────────

def test_create_weighted_average_mapping_with_three_pairs(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]

    resp = client.post(
        "/simulation/models",
        json=_weighted_average_payload(device["id"], point_ids, weight_point_ids),
    )
    assert resp.status_code == 201
    agg = next(m for m in resp.json()["mappings"] if m["variable"] == WA_VARIABLE)
    assert agg["operation"] == "weighted_average"
    assert agg["point_ids"] == point_ids
    assert agg["weight_point_ids"] == weight_point_ids


def test_reload_preserves_weighted_average_operation_and_weight_point_ids(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]
    created = client.post(
        "/simulation/models",
        json=_weighted_average_payload(device["id"], point_ids, weight_point_ids),
    ).json()

    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    agg = next(m for m in reloaded["mappings"] if m["variable"] == WA_VARIABLE)
    assert agg["operation"] == "weighted_average"
    assert agg["point_ids"] == point_ids
    assert agg["weight_point_ids"] == weight_point_ids


# ─── 11. Update membership ───────────────────────────────────────────────

def test_update_weighted_average_replaces_pairs(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client, count=4)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]
    created = client.post(
        "/simulation/models",
        json=_weighted_average_payload(device["id"], point_ids[:3], weight_point_ids[:3]),
    ).json()

    resp = client.put(
        f"/simulation/models/{created['id']}",
        json=_weighted_average_payload(device["id"], point_ids[1:4], weight_point_ids[1:4]),
    )
    assert resp.status_code == 200
    agg = next(m for m in resp.json()["mappings"] if m["variable"] == WA_VARIABLE)
    assert agg["point_ids"] == point_ids[1:4]
    assert agg["weight_point_ids"] == weight_point_ids[1:4]


# ─── 12-16. Validation ────────────────────────────────────────────────────

def test_create_weighted_average_rejects_omitted_weight_point_ids(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, _weight_points = _make_device_with_value_and_weight_points(client, count=2)
    point_ids = [p["id"] for p in value_points]
    resp = client.post("/simulation/models", json=_weighted_average_payload(device["id"], point_ids, None))
    assert resp.status_code == 422


def test_create_weighted_average_rejects_length_mismatch(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client, count=3)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points][:2]  # one short
    resp = client.post("/simulation/models", json=_weighted_average_payload(device["id"], point_ids, weight_point_ids))
    assert resp.status_code == 422


def test_create_rejects_weight_point_ids_on_max_operation(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client, count=2)
    payload = _max_payload(device["id"], [p["id"] for p in value_points])
    payload["aggregate_mappings"][0]["weight_point_ids"] = [p["id"] for p in weight_points]
    resp = client.post("/simulation/models", json=payload)
    assert resp.status_code == 422


def test_create_weighted_average_rejects_nonexistent_weight_point(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client, count=2)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [weight_points[0]["id"], 999999]
    resp = client.post("/simulation/models", json=_weighted_average_payload(device["id"], point_ids, weight_point_ids))
    assert resp.status_code == 422
    assert "999999" in str(resp.json()["detail"])


def test_create_weighted_average_rejects_non_numeric_weight_point(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client, count=2)
    binary_point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "binary-input", "object_instance": 200, "name": "VAV-Fan-Status",
    }).json()
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [weight_points[0]["id"], binary_point["id"]]
    resp = client.post("/simulation/models", json=_weighted_average_payload(device["id"], point_ids, weight_point_ids))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    message = detail["message"] if isinstance(detail, dict) else detail
    assert "numeric" in message.lower()
    assert "weight point" in message.lower()


def test_create_weighted_average_rejects_missing_point(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client, count=2)
    point_ids = [value_points[0]["id"], 888888]
    weight_point_ids = [p["id"] for p in weight_points]
    resp = client.post("/simulation/models", json=_weighted_average_payload(device["id"], point_ids, weight_point_ids))
    assert resp.status_code == 422
    assert "888888" in str(resp.json()["detail"])


# ─── 17. Deleting a weight point is rejected (same protection as value points) ─

def test_delete_weighted_average_weight_point_is_rejected(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]
    created = client.post(
        "/simulation/models",
        json=_weighted_average_payload(device["id"], point_ids, weight_point_ids),
    ).json()

    resp = client.delete(f"/devices/{device['id']}/objects/{weight_point_ids[0]}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert WA_VARIABLE in detail["message"]

    _headers, members = _aggregate_db_rows(database, created["id"])
    assert sorted(m["weight_point_id"] for m in members) == sorted(weight_point_ids)


def test_delete_weighted_average_value_point_is_still_rejected(client, database, monkeypatch):
    """Regression: the existing value-point delete protection (already
    tested for "max") must still work for a weighted_average row too."""
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]
    client.post("/simulation/models", json=_weighted_average_payload(device["id"], point_ids, weight_point_ids))

    resp = client.delete(f"/devices/{device['id']}/objects/{point_ids[0]}")
    assert resp.status_code == 409


# ─── 18. Runtime wiring: both value AND weight points registered as inputs ──

class _FakeFMUProvider:
    created: list["_FakeFMUProvider"] = []

    def __init__(self, *, runtime_url, model, bindings, aggregate_inputs=None, input_exposures=None, input_defaults, timeout_s, input_variables, output_variables) -> None:
        self.bindings = list(bindings)
        self.aggregate_inputs = list(aggregate_inputs or [])
        self.input_exposures = list(input_exposures or [])
        self.created.append(self)


class _FakeEngine:
    def resolve_provider_input_value(self, point_id: int):
        return None

    def register_simulation_provider(self, *args, **kwargs) -> None:
        pass


def test_reconstructed_runtime_config_wires_weighted_average(client, database, monkeypatch):
    definition = _patch_definition(monkeypatch)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    _FakeFMUProvider.created.clear()

    device, value_points, weight_points = _make_device_with_value_and_weight_points(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]
    created = client.post(
        "/simulation/models",
        json=_weighted_average_payload(device["id"], point_ids, weight_point_ids),
    ).json()

    config = {**get_simulation_model(database, created["id"]), "_settings": {}}
    provider, context, inputs, outputs = model_runtime._build_fmu_provider(config, _FakeEngine())

    assert len(_FakeFMUProvider.created) == 1
    built = _FakeFMUProvider.created[-1]
    assert built.bindings == []
    assert len(built.aggregate_inputs) == 1
    assert built.aggregate_inputs[0].operation == "weighted_average"
    assert built.aggregate_inputs[0].point_ids == tuple(point_ids)
    assert built.aggregate_inputs[0].weight_point_ids == tuple(weight_point_ids)

    # Both value AND weight points must be registered as inputs, or the
    # engine will never feed live weight values into the provider.
    assert inputs == set(point_ids) | set(weight_point_ids)
    assert context.metadata["aggregate_inputs"] == [
        {
            "variable": WA_VARIABLE, "source": "aggregate", "operation": "weighted_average",
            "point_ids": point_ids, "weight_point_ids": weight_point_ids,
        }
    ]
    member_bindings = [b for b in context.metadata["bindings"] if b["variable"] == WA_VARIABLE]
    assert {b["point_id"] for b in member_bindings} == set(point_ids) | set(weight_point_ids)
    assert definition.runtime_model == "SimpleAHU"


def test_weighted_average_participates_in_real_computation_after_persistence(client, database, monkeypatch):
    """Closes the gap all the way: persisted config -> real
    FMUSimulationProvider (not the fake above) -> actual weighted-average
    math, using freshly-set live values."""
    _patch_definition(monkeypatch)
    device, value_points, weight_points = _make_device_with_value_and_weight_points(client)
    point_ids = [p["id"] for p in value_points]
    weight_point_ids = [p["id"] for p in weight_points]
    created = client.post(
        "/simulation/models",
        json=_weighted_average_payload(device["id"], point_ids, weight_point_ids),
    ).json()

    config = {**get_simulation_model(database, created["id"]), "_settings": {}}
    provider, context, inputs, outputs = model_runtime._build_fmu_provider(config, _FakeEngine())

    assert inputs == set(point_ids) | set(weight_point_ids)
    agg_input = provider._aggregate_inputs[0]

    values = {point_ids[0]: 19.0, point_ids[1]: 23.0, point_ids[2]: 29.0}
    weights = {weight_point_ids[0]: 2.0, weight_point_ids[1]: 1.0, weight_point_ids[2]: 1.0}
    provider.set_inputs({**values, **weights})

    result, detail, _diag = provider._resolve_one_aggregate(agg_input)
    assert detail is None
    expected = (19.0 * 2.0 + 23.0 * 1.0 + 29.0 * 1.0) / (2.0 + 1.0 + 1.0)
    assert result == pytest.approx(expected)


# ─── 19-20. Existing Maximum/etc. aggregates remain unchanged (regression) ──

def test_existing_max_aggregate_create_and_reload_unaffected(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, value_points, _weight_points = _make_device_with_value_and_weight_points(client, count=3)
    point_ids = [p["id"] for p in value_points]

    created = client.post("/simulation/models", json=_max_payload(device["id"], point_ids)).json()
    agg = next(m for m in created["mappings"] if m["variable"] == WA_VARIABLE)
    assert agg["operation"] == "max"
    assert agg["point_ids"] == point_ids
    # No weight pairing leaks into a plain "max" aggregate.
    assert not any(agg.get("weight_point_ids") or [])

    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    agg_reloaded = next(m for m in reloaded["mappings"] if m["variable"] == WA_VARIABLE)
    assert agg_reloaded["operation"] == "max"
    assert agg_reloaded["point_ids"] == point_ids


def test_existing_max_aggregate_rejects_unsupported_operation_still_works(client, database, monkeypatch):
    """Regression: the pre-existing "unsupported operation" rejection (now
    guarding a three-member set {max, min, weighted_average} instead of a
    two-member set {max, weighted_average}) still rejects anything outside
    it. "min" is itself a real, supported operation as of the Minimum
    aggregate task, so it can no longer stand in for "unsupported" here --
    "avg" is not implemented, so it still exercises this rejection path."""
    _patch_definition(monkeypatch)
    device, value_points, _weight_points = _make_device_with_value_and_weight_points(client, count=1)
    payload = _max_payload(device["id"], [value_points[0]["id"]])
    payload["aggregate_mappings"][0]["operation"] = "avg"
    resp = client.post("/simulation/models", json=payload)
    assert resp.status_code == 422
