"""
Behavioral/regression tests for the read-only BACnet client adapter
(src/bacnet/client/). Uses a configurable fake transport for most cases, plus
genuinely-constructed real bacpypes3 objects (bacpypes3.apdu.IAmRequest,
bacpypes3.primitivedata.ObjectIdentifier) where normalization needs to be
checked against the real shape rather than a guessed one -- both are
constructible standalone, no live network required.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from bacpypes3.apdu import ErrorRejectAbortNack, IAmRequest
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


# ── Bacpypes3Transport.read_property() must pass a real ObjectIdentifier ───
# (regression: found via live testing against a real simulator instance --
# Application.read_property() indexes objid[0] directly to look up the
# object class for decoding and does NOT coerce a "type,instance" string
# first. A plain string silently returned its first character there instead
# of raising, so every live read "succeeded" but decoded to the literal
# fallback "-no object class-".)

class _CapturingApplication:
    def __init__(self, return_value: Any = "ok") -> None:
        self.calls: list[tuple] = []
        self._return_value = return_value

    async def read_property(self, address, objid, prop, array_index=None):
        self.calls.append((address, objid, prop, array_index))
        return self._return_value


@pytest.mark.asyncio
async def test_read_property_passes_real_object_identifier_not_string():
    app = _CapturingApplication()
    transport = Bacpypes3Transport(app)

    await transport.read_property(
        address="10.0.0.10",
        object_identifier=ObjectIdentifier(type=8, instance=1000),
        property_identifier=77,
    )

    assert len(app.calls) == 1
    _address, objid, _prop, _array_index = app.calls[0]
    assert isinstance(objid, Bp3ObjectIdentifier)
    assert int(objid[0]) == 8  # device
    assert objid[1] == 1000


# ── Bacpypes3Transport.read_property() must not let BaseException-rooted ───
# BACnet protocol errors escape as-is (regression: found via live testing --
# bacpypes3.apdu.ErrorRejectAbortNack subclasses BaseException, not
# Exception, so a completely normal real-device response like "this object
# doesn't support Priority_Array" would otherwise crash every caller's plain
# `except Exception` handling instead of being treated as one failed read.)

class _FakeProtocolError(ErrorRejectAbortNack):
    def __str__(self) -> str:
        return "simulated-protocol-error"


class _RaisingApplication:
    async def read_property(self, address, objid, prop, array_index=None):
        raise _FakeProtocolError("simulated")


class _ReturningApplication:
    async def read_property(self, address, objid, prop, array_index=None):
        return _FakeProtocolError("simulated")


@pytest.mark.asyncio
async def test_read_property_converts_raised_protocol_error_to_exception():
    transport = Bacpypes3Transport(_RaisingApplication())

    with pytest.raises(Exception) as exc_info:
        await transport.read_property(
            address="10.0.0.10",
            object_identifier=ObjectIdentifier(type=0, instance=1),
            property_identifier=85,
        )
    assert not isinstance(exc_info.value, ErrorRejectAbortNack)


@pytest.mark.asyncio
async def test_read_property_converts_returned_protocol_error_to_exception():
    transport = Bacpypes3Transport(_ReturningApplication())

    with pytest.raises(Exception) as exc_info:
        await transport.read_property(
            address="10.0.0.10",
            object_identifier=ObjectIdentifier(type=0, instance=1),
            property_identifier=85,
        )
    assert not isinstance(exc_info.value, ErrorRejectAbortNack)


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


# ── 11b. Present-Value failure degrades to None, doesn't drop the object ───
# (regression target: unlike Units/Priority_Array, Present-Value used to be
# unguarded and a failed read dropped the whole object from the scan --
# real BACnet devices vary considerably, this must degrade gracefully too.)

@pytest.mark.asyncio
async def test_validate_present_value_failure_does_not_drop_object():
    device = DiscoveredDevice(
        name="ahu_1", fingerprint="f", host="10.0.0.10",
        port=47808, device_instance=1001,
    )
    refs = [Bp3ObjectIdentifier(("analog-input", 1))]

    transport = FakeTransport(
        property_values={
            ("10.0.0.10", 8, 1001, 76): refs,
            ("10.0.0.10", 0, 1, 77): "AI-1",
            ("10.0.0.10", 0, 1, 117): int(EngineeringUnits("degrees-celsius")),
            # Present-Value (85) deliberately has no configured value --
            # FakeTransport raises KeyError for it, simulating a real read
            # failure.
        },
    )
    discovery = BACnetDiscovery(transport)

    result = await discovery.validate(device)

    assert len(result["dataPoints"]) == 1
    assert result["dataPoints"][0]["objectInstance"] == 1
    assert result["dataPoints"][0]["presentValue"] is None


# ── 11c. Description is read best-effort per object ─────────────────────────

@pytest.mark.asyncio
async def test_validate_reads_description_when_available():
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
            ("10.0.0.10", 0, 1, 28): "Supply air temperature",
        },
    )
    discovery = BACnetDiscovery(transport)

    result = await discovery.validate(device)

    assert result["dataPoints"][0]["description"] == "Supply air temperature"


@pytest.mark.asyncio
async def test_validate_missing_description_does_not_fail_object():
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
            # No 28 (DESCRIPTION) configured -- FakeTransport raises for it.
        },
    )
    discovery = BACnetDiscovery(transport)

    result = await discovery.validate(device)

    assert len(result["dataPoints"]) == 1
    assert result["dataPoints"][0]["description"] is None


# ── 11d. read_present_values(): lighter Refresh path, skips Object_List ────

@pytest.mark.asyncio
async def test_read_present_values_returns_values_for_known_points():
    transport = FakeTransport(
        property_values={
            ("10.0.0.10", 0, 1, 85): 22.5,
            ("10.0.0.10", 3, 2, 85): 1,
        },
    )
    discovery = BACnetDiscovery(transport)

    values = await discovery.read_present_values(
        "10.0.0.10", 1001, [("analog-input", 1), ("binary-input", 2)],
    )

    assert values[("analog-input", 1)] == 22.5
    assert values[("binary-input", 2)] == 1


@pytest.mark.asyncio
async def test_read_present_values_does_not_read_object_list():
    transport = FakeTransport(
        property_values={("10.0.0.10", 0, 1, 85): 22.5},
    )
    discovery = BACnetDiscovery(transport)

    await discovery.read_present_values("10.0.0.10", 1001, [("analog-input", 1)])

    object_list_key = ("10.0.0.10", 8, 1001, 76)
    assert transport.call_count(object_list_key) == 0


@pytest.mark.asyncio
async def test_read_present_values_degrades_failed_point_to_none():
    transport = FakeTransport(
        property_values={("10.0.0.10", 0, 1, 85): 22.5},
        always_fail_keys={("10.0.0.10", 0, 2, 85)},
    )
    discovery = BACnetDiscovery(transport)

    values = await discovery.read_present_values(
        "10.0.0.10", 1001, [("analog-input", 1), ("analog-input", 2)],
    )

    assert values[("analog-input", 1)] == 22.5
    assert values[("analog-input", 2)] is None


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


# ── 18. Targeted (unicast) Who-Is discovers all devices behind one host ────
#
# Regression coverage for: bacpypes3's own WhoIsFuture.only_one is True
# whenever an explicit address is given to Application.who_is(), so it stops
# collecting after the first I-Am -- the wrong tool for "discover every
# device behind this one IP." Bacpypes3Transport._collect_i_ams() sidesteps
# this entirely via a listener instead of WhoIsFuture. Two fake Application
# shapes are exercised: a bare one (no native listener support, mimicking a
# third-party Application -- exercises _ensure_i_am_listener_support's
# one-time polyfill) and a native one (mimicking SimApplication, which
# defines add_i_am_listener()/remove_i_am_listener() natively).

class _BareApplication:
    """Mimics a bare third-party bacpypes3 Application: only do_IAmRequest
    and request(), no native listener support."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.do_i_am_calls = 0

    async def do_IAmRequest(self, apdu) -> None:
        self.do_i_am_calls += 1

    def request(self, pdu) -> None:
        self.requests.append(pdu)


class _NativeListenerApplication:
    """Mimics SimApplication: already exposes native
    add_i_am_listener()/remove_i_am_listener(), so _ensure_i_am_listener_support
    must short-circuit without touching do_IAmRequest at all."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self._i_am_listeners: list[Any] = []

    def add_i_am_listener(self, listener) -> None:
        self._i_am_listeners.append(listener)

    def remove_i_am_listener(self, listener) -> None:
        if listener in self._i_am_listeners:
            self._i_am_listeners.remove(listener)

    def request(self, pdu) -> None:
        self.requests.append(pdu)

    def fire(self, apdu) -> None:
        for listener in list(self._i_am_listeners):
            listener(apdu)


class _FakeRoutedSource:
    """Stands in for a routed reply's pduSource (a RemoteStation in real
    bacpypes3) -- deliberately NOT an IPv4Address instance."""

    def __str__(self) -> str:
        return "remote-station-stand-in"


async def _who_is_with_injected_i_ams(transport, app, apdus, **who_is_kwargs):
    """Starts transport.who_is() as a background task, yields once so
    _collect_i_ams can register its listener and send the WhoIsRequest, then
    delivers the given I-Am APDUs as if they'd just arrived over the wire."""
    task = asyncio.create_task(transport.who_is(**who_is_kwargs))
    await asyncio.sleep(0)
    for apdu in apdus:
        if hasattr(app, "fire"):
            app.fire(apdu)
        else:
            await app.do_IAmRequest(apdu)  # polyfilled dispatching wrapper by now
    return await task


@pytest.mark.asyncio
@pytest.mark.parametrize("app_factory", [_BareApplication, _NativeListenerApplication])
async def test_targeted_who_is_collects_multiple_devices_from_one_host(app_factory):
    app = app_factory()
    transport = Bacpypes3Transport(app)
    apdus = [
        make_real_i_am(1000, "172.22.0.21"),
        make_real_i_am(1001, "172.22.0.21"),
        make_real_i_am(1002, "172.22.0.21"),
    ]

    devices = await _who_is_with_injected_i_ams(
        transport, app, apdus,
        address="172.22.0.21", low_limit=0, high_limit=4194303, timeout_ms=20,
    )

    assert {d.device_instance for d in devices} == {1000, 1001, 1002}


@pytest.mark.asyncio
async def test_targeted_who_is_dedups_duplicate_device_instance():
    app = _BareApplication()
    transport = Bacpypes3Transport(app)
    apdus = [
        make_real_i_am(1000, "172.22.0.21", vendor_id=1),
        make_real_i_am(1000, "172.22.0.21", vendor_id=2),  # same instance, arrives second
    ]

    devices = await _who_is_with_injected_i_ams(
        transport, app, apdus,
        address="172.22.0.21", low_limit=0, high_limit=4194303, timeout_ms=20,
    )

    assert len(devices) == 1
    assert devices[0].vendor_id == 1  # first-seen wins, matches discover()'s dedup convention


@pytest.mark.asyncio
async def test_targeted_who_is_filters_by_device_instance_range():
    app = _BareApplication()
    transport = Bacpypes3Transport(app)
    apdus = [
        make_real_i_am(500, "172.22.0.21"),   # below range
        make_real_i_am(1005, "172.22.0.21"),  # in range
        make_real_i_am(2000, "172.22.0.21"),  # above range
    ]

    devices = await _who_is_with_injected_i_ams(
        transport, app, apdus,
        address="172.22.0.21", low_limit=1000, high_limit=1010, timeout_ms=20,
    )

    assert {d.device_instance for d in devices} == {1005}


@pytest.mark.asyncio
async def test_targeted_who_is_excludes_out_of_range_noise_among_many_replies():
    app = _BareApplication()
    transport = Bacpypes3Transport(app)
    apdus = [
        make_real_i_am(1, "172.22.0.21"),
        make_real_i_am(2, "172.22.0.21"),
        make_real_i_am(1050, "172.22.0.21"),  # the only one actually in range
        make_real_i_am(9999, "172.22.0.21"),
    ]

    devices = await _who_is_with_injected_i_ams(
        transport, app, apdus,
        address="172.22.0.21", low_limit=1050, high_limit=1050, timeout_ms=20,
    )

    assert [d.device_instance for d in devices] == [1050]


@pytest.mark.asyncio
async def test_targeted_who_is_cleans_up_listener_on_pure_timeout():
    app = _BareApplication()
    transport = Bacpypes3Transport(app)

    devices = await transport.who_is(
        address="172.22.0.21", low_limit=0, high_limit=4194303, timeout_ms=20,
    )

    assert devices == []
    assert app._i_am_listeners == []


@pytest.mark.asyncio
async def test_targeted_who_is_cleans_up_listener_when_another_listener_raises():
    app = _BareApplication()
    transport = Bacpypes3Transport(app)

    def bad_listener(apdu) -> None:
        raise RuntimeError("simulated external listener failure")

    apdus = [make_real_i_am(1000, "172.22.0.21")]

    async def who_is_with_bad_listener():
        # _ensure_i_am_listener_support runs on the first who_is() call;
        # register a second, misbehaving listener right after so it's
        # present when the I-Am arrives -- proves the dispatching wrapper's
        # per-listener try/except keeps our own collector's cleanup intact
        # regardless of what other listeners do.
        task = asyncio.create_task(transport.who_is(
            address="172.22.0.21", low_limit=0, high_limit=4194303, timeout_ms=20,
        ))
        await asyncio.sleep(0)
        app.add_i_am_listener(bad_listener)
        for apdu in apdus:
            await app.do_IAmRequest(apdu)
        return await task

    devices = await who_is_with_bad_listener()

    assert {d.device_instance for d in devices} == {1000}
    # _collect_i_ams's own listener is removed in `finally` regardless of
    # what bad_listener does; bad_listener itself is never touched by the
    # transport (it's not the collector's to manage) so it's still there.
    assert app._i_am_listeners == [bad_listener]
    app.remove_i_am_listener(bad_listener)


@pytest.mark.asyncio
async def test_targeted_who_is_do_i_am_request_identity_stable_across_calls():
    app = _BareApplication()
    transport = Bacpypes3Transport(app)

    await transport.who_is(address="172.22.0.21", low_limit=1, high_limit=1, timeout_ms=20)
    first_handler = app.do_IAmRequest

    await transport.who_is(address="172.22.0.21", low_limit=2, high_limit=2, timeout_ms=20)
    second_handler = app.do_IAmRequest

    assert first_handler is second_handler  # never reinstalled after the one-time polyfill


@pytest.mark.asyncio
async def test_targeted_who_is_single_device_resolves_promptly_not_after_full_timeout():
    app = _BareApplication()
    transport = Bacpypes3Transport(app)
    apdus = [make_real_i_am(1000, "172.22.0.21")]

    task = asyncio.create_task(_who_is_with_injected_i_ams(
        transport, app, apdus,
        address="172.22.0.21", low_limit=1000, high_limit=1000, timeout_ms=5000,
    ))
    devices = await asyncio.wait_for(task, timeout=0.5)  # would fail if it waited out the 5s window

    assert {d.device_instance for d in devices} == {1000}


@pytest.mark.asyncio
async def test_broadcast_who_is_path_unchanged_no_listener_used():
    class _SpyApplication:
        def __init__(self) -> None:
            self.who_is_calls: list[tuple] = []
            self.add_i_am_listener_calls = 0

        async def who_is(self, *, low_limit, high_limit):
            self.who_is_calls.append((low_limit, high_limit))
            return [make_real_i_am(1000, "172.22.0.21")]

        def add_i_am_listener(self, listener) -> None:
            self.add_i_am_listener_calls += 1

    app = _SpyApplication()
    transport = Bacpypes3Transport(app)

    devices = await transport.who_is(address=None, low_limit=0, high_limit=4194303, timeout_ms=20)

    assert {d.device_instance for d in devices} == {1000}
    assert app.who_is_calls == [(0, 4194303)]
    assert app.add_i_am_listener_calls == 0


@pytest.mark.asyncio
async def test_overlapping_targeted_who_is_calls_do_not_cross_contaminate():
    app = _NativeListenerApplication()
    transport = Bacpypes3Transport(app)

    async def run_one(low: int, high: int, apdus: list) -> Any:
        return await _who_is_with_injected_i_ams(
            transport, app, apdus,
            address="172.22.0.21", low_limit=low, high_limit=high, timeout_ms=30,
        )

    results = await asyncio.gather(
        run_one(1000, 1000, [make_real_i_am(1000, "172.22.0.21")]),
        run_one(2000, 2000, [make_real_i_am(2000, "172.22.0.21")]),
    )

    first_instances = {d.device_instance for d in results[0]}
    second_instances = {d.device_instance for d in results[1]}
    assert first_instances == {1000}
    assert second_instances == {2000}


@pytest.mark.asyncio
async def test_targeted_who_is_excludes_unrelated_source_i_am():
    app = _BareApplication()
    transport = Bacpypes3Transport(app)
    apdus = [
        make_real_i_am(1000, "172.22.0.21"),  # the actual target host
        make_real_i_am(1001, "172.22.0.99"),  # unrelated device, in-range, wrong source
    ]

    devices = await _who_is_with_injected_i_ams(
        transport, app, apdus,
        address="172.22.0.21", low_limit=0, high_limit=4194303, timeout_ms=20,
    )

    assert {d.device_instance for d in devices} == {1000}


@pytest.mark.asyncio
async def test_targeted_who_is_does_not_incorrectly_filter_routed_source():
    app = _BareApplication()
    transport = Bacpypes3Transport(app)
    routed_i_am = make_real_i_am(1000, "172.22.0.21")
    routed_i_am.pduSource = _FakeRoutedSource()  # not an IPv4Address -- can't be compared reliably

    devices = await _who_is_with_injected_i_ams(
        transport, app, [routed_i_am],
        address="172.22.0.21", low_limit=0, high_limit=4194303, timeout_ms=20,
    )

    assert {d.device_instance for d in devices} == {1000}
