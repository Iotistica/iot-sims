"""Simulator event logging helpers.

Physically extracted from src/legacy.py -- continuing the GH #15 refactor,
same "moved verbatim, no behavior changes" standard as the Database and
simulation-engine extractions.

_MAX_LOG, _device_logs, and _global_log are mutated at runtime by
legacy.py's _apply_settings_live() -- it reaches into this module directly
(`event_log._global_log = ...`, an attribute assignment, not a `global`
statement, since that mutation happens from *outside* this module) rather
than through a lazy resolver, since this module has no risk of a circular
import back to legacy.py.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

log = logging.getLogger("bacnet-sim")

# ─── Per-device event log ─────────────────────────────────────────────────────
_device_logs: dict[int, deque] = {}
_global_log: deque = deque(maxlen=1000)
_device_names: dict[int, str] = {}
_MAX_LOG = 300


def get_device_log_entries(
    device_id: int,
    limit: int,
) -> list[dict]:
    entries = list(
        _device_logs.get(device_id, [])
    )
    return entries[-limit:]


def get_global_log_entries(
    limit: int,
) -> list[dict]:
    entries = list(_global_log)
    return entries[-limit:]


def _log_event(device_id: Optional[int], level: str, message: str, *, category: str = "audit") -> None:
    """
    device_id=None records a simulator-level event not attributable to any
    one virtual device — e.g. an incoming Event Notification, which (since
    every virtual device shares one socket/address) carries the *sender's*
    device identifier but nothing about which of our devices it was
    addressed to. That's a real BACnet Event Notification limitation, not
    something to paper over with a guess.

    category distinguishes audit/config-change events (the default -- every
    call site that existed before this parameter was added) from simulation
    lifecycle events (category="simulation": FMU model started, simulation
    enabled/disabled, recovery outcomes -- see src/simulation/models/runtime.py
    and the PUT .../enabled route). It's a separate dimension from `level`:
    the admin UI's Activity Log filters on both independently.
    """
    entry = {
        "ts": time.time(),
        "level": level,
        "category": category,
        "device_id": device_id,
        "device_name": _device_names.get(device_id, f"#{device_id}") if device_id is not None else "Simulator",
        "message": message,
    }
    if device_id is not None:
        if device_id not in _device_logs:
            _device_logs[device_id] = deque(maxlen=_MAX_LOG)
        _device_logs[device_id].append(entry)
    _global_log.append(entry)


def _log_event_notification_received(
    sender_instance: Optional[int],
    recipient_instance: Optional[int],
    message_text: str,
    from_state: str,
    to_state: str,
) -> None:
    """
    Records that an Event Notification "arrived" — closes the loop on
    alarms.send_event_notification(). For device-type recipients this is
    called directly in-process (see send_event_notification's
    on_local_delivery callback) rather than from a real received APDU:
    bacpypes3's IPv4 transport (ipv4/__init__.py, IPv4DatagramServer.
    confirmation()) silently drops any inbound packet whose source address
    equals our own bound address, treating it as a reflected broadcast — so
    a genuine network round-trip can never reach Application.indication()
    for a notification addressed to one of our own devices (every virtual
    device here shares one socket/address, so that's always the case for a
    device-type recipient). Simulating receipt directly sidesteps a real,
    structural bacpypes3 limitation rather than fighting it — and, since
    it's in-process, we actually know the recipient here, unlike a genuine
    received APDU (which only ever carries the *sender's* device identifier;
    recipient_instance is None when called from that path, e.g. an external
    address-type sender).

    Logged at the simulator level (device_id=None) rather than attributed to
    the recipient device: it's still a real BACnet Event Notification
    limitation that a genuinely external client would face — the recipient
    is only ever a network address on the wire, this simulator just happens
    to have out-of-band knowledge of it for its own device-type recipients.
    """
    sender = f"device {sender_instance}" if sender_instance is not None else "an unknown device"
    recipient = f" to device {recipient_instance}" if recipient_instance is not None else ""
    level = "info" if to_state == "normal" else "error" if to_state == "fault" else "warn"
    msg = f"Received Event Notification from {sender}{recipient}: {message_text} ({from_state} -> {to_state})"
    log.info(msg)
    _log_event(None, level, msg)


__all__ = [
    "_log_event", "_device_logs", "_global_log", "_device_names",
    "get_device_log_entries", "get_global_log_entries",
    "_log_event_notification_received",
]
