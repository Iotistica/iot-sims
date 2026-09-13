from __future__ import annotations
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable, Optional
from .models import CapturedTraffic, Endpoint, Direction
from .decoder import DecoderRegistry
from .enrichment import EnricherRegistry

class TrafficCapture:
    """
    Thread-safe, bounded application-level network capture.

    Supported protocols require explicit decoder AND enricher registration.
    An empty association is valid; a missing enricher is a configuration error.
    Registration and argument errors fail explicitly; listeners cannot interrupt recording.
    """

    def __init__(
        self,
        *,
        decoder_registry: DecoderRegistry,
        enricher_registry: EnricherRegistry,
        max_packets: int = 10_000,
        max_payload_bytes: int = 65_535,
    ) -> None:
        if max_packets < 1:
            raise ValueError("max_packets must be at least 1")

        if not 1 <= max_payload_bytes <= 65_535:
            raise ValueError(
                "max_payload_bytes must be between 1 and 65535"
            )

        self.decoder_registry = decoder_registry
        self.enricher_registry = enricher_registry
        self._max_packets = max_packets
        self._max_payload_bytes = max_payload_bytes
        self._packets: deque[CapturedTraffic] = deque(maxlen=max_packets)
        self._lock = threading.Lock()

        self._running = False
        self._session_id: Optional[str] = None
        self._started_at: Optional[float] = None
        self._stopped_at: Optional[float] = None

        self._packets_seen = 0
        self._packets_dropped = 0
        self._inbound_count = 0
        self._outbound_count = 0
        self._bytes_captured = 0

        self._on_packet: Optional[Callable[[CapturedTraffic], None]] = None

    @property
    def running(self) -> bool:
        return self._running

    def set_packet_listener(
        self,
        listener: Optional[Callable[[CapturedTraffic], None]],
    ) -> None:
        self._on_packet = listener

    def start(self, *, clear_existing: bool = True) -> dict:
        with self._lock:
            if clear_existing:
                self._packets.clear()
                self._reset_counters()

            self._running = True
            self._session_id = str(uuid.uuid4())
            self._started_at = time.time()
            self._stopped_at = None

            return self._status_unlocked()

    def stop(self) -> dict:
        with self._lock:
            self._running = False
            self._stopped_at = time.time()
            return self._status_unlocked()

    def clear(self) -> dict:
        with self._lock:
            self._packets.clear()
            self._reset_counters()
            return self._status_unlocked()

    def status(self) -> dict:
        with self._lock:
            return self._status_unlocked()

    def snapshot(self):
        with self._lock:
            return list(self._packets)

    def list_packets(self, *, protocol=None, direction=None, operation=None, device_id=None,
                     unassociated=False, offset=0, limit=100, newest_first=True, context=None):
        if protocol is not None:
            self.decoder_registry.get(protocol)
            self.enricher_registry.get(protocol)
        if direction not in (None, 'inbound', 'outbound'):
            raise ValueError('direction must be inbound or outbound')
        raw = self.snapshot()
        if protocol is not None:
            raw = [p for p in raw if p.protocol == protocol]
        # Keep both directions for request/response correlation during enrichment.
        packets = self.serialize(context=context, packets=raw)
        filtered = [p for p in packets
                    if (protocol is None or p['protocol'] == protocol)
                    and (direction is None or p['direction'] == direction)
                    and (operation is None or p['operation'] == operation)
                    and (device_id is None or p['simulator']['device_id'] == device_id)
                    and (not unassociated or p['simulator']['device_id'] is None)]
        if newest_first:
            filtered.reverse()
        offset, limit = max(0, offset), max(1, min(limit, 250))
        return {'items': filtered[offset:offset+limit], 'total': len(filtered), 'offset': offset, 'limit': limit}

    def get_packet(self, packet_id, *, context=None):
        return next((p for p in self.serialize(context=context) if p['packet_id'] == packet_id), None)

    def serialize(self, *, context=None, packets=None):
        raw = self.snapshot() if packets is None else packets
        decoded = [self.decoder_registry.get(p.protocol).decode(p) for p in raw]
        packets = []
        for p, d in zip(raw, decoded):
            value = asdict(p)
            value.pop('payload')
            value.update(asdict(d))
            value.update(timestamp_utc=p.timestamp_utc, length=p.length, raw_hex=p.raw_hex)
            packets.append(value)
        context = dict(context or {})
        context['packets'] = packets
        for p, d, value in zip(raw, decoded, packets):
            value['simulator'] = self.enricher_registry.get(p.protocol).enrich(p, d, context).as_dict()
        return packets

    def record(
        self,
        *,
        protocol: str,
        transport: str,
        direction: Direction,
        payload: bytes,
        source: Endpoint,
        destination: Endpoint,
        timestamp: Optional[float] = None,
    ) -> None:
        self.decoder_registry.get(protocol)
        self.enricher_registry.get(protocol)
        if not transport or direction not in ('inbound', 'outbound'):
            raise ValueError('Explicit transport and valid direction are required')
        if not isinstance(source, Endpoint) or not isinstance(destination, Endpoint):
            raise TypeError('source and destination must be Endpoint objects')
        if not self._running:
            return

        raw_payload = bytes(payload)

        with self._lock:
            if not self._running:
                return

            self._packets_seen += 1

            if len(raw_payload) > self._max_payload_bytes:
                raw_payload = raw_payload[: self._max_payload_bytes]
                self._packets_dropped += 1

            if len(self._packets) == self._max_packets:
                self._packets_dropped += 1

            packet = CapturedTraffic(
                packet_id=str(uuid.uuid4()),
                session_id=self._session_id,
                timestamp=time.time() if timestamp is None else timestamp,
                protocol=protocol, transport=transport,
                direction=direction,
                source=source,
                destination=destination,
                payload=raw_payload,
            )

            self._packets.append(packet)
            self._bytes_captured += len(raw_payload)

            if direction == "inbound":
                self._inbound_count += 1
            else:
                self._outbound_count += 1

        listener = self._on_packet
        if listener is not None:
            try:
                listener(packet)
            except Exception:
                # Observer failures must not disrupt capture or network traffic.
                pass

    def _reset_counters(self) -> None:
        self._packets_seen = 0
        self._packets_dropped = 0
        self._inbound_count = 0
        self._outbound_count = 0
        self._bytes_captured = 0

    def _status_unlocked(self) -> dict:
        return {
            "state": "running" if self._running else "stopped",
            "session_id": self._session_id,
            "started_at": _iso_timestamp(self._started_at),
            "stopped_at": _iso_timestamp(self._stopped_at),
            "packets_seen": self._packets_seen,
            "packets_stored": len(self._packets),
            "packets_dropped": self._packets_dropped,
            "inbound_count": self._inbound_count,
            "outbound_count": self._outbound_count,
            "bytes_captured": self._bytes_captured,
            "max_packets": self._max_packets,
        }

def _iso_timestamp(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None

    return datetime.fromtimestamp(
        value,
        tz=timezone.utc,
    ).isoformat()

