from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from typing import Any, Awaitable, Callable, MutableSequence

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse


router = APIRouter(tags=["analytics"])


SnapshotBuilder = Callable[[], Awaitable[dict]]
TokenResolver = Callable[[str], dict | None]


# ─── Runtime dependencies ─────────────────────────────────────────────────────

def get_snapshot_builder(
    request: Request,
) -> SnapshotBuilder:
    builder = getattr(
        request.app.state,
        "build_metrics_snapshot",
        None,
    )

    if builder is None:
        raise HTTPException(
            status_code=503,
            detail="Analytics service is unavailable",
        )

    return builder


def get_websocket_snapshot_builder(
    websocket: WebSocket,
) -> SnapshotBuilder | None:
    return getattr(
        websocket.app.state,
        "build_metrics_snapshot",
        None,
    )


def get_token_resolver(
    websocket: WebSocket,
) -> TokenResolver | None:
    return getattr(
        websocket.app.state,
        "user_from_token",
        None,
    )


def get_metrics_clients(
    websocket: WebSocket,
) -> MutableSequence[WebSocket] | None:
    return getattr(
        websocket.app.state,
        "metrics_ws_clients",
        None,
    )


# ─── Snapshot and export ──────────────────────────────────────────────────────

@router.get("/analytics/snapshot")
async def analytics_snapshot(
    request: Request,
):
    build_snapshot = get_snapshot_builder(request)
    return await build_snapshot()


@router.get("/analytics/export")
async def analytics_export(
    request: Request,
    format: str = "json",
):
    build_snapshot = get_snapshot_builder(request)
    snapshot = await build_snapshot()

    normalized_format = format.strip().lower()

    if normalized_format == "json":
        return JSONResponse(content=snapshot)

    if normalized_format != "csv":
        raise HTTPException(
            status_code=400,
            detail="format must be 'json' or 'csv'",
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(
        [
            "section",
            "key",
            "value",
        ]
    )

    for section, payload in snapshot.items():
        if section == "ts":
            continue

        if not isinstance(payload, dict):
            continue

        for key, value in payload.items():
            # Preserve the current export behavior: nested data is
            # omitted from CSV rather than flattened.
            if isinstance(value, (dict, list)):
                continue

            writer.writerow(
                [
                    section,
                    key,
                    value,
                ]
            )

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                "attachment; "
                f'filename="analytics_{int(time.time())}.csv"'
            ),
        },
    )


# ─── Live analytics WebSocket ─────────────────────────────────────────────────

@router.websocket("/ws/analytics")
async def analytics_websocket(
    websocket: WebSocket,
):
    resolve_token = get_token_resolver(websocket)
    build_snapshot = get_websocket_snapshot_builder(
        websocket
    )
    clients = get_metrics_clients(websocket)

    if (
        resolve_token is None
        or build_snapshot is None
        or clients is None
    ):
        await websocket.close(code=1011)
        return

    token = websocket.query_params.get(
        "token",
        "",
    )

    user = await asyncio.to_thread(
        resolve_token,
        token,
    )

    if user is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    clients.append(websocket)

    try:
        await websocket.send_text(
            json.dumps(
                await build_snapshot()
            )
        )

        while True:
            # The background metrics task pushes snapshots.
            # Incoming messages are only used as a keep-alive.
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass

    finally:
        if websocket in clients:
            clients.remove(websocket)