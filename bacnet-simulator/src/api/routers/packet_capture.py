from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
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


def enrich_packet_with_simulator_context(
    packet: dict,
    engine,
) -> dict:
    enriched = dict(packet)
    enriched["simulator_context"] = None

    if engine is None:
        return enriched

    summary = enriched.get("summary") or {}
    service = enriched.get("service") or {}

    object_type = (
        summary.get("object_type")
        or service.get("object_type")
    )

    object_instance = (
        summary.get("object_instance")
        if summary.get("object_instance") is not None
        else service.get("object_instance")
    )

    if object_type is None or object_instance is None:
        return enriched

    try:
        enriched["simulator_context"] = (
            engine.resolve_wire_object(
                object_type=str(object_type),
                physical_instance=int(object_instance),
            )
        )
    except Exception:
        # Packet details should still be returned even when
        # simulator-context enrichment fails.
        enriched["simulator_context"] = None

    return enriched


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

    return capture.list_packets(
        direction=direction,
        source_ip=source_ip,
        destination_ip=destination_ip,
        service=service,
        offset=offset,
        limit=limit,
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

    return enrich_packet_with_simulator_context(
        packet,
        get_engine(request),
    )


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