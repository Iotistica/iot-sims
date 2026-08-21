"""Metrics model and aggregation helpers.

Physically extracted from src/legacy.py -- continuing the GH #15 refactor,
same "moved verbatim, no behavior changes" standard as the Database and
simulation-engine extractions.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque

import psutil
from bacpypes3.primitivedata import ObjectIdentifier


def _dependencies():
    """
    Resolve src.dependencies lazily.

    IMPORTANT:
    Do not import src.dependencies at module import time.

    src.dependencies imports this module (for `metrics`) during its own
    startup. If this module imports src.dependencies eagerly, Python
    re-enters the partially initialized dependencies module and raises a
    circular-import ImportError.

    build_metrics_snapshot()/broadcast_metrics() read the `db`/`engine`/
    `metrics_ws_clients` module globals, which live in src.dependencies.
    Resolving it lazily is what lets this keep working.
    """
    from .. import dependencies
    return dependencies


# ─── Analytics metrics store ───────────────────────────────────────────────────
# In-memory only (never persisted), same pattern as the event log — reset
# on process restart. Plain dict/deque mutations only, no locks needed (single
# asyncio event loop) and no per-request DB writes, so this stays cheap enough
# to not affect simulator performance.

class Metrics:
    def __init__(self) -> None:
        self.start_time = time.time()
        # (str(pduSource), invoke_id) -> {device, object, service, started}
        # stamped at request-start, popped in SimApplication.response()
        self.pending: dict[tuple, dict] = {}

        self.requests_total = 0
        self.requests_by_service: dict[str, int] = defaultdict(int)
        self.requests_by_device: dict[int, int] = defaultdict(int)
        self.requests_broadcast = 0
        self.requests_unicast = 0
        self.reads_total = 0
        self.writes_total = 0

        # "reject:<reason>" / "abort:<reason>" / "error:<class>.<code>"
        self.errors_by_type: dict[str, int] = defaultdict(int)
        self.recent_errors: deque = deque(maxlen=200)

        self.object_reads: dict[int, int] = defaultdict(int)
        self.object_writes: dict[int, int] = defaultdict(int)

        self.discovery_total = 0
        self.iam_seen: dict[int, float] = {}          # device_instance -> last-seen ts
        self.new_devices_timeline: deque = deque(maxlen=200)
        self.duplicate_id_events: deque = deque(maxlen=100)

        self.recent_requests: deque = deque(maxlen=500)   # live traffic feed
        self.latencies_ms: deque = deque(maxlen=500)
        self.clients_seen: dict[str, float] = {}            # source addr -> last-seen ts


metrics = Metrics()


# ─── Analytics aggregation ─────────────────────────────────────────────────────
# Per-request instrumentation (in SimApplication) only does cheap O(1)
# counter/dict updates. All cross-referencing and sorting — which is more
# expensive but still bounded (object counts are small; deques capped at
# <=500) — happens here instead, once per metrics tick rather than once per
# BACnet request, so it can't add per-request latency to the simulator.

_PROCESS = psutil.Process()


def _object_to_device_map() -> dict[str, int]:
    """Reverse of engine.app._virtual_object_lists (device -> [objid]),
    rebuilt each tick rather than cached, since it's cheap and always in sync
    with the current device set (no invalidation-on-reload bookkeeping
    needed)."""
    mapping: dict[str, int] = {}
    engine = _dependencies().engine
    if engine is None or engine.app is None:
        return mapping
    for did, objids in engine.app._virtual_object_lists.items():
        for objid in objids:
            mapping[str(ObjectIdentifier(objid))] = did
    return mapping


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(len(s) * pct))
    return s[idx]


async def build_metrics_snapshot() -> dict:
    dependencies = _dependencies()
    db = dependencies.db
    engine = dependencies.engine

    now = time.time()
    devices = await asyncio.to_thread(db.get_devices) if db is not None else []
    obj_to_device = _object_to_device_map()

    # Overview
    recent_1s = [r for r in metrics.recent_requests if now - r["ts"] <= 1.0]
    active_clients = [addr for addr, ts in metrics.clients_seen.items() if now - ts <= 30.0]
    online_devices = sum(1 for d in devices if d.get("enabled"))
    active_alarms = sum(
        1 for dev in engine.get_state().get("devices", []) if engine is not None
        for o in dev["objects"] if o.get("behavior") == "fault"
    ) if engine is not None else 0

    # Traffic
    device_activity: dict[int, int] = defaultdict(int)
    for objid_key, count in list(metrics.object_reads.items()) + list(metrics.object_writes.items()):
        did = obj_to_device.get(objid_key)
        if did is not None:
            device_activity[did] += count
    top_devices = sorted(device_activity.items(), key=lambda kv: kv[1], reverse=True)[:10]
    device_names = {d["device_instance"]: d["name"] for d in devices}

    # Object analytics
    all_objids = set(obj_to_device.keys())
    accessed_objids = set(metrics.object_reads.keys()) | set(metrics.object_writes.keys())
    unused_objects = len(all_objids - accessed_objids)
    top_objects = sorted(
        ((k, metrics.object_reads.get(k, 0) + metrics.object_writes.get(k, 0)) for k in accessed_objids),
        key=lambda kv: kv[1], reverse=True,
    )[:15]

    # Performance
    lat = list(metrics.latencies_ms)
    error_count_recent = sum(1 for e in metrics.recent_errors if now - e["ts"] <= 60.0)
    total_count_recent = sum(1 for r in metrics.recent_requests if now - r["ts"] <= 60.0)

    return {
        "ts": now,
        "overview": {
            "total_devices": len(devices),
            "online_devices": online_devices,
            "offline_devices": len(devices) - online_devices,
            "active_clients": len(active_clients),
            "requests_per_sec": len(recent_1s),
            "avg_response_time_ms": round(sum(lat) / len(lat), 2) if lat else 0.0,
            "active_alarms": active_alarms,
        },
        "traffic": {
            "requests_total": metrics.requests_total,
            "reads_total": metrics.reads_total,
            "writes_total": metrics.writes_total,
            "requests_by_service": dict(metrics.requests_by_service),
            "broadcast": metrics.requests_broadcast,
            "unicast": metrics.requests_unicast,
            "top_devices": [
                {"device_instance": did, "name": device_names.get(did, f"#{did}"), "count": c}
                for did, c in top_devices
            ],
            "recent_requests": list(metrics.recent_requests)[-100:],
        },
        "devices": {
            "list": [
                {
                    "id": d["id"],
                    "device_instance": d["device_instance"],
                    "name": d["name"],
                    "enabled": bool(d.get("enabled")),
                    "object_count": len(engine.app._virtual_object_lists.get(d["device_instance"], [])) if engine and engine.app else 0,
                    "activity": device_activity.get(d["device_instance"], 0),
                }
                for d in devices
            ],
            "uptime_seconds": engine.state.elapsed_seconds if engine else 0,
        },
        "objects": {
            "total": len(all_objids),
            "unused": unused_objects,
            "top_accessed": [{"object": k, "count": c} for k, c in top_objects],
            "reads_total": sum(metrics.object_reads.values()),
            "writes_total": sum(metrics.object_writes.values()),
        },
        "performance": {
            "avg_response_time_ms": round(sum(lat) / len(lat), 2) if lat else 0.0,
            "p95_response_time_ms": round(_percentile(lat, 0.95), 2),
            "throughput_per_sec": len(recent_1s),
            "concurrent_clients": len(active_clients),
            "cpu_percent": _PROCESS.cpu_percent(interval=None),
            "memory_mb": round(_PROCESS.memory_info().rss / (1024 * 1024), 1),
            "error_rate_percent": round(100 * error_count_recent / total_count_recent, 2) if total_count_recent else 0.0,
        },
        "errors": {
            "total": sum(metrics.errors_by_type.values()),
            "by_type": dict(metrics.errors_by_type),
            "duplicate_device_ids": list(metrics.duplicate_id_events)[-20:],
            "recent": list(metrics.recent_errors)[-50:],
        },
        "discovery": {
            "who_is_total": metrics.discovery_total,
            "devices_seen": len(metrics.iam_seen),
            "new_devices_timeline": list(metrics.new_devices_timeline)[-50:],
        },
    }


async def broadcast_metrics() -> None:
    dependencies = _dependencies()
    metrics_ws_clients = dependencies.metrics_ws_clients
    if not metrics_ws_clients:
        return
    snapshot = await build_metrics_snapshot()
    data = json.dumps(snapshot)
    dead = []
    for ws in metrics_ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        metrics_ws_clients.remove(ws)


__all__ = ["Metrics", "metrics", "build_metrics_snapshot", "broadcast_metrics"]
