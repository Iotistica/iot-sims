"""
Real-time analytics instrumentation for the OPC UA simulator — mirrors the
sibling BACnet simulator's Metrics/build_metrics_snapshot architecture
(bacnet-simulator/bacnet_simulator.py) but hooked into asyncua's actual
server internals instead of bacpypes3's Application dispatcher.

Two interception strategies, matching what asyncua==2.0.1 actually exposes:
  - Read/Write/monitored-item lifecycle: asyncua's own public
    Server.subscribe_server_callback() extension point (CallbackType.*) —
    no patching, first-class API.
  - Everything else (per-service timing/counts, service-level errors,
    session lifecycle, secure channel opens, dropped notifications):
    UaProcessor/InternalSession/InternalSubscription are hard-constructed by
    the transport layer, not injectable via subclassing, so a handful of
    their methods are wrapped once at install time and reassigned on the
    class — same "observe, don't change" technique the BACnet simulator
    uses by overriding Application.response(), just applied via monkeypatch
    here since these classes can't be subclassed in through asyncua's own
    construction path. Every wrapper calls the original unchanged and only
    records the outcome, so protocol behavior is untouched.

All hooks below do a handful of dict/deque operations — O(1), no per-request
I/O or allocation-heavy work — matching the "lightweight, must not affect
simulator performance" requirement. Expensive cross-referencing (sorting,
joining against device/tag names) happens once per second in
build_metrics_snapshot(), not once per request.
"""
import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Optional

import psutil
from asyncua import Server, ua
from asyncua.common.callback import CallbackType
from asyncua.common.utils import ServiceError
from asyncua.server.internal_session import InternalSession, SessionState
from asyncua.server.internal_subscription import InternalSubscription
from asyncua.server.uaprocessor import UaProcessor

logger = logging.getLogger("opcua-sim.analytics")
_PROCESS = psutil.Process()

# typeid.Identifier (an ObjectIds int) -> readable service name, covering
# every request type UaProcessor._process_message actually dispatches
# (mirrors its own if/elif chain in uaprocessor.py) — a lookup table we own
# rather than depending on an asyncua-internal reverse-name helper.
_SERVICE_NAMES: dict[int, str] = {
    ua.ObjectIds.CreateSessionRequest_Encoding_DefaultBinary: "CreateSession",
    ua.ObjectIds.CloseSessionRequest_Encoding_DefaultBinary: "CloseSession",
    ua.ObjectIds.ActivateSessionRequest_Encoding_DefaultBinary: "ActivateSession",
    ua.ObjectIds.FindServersRequest_Encoding_DefaultBinary: "FindServers",
    ua.ObjectIds.GetEndpointsRequest_Encoding_DefaultBinary: "GetEndpoints",
    ua.ObjectIds.RegisterServerRequest_Encoding_DefaultBinary: "RegisterServer",
    ua.ObjectIds.RegisterServer2Request_Encoding_DefaultBinary: "RegisterServer2",
    ua.ObjectIds.CloseSecureChannelRequest_Encoding_DefaultBinary: "CloseSecureChannel",
    ua.ObjectIds.ReadRequest_Encoding_DefaultBinary: "Read",
    ua.ObjectIds.WriteRequest_Encoding_DefaultBinary: "Write",
    ua.ObjectIds.BrowseRequest_Encoding_DefaultBinary: "Browse",
    ua.ObjectIds.TranslateBrowsePathsToNodeIdsRequest_Encoding_DefaultBinary: "TranslateBrowsePaths",
    ua.ObjectIds.AddNodesRequest_Encoding_DefaultBinary: "AddNodes",
    ua.ObjectIds.DeleteNodesRequest_Encoding_DefaultBinary: "DeleteNodes",
    ua.ObjectIds.AddReferencesRequest_Encoding_DefaultBinary: "AddReferences",
    ua.ObjectIds.DeleteReferencesRequest_Encoding_DefaultBinary: "DeleteReferences",
    ua.ObjectIds.CreateSubscriptionRequest_Encoding_DefaultBinary: "CreateSubscription",
    ua.ObjectIds.ModifySubscriptionRequest_Encoding_DefaultBinary: "ModifySubscription",
    ua.ObjectIds.DeleteSubscriptionsRequest_Encoding_DefaultBinary: "DeleteSubscriptions",
    ua.ObjectIds.CreateMonitoredItemsRequest_Encoding_DefaultBinary: "CreateMonitoredItems",
    ua.ObjectIds.ModifyMonitoredItemsRequest_Encoding_DefaultBinary: "ModifyMonitoredItems",
    ua.ObjectIds.DeleteMonitoredItemsRequest_Encoding_DefaultBinary: "DeleteMonitoredItems",
    ua.ObjectIds.HistoryReadRequest_Encoding_DefaultBinary: "HistoryRead",
    ua.ObjectIds.RegisterNodesRequest_Encoding_DefaultBinary: "RegisterNodes",
    ua.ObjectIds.UnregisterNodesRequest_Encoding_DefaultBinary: "UnregisterNodes",
    ua.ObjectIds.PublishRequest_Encoding_DefaultBinary: "Publish",
    ua.ObjectIds.RepublishRequest_Encoding_DefaultBinary: "Republish",
    ua.ObjectIds.TransferSubscriptionsRequest_Encoding_DefaultBinary: "TransferSubscriptions",
    ua.ObjectIds.CallRequest_Encoding_DefaultBinary: "Call",
    ua.ObjectIds.SetMonitoringModeRequest_Encoding_DefaultBinary: "SetMonitoringMode",
    ua.ObjectIds.SetPublishingModeRequest_Encoding_DefaultBinary: "SetPublishingMode",
}


def _service_name(typeid: ua.NodeId) -> str:
    return _SERVICE_NAMES.get(typeid.Identifier, f"Unknown({typeid.Identifier})")


def _fmt_peer(name: Any) -> str:
    if isinstance(name, tuple) and len(name) == 2:
        return f"{name[0]}:{name[1]}"
    return str(name) if name else "unknown"


# ─── Metrics store ──────────────────────────────────────────────────────────

class Metrics:
    def __init__(self) -> None:
        self.start_time = time.time()

        # Traffic / performance
        self.requests_total = 0
        self.requests_ok = 0
        self.requests_failed = 0
        self.requests_by_service: dict[str, int] = defaultdict(int)
        self.reads_total = 0
        self.writes_total = 0
        self.browse_total = 0
        self.call_total = 0
        self.recent_requests: deque = deque(maxlen=500)  # live traffic feed
        self.latencies_ms: deque = deque(maxlen=1000)
        self.clients_seen: dict[str, float] = {}  # peer "host:port" -> last-seen ts

        # Node analytics
        self.node_reads: dict[str, int] = defaultdict(int)   # NodeId string -> count
        self.node_writes: dict[str, int] = defaultdict(int)

        # Session analytics
        self.sessions_created = 0
        self.sessions_closed = 0
        self.recent_session_events: deque = deque(maxlen=200)
        self._session_started: dict[str, float] = {}  # session_id str -> monotonic start
        self._session_auth: dict[str, str] = {}        # session_id str -> auth method
        self.session_durations_s: deque = deque(maxlen=200)

        # Subscription analytics
        self.monitored_items_created = 0
        self.monitored_items_deleted = 0
        self.dropped_notifications = 0

        # Error analytics
        self.errors_by_type: dict[str, int] = defaultdict(int)
        self.recent_errors: deque = deque(maxlen=200)

        # Security analytics
        self.secure_channel_opens = 0
        self.auth_failures = 0
        self.rejected_connections = 0

        # Alarm & event analytics (synthetic — see record_fault_transition)
        self.alarms: dict[int, dict] = {}          # tag_id -> current/latest alarm record
        self.alarm_events: deque = deque(maxlen=300)
        self.alarm_ack_times_s: deque = deque(maxlen=200)


def _record_error(metrics: Metrics, service: str, peer: str, status_name: str, started: float, ts: float) -> None:
    latency_ms = (time.monotonic() - started) * 1000
    metrics.latencies_ms.append(latency_ms)
    metrics.requests_failed += 1
    metrics.errors_by_type[status_name] += 1
    metrics.recent_errors.append({"ts": ts, "service": service, "peer": peer, "status": status_name})
    metrics.recent_requests.append({
        "ts": ts, "service": service, "peer": peer, "ok": False, "latency_ms": round(latency_ms, 2),
    })


# ─── Instrumentation installers ─────────────────────────────────────────────

def _install_uaprocessor_patch(metrics: Metrics) -> None:
    if getattr(UaProcessor, "_analytics_patched", False):
        return
    original_process = UaProcessor._process_message
    original_open_channel = UaProcessor.open_secure_channel

    async def patched_process_message(self, typeid, requesthdr, seqhdr, body):
        name = _service_name(typeid)
        started = time.monotonic()
        ts = time.time()
        peer = _fmt_peer(getattr(self, "name", None))
        metrics.clients_seen[peer] = ts
        metrics.requests_total += 1
        metrics.requests_by_service[name] += 1
        if name == "Read":
            metrics.reads_total += 1
        elif name == "Write":
            metrics.writes_total += 1
        elif name == "Browse":
            metrics.browse_total += 1
        elif name == "Call":
            metrics.call_total += 1

        try:
            result = await original_process(self, typeid, requesthdr, seqhdr, body)
        except (ServiceError, ua.uaerrors.UaStatusCodeError) as e:
            _record_error(metrics, name, peer, ua.StatusCode(e.code).name, started, ts)
            raise
        except ua.uaerrors.BadUserAccessDenied:
            metrics.auth_failures += 1
            _record_error(metrics, name, peer, "BadUserAccessDenied", started, ts)
            raise
        except Exception:
            _record_error(metrics, name, peer, "BadInternalError", started, ts)
            raise
        else:
            latency_ms = (time.monotonic() - started) * 1000
            metrics.latencies_ms.append(latency_ms)
            metrics.requests_ok += 1
            metrics.recent_requests.append({
                "ts": ts, "service": name, "peer": peer, "ok": True, "latency_ms": round(latency_ms, 2),
            })
            return result

    def patched_open_secure_channel(self, algohdr, seqhdr, body):
        metrics.secure_channel_opens += 1
        return original_open_channel(self, algohdr, seqhdr, body)

    patched_process_message.__name__ = original_process.__name__
    patched_open_secure_channel.__name__ = original_open_channel.__name__
    UaProcessor._process_message = patched_process_message
    UaProcessor.open_secure_channel = patched_open_secure_channel
    UaProcessor._analytics_patched = True


def _install_session_patches(metrics: Metrics) -> None:
    if getattr(InternalSession, "_analytics_patched", False):
        return
    original_create = InternalSession.create_session
    original_activate = InternalSession.activate_session
    original_close = InternalSession.close_session

    async def patched_create_session(self, params, sockname=None):
        result = await original_create(self, params, sockname=sockname)
        metrics.sessions_created += 1
        sid = self.session_id.to_string()
        metrics._session_started[sid] = time.monotonic()
        metrics.recent_session_events.append({
            "ts": time.time(), "event": "created", "session_id": sid, "name": self.name,
        })
        return result

    def patched_activate_session(self, params, peer_certificate):
        try:
            result = original_activate(self, params, peer_certificate)
        except (ServiceError, ua.uaerrors.UaStatusCodeError) as e:
            if e.code == ua.StatusCodes.BadMaxConnectionsReached:
                metrics.rejected_connections += 1
            else:
                metrics.auth_failures += 1
            metrics.recent_session_events.append({
                "ts": time.time(), "event": "auth_failed", "session_id": self.session_id.to_string(),
                "status": ua.StatusCode(e.code).name,
            })
            raise

        sid = self.session_id.to_string()
        token = params.UserIdentityToken
        if token is None or (isinstance(token, ua.ExtensionObject) and token.TypeId == ua.NodeId(ua.ObjectIds.Null)):
            auth_method = "Anonymous"
        else:
            auth_method = type(token).__name__.replace("IdentityToken", "")
        metrics._session_auth[sid] = auth_method
        metrics.recent_session_events.append({
            "ts": time.time(), "event": "activated", "session_id": sid, "auth_method": auth_method,
        })
        return result

    async def patched_close_session(self, delete_subs: bool = True):
        # close_session() is idempotent (early-returns if already Closed) —
        # e.g. a client's explicit CloseSession followed by the transport
        # cleanup path (UaProcessor.close()) calling it again on disconnect.
        # Only count/record it the one time it actually closes something.
        already_closed = self.state == SessionState.Closed
        sid = self.session_id.to_string()
        result = await original_close(self, delete_subs)
        if already_closed:
            return result
        started = metrics._session_started.pop(sid, None)
        auth_method = metrics._session_auth.pop(sid, None)
        metrics.sessions_closed += 1
        duration_s = None
        if started is not None:
            duration_s = round(time.monotonic() - started, 2)
            metrics.session_durations_s.append(duration_s)
        metrics.recent_session_events.append({
            "ts": time.time(), "event": "closed", "session_id": sid,
            "duration_s": duration_s, "auth_method": auth_method,
        })
        return result

    InternalSession.create_session = patched_create_session
    InternalSession.activate_session = patched_activate_session
    InternalSession.close_session = patched_close_session
    InternalSession._analytics_patched = True


def _install_subscription_patch(metrics: Metrics) -> None:
    if getattr(InternalSubscription, "_analytics_patched", False):
        return
    original_enqueue = InternalSubscription._enqueue_event

    async def patched_enqueue_event(self, mid, eventdata, size, queue):
        if size != 0 and mid in queue and len(queue[mid]) >= size:
            metrics.dropped_notifications += 1
        return await original_enqueue(self, mid, eventdata, size, queue)

    InternalSubscription._enqueue_event = patched_enqueue_event
    InternalSubscription._analytics_patched = True


def _install_callbacks(server: Server, metrics: Metrics) -> None:
    """Official Server.subscribe_server_callback() extension point — no patching."""

    async def on_post_read(event, _service):
        # PostRead/PostWrite fire for the server's own internal session too
        # (e.g. periodic ServerStatus/CurrentTime housekeeping writes) — those
        # aren't external client traffic and would otherwise show up as
        # "most written nodes". is_external distinguishes real client
        # sessions from the server's internal one (see UaProcessor's
        # `external=True` at session creation vs InternalServer.isession).
        if not event.is_external:
            return
        params = event.request_params
        results = event.response_params
        for rv, dv in zip(params.NodesToRead, results or []):
            key = rv.NodeId.to_string()
            metrics.node_reads[key] += 1
            if not dv.StatusCode.is_good():
                metrics.errors_by_type[dv.StatusCode.name] += 1
                metrics.recent_errors.append({
                    "ts": time.time(), "service": "Read", "peer": None,
                    "status": dv.StatusCode.name, "node": key,
                })

    async def on_post_write(event, _service):
        if not event.is_external:
            return
        params = event.request_params
        results = event.response_params
        for wv, status in zip(params.NodesToWrite, results or []):
            key = wv.NodeId.to_string()
            metrics.node_writes[key] += 1
            if not status.is_good():
                metrics.errors_by_type[status.name] += 1
                metrics.recent_errors.append({
                    "ts": time.time(), "service": "Write", "peer": None,
                    "status": status.name, "node": key,
                })

    async def on_item_created(event, _service):
        results = event.response_params or []
        metrics.monitored_items_created += sum(1 for r in results if r.StatusCode.is_good())

    async def on_item_deleted(event, _service):
        results = event.response_params or []
        metrics.monitored_items_deleted += sum(1 for r in results if r.is_good())

    server.subscribe_server_callback(CallbackType.PostRead, on_post_read)
    server.subscribe_server_callback(CallbackType.PostWrite, on_post_write)
    server.subscribe_server_callback(CallbackType.ItemSubscriptionCreated, on_item_created)
    server.subscribe_server_callback(CallbackType.ItemSubscriptionDeleted, on_item_deleted)


# ─── Snapshot aggregation ────────────────────────────────────────────────────
# Cheap per-request instrumentation above only does O(1) counter/dict
# updates. Cross-referencing (sorting, joining node ids against device/tag
# names) happens here instead, once per second (see metrics_loop() in
# opcua_simulator.py) rather than once per request.

def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * pct))
    return s[idx]


def _identifier_from_nodeid_string(s: str) -> str:
    """Reverse of NodeId.to_string()'s "ns=<idx>;s=<identifier>" format —
    every device/tag node in this simulator is a string NodeId (lib/nodes.py),
    always in a registered (non-zero) namespace, so this format is stable."""
    if ";s=" in s:
        return s.split(";s=", 1)[1]
    if s.startswith("s="):
        return s[2:]
    return s


def _node_label(nodeid_str: str, device_names: dict[str, str]) -> dict:
    """Node identifiers are built as "{device.key}/{tag.name}" (lib/nodes.py
    create_tag()) — split back into a human-readable device/tag pair where
    possible, falling back to the raw identifier for anything else (server
    built-ins, NodeSet-imported nodes with a different identifier scheme)."""
    ident = _identifier_from_nodeid_string(nodeid_str)
    device_key, sep, tag_name = ident.partition("/")
    if sep:
        return {"node": nodeid_str, "device": device_names.get(device_key, device_key), "tag": tag_name}
    return {"node": nodeid_str, "device": None, "tag": ident}


async def build_metrics_snapshot(engine: Any, metrics: Metrics) -> dict:
    now = time.time()
    devices = await asyncio.to_thread(engine.db.get_devices)
    device_names = {d["key"]: d["name"] for d in devices}

    iserver = engine.server.iserver if engine.server else None
    sessions = list(iserver._external_sessions.values()) if iserver else []
    subscriptions = list(iserver.subscription_service.subscriptions.values()) if iserver else []

    recent_1s = [r for r in metrics.recent_requests if now - r["ts"] <= 1.0]
    active_clients = [p for p, ts in metrics.clients_seen.items() if now - ts <= 30.0]
    lat = list(metrics.latencies_ms)
    error_count_recent = sum(1 for e in metrics.recent_errors if now - e["ts"] <= 60.0)
    total_count_recent = sum(1 for r in metrics.recent_requests if now - r["ts"] <= 60.0)

    total_monitored_items = sum(len(s.monitored_item_srv._monitored_items) for s in subscriptions)

    accessed_nodes = set(metrics.node_reads) | set(metrics.node_writes)
    top_nodes = sorted(
        ((k, metrics.node_reads.get(k, 0) + metrics.node_writes.get(k, 0)) for k in accessed_nodes),
        key=lambda kv: kv[1], reverse=True,
    )[:15]
    top_read_nodes = sorted(metrics.node_reads.items(), key=lambda kv: kv[1], reverse=True)[:15]
    top_written_nodes = sorted(metrics.node_writes.items(), key=lambda kv: kv[1], reverse=True)[:15]

    top_clients = sorted(
        ((p, sum(1 for r in metrics.recent_requests if r["peer"] == p)) for p in metrics.clients_seen),
        key=lambda kv: kv[1], reverse=True,
    )[:10]

    active_alarms = [a for a in metrics.alarms.values() if a["cleared_ts"] is None]
    ack_times = list(metrics.alarm_ack_times_s)

    return {
        "ts": now,
        "overview": {
            "total_servers": 1,
            "active_sessions": len(sessions),
            "connected_clients": len(active_clients),
            "subscriptions": len(subscriptions),
            "monitored_items": total_monitored_items,
            "requests_per_sec": len(recent_1s),
            "active_alarms": len(active_alarms),
        },
        "traffic": {
            "requests_total": metrics.requests_total,
            "requests_ok": metrics.requests_ok,
            "requests_failed": metrics.requests_failed,
            "requests_by_service": dict(metrics.requests_by_service),
            "reads_total": metrics.reads_total,
            "writes_total": metrics.writes_total,
            "browse_total": metrics.browse_total,
            "call_total": metrics.call_total,
            "top_clients": [{"client": c, "count": n} for c, n in top_clients],
            "top_nodes": [{**_node_label(k, device_names), "count": c} for k, c in top_nodes],
            "recent_requests": list(metrics.recent_requests)[-100:],
        },
        "sessions": {
            "active": len(sessions),
            "created_total": metrics.sessions_created,
            "closed_total": metrics.sessions_closed,
            "avg_duration_s": round(sum(metrics.session_durations_s) / len(metrics.session_durations_s), 1)
            if metrics.session_durations_s else 0.0,
            "list": [
                {
                    "session_id": s.session_id.to_string(),
                    "peer": _fmt_peer(s.name),
                    "state": s.state.name,
                    "user_role": s.user.role.name if getattr(s, "user", None) and getattr(s.user, "role", None) else None,
                    "timeout_s": s.session_timeout,
                }
                for s in sessions
            ],
            "recent_events": list(metrics.recent_session_events)[-100:],
        },
        "nodes": {
            "top_read": [{**_node_label(k, device_names), "count": c} for k, c in top_read_nodes],
            "top_written": [{**_node_label(k, device_names), "count": c} for k, c in top_written_nodes],
            "reads_total": sum(metrics.node_reads.values()),
            "writes_total": sum(metrics.node_writes.values()),
            "distinct_nodes_accessed": len(accessed_nodes),
        },
        "subscriptions": {
            "active": len(subscriptions),
            "monitored_items": total_monitored_items,
            "monitored_items_created_total": metrics.monitored_items_created,
            "monitored_items_deleted_total": metrics.monitored_items_deleted,
            "dropped_notifications": metrics.dropped_notifications,
            "list": [
                {
                    "subscription_id": s.data.SubscriptionId,
                    "session_id": s.session_id.to_string(),
                    "publishing_interval_ms": s.data.RevisedPublishingInterval,
                    "monitored_item_count": len(s.monitored_item_srv._monitored_items),
                    "avg_queue_size": round(
                        sum(m.queue_size for m in s.monitored_item_srv._monitored_items.values())
                        / len(s.monitored_item_srv._monitored_items), 1,
                    ) if s.monitored_item_srv._monitored_items else 0.0,
                }
                for s in subscriptions
            ],
        },
        "performance": {
            "avg_response_time_ms": round(sum(lat) / len(lat), 2) if lat else 0.0,
            "p95_response_time_ms": round(_percentile(lat, 0.95), 2),
            "p99_response_time_ms": round(_percentile(lat, 0.99), 2),
            "requests_per_sec": len(recent_1s),
            "concurrent_clients": len(active_clients),
            "cpu_percent": _PROCESS.cpu_percent(interval=None),
            "memory_mb": round(_PROCESS.memory_info().rss / (1024 * 1024), 1),
            "error_rate_percent": round(100 * error_count_recent / total_count_recent, 2) if total_count_recent else 0.0,
        },
        "errors": {
            "total": sum(metrics.errors_by_type.values()),
            "by_type": dict(metrics.errors_by_type),
            "recent": list(metrics.recent_errors)[-50:],
        },
        "security": {
            "policy": ", ".join(p.name for p in engine.server._security_policy) if engine.server else "Unknown",
            "anonymous_allowed": bool(iserver and ua.AnonymousIdentityToken in iserver.supported_tokens),
            "secure_channel_opens": metrics.secure_channel_opens,
            "auth_failures": metrics.auth_failures,
            "rejected_connections": metrics.rejected_connections,
        },
        "alarms": {
            "active": len(active_alarms),
            "active_list": sorted(active_alarms, key=lambda a: a["opened_ts"], reverse=True),
            "avg_ack_time_s": round(sum(ack_times) / len(ack_times), 1) if ack_times else 0.0,
            "recent_events": list(metrics.alarm_events)[-100:],
        },
    }


def install_analytics(server: Server, metrics: Metrics) -> None:
    """Wire every instrumentation hook against a live Server instance. Call
    once, before server.start() so hooks are live before any client can
    connect. The class-level patches are idempotent (guarded by a flag on
    the class itself), so calling this more than once across multiple
    Server instances in the same process is safe."""
    _install_callbacks(server, metrics)
    _install_uaprocessor_patch(metrics)
    _install_session_patches(metrics)
    _install_subscription_patch(metrics)


# ─── Dashboard alarm feed (derived from FaultBehavior) ──────────────────────
# This proxy reuses FaultBehavior's own _fault_active flag (a real concept in
# the sim: a tag's value generator going into a fault state) so "alarms"
# reflect actual simulated fault conditions rather than being fabricated from
# nothing. This feeds the dashboard's own Alarm & Event Analytics panel only.
# The same open/clear edges also drive lib/alarms.py's fire_alarm_condition(),
# which fires a real, spec-shaped OPC UA AlarmConditionType event any real
# client can subscribe to (v1: no Acknowledge/Confirm or ConditionRefresh —
# see lib/alarms.py's module docstring) — the two are separate call sites in
# opcua_simulator.py, kept independent so a failure in one can't affect the
# other.

def record_fault_transition(
    metrics: Metrics, tag_id: int, tag_name: str, device_name: str,
    is_active: bool, was_active: bool, fault_type: str,
) -> None:
    now = time.time()
    if is_active and not was_active:
        severity = "critical" if fault_type == "offline" else "warning"
        record = {
            "tag_id": tag_id, "tag_name": tag_name, "device_name": device_name,
            "fault_type": fault_type, "severity": severity,
            "opened_ts": now, "acknowledged": False, "acknowledged_ts": None, "cleared_ts": None,
        }
        metrics.alarms[tag_id] = record
        metrics.alarm_events.append({"ts": now, "event": "open", **record})
    elif not is_active and was_active:
        record = metrics.alarms.get(tag_id)
        if record is not None:
            record["cleared_ts"] = now
            metrics.alarm_events.append({"ts": now, "event": "clear", **record})


def acknowledge_alarm(metrics: Metrics, tag_id: int) -> Optional[dict]:
    record = metrics.alarms.get(tag_id)
    if record is None or record["acknowledged"]:
        return None
    now = time.time()
    record["acknowledged"] = True
    record["acknowledged_ts"] = now
    metrics.alarm_ack_times_s.append(now - record["opened_ts"])
    metrics.alarm_events.append({"ts": now, "event": "ack", **record})
    return record
