"""
Behavioral/regression tests for the read-only BACnet client adapter
(src/bacnet/client/). Uses a configurable fake transport for most cases, plus
genuinely-constructed real bacpypes3 objects (bacpypes3.apdu.IAmRequest,
bacpypes3.primitivedata.ObjectIdentifier) where normalization needs to be
checked against the real shape rather than a guessed one -- both are
constructible standalone, no live network required.
"""
from __future__ import annotations

from typing import Any

import pytest
from bacpypes3.apdu import IAmRequest
from bacpypes3.basetypes import EngineeringUnits
from bacpypes3.pdu import Address as Bp3Address
from bacpypes3.primitivedata import ObjectIdentifier as Bp3ObjectIdentifier

from src.bacnet.client.adapter import BACnetAdapter
from src.bacnet.client.client import BACnetClient, ReadOnlyBACnetError
from src.bacnet.client.discovery import BACnetDiscovery
from src.bacnet.client.transport import Bacpypes3Transport
from src.bacnet.client.types import (
    BACnetAdapterConfig,
    BACnetDevice,
    BACnetObject,
    DiscoveredDevice,
    DiscoveryOptions,
    IAmDevice,
    ObjectIdentifier,
)


def make_real_i_am(
    device_instance: int,
    address: str,
    vendor_id: int = 999,
) -> IAmRequest:
    i_am = IAmRequest(
        iAmDeviceIdentifier=("device", device_instance),
        maxAPDULengthAccepted=1024,
        segmentationSupported="noSegmentation",
        vendorID=vendor_id,
    )
    i_am.pduSource = Bp3Address(address)
    return i_am


class FakeTransport:
    """
    Configurable BACnetTransport-shaped fake. Records every read_property
    call (keyed by address/object identifier/property) so tests can assert
    dedup, retry, and duplicate-read behavior precisely.
    """

    def __init__(
        self,
        *,
        who_is_devices: list[IAmDevice] | None = None,
        property_values: dict[tuple, Any] | None = None,
        fail_before_success: dict[tuple, int] | None = None,
        always_fail_keys: set | None = None,
    ) -> None:
        self._who_is_devices = who_is_devices if who_is_devices is not None else []
        self._property_values = property_values or {}
        self._fail_before_success = dict(fail_before_success or {})
        self._always_fail_keys = set(always_fail_keys or set())
        self.read_property_calls: list[tuple] = []

    async def who_is(self, *, address, low_limit, high_limit, timeout_ms):
        return list(self._who_is_devices)

    async def read_property(
        self,
        *,
        address,
        object_identifier: ObjectIdentifier,
        property_identifier,
        array_index=None,
        timeout_ms=5000,
    ):
        key = (address, object_identifier.type, object_identifier.instance, int(property_identifier))
        self.read_property_calls.append(key)

        if key in self._always_fail_keys:
            raise RuntimeError(f"simulated failure for {key}")

        remaining = self._fail_before_success.get(key, 0)
        if remaining > 0:
            self._fail_before_success[key] = remaining - 1
            raise RuntimeError(f"simulated transient failure for {key}")

        if key not in self._property_values:
            raise KeyError(f"FakeTransport has no configured value for {key}")
        return self._property_values[key]

    async def close(self):
        pass

    def call_count(self, key: tuple) -> int:
        return self.read_property_calls.count(key)


# ── 1. I-Am normalization (real bacpypes3.apdu.IAmRequest) ─────────────────

def test_normalize_i_am_real_shape():
    i_am = make_real_i_am(1001, "10.0.0.10", vendor_id=999)
    result = Bacpypes3Transport._normalize_i_am(i_am)
    assert result == IAmDevice(
        device_instance=1001,
        address="10.0.0.10",
        port=47808,
        vendor_id=999,
    )


def test_normalize_i_am_real_shape_with_port():
    i_am = make_real_i_am(2002, "10.0.0.20:47809", vendor_id=5)
    result = Bacpypes3Transport._normalize_i_am(i_am)
    assert result is not None
    assert result.device_instance == 2002
    assert result.address == "10.0.0.20"
    assert result.port == 47809
    assert result.vendor_id == 5


# ── 2. ObjectIdentifier normalization (real bacpypes3.primitivedata.ObjectIdentifier) ──

def test_normalize_object_list_real_object_identifiers():
    point = Bp3ObjectIdentifier(("analog-input", 1))
    device_obj = Bp3ObjectIdentifier(("device", 1001))

    refs = BACnetDiscovery._normalize_object_list([point, device_obj])

    assert refs == [
        ObjectIdentifier(type=0, instance=1),
        ObjectIdentifier(type=8, instance=1001),
    ]


# ── 3. Discovery dedup ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discover_dedups_by_device_instance():
    first = IAmDevice(device_instance=1001, address="10.0.0.10")
    duplicate = IAmDevice(device_instance=1001, address="10.0.0.99")

    transport = FakeTransport(
        who_is_devices=[first, duplicate],
        property_values={
            ("10.0.0.10", 8, 1001, 77): "AHU-1",
            ("10.0.0.10", 8, 1001, 121): "Acme",
            ("10.0.0.10", 8, 1001, 70): "Model-X",
            ("10.0.0.10", 8, 1001, 28): "Roof AHU",
        },
    )
    discovery = BACnetDiscovery(transport)

    devices = await discovery.discover(DiscoveryOptions())

    assert len(devices) == 1
    assert devices[0].device_instance == 1001
    assert devices[0].host == "10.0.0.10"  # first-seen reply kept, not the duplicate


# ── 4. Device metadata reads ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discover_reads_device_metadata():
    transport = FakeTransport(
        who_is_devices=[IAmDevice(device_instance=1001, address="10.0.0.10", vendor_id=42)],
        property_values={
            ("10.0.0.10", 8, 1001, 77): "AHU-1",
            ("10.0.0.10", 8, 1001, 121): "Acme Controls",
            ("10.0.0.10", 8, 1001, 70): "Model-X",
            ("10.0.0.10", 8, 1001, 28): "Roof AHU",
        },
    )
    discovery = BACnetDiscovery(transport)

    devices = await discovery.discover(DiscoveryOptions())

    assert len(devices) == 1
    metadata = devices[0].metadata
    assert metadata["objectName"] == "AHU-1"
    assert metadata["vendorName"] == "Acme Controls"
    assert metadata["modelName"] == "Model-X"
    assert metadata["description"] == "Roof AHU"
    assert devices[0].confidence == "high"


# ── 5. Object_List parsing (mixed, including an unsupported type) ──────────

@pytest.mark.asyncio
async def test_validate_skips_unsupported_object_types():
    device = DiscoveredDevice(
        name="ahu_1", fingerprint="f", host="10.0.0.10",
        port=47808, device_instance=1001,
    )
    trend_log = Bp3ObjectIdentifier(("trend-log", 1))  # not in the 10-type table
    analog_input = Bp3ObjectIdentifier(("analog-input", 1))

    transport = FakeTransport(
        property_values={
            ("10.0.0.10", 8, 1001, 76): [trend_log, analog_input],
            ("10.0.0.10", 0, 1, 77): "AHU-1.SAT",
            ("10.0.0.10", 0, 1, 85): 22.5,
            ("10.0.0.10", 0, 1, 117): int(EngineeringUnits("degrees-celsius")),
        },
    )
    discovery = BACnetDiscovery(transport)

    result = await discovery.validate(device)

    assert len(result["dataPoints"]) == 1
    assert result["dataPoints"][0]["objectType"] == "analog-input"


# ── 6/7/8. Present-Value reads per point kind ───────────────────────────────

@pytest.mark.asyncio
async def test_validate_reads_analog_binary_multistate_present_values():
    device = DiscoveredDevice(
        name="ahu_1", fingerprint="f", host="10.0.0.10",
        port=47808, device_instance=1001,
    )
    refs = [
        Bp3ObjectIdentifier(("analog-input", 1)),
        Bp3ObjectIdentifier(("binary-input", 2)),
        Bp3ObjectIdentifier(("multi-state-input", 3)),
    ]

    transport = FakeTransport(
        property_values={
            ("10.0.0.10", 8, 1001, 76): refs,
            ("10.0.0.10", 0, 1, 77): "AI-1",
            ("10.0.0.10", 0, 1, 85): 22.5,
            ("10.0.0.10", 0, 1, 117): int(EngineeringUnits("degrees-celsius")),
            ("10.0.0.10", 3, 2, 77): "BI-2",
            ("10.0.0.10", 3, 2, 85): 1,
            ("10.0.0.10", 13, 3, 77): "MSI-3",
            ("10.0.0.10", 13, 3, 85): 2,
        },
    )
    discovery = BACnetDiscovery(transport)

    result = await discovery.validate(device)

    by_type = {p["objectType"]: p for p in result["dataPoints"]}
    assert by_type["analog-input"]["presentValue"] == 22.5
    assert by_type["binary-input"]["presentValue"] == 1
    assert by_type["multi-state-input"]["presentValue"] == 2


# ── 9/10. Units normalization ───────────────────────────────────────────────

def test_unit_curated_symbols():
    assert BACnetDiscovery._unit(int(EngineeringUnits("degrees-celsius"))) == "°C"
    assert BACnetDiscovery._unit(int(EngineeringUnits("kilowatts"))) == "kW"


def test_unit_humanized_fallback_for_uncurated_code():
    code = int(EngineeringUnits("cubic-meters-per-hour"))
    assert BACnetDiscovery._unit(code) == "cubic meters per hour"


def test_unit_no_units_and_unknown_code():
    assert BACnetDiscovery._unit(int(EngineeringUnits("no-units"))) is None
    assert BACnetDiscovery._unit(None) is None
    assert BACnetDiscovery._unit(5000) == "bacnet-unit-5000"


# ── 11. One failed point does not fail the whole device validation ─────────

@pytest.mark.asyncio
async def test_validate_one_failed_point_does_not_fail_device():
    device = DiscoveredDevice(
        name="ahu_1", fingerprint="f", host="10.0.0.10",
        port=47808, device_instance=1001,
    )
    refs = [
        Bp3ObjectIdentifier(("analog-input", 1)),
        Bp3ObjectIdentifier(("analog-input", 2)),
    ]

    transport = FakeTransport(
        property_values={
            ("10.0.0.10", 8, 1001, 76): refs,
            ("10.0.0.10", 0, 1, 77): "AI-1",
            ("10.0.0.10", 0, 1, 85): 22.5,
            ("10.0.0.10", 0, 1, 117): int(EngineeringUnits("degrees-celsius")),
            # AI-2's object name read always fails -- point 2 should be
            # dropped, not the whole validate() call.
        },
        always_fail_keys={("10.0.0.10", 0, 2, 77)},
    )
    discovery = BACnetDiscovery(transport)

    result = await discovery.validate(device)

    assert len(result["dataPoints"]) == 1
    assert result["dataPoints"][0]["objectInstance"] == 1


# ── 12. Retry behavior ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_object_retries_then_succeeds():
    obj = BACnetObject(name="sat", object_type="analog-input", object_instance=1)
    device = BACnetDevice(
        name="ahu_1", ip_address="10.0.0.10", device_instance=1001,
        retry_attempts=2, retry_delay_ms=1,
    )
    key = ("10.0.0.10", 0, 1, 85)
    transport = FakeTransport(
        who_is_devices=[IAmDevice(device_instance=1001, address="10.0.0.10")],
        property_values={key: 22.5},
        fail_before_success={key: 2},
    )
    client = BACnetClient(device, transport)
    await client.connect()

    result = await client.read_object(obj)

    assert result.quality == "GOOD"
    assert result.value == 22.5
    assert transport.call_count(key) == 3  # 2 failures + 1 success


@pytest.mark.asyncio
async def test_read_object_exhausts_retries_and_returns_bad():
    obj = BACnetObject(name="sat", object_type="analog-input", object_instance=1)
    device = BACnetDevice(
        name="ahu_1", ip_address="10.0.0.10", device_instance=1001,
        retry_attempts=1, retry_delay_ms=1,
    )
    key = ("10.0.0.10", 0, 1, 85)
    transport = FakeTransport(
        who_is_devices=[IAmDevice(device_instance=1001, address="10.0.0.10")],
        always_fail_keys={key},
    )
    client = BACnetClient(device, transport)
    await client.connect()

    result = await client.read_object(obj)

    assert result.quality == "BAD"
    assert result.error is not None
    assert transport.call_count(key) == 2  # 1 initial attempt + 1 retry


# ── 13. Duplicate Present-Value reads are not performed (Phase D regression) ──

@pytest.mark.asyncio
async def test_validate_reads_present_value_exactly_once_per_point():
    device = DiscoveredDevice(
        name="ahu_1", fingerprint="f", host="10.0.0.10",
        port=47808, device_instance=1001,
    )
    refs = [Bp3ObjectIdentifier(("analog-input", 1))]

    transport = FakeTransport(
        property_values={
            ("10.0.0.10", 8, 1001, 76): refs,
            ("10.0.0.10", 0, 1, 77): "AI-1",
            ("10.0.0.10", 0, 1, 85): 22.5,
            ("10.0.0.10", 0, 1, 117): int(EngineeringUnits("degrees-celsius")),
        },
    )
    discovery = BACnetDiscovery(transport)

    result = await discovery.validate(device)

    present_value_key = ("10.0.0.10", 0, 1, 85)
    assert transport.call_count(present_value_key) == 1
    assert result["dataPoints"][0]["presentValue"] == 22.5


# ── 14. Source commandability detection ─────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_detects_source_commandability():
    device = DiscoveredDevice(
        name="ahu_1", fingerprint="f", host="10.0.0.10",
        port=47808, device_instance=1001,
    )
    refs = [
        Bp3ObjectIdentifier(("analog-output", 1)),  # priority array read succeeds
        Bp3ObjectIdentifier(("analog-output", 2)),  # priority array read fails
    ]

    transport = FakeTransport(
        property_values={
            ("10.0.0.10", 8, 1001, 76): refs,
            ("10.0.0.10", 1, 1, 77): "AO-1",
            ("10.0.0.10", 1, 1, 85): 50.0,
            ("10.0.0.10", 1, 1, 117): int(EngineeringUnits("percent")),
            ("10.0.0.10", 1, 1, 87): [None] * 16,
            ("10.0.0.10", 1, 2, 77): "AO-2",
            ("10.0.0.10", 1, 2, 85): 50.0,
            ("10.0.0.10", 1, 2, 117): int(EngineeringUnits("percent")),
        },
        always_fail_keys={("10.0.0.10", 1, 2, 87)},
    )
    discovery = BACnetDiscovery(transport)

    result = await discovery.validate(device)

    by_instance = {p["objectInstance"]: p for p in result["dataPoints"]}
    assert by_instance[1]["sourceCommandable"] is True
    assert by_instance[2]["sourceCommandable"] is False


# ── 15. Client write() and write_property() both fail closed ───────────────

@pytest.mark.asyncio
async def test_client_read_works_but_writes_are_blocked():
    device = BACnetDevice(
        name="ahu_1", ip_address="10.0.0.10", device_instance=1001,
        objects=[
            BACnetObject(
                name="sat", object_name="AHU-1.SAT",
                object_type="analog-input", object_instance=1, unit="°C",
            )
        ],
    )
    key = ("10.0.0.10", 0, 1, 85)
    transport = FakeTransport(
        who_is_devices=[IAmDevice(device_instance=1001, address="10.0.0.10")],
        property_values={key: 22.5},
    )
    client = BACnetClient(device, transport)
    await client.connect()

    result = await client.read_object(device.objects[0])
    assert result.quality == "GOOD"
    assert result.value == 22.5

    with pytest.raises(ReadOnlyBACnetError):
        await client.write_property("sat", 18.0)

    with pytest.raises(ReadOnlyBACnetError):
        await client.write("sat", 18.0)


# ── 16. Adapter write_property() fails closed ───────────────────────────────

@pytest.mark.asyncio
async def test_adapter_write_property_is_blocked():
    adapter = BACnetAdapter(BACnetAdapterConfig(devices=[]), FakeTransport())

    with pytest.raises(ReadOnlyBACnetError):
        await adapter.write_property("any_device", "any_point", 1.0)


# ── 17. Adapter start()/stop() leaves no polling task running ──────────────

@pytest.mark.asyncio
async def test_adapter_stop_leaves_no_poll_task_running():
    adapter = BACnetAdapter(BACnetAdapterConfig(devices=[]), FakeTransport())

    await adapter.start()
    assert adapter._poll_task is not None
    assert not adapter._poll_task.done()

    await adapter.stop()
    assert adapter._poll_task is None
    assert adapter.running is False
