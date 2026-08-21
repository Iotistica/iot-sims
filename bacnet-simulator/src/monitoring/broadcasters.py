"""WebSocket broadcast helpers.

Physically extracted from src/legacy.py -- continuing the GH #15 refactor,
same "moved verbatim, no behavior changes" standard as the Database and
simulation-engine extractions. broadcast_metrics itself lives in
.metrics (it calls build_metrics_snapshot, same module/domain) and is just
re-exported here to match this facade's original __all__.
"""
from __future__ import annotations

import asyncio
import json

from ..bacnet.packet_capture import CapturedPacket
from ..api.routers.packet_capture import resolve_packet_simulator_context
from ..core.logging import log
from .metrics import broadcast_metrics


def _dependencies():
    """
    Resolve src.dependencies lazily.

    IMPORTANT:
    Do not import src.dependencies at module import time.

    src.dependencies imports this module (for _on_packet_captured) during
    its own startup. If this module imports src.dependencies eagerly,
    Python re-enters the partially initialized dependencies module and
    raises a circular-import ImportError.

    These broadcasters read the `ws_clients`/`engine`/`packet_stream_ws_clients`
    module globals, which live in src.dependencies. Resolving it lazily is
    what lets this keep working.
    """
    from .. import dependencies
    return dependencies


# ─── WebSocket broadcaster ────────────────────────────────────────────────────

async def broadcast_state() -> None:
    dependencies = _dependencies()
    ws_clients = dependencies.ws_clients
    if not ws_clients:
        return

    data = json.dumps(
        dependencies.engine.get_state()
    )

    dead_clients = []

    for websocket in list(ws_clients):
        try:
            await websocket.send_text(data)
        except Exception:
            dead_clients.append(websocket)

    for websocket in dead_clients:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


# ─── Packet capture stream broadcaster ─────────────────────────────────────────

async def broadcast_captured_packet(packet: CapturedPacket) -> None:
    dependencies = _dependencies()
    packet_stream_ws_clients = dependencies.packet_stream_ws_clients
    if not packet_stream_ws_clients:
        return

    try:
        payload = packet.to_dict(include_hex=True)

        # Cheap, in-memory device-only association (I-Am / directed Who-Is /
        # device-object) -- see plan notes. The no-op resolver skips the
        # expensive per-object DB/O(N) path entirely for live packets;
        # ordinary point-level traffic stays unassociated until the next
        # REST fetch.
        engine = dependencies.engine
        resolve_packet_simulator_context(
            payload,
            devices_by_instance=(
                engine.get_devices_by_instance() if engine else {}
            ),
            resolve_object=lambda *_args, **_kwargs: None,
        )

        data = json.dumps(payload)
    except Exception as exc:
        # Never let a malformed/unexpected packet break the capture path --
        # matches _record()'s own invariant -- but don't fail silently either.
        log.debug("packet-capture stream: failed to prepare packet: %s", exc)
        return

    dead_clients = []

    for websocket in list(packet_stream_ws_clients):
        try:
            await websocket.send_text(data)
        except Exception:
            dead_clients.append(websocket)

    for websocket in dead_clients:
        if websocket in packet_stream_ws_clients:
            packet_stream_ws_clients.remove(websocket)


def _on_packet_captured(packet: CapturedPacket) -> None:
    if _dependencies().packet_stream_ws_clients:  # skip task creation with nobody listening
        asyncio.create_task(broadcast_captured_packet(packet))


__all__ = ["broadcast_state", "broadcast_metrics", "broadcast_captured_packet", "_on_packet_captured"]
