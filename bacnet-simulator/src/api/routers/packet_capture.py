from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response

from ...bacnet.packet_capture import PacketCapture


router = APIRouter(
    prefix="/packet-capture",
    tags=["packet-capture"],
)


def get_packet_capture(request: Request) -> PacketCapture:
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


async def _load_devices_by_instance(request: Request) -> dict:
    """One query (not one per packet) building device_instance -> device
    row, used for the Who-Is/I-Am/device-object association path. Off the
    event loop via asyncio.to_thread, matching the established convention
    for Database calls elsewhere (e.g. devices.py) -- unlike
    resolve_object()/resolve_wire_object(), this doesn't touch live engine
    state, so it's safe (and correct) to run on a worker thread."""
    database = get_database(request)

    if database is None:
        return {}

    devices = await asyncio.to_thread(database.get_devices)
    return {d["device_instance"]: d for d in devices}


def _empty_simulator_fields() -> dict:
    return {
        "simulator_device_id": None,
        "simulator_device_instance": None,
        "simulator_device_name": None,
        "simulator_object_id": None,
        "simulator_object_type": None,
        "simulator_object_instance": None,
        "simulator_object_name": None,
    }


def resolve_packet_simulator_context(
    packet: dict,
    *,
    devices_by_instance: dict,
    resolve_object: Callable[[str, int], Optional[dict]],
) -> None:
    """
    Mutates `packet` in place, attaching simulator device/object identity --
    but only when the decoded BACnet content gives enough evidence. Every
    field defaults to None and stays None unless one of these evidence-gated
    cases applies (checked in order):

      1. I-Am with a device_instance -> direct device lookup (I-Am is
         always sent BY one specific device).
      2. A directed Who-Is (range low == high) -> same device lookup.
      3. object_type == "device" -> object_instance IS the real
         device_instance (device objects keep it unmangled on the wire,
         unlike regular objects -- see SimEngine._create_object), so this
         resolves directly rather than via resolve_object().
      4. Any other object_type/object_instance -> resolve_object() (wraps
         SimEngine.resolve_wire_object, the only thing that knows the live
         wire-instance mapping).

    Anything else (RPM/WPM/SubscribeCOV/ReadRange -- not decoded into
    object fields -- broadcast Who-Is, or a packet with no usable summary
    at all) is left fully unassociated. The packet is never dropped, only
    left with simulator_* fields set to None.
    """
    packet.update(_empty_simulator_fields())

    summary = packet.get("summary") or {}
    operation = summary.get("operation")

    device = None

    if operation == "I-Am":
        device_instance = summary.get("device_instance")
        if device_instance is not None:
            device = devices_by_instance.get(int(device_instance))

    elif operation == "Who-Is":
        low = summary.get("device_instance_range_low")
        high = summary.get("device_instance_range_high")
        if low is not None and high is not None and int(low) == int(high):
            device = devices_by_instance.get(int(low))

    if device is not None:
        packet["simulator_device_id"] = device["id"]
        packet["simulator_device_instance"] = device["device_instance"]
        packet["simulator_device_name"] = device["name"]
        return

    object_type = summary.get("object_type")
    object_instance = summary.get("object_instance")

    if object_type is None or object_instance is None:
        return

    if str(object_type) == "device":
        device = devices_by_instance.get(int(object_instance))
        if device is not None:
            packet["simulator_device_id"] = device["id"]
            packet["simulator_device_instance"] = device["device_instance"]
            packet["simulator_device_name"] = device["name"]
        return

    context = resolve_object(str(object_type), int(object_instance))

    if context is None:
        return

    packet["simulator_device_id"] = context.get("device_id")
    packet["simulator_device_instance"] = context.get("device_instance")
    packet["simulator_device_name"] = context.get("device_name")
    packet["simulator_object_id"] = context.get("object_id")
    packet["simulator_object_type"] = context.get("object_type")
    packet["simulator_object_instance"] = context.get("object_instance")
    packet["simulator_object_name"] = context.get("object_name")


def make_object_resolver(engine) -> Callable[[str, int], Optional[dict]]:
    """
    Memoized wrapper around engine.resolve_wire_object() -- a capture page
    typically references only a handful of distinct objects even across
    many packets (repeated ReadProperty/ACK pairs for the same monitored
    points), so this caps SQL queries at the number of DISTINCT objects
    actually referenced in one request, not one per packet. The cache is a
    fresh dict per call to this factory (i.e. per request) -- never
    persisted across requests, so it can't go stale across reloads/project
    loads.

    Calls engine.resolve_wire_object() directly/synchronously, never via
    asyncio.to_thread: it reads SimEngine._objects, which SimEngine.reload()
    mutates in place on the event loop. Dispatching it to a worker thread
    would introduce a cross-thread race that doesn't exist today (the
    existing single-packet detail route already calls it synchronously).
    """
    cache: dict[tuple[str, int], Optional[dict]] = {}

    def resolve(object_type: str, object_instance: int) -> Optional[dict]:
        key = (object_type, object_instance)

        if key not in cache:
            if engine is None:
                cache[key] = None
            else:
                try:
                    cache[key] = engine.resolve_wire_object(
                        object_type=object_type,
                        physical_instance=object_instance,
                    )
                except Exception:
                    cache[key] = None

        return cache[key]

    return resolve


@router.post("/test-packet")
async def packet_capture_test_packet(
    request: Request,
):
    capture = get_packet_capture(request)

    sample_who_is = bytes.fromhex(
        "81 0b 00 0c 01 20 ff ff 00 ff 10 08"
    )

    capture.record_inbound(
        sample_who_is,
        source=("10.0.0.60", 47810),
        destination=("255.255.255.255", 47808),
    )

    return capture.status()


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
    source_ip: Optional[str] = None,
    destination_ip: Optional[str] = None,
    service: Optional[str] = None,
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
    devices_by_instance = await _load_devices_by_instance(request)
    resolve_object = make_object_resolver(get_engine(request))

    def packet_hook(packet: dict) -> bool:
        resolve_packet_simulator_context(
            packet,
            devices_by_instance=devices_by_instance,
            resolve_object=resolve_object,
        )

        if device_id is not None and packet.get("simulator_device_id") != device_id:
            return False

        if unassociated and packet.get("simulator_device_id") is not None:
            return False

        return True

    return capture.list_packets(
        direction=direction,
        source_ip=source_ip,
        destination_ip=destination_ip,
        service=service,
        offset=offset,
        limit=limit,
        packet_hook=packet_hook,
    )


@router.get("/packets/{packet_id}")
async def packet_capture_packet(
    packet_id: str,
    request: Request,
):
    capture = get_packet_capture(request)
    packet = capture.get_packet(packet_id)

    if packet is None:
        raise HTTPException(
            status_code=404,
            detail="Captured packet not found",
        )

    devices_by_instance = await _load_devices_by_instance(request)

    resolve_packet_simulator_context(
        packet,
        devices_by_instance=devices_by_instance,
        resolve_object=make_object_resolver(get_engine(request)),
    )

    return packet


@router.get("/export")
async def packet_capture_export(
    request: Request,
):
    capture = get_packet_capture(request)
    data = capture.export_pcap()

    filename = datetime.now(
        timezone.utc
    ).strftime(
        "bacnet-capture-%Y%m%dT%H%M%SZ.pcap"
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