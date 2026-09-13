"""`ProtocolAdapter` -- the seam between SimEngine (how a value is computed, when ticks occur,
recordings/alarms/trends/FDD/calibration) and a wire protocol (how a device/point is exposed and
how a computed value reaches it). Sibling in spirit to SimulationProvider
(src/simulation/providers/base.py), but for the opposite side of the pipeline:
SimulationProvider answers "how is this point's value computed"; ProtocolAdapter answers "how
does that computed value reach the network."

Deliberately excludes BACnet-shaped concepts (object_type/object_instance/otype, Reliability,
Status_Flags, bacpypes3 types) -- those live only inside protocol_config dicts and each
protocol's own adapter module (e.g. src/simulation/protocols/bacnet.py). No
set_point_health/set_reliability-style method either: quality translation is each adapter's own
internal concern, driven by `PointValue.quality` in write_point().

Method signatures are sized to actual current need, verified against the real BACnet
implementation, not assumed for symmetry:
  - start()/stop() are async -- SimEngine.start()/stop() genuinely await DB calls and
    self.app.close().
  - add_device()/remove_device()/add_point()/remove_point() are sync -- their real BACnet
    equivalents (_make_device_object(), _create_object(), and every self.app.delete_object(...)
    call site) are all plain `def`, with zero `await` in any of them today. A future protocol
    whose adapter genuinely needs async point/device creation (plausible for OPC UA's asyncua)
    can widen just that one method's signature when that adapter is actually built.
  - write_point() is sync -- matches _update_value()'s existing sync signature, called
    synchronously inside the per-object tick loop."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..protocol_runtime import DeviceRuntimeConfig, PointRuntimeConfig, PointValue


@dataclass(frozen=True)
class ProtocolContext:
    """What SimEngine hands a ProtocolAdapter's start() -- just the devices participating in
    this protocol for this start() call. Nothing BACnet/OPC-UA/Modbus-specific here; each
    device's own protocol_config carries whatever that adapter needs beyond the three neutral
    fields on DeviceRuntimeConfig."""
    devices: list[DeviceRuntimeConfig]


class ProtocolAdapter(ABC):
    protocol: str
    builds_runtime_points_in_start: bool = False

    @abstractmethod
    async def start(self, context: ProtocolContext) -> None:
        """Bring up this protocol's runtime (server/socket/whatever it needs) for the given
        participating devices. Called at most once per SimEngine.start() -- never for a protocol
        with zero participating devices this cycle."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Tear down this protocol's runtime. Called from SimEngine.stop()/reload() for every
        protocol currently active, regardless of whether any OTHER protocol (e.g. BACnet) is
        also active or was ever started."""
        ...

    @abstractmethod
    def add_device(self, device: DeviceRuntimeConfig) -> Any:
        """Register one device with this protocol's runtime, returning an opaque handle
        (whatever this adapter itself needs to remember -- BACnet: a live bacpypes3 DeviceObject;
        a future OPC-UA adapter: an asyncua node reference)."""
        ...

    @abstractmethod
    def remove_device(self, handle: Any) -> None:
        ...

    @abstractmethod
    def add_point(self, device_handle: Any, point: PointRuntimeConfig) -> Any:
        """Register one point under the given device handle, returning an opaque point handle --
        what RuntimePoint.handle holds, and what every later write_point()/remove_point() call
        for this point is given back."""
        ...

    @abstractmethod
    def remove_point(self, handle: Any) -> None:
        ...

    @abstractmethod
    def write_point(self, handle: Any, update: PointValue) -> Any:
        """Write a freshly computed value (and its quality) to this point. Returns whatever was
        actually written after any protocol-level coercion/clamping (mirrors _update_value()'s
        existing "return the post-coercion value" contract) -- callers use this return value for
        _prev_values/history rather than the pre-coercion value, so those can never disagree with
        what a real client of this protocol would read."""
        ...
