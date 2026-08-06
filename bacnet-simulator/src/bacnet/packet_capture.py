"""
Application-level BACnet/IP packet capture.

This module does not open a network socket. The existing BACnet UDP transport
must call record_inbound() and record_outbound() with the raw UDP payload.

Captured packets are stored in a bounded in-memory ring buffer and can be
exported as Wireshark-compatible PCAP.
"""

from __future__ import annotations

import ipaddress
import io
import socket
import struct
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal, Optional


Direction = Literal["inbound", "outbound"]

ERROR_CLASSES = {
    0: "device",
    1: "object",
    2: "property",
    3: "resources",
    4: "security",
    5: "services",
    6: "virtual-terminal",
    7: "communication",
}

ERROR_CODES = {
    0: "other",
    1: "authentication-failed",
    2: "configuration-in-progress",
    7: "device-busy",
    8: "dynamic-creation-not-supported",
    9: "file-access-denied",
    10: "incompatible-security-levels",
    11: "inconsistent-parameters",
    12: "inconsistent-selection-criterion",
    13: "invalid-data-type",
    14: "invalid-file-access-method",
    15: "invalid-file-start-position",
    16: "invalid-operator-name",
    17: "invalid-parameter-data-type",
    18: "invalid-time-stamp",
    19: "key-generation-error",
    20: "missing-required-parameter",
    21: "no-objects-of-specified-type",
    22: "no-space-for-object",
    23: "no-space-to-add-list-element",
    24: "no-space-to-write-property",
    25: "no-vt-sessions-available",
    26: "property-is-not-a-list",
    27: "object-deletion-not-permitted",
    30: "unknown-object",
    31: "unknown-property",
    32: "unknown-vt-class",
    33: "unknown-vt-session",
    34: "unsupported-object-type",
    35: "value-out-of-range",
    36: "vt-session-already-closed",
    37: "vt-session-termination-failure",
    40: "write-access-denied",
    42: "invalid-array-index",
    45: "read-access-denied",
    48: "unknown-device",
    50: "unknown-route",
    51: "value-not-in-array",
    54: "not-cov-property",
    56: "optional-functionality-not-supported",
    57: "invalid-configuration-data",
    58: "datatype-not-supported",
    59: "duplicate-name",
    60: "duplicate-object-id",
    61: "property-is-not-an-array",
    62: "abort-buffer-overflow",
    63: "abort-invalid-apdu-in-this-state",
    64: "abort-preempted-by-higher-priority-task",
    65: "abort-segmentation-not-supported",
    66: "abort-proprietary",
    67: "abort-other",
    68: "invalid-tag",
    69: "network-down",
    70: "reject-buffer-overflow",
    71: "reject-inconsistent-parameters",
    72: "reject-invalid-parameter-data-type",
    73: "reject-invalid-tag",
    74: "reject-missing-required-parameter",
    75: "reject-parameter-out-of-range",
    76: "reject-too-many-arguments",
    77: "reject-undefined-enumeration",
    78: "reject-unrecognized-service",
}

BACNET_OBJECT_TYPES = {
    0: "analog-input",
    1: "analog-output",
    2: "analog-value",
    3: "binary-input",
    4: "binary-output",
    5: "binary-value",
    8: "device",
    13: "multi-state-input",
    14: "multi-state-output",
    19: "multi-state-value",
    20: "trend-log",
}

BACNET_PROPERTIES = {
    75: "object-identifier",
    77: "object-name",
    79: "object-type",
    85: "present-value",
    87: "priority-array",
    104: "relinquish-default",
    111: "status-flags",
    117: "units",
}

CONFIRMED_SERVICES = {
    5: "SubscribeCOV",
    12: "ReadProperty",
    14: "ReadPropertyMultiple",
    15: "WriteProperty",
    16: "WritePropertyMultiple",
    26: "ReadRange",
}


@dataclass(frozen=True, slots=True)
class CapturedPacket:
    packet_id: str
    session_id: str
    timestamp: float
    direction: Direction

    source_ip: str
    source_port: int
    destination_ip: str
    destination_port: int

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

    def to_dict(self, include_hex: bool = True) -> dict:
        result = asdict(self)
        result.pop("payload", None)

        result["timestamp_utc"] = self.timestamp_utc
        result["length"] = self.length

        if include_hex:
            result["raw_hex"] = self.raw_hex

        result.update(decode_bacnet_summary(self.payload))
        return result


class PacketCapture:
    """
    Thread-safe, bounded application-level BACnet/IP capture.

    The class never raises capture errors into the simulator's networking path.
    """

    def __init__(
        self,
        *,
        max_packets: int = 10_000,
        max_payload_bytes: int = 65_535,
    ) -> None:
        if max_packets < 1:
            raise ValueError("max_packets must be at least 1")

        if not 1 <= max_payload_bytes <= 65_535:
            raise ValueError(
                "max_payload_bytes must be between 1 and 65535"
            )

        self._max_packets = max_packets
        self._max_payload_bytes = max_payload_bytes
        self._packets: deque[CapturedPacket] = deque(maxlen=max_packets)
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

    @property
    def running(self) -> bool:
        return self._running

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

    def record_inbound(
        self,
        payload: bytes,
        *,
        source: tuple[str, int],
        destination: tuple[str, int],
        timestamp: Optional[float] = None,
    ) -> None:
        self._record(
            "inbound",
            payload,
            source=source,
            destination=destination,
            timestamp=timestamp,
        )

    def record_outbound(
        self,
        payload: bytes,
        *,
        source: tuple[str, int],
        destination: tuple[str, int],
        timestamp: Optional[float] = None,
    ) -> None:
        self._record(
            "outbound",
            payload,
            source=source,
            destination=destination,
            timestamp=timestamp,
        )

    def list_packets(
        self,
        *,
        direction: Optional[Direction] = None,
        source_ip: Optional[str] = None,
        destination_ip: Optional[str] = None,
        service: Optional[str] = None,
        offset: int = 0,
        limit: int = 100,
        newest_first: bool = True,
    ) -> dict:
        limit = max(1, min(limit, 250))
        offset = max(0, offset)

        with self._lock:
            packets = list(self._packets)

        if newest_first:
            packets.reverse()

        filtered: list[CapturedPacket] = []

        for packet in packets:
            if direction and packet.direction != direction:
                continue

            if source_ip and packet.source_ip != source_ip:
                continue

            if destination_ip and packet.destination_ip != destination_ip:
                continue

            if service:
                decoded = decode_bacnet_summary(packet.payload)
                if decoded.get("service_name") != service:
                    continue

            filtered.append(packet)


        decoded_packets = [
                packet.to_dict()
                for packet in filtered
            ]

        decoded_packets = enrich_packet_context(
                decoded_packets
            )

        page = decoded_packets[offset : offset + limit]

        return {
                "items": page,
                "total": len(decoded_packets),
                "offset": offset,
                "limit": limit,
            }

    def get_packet(
        self,
        packet_id: str,
    ) -> Optional[dict]:
        with self._lock:
            packets = list(self._packets)

        decoded = [
            packet.to_dict(include_hex=True)
            for packet in packets
        ]

        enriched = enrich_packet_context(decoded)

        for packet in enriched:
            if packet["packet_id"] == packet_id:
                return packet

        return None

    def export_pcap(self) -> bytes:
        """
        Return a classic PCAP containing reconstructed Ethernet/IPv4/UDP frames.

        Link type 1 means Ethernet.
        """

        with self._lock:
            packets = list(self._packets)

        output = io.BytesIO()

        # PCAP global header, little-endian.
        output.write(
            struct.pack(
                "<IHHIIII",
                0xA1B2C3D4,  # magic
                2,           # major
                4,           # minor
                0,           # timezone
                0,           # timestamp accuracy
                65_535,      # snapshot length
                1,           # Ethernet
            )
        )

        for packet in packets:
            frame = build_udp_ethernet_frame(
                source_ip=packet.source_ip,
                destination_ip=packet.destination_ip,
                source_port=packet.source_port,
                destination_port=packet.destination_port,
                payload=packet.payload,
            )

            seconds = int(packet.timestamp)
            microseconds = int(
                (packet.timestamp - seconds) * 1_000_000
            )

            output.write(
                struct.pack(
                    "<IIII",
                    seconds,
                    microseconds,
                    len(frame),
                    len(frame),
                )
            )
            output.write(frame)

        return output.getvalue()

    def _record(
        self,
        direction: Direction,
        payload: bytes,
        *,
        source: tuple[str, int],
        destination: tuple[str, int],
        timestamp: Optional[float],
    ) -> None:
        if not self._running:
            return

        try:
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

                packet = CapturedPacket(
                    packet_id=str(uuid.uuid4()),
                    session_id=self._session_id or "unknown",
                    timestamp=timestamp or time.time(),
                    direction=direction,
                    source_ip=source[0],
                    source_port=int(source[1]),
                    destination_ip=destination[0],
                    destination_port=int(destination[1]),
                    payload=raw_payload,
                )

                self._packets.append(packet)
                self._bytes_captured += len(raw_payload)

                if direction == "inbound":
                    self._inbound_count += 1
                else:
                    self._outbound_count += 1

        except Exception:
            # Packet capture must never interrupt BACnet traffic.
            return

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

def decode_object_identifier(raw: bytes) -> dict:
    """Decode BACnet's four-byte ObjectIdentifier value."""
    if len(raw) != 4:
        raise ValueError("BACnet object identifier must contain exactly 4 bytes")

    value = int.from_bytes(raw, byteorder="big")
    object_type_code = value >> 22
    object_instance = value & 0x3FFFFF

    object_type = BACNET_OBJECT_TYPES.get(
        object_type_code,
        f"object-type-{object_type_code}",
    )

    return {
        "object_type_code": object_type_code,
        "object_type": object_type,
        "object_instance": object_instance,
        "object_identifier": f"{object_type}:{object_instance}",
    }


def _read_context_unsigned(
    payload: bytes,
    offset: int,
    expected_tag: int,
) -> tuple[int, int]:
    """
    Decode a primitive BACnet context-tagged unsigned value.

    Returns (value, next_offset). Supports the normal one-to-four byte forms
    used by ReadProperty/WriteProperty object, property, array-index, and
    priority fields.
    """
    if offset >= len(payload):
        raise ValueError(f"Missing context tag {expected_tag}")

    first = payload[offset]
    tag_number = first >> 4
    context_specific = bool(first & 0x08)
    length_value_type = first & 0x07

    if not context_specific or tag_number != expected_tag:
        raise ValueError(
            f"Expected context tag {expected_tag}, "
            f"found 0x{first:02x}"
        )

    offset += 1

    if length_value_type == 5:
        if offset >= len(payload):
            raise ValueError("Truncated extended context-tag length")

        length = payload[offset]
        offset += 1

        if length == 254:
            if offset + 2 > len(payload):
                raise ValueError("Truncated 16-bit context-tag length")
            length = int.from_bytes(payload[offset:offset + 2], "big")
            offset += 2
        elif length == 255:
            if offset + 4 > len(payload):
                raise ValueError("Truncated 32-bit context-tag length")
            length = int.from_bytes(payload[offset:offset + 4], "big")
            offset += 4
    elif length_value_type in (6, 7):
        raise ValueError(
            f"Context tag {expected_tag} is an opening/closing tag, "
            "not an unsigned value"
        )
    else:
        length = length_value_type

    if length < 1 or length > 4:
        raise ValueError(
            f"Unsupported context-tagged unsigned length {length}"
        )

    if offset + length > len(payload):
        raise ValueError(
            f"Truncated value for context tag {expected_tag}"
        )

    value = int.from_bytes(payload[offset:offset + length], "big")
    return value, offset + length


def _context_tag_kind(
    payload: bytes,
    offset: int,
) -> tuple[int, str] | None:
    """Return (tag_number, primitive/opening/closing) for a context tag."""
    if offset >= len(payload):
        return None

    first = payload[offset]
    if not (first & 0x08):
        return None

    tag_number = first >> 4
    lvt = first & 0x07

    if lvt == 6:
        kind = "opening"
    elif lvt == 7:
        kind = "closing"
    else:
        kind = "primitive"

    return tag_number, kind


def _property_name(property_code: int) -> str:
    return BACNET_PROPERTIES.get(
        property_code,
        f"property-{property_code}",
    )


def _decode_read_property(
    payload: bytes,
    offset: int,
) -> dict:
    """Decode the service parameters of a Confirmed ReadProperty request."""
    object_value, offset = _read_context_unsigned(
        payload,
        offset,
        0,
    )

    # ObjectIdentifier is always a four-byte value, even though the generic
    # context unsigned helper returns it as an integer.
    object_info = decode_object_identifier(
        object_value.to_bytes(4, "big")
    )

    property_code, offset = _read_context_unsigned(
        payload,
        offset,
        1,
    )

    array_index = None
    tag = _context_tag_kind(payload, offset)
    if tag and tag == (2, "primitive"):
        array_index, offset = _read_context_unsigned(
            payload,
            offset,
            2,
        )

    property_name = _property_name(property_code)

    return {
        "service": {
            "operation": "ReadProperty",
            **object_info,
            "property_identifier": property_code,
            "property": property_name,
            "array_index": array_index,
        },
        "summary": {
            "operation": "ReadProperty",
            "object_type": object_info["object_type"],
            "object_instance": object_info["object_instance"],
            "object_identifier": object_info["object_identifier"],
            "property_identifier": property_code,
            "property": property_name,
            "array_index": array_index,
        },
    }


def _decode_write_property(
    payload: bytes,
    offset: int,
) -> dict:
    """
    Decode the reliably identifiable portion of WriteProperty.

    The property value is enclosed in context tag 3 and can contain any BACnet
    application value. Preserve those bytes as raw hex rather than guessing
    the datatype. Decode priority when present.
    """
    object_value, offset = _read_context_unsigned(payload, offset, 0)
    object_info = decode_object_identifier(
        object_value.to_bytes(4, "big")
    )

    property_code, offset = _read_context_unsigned(payload, offset, 1)

    array_index = None
    tag = _context_tag_kind(payload, offset)
    if tag and tag == (2, "primitive"):
        array_index, offset = _read_context_unsigned(payload, offset, 2)

    raw_value_hex = None
    if _context_tag_kind(payload, offset) == (3, "opening"):
        value_start = offset + 1
        cursor = value_start
        depth = 1

        while cursor < len(payload) and depth:
            current = _context_tag_kind(payload, cursor)

            if current == (3, "opening"):
                depth += 1
                cursor += 1
                continue

            if current == (3, "closing"):
                depth -= 1
                if depth == 0:
                    raw_value_hex = payload[value_start:cursor].hex(" ")
                    cursor += 1
                    offset = cursor
                    break
                cursor += 1
                continue

            cursor += 1

    priority = None
    tag = _context_tag_kind(payload, offset)
    if tag and tag == (4, "primitive"):
        priority, offset = _read_context_unsigned(payload, offset, 4)

    property_name = _property_name(property_code)

    service = {
        "operation": "WriteProperty",
        **object_info,
        "property_identifier": property_code,
        "property": property_name,
        "array_index": array_index,
        "priority": priority,
        "raw_value_hex": raw_value_hex,
    }

    summary = {
        "operation": "WriteProperty",
        "object_type": object_info["object_type"],
        "object_instance": object_info["object_instance"],
        "object_identifier": object_info["object_identifier"],
        "property_identifier": property_code,
        "property": property_name,
        "array_index": array_index,
        "priority": priority,
    }

    return {
        "service": service,
        "summary": summary,
    }


def _decode_confirmed_request(
    payload: bytes,
    offset: int,
) -> dict:
    """Decode a BACnet Confirmed-Request APDU."""
    if offset + 4 > len(payload):
        raise ValueError("Truncated Confirmed-Request APDU")

    first = payload[offset]
    segmented_message = bool(first & 0x08)
    more_follows = bool(first & 0x04)
    segmented_response_accepted = bool(first & 0x02)

    max_segments_apdu = payload[offset + 1]
    invoke_id = payload[offset + 2]
    cursor = offset + 3

    sequence_number = None
    proposed_window_size = None

    if segmented_message:
        if cursor + 2 > len(payload):
            raise ValueError("Truncated segmented Confirmed-Request header")
        sequence_number = payload[cursor]
        proposed_window_size = payload[cursor + 1]
        cursor += 2

    if cursor >= len(payload):
        raise ValueError("Confirmed-Request has no service choice")

    service_choice = payload[cursor]
    cursor += 1

    service_operation = CONFIRMED_SERVICES.get(
        service_choice,
        f"Confirmed-Service-{service_choice}",
    )

    result = {
        "service_name": "Confirmed-Request",
        "apdu": {
            "type_code": 0,
            "type_name": "Confirmed-Request",
            "segmented_message": segmented_message,
            "more_follows": more_follows,
            "segmented_response_accepted": segmented_response_accepted,
            "max_segments_apdu": max_segments_apdu,
            "invoke_id": invoke_id,
            "sequence_number": sequence_number,
            "proposed_window_size": proposed_window_size,
            "service_choice": service_choice,
            "service_name": service_operation,
        },
        "service": {
            "operation": service_operation,
        },
        "summary": {
            "operation": service_operation,
            "invoke_id": invoke_id,
        },
    }

    if service_choice == 12:
        decoded = _decode_read_property(payload, cursor)
        result["service"].update(decoded["service"])
        result["summary"].update(decoded["summary"])
        result["summary"]["invoke_id"] = invoke_id

    elif service_choice == 15:
        decoded = _decode_write_property(payload, cursor)
        result["service"].update(decoded["service"])
        result["summary"].update(decoded["summary"])
        result["summary"]["invoke_id"] = invoke_id

    return result


def _decode_simple_ack(
    payload: bytes,
    offset: int,
) -> dict:
    if offset + 3 > len(payload):
        raise ValueError("Truncated Simple-ACK APDU")

    invoke_id = payload[offset + 1]
    service_choice = payload[offset + 2]
    acknowledged_service = CONFIRMED_SERVICES.get(
        service_choice,
        f"Confirmed-Service-{service_choice}",
    )

    return {
        "service_name": "Simple-ACK",
        "apdu": {
            "type_code": 2,
            "type_name": "Simple-ACK",
            "invoke_id": invoke_id,
            "service_choice": service_choice,
            "service_name": acknowledged_service,
        },
        "service": {
            "operation": "SimpleACK",
            "acknowledged_service": acknowledged_service,
        },
        "summary": {
            "operation": "SimpleACK",
            "invoke_id": invoke_id,
        },
    }


def _decode_complex_ack(
    payload: bytes,
    offset: int,
) -> dict:
    if offset + 3 > len(payload):
        raise ValueError("Truncated Complex-ACK APDU")

    first = payload[offset]
    segmented_message = bool(first & 0x08)
    more_follows = bool(first & 0x04)
    invoke_id = payload[offset + 1]
    cursor = offset + 2

    sequence_number = None
    proposed_window_size = None

    if segmented_message:
        if cursor + 2 > len(payload):
            raise ValueError("Truncated segmented Complex-ACK header")
        sequence_number = payload[cursor]
        proposed_window_size = payload[cursor + 1]
        cursor += 2

    if cursor >= len(payload):
        raise ValueError("Complex-ACK has no service choice")

    service_choice = payload[cursor]
    operation = {
        12: "ReadPropertyACK",
        14: "ReadPropertyMultipleACK",
        26: "ReadRangeACK",
    }.get(
        service_choice,
        f"ComplexACK-Service-{service_choice}",
    )

    return {
        "service_name": "Complex-ACK",
        "apdu": {
            "type_code": 3,
            "type_name": "Complex-ACK",
            "segmented_message": segmented_message,
            "more_follows": more_follows,
            "invoke_id": invoke_id,
            "sequence_number": sequence_number,
            "proposed_window_size": proposed_window_size,
            "service_choice": service_choice,
            "service_name": operation,
        },
        "service": {
            "operation": operation,
        },
        "summary": {
            "operation": operation,
            "invoke_id": invoke_id,
        },
    }


def _decode_error_apdu(
    payload: bytes,
    offset: int,
) -> dict:
    if offset + 3 > len(payload):
        raise ValueError("Truncated Error APDU")

    invoke_id = payload[offset + 1]
    service_choice = payload[offset + 2]

    failed_service = CONFIRMED_SERVICES.get(
        service_choice,
        f"Confirmed-Service-{service_choice}",
    )

    cursor = offset + 3

    error_class = None
    error_code = None
    error_class_name = None
    error_code_name = None
    decode_warning = None

    try:
        error_class, cursor = _decode_application_enumerated(
            payload,
            cursor
        )

        error_code, cursor = _decode_application_enumerated(
            payload,
            cursor
        )

        error_class_name = ERROR_CLASSES.get(
            error_class,
            f"error-class-{error_class}",
        )

        error_code_name = ERROR_CODES.get(
            error_code,
            f"error-code-{error_code}",
        )

    except ValueError as exc:
        decode_warning = str(exc)

    service = {
        "operation": "Error",
        "failed_service": failed_service,
        "error_class": error_class,
        "error_class_name": error_class_name,
        "error_code": error_code,
        "error_code_name": error_code_name,
        "raw_error_hex": payload[offset + 3:].hex(" "),
    }

    if decode_warning:
        service["decode_warning"] = decode_warning

    summary = {
        "operation": "Error",
        "invoke_id": invoke_id,
        "failed_service": failed_service,
        "error_class": error_class_name,
        "error_code": error_code_name,
    }

    return {
        "service_name": "Error",
        "apdu": {
            "type_code": 5,
            "type_name": "Error",
            "invoke_id": invoke_id,
            "service_choice": service_choice,
            "service_name": failed_service,
        },
        "service": service,
        "summary": summary,
    }


def _decode_reject_apdu(
    payload: bytes,
    offset: int,
) -> dict:
    if offset + 3 > len(payload):
        raise ValueError("Truncated Reject APDU")

    invoke_id = payload[offset + 1]
    reason = payload[offset + 2]

    reject_reasons = {
        0: "other",
        1: "buffer-overflow",
        2: "inconsistent-parameters",
        3: "invalid-parameter-data-type",
        4: "invalid-tag",
        5: "missing-required-parameter",
        6: "parameter-out-of-range",
        7: "too-many-arguments",
        8: "undefined-enumeration",
        9: "unrecognized-service",
    }

    reason_name = reject_reasons.get(reason, f"reject-reason-{reason}")

    return {
        "service_name": "Reject",
        "apdu": {
            "type_code": 6,
            "type_name": "Reject",
            "invoke_id": invoke_id,
            "reject_reason": reason,
            "reject_reason_name": reason_name,
        },
        "service": {
            "operation": "Reject",
            "reason": reason_name,
        },
        "summary": {
            "operation": "Reject",
            "invoke_id": invoke_id,
        },
    }


def _decode_abort_apdu(
    payload: bytes,
    offset: int,
) -> dict:
    if offset + 3 > len(payload):
        raise ValueError("Truncated Abort APDU")

    first = payload[offset]
    server = bool(first & 0x01)
    invoke_id = payload[offset + 1]
    reason = payload[offset + 2]

    abort_reasons = {
        0: "other",
        1: "buffer-overflow",
        2: "invalid-apdu-in-this-state",
        3: "preempted-by-higher-priority-task",
        4: "segmentation-not-supported",
        5: "security-error",
        6: "insufficient-security",
        7: "window-size-out-of-range",
        8: "application-exceeded-reply-time",
        9: "out-of-resources",
        10: "tsm-timeout",
        11: "apdu-too-long",
    }

    reason_name = abort_reasons.get(reason, f"abort-reason-{reason}")

    return {
        "service_name": "Abort",
        "apdu": {
            "type_code": 7,
            "type_name": "Abort",
            "server": server,
            "invoke_id": invoke_id,
            "abort_reason": reason,
            "abort_reason_name": reason_name,
        },
        "service": {
            "operation": "Abort",
            "server": server,
            "reason": reason_name,
        },
        "summary": {
            "operation": "Abort",
            "invoke_id": invoke_id,
        },
    }


def _decode_unconfirmed_request(
    payload: bytes,
    offset: int,
) -> dict:
    if offset + 2 > len(payload):
        raise ValueError("Truncated Unconfirmed-Request APDU")

    service_choice = payload[offset + 1]

    unconfirmed_services = {
        0: "I-Am",
        1: "I-Have",
        2: "Unconfirmed-COV-Notification",
        3: "Unconfirmed-Event-Notification",
        4: "Unconfirmed-Private-Transfer",
        5: "Unconfirmed-Text-Message",
        6: "Time-Synchronization",
        7: "Who-Has",
        8: "Who-Is",
        9: "UTC-Time-Synchronization",
        10: "Write-Group",
        11: "Unconfirmed-COV-Notification-Multiple",
        12: "Unconfirmed-Audit-Notification",
        13: "Who-Am-I",
        14: "You-Are",
    }

    operation = unconfirmed_services.get(
        service_choice,
        f"Unconfirmed-Service-{service_choice}",
    )

    return {
        "service_name": operation,
        "apdu": {
            "type_code": 1,
            "type_name": "Unconfirmed-Request",
            "service_choice": service_choice,
            "service_name": operation,
        },
        "service": {
            "operation": operation,
        },
        "summary": {
            "operation": operation,
        },
    }


def _decode_apdu(
    payload: bytes,
    offset: int,
) -> dict:
    if offset >= len(payload):
        raise ValueError("Packet has no APDU")

    apdu_type = payload[offset] >> 4

    if apdu_type == 0:
        return _decode_confirmed_request(payload, offset)
    if apdu_type == 1:
        return _decode_unconfirmed_request(payload, offset)
    if apdu_type == 2:
        return _decode_simple_ack(payload, offset)
    if apdu_type == 3:
        return _decode_complex_ack(payload, offset)
    if apdu_type == 5:
        return _decode_error_apdu(payload, offset)
    if apdu_type == 6:
        return _decode_reject_apdu(payload, offset)
    if apdu_type == 7:
        return _decode_abort_apdu(payload, offset)

    return {
        "service_name": f"Unknown-APDU-{apdu_type}",
        "apdu": {
            "type_code": apdu_type,
            "type_name": f"Unknown-APDU-{apdu_type}",
        },
        "service": {},
        "summary": {
            "operation": f"Unknown-APDU-{apdu_type}",
        },
    }


def decode_bacnet_summary(payload: bytes) -> dict:
    """
    Decode enough BACnet/IP to support the packet list and details drawer.

    The decoder is deliberately defensive: malformed and unsupported packets
    remain visible, and no decoder exception is allowed to escape into the
    simulator's networking path.
    """
    result: dict = {
        "decode_status": "unknown",
        "bvlc_function": None,
        "service_name": None,
        "decode_error": None,
        "bvlc": {},
        "npdu": {},
        "apdu": {},
        "service": {},
        "summary": {},
    }

    try:
        if len(payload) < 4:
            raise ValueError("Packet is shorter than the BVLC header")

        bvlc_type = payload[0]
        bvlc_function = payload[1]
        declared_length = int.from_bytes(payload[2:4], "big")

        if bvlc_type != 0x81:
            raise ValueError(
                f"Unexpected BVLC type 0x{bvlc_type:02x}"
            )

        if declared_length != len(payload):
            result["decode_error"] = (
                f"BVLC length says {declared_length}, "
                f"actual payload is {len(payload)}"
            )

        function_names = {
            0x00: "BVLC-Result",
            0x04: "Forwarded-NPDU",
            0x05: "Register-Foreign-Device",
            0x09: "Distribute-Broadcast-To-Network",
            0x0A: "Original-Unicast-NPDU",
            0x0B: "Original-Broadcast-NPDU",
        }

        bvlc_name = function_names.get(
            bvlc_function,
            f"BVLC-0x{bvlc_function:02x}",
        )

        result["bvlc_function"] = bvlc_name
        result["bvlc"] = {
            "type": bvlc_type,
            "type_hex": f"0x{bvlc_type:02x}",
            "function": bvlc_function,
            "function_name": bvlc_name,
            "declared_length": declared_length,
        }

        # Forwarded-NPDU includes a six-byte originating address.
        npdu_offset = 10 if bvlc_function == 0x04 else 4

        if bvlc_function == 0x04 and len(payload) >= 10:
            origin_ip = socket.inet_ntoa(payload[4:8])
            origin_port = int.from_bytes(payload[8:10], "big")
            result["bvlc"]["originating_address"] = (
                f"{origin_ip}:{origin_port}"
            )

        if len(payload) <= npdu_offset + 1:
            result["decode_status"] = "partial"
            return result

        npdu_version = payload[npdu_offset]
        control = payload[npdu_offset + 1]
        cursor = npdu_offset + 2

        result["npdu"] = {
            "version": npdu_version,
            "control": control,
            "control_hex": f"0x{control:02x}",
            "network_layer_message": bool(control & 0x80),
            "destination_specified": bool(control & 0x20),
            "source_specified": bool(control & 0x08),
            "expecting_reply": bool(control & 0x04),
            "priority": control & 0x03,
        }

        if npdu_version != 0x01:
            result["decode_status"] = "partial"
            result["decode_error"] = (
                result["decode_error"]
                or f"Unsupported NPDU version {npdu_version}"
            )
            return result

        if control & 0x20:
            if len(payload) < cursor + 3:
                raise ValueError("Truncated NPDU destination")

            destination_network = int.from_bytes(
                payload[cursor:cursor + 2],
                "big",
            )
            cursor += 2

            destination_length = payload[cursor]
            cursor += 1

            if cursor + destination_length + 1 > len(payload):
                raise ValueError("Truncated NPDU destination address")

            destination_mac = payload[
                cursor:cursor + destination_length
            ]
            cursor += destination_length
            hop_count = payload[cursor]
            cursor += 1

            result["npdu"].update({
                "destination_network": destination_network,
                "destination_mac": destination_mac.hex(" "),
                "hop_count": hop_count,
            })

        if control & 0x08:
            if len(payload) < cursor + 3:
                raise ValueError("Truncated NPDU source")

            source_network = int.from_bytes(
                payload[cursor:cursor + 2],
                "big",
            )
            cursor += 2

            source_length = payload[cursor]
            cursor += 1

            if cursor + source_length > len(payload):
                raise ValueError("Truncated NPDU source address")

            source_mac = payload[cursor:cursor + source_length]
            cursor += source_length

            result["npdu"].update({
                "source_network": source_network,
                "source_mac": source_mac.hex(" "),
            })

        if control & 0x80:
            if cursor >= len(payload):
                raise ValueError("Network-layer message has no message type")

            network_message_type = payload[cursor]
            result["npdu"]["network_message_type"] = network_message_type
            result["service_name"] = "Network-Layer-Message"
            result["summary"] = {
                "operation": "Network-Layer-Message",
            }
            result["decode_status"] = (
                "decoded"
                if result["decode_error"] is None
                else "warning"
            )
            return result

        apdu_result = _decode_apdu(payload, cursor)

        result["service_name"] = apdu_result.get("service_name")
        result["apdu"] = apdu_result.get("apdu", {})
        result["service"] = apdu_result.get("service", {})
        result["summary"] = apdu_result.get("summary", {})

        result["decode_status"] = (
            "decoded"
            if result["decode_error"] is None
            else "warning"
        )

    except Exception as exc:
        result["decode_status"] = "error"
        result["decode_error"] = str(exc)

    return result


def build_udp_ethernet_frame(
    *,
    source_ip: str,
    destination_ip: str,
    source_port: int,
    destination_port: int,
    payload: bytes,
) -> bytes:
    source_ip_bytes = ipaddress.ip_address(source_ip).packed
    destination_ip_bytes = ipaddress.ip_address(destination_ip).packed

    if len(source_ip_bytes) != 4 or len(destination_ip_bytes) != 4:
        raise ValueError("Initial PCAP implementation supports IPv4 only")

    # Synthetic MAC addresses. Wireshark only needs a valid Ethernet frame.
    destination_mac = b"\x02\x00\x00\x00\x00\x02"
    source_mac = b"\x02\x00\x00\x00\x00\x01"
    ethernet_type_ipv4 = b"\x08\x00"

    ethernet_header = (
        destination_mac
        + source_mac
        + ethernet_type_ipv4
    )

    udp_length = 8 + len(payload)
    total_ip_length = 20 + udp_length

    ip_header_without_checksum = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,                # IPv4 + 20-byte header
        0,                   # DSCP/ECN
        total_ip_length,
        0,                   # identification
        0,                   # flags/fragment offset
        64,                  # TTL
        socket.IPPROTO_UDP,
        0,                   # checksum placeholder
        source_ip_bytes,
        destination_ip_bytes,
    )

    ip_checksum = internet_checksum(ip_header_without_checksum)

    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_ip_length,
        0,
        0,
        64,
        socket.IPPROTO_UDP,
        ip_checksum,
        source_ip_bytes,
        destination_ip_bytes,
    )

    udp_header_without_checksum = struct.pack(
        "!HHHH",
        source_port,
        destination_port,
        udp_length,
        0,
    )

    pseudo_header = struct.pack(
        "!4s4sBBH",
        source_ip_bytes,
        destination_ip_bytes,
        0,
        socket.IPPROTO_UDP,
        udp_length,
    )

    udp_checksum = internet_checksum(
        pseudo_header + udp_header_without_checksum + payload
    )

    if udp_checksum == 0:
        udp_checksum = 0xFFFF

    udp_header = struct.pack(
        "!HHHH",
        source_port,
        destination_port,
        udp_length,
        udp_checksum,
    )

    return ethernet_header + ip_header + udp_header + payload


def internet_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"

    words = struct.unpack(f"!{len(data) // 2}H", data)
    total = sum(words)

    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)

    return (~total) & 0xFFFF


def _iso_timestamp(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None

    return datetime.fromtimestamp(
        value,
        tz=timezone.utc,
    ).isoformat()

def enrich_packet_context(
    packets: list[dict],
) -> list[dict]:
    """
    Copy object/property context from confirmed requests into their
    Error, Reject, Abort, Simple-ACK, and Complex-ACK responses.
    """
    pending: dict[
        tuple[str, int, str, int, int],
        dict,
    ] = {}

    # Process oldest first so requests are seen before responses.
    ordered = sorted(
        packets,
        key=lambda packet: packet.get("timestamp", 0),
    )

    for packet in ordered:
        summary = packet.get("summary") or {}
        apdu = packet.get("apdu") or {}

        invoke_id = (
            summary.get("invoke_id")
            if summary.get("invoke_id") is not None
            else apdu.get("invoke_id")
        )

        if invoke_id is None:
            continue

        operation = summary.get("operation")

        if packet.get("direction") == "inbound":
            # Request key: client -> simulator
            key = (
                packet["source_ip"],
                packet["source_port"],
                packet["destination_ip"],
                packet["destination_port"],
                int(invoke_id),
            )

            if operation in {
                "ReadProperty",
                "ReadPropertyMultiple",
                "WriteProperty",
                "WritePropertyMultiple",
                "ReadRange",
                "SubscribeCOV",
            }:
                pending[key] = {
                    "request_packet_id": packet["packet_id"],
                    "operation": operation,
                    "object_type": summary.get("object_type"),
                    "object_instance": summary.get(
                        "object_instance"
                    ),
                    "object_identifier": summary.get(
                        "object_identifier"
                    ),
                    "property_identifier": summary.get(
                        "property_identifier"
                    ),
                    "property": summary.get("property"),
                    "array_index": summary.get("array_index"),
                    "priority": summary.get("priority"),
                }

            continue

        # Response key is reversed: client -> simulator.
        request_key = (
            packet["destination_ip"],
            packet["destination_port"],
            packet["source_ip"],
            packet["source_port"],
            int(invoke_id),
        )

        request = pending.get(request_key)

        if not request:
            continue

        packet_summary = packet.setdefault("summary", {})
        packet_service = packet.setdefault("service", {})

        for field in (
            "object_type",
            "object_instance",
            "object_identifier",
            "property_identifier",
            "property",
            "array_index",
            "priority",
        ):
            value = request.get(field)

            if value is not None:
                packet_summary.setdefault(field, value)
                packet_service.setdefault(field, value)

        packet["request_packet_id"] = request[
            "request_packet_id"
        ]
        packet["request_operation"] = request["operation"]

        # Keep Error as the response operation but show which
        # request failed.
        if operation == "Error":
            packet_service["failed_operation"] = request[
                "operation"
            ]

    return packets

def _decode_context_enumerated(
    payload: bytes,
    offset: int,
    expected_tag: int,
) -> tuple[int, int]:
    return _read_context_unsigned(
        payload,
        offset,
        expected_tag,
    )

def _decode_application_enumerated(payload, offset):
    first = payload[offset]

    tag = first >> 4
    length = first & 0x07

    if tag != 9:
        raise ValueError(
            f"Expected application enumerated, got 0x{first:02x}"
        )

    offset += 1

    value = int.from_bytes(
        payload[offset:offset + length],
        "big",
    )

    return value, offset + length