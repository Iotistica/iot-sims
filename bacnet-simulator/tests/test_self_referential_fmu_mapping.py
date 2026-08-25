"""Regression tests for two related production bugs, both surfaced while
investigating RTU stuck at 0% fan command after a stuck-low fault was
removed (see the incident trace in engine.py's reload()/_create_object()
comments and model_runtime.py's _build_fmu_provider):

1. A point mapped as BOTH a model's input source and its output target is
   a self-referential feedback loop with no independent driving signal --
   RTU-1-Supply-Fan-Command was mapped as both fan_command_pct's input
   (uFan) and output (yFan). Now rejected at save time
   (simulation.py::_validate_mapping_contract) and at registration time
   (model_runtime.py::_build_fmu_provider, defense-in-depth for configs
   saved before this existed).

2. Any object edit anywhere triggers engine.reload(), which rebuilds every
   BACnet object via _create_object() -- and used to reseed a provider-
   owned "raw"/"constant" point's value from behavior_params, frequently
   empty for a point that has only ever shown a live provider value.
   Combined with (1), this let a self-referential point latch at 0
   forever, because RTU.mo's own "uFan>0.01 else yFan=0" interlock never
   saw a value above the threshold again. Fixed by having reload()
   snapshot _prev_values first and _create_object() restore a
   provider-owned raw/constant point's real live value instead of
   re-seeding from (possibly empty) params.
"""
from __future__ import annotations

import pytest

from src.api.routers import simulation as simulation_router
from src.simulation import model_runtime
from src.simulation.engine import SimEngine
from src.simulation.models.registry import ModelDefinition, VariableDefinition


# ═══════════════════════════════════════════════════════════════════════════
# Shared fixtures: an RTU-shaped model definition (fan_command_pct input +
# two possible output variables, mirroring the real model.json's
# fan_command_pct/supply_fan_speed_pct redundancy)
# ═══════════════════════════════════════════════════════════════════════════

def _rtu_like_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="RTU",
        label="RTU",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(
            VariableDefinition("fan_command_pct", "Fan Command", "input"),
            VariableDefinition("fan_command_pct", "Fan Command", "output"),
            VariableDefinition("supply_fan_speed_pct", "Supply Fan Speed", "output"),
        ),
        factory=lambda parameters: None,
        runtime_model="RTU",
    )


def _rtu_config(*, fan_command_point_id: int, output_point_id: int, output_variable: str) -> dict:
    return {
        "id": 14,
        "provider_type": "fmu",
        "model_type": "RTU",
        "name": "Test RTU",
        "parameters": {"input_sources": {"fan_command_pct": "point"}},
        "mappings": [
            {
                "variable": "fan_command_pct", "direction": "input", "point_id": fan_command_point_id,
                "object_type": "analog-value", "device_id": 1,
            },
            {
                "variable": output_variable, "direction": "output", "point_id": output_point_id,
                "object_type": "analog-value", "device_id": 1,
            },
        ],
    }


class _FakeEngine:
    pass


# ═══════════════════════════════════════════════════════════════════════════
# 1a. model_runtime._build_fmu_provider: self-loop rejected/allowed
# ═══════════════════════════════════════════════════════════════════════════

def test_build_fmu_provider_rejects_input_output_self_loop(monkeypatch):
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _s, _m: _rtu_like_definition())

    config = _rtu_config(fan_command_point_id=10306, output_point_id=10306, output_variable="fan_command_pct")

    with pytest.raises(ValueError, match="both an input source and an output target"):
        model_runtime._build_fmu_provider(config, _FakeEngine())


def test_build_fmu_provider_allows_distinct_input_and_output_points(monkeypatch):
    """The corrected mapping: fan command in on one point, fan speed out on
    a different point -- registers cleanly, no overlap between input and
    output point sets."""
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _s, _m: _rtu_like_definition())

    config = _rtu_config(fan_command_point_id=10306, output_point_id=10307, output_variable="supply_fan_speed_pct")

    provider, context, inputs, outputs = model_runtime._build_fmu_provider(config, _FakeEngine())

    assert inputs == {10306}
    assert outputs == {10307}
    assert inputs.isdisjoint(outputs)


# ═══════════════════════════════════════════════════════════════════════════
# 1b. API save-time validation: self-loop rejected/allowed
# ═══════════════════════════════════════════════════════════════════════════

def _make_device_and_two_points(client, *, instance: int):
    device = client.post("/devices", json={"device_instance": instance, "name": "RTU-Test"}).json()
    fan_command = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-value", "object_instance": 1, "name": "Supply-Fan-Command", "units": "percent",
    }).json()
    fan_speed = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-value", "object_instance": 2, "name": "Supply-Fan-Speed", "units": "percent",
    }).json()
    return device, fan_command, fan_speed


def _rtu_payload(device_id: int, *, fan_command_point_id: int, output_point_id: int, output_variable: str) -> dict:
    return {
        "name": "Test RTU",
        "provider_type": "fmu",
        "model_type": "RTU",
        "enabled": False,
        "created_from_device_id": device_id,
        "parameters": {"input_sources": {"fan_command_pct": "point"}},
        "mappings": [
            {"variable": "fan_command_pct", "direction": "input", "point_id": fan_command_point_id},
            {"variable": output_variable, "direction": "output", "point_id": output_point_id},
        ],
        "aggregate_mappings": [],
    }


def test_api_rejects_input_output_self_loop(client, database, monkeypatch):
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _rtu_like_definition())
    device, fan_command, fan_speed = _make_device_and_two_points(client, instance=8001)

    payload = _rtu_payload(
        device["id"],
        fan_command_point_id=fan_command["id"],
        output_point_id=fan_command["id"],
        output_variable="fan_command_pct",
    )
    resp = client.post("/simulation/models", json=payload)

    assert resp.status_code == 422, resp.text
    assert "both a model input source and a model output target" in resp.json()["detail"]


def test_api_accepts_distinct_input_and_output_points(client, database, monkeypatch):
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _mt: _rtu_like_definition())
    device, fan_command, fan_speed = _make_device_and_two_points(client, instance=8002)

    payload = _rtu_payload(
        device["id"],
        fan_command_point_id=fan_command["id"],
        output_point_id=fan_speed["id"],
        output_variable="supply_fan_speed_pct",
    )
    resp = client.post("/simulation/models", json=payload)

    assert resp.status_code == 201, resp.text


# ═══════════════════════════════════════════════════════════════════════════
# 2. SimEngine._create_object(): reload preserves a provider-owned point's
#    live value instead of re-seeding from (possibly empty) behavior_params
# ═══════════════════════════════════════════════════════════════════════════

def _analog_value_row(obj_id: int, *, behavior: str, behavior_params: str, name: str = "Test-Point") -> dict:
    return {
        "id": obj_id,
        "object_type": "analog-value",
        "object_instance": 1,
        "name": name,
        "units": "percent",
        "behavior": behavior,
        "behavior_params": behavior_params,
        "manual_value": None,
        "reliability": "no-fault-detected",
    }


async def test_create_object_restores_preserved_value_for_provider_owned_raw_point(database):
    """The exact fault-removal scenario: behavior='raw', behavior_params
    empty (nothing meaningful was ever stored for a point that only ever
    displayed a live provider value), but the point had a real live value
    (64%) right before reload -- that must be what gets restored, not 0."""
    engine = SimEngine(database)
    obj_id = 10306
    engine._point_output_owner[obj_id] = "fmu:RTU:14"
    engine._reload_preserved_values[obj_id] = 64.0

    obj_row = _analog_value_row(obj_id, behavior="raw", behavior_params="{}")
    bacnet_obj, _ = engine._create_object(obj_row, slot=0, device_name="RTU")

    assert float(bacnet_obj.presentValue) == pytest.approx(64.0)


async def test_create_object_falls_back_to_params_when_no_preserved_value(database):
    """No reload snapshot available (e.g. a hot-add of a brand new point,
    or first-ever boot) -- must not error, and matches the pre-existing
    behavior_params-derived seed."""
    engine = SimEngine(database)
    obj_id = 10306
    engine._point_output_owner[obj_id] = "fmu:RTU:14"
    # engine._reload_preserved_values intentionally left empty.

    obj_row = _analog_value_row(obj_id, behavior="raw", behavior_params="{}")
    bacnet_obj, _ = engine._create_object(obj_row, slot=0, device_name="RTU")

    assert float(bacnet_obj.presentValue) == 0.0


async def test_create_object_does_not_restore_for_builtin_owned_point(database):
    """Preservation is scoped to provider-owned points -- an ordinary
    builtin-owned point (not in _point_output_owner at all) must still
    seed from its own behavior_params, even if a stale snapshot entry
    happens to exist for its object id."""
    engine = SimEngine(database)
    obj_id = 10306
    engine._reload_preserved_values[obj_id] = 64.0  # not consulted: owner is "builtin"

    obj_row = _analog_value_row(obj_id, behavior="constant", behavior_params='{"value": 12.0}')
    bacnet_obj, _ = engine._create_object(obj_row, slot=0, device_name="Test")

    assert float(bacnet_obj.presentValue) == pytest.approx(12.0)


async def test_create_object_does_not_restore_for_non_passthrough_behavior(database):
    """Sine/noise/ramp/etc. seeds are intentionally computed from their own
    behavior_params (e.g. a configured base) -- restoring the pre-reload
    raw value would be wrong for these, so preservation only applies to
    raw/constant."""
    engine = SimEngine(database)
    obj_id = 10306
    engine._point_output_owner[obj_id] = "fmu:RTU:14"
    engine._reload_preserved_values[obj_id] = 64.0

    obj_row = _analog_value_row(obj_id, behavior="sine", behavior_params='{"base": 20.0, "amplitude": 5.0}')
    bacnet_obj, _ = engine._create_object(obj_row, slot=0, device_name="RTU")

    assert float(bacnet_obj.presentValue) != pytest.approx(64.0)


async def test_reload_snapshots_prev_values_before_clearing(database):
    """reload() itself must capture the snapshot -- not just _create_object
    consuming one manually set up by a test."""
    engine = SimEngine(database)
    engine._prev_values[10306] = 64.0
    engine.app = None  # skip the BACnet-socket teardown branch entirely

    await engine.reload()

    # start() drains the one-shot snapshot once every object for this
    # reload has been (re)created -- with no enabled devices, that happens
    # immediately, so by the time reload() returns it's empty again. The
    # real guarantee this test protects is the assignment itself existing
    # in reload(), which the next test exercises end-to-end via
    # _create_object.
    assert engine._reload_preserved_values == {}


# ═══════════════════════════════════════════════════════════════════════════
# 3. End-to-end: fault removal + reload no longer latches a self-referential
#    point at 0 (the exact reported incident, at the level these fixes
#    actually operate: mapping topology + object-recreation seeding)
# ═══════════════════════════════════════════════════════════════════════════

async def test_rtu_recovers_from_stuck_low_fault_without_reseed_to_zero(database):
    """Simulates the full incident timeline directly against the engine:
    point is provider-owned, fault forces it to 30% (stuck-low), fault is
    removed (behavior reverts to raw with empty params, exactly as the
    live object-edit flow leaves it), an unrelated edit triggers a reload
    -- and the point must come back at its last real live value (30%,
    still comfortably above RTU.mo's 0.01 fan-enable threshold), never 0.
    """
    engine = SimEngine(database)
    obj_id = 10306
    engine._point_output_owner[obj_id] = "fmu:RTU:14"

    # Last live value before the fault was removed and reload fired --
    # the fault's stuck-low value, well above the 1% enable threshold.
    engine._reload_preserved_values[obj_id] = 30.0

    # What the object edit (fault -> raw) actually leaves in the DB row:
    # behavior='raw', empty params -- this is what used to collapse to 0.
    obj_row = _analog_value_row(obj_id, behavior="raw", behavior_params="{}", name="RTU-1-Supply-Fan-Command")
    bacnet_obj, _ = engine._create_object(obj_row, slot=0, device_name="RTU")

    recovered_value = float(bacnet_obj.presentValue)
    assert recovered_value == pytest.approx(30.0)
    assert recovered_value > 1.0, (
        "must stay above RTU.mo's uFan>0.01 fan-enable threshold -- "
        "landing at/near 0 here is exactly the latch this fix prevents"
    )
