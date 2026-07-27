"""
Shared mutable runtime state for the OPC UA Simulator's FastAPI app.

opcua_simulator.py was growing into a single-file monolith as routes kept
getting added (devices/tags, profiles, NodeSet import, folders, analytics...).
Route handlers now live in separate lib/routes_*.py modules (FastAPI
APIRouters, mounted in opcua_simulator.py) — this module is what lets them
all see the same `db`/`engine`/`metrics` instances without a circular import
back to the main file.

Access as `import lib.state as state; state.db` (not `from lib.state import
db`) everywhere — `db`/`engine` are reassigned once at startup in
opcua_simulator.py's lifespan(), and a `from` import would freeze in the
None placeholder from before that assignment ran.
"""
import time
from collections import deque
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException, Request, WebSocket

from lib.db import Database, user_from_token

if TYPE_CHECKING:
    from lib.analytics import Metrics
    from opcua_simulator import SimEngine

db: Optional[Database] = None
engine: Optional["SimEngine"] = None
metrics: Optional["Metrics"] = None

ws_clients: list[WebSocket] = []
metrics_ws_clients: list[WebSocket] = []

_device_logs: dict[int, deque] = {}
_global_log: deque = deque(maxlen=1000)
_device_names: dict[int, str] = {}
_MAX_LOG = 300


def log_event(device_id: int, level: str, message: str) -> None:
    entry = {
        "ts": time.time(),
        "level": level,
        "device_id": device_id,
        "device_name": _device_names.get(device_id, f"#{device_id}"),
        "message": message,
    }
    _device_logs.setdefault(device_id, deque(maxlen=_MAX_LOG)).append(entry)
    _global_log.append(entry)


def track(coro, description: str) -> None:
    """asyncio.create_task() swallows exceptions unless something awaits the
    task or checks its result — several endpoints fire structural live-node
    mutations without awaiting them (the DB write already succeeded and is
    the source of truth; the REST response shouldn't wait on OPC UA node
    creation). This makes sure a failure there still gets logged with a
    stack trace instead of vanishing silently."""
    import asyncio
    import logging

    log = logging.getLogger("opcua-sim")
    task = asyncio.create_task(coro)

    def _on_done(t: "asyncio.Task") -> None:
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            log.error("Background task failed (%s):", description, exc_info=exc)

    task.add_done_callback(_on_done)


def get_current_user(request: Request) -> dict:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = user_from_token(db, auth_header[7:].strip())
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user
