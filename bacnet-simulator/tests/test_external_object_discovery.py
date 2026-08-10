"""Integration-level tests for the discover-then-persist pipeline
src/api/routers/external_objects.py's /discover route runs: BACnetDiscovery.
validate() (against a FakeTransport, no real socket needed) followed by
Database.sync_external_objects(). The route itself also needs a real
bacpypes3 Application (via _discovery_session()) to reach a real device --
that layer is thin glue already covered by the live Docker acceptance test;
this file exercises the actual discovery+persistence logic exactly as the
route composes it."""
from __future__ import annotations

import pytest
from bacpypes3.basetypes import EngineeringUnits
from bacpypes3.primitivedata import ObjectIdentifier as Bp3ObjectIdentifier

from src.bacnet.client.discovery import BACnetDiscovery
from src.bacnet.client.types import DiscoveredDevice

from tests.test_bacnet_readonly_client import FakeTransport


def _points_from_validate_result(result: dict) -> list[dict]:
    """Mirrors external_objects.py's discover_external_objects() point
    extraction exactly."""
    return [
        {
            "object_type": p["objectType"],
            "object_instance": p["objectInstance"],
            "name": p["objectName"] or f"{p['objectType']}_{p['objectInstance']}",
            "units": p["unit"] or "no-units",
            "description": p.get("description"),
        }
        for p in result["dataPoints"]
    ]


@pytest.mark.asyncio
async def test_discovered_objects_persist_associated_with_correct_device(database):
    devices = database.sync_external_devices([
        {"device_instance": 1001, "name": "ahu", "host": "10.0.0.10", "port": 47808, "metadata": {}},
    ])
    device_id = devices[0]["id"]

    transport = FakeTransport(
        property_values={
            ("10.0.0.10", 8, 1001, 76): [Bp3ObjectIdentifier(("analog-input", 1))],
            ("10.0.0.10", 0, 1, 77): "SAT",
            ("10.0.0.10", 0, 1, 85): 22.5,
            ("10.0.0.10", 0, 1, 117): int(EngineeringUnits("degrees-celsius")),
        },
    )
    discovery = BACnetDiscovery(transport)
    result = await discovery.validate(DiscoveredDevice(
        name="ahu", fingerprint="", host="10.0.0.10", port=47808, device_instance=1001,
    ))

    persisted = database.sync_external_objects(device_id, _points_from_validate_result(result))

    assert len(persisted) == 1
    assert persisted[0]["device_id"] == device_id
    assert persisted[0]["name"] == "SAT"
    assert persisted[0]["units"] == "°C"


@pytest.mark.asyncio
async def test_second_discover_upserts_not_duplicates(database):
    devices = database.sync_external_devices([
        {"device_instance": 1001, "name": "ahu", "host": "10.0.0.10", "port": 47808, "metadata": {}},
    ])
    device_id = devices[0]["id"]

    def make_transport(name: str) -> FakeTransport:
        return FakeTransport(
            property_values={
                ("10.0.0.10", 8, 1001, 76): [Bp3ObjectIdentifier(("analog-input", 1))],
                ("10.0.0.10", 0, 1, 77): name,
                ("10.0.0.10", 0, 1, 85): 22.5,
                ("10.0.0.10", 0, 1, 117): int(EngineeringUnits("degrees-celsius")),
            },
        )

    device = DiscoveredDevice(name="ahu", fingerprint="", host="10.0.0.10", port=47808, device_instance=1001)

    result1 = await BACnetDiscovery(make_transport("SAT")).validate(device)
    first = database.sync_external_objects(device_id, _points_from_validate_result(result1))

    result2 = await BACnetDiscovery(make_transport("SAT-Renamed")).validate(device)
    second = database.sync_external_objects(device_id, _points_from_validate_result(result2))

    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["name"] == "SAT-Renamed"


@pytest.mark.asyncio
async def test_object_with_failed_optional_property_still_persists(database):
    """A point whose Units read fails must still appear in the persisted
    inventory -- only Object_Name failures (structural) drop an object."""
    devices = database.sync_external_devices([
        {"device_instance": 1001, "name": "ahu", "host": "10.0.0.10", "port": 47808, "metadata": {}},
    ])
    device_id = devices[0]["id"]

    transport = FakeTransport(
        property_values={
            ("10.0.0.10", 8, 1001, 76): [Bp3ObjectIdentifier(("analog-input", 1))],
            ("10.0.0.10", 0, 1, 77): "SAT",
            ("10.0.0.10", 0, 1, 85): 22.5,
            # No Units (117) configured -- read fails, must degrade to
            # "no-units" (via unit=None -> the route's `or "no-units"`
            # fallback), not drop the object.
        },
    )
    discovery = BACnetDiscovery(transport)
    result = await discovery.validate(DiscoveredDevice(
        name="ahu", fingerprint="", host="10.0.0.10", port=47808, device_instance=1001,
    ))

    persisted = database.sync_external_objects(device_id, _points_from_validate_result(result))

    assert len(persisted) == 1
    assert persisted[0]["units"] == "no-units"


@pytest.mark.asyncio
async def test_refresh_only_updates_present_value_not_persisted_row(database):
    devices = database.sync_external_devices([
        {"device_instance": 1001, "name": "ahu", "host": "10.0.0.10", "port": 47808, "metadata": {}},
    ])
    device_id = devices[0]["id"]
    persisted = database.sync_external_objects(device_id, [
        {"object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius"},
    ])
    before = database.get_objects(device_id)

    transport = FakeTransport(property_values={("10.0.0.10", 0, 1, 85): 23.9})
    discovery = BACnetDiscovery(transport)
    values = await discovery.read_present_values("10.0.0.10", 1001, [("analog-input", 1)])

    after = database.get_objects(device_id)
    # The persisted row itself is byte-for-byte unchanged by a refresh --
    # only the transient response (values dict) carries the new reading.
    assert before == after
    assert values[("analog-input", 1)] == 23.9
