"""Shared runtime state.

Physically extracted from src/legacy.py -- the last piece of it, closing
out the GH #15 refactor. This module deliberately stays lightweight and
dependency-free of the FastAPI app itself (application.py, and everything
it pulls in -- all 26 routers): every other module that needs `db`/
`engine`/etc. resolves this module lazily (see each module's own
`_legacy()`-style resolver, now pointed here), including from test
contexts that construct a bare FastAPI() directly and never import
src.application or src.main at all. If this state lived in main.py
instead, every one of those lazy resolutions would trigger importing the
entire app.

`AppContext` below is separate, older scaffolding for a possible future
phase (bundling this state into one object routers read via
app.state.context instead of individual app.state.X attributes) -- not
wired up anywhere yet, left untouched.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import WebSocket

from .bacnet.packet_capture import PacketCapture
from .core.config import JWT_EXPIRE_HOURS
from .db import Database
from .energy import EnergyEngine
from .fault_detection import FaultDetectionEngine
from .monitoring import event_log as _event_log_module
from .monitoring.broadcasters import _on_packet_captured
from .monitoring.metrics import metrics


@dataclass
class AppContext:
    db: Any
    packet_capture: Any
    metrics: Any
    engine: Optional[Any] = None
    ws_clients: set[Any] = field(default_factory=set)
    metrics_ws_clients: set[Any] = field(default_factory=set)


# ─── Cadence / live-reloadable settings ────────────────────────────────────────
TICK_SECONDS = 5.0  # cadence of the engine tick loop; see tick_loop()/tick()
MIRROR_POLL_SECONDS = 3.0  # cadence of mirror_sync_loop; matches frontend 3 s external-device poll
# replay_recording_loop doesn't poll at a fixed cadence -- it sleeps until the
# next recording's own sample_interval_seconds is actually due (see
# _next_replay_recording_sleep_seconds in simulation/runtime.py). This is only
# the fallback re-check ceiling for when nothing is currently due (e.g. no
# active recordings yet), so a newly-started recording is still picked up
# promptly without the loop needing to be woken explicitly.
REPLAY_RECORDING_IDLE_CEILING_SECONDS = 5.0
# cadence of replay_playback_loop -- independent of TICK_SECONDS since a
# recording's sample_interval_seconds (and playback speed) can be much finer
# than the 5s tick.
REPLAY_PLAYBACK_POLL_SECONDS = 0.2
SIMULATION_RECOVERY_SECONDS = 30.0  # cadence of simulation_recovery_loop(); deliberately coarser
# than TICK_SECONDS -- each recovery attempt can involve a real ~300s-simulated FMU warmup
# (measured 250-800ms wall-clock per model), so this must not compete with tick timing.
OBJECT_HISTORY_MAXLEN = 720  # per-object value-history ring buffer length; see tick()


def _effective_can_receive_events(dev: dict) -> bool:
    """
    Whether this device can receive BACnet Event Notifications — real BACnet
    devices vary here (BIBBs like AE-N-I-B/AE-N-E-B vs. AE-N-A-only), and not
    every simulated device should behave as if it were a supervisory alarm
    sink. can_receive_event_notifications is an explicit per-device override
    (0/1); when unset (NULL), infer from equipment_type: devices tagged as a
    piece of physical HVAC/lighting equipment (AHU, VAV, Boiler, ...) are
    field-level devices and default to False, while untagged devices
    (workstations, BMS servers, gateways — this vocabulary has no equipment
    class for those) default to True.
    """
    override = dev.get("can_receive_event_notifications")
    if override is not None:
        return bool(override)
    return dev.get("equipment_type") is None


# ─── Globals (shared between FastAPI and engine) ──────────────────────────────

db: Database = None  # type: ignore
engine: Any = None  # type: ignore  # simulation.engine.SimEngine, untyped here to avoid importing it
ws_clients: list[WebSocket] = []
fault_detection_engine: FaultDetectionEngine | None = None
energy_engine: EnergyEngine | None = None

packet_capture = PacketCapture(
    max_packets=10_000,
    max_payload_bytes=65_535,
)
packet_stream_ws_clients: list[WebSocket] = []
metrics_ws_clients: list[WebSocket] = []

packet_capture.set_packet_listener(_on_packet_captured)


def _apply_settings_live(values: dict) -> None:
    """Push a settings dict into the module globals/buffers that actually
    drive behavior, so a save takes effect immediately — no restart. Safe to
    call repeatedly (e.g. once at startup, then again on every PUT /settings).
    Resizing a deque via deque(old, maxlen=new) keeps only the newest `new`
    items, matching normal ring-buffer truncation semantics."""
    global TICK_SECONDS, JWT_EXPIRE_HOURS, OBJECT_HISTORY_MAXLEN

    TICK_SECONDS = values["tick_seconds"]
    JWT_EXPIRE_HOURS = values["jwt_expire_hours"]
    OBJECT_HISTORY_MAXLEN = values["object_history_maxlen"]

    # _device_logs/_global_log/_MAX_LOG live in monitoring.event_log --
    # mutated here via module-attribute assignment (not `global`, since this
    # function isn't defined in that module) so get_device_log_entries() /
    # _log_event() there see the update immediately.
    _event_log_module._MAX_LOG = values["device_log_maxlen"]
    for device_id in list(_event_log_module._device_logs):
        _event_log_module._device_logs[device_id] = deque(
            _event_log_module._device_logs[device_id], maxlen=_event_log_module._MAX_LOG
        )
    _event_log_module._global_log = deque(_event_log_module._global_log, maxlen=values["global_log_maxlen"])

    metrics.recent_errors = deque(metrics.recent_errors, maxlen=values["metrics_errors_maxlen"])
    metrics.new_devices_timeline = deque(metrics.new_devices_timeline, maxlen=values["metrics_new_devices_maxlen"])
    metrics.duplicate_id_events = deque(metrics.duplicate_id_events, maxlen=values["metrics_duplicate_id_maxlen"])
    metrics.recent_requests = deque(metrics.recent_requests, maxlen=values["metrics_traffic_feed_maxlen"])
    metrics.latencies_ms = deque(metrics.latencies_ms, maxlen=values["metrics_traffic_feed_maxlen"])

    if engine is not None:
        for obj_id in list(engine._history):
            engine._history[obj_id] = deque(engine._history[obj_id], maxlen=OBJECT_HISTORY_MAXLEN)
