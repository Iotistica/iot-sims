"""BACnet Intrinsic Reporting (Clause 13) — Phases 1 & 2.

Scope: Analog Input/Output/Value out-of-range alarming, Binary Input/Output/
Value change-of-state alarming, and Multi-state Input/Output/Value
change-of-state alarming (a configurable set of "alarm" state values), all
routed through per-device Notification Class objects, with time delays and
acknowledgment.

Deferred (see GH issue #6 follow-ups): Algorithmic Reporting / Event
Enrollment, and the rest of the BACnet event algorithm family beyond
out-of-range / change-of-state.

This module only computes state transitions and builds/sends the resulting
Event Notification APDUs — persistence (alarm_log, notification_classes,
object_alarm_configs) lives in bacnet_simulator.py's Database class, and the
per-tick evaluation loop lives in SimulatorEngine.tick().
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from bacpypes3.apdu import ConfirmedEventNotificationRequest, UnconfirmedEventNotificationRequest
from bacpypes3.basetypes import EventState, EventType, NotifyType, TimeStamp
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import Unsigned

from .config import BACNET_PORT

log = logging.getLogger("alarms")

ANALOG_TYPES = {"analog-input", "analog-output", "analog-value"}
BINARY_TYPES = {"binary-input", "binary-output", "binary-value"}
MULTISTATE_TYPES = {"multi-state-input", "multi-state-output", "multi-state-value"}

# String states used for storage (DB, alarm_log, API) map onto bacpypes3's
# EventState enum only when actually building an APDU.
EVENT_STATE_CODE = {
    "normal": EventState.normal,
    "offnormal": EventState.offnormal,
    "high-limit": EventState.highLimit,
    "low-limit": EventState.lowLimit,
    "fault": EventState.fault,
}

TRANSITION_NAME = {"normal": "to-normal", "fault": "to-fault"}  # else "to-offnormal"


def transition_name(to_state: str) -> str:
    return TRANSITION_NAME.get(to_state, "to-offnormal")


@dataclass
class AlarmRuntime:
    """In-memory per-object state, mirrors the pattern SimulatorEngine already
    uses to carry behavior state across ticks (not persisted — a restart
    starts every object back at "normal", which is an acceptable simulator
    simplification)."""
    confirmed_state: str = "normal"
    pending_state: Optional[str] = None
    pending_since: float = 0.0


def _analog_target_state(value: float, params: dict, current_state: str) -> str:
    high = params.get("high_limit")
    low = params.get("low_limit")
    deadband = params.get("deadband") or 0
    # Hysteresis: once alarmed, require crossing back past the deadband
    # before returning to normal, matching the BACnet OUT_OF_RANGE algorithm.
    if current_state == "high-limit" and high is not None and value >= high - deadband:
        return "high-limit"
    if current_state == "low-limit" and low is not None and value <= low + deadband:
        return "low-limit"
    if high is not None and value >= high:
        return "high-limit"
    if low is not None and value <= low:
        return "low-limit"
    return "normal"


def _binary_target_state(value: bool, params: dict) -> str:
    alarm_value = params.get("alarm_value", True)
    return "offnormal" if bool(value) == bool(alarm_value) else "normal"


def _multistate_target_state(value: int, params: dict) -> str:
    alarm_values = params.get("alarm_values") or []
    return "offnormal" if int(value) in alarm_values else "normal"


def describe_transition(
    otype: str, value: Any, params: dict, from_state: str, to_state: str, units: str = "",
) -> str:
    """Human-readable detail for the alarm log / device log / notification
    messageText — e.g. "31.5 degrees-celsius >= high limit 30 (deadband 2)"."""
    unit_suffix = f" {units}" if units and units != "no-units" else ""
    if otype in ANALOG_TYPES:
        deadband = params.get("deadband") or 0
        if to_state == "high-limit":
            return f"{value}{unit_suffix} >= high limit {params.get('high_limit')}{unit_suffix}"
        if to_state == "low-limit":
            return f"{value}{unit_suffix} <= low limit {params.get('low_limit')}{unit_suffix}"
        if to_state == "normal":
            bound = params.get("high_limit") if from_state == "high-limit" else params.get("low_limit")
            if bound is not None:
                return f"{value}{unit_suffix} back within limit {bound}{unit_suffix} (deadband {deadband}{unit_suffix})"
            return f"{value}{unit_suffix} back to normal"
    elif otype in BINARY_TYPES:
        alarm_value = params.get("alarm_value", True)
        if to_state == "offnormal":
            return f"value = {value} (alarms when {alarm_value})"
        if to_state == "normal":
            return f"value = {value} (back to normal)"
    elif otype in MULTISTATE_TYPES:
        alarm_values = params.get("alarm_values") or []
        if to_state == "offnormal":
            return f"state = {value} (alarm states: {alarm_values})"
        if to_state == "normal":
            return f"state = {value} (back to normal)"
    return f"{from_state} -> {to_state}"


def _confirm_transition(
    target: str,
    runtime: AlarmRuntime,
    elapsed_seconds: float,
    time_delay: float,
    time_delay_normal: float,
) -> Optional[tuple[str, str]]:
    """Time-delay confirmation shared by both per-object Intrinsic Reporting
    (evaluate()) and Event Enrollment Algorithmic Reporting
    (evaluate_enrollment()) — the only difference between them is how the
    target state is computed, not how a transition gets confirmed."""
    if target == runtime.confirmed_state:
        runtime.pending_state = None
        return None

    if runtime.pending_state != target:
        runtime.pending_state = target
        runtime.pending_since = elapsed_seconds

    delay = time_delay_normal if target == "normal" else time_delay
    if elapsed_seconds - runtime.pending_since < delay:
        return None

    from_state = runtime.confirmed_state
    runtime.confirmed_state = target
    runtime.pending_state = None
    return from_state, target


def evaluate(
    otype: str,
    value: Any,
    params: dict,
    runtime: AlarmRuntime,
    elapsed_seconds: float,
    time_delay: float,
    time_delay_normal: float,
) -> Optional[tuple[str, str]]:
    """Advance one object's alarm state by one tick. Returns (from_state,
    to_state) if a delay-confirmed transition completed this tick, else None."""
    if otype in ANALOG_TYPES:
        target = _analog_target_state(float(value), params, runtime.confirmed_state)
    elif otype in BINARY_TYPES:
        target = _binary_target_state(bool(value), params)
    elif otype in MULTISTATE_TYPES:
        target = _multistate_target_state(int(round(float(value))), params)
    else:
        return None
    return _confirm_transition(target, runtime, elapsed_seconds, time_delay, time_delay_normal)


# ── Algorithmic Reporting (Event Enrollment) ────────────────────────────────
#
# The registry below is the extension point for adding more algorithms later
# without touching evaluate_enrollment() or the engine's tick loop. Each
# entry's signature is (otype, value, params, current_state) -> target_state
# — current_state is only used by out-of-range (deadband hysteresis) but
# every entry takes it for a uniform dispatch signature.
#
# Per the BACnet spec, Change-of-State only applies to discrete properties
# (boolean/enumerated) and Out-of-Range only to analog ones — object-type
# validation for each algorithm lives in bacnet_simulator.py's
# _validate_enrollment().

def _change_of_state_enrollment_target(otype: str, value: Any, params: dict, current_state: str) -> str:
    if otype in BINARY_TYPES:
        return _binary_target_state(bool(value), params)
    if otype in MULTISTATE_TYPES:
        return _multistate_target_state(int(round(float(value))), params)
    return "normal"


def _out_of_range_enrollment_target(otype: str, value: Any, params: dict, current_state: str) -> str:
    if otype in ANALOG_TYPES:
        return _analog_target_state(float(value), params, current_state)
    return "normal"


ENROLLMENT_ALGORITHMS = {
    "change-of-state": _change_of_state_enrollment_target,
    "out-of-range": _out_of_range_enrollment_target,
}


def evaluate_enrollment(
    algorithm: str,
    otype: str,
    value: Any,
    params: dict,
    runtime: AlarmRuntime,
    elapsed_seconds: float,
    time_delay: float,
    time_delay_normal: float,
) -> Optional[tuple[str, str]]:
    """Same shape as evaluate(), but for an Event Enrollment watching another
    object's present-value independently of that object's own alarm config —
    algorithm selects which target-state function to use."""
    target_fn = ENROLLMENT_ALGORITHMS.get(algorithm)
    if target_fn is None:
        return None
    target = target_fn(otype, value, params, runtime.confirmed_state)
    return _confirm_transition(target, runtime, elapsed_seconds, time_delay, time_delay_normal)


def _resolve_recipient_address(recipient: dict) -> Optional[str]:
    """
    Resolves an address-type (or pre-split legacy) recipient to a network
    address. Device-type recipients never reach this — see
    send_event_notification's on_local_delivery for why.
    """
    if recipient.get("recipient_type") == "address":
        ip = recipient.get("ip_address")
        if not ip:
            return None
        port = recipient.get("port") or BACNET_PORT
        return f"{ip}:{port}"

    # Legacy rows saved before the device/address split: a plain "address"
    # string like "192.168.1.50:47808".
    return recipient.get("address")


async def send_event_notification(
    app: Any,
    device_instance: int,
    obj_row: dict,
    notification_class: dict,
    from_state: str,
    to_state: str,
    priority: int,
    ack_required: bool,
    *,
    device_capabilities: Optional[dict[int, bool]] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    on_local_delivery: Optional[Callable[[int, Optional[int], str, str, str], None]] = None,
) -> None:
    """Best-effort delivery to every recipient. Recipients that don't
    resolve are still recorded in alarm_log by the caller — they just don't
    get a live BACnet notification.

    device_capabilities maps device_instance -> can-receive-events for every
    currently enabled device (see _effective_can_receive_events in
    simulator.py). A device-type recipient that can't receive events is
    skipped — its rejection is reported via log_fn rather than silently
    treated the same as a successful send.

    Device-type recipients never go over the real network at all: every
    virtual device in this simulator shares one BACnet/IP socket, so a
    device-type recipient's address always resolves to our own bound
    address — and bacpypes3's IPv4 transport (ipv4/__init__.py,
    IPv4DatagramServer.confirmation()) silently drops any inbound packet
    whose source equals its own bound address as a reflected-broadcast
    safety measure, before it ever reaches Application.indication(). A real
    round-trip therefore cannot work for this case; on_local_delivery
    simulates receipt in-process instead. Only address-type recipients
    (genuine external network addresses) go through the real send path
    below."""
    try:
        recipients = json.loads(notification_class.get("recipients") or "[]")
    except (TypeError, ValueError):
        recipients = []
    if not recipients:
        return

    otype = obj_row["object_type"]
    event_type = EventType.outOfRange if otype in ANALOG_TYPES else EventType.changeOfState
    message = f"{obj_row['name']} transitioned {from_state} -> {to_state}"

    for recipient in recipients:
        if recipient.get("recipient_type") == "device":
            target_instance = recipient.get("device_instance")
            if device_capabilities is not None and target_instance in device_capabilities and not device_capabilities[target_instance]:
                if log_fn:
                    log_fn(
                        "warn",
                        f"Event notification to device {target_instance} rejected — "
                        f"that device does not support Event Notification reception",
                    )
                continue
            if on_local_delivery:
                on_local_delivery(device_instance, target_instance, message, from_state, to_state)
            continue

        address = _resolve_recipient_address(recipient)
        if not address:
            continue
        confirmed = bool(recipient.get("confirmed"))
        apdu_cls = ConfirmedEventNotificationRequest if confirmed else UnconfirmedEventNotificationRequest
        try:
            apdu = apdu_cls(
                processIdentifier=int(recipient.get("process_identifier", 1)),
                initiatingDeviceIdentifier=("device", device_instance),
                eventObjectIdentifier=(otype, obj_row["object_instance"]),
                timeStamp=TimeStamp(sequenceNumber=Unsigned(int(time.time()) % 65536)),
                notificationClass=notification_class["id"],
                priority=priority,
                eventType=event_type,
                messageText=message,
                notifyType=NotifyType.alarm,
                ackRequired=ack_required,
                fromState=EVENT_STATE_CODE.get(from_state, EventState.normal),
                toState=EVENT_STATE_CODE.get(to_state, EventState.normal),
            )
            apdu.pduDestination = Address(address)
            log.info("Sending %s event notification to %s: %s", "confirmed" if confirmed else "unconfirmed", address, message)
            result = app.request(apdu)
            if confirmed:
                await asyncio.wait_for(result, timeout=5.0)
        except Exception:
            log.warning("Failed to deliver event notification to %s", address, exc_info=True)
