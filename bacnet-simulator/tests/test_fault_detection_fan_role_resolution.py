"""FaultDetectionEngine._resolve_fan_role_points() (src/fault_detection/
engine.py): Supply_Fan_Command/Supply_Fan_Status are FDD-internal role
keys, not Brick classes, and must resolve via the same Supply_Fan/
Return_Fan sub-equipment (isPartOf) + Fan_Speed_Command/Fan_Command/
Fan_Status (isPointOf) relationship walk src/semantics/resolver.py's
resolve_ahu_fans() already does for the Energy Engine -- not a flat
point_type dict lookup, which would nondeterministically collide between
two fans sharing the same generic point_type on one device.

Uses the Rooftop_Unit equipment type specifically (not Air_Handling_Unit)
since get_equipment_entity()/get_sub_equipment() must work for any
equipment class with fan sub-equipment, not just AHUs -- this is exactly
the case the RTU FDD needs.
"""
from __future__ import annotations

import pytest

from src.fault_detection.engine import FaultDetectionEngine


class _StubSimulationEngine:
    def __init__(self, values: dict[int, object]):
        self._values = values

    def get_object_value(self, object_id: int):
        return self._values.get(object_id)


def _make_rtu_with_supply_fan(database):
    """Reuses the same legacy-alias-tagged fixture shape
    test_ahu_fan_alias_migration.py's _make_legacy_tagged_ahu() uses --
    database.setup() migrates the aliases and builds the real Supply_Fan
    sub-equipment + isPartOf + isPointOf relationships, giving a
    realistic semantic graph without hand-writing it."""
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name, equipment_type) VALUES (?,?,?)",
            (961, "RTU-Fan-Test", "Rooftop_Unit"),
        )
        device_id = conn.execute("SELECT id FROM devices WHERE device_instance=961").fetchone()[0]
        conn.execute(
            "INSERT INTO objects (device_id, object_type, object_instance, name, point_type) VALUES (?,?,?,?,?)",
            (device_id, "binary-input", 1, "SF-Run", "Supply_Fan_Status"),
        )
        conn.execute(
            "INSERT INTO objects (device_id, object_type, object_instance, name, point_type) VALUES (?,?,?,?,?)",
            (device_id, "analog-output", 2, "SF-Cmd", "Supply_Fan_Command"),
        )
        conn.commit()
    return device_id


@pytest.mark.asyncio
async def test_supply_fan_command_and_status_resolve_for_rtu(database):
    device_id = _make_rtu_with_supply_fan(database)
    database.setup()  # runs migrate_ahu_fan_aliases() -- builds Supply_Fan sub-equipment

    objects = {o["name"]: o for o in database.get_objects(device_id)}
    stub = _StubSimulationEngine({
        objects["SF-Run"]["id"]: True,
        objects["SF-Cmd"]["id"]: 0.72,
    })
    engine = FaultDetectionEngine(database=database, simulation_engine=stub, registry=None)

    context = await engine._build_context(device_id)

    assert context.value("Supply_Fan_Status") is True
    assert context.value("Supply_Fan_Command") == pytest.approx(0.72)


@pytest.mark.asyncio
async def test_fan_role_resolution_is_noop_without_sub_equipment(database):
    """A device with no Supply_Fan/Return_Fan sub-equipment (e.g. no
    equipment_type set, so migrate_ahu_fan_aliases() has nothing to
    attach to) must not raise and must simply leave the roles unresolved
    -- context.value() returns None, not a crash."""
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name) VALUES (?,?)",
            (962, "Plain-Device"),
        )
        device_id = conn.execute("SELECT id FROM devices WHERE device_instance=962").fetchone()[0]
        conn.commit()

    engine = FaultDetectionEngine(database=database, simulation_engine=_StubSimulationEngine({}), registry=None)
    context = await engine._build_context(device_id)

    assert context.value("Supply_Fan_Status") is None
    assert context.value("Supply_Fan_Command") is None


@pytest.mark.asyncio
async def test_two_fans_sharing_generic_point_type_no_longer_collide(database):
    """The bug this fix actually closes: Supply_Fan's and Return_Fan's
    Fan_Status points share the identical generic point_type -- the flat
    points[]/points_by_type[] dict (last-write-wins across ALL objects on
    the device, order-dependent) would silently return whichever fan's
    object the DB happened to return last for bare "Fan_Status". The
    Supply_Fan_Status role key must always resolve to the SUPPLY fan's
    value specifically, regardless of insertion/iteration order."""
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name, equipment_type) VALUES (?,?,?)",
            (963, "RTU-Two-Fans", "Rooftop_Unit"),
        )
        device_id = conn.execute("SELECT id FROM devices WHERE device_instance=963").fetchone()[0]
        rows = [
            (device_id, "binary-input", 1, "SF-Run", "Supply_Fan_Status"),
            (device_id, "binary-input", 2, "RF-Run", "Return_Fan_Status"),
        ]
        conn.executemany(
            "INSERT INTO objects (device_id, object_type, object_instance, name, point_type) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
    database.setup()

    objects = {o["name"]: o for o in database.get_objects(device_id)}
    stub = _StubSimulationEngine({
        objects["SF-Run"]["id"]: True,
        objects["RF-Run"]["id"]: False,
    })
    engine = FaultDetectionEngine(database=database, simulation_engine=stub, registry=None)

    context = await engine._build_context(device_id)

    assert context.value("Supply_Fan_Status") is True
    assert context.value("Return_Fan_Status") is False
