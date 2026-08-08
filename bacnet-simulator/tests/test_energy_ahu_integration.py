"""End-to-end integration test through the real EnergyEngine.evaluate_ahu()
async method (not just the isolated resolver/context functions) -- proves
the semantic_resolver wiring in src/energy/engine.py actually produces
distinct supply_fan_power_kw/return_fan_power_kw for the seeded AHU-1,
where they'd previously both have been derived from the same collapsed
Fan_Status/Fan_Speed_Command dict value."""
from __future__ import annotations

import json

import pytest

from src.energy.engine import EnergyEngine


class _StubSimulationEngine:
    def __init__(self, values: dict[int, object]):
        self._values = values

    def get_object_value(self, object_id: int):
        return self._values.get(object_id)

    def get_device_point_values(self, objects: list[dict]) -> dict[str, object]:
        # Mirrors SimEngine.get_device_point_values() exactly (last write
        # wins) -- evaluate_ahu() still calls this for the legacy flat
        # dict alongside the new semantic_resolver.resolve_ahu_fans() path.
        values: dict[str, object] = {}
        for obj in objects:
            point_type = obj.get("point_type")
            if point_type:
                values[str(point_type)] = self.get_object_value(obj["id"])
        return values


@pytest.mark.asyncio
async def test_evaluate_ahu_produces_distinct_fan_power(seeded_database):
    devices = seeded_database.get_devices()
    ahu1_id = next(d["id"] for d in devices if d["device_instance"] == 1003)
    objects = seeded_database.get_objects(ahu1_id)
    by_name = {o["name"]: o for o in objects}

    stub = _StubSimulationEngine({
        by_name["SF-Run"]["id"]: True,
        by_name["SF-Speed"]["id"]: 100.0,
        by_name["RF-Run"]["id"]: True,
        by_name["RF-Speed"]["id"]: 25.0,
    })

    engine = EnergyEngine(database=seeded_database, simulation_engine=stub)

    parameters = json.dumps({
        "supply_fan_rated_power_kw": 15.0,
        "return_fan_rated_power_kw": 10.0,
    })

    result = await engine.evaluate_ahu(
        device_id=ahu1_id,
        parameters_json=parameters,
        elapsed_seconds=60.0,
    )

    assert result["supply_fan_power_kw"] is not None
    assert result["return_fan_power_kw"] is not None
    # Supply fan running at 100% vs return fan at 25% on different rated
    # power -- must NOT be equal (would indicate both are still reading
    # off the same collapsed flat-dict value).
    assert result["supply_fan_power_kw"] != result["return_fan_power_kw"]
    assert result["supply_fan_power_kw"] > result["return_fan_power_kw"]
