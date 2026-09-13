"""POST /simulation/models/{id}/pause|resume|reset -- the HTTP layer for
per-device runtime controls (see test_provider_pause_not_auto_resumed.py for
the actual engine-level pause/resume/reset/local-pause-survives-global-
pause behavior this relies on). Same client/engine fixture-override shape
as test_simulation_model_enabled_toggle.py's own _RecordingEngine, extended
with pause_provider/resume_provider/reset_provider so these routes' own
404-on-missing-model and 404-on-not-registered paths can be exercised
without a real SimEngine or FMU runtime.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.api.routers import simulation as simulation_router
from src.simulation.models import runtime as model_runtime
from src.simulation.models.registry import ModelDefinition, VariableDefinition


INPUT_VARIABLE = "supply_air_temp_setpoint_c"


class _RecordingEngine:
    """Extends test_simulation_model_enabled_toggle.py's own _RecordingEngine
    shape with the three new per-provider actions -- pause_provider/
    resume_provider succeed only for a runtime_id that's actually
    registered (mirrors real SimEngine's own `self._providers.get(...) is
    None -> False` contract), reset_provider the same."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}
        self.register_calls: list[str] = []
        self.unregister_calls: list[str] = []
        self.paused: set[str] = set()
        self.pause_calls: list[str] = []
        self.resume_calls: list[str] = []
        self.reset_calls: list[str] = []
        self.real_time_factor: dict[str, float] = {}

    async def reload(self) -> None:
        pass

    async def add_object_hot(self, device_instance: int, obj: dict) -> None:
        pass

    def get_simulation_providers(self) -> dict[str, Any]:
        return {
            runtime_id: {
                "status": "paused" if runtime_id in self.paused else "running",
                "real_time_factor": self.real_time_factor.get(runtime_id, 1.0),
            }
            for runtime_id in self.registered
        }

    def register_simulation_provider(
        self, runtime_id: str, provider: Any, *,
        context: Any = None, input_point_ids: Any = None, output_point_ids: Any = None,
        replace: bool = True,
    ) -> None:
        self.registered[runtime_id] = provider
        self.register_calls.append(runtime_id)

    def unregister_simulation_provider(self, runtime_id: str) -> bool:
        self.unregister_calls.append(runtime_id)
        self.paused.discard(runtime_id)
        return self.registered.pop(runtime_id, None) is not None

    def pause_provider(self, runtime_id: str) -> bool:
        self.pause_calls.append(runtime_id)
        if runtime_id not in self.registered:
            return False
        self.paused.add(runtime_id)
        return True

    def resume_provider(self, runtime_id: str) -> bool:
        self.resume_calls.append(runtime_id)
        if runtime_id not in self.registered:
            return False
        self.paused.discard(runtime_id)
        return True

    def reset_provider(self, runtime_id: str) -> bool:
        self.reset_calls.append(runtime_id)
        if runtime_id not in self.registered:
            return False
        self.paused.discard(runtime_id)
        return True

    def set_provider_real_time_factor(self, runtime_id: str, target: float) -> bool:
        if runtime_id not in self.registered:
            return False
        self.real_time_factor[runtime_id] = target
        return True


class _FakeFMUProvider:
    def __init__(self, *, runtime_url, model, bindings, aggregate_inputs=None,
                 input_exposures=None, input_defaults, timeout_s, input_variables, output_variables,
                 api_key=None) -> None:
        self.runtime_url = runtime_url
        self.model = model
        self.bindings = list(bindings)


@pytest.fixture
def client(client):
    engine = _RecordingEngine()
    client.app.state.engine = engine
    return client


@pytest.fixture
def engine(client) -> _RecordingEngine:
    return client.app.state.engine


def _fake_ahu_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="SimpleAHU",
        label="Simple AHU",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(
            VariableDefinition(INPUT_VARIABLE, "Supply Air Temp Setpoint", "input"),
        ),
        factory=lambda parameters: None,
        runtime_model="SimpleAHU",
    )


def _patch_definition(monkeypatch) -> ModelDefinition:
    definition = _fake_ahu_definition()
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _model_type: definition)
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _settings, _model_type: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    return definition


def _make_device_and_point(client, database, *, instance: int, name: str = "AHU-Test"):
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": instance, "name": name}).json()
    # database.create_object(...) directly, not POST /devices/{id}/objects --
    # that HTTP route currently 404s in this test app (confirmed pre-existing,
    # unrelated to this feature: tests/test_ede_export.py hits the exact same
    # 404 on an untouched file/commit), so every other test file added this
    # session creates points this way instead (see test_training_export_
    # routes.py's own _make_point).
    point = database.create_object(device["id"], {
        "object_type": "analog-value",
        "object_instance": 1,
        "name": "SAT-Setpoint",
        "units": "degC",
        "behavior": "constant",
        "behavior_params": '{"value":0}',
        "enabled": 1,
        "number_of_states": 2,
        "reliability": "no-fault-detected",
        "polarity": "normal",
        "point_type": None,
    })
    return device, point


def _payload(device_id: int, point_id: int, *, enabled: bool) -> dict:
    return {
        "name": "AHU-Test Simple AHU",
        "provider_type": "fmu",
        "model_type": "SimpleAHU",
        "enabled": enabled,
        "created_from_device_id": device_id,
        "parameters": {"input_sources": {INPUT_VARIABLE: "point"}},
        "mappings": [
            {"variable": INPUT_VARIABLE, "direction": "input", "point_id": point_id},
        ],
        "aggregate_mappings": [],
    }


def test_get_runtime_404_for_unknown_model(client, database):
    resp = client.get("/simulation/models/999999/runtime")
    assert resp.status_code == 404


def test_get_runtime_has_no_side_effects(client, database, monkeypatch, engine):
    """The whole reason this is a separate GET route from the action ones
    below: viewing status must never itself pause/resume/reload anything."""
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, database, instance=8000, name="AHU-GetRuntime")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    runtime_id = created["runtime_id"]
    pause_calls_before = list(engine.pause_calls)
    resume_calls_before = list(engine.resume_calls)

    resp = client.get(f"/simulation/models/{created['id']}/runtime")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "model_id": created["id"],
        "runtime_id": runtime_id,
        "runtime": engine.get_simulation_providers()[runtime_id],
    }
    assert engine.pause_calls == pause_calls_before
    assert engine.resume_calls == resume_calls_before


@pytest.mark.parametrize("action", ["pause", "resume", "reset"])
def test_action_404_for_unknown_model(client, database, action):
    resp = client.post(f"/simulation/models/999999/{action}")
    assert resp.status_code == 404


@pytest.mark.parametrize("action", ["pause", "resume", "reset"])
def test_action_404_when_model_disabled_no_live_provider(client, database, monkeypatch, engine, action):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, database, instance=8001, name=f"AHU-{action}-disabled")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=False)).json()

    resp = client.post(f"/simulation/models/{created['id']}/{action}")
    assert resp.status_code == 404


def test_pause_route_returns_expected_shape_and_pauses(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, database, instance=8002, name="AHU-Pause")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    runtime_id = created["runtime_id"]

    resp = client.post(f"/simulation/models/{created['id']}/pause")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "ok": True,
        "model_id": created["id"],
        "runtime_id": runtime_id,
        "runtime": engine.get_simulation_providers()[runtime_id],
    }
    assert runtime_id in engine.paused


def test_resume_route_resumes(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, database, instance=8003, name="AHU-Resume")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    runtime_id = created["runtime_id"]
    client.post(f"/simulation/models/{created['id']}/pause")
    assert runtime_id in engine.paused

    resp = client.post(f"/simulation/models/{created['id']}/resume")
    assert resp.status_code == 200, resp.text
    assert runtime_id not in engine.paused


def test_reset_route_resets(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, database, instance=8004, name="AHU-Reset")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    runtime_id = created["runtime_id"]
    client.post(f"/simulation/models/{created['id']}/pause")

    resp = client.post(f"/simulation/models/{created['id']}/reset")
    assert resp.status_code == 200, resp.text
    assert runtime_id in engine.reset_calls
    assert runtime_id not in engine.paused


def test_pausing_one_model_does_not_affect_another(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device_a, point_a = _make_device_and_point(client, database, instance=8005, name="AHU-A")
    device_b, point_b = _make_device_and_point(client, database, instance=8006, name="AHU-B")
    model_a = client.post("/simulation/models", json=_payload(device_a["id"], point_a["id"], enabled=True)).json()
    model_b = client.post("/simulation/models", json=_payload(device_b["id"], point_b["id"], enabled=True)).json()

    resp = client.post(f"/simulation/models/{model_a['id']}/pause")
    assert resp.status_code == 200

    assert model_a["runtime_id"] in engine.paused
    assert model_b["runtime_id"] not in engine.paused


# ─── Per-provider real-time-factor ─────────────────────────────────────────

def test_rtf_route_404_for_unknown_model(client, database):
    resp = client.post("/simulation/models/999999/rtf", json={"target": 2.0})
    assert resp.status_code == 404


def test_rtf_route_400_for_non_positive_target(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, database, instance=8007, name="AHU-RTF-Invalid")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()

    for bad_target in (0, -1.0):
        resp = client.post(f"/simulation/models/{created['id']}/rtf", json={"target": bad_target})
        assert resp.status_code == 400


def test_rtf_route_sets_target_and_is_reflected_in_runtime(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, database, instance=8008, name="AHU-RTF")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    runtime_id = created["runtime_id"]

    resp = client.post(f"/simulation/models/{created['id']}/rtf", json={"target": 2.5})
    assert resp.status_code == 200, resp.text
    assert resp.json()["runtime"]["real_time_factor"] == 2.5
    assert engine.real_time_factor[runtime_id] == 2.5
