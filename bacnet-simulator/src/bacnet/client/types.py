from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Any, Literal, Protocol


class BACnetObjectType(StrEnum):
    ANALOG_INPUT = "analog-input"
    ANALOG_OUTPUT = "analog-output"
    ANALOG_VALUE = "analog-value"
    BINARY_INPUT = "binary-input"
    BINARY_OUTPUT = "binary-output"
    BINARY_VALUE = "binary-value"
    DEVICE = "device"
    MULTI_STATE_INPUT = "multi-state-input"
    MULTI_STATE_OUTPUT = "multi-state-output"
    MULTI_STATE_VALUE = "multi-state-value"


OBJECT_TYPE_CODE_TO_NAME: dict[int, str] = {
    0: BACnetObjectType.ANALOG_INPUT,
    1: BACnetObjectType.ANALOG_OUTPUT,
    2: BACnetObjectType.ANALOG_VALUE,
    3: BACnetObjectType.BINARY_INPUT,
    4: BACnetObjectType.BINARY_OUTPUT,
    5: BACnetObjectType.BINARY_VALUE,
    8: BACnetObjectType.DEVICE,
    13: BACnetObjectType.MULTI_STATE_INPUT,
    14: BACnetObjectType.MULTI_STATE_OUTPUT,
    19: BACnetObjectType.MULTI_STATE_VALUE,
}

OBJECT_TYPE_NAME_TO_CODE: dict[str, int] = {
    str(name): code for code, name in OBJECT_TYPE_CODE_TO_NAME.items()
}

class BACnetTransport(Protocol):
    """
    Minimal read-only protocol boundary used by the adapter.

    Implement this against the simulator's existing BACpypes3 Application.
    No write method exists by design.
    """

    async def who_is(
        self,
        *,
        address: str | None,
        low_limit: int,
        high_limit: int,
        timeout_ms: int,
    ) -> list[IAmDevice]:
        ...

    async def read_property(
        self,
        *,
        address: str,
        object_identifier: ObjectIdentifier,
        property_identifier: int | str,
        array_index: int | None = None,
        timeout_ms: int = 5000,
    ) -> Any:
        ...

    async def close(self) -> None:
        ...



class BACnetProperty(IntEnum):
    DESCRIPTION = 28
    MODEL_NAME = 70
    OBJECT_LIST = 76
    OBJECT_NAME = 77
    OUT_OF_SERVICE = 81
    PRESENT_VALUE = 85
    PRIORITY_ARRAY = 87
    RELIABILITY = 103
    STATUS_FLAGS = 111
    UNITS = 117
    VENDOR_NAME = 121


Quality = Literal["GOOD", "BAD"]


@dataclass(slots=True, frozen=True)
class ObjectIdentifier:
    type: int
    instance: int

    @property
    def type_name(self) -> str:
        return str(OBJECT_TYPE_CODE_TO_NAME.get(self.type, f"type-{self.type}"))


@dataclass(slots=True)
class BACnetObject:
    name: str
    object_type: str
    object_instance: int
    object_name: str | None = None
    property_id: int = int(BACnetProperty.PRESENT_VALUE)
    unit: str | None = None
    enabled: bool = True

    # Discovery-only metadata. This says the source BACnet device appears to
    # support Priority_Array for this object. It does NOT grant write access.
    source_commandable: bool = False


@dataclass(slots=True)
class BACnetDevice:
    name: str
    ip_address: str
    device_instance: int
    port: int = 47808
    display_name: str | None = None
    enabled: bool = True
    objects: list[BACnetObject] = field(default_factory=list)
    poll_interval_ms: int = 5000
    max_concurrent_reads: int = 5
    connection_timeout_ms: int = 5000
    retry_attempts: int = 1
    retry_delay_ms: int = 500

    vendor_name: str | None = None
    vendor_id: int | None = None
    model_name: str | None = None
    description: str | None = None


@dataclass(slots=True)
class BACnetAdapterConfig:
    enabled: bool = True
    devices: list[BACnetDevice] = field(default_factory=list)
    global_poll_interval_ms: int = 5000
    max_concurrent_devices: int = 10


@dataclass(slots=True)
class BACnetReadResult:
    value: Any
    quality: Quality
    error: str | None = None


@dataclass(slots=True)
class DiscoveredDevice:
    name: str
    fingerprint: str
    host: str
    port: int
    device_instance: int
    confidence: Literal["high", "medium"] = "medium"
    discovered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    validated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    data_points: list[BACnetObject] = field(default_factory=list)


@dataclass(slots=True)
class DiscoveryOptions:
    discovery_targets: list[str] = field(default_factory=list)
    broadcast_address: str | None = None
    port: int = 47808
    timeout_ms: int = 5000
    max_devices: int = 100
    device_id_range: tuple[int, int] = (0, 4_194_303)
    max_objects_per_device: int | None = None
    validation_concurrency: int = 4


@dataclass(slots=True, frozen=True)
class IAmDevice:
    device_instance: int
    address: str
    port: int = 47808
    vendor_id: int | None = None


@dataclass(slots=True)
class DeviceDataPoint:
    device_name: str
    metric: str
    value: Any
    unit: str | None
    timestamp: str
    quality: Quality
    quality_code: str | None = None
    protocol: str = "bacnet"
    resolved_display_name: str | None = None
    raw_object_name: str | None = None
    raw_point_name: str | None = None
