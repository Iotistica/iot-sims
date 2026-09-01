from __future__ import annotations

from src.api.routers import simulation as simulation_router
from src.simulation.models import runtime as model_runtime
from src.simulation.models.store import (
    create_simulation_model,
    get_simulation_model,
    update_simulation_model,
)
from src.simulation.models.registry import ModelDefinition, VariableDefinition


def _fake_ahu_definition() -> ModelDefinition:
    return ModelDefinition(
        model_type="SimpleAHU",
        label="Simple AHU",
        provider_type="fmu",
        description="",
        parameters=(),
        variables=(
            VariableDefinition("fan_command_pct", "Fan Command", "input"),
            VariableDefinition("fan_command_pct", "Fan Command", "output"),
        ),
        factory=lambda parameters: None,
        runtime_model="SimpleAHU",
    )


class _FakeEngine:
    def __init__(self) -> None:
        self.registrations: dict[str, dict] = {}

    def get_simulation_providers(self) -> dict:
        return dict(self.registrations)

    def unregister_simulation_provider(self, runtime_id: str) -> bool:
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


class _FakeFMUProvider:
    created: list["_FakeFMUProvider"] = []

    def __init__(
        self,
        *,
        runtime_url,
        model,
        bindings,
        aggregate_inputs=None,
        input_exposures=None,
        input_defaults,
        timeout_s,
        input_variables,
        output_variables,
        api_key=None,
    ) -> None:
        self.runtime_url = runtime_url
        self.model = model
        self.bindings = list(bindings)
        self.aggregate_inputs = list(aggregate_inputs or [])
        self.input_exposures = list(input_exposures or [])
        self.input_defaults = dict(input_defaults)
        self.timeout_s = timeout_s
        self.input_variables = set(input_variables)
        self.output_variables = set(output_variables)
        self.created.append(self)


def test_fmu_input_source_constant_persists_and_bootstraps_without_stale_point(
    client,
    database,
    monkeypatch,
) -> None:
    definition = _fake_ahu_definition()
    monkeypatch.setattr(simulation_router, "_runtime_definition", lambda _db, _model_type: definition)
    monkeypatch.setattr(model_runtime, "get_remote_model_definition", lambda _settings, _model_type: definition)
    monkeypatch.setattr(model_runtime, "FMUSimulationProvider", _FakeFMUProvider)
    _FakeFMUProvider.created.clear()

    device = client.post("/devices", json={"device_instance": 2401, "name": "AHU-Test"}).json()
    fan_point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-output",
        "object_instance": 1,
        "name": "SF-Speed",
        "units": "percent",
    }).json()
    fan_output_point = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-input",
        "object_instance": 2,
        "name": "Fan-Speed-Model",
        "units": "percent",
    }).json()

    point_payload = simulation_router.SimulationModelPayload(
        name="AHU-Test Simple AHU",
        provider_type="fmu",
        model_type="SimpleAHU",
        enabled=True,
        created_from_device_id=device["id"],
        parameters={"input_sources": {"fan_command_pct": "point"}},
        mappings=[
            {"variable": "fan_command_pct", "direction": "input", "point_id": fan_point["id"]},
            {"variable": "fan_command_pct", "direction": "output", "point_id": fan_output_point["id"]},
        ],
    )
    point_payload.parameters = simulation_router._normalized_parameters(database, point_payload)
    model = create_simulation_model(
        database,
        name=point_payload.name,
        provider_type=point_payload.provider_type,
        model_type=point_payload.model_type,
        enabled=point_payload.enabled,
        parameters=point_payload.parameters,
        created_from_device_id=point_payload.created_from_device_id,
        mappings=simulation_router._mapping_dicts(point_payload),
    )

    constant_payload = simulation_router.SimulationModelPayload(
        name="AHU-Test Simple AHU",
        provider_type="fmu",
        model_type="SimpleAHU",
        enabled=True,
        created_from_device_id=device["id"],
        parameters={
            "input_sources": {"fan_command_pct": "constant"},
            "input_defaults": {"fan_command_pct": 80},
        },
        mappings=[
            # Simulates stale drawer state from the previous Point mapping.
            {"variable": "fan_command_pct", "direction": "input", "point_id": fan_point["id"]},
            {"variable": "fan_command_pct", "direction": "output", "point_id": fan_output_point["id"]},
        ],
    )
    constant_payload.parameters = simulation_router._normalized_parameters(database, constant_payload)
    updated = update_simulation_model(
        database,
        int(model["id"]),
        name=constant_payload.name,
        provider_type=constant_payload.provider_type,
        model_type=constant_payload.model_type,
        enabled=constant_payload.enabled,
        parameters=constant_payload.parameters,
        created_from_device_id=constant_payload.created_from_device_id,
        mappings=simulation_router._mapping_dicts(constant_payload),
    )
    assert updated is not None

    reloaded = get_simulation_model(database, int(model["id"]))
    assert reloaded is not None
    assert reloaded["parameters"]["input_sources"]["fan_command_pct"] == "constant"
    assert reloaded["parameters"]["input_defaults"]["fan_command_pct"] == 80
    assert [
        (m["variable"], m["direction"], m["point_id"])
        for m in reloaded["mappings"]
    ] == [("fan_command_pct", "output", fan_output_point["id"])]

    with database._conn() as conn:
        conn.execute(
            """
            INSERT INTO simulation_model_mappings
                (model_config_id, variable, direction, point_id)
            VALUES (?, ?, ?, ?)
            """,
            (int(model["id"]), "fan_command_pct", "input", fan_point["id"]),
        )
        conn.commit()

    engine = _FakeEngine()
    result = model_runtime.reconcile_enabled_models(database, engine)

    assert result["errors"] == []
    assert _FakeFMUProvider.created[-1].input_defaults["fan_command_pct"] == 80
    assert [
        (binding.variable, binding.direction, binding.point_id)
        for binding in _FakeFMUProvider.created[-1].bindings
    ] == [("fan_command_pct", "output", fan_output_point["id"])]
    registration = next(iter(engine.registrations.values()))
    assert registration["input_point_ids"] == set()
    assert registration["output_point_ids"] == {fan_output_point["id"]}
