"""
Protocol-agnostic network capture data model.

Nothing in this module knows about BACnet (or any other protocol). The
generic packet model carries only transport-level facts every protocol
shares -- endpoints, direction, a protocol tag, an opaque payload. All
protocol-specific structure lives in the decoded result's ``summary`` /
``details`` mappings, produced by a registered decoder, never here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Mapping, Optional


Direction = Literal["inbound", "outbound"]

DecodeStatus = Literal["unknown", "partial", "decoded", "warning", "error"]


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One side of a captured exchange. ``port`` is optional because not
    every transport is port-addressed."""

    address: str
    port: Optional[int] = None

    def as_dict(self) -> dict:
        return {"address": self.address, "port": self.port}


@dataclass(frozen=True, slots=True)
class CapturedTraffic:
    """
    One captured unit of traffic, protocol-neutral.

    ``protocol`` and ``transport`` are REQUIRED and are set explicitly by
    whatever capture hook recorded the traffic -- there is no default, no
    guessing, and no fallback. ``payload`` is the opaque protocol payload;
    decoding it is the job of the decoder registered for ``protocol``.
    """

    packet_id: str
    session_id: str
    timestamp: float

    protocol: str
    transport: str

    direction: Direction

    source: Endpoint
    destination: Endpoint

    payload: bytes

    @property
    def timestamp_utc(self) -> str:
        return datetime.fromtimestamp(
            self.timestamp,
            tz=timezone.utc,
        ).isoformat()

    @property
    def length(self) -> int:
        return len(self.payload)

    @property
    def raw_hex(self) -> str:
        return self.payload.hex(" ")


@dataclass(frozen=True, slots=True)
class DecodedTraffic:
    """
    The result of running a protocol decoder over one ``CapturedTraffic``.

    ``operation`` and ``status`` are the two generic, cross-protocol fields
    the Network UI shows in its main table (the protocol-neutral "what
    happened" / "did it succeed"). ``summary`` and ``details`` are
    protocol-shaped mappings -- the UI renders ``summary`` generically and
    hands ``details`` to a protocol-specific renderer.
    """

    decode_status: DecodeStatus
    operation: Optional[str] = None
    status: Optional[str] = None
    summary: Mapping = field(default_factory=dict)
    details: Mapping = field(default_factory=dict)
    decode_error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class SimulatorAssociation:
    """
    Links a captured packet to a simulator device / point, when (and only
    when) the decoded content gives enough evidence. Every field is
    optional and stays ``None`` when there is no association -- enrichers
    never guess.
    """

    device_id: Optional[int] = None
    device_name: Optional[str] = None
    point_id: Optional[int] = None
    point_name: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "point_id": self.point_id,
            "point_name": self.point_name,
        }
