"""Simulator device/object association for captured packets
(resolve_packet_simulator_context / make_object_resolver in
src/api/routers/packet_capture.py). Pure-function tests against fake
devices_by_instance/resolve_object -- no FastAPI/DB/engine needed, since
association is evidence-based dict lookups, not simulator queries itself.

Also exercises the interaction with the EXISTING request/response
correlation (enrich_packet_context, src/bacnet/packet_capture.py): a
response naturally inherits its request's simulator context by inheriting
the request's object_type/object_instance BEFORE resolution runs, not via
any new correlation mechanism of our own."""
from __future__ import annotations

from src.api.routers.packet_capture import (
    make_object_resolver,
    resolve_packet_simulator_context,
)
from src.bacnet.packet_capture import enrich_packet_context


def _never_resolve(object_type, object_instance):
    raise AssertionError(
        f"resolve_object should not have been called for "
        f"({object_type!r}, {object_instance!r})"
    )


def test_i_am_resolves_known_device():
    packet = {"summary": {"operation": "I-Am", "device_instance": 1003}}
    devices_by_instance = {
        1003: {"id": 5, "device_instance": 1003, "name": "AHU-1"},
    }

    resolve_packet_simulator_context(
        packet, devices_by_instance=devices_by_instance, resolve_object=_never_resolve,
    )

    assert packet["simulator_device_id"] == 5
    assert packet["simulator_device_instance"] == 1003
    assert packet["simulator_device_name"] == "AHU-1"
    assert packet["simulator_object_id"] is None


def test_object_specific_resolution():
    packet = {"summary": {"object_type": "analog-input", "object_instance": 8}}

    def resolve_object(object_type, object_instance):
        assert (object_type, object_instance) == ("analog-input", 8)
        return {
            "device_id": 5, "device_instance": 1003, "device_name": "AHU-1",
            "object_id": 42, "object_type": "analog-input", "object_instance": 8,
            "object_name": "OAT",
        }

    resolve_packet_simulator_context(
        packet, devices_by_instance={}, resolve_object=resolve_object,
    )

    assert packet["simulator_device_id"] == 5
    assert packet["simulator_object_id"] == 42
    assert packet["simulator_object_name"] == "OAT"


def test_two_devices_sharing_local_instance_not_confused():
    # Two devices whose LOCAL object_instance is both 8 -- but the packet's
    # summary carries the WIRE instance (globally unique via the
    # slot*1000+local scheme), so these are two distinct resolver keys.
    wire_map = {
        ("analog-input", 8): {
            "device_id": 1, "device_instance": 1001, "device_name": "AHU-1",
            "object_id": 10, "object_type": "analog-input", "object_instance": 8,
            "object_name": "OAT",
        },
        ("analog-input", 1008): {
            "device_id": 2, "device_instance": 1002, "device_name": "AHU-2",
            "object_id": 20, "object_type": "analog-input", "object_instance": 8,
            "object_name": "OAT",
        },
    }

    def resolve_object(object_type, object_instance):
        return wire_map.get((object_type, object_instance))

    packet_a = {"summary": {"object_type": "analog-input", "object_instance": 8}}
    packet_b = {"summary": {"object_type": "analog-input", "object_instance": 1008}}

    resolve_packet_simulator_context(packet_a, devices_by_instance={}, resolve_object=resolve_object)
    resolve_packet_simulator_context(packet_b, devices_by_instance={}, resolve_object=resolve_object)

    assert packet_a["simulator_device_name"] == "AHU-1"
    assert packet_b["simulator_device_name"] == "AHU-2"
    assert packet_a["simulator_object_id"] != packet_b["simulator_object_id"]


def test_device_object_type_resolves_via_devices_by_instance_directly():
    # object_type=="device" objects keep their real device_instance unmangled
    # on the wire and are never in SimEngine._objects/resolve_wire_object's
    # scan -- this must NOT go through resolve_object() at all.
    packet = {"summary": {"object_type": "device", "object_instance": 1003}}
    devices_by_instance = {
        1003: {"id": 5, "device_instance": 1003, "name": "AHU-1"},
    }

    resolve_packet_simulator_context(
        packet, devices_by_instance=devices_by_instance, resolve_object=_never_resolve,
    )

    assert packet["simulator_device_id"] == 5
    assert packet["simulator_object_id"] is None


def test_no_evidence_leaves_all_fields_none():
    packet = {"summary": {"operation": "ReadPropertyMultiple"}}

    resolve_packet_simulator_context(
        packet, devices_by_instance={}, resolve_object=lambda t, i: None,
    )

    assert packet["simulator_device_id"] is None
    assert packet["simulator_object_id"] is None


def test_rpm_request_and_response_stay_unassociated():
    # RPM isn't decoded into object fields (deliberately, see the plan) --
    # enrich_packet_context has nothing to forward-fill, so both the
    # request and its correlated response resolve to fully unassociated.
    request = {
        "packet_id": "req-1", "direction": "inbound",
        "source_ip": "10.0.0.5", "source_port": 47810,
        "destination_ip": "10.0.0.2", "destination_port": 47808,
        "timestamp": 1.0,
        "summary": {"operation": "ReadPropertyMultiple", "invoke_id": 7},
        "apdu": {"invoke_id": 7},
    }
    response = {
        "packet_id": "resp-1", "direction": "outbound",
        "source_ip": "10.0.0.2", "source_port": 47808,
        "destination_ip": "10.0.0.5", "destination_port": 47810,
        "timestamp": 1.1,
        "summary": {"operation": "ReadPropertyMultipleACK", "invoke_id": 7},
        "apdu": {"invoke_id": 7},
    }

    enriched = enrich_packet_context([request, response])

    for packet in enriched:
        resolve_packet_simulator_context(
            packet, devices_by_instance={}, resolve_object=lambda t, i: None,
        )
        assert packet["simulator_device_id"] is None
        assert packet["simulator_object_id"] is None


def test_i_am_from_unknown_device_resolves_to_none():
    packet = {"summary": {"operation": "I-Am", "device_instance": 9999}}

    resolve_packet_simulator_context(
        packet,
        devices_by_instance={1003: {"id": 5, "device_instance": 1003, "name": "AHU-1"}},
        resolve_object=lambda t, i: None,
    )

    assert packet["simulator_device_id"] is None


def test_correlated_response_inherits_request_simulator_context():
    request = {
        "packet_id": "req-1", "direction": "inbound",
        "source_ip": "10.0.0.5", "source_port": 47810,
        "destination_ip": "10.0.0.2", "destination_port": 47808,
        "timestamp": 1.0,
        "summary": {
            "operation": "ReadProperty", "invoke_id": 3,
            "object_type": "analog-input", "object_instance": 8,
        },
        "apdu": {"invoke_id": 3},
    }
    response = {
        "packet_id": "resp-1", "direction": "outbound",
        "source_ip": "10.0.0.2", "source_port": 47808,
        "destination_ip": "10.0.0.5", "destination_port": 47810,
        "timestamp": 1.1,
        "summary": {"operation": "ReadPropertyACK", "invoke_id": 3},
        "apdu": {"invoke_id": 3},
    }

    enriched = enrich_packet_context([request, response])

    def resolve_object(object_type, object_instance):
        assert (object_type, object_instance) == ("analog-input", 8)
        return {
            "device_id": 1, "device_instance": 1001, "device_name": "AHU-1",
            "object_id": 10, "object_type": "analog-input", "object_instance": 8,
            "object_name": "OAT",
        }

    for packet in enriched:
        resolve_packet_simulator_context(
            packet, devices_by_instance={}, resolve_object=resolve_object,
        )

    resolved = {p["packet_id"]: p for p in enriched}
    assert resolved["req-1"]["simulator_object_id"] == 10
    assert resolved["resp-1"]["simulator_object_id"] == 10
    assert resolved["resp-1"]["simulator_device_name"] == "AHU-1"


def test_stale_invoke_id_response_gets_no_inherited_context():
    response = {
        "packet_id": "resp-1", "direction": "outbound",
        "source_ip": "10.0.0.2", "source_port": 47808,
        "destination_ip": "10.0.0.5", "destination_port": 47810,
        "timestamp": 1.1,
        "summary": {"operation": "ReadPropertyACK", "invoke_id": 99},
        "apdu": {"invoke_id": 99},
    }

    enriched = enrich_packet_context([response])
    resolve_packet_simulator_context(
        enriched[0], devices_by_instance={}, resolve_object=lambda t, i: None,
    )

    assert enriched[0]["simulator_device_id"] is None
    assert enriched[0]["simulator_object_id"] is None


def test_correlation_does_not_leak_between_independent_calls():
    # enrich_packet_context builds a fresh correlation dict per call --
    # calling it once for a request, then separately for an unrelated
    # response sharing the same invoke_id but disjoint addressing, must
    # not accidentally correlate them.
    request_a = {
        "packet_id": "req-a", "direction": "inbound",
        "source_ip": "10.0.0.5", "source_port": 1,
        "destination_ip": "10.0.0.2", "destination_port": 47808,
        "timestamp": 1.0,
        "summary": {
            "operation": "ReadProperty", "invoke_id": 1,
            "object_type": "analog-input", "object_instance": 8,
        },
        "apdu": {"invoke_id": 1},
    }
    response_b = {
        "packet_id": "resp-b", "direction": "outbound",
        "source_ip": "10.0.0.9", "source_port": 2,
        "destination_ip": "10.0.0.20", "destination_port": 3,
        "timestamp": 2.0,
        "summary": {"operation": "ReadPropertyACK", "invoke_id": 1},
        "apdu": {"invoke_id": 1},
    }

    enrich_packet_context([request_a])
    enriched_b = enrich_packet_context([response_b])

    resolve_packet_simulator_context(
        enriched_b[0], devices_by_instance={}, resolve_object=lambda t, i: None,
    )

    assert enriched_b[0]["simulator_object_id"] is None


def test_resolution_uses_whichever_snapshot_is_passed():
    # Simulates before/after a project reload: same summary, two different
    # devices_by_instance snapshots -- resolution must reflect whichever
    # snapshot was passed for that call, with no leftover memoized state.
    packet_before = {"summary": {"operation": "I-Am", "device_instance": 1003}}
    resolve_packet_simulator_context(
        packet_before,
        devices_by_instance={1003: {"id": 1, "device_instance": 1003, "name": "Old-AHU"}},
        resolve_object=lambda t, i: None,
    )

    packet_after = {"summary": {"operation": "I-Am", "device_instance": 1003}}
    resolve_packet_simulator_context(
        packet_after,
        devices_by_instance={1003: {"id": 2, "device_instance": 1003, "name": "New-AHU"}},
        resolve_object=lambda t, i: None,
    )

    assert packet_before["simulator_device_name"] == "Old-AHU"
    assert packet_after["simulator_device_name"] == "New-AHU"


def test_make_object_resolver_memoizes_per_key():
    calls = []

    class FakeEngine:
        def resolve_wire_object(self, *, object_type, physical_instance):
            calls.append((object_type, physical_instance))
            return {"device_id": 1, "object_id": physical_instance}

    resolve = make_object_resolver(FakeEngine())
    resolve("analog-input", 8)
    resolve("analog-input", 8)
    resolve("analog-input", 9)

    assert calls == [("analog-input", 8), ("analog-input", 9)]


def test_make_object_resolver_handles_none_engine():
    resolve = make_object_resolver(None)
    assert resolve("analog-input", 8) is None


def test_make_object_resolver_swallows_engine_exceptions():
    class BrokenEngine:
        def resolve_wire_object(self, *, object_type, physical_instance):
            raise RuntimeError("boom")

    resolve = make_object_resolver(BrokenEngine())
    assert resolve("analog-input", 8) is None
