from __future__ import annotations

import logging

import pytest

from src.simulation import model_runtime
from src.simulation.model_store import create_simulation_model
from src.simulation.models.registry import ModelDefinition, VariableDefinition
from src.simulation.providers import FMUPointBinding, SimulationContext
from src.simulation.providers.fmu import FMUInputResolutionError, FMUSimulationProvider


def _fake_vav_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="SimpleVAVZone",
        label="Simple VAV Zone",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(
            VariableDefinition("supply_air_temp_c", "Supply Air Temperature", "input"),
            VariableDefinition("zone_temp_c", "Zone Temperature", "output"),
        ),
        factory=lambda parameters: None,
        runtime_model="SimpleVAVZone",
    )


class _FakeFMUProvider:
    """Mirrors tests/test_simulation_model_input_sources.py's fake -- records
    constructor kwargs so tests can assert what model_runtime built, without
    ever contacting a real FMU runtime."""

    created: list["_FakeFMUProvider"] = []

    def __init__(
        self,
        *,
        runtime_url,
        model,
        bindings,
        aggregate_inputs=None,
        input_defaults,
        timeout_s,
        input_variables,
        output_variables,
    ) -> None:
        self.runtime_url = runtime_url
        self.model = model
        self.bindings = list(bindings)
        self.aggregate_inputs = list(aggregate_inputs or [])
        self.input_defaults = dict(input_defaults)
        self.timeout_s = timeout_s
        self.input_variables = set(input_variables)
        self.output_variables = set(output_variables)
        self.created.append(self)


class _FakeEngine:
    """Extends the convention in test_simulation_model_input_sources.py with
    resolve_provider_input_value() (live-value resolution) and a
    configurable per-runtime-id status for get_simulation_providers(), so
    the recovery sweep can be exercised without a real SimEngine."""

    def __init__(self, *, live_values: dict[int, object] | None = None) -> None:
        self.registrations: dict[str, dict] = {}
        self.statuses: dict[str, str] = {}
        self._live_values = dict(live_values or {})

    def resolve_provider_input_value(self, point_id: int):
        return self._live_values.get(point_id)

    def get_simulation_providers(self) -> dict:
        return {
            runtime_id: {"status": self.statuses.get(runtime_id, "running")}
            for runtime_id in self.registrations
        }

    def unregister_simulation_provider(self, runtime_id: str) -> bool:
        self.statuses.pop(runtime_id, None)
        return self.registrations.pop(runtime_id, None) is not None

    def register_simulation_provider(
        self,
        runtime_id,
        provider,
        *,
        context,
        input_point_ids,
        output_point_ids,
        replace,
    ) -> None:
        self.registrations[runtime_id] = {
            "provider": provider,
            "context": context,
            "input_point_ids": set(input_point_ids),
            "output_point_ids": set(output_point_ids),
            "replace": replace,
        }
        self.statuses[runtime_id] = "running"


def _create_fmu_model(
    database,
    *,
    name: str,
    mappings: list[dict] | None = None,
    parameters: dict | None = None,
) -> dict:
    return create_simulation_model(
        database,
        name=name,
        provider_type="fmu",
        model_type="SimpleVAVZone",
        enabled=True,
        parameters=parameters or {},
        created_from_device_id=None,
        mappings=mappings or [],
    )


# ---------------------------------------------------------------------------
# 1. Live Point values are resolved before initialize() -- model_runtime layer
# ---------------------------------------------------------------------------

def test_point_input_resolves_live_value_before_registration(
    client,
    database,
    monkeypatch,
) -> None:
    definition = _fake_vav_definition()
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _s, _m: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    _FakeFMUProvider.created.clear()

    device = client.post("/devices", json={"device_instance": 2501, "name": "AHU-1"}).json()
    sat_point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input",
        "object_instance": 5,
        "name": "SAT",
        "units": "degrees-celsius",
    }).json()

    model = _create_fmu_model(
        database,
        name="VAV Zone 2",
        parameters={"input_sources": {"supply_air_temp_c": "point"}},
        mappings=[
            {"variable": "supply_air_temp_c", "direction": "input", "point_id": sat_point["id"]},
        ],
    )

    engine = _FakeEngine(live_values={sat_point["id"]: 20.0})
    result = model_runtime.reconcile_enabled_models(database, engine)

    assert result["errors"] == []
    runtime_id = model_runtime.provider_runtime_id(model)
    context: SimulationContext = engine.registrations[runtime_id]["context"]
    assert context.metadata["initial_point_inputs"] == {sat_point["id"]: 20.0}


def test_unresolvable_point_is_omitted_from_initial_inputs(
    client,
    database,
    monkeypatch,
) -> None:
    definition = _fake_vav_definition()
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _s, _m: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    _FakeFMUProvider.created.clear()

    device = client.post("/devices", json={"device_instance": 2502, "name": "AHU-2"}).json()
    sat_point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input",
        "object_instance": 5,
        "name": "SAT",
        "units": "degrees-celsius",
    }).json()

    model = _create_fmu_model(
        database,
        name="VAV Zone 3",
        parameters={"input_sources": {"supply_air_temp_c": "point"}},
        mappings=[
            {"variable": "supply_air_temp_c", "direction": "input", "point_id": sat_point["id"]},
        ],
    )

    # No live value available yet -- engine's resolver returns None for every point.
    engine = _FakeEngine(live_values={})
    result = model_runtime.reconcile_enabled_models(database, engine)

    assert result["errors"] == []
    runtime_id = model_runtime.provider_runtime_id(model)
    context: SimulationContext = engine.registrations[runtime_id]["context"]
    assert context.metadata["initial_point_inputs"] == {}


# ---------------------------------------------------------------------------
# 2. FMUSimulationProvider.initialize() aborts (never contacts the runtime)
#    when a configured Point input has no live value -- fmu.py layer
# ---------------------------------------------------------------------------

def _never_call_runtime(*_args, **_kwargs):
    raise AssertionError(
        "FMU runtime must not be contacted when a Point input is unresolved"
    )


def test_initialize_aborts_on_unresolved_point_without_contacting_runtime(
    monkeypatch,
    caplog,
) -> None:
    binding = FMUPointBinding(point_id=10141, variable="supply_air_temp_c", direction="input")
    provider = FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="SimpleVAVZone",
        bindings=[binding],
        input_defaults={},
        input_variables={"supply_air_temp_c"},
        output_variables=set(),
    )
    monkeypatch.setattr(provider._client, "health", _never_call_runtime)
    monkeypatch.setattr(provider._client, "initialize", _never_call_runtime)

    context = SimulationContext(
        participant_device_ids=[],
        point_configs=[],
        metadata={
            "provider_id": "fmu:SimpleVAVZone:6",
            "simulation_model_id": 6,
            "model": "SimpleVAVZone",
            # No "initial_point_inputs" entry for point 10141 -- unresolved.
            "initial_point_inputs": {},
            "bindings": [
                {
                    "point_id": 10141,
                    "variable": "supply_air_temp_c",
                    "direction": "input",
                    "point_name": "SAT",
                    "device_name": "AHU-1",
                    "device_id": 1671,
                    "object_type": "analog-input",
                    "object_instance": 5,
                },
            ],
        },
    )

    caplog.set_level(logging.WARNING, logger="bacnet-sim")
    with pytest.raises(FMUInputResolutionError):
        provider.initialize(context)

    warning_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "FMU INPUT RESOLVE" in line
        and "input=supply_air_temp_c" in line
        and "mode=point" in line
        and "source=AHU-1/SAT" in line
        and "value=MISSING" in line
        and "device_id=1671" in line
        and "point_id=10141" in line
        for line in warning_lines
    )


def test_initialize_uses_resolved_point_value_and_preserves_constants(
    monkeypatch,
) -> None:
    binding = FMUPointBinding(point_id=10141, variable="supply_air_temp_c", direction="input")
    provider = FMUSimulationProvider(
        runtime_url="http://fmu-runtime:8002",
        model="SimpleVAVZone",
        bindings=[binding],
        input_defaults={"internal_gain_w": 5000},
        input_variables={"supply_air_temp_c", "internal_gain_w"},
        output_variables=set(),
    )

    captured: dict = {}

    def _fake_health(self):
        return {"status": "ok"}

    def _fake_initialize(self, model_id, inputs=None):
        captured["inputs"] = dict(inputs or {})
        return {"session_id": "new-session-123", "state": "RUNNING"}

    monkeypatch.setattr(type(provider._client), "health", _fake_health)
    monkeypatch.setattr(type(provider._client), "initialize", _fake_initialize)

    context = SimulationContext(
        participant_device_ids=[],
        point_configs=[],
        metadata={
            "provider_id": "fmu:SimpleVAVZone:6",
            "simulation_model_id": 6,
            "model": "SimpleVAVZone",
            "initial_point_inputs": {10141: 20.0},
            "bindings": [
                {
                    "point_id": 10141,
                    "variable": "supply_air_temp_c",
                    "direction": "input",
                    "point_name": "SAT",
                    "device_name": "AHU-1",
                    "device_id": 1671,
                    "object_type": "analog-input",
                    "object_instance": 5,
                },
            ],
        },
    )

    provider.initialize(context)

    assert captured["inputs"]["supply_air_temp_c"] == 20.0
    assert captured["inputs"]["internal_gain_w"] == 5000
    assert provider._session_id == "new-session-123"


# ---------------------------------------------------------------------------
# 3-5. Periodic self-heal sweep -- recover_unhealthy_simulation_models()
# ---------------------------------------------------------------------------

class _FakeHealthyRuntimeClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def health(self) -> dict:
        return {"status": "ok"}


class _FakeUnreachableRuntimeClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def health(self) -> dict:
        raise RuntimeError("FMU runtime is unreachable at http://fmu-runtime:8002: refused")


def test_recovery_sweep_only_reloads_unhealthy_providers(
    database,
    monkeypatch,
) -> None:
    monkeypatch.setattr(model_runtime, "FMURuntimeClient", _FakeHealthyRuntimeClient)

    healthy = _create_fmu_model(database, name="Healthy VAV")
    broken = _create_fmu_model(database, name="Broken VAV")

    engine = _FakeEngine()
    healthy_id = model_runtime.provider_runtime_id(healthy)
    broken_id = model_runtime.provider_runtime_id(broken)
    engine.registrations[healthy_id] = {}
    engine.statuses[healthy_id] = "running"
    engine.registrations[broken_id] = {}
    engine.statuses[broken_id] = "error"

    reload_calls: list[int] = []

    def _spy_reload_model(_db, _engine, model_id):
        reload_calls.append(model_id)
        engine.statuses[broken_id] = "running"
        return {}

    monkeypatch.setattr(model_runtime, "reload_model", _spy_reload_model)

    result = model_runtime.recover_unhealthy_simulation_models(database, engine)

    assert reload_calls == [broken["id"]]
    assert result["recovered"] == [broken_id]
    assert result["runtime_unreachable"] is False


def test_recovery_sweep_does_not_reload_twice_once_healthy(
    database,
    monkeypatch,
) -> None:
    monkeypatch.setattr(model_runtime, "FMURuntimeClient", _FakeHealthyRuntimeClient)

    broken = _create_fmu_model(database, name="Broken VAV")
    broken_id = model_runtime.provider_runtime_id(broken)

    engine = _FakeEngine()
    # Not registered at all yet -- simulates "session never established"
    # (e.g. a prior initialize() aborted on an unresolved Point input).

    reload_calls: list[int] = []

    def _spy_reload_model(_db, _engine, model_id):
        reload_calls.append(model_id)
        engine.registrations[broken_id] = {}
        engine.statuses[broken_id] = "running"
        return {}

    monkeypatch.setattr(model_runtime, "reload_model", _spy_reload_model)

    model_runtime.recover_unhealthy_simulation_models(database, engine)
    model_runtime.recover_unhealthy_simulation_models(database, engine)

    assert reload_calls == [broken["id"]]


def test_recovery_sweep_defers_everything_when_runtime_unreachable(
    database,
    monkeypatch,
) -> None:
    monkeypatch.setattr(model_runtime, "FMURuntimeClient", _FakeUnreachableRuntimeClient)

    broken = _create_fmu_model(database, name="Broken VAV")
    broken_id = model_runtime.provider_runtime_id(broken)

    engine = _FakeEngine()
    engine.registrations[broken_id] = {}
    engine.statuses[broken_id] = "error"

    reload_calls: list[int] = []
    monkeypatch.setattr(
        model_runtime,
        "reload_model",
        lambda _db, _engine, model_id: reload_calls.append(model_id),
    )

    result = model_runtime.recover_unhealthy_simulation_models(database, engine)

    assert reload_calls == []
    assert result["recovered"] == []
    assert result["runtime_unreachable"] is True
    assert result["skipped"] == [broken["id"]]
