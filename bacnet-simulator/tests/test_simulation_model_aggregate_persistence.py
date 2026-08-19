"""Persisted FMU Aggregate (MAX) input mappings -- DB + API layer, closing
the gap between the already-tested runtime layer
(tests/test_simulation_model_aggregate_inputs.py) and real persisted data.

Follows tests/test_simulation_model_input_sources.py's conventions: real
device/object creation via the `client` fixture, `_runtime_definition`/
`get_remote_model_definition` monkeypatched to a small fake AHU definition.
Every create/update payload here uses enabled=False (a saved draft) --
enabled=True would trigger add_simulation_model's reload_model() call
against request.app.state.engine, which in tests/conftest.py's `_FakeEngine`
has no register_simulation_provider() at all (no existing test exercises
enabled=True through the live HTTP endpoints for this reason). Disabled
models still run the full per-mapping validation loop (point exists,
simulated device, numeric type, duplicate/conflict checks) -- only the
final "required variable missing" check is skipped, which doesn't affect
any test here since the fake definition's one variable is always either
covered or the specific point of the negative test.
"""
from __future__ import annotations

import pytest

from src.api.routers import simulation as simulation_router
from src.simulation import model_runtime
from src.simulation.model_store import get_simulation_model
from src.simulation.models.registry import ModelDefinition, VariableDefinition


AGGREGATE_VARIABLE = "most_open_vav_damper_pct"


class _MinimalSimEngine:
    """Superset of conftest.py's own _FakeEngine (device/object routes still
    need .reload()/.add_object_hot()) plus get_simulation_providers(), which
    POST/PUT /simulation/models unconditionally call at the end even for
    enabled=False drafts (which never reach reload_model/
    register_simulation_provider). Every test in this file goes through the
    live HTTP endpoints, so the `client` fixture below overrides
    app.state.engine with this fuller-but-still-minimal fake."""
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
            VariableDefinition(AGGREGATE_VARIABLE, "Most-Open VAV Damper Position", "input"),
        ),
        factory=lambda parameters: None,
        runtime_model="SimpleAHU",
    )


def _patch_definition(monkeypatch) -> ModelDefinition:
    definition = _fake_ahu_definition()
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _model_type: definition)
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _settings, _model_type: definition)
    return definition


def _make_device_and_points(client, *, count=3, instance=3001, object_type="analog-input"):
    device = client.post("/devices", json={"device_instance": instance, "name": "AHU-Test"}).json()
    points = [
        client.post(f"/devices/{device['id']}/objects", json={
            "object_type": object_type,
            "object_instance": i + 1,
            "name": f"VAV{i + 1}-Damper",
            "units": "percent",
        }).json()
        for i in range(count)
    ]
    return device, points


def _aggregate_payload(device_id: int, point_ids: list[int], *, enabled: bool = False) -> dict:
    return {
        "name": "AHU-Test Simple AHU",
        "provider_type": "fmu",
        "model_type": "SimpleAHU",
        "enabled": enabled,
        "created_from_device_id": device_id,
        "parameters": {"input_sources": {AGGREGATE_VARIABLE: "aggregate"}},
        "mappings": [],
        "aggregate_mappings": [
            {"variable": AGGREGATE_VARIABLE, "direction": "input", "operation": "max", "point_ids": point_ids},
        ],
    }


def _point_payload(device_id: int, point_id: int, *, enabled: bool = False) -> dict:
    return {
        "name": "AHU-Test Simple AHU",
        "provider_type": "fmu",
        "model_type": "SimpleAHU",
        "enabled": enabled,
        "created_from_device_id": device_id,
        "parameters": {"input_sources": {AGGREGATE_VARIABLE: "point"}},
        "mappings": [
            {"variable": AGGREGATE_VARIABLE, "direction": "input", "point_id": point_id},
        ],
        "aggregate_mappings": [],
    }


def _constant_payload(device_id: int, *, enabled: bool = False) -> dict:
    return {
        "name": "AHU-Test Simple AHU",
        "provider_type": "fmu",
        "model_type": "SimpleAHU",
        "enabled": enabled,
        "created_from_device_id": device_id,
        "parameters": {"input_sources": {AGGREGATE_VARIABLE: "constant"}, "input_defaults": {AGGREGATE_VARIABLE: 50}},
        "mappings": [],
        "aggregate_mappings": [],
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


# ─── 1-2. Create + reload ────────────────────────────────────────────────

def test_create_aggregate_mapping_with_three_points(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client)
    point_ids = [p["id"] for p in points]

    resp = client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids))
    assert resp.status_code == 201
    agg = next(m for m in resp.json()["mappings"] if m["variable"] == AGGREGATE_VARIABLE)
    assert agg["operation"] == "max"
    assert sorted(agg["point_ids"]) == sorted(point_ids)


def test_reload_preserves_aggregate_operation_and_point_ids(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client)
    point_ids = [p["id"] for p in points]
    created = client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids)).json()

    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    agg = next(m for m in reloaded["mappings"] if m["variable"] == AGGREGATE_VARIABLE)
    assert agg["operation"] == "max"
    assert agg["point_ids"] == point_ids  # insertion order preserved


# ─── 3. Update membership ────────────────────────────────────────────────

def test_update_aggregate_membership_replaces_members(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client, count=4)
    point_ids = [p["id"] for p in points]
    created = client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids[:3])).json()

    resp = client.put(f"/simulation/models/{created['id']}", json=_aggregate_payload(device["id"], point_ids[1:4]))
    assert resp.status_code == 200
    agg = next(m for m in resp.json()["mappings"] if m["variable"] == AGGREGATE_VARIABLE)
    assert sorted(agg["point_ids"]) == sorted(point_ids[1:4])


# ─── 4-6. Source-mode transitions ────────────────────────────────────────

def test_aggregate_to_point_transition_clears_aggregate_rows(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client)
    point_ids = [p["id"] for p in points]
    created = client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids)).json()

    resp = client.put(f"/simulation/models/{created['id']}", json=_point_payload(device["id"], point_ids[0]))
    assert resp.status_code == 200

    headers, members = _aggregate_db_rows(database, created["id"])
    assert headers == [] and members == []
    with database._conn() as conn:
        point_rows = conn.execute(
            "SELECT * FROM simulation_model_mappings WHERE model_config_id=? AND direction='input'",
            (created["id"],),
        ).fetchall()
        assert len(point_rows) == 1
        assert point_rows[0]["point_id"] == point_ids[0]


def test_aggregate_to_constant_transition_clears_aggregate_rows(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client)
    point_ids = [p["id"] for p in points]
    created = client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids)).json()

    resp = client.put(f"/simulation/models/{created['id']}", json=_constant_payload(device["id"]))
    assert resp.status_code == 200

    headers, members = _aggregate_db_rows(database, created["id"])
    assert headers == [] and members == []


def test_point_to_aggregate_transition_replaces_point_mapping(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client)
    point_ids = [p["id"] for p in points]
    created = client.post("/simulation/models", json=_point_payload(device["id"], point_ids[0])).json()

    resp = client.put(f"/simulation/models/{created['id']}", json=_aggregate_payload(device["id"], point_ids))
    assert resp.status_code == 200

    with database._conn() as conn:
        point_rows = conn.execute(
            "SELECT * FROM simulation_model_mappings WHERE model_config_id=? AND direction='input'",
            (created["id"],),
        ).fetchall()
        assert point_rows == []
    headers, members = _aggregate_db_rows(database, created["id"])
    assert len(headers) == 1
    assert sorted(m["point_id"] for m in members) == sorted(point_ids)


# ─── 7-12. Validation ─────────────────────────────────────────────────────

def test_create_aggregate_rejects_empty_point_ids(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, _points = _make_device_and_points(client, count=0)
    resp = client.post("/simulation/models", json=_aggregate_payload(device["id"], []))
    assert resp.status_code == 422


def test_create_aggregate_rejects_unsupported_operation(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client, count=1)
    payload = _aggregate_payload(device["id"], [points[0]["id"]])
    payload["aggregate_mappings"][0]["operation"] = "min"
    resp = client.post("/simulation/models", json=payload)
    assert resp.status_code == 422


def test_create_aggregate_rejects_missing_point(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client, count=2)
    point_ids = [points[0]["id"], 999999]
    resp = client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids))
    assert resp.status_code == 422
    assert "999999" in str(resp.json()["detail"])


def test_create_aggregate_rejects_duplicate_point_ids(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client, count=1)
    pid = points[0]["id"]
    resp = client.post("/simulation/models", json=_aggregate_payload(device["id"], [pid, pid]))
    assert resp.status_code == 422


def test_create_aggregate_rejects_non_numeric_member_point(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, analog_points = _make_device_and_points(client, count=1)
    binary_point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "binary-input",
        "object_instance": 50,
        "name": "VAV-Fault-Alarm",
    }).json()
    point_ids = [analog_points[0]["id"], binary_point["id"]]
    resp = client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    message = detail["message"] if isinstance(detail, dict) else detail
    assert "numeric" in message.lower()


def test_create_rejects_point_and_aggregate_for_same_variable(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client, count=2)
    payload = _aggregate_payload(device["id"], [points[0]["id"]])
    payload["mappings"] = [
        {"variable": AGGREGATE_VARIABLE, "direction": "input", "point_id": points[1]["id"]},
    ]
    resp = client.post("/simulation/models", json=payload)
    assert resp.status_code == 422
    assert AGGREGATE_VARIABLE in str(resp.json()["detail"])


# ─── 13-14. Existing Point/Constant behavior unaffected (regression) ─────

def test_existing_point_mapping_create_and_reload_unaffected(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client, count=1)
    created = client.post("/simulation/models", json=_point_payload(device["id"], points[0]["id"])).json()
    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    mapping = next(m for m in reloaded["mappings"] if m["variable"] == AGGREGATE_VARIABLE)
    assert mapping["point_id"] == points[0]["id"]
    assert "point_ids" not in mapping


def test_existing_constant_mapping_create_and_reload_unaffected(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, _points = _make_device_and_points(client, count=0)
    created = client.post("/simulation/models", json=_constant_payload(device["id"])).json()
    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    assert reloaded["mappings"] == []
    assert reloaded["parameters"]["input_sources"][AGGREGATE_VARIABLE] == "constant"


# ─── 15. Closes-the-gap: persisted config reaches the tested runtime path ─

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


def test_reconstructed_runtime_config_wires_aggregate_input(client, database, monkeypatch):
    definition = _patch_definition(monkeypatch)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    _FakeFMUProvider.created.clear()

    device, points = _make_device_and_points(client)
    point_ids = [p["id"] for p in points]
    created = client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids)).json()

    config = {**get_simulation_model(database, created["id"]), "_settings": {}}
    provider, context, inputs, outputs = model_runtime._build_fmu_provider(config, _FakeEngine())

    assert len(_FakeFMUProvider.created) == 1
    built = _FakeFMUProvider.created[-1]
    assert built.bindings == []  # no ordinary FMUPointBinding created for the aggregate variable
    assert len(built.aggregate_inputs) == 1
    assert built.aggregate_inputs[0].operation == "max"
    assert sorted(built.aggregate_inputs[0].point_ids) == sorted(point_ids)
    assert inputs == set(point_ids)
    assert context.metadata["aggregate_inputs"] == [
        {"variable": AGGREGATE_VARIABLE, "source": "aggregate", "operation": "max", "point_ids": point_ids}
    ]
    assert definition.runtime_model == "SimpleAHU"


# ─── No enforced cap on aggregate membership count ───────────────────────
#
# There is no max_length on SimulationModelAggregateMappingPayload.point_ids,
# no UNIQUE/CHECK constraint capping simulation_model_aggregate_members rows
# per aggregate, no slice/truncation in _load_aggregate_mappings or
# _replace_aggregate_mappings, and no cap on FMUAggregateInput.point_ids
# (a plain tuple) -- confirmed by reading each layer, not by assumption.
# This test locks that in: 10 points (more than any example used elsewhere
# in this file) survive create -> reload verbatim, and all 10 -- not just
# the first/last -- genuinely participate in the runtime's MAX computation
# (proven by using the REAL FMUSimulationProvider, not the _FakeFMUProvider
# used above, and putting the highest value on an interior point to rule
# out an off-by-one/truncation bug hiding at either end of the list).

def test_ten_point_aggregate_survives_persistence_and_participates_in_max(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client, count=10)
    point_ids = [p["id"] for p in points]

    created = client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids)).json()
    agg = next(m for m in created["mappings"] if m["variable"] == AGGREGATE_VARIABLE)
    assert len(agg["point_ids"]) == 10
    assert sorted(agg["point_ids"]) == sorted(point_ids)

    reloaded = client.get(f"/simulation/models/{created['id']}").json()
    agg_reloaded = next(m for m in reloaded["mappings"] if m["variable"] == AGGREGATE_VARIABLE)
    assert agg_reloaded["point_ids"] == point_ids  # insertion order preserved, all 10 present

    # Feed the reconstructed config into the REAL FMUSimulationProvider
    # (model_runtime.FMUSimulationProvider is left un-patched here, unlike
    # test_reconstructed_runtime_config_wires_aggregate_input above) so the
    # MAX assertion below exercises the actual resolution/compute path, not
    # a fake that only records what it was constructed with.
    config = {**get_simulation_model(database, created["id"]), "_settings": {}}
    provider, context, inputs, outputs = model_runtime._build_fmu_provider(config, _FakeEngine())

    assert inputs == set(point_ids)
    agg_input = provider._aggregate_inputs[0]
    assert len(agg_input.point_ids) == 10
    assert sorted(agg_input.point_ids) == sorted(point_ids)

    # Highest value deliberately placed on an interior point (index 6 of
    # 10), not the first or last, so a slice/truncation bug at either end
    # of the list would surface as a wrong MAX instead of accidentally
    # still passing.
    values = {pid: float(60 + i) for i, pid in enumerate(point_ids)}
    highest_point_id = point_ids[6]
    values[highest_point_id] = 999.0
    provider.set_inputs(values)

    raw_values, missing, non_numeric = provider._resolve_aggregate_source_values(agg_input)
    assert missing == [] and non_numeric == []
    assert len(raw_values) == 10
    assert provider._compute_aggregate(agg_input, raw_values) == 999.0


# ─── 16. Deleting an aggregate member point/device is rejected ──────────

def test_delete_aggregate_member_point_is_rejected(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client)
    point_ids = [p["id"] for p in points]
    created = client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids)).json()

    resp = client.delete(f"/devices/{device['id']}/objects/{point_ids[0]}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert AGGREGATE_VARIABLE in detail["message"]

    _headers, members = _aggregate_db_rows(database, created["id"])
    assert sorted(m["point_id"] for m in members) == sorted(point_ids)


def test_delete_device_owning_aggregate_member_is_rejected(client, database, monkeypatch):
    _patch_definition(monkeypatch)
    device, points = _make_device_and_points(client)
    point_ids = [p["id"] for p in points]
    client.post("/simulation/models", json=_aggregate_payload(device["id"], point_ids))

    resp = client.delete(f"/devices/{device['id']}")
    assert resp.status_code == 409

    still_there = client.get(f"/devices/{device['id']}/objects/{point_ids[0]}")
    assert still_there.status_code == 200
