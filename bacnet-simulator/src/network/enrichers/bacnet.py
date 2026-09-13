from __future__ import annotations
from typing import Callable, Optional
from ..models import SimulatorAssociation
from ..enrichment import TrafficEnricher

def make_object_resolver(engine) -> Callable[[str, int], Optional[dict]]:
    """
    Memoized wrapper around engine.resolve_wire_object() -- a capture page
    typically references only a handful of distinct objects even across
    many packets (repeated ReadProperty/ACK pairs for the same monitored
    points), so this caps SQL queries at the number of DISTINCT objects
    actually referenced in one request, not one per packet. The cache is a
    fresh dict per call to this factory (i.e. per request) -- never
    persisted across requests, so it can't go stale across reloads/project
    loads.

    Calls engine.resolve_wire_object() directly/synchronously, never via
    asyncio.to_thread: it reads SimEngine._objects, which SimEngine.reload()
    mutates in place on the event loop. Dispatching it to a worker thread
    would introduce a cross-thread race that doesn't exist today (the
    existing single-packet detail route already calls it synchronously).
    """
    # None means no matching object. Unexpected resolution failures propagate
    # and are never cached as a successful lookup with no association.
    cache: dict[tuple[str, int], Optional[dict]] = {}

    def resolve(object_type: str, object_instance: int) -> Optional[dict]:
        key = (object_type, object_instance)

        if key not in cache:
            if engine is None:
                cache[key] = None
            else:
                cache[key] = engine.resolve_wire_object(
                    object_type=object_type,
                    physical_instance=object_instance,
                )

        return cache[key]

    return resolve


def enrich_packet_context(
    packets: list[dict],
) -> list[dict]:
    """
    Copy object/property context from confirmed requests into their
    Error, Reject, Abort, Simple-ACK, and Complex-ACK responses.
    """
    pending: dict[
        tuple[str, str, int, str, int, int],
        dict,
    ] = {}

    # Process oldest first so requests are seen before responses.
    ordered = sorted(
        packets,
        key=lambda packet: packet.get("timestamp", 0),
    )

    for packet in ordered:
        summary = packet.get("summary") or {}
        apdu = packet["details"].get("apdu") or {}

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
                packet["session_id"],
                packet["source"]["address"],
                packet["source"]["port"],
                packet["destination"]["address"],
                packet["destination"]["port"],
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
            packet["session_id"],
            packet["destination"]["address"],
            packet["destination"]["port"],
            packet["source"]["address"],
            packet["source"]["port"],
            int(invoke_id),
        )

        request = pending.get(request_key)

        if not request:
            continue

        packet_summary = packet.setdefault("summary", {})
        packet_service = packet["details"].setdefault("service", {})

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

        packet["details"]["request_packet_id"] = request[
            "request_packet_id"
        ]
        packet["details"]["request_operation"] = request["operation"]

        # Keep Error as the response operation but show which
        # request failed.
        if operation == "Error":
            packet_service["failed_operation"] = request[
                "operation"
            ]

    return packets


class BacnetTrafficEnricher(TrafficEnricher):
    protocol = "bacnet"

    def enrich(self, traffic, decoded, context) -> SimulatorAssociation:
        state = context.setdefault('bacnet', {})
        if 'packets' not in state:
            packets = [p for p in context['packets'] if p['protocol'] == self.protocol]
            state['packets'] = {p['packet_id']: p for p in enrich_packet_context(packets)}
            engine = context.get('engine')
            if context.get('live'):
                state['devices'] = engine.get_devices_by_instance() if engine else {}
                state['resolve'] = lambda *_: None
            else:
                state['devices'] = {d['device_instance']: d for d in context.get('devices', [])}
                state['resolve'] = make_object_resolver(engine)
        summary = state['packets'][traffic.packet_id]['summary']
        operation = summary.get('operation')
        instance = None
        if operation == 'I-Am':
            instance = summary.get('device_instance')
        elif operation == 'Who-Is':
            low, high = summary.get('device_instance_range_low'), summary.get('device_instance_range_high')
            if low is not None and high is not None and int(low) == int(high):
                instance = int(low)
        if summary.get('object_type') == 'device':
            instance = summary.get('object_instance')
        if instance is not None:
            device = state['devices'].get(int(instance))
            if device:
                return SimulatorAssociation(device_id=device['id'], device_name=device['name'])
            return SimulatorAssociation()
        kind, instance = summary.get('object_type'), summary.get('object_instance')
        if kind is not None and instance is not None:
            obj = state['resolve'](str(kind), int(instance))
            if obj:
                return SimulatorAssociation(device_id=obj.get('device_id'), device_name=obj.get('device_name'),
                    point_id=obj.get('object_id'), point_name=obj.get('object_name'))
        return SimulatorAssociation()
