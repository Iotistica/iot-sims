"""purge_unsupported_simulation_models / reconcile_enabled_models --
regression coverage for a real bug: a simulation_model_configs row whose
provider_type names a provider that's been removed from the codebase (e.g.
the local 'weather' provider, superseded by the FMU-based Weather.mo model
and deleted in 47a1a46) used to survive forever. Every restart,
reconcile_enabled_models would try to register_model_config() it, get
"Unsupported provider type: weather", and log the same failure again --
while the Simulation Model drawer kept hydrating from the same stale row
every time it was opened for that device. reconcile_enabled_models now
purges any config with an unsupported provider_type before attempting to
register anything, so the row (and its mappings, via ON DELETE CASCADE)
is gone after the first reconcile and the device falls back to built-in.
"""
from __future__ import annotations

from src.simulation.model_runtime import reconcile_enabled_models
from src.simulation.model_store import (
    create_simulation_model,
    get_active_simulation_models_by_device,
    list_simulation_models,
    purge_unsupported_simulation_models,
)


def _make_device(client, *, instance: int, name: str) -> dict:
    return client.post("/devices", json={"device_instance": instance, "name": name}).json()


def test_purge_removes_unsupported_provider_config(client, database):
    device = _make_device(client, instance=9601, name="Weather Station Purge Test")

    create_simulation_model(
        database,
        name=f"{device['name']} Weather",
        provider_type="weather",
        model_type="weather_can_on_toronto",
        enabled=True,
        parameters={},
        created_from_device_id=device["id"],
        mappings=[],
    )

    purged = purge_unsupported_simulation_models(database)

    assert len(purged) == 1
    assert purged[0]["provider_type"] == "weather"
    assert purged[0]["created_from_device_id"] == device["id"]
    assert list_simulation_models(database) == []


def test_purge_leaves_supported_and_legacy_system_providers_alone(client, database):
    device = _make_device(client, instance=9602, name="Purge Leaves FMU Alone")

    create_simulation_model(
        database,
        name=f"{device['name']} Lighting Zone",
        provider_type="fmu",
        model_type="LightingZone",
        enabled=True,
        parameters={},
        created_from_device_id=device["id"],
        mappings=[],
    )
    create_simulation_model(
        database,
        name="Legacy system row",
        provider_type="system",
        model_type="whatever",
        enabled=True,
        parameters={},
        created_from_device_id=None,
        mappings=[],
    )

    purged = purge_unsupported_simulation_models(database)

    assert purged == []
    assert len(list_simulation_models(database, include_legacy_system=True)) == 2


def test_reconcile_purges_unsupported_provider_and_no_longer_errors(client, database):
    device = _make_device(client, instance=9603, name="Weather Station Reconcile Test")

    create_simulation_model(
        database,
        name=f"{device['name']} Weather",
        provider_type="weather",
        model_type="weather_can_on_toronto",
        enabled=True,
        parameters={},
        created_from_device_id=device["id"],
        mappings=[],
    )

    class _FakeEngine:
        def get_simulation_providers(self):
            return {}

        def unregister_simulation_provider(self, runtime_id):
            return False

    result = reconcile_enabled_models(database, _FakeEngine())

    assert result["errors"] == []
    assert device["id"] not in get_active_simulation_models_by_device(database)
