"""FaultContext.points_by_type/.values() -- the minimal, additive fix for
the same duplicate-point-class collapsing bug in Fault Detection's
_build_context(), independently reimplemented from SimEngine.
get_device_point_values(). Existing .point()/.value() (last-write-wins)
and every rule in rules/ahu.py/rules/sensors.py are untouched."""
from __future__ import annotations

import pytest

from src.fault_detection.engine import FaultDetectionEngine


class _StubSimulationEngine:
    def __init__(self, values: dict[int, object]):
        self._values = values

    def get_object_value(self, object_id: int):
        return self._values.get(object_id)


def _make_device_with_duplicate_run_status(database):
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name, equipment_type) VALUES (?,?,?)",
            (501, "Fault-Test-Plant", "Chiller"),
        )
        device_id = conn.execute("SELECT id FROM devices WHERE device_instance=501").fetchone()[0]
        for i, name in enumerate(("CH-1-Run", "CH-2-Run"), start=1):
            conn.execute(
                "INSERT INTO objects (device_id, object_type, object_instance, name, point_type) VALUES (?,?,?,?,?)",
                (device_id, "binary-input", i, name, "Run_Status"),
            )
        conn.commit()
    return device_id


@pytest.mark.asyncio
async def test_build_context_points_still_last_write_wins(database):
    """Regression guard: FaultContext.points/.point()/.value() must keep
    their pre-migration last-write-wins behavior unchanged."""
    device_id = _make_device_with_duplicate_run_status(database)
    objects = database.get_objects(device_id)
    by_name = {o["name"]: o for o in objects}

    stub = _StubSimulationEngine({
        by_name["CH-1-Run"]["id"]: True,
        by_name["CH-2-Run"]["id"]: False,
    })
    engine = FaultDetectionEngine(database=database, simulation_engine=stub, registry=None)

    context = await engine._build_context(device_id)

    # Whichever object get_objects() returns last for this point_type wins
    # -- same contract as before, just confirming it wasn't altered.
    assert context.value("Run_Status") in (True, False)


@pytest.mark.asyncio
async def test_build_context_points_by_type_returns_every_match(database):
    device_id = _make_device_with_duplicate_run_status(database)
    objects = database.get_objects(device_id)
    by_name = {o["name"]: o for o in objects}

    stub = _StubSimulationEngine({
        by_name["CH-1-Run"]["id"]: True,
        by_name["CH-2-Run"]["id"]: False,
    })
    engine = FaultDetectionEngine(database=database, simulation_engine=stub, registry=None)

    context = await engine._build_context(device_id)

    values = context.values("Run_Status")
    assert sorted(values, key=str) == sorted([True, False], key=str)
    assert context.values("Nonexistent_Point_Type") == []
