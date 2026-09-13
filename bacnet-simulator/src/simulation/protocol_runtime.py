"""Protocol-neutral runtime types -- the vocabulary `SimEngine` and every `ProtocolAdapter`
(src/simulation/protocols/) share, so the engine's tick loop, recordings, calibration, and FDD
never need to know which wire protocol a point actually speaks.

Named `protocol_runtime.py`, not `runtime.py` -- that filename is already taken by
src/simulation/runtime.py (background tasks/FastAPI lifespan, extracted from legacy.py's
tick_loop/mirror_sync_loop/etc., unrelated to this module).

This is deliberately NOT a storage redesign: `devices`/`objects` keep their exact current schema
(see src/db/migrations/registry.py's migration 32, which only adds `devices.protocol`). These
types are an engine-internal translation layer built from the same DB row dicts
`Database.get_device`/`get_object` already return -- see SimEngine._device_runtime_config/
_point_runtime_config.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from .behaviors import Behavior
    from .protocols.base import ProtocolAdapter


@dataclass(frozen=True)
class DeviceRuntimeConfig:
    """One simulated device, protocol-neutral. `protocol_config` carries whatever the owning
    adapter needs beyond the three fields above -- for BACnet, this is the full raw `devices` row
    dict (not just `device_instance`), so BacnetProtocolAdapter's construction methods (which
    already expect raw row dicts, same shape SimEngine's own DB layer returns) don't need a
    second DB round-trip just to reconstruct fields this dataclass chose not to carry."""
    device_id: int
    name: str
    protocol: str
    protocol_config: Mapping[str, Any]


@dataclass(frozen=True)
class PointRuntimeConfig:
    """One simulated point, protocol-neutral. Same `protocol_config` convention as
    DeviceRuntimeConfig -- for BACnet, the full raw `objects` row dict."""
    point_id: int
    device_id: int
    name: str
    units: str | None
    protocol_config: Mapping[str, Any]


class PointQuality(str, Enum):
    """Only GOOD and OFFLINE have defined behavior this phase -- see FaultBehavior's own class
    docstring (behaviors.py) for why `offline` is the one fault_type that changes protocol-level
    status rather than the reported value. UNCERTAIN/BAD are declared for future fault types to
    map onto, deliberately left undefined here rather than inventing BACnet/OPC-UA/Modbus
    semantics for them ahead of need."""
    GOOD = "good"
    UNCERTAIN = "uncertain"
    BAD = "bad"
    OFFLINE = "offline"


@dataclass(frozen=True)
class PointValue:
    """What SimEngine.tick() hands to `ProtocolAdapter.write_point()` -- the computed value plus
    its quality, letting each adapter translate `quality` into whatever its own protocol calls
    "this point is unreliable" (BACnet: Reliability + Status_Flags.fault; a future OPC-UA adapter:
    a Bad/Uncertain StatusCode) without the engine needing to know which."""
    value: Any
    quality: PointQuality = PointQuality.GOOD
    timestamp: datetime | None = None


@dataclass
class RuntimePoint:
    """A live simulated point, as SimEngine actually tracks it -- replaces the old
    `self._objects: dict[int, tuple[bacpypes3_object, Behavior]]`.

    `behavior` is a TEMPORARY COMPATIBILITY FIELD, not part of the intended protocol boundary --
    a ProtocolAdapter does not own simulation behavior; that stays with the engine/provider layer
    (SimEngine._point_behaviors, BuiltInSimulationProvider's own internal registry), exactly as
    today. It's carried here only because SimEngine.set_manual_value() (and, historically,
    write_object()) reads/mutates the behavior stored alongside the runtime handle directly --
    removing that coupling now would mean redesigning how manual overrides locate/replace a
    point's active behavior, unrelated churn this task doesn't need. A later, separate cleanup may
    remove this field once all behavior access is consistently routed through the existing
    behavior/provider registries -- not attempted here."""
    config: PointRuntimeConfig
    adapter: "ProtocolAdapter"
    handle: Any
    behavior: "Behavior"
