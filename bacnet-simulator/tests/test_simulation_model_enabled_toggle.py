"""Per-model Enabled ON/OFF control (PUT /simulation/models/{id}/enabled) --
the dedicated SimEngine-participation toggle added to the Simulation Model
drawer's footer, distinct from the existing "Save as draft" (enabled=false)
vs "Apply" (enabled=true) flow already exercised in
test_simulation_model_aggregate_persistence.py.

Unlike that file's tests (which deliberately stay on enabled=false because
the shared conftest fixtures have no working engine.register_simulation_
provider()), toggling enabled=true genuinely re-registers a runtime
provider -- so this file builds its own _RecordingEngine (a working
register/unregister pair that records calls) and monkeypatches
FMUSimulationProvider the same way
test_reconstructed_runtime_config_wires_aggregate_input does, so the full
reload_model() lifecycle actually runs instead of being avoided.

Covers the four things asked for: disable, re-enable, persistence
(project save/reload), and isolation (one model's toggle must not affect
another's registration or configuration).
"""
from __future__ import annotations

from typing import Any

import pytest

from src.api.routers import simulation as simulation_router
from src.monitoring.event_log import get_device_log_entries
from src.simulation import model_runtime
from src.simulation.model_store import list_simulation_models
from src.simulation.models.registry import ModelDefinition, VariableDefinition


INPUT_VARIABLE = "supply_air_temp_setpoint_c"


class _RecordingEngine:
    """A working register/unregister pair (unlike conftest's _MinimalSimEngine,
    which has neither) so enabled=true actually exercises
    model_runtime.register_model_config, not just enabled=false's
    already-covered "never touch the engine" path."""
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}
        self.register_calls: list[str] = []
        self.unregister_calls: list[str] = []

    async def reload(self) -> None:
        pass

    async def add_object_hot(self, device_instance: int, obj: dict) -> None:
        pass

    def get_simulation_providers(self) -> dict[str, Any]:
        return dict(self.registered)

    def register_simulation_provider(
        self, runtime_id: str, provider: Any, *,
        context: Any = None, input_point_ids: Any = None, output_point_ids: Any = None,
        replace: bool = True,
    ) -> None:
        self.registered[runtime_id] = provider
        self.register_calls.append(runtime_id)

    def unregister_simulation_provider(self, runtime_id: str) -> bool:
        self.unregister_calls.append(runtime_id)
        return self.registered.pop(runtime_id, None) is not None

    def get_simulation_providers_count(self) -> int:
        return len(self.registered)


class _FakeFMUProvider:
    """Mirrors test_simulation_model_aggregate_persistence.py's own
    _FakeFMUProvider -- construction must succeed without a real FMU
    runtime for register_model_config to complete."""
    def __init__(self, *, runtime_url, model, bindings, aggregate_inputs=None,
                 input_exposures=None, input_defaults, timeout_s, input_variables, output_variables) -> None:
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


def _make_device_and_point(client, *, instance: int, name: str = "AHU-Test"):
    device = client.post("/devices", json={"device_instance": instance, "name": name}).json()
    point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-value",
        "object_instance": 1,
        "name": "SAT-Setpoint",
        "units": "degC",
    }).json()
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


# ─── 1. Disable ─────────────────────────────────────────────────────────

def test_disable_unregisters_provider_and_persists(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=4001)
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    assert created["enabled"] is True
    runtime_id = created["runtime_id"]
    assert runtime_id in engine.registered

    resp = client.put(f"/simulation/models/{created['id']}/enabled", json={"enabled": False})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False

    # Actually unregistered from the (fake, but working) runtime engine --
    # this is the concrete "no longer stepped/no longer writing outputs"
    # guarantee, since SimEngine only steps registered providers.
    assert runtime_id not in engine.registered
    assert runtime_id in engine.unregister_calls

    # Persisted, not just returned in the response.
    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    assert reloaded["enabled"] is False


# ─── 2. Re-enable ───────────────────────────────────────────────────────

def test_reenable_reregisters_provider(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=4002)
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=False)).json()
    assert created["enabled"] is False
    runtime_id = created["runtime_id"]
    assert runtime_id not in engine.registered

    resp = client.put(f"/simulation/models/{created['id']}/enabled", json={"enabled": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True

    assert runtime_id in engine.registered
    assert runtime_id in engine.register_calls

    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    assert reloaded["enabled"] is True


# ─── 3. Configuration/mappings preserved while disabled ────────────────

def test_disable_preserves_mappings_exactly(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=4003)
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    mappings_before = created["mappings"]
    parameters_before = created["parameters"]

    client.put(f"/simulation/models/{created['id']}/enabled", json={"enabled": False})

    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    assert reloaded["mappings"] == mappings_before
    assert reloaded["parameters"] == parameters_before
    assert reloaded["name"] == created["name"]
    assert reloaded["model_type"] == created["model_type"]


def test_toggle_missing_model_404(client, database):
    resp = client.put("/simulation/models/999999/enabled", json={"enabled": False})
    assert resp.status_code == 404


# ─── 4. Isolation between models ────────────────────────────────────────

def test_disabling_one_model_does_not_affect_another(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device_a, point_a = _make_device_and_point(client, instance=4004, name="AHU-A")
    device_b, point_b = _make_device_and_point(client, instance=4005, name="AHU-B")
    model_a = client.post("/simulation/models", json=_payload(device_a["id"], point_a["id"], enabled=True)).json()
    model_b = client.post("/simulation/models", json=_payload(device_b["id"], point_b["id"], enabled=True)).json()

    runtime_id_a = model_a["runtime_id"]
    runtime_id_b = model_b["runtime_id"]
    assert runtime_id_a in engine.registered
    assert runtime_id_b in engine.registered

    # Snapshot calls made so far (each model's own creation already
    # produced one unregister-before-register no-op via reload_model --
    # not itself a sign of cross-model interference) so the assertions
    # below only look at what the PUT .../enabled call itself does.
    calls_before = list(engine.unregister_calls)

    resp = client.put(f"/simulation/models/{model_a['id']}/enabled", json={"enabled": False})
    assert resp.status_code == 200

    # A is gone from the runtime; B is completely untouched -- the toggle
    # only ever touches A's runtime_id (no spurious reload of an unrelated
    # model), and B's own DB row/mappings are unaffected.
    assert runtime_id_a not in engine.registered
    assert runtime_id_b in engine.registered
    new_calls = engine.unregister_calls[len(calls_before):]
    assert new_calls == [runtime_id_a]

    model_b_reloaded = client.get(f"/simulation/models/{model_b['id']}").json()
    assert model_b_reloaded["enabled"] is True
    assert model_b_reloaded["mappings"] == model_b["mappings"]


# ─── 5. Persistence through project save/reload ────────────────────────
# Uses the `client` fixture to create the device/point/model (same as the
# tests above -- avoids hand-building raw Database.create_device/
# create_object dicts, which require every named SQL column present or
# sqlite3 raises), then drops to the `database` fixture directly for the
# save_project/clear_live_state/load_project round trip itself, mirroring
# test_custom_graphs_project_roundtrip.py's pattern.

def _sole_model_id(database) -> int:
    models = list_simulation_models(database)
    assert len(models) == 1
    return models[0]["id"]


def test_disabled_state_survives_project_save_and_reload(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=5001, name="AHU-Persist")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    client.put(f"/simulation/models/{created['id']}/enabled", json={"enabled": False})

    project = database.save_project("Round-trip Test", "")
    database.clear_live_state()
    database.load_project(project["id"])

    reloaded = client.get(f"/simulation/models/{_sole_model_id(database)}").json()
    assert reloaded["enabled"] is False
    assert len(reloaded["mappings"]) == 1
    assert reloaded["mappings"][0]["variable"] == INPUT_VARIABLE


def test_reenabled_state_survives_project_save_and_reload(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=5002, name="AHU-Persist2")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=False)).json()
    client.put(f"/simulation/models/{created['id']}/enabled", json={"enabled": True})

    project = database.save_project("Round-trip Test", "")
    database.clear_live_state()
    database.load_project(project["id"])

    reloaded = client.get(f"/simulation/models/{_sole_model_id(database)}").json()
    assert reloaded["enabled"] is True


# ─── 6. Activity Log: category="simulation" events ─────────────────────
# See src/monitoring/event_log.py's category parameter and the controller-
# contextual Activity Log feature -- these two dimensions (level, category)
# are what the admin UI's Simulation/Changes/Errors filters key off of.

def test_enable_route_logs_simulation_event(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=6001, name="AHU-LogEnable")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=False)).json()
    client.app.state.logged_events.clear()

    resp = client.put(f"/simulation/models/{created['id']}/enabled", json={"enabled": True})
    assert resp.status_code == 200, resp.text

    matches = [
        e for e in client.app.state.logged_events
        if e["category"] == "simulation" and "enabled" in e["message"]
    ]
    assert len(matches) == 1, client.app.state.logged_events
    assert matches[0]["device_id"] == device["id"]
    assert matches[0]["level"] == "info"


def test_disable_route_logs_simulation_event(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=6002, name="AHU-LogDisable")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    client.app.state.logged_events.clear()

    resp = client.put(f"/simulation/models/{created['id']}/enabled", json={"enabled": False})
    assert resp.status_code == 200, resp.text

    matches = [
        e for e in client.app.state.logged_events
        if e["category"] == "simulation" and "disabled" in e["message"]
    ]
    assert len(matches) == 1, client.app.state.logged_events
    assert matches[0]["device_id"] == device["id"]


class _RecordedLog:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def __call__(self, device_id, level, message, *, category="audit") -> None:
        self.entries.append({
            "device_id": device_id, "level": level, "message": message, "category": category,
        })


def test_reload_model_logs_on_success(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    recorded = _RecordedLog()
    monkeypatch.setattr(model_runtime, "_log_event", recorded)

    device, point = _make_device_and_point(client, instance=6003, name="AHU-ReloadOK")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=False)).json()
    recorded.entries.clear()

    model_runtime.reload_model(database, engine, created["id"])

    started = [e for e in recorded.entries if "FMU model started" in e["message"]]
    assert not started, "enabled=false must not register a provider or log a start event"

    from src.simulation.model_store import set_simulation_model_enabled
    set_simulation_model_enabled(database, created["id"], True)
    recorded.entries.clear()

    model_runtime.reload_model(database, engine, created["id"])

    started = [e for e in recorded.entries if "FMU model started" in e["message"]]
    assert len(started) == 1, recorded.entries
    assert started[0]["category"] == "simulation"
    assert started[0]["level"] == "info"
    assert started[0]["device_id"] == device["id"]


def test_reload_model_logs_on_failure(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    recorded = _RecordedLog()
    monkeypatch.setattr(model_runtime, "_log_event", recorded)

    device, point = _make_device_and_point(client, instance=6004, name="AHU-ReloadFail")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    recorded.entries.clear()

    def _boom(**_kwargs):
        raise RuntimeError("FMU runtime unreachable")

    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _boom)

    with pytest.raises(RuntimeError):
        model_runtime.reload_model(database, engine, created["id"])

    failed = [e for e in recorded.entries if "FMU registration failed" in e["message"]]
    assert len(failed) == 1, recorded.entries
    assert failed[0]["category"] == "simulation"
    assert failed[0]["level"] == "error"
    assert failed[0]["device_id"] == device["id"]
    assert "FMU runtime unreachable" in failed[0]["message"]


# ─── 7. reconcile_enabled_models: the app-startup path ──────────────────
# bootstrap_simulation_models() (called once from lifespan() after
# engine.start()) calls this directly, NOT reload_model() -- it has its own
# register_model_config() call and its own try/except, so it needs its own
# logging rather than inheriting reload_model()'s. Without this, a model
# that registers successfully at boot (the common case) never produced any
# Activity Log entry at all, while an unhealthy one only got logged once
# the periodic recovery sweep (which does go through reload_model())
# started retrying it -- exactly the asymmetry a real boot surfaced.

def test_reconcile_enabled_models_logs_on_success(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    recorded = _RecordedLog()
    monkeypatch.setattr(model_runtime, "_log_event", recorded)

    device, point = _make_device_and_point(client, instance=6005, name="AHU-ReconcileOK")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    recorded.entries.clear()

    model_runtime.reconcile_enabled_models(database, engine)

    started = [e for e in recorded.entries if "FMU model started" in e["message"]]
    assert len(started) == 1, recorded.entries
    assert started[0]["category"] == "simulation"
    assert started[0]["level"] == "info"
    assert started[0]["device_id"] == device["id"]


def test_reconcile_enabled_models_logs_on_failure(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    recorded = _RecordedLog()
    monkeypatch.setattr(model_runtime, "_log_event", recorded)

    device, point = _make_device_and_point(client, instance=6006, name="AHU-ReconcileFail")
    client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True))
    recorded.entries.clear()

    def _boom(**_kwargs):
        raise RuntimeError("FMU runtime unreachable")

    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _boom)

    result = model_runtime.reconcile_enabled_models(database, engine)
    assert result["errors"], "reconcile_enabled_models must not raise -- it collects errors"

    failed = [e for e in recorded.entries if "FMU registration failed" in e["message"]]
    assert len(failed) == 1, recorded.entries
    assert failed[0]["category"] == "simulation"
    assert failed[0]["level"] == "error"
    assert failed[0]["device_id"] == device["id"]


# ─── 8. Save/Apply must not change the Enabled state ─────────────────────
# The actual bug this closes: SimulationModelDrawer.vue's buildPayload(apply)
# used to send enabled=apply -- Save always sent enabled=false (silently
# disabling a running model), Apply always sent enabled=true (silently
# enabling a disabled one) -- and PUT .../{id} unconditionally reloaded the
# runtime on every save regardless. See src/api/routers/simulation.py's
# edit_simulation_model `apply` query param (default False = pure
# persistence, zero engine calls) and the drawer's buildPayload/persist.

def _edited_payload(device_id: int, point_id: int, *, enabled: bool) -> dict:
    """Same _payload() shape but with a real, harmless-to-validate field
    change (name), so PUT genuinely has something new to persist -- avoids
    touching `parameters`, which the backend validates against the fake
    model definition's (empty) declared parameter set."""
    payload = _payload(device_id, point_id, enabled=enabled)
    payload["name"] = "AHU-Test Simple AHU (edited)"
    return payload


def test_disabled_edit_save_stays_disabled(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=7001, name="AHU-DisSave")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=False)).json()
    assert created["enabled"] is False
    runtime_id = created["runtime_id"]
    assert runtime_id not in engine.registered
    register_calls_before = list(engine.register_calls)
    unregister_calls_before = list(engine.unregister_calls)

    resp = client.put(
        f"/simulation/models/{created['id']}",
        json=_edited_payload(device["id"], point["id"], enabled=False),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    assert resp.json()["name"] == "AHU-Test Simple AHU (edited)"

    assert runtime_id not in engine.registered
    assert engine.register_calls == register_calls_before
    assert engine.unregister_calls == unregister_calls_before


def test_disabled_edit_apply_stays_disabled(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=7002, name="AHU-DisApply")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=False)).json()
    runtime_id = created["runtime_id"]

    resp = client.put(
        f"/simulation/models/{created['id']}?apply=true",
        json=_edited_payload(device["id"], point["id"], enabled=False),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False

    # Apply on a disabled model does reach reload_model() (unregister --
    # a no-op here, nothing was registered -- then it correctly skips
    # re-registering since enabled=False): configuration is pushed, but
    # nothing ever gets registered/started.
    assert runtime_id not in engine.registered
    assert runtime_id not in engine.register_calls


def test_enabled_edit_save_stays_enabled(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=7003, name="AHU-EnSave")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    assert created["enabled"] is True
    runtime_id = created["runtime_id"]
    assert runtime_id in engine.registered
    register_calls_before = list(engine.register_calls)
    unregister_calls_before = list(engine.unregister_calls)

    resp = client.put(
        f"/simulation/models/{created['id']}",
        json=_edited_payload(device["id"], point["id"], enabled=True),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True

    # Still registered, and -- the actual point of this test -- Save did
    # not blip it: zero new register/unregister calls.
    assert runtime_id in engine.registered
    assert engine.register_calls == register_calls_before
    assert engine.unregister_calls == unregister_calls_before


def test_enabled_edit_apply_stays_enabled(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=7004, name="AHU-EnApply")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    runtime_id = created["runtime_id"]
    register_calls_before = list(engine.register_calls)
    unregister_calls_before = list(engine.unregister_calls)

    resp = client.put(
        f"/simulation/models/{created['id']}?apply=true",
        json=_edited_payload(device["id"], point["id"], enabled=True),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True

    # Still enabled and registered -- Apply genuinely did reload it this
    # time (one new unregister + one new register), on purpose.
    assert runtime_id in engine.registered
    assert engine.register_calls[len(register_calls_before):] == [runtime_id]
    assert engine.unregister_calls[len(unregister_calls_before):] == [runtime_id]


def test_only_enabled_toggle_changes_participation(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    device, point = _make_device_and_point(client, instance=7005, name="AHU-OnlyToggle")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=True)).json()
    runtime_id = created["runtime_id"]
    assert runtime_id in engine.registered
    # Creation itself already produced one unregister-before-register
    # no-op via reload_model() -- not itself a sign of anything wrong, see
    # test_disabling_one_model_does_not_affect_another's identical note.
    # Snapshot here so the loop below only asserts on what the 3 saves add.
    register_calls_before = list(engine.register_calls)
    unregister_calls_before = list(engine.unregister_calls)

    for i in range(3):
        payload = _payload(device["id"], point["id"], enabled=True)
        payload["name"] = f"AHU-Test Simple AHU (edit {i})"
        resp = client.put(f"/simulation/models/{created['id']}", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is True

    # Three plain saves, zero *new* engine calls -- participation is
    # completely unaffected by anything except the dedicated toggle below.
    assert engine.register_calls == register_calls_before
    assert engine.unregister_calls == unregister_calls_before
    assert runtime_id in engine.registered

    resp = client.put(f"/simulation/models/{created['id']}/enabled", json={"enabled": False})
    assert resp.status_code == 200, resp.text
    assert runtime_id not in engine.registered
    assert runtime_id in engine.unregister_calls


# ─── 9. Enable toggle must produce exactly one Activity Log line ─────────
# In production, app.state.log_event IS monitoring.event_log._log_event
# (wired in simulation/runtime.py's lifespan()), so the route's own
# "simulation enabled" message and reload_model()'s generic "FMU model
# started" message both land in the SAME per-device buffer the admin UI's
# Activity Log reads -- that's exactly how a user sees two lines for one
# click. tests/conftest.py's test harness deliberately gives app.state.
# log_event its own isolated recorder instead (app.state.logged_events,
# see test_enable_route_logs_simulation_event above) rather than routing
# through the real global bufer, so the two can't collide across unrelated
# tests -- which means the real global buffer isn't the right place to
# observe this from a test. Assert directly on reload_model()'s new
# log_success param instead: this is what actually eliminates the second
# line, and test_enable_route_logs_simulation_event already confirms the
# route's own message still fires exactly once.

def test_reload_model_log_success_false_suppresses_started_message(client, database, monkeypatch, engine):
    _patch_definition(monkeypatch)
    recorded = _RecordedLog()
    monkeypatch.setattr(model_runtime, "_log_event", recorded)

    device, point = _make_device_and_point(client, instance=7006, name="AHU-SuppressLog")
    created = client.post("/simulation/models", json=_payload(device["id"], point["id"], enabled=False)).json()

    from src.simulation.model_store import set_simulation_model_enabled
    set_simulation_model_enabled(database, created["id"], True)
    recorded.entries.clear()

    model_runtime.reload_model(database, engine, created["id"], log_success=False)

    started = [e for e in recorded.entries if "FMU model started" in e["message"]]
    assert started == [], recorded.entries
    # The provider genuinely did get registered -- log_success only
    # suppresses the log line, not the actual activation.
    assert created["runtime_id"] in engine.registered
