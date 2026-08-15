from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fcntl
import ipaddress
import logging
import socket
import struct
from typing import Any, AsyncIterator, NoReturn

from fastapi import APIRouter, HTTPException, Request, Response

from bacpypes3.app import Application
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.networkport import NetworkPortObject

from ...bacnet.client import Bacpypes3Transport, BACnetDiscovery, DiscoveryOptions
from ...bacnet.schemas import DiscoveryConnectionPayload, DiscoveryTriggerRequest


router = APIRouter(
    prefix="/bacnet-discovery",
    tags=["discovery"],
)

log = logging.getLogger(__name__)


def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return database

# Every discovery call binds the same real port (47808 -- see _run_discovery's
# docstring for why it can't be ephemeral). Two overlapping calls would each
# try to bind that port and collide, which is exactly the "Address already
# in use" BACpypes3 retries forever on (see the finally block below) --
# serializing avoids that regardless of what triggered the overlap (a
# double-click, two browser tabs, etc.).
_discovery_lock = asyncio.Lock()

# Purely a placeholder self-identity for the throwaway discovery client's own
# outbound Who-Is/ReadProperty requests -- not a real simulated or discovered
# device, never persisted, never exposed. Same value already verified live
# against sim-bacnet-1 in this session's acceptance test.
_CLIENT_DEVICE_INSTANCE = 999999


_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B


def _iface_ioctl(sock: socket.socket, request: int, ifname: str) -> str | None:
    try:
        raw = fcntl.ioctl(sock.fileno(), request, struct.pack("256s", ifname[:15].encode()))
        return socket.inet_ntoa(raw[20:24])
    except OSError:
        return None


def _resolve_local_bind_address() -> str:
    """
    Resolves this host's own IP plus its real interface netmask, e.g.
    "172.22.0.7/16", not just a bare IP.

    BACpypes3 only enables broadcast Who-Is when its NetworkPortObject is
    given a real netmask -- a bare IP defaults to /32 (netmask == address),
    and the wildcard "0.0.0.0" has no netmask at all; either one makes
    BACpypes3 skip broadcast sending/receiving entirely (verified live: a
    bare-IP or wildcard bind raises "no broadcast" / silently finds nothing,
    even with a real device up and responding to targeted Who-Is on the same
    subnet). Detected via ioctl (Linux only, matching this image's runtime)
    with a graceful bare-IP fallback if detection fails for any reason --
    targeted discovery doesn't depend on this at all, only the broadcast
    (empty Discovery Target) case does.
    """
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        return "0.0.0.0"
    if not ip or ip == "127.0.0.1":
        return "0.0.0.0"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _, ifname in socket.if_nameindex():
                if _iface_ioctl(s, _SIOCGIFADDR, ifname) != ip:
                    continue
                netmask = _iface_ioctl(s, _SIOCGIFNETMASK, ifname)
                if netmask:
                    prefix_len = ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
                    return f"{ip}/{prefix_len}"
    except Exception:
        pass
    return ip


def _force_close_transports(app: Any) -> None:
    """
    Defensive cleanup for what app.close() can't handle: if a link-layer UDP
    endpoint's creation task hadn't finished yet when close() ran (the
    AttributeError caught below), close() bails out without ever touching
    it -- BACpypes3's own endpoint setup (retrying_create_datagram_endpoint,
    ipv4/__init__.py) then keeps that task retrying forever in the
    background, fully detached from this now-abandoned Application. If it
    eventually succeeds, it binds a real socket nothing else will ever
    release, which then permanently blocks every future discovery call
    trying to bind that same address (verified live: a single failed close()
    left a live socket bound in sim-bacnet's own /proc/net/udp with zero
    live devices, and every discovery call after that failed the same way).
    Reaches into each link layer's server and either cancels the task
    (still pending) or closes the transport it already produced (finished
    just after close() gave up).
    """
    for link_layer in getattr(app, "link_layers", {}).values():
        server = getattr(link_layer, "server", None)
        for task in getattr(server, "_transport_tasks", None) or []:
            if not task.done():
                task.cancel()
                continue
            try:
                transport, _protocol = task.result()
            except Exception:
                continue
            try:
                transport.close()
            except Exception:
                pass


class DiscoveryBindError(RuntimeError):
    pass


def _engine_bacnet_running(request: Request) -> bool | None:
    """
    Whether SimEngine currently has its own BACnet stack bound to port
    47808 -- the real signal for "is something of mine holding the port,"
    and deliberately NOT the same thing as `sim_state`/the Start-Pause-Stop
    simulation clock. Those buttons only pause/reset the tick loop
    (SimEngine.resume()/.pause()/.reset() in simulation.py) -- they never
    touch `engine.app`, which SimEngine only tears down when the project
    has zero *enabled simulated* devices left (see SimEngine.start()'s
    early-return) or on full process shutdown (engine.stop(), never called
    from any user-facing route). A user who clicks Stop and retries
    discovery will hit the exact same bind failure every time -- verified
    live this session. Returns None if the engine isn't reachable at all,
    so the caller can fall back to a generic message instead of asserting a
    possibly-wrong diagnosis.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return None
    return getattr(engine, "app", None) is not None


def raise_discovery_bind_error(request: Request, exc: DiscoveryBindError) -> NoReturn:
    """
    Turns a raw DiscoveryBindError into an HTTPException whose message
    actually matches the cause -- checked via _engine_bacnet_running()
    rather than assumed, since "this simulator's own live devices" is only
    sometimes the real explanation (see its docstring). By the time this can
    fire at all, it's always the broadcast (needs_broadcast=True) session --
    every targeted call (a specific Discovery Target, "Discover Objects",
    live-value refresh) now binds an ephemeral local port and structurally
    cannot hit this (see _discovery_session's docstring), so the message
    below is deliberately scoped to "broadcast discovery" rather than
    "discovery" generally.
    """
    running = _engine_bacnet_running(request)
    if running is True:
        detail = (
            "Could not start a broadcast BACnet discovery scan -- port 47808 is "
            "already bound by this simulator's own BACnet stack, which has "
            "enabled simulated device(s) loaded. A subnet-wide scan (no Discovery "
            "Target) needs the real local port to receive broadcast replies, so it "
            "can't run at the same time as this simulator's own devices -- targeted "
            "discovery against a specific host/IP is unaffected and works "
            "regardless. The Start/Pause/Stop simulation clock does NOT release "
            "this port -- disable or remove every enabled simulated device in this "
            "project if you need a broadcast scan."
        )
    elif running is False:
        detail = (
            "Could not start a broadcast BACnet discovery scan -- port 47808 is in "
            "use, but not by this simulator (it currently has no enabled simulated "
            "devices loaded, so its own BACnet stack is idle). This is likely a "
            "leftover discovery session or another process on this host using the "
            "port -- wait a few seconds and try again; if it persists, restart the "
            "simulator. Targeted discovery against a specific host/IP is "
            "unaffected by this."
        )
    else:
        detail = str(exc)
    raise HTTPException(status_code=503, detail=detail) from exc


async def _wait_for_bind(app: Any, timeout: float = 2.0) -> None:
    """
    BACpypes3's own endpoint binder (retrying_create_datagram_endpoint,
    ipv4/__init__.py) retries forever on OSError with no external failure
    signal -- most commonly because this host's own BACnet port is already
    taken, e.g. this same container currently has live simulated devices of
    its own, so SimEngine's SimApplication is already bound to
    <own-ip>:47808 (verified live: sim-bacnet running 18 enabled devices,
    every discovery call against it silently never bound at all). Without
    this, the client would sit there never actually sending anything, and
    discover() would just time out with an empty result -- indistinguishable
    from "genuinely no devices found". Poll for the bind actually completing,
    bounded, so a real conflict fails fast with a clear, actionable message
    instead.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    for link_layer in getattr(app, "link_layers", {}).values():
        server = getattr(link_layer, "server", None)
        if server is None:
            continue
        while not hasattr(server, "local_transport"):
            if loop.time() >= deadline:
                raise DiscoveryBindError(
                    "Could not start the BACnet discovery client -- port 47808 is "
                    "already in use on this host, most likely by this simulator's "
                    "own live devices. Stop or unload the local simulation before "
                    "running external discovery."
                )
            await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def _discovery_session(*, needs_broadcast: bool = False) -> AsyncIterator[BACnetDiscovery]:
    """
    Builds a short-lived, standalone BACpypes3 Application, yields a
    BACnetDiscovery wrapping it, and guarantees cleanup on exit.

    Deliberately independent of the simulator's own SimEngine/SimApplication
    (see plan notes): sharing that stack would risk a device-instance
    identity collision between the simulator's own virtual devices and real
    external devices being discovered. A fresh Application per discovery
    call keeps external discovery fully isolated from the live simulation.

    Binds to an OS-assigned ephemeral local UDP port by default
    (needs_broadcast=False) rather than the real BACnet port (47808) --
    this lets targeted Who-Is/ReadProperty coexist with the simulator's own
    SimApplication, which stays bound to the real port for its own
    simulated devices. Verified against the pinned bacpypes3==0.0.91:
    IPv4DatagramServer only skips creating a *broadcast-receive* transport
    for a port-0 bind (ipv4/__init__.py: "no broadcast if this is an
    ephemeral port") -- ordinary unicast send/receive over that same
    ephemeral socket works identically to a fixed-port bind, and every
    targeted caller (device-instance Who-Is against a known host, Object_List
    read, per-object property reads, present-value refresh) only ever builds
    an explicit unicast destination, never broadcast. So a project mixing
    simulated + external-BACnet devices no longer has to choose between them
    for any of that.

    needs_broadcast=True is the one remaining exception: a genuine subnet-
    wide discovery scan (no Discovery Target given) needs bacpypes3's own
    broadcast Who-Is collection, which unconditionally requires a real local
    port -- an ephemeral bind cannot receive broadcast I-Am replies at all
    (same source above). That case still binds 47808 and can still collide
    with the simulator's own live devices; _wait_for_bind() turns that
    specific conflict into a clear, fast, correctly-scoped error (see
    raise_discovery_bind_error) rather than a silent, permanent hang.

    Every caller that needs a throwaway discovery Application (device
    inventory discovery, per-device object discovery, present-value
    refresh) MUST go through this one context manager rather than
    reimplementing the scaffold -- copy-pasting it would reintroduce the
    port-47808 hang/leak bugs already found and fixed live this session.
    """
    port = 47808 if needs_broadcast else 0
    async with _discovery_lock:
        device_obj = DeviceObject(
            objectIdentifier=f"device,{_CLIENT_DEVICE_INSTANCE}",
            objectName="DiscoveryClient",
            vendorIdentifier=999,
        )
        network_port = NetworkPortObject(
            f"{_resolve_local_bind_address()}:{port}",
            objectIdentifier=("network-port", 1),
            objectName="NetworkPort-1",
        )
        app = Application.from_object_list([device_obj, network_port])
        try:
            await _wait_for_bind(app)
            yield BACnetDiscovery(Bacpypes3Transport(app))
        finally:
            # app.close() can itself raise (e.g. AttributeError if a link-layer
            # UDP endpoint never finished binding -- BACpypes3's own endpoint
            # setup retries forever on OSError, so local_transport/
            # broadcast_transport may not exist yet on a slow/loaded host).
            # This throwaway Application is being discarded either way; letting
            # a cleanup failure here replace/mask an otherwise-successful
            # result would turn a working response into a 500.
            try:
                app.close()
            except Exception:
                log.warning("Failed to cleanly close discovery Application", exc_info=True)
            finally:
                # Runs whether or not app.close() itself raised -- always
                # sweep for any endpoint task close() didn't get to, so a
                # failure here can never leak a live socket bind past this
                # request (see _force_close_transports's docstring).
                try:
                    _force_close_transports(app)
                except Exception:
                    log.warning("Failed to force-close discovery transports", exc_info=True)


async def _run_discovery(body: DiscoveryTriggerRequest) -> list[dict[str, Any]]:
    # A Discovery Target means every Who-Is discover() sends is targeted/
    # unicast (see BACnetDiscovery.discover()'s discovery_targets branch) --
    # only the no-target case falls through to a real broadcast scan and
    # needs the real local port (see _discovery_session's docstring).
    async with _discovery_session(needs_broadcast=not body.discovery_target) as discovery:
        options = DiscoveryOptions(
            discovery_targets=[body.discovery_target] if body.discovery_target else [],
            timeout_ms=body.timeout_ms,
            device_id_range=(body.device_instance_low, body.device_instance_high),
        )
        devices = await discovery.discover(options)
        return [dataclasses.asdict(d) for d in devices]


def _request_from_connection(connection: dict[str, Any]) -> DiscoveryTriggerRequest:
    return DiscoveryTriggerRequest(
        discovery_target=connection.get("target"),
        device_instance_low=connection.get("device_instance_low", 0),
        device_instance_high=connection.get("device_instance_high", 4194303),
        timeout_ms=connection.get("timeout_ms", 5000),
    )


async def _sync_discovery_request(
    database: Any,
    body: DiscoveryTriggerRequest,
    request: Request,
) -> list[dict]:
    try:
        devices = await _run_discovery(body)
    except DiscoveryBindError as exc:
        raise_discovery_bind_error(request, exc)
    return await asyncio.to_thread(database.sync_external_devices, devices)


@router.post("/run")
async def run_discovery(
    body: DiscoveryTriggerRequest,
    request: Request,
):
    try:
        devices = await _run_discovery(body)
    except DiscoveryBindError as exc:
        raise_discovery_bind_error(request, exc)
    return {"devices": devices}


@router.get("/connections/{project_id}")
async def list_discovery_connections(
    project_id: int,
    request: Request,
):
    database = get_database(request)
    connections = await asyncio.to_thread(
        database.get_project_discovery_connections,
        project_id,
    )
    if connections is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"connections": connections}


@router.post(
    "/connections/{project_id}",
    status_code=201,
)
async def create_discovery_connection(
    project_id: int,
    body: DiscoveryConnectionPayload,
    request: Request,
):
    database = get_database(request)
    connection = await asyncio.to_thread(
        database.create_project_discovery_connection,
        project_id,
        body.model_dump() if hasattr(body, "model_dump") else body.dict(),
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return connection


@router.post("/connections/{project_id}/sync-enabled")
async def sync_enabled_discovery_connections(
    project_id: int,
    request: Request,
):
    database = get_database(request)
    connections = await asyncio.to_thread(
        database.get_project_discovery_connections,
        project_id,
    )
    if connections is None:
        raise HTTPException(status_code=404, detail="Project not found")

    enabled = [c for c in connections if c.get("enabled", True)]
    persisted_by_instance: dict[int, dict] = {}
    for connection in enabled:
        persisted = await _sync_discovery_request(
            database,
            _request_from_connection(connection),
            request,
        )
        for device in persisted:
            persisted_by_instance[device["device_instance"]] = device

    return {
        "devices": list(persisted_by_instance.values()),
        "connections": len(enabled),
    }


@router.put("/connections/{project_id}/{connection_id}")
async def update_discovery_connection(
    project_id: int,
    connection_id: int,
    body: DiscoveryConnectionPayload,
    request: Request,
):
    database = get_database(request)
    connection = await asyncio.to_thread(
        database.update_project_discovery_connection,
        project_id,
        connection_id,
        body.model_dump() if hasattr(body, "model_dump") else body.dict(),
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if connection == {}:
        raise HTTPException(status_code=404, detail="Discovery connection not found")
    return connection


@router.delete(
    "/connections/{project_id}/{connection_id}",
    status_code=204,
)
async def delete_discovery_connection(
    project_id: int,
    connection_id: int,
    request: Request,
) -> Response:
    database = get_database(request)
    deleted = await asyncio.to_thread(
        database.delete_project_discovery_connection,
        project_id,
        connection_id,
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not deleted:
        raise HTTPException(status_code=404, detail="Discovery connection not found")
    return Response(status_code=204)


@router.post("/connections/{project_id}/{connection_id}/sync")
async def sync_discovery_connection(
    project_id: int,
    connection_id: int,
    request: Request,
):
    database = get_database(request)
    connections = await asyncio.to_thread(
        database.get_project_discovery_connections,
        project_id,
    )
    if connections is None:
        raise HTTPException(status_code=404, detail="Project not found")
    connection = next((c for c in connections if c["id"] == connection_id), None)
    if connection is None:
        raise HTTPException(status_code=404, detail="Discovery connection not found")
    persisted = await _sync_discovery_request(database, _request_from_connection(connection), request)
    return {"devices": persisted}


@router.post("/sync")
async def sync_discovery(
    body: DiscoveryTriggerRequest,
    request: Request,
):
    """
    Runs discovery and persists the results as external-BACnet devices in
    the project inventory (upsert on device_instance, never deleting a
    device just because one run didn't see it -- see
    Database.sync_external_devices). This is what the admin UI's
    "Discover Devices"/"Rediscover" action calls; POST /run stays as a
    pure ephemeral dry-run.
    """
    database = get_database(request)
    persisted = await _sync_discovery_request(database, body, request)
    return {"devices": persisted}
