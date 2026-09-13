from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response

from ...network.capture import TrafficCapture
from ...network.registration import EXPORTERS


router = APIRouter(
    prefix="/packet-capture",
    tags=["packet-capture"],
)


def get_packet_capture(request: Request) -> TrafficCapture:
    capture = getattr(
        request.app.state,
        "packet_capture",
        None,
    )

    if capture is None:
        raise HTTPException(
            status_code=503,
            detail="Packet capture is unavailable",
        )

    return capture


def get_engine(request: Request):
    return getattr(
        request.app.state,
        "engine",
        None,
    )


def get_database(request: Request):
    return getattr(
        request.app.state,
        "db",
        None,
    )


async def capture_context(request):
    database = get_database(request)
    devices = await asyncio.to_thread(database.get_devices) if database else []
    return {'devices': devices, 'engine': get_engine(request)}


@router.get("/status")
async def packet_capture_status(
    request: Request,
):
    return get_packet_capture(request).status()


@router.post("/start")
async def packet_capture_start(
    request: Request,
):
    return get_packet_capture(request).start(
        clear_existing=True
    )


@router.post("/stop")
async def packet_capture_stop(
    request: Request,
):
    return get_packet_capture(request).stop()


@router.post("/clear")
async def packet_capture_clear(
    request: Request,
):
    return get_packet_capture(request).clear()


@router.get("/packets")
async def packet_capture_packets(
    request: Request,
    direction: Optional[str] = None,
    protocol: Optional[str] = None,
    operation: Optional[str] = None,
    device_id: Optional[int] = None,
    unassociated: Optional[bool] = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=250),
):
    if direction not in {
        None,
        "inbound",
        "outbound",
    }:
        raise HTTPException(
            status_code=400,
            detail="direction must be inbound or outbound",
        )

    capture = get_packet_capture(request)
    try:
        return capture.list_packets(protocol=protocol, direction=direction, operation=operation,
            device_id=device_id, unassociated=unassociated, offset=offset, limit=limit,
            context=await capture_context(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/packets/{packet_id}")
async def packet_capture_packet(
    packet_id: str,
    request: Request,
):
    capture = get_packet_capture(request)
    packet = capture.get_packet(packet_id, context=await capture_context(request))

    if packet is None:
        raise HTTPException(
            status_code=404,
            detail="Captured packet not found",
        )

    return packet


@router.get("/export")
async def packet_capture_export(
    request: Request,
    protocol: str,
):
    capture = get_packet_capture(request)
    if protocol not in EXPORTERS:
        raise HTTPException(status_code=400, detail=f"Unsupported PCAP export protocol: {protocol}")
    try:
        data = EXPORTERS[protocol](capture.snapshot())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename = datetime.now(
        timezone.utc
    ).strftime(
        f"{protocol}-capture-%Y%m%dT%H%M%SZ.pcap"
    )

    return Response(
        content=data,
        media_type="application/vnd.tcpdump.pcap",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
        },
    )


def get_token_resolver(websocket: WebSocket):
    return getattr(websocket.app.state, "user_from_token", None)


def get_packet_stream_clients(websocket: WebSocket):
    return getattr(websocket.app.state, "packet_stream_ws_clients", None)


@router.websocket("/stream")
async def packet_capture_stream(websocket: WebSocket):
    resolve_token = get_token_resolver(websocket)
    clients = get_packet_stream_clients(websocket)

    if resolve_token is None or clients is None:
        await websocket.close(code=1011)
        return

    token = websocket.query_params.get("token", "")
    user = await asyncio.to_thread(resolve_token, token)

    if user is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    clients.append(websocket)

    try:
        # No replay: only packets captured after this connection is
        # accepted are ever sent. Inbound messages are only used to
        # detect disconnects.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        # Mirrors /ws (simulation_websocket) -- a dropped connection
        # doesn't always surface as WebSocketDisconnect (e.g. browser
        # refresh).
        pass
    finally:
        if websocket in clients:
            clients.remove(websocket)