"""Analytics dashboard routes — split out of opcua_simulator.py.
Fully separate from the /ws + tick_loop() device-simulation path — different
cadence (1s vs TICK_SECONDS=5s), different client list, so the dashboard can
never add latency to actual device-value simulation. See lib/analytics.py.
"""
import asyncio
import csv
import io
import json
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

import lib.state as state
from lib.analytics import acknowledge_alarm, build_metrics_snapshot
from lib.db import user_from_token

router = APIRouter()


@router.get("/analytics/snapshot")
async def analytics_snapshot():
    return await build_metrics_snapshot(state.engine, state.metrics)


@router.get("/analytics/export")
async def analytics_export(format: str = "json"):
    snapshot = await build_metrics_snapshot(state.engine, state.metrics)
    if format == "json":
        return JSONResponse(content=snapshot)

    if format != "csv":
        raise HTTPException(400, "format must be 'json' or 'csv'")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["section", "key", "value"])
    for section, payload in snapshot.items():
        if section == "ts":
            continue
        if isinstance(payload, dict):
            for k, v in payload.items():
                if isinstance(v, (dict, list)):
                    continue
                writer.writerow([section, k, v])
    content = buf.getvalue()
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="analytics_{int(time.time())}.csv"'},
    )


@router.post("/analytics/alarms/{tag_id}/ack")
async def analytics_ack_alarm(tag_id: int):
    record = acknowledge_alarm(state.metrics, tag_id)
    if record is None:
        raise HTTPException(404, "No open, unacknowledged alarm for this tag")
    return record


@router.websocket("/ws/analytics")
async def ws_analytics_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    if not await asyncio.to_thread(user_from_token, state.db, token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    state.metrics_ws_clients.append(websocket)
    try:
        await websocket.send_text(json.dumps(await build_metrics_snapshot(state.engine, state.metrics)))
        while True:
            await websocket.receive_text()  # keep alive (ping)
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in state.metrics_ws_clients:
            state.metrics_ws_clients.remove(websocket)
