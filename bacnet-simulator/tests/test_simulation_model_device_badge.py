"""get_active_simulation_models_by_device / get_devices_with_disabled_simulation_model
-- the device-list header badge (green "Sim" tag + provider-colored tag)
is driven by these. Regression coverage for a real bug: they used to
require an output mapping to exist before a device counted as having an
active/stopped model at all, so a device with an enabled FMU model but no
mapped outputs yet (e.g. right after creating the model, before Point
Mapping) silently showed no badge. Now keyed by created_from_device_id --
the device a model was configured for -- not by whether any of its
outputs happen to be mapped to a point.
"""
from __future__ import annotations

from src.simulation.models.store import (
    create_simulation_model,
    get_active_simulation_models_by_device,
    get_devices_with_disabled_simulation_model,
)


def _make_device(client, *, instance: int, name: str) -> dict:
    return client.post("/devices", json={"device_instance": instance, "name": name}).json()


def test_active_model_shown_without_output_mappings(client, database):
    device = _make_device(client, instance=9501, name="Lighting Test A")

    create_simulation_model(
        database,
        name="Lighting Test A Lighting Zone",
        provider_type="fmu",
        model_type="LightingZone",
        enabled=True,
        parameters={},
        created_from_device_id=device["id"],
        mappings=[],
    )

    active = get_active_simulation_models_by_device(database)
    assert device["id"] in active
    assert active[device["id"]]["provider_type"] == "fmu"

    stopped = get_devices_with_disabled_simulation_model(database)
    assert device["id"] not in stopped


def test_disabled_model_shown_as_stopped_without_output_mappings(client, database):
    device = _make_device(client, instance=9502, name="Lighting Test B")

    create_simulation_model(
        database,
        name="Lighting Test B Lighting Zone",
        provider_type="fmu",
        model_type="LightingZone",
        enabled=False,
        parameters={},
        created_from_device_id=device["id"],
        mappings=[],
    )

    active = get_active_simulation_models_by_device(database)
    assert device["id"] not in active

    stopped = get_devices_with_disabled_simulation_model(database)
    assert device["id"] in stopped


def test_device_with_no_simulation_model_shows_neither_badge(client, database):
    device = _make_device(client, instance=9503, name="Lighting Test C")

    active = get_active_simulation_models_by_device(database)
    stopped = get_devices_with_disabled_simulation_model(database)
    assert device["id"] not in active
    assert device["id"] not in stopped
