from __future__ import annotations
import socket
from typing import Optional
from ..models import CapturedTraffic, DecodedTraffic
from ..decoder import TrafficDecoder

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


def _read_application_tag_header(
    payload: bytes,
    offset: int,
) -> tuple[int, int, int]:
    """
    Decode the tag byte of a BACnet application-tagged (not context-tagged)
    primitive value.

    Returns (tag_number, length, value_offset) -- value_offset points at the
    start of the value bytes, the caller reads `length` bytes from there.
    Who-Is/I-Am parameters are application-tagged (unlike ReadProperty/
    WriteProperty's context-tagged parameters, which _read_context_unsigned
    above already handles), so this is a separate primitive reader.
    """
    if offset >= len(payload):
        raise ValueError("Missing application tag")

    first = payload[offset]
    tag_number = first >> 4
    context_specific = bool(first & 0x08)

    if context_specific:
        raise ValueError(
            f"Expected application tag, found context tag 0x{first:02x}"
        )

    length_value_type = first & 0x07
    offset += 1

    if length_value_type == 5:
        if offset >= len(payload):
            raise ValueError("Truncated extended application-tag length")

        length = payload[offset]
        offset += 1

        if length == 254:
            if offset + 2 > len(payload):
                raise ValueError("Truncated 16-bit application-tag length")
            length = int.from_bytes(payload[offset:offset + 2], "big")
            offset += 2
        elif length == 255:
            if offset + 4 > len(payload):
                raise ValueError("Truncated 32-bit application-tag length")
            length = int.from_bytes(payload[offset:offset + 4], "big")
            offset += 4
    else:
        length = length_value_type

    if offset + length > len(payload):
        raise ValueError(
            f"Truncated application-tagged value (tag {tag_number})"
        )

    return tag_number, length, offset


def _read_application_unsigned(
    payload: bytes,
    offset: int,
    expected_tag: int,
) -> tuple[int, int]:
    tag_number, length, value_offset = _read_application_tag_header(
        payload, offset
    )

    if tag_number != expected_tag:
        raise ValueError(
            f"Expected application tag {expected_tag}, found {tag_number}"
        )

    value = int.from_bytes(
        payload[value_offset:value_offset + length], "big"
    )

    return value, value_offset + length


def _decode_i_am(payload: bytes, offset: int) -> dict:
    """
    Decode I-Am-Request parameters: device object identifier,
    max-apdu-length-accepted, segmentation-supported, vendor-id -- all
    application-tagged primitives, in this fixed order per the standard.

    I-Am is always sent BY one specific device, so its device identifier is
    direct, reliable evidence for simulator device association (unlike
    Who-Is, whose range parameters are optional and often a broadcast).
    """
    tag_number, length, value_offset = _read_application_tag_header(
        payload, offset
    )

    if tag_number != 12 or length != 4:
        raise ValueError(
            "I-Am device identifier is not a 4-byte object identifier"
        )

    device_info = decode_object_identifier(
        payload[value_offset:value_offset + 4]
    )
    offset = value_offset + 4

    max_apdu, offset = _read_application_unsigned(payload, offset, 2)
    segmentation, offset = _read_application_unsigned(payload, offset, 9)
    vendor_id, offset = _read_application_unsigned(payload, offset, 2)

    return {
        "service": {
            "operation": "I-Am",
            "device_instance": device_info["object_instance"],
            "device_identifier": device_info["object_identifier"],
            "max_apdu_length_accepted": max_apdu,
            "segmentation_supported": segmentation,
            "vendor_id": vendor_id,
        },
        "summary": {
            "operation": "I-Am",
            "device_instance": device_info["object_instance"],
        },
    }


def _decode_who_is(payload: bytes, offset: int) -> dict:
    """
    Decode Who-Is-Request's OPTIONAL device-instance-range parameters.

    Absent (offset already at the end of the payload) means a broadcast
    "who is anybody" query -- not evidence of any single device, left
    unassociated by the caller. When present, low and high limits are
    always sent together; low == high means a single targeted device
    instance, which IS usable evidence.
    """
    if offset >= len(payload):
        return {
            "service": {"operation": "Who-Is"},
            "summary": {"operation": "Who-Is"},
        }

    low, offset = _read_application_unsigned(payload, offset, 2)
    high, offset = _read_application_unsigned(payload, offset, 2)

    return {
        "service": {
            "operation": "Who-Is",
            "device_instance_range_low": low,
            "device_instance_range_high": high,
        },
        "summary": {
            "operation": "Who-Is",
            "device_instance_range_low": low,
            "device_instance_range_high": high,
        },
    }


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

    result = {
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

    cursor = offset + 2

    if service_choice == 0:
        decoded = _decode_i_am(payload, cursor)
        result["service"].update(decoded["service"])
        result["summary"].update(decoded["summary"])

    elif service_choice == 8:
        decoded = _decode_who_is(payload, cursor)
        result["service"].update(decoded["service"])
        result["summary"].update(decoded["summary"])

    return result


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
class BacnetTrafficDecoder(TrafficDecoder):
    protocol = "bacnet"

    def decode(self, traffic: CapturedTraffic) -> DecodedTraffic:
        result = decode_bacnet_summary(traffic.payload)
        operation = result['summary'].get('operation') or result['service_name']
        return DecodedTraffic(
            decode_status=result['decode_status'], operation=operation,
            status=None, summary=result['summary'],
            details={key: result[key] for key in ('bvlc', 'npdu', 'apdu', 'service')},
            decode_error=result['decode_error'],
        )
