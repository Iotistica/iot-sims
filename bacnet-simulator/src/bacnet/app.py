"""BACnet protocol application layer.

Physically extracted from src/legacy.py's `SimApplication` (the shared
bacpypes3 Application all virtual devices register against) plus the small
BACnet-protocol helper functions only it and SimEngine use -- continuing the
GH #15 refactor, same "moved verbatim, no behavior changes" standard as the
Database and API-router extractions.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Any, Callable, Optional

from bacpypes3.apdu import (
    AbortPDU,
    ErrorPDU,
    ReadPropertyACK,
    ReadRangeACK,
    RejectPDU,
    SimpleAckPDU,
)
from bacpypes3.app import Application
from bacpypes3.basetypes import BinaryPV, LogRecord, Polarity, Reliability, StatusFlags
from bacpypes3.constructeddata import Any as BACnetAny
from bacpypes3.constructeddata import SequenceOf
from bacpypes3.debugging import bacpypes_debugging
from bacpypes3.errors import ExecutionError
from bacpypes3.ipv4 import IPv4DatagramServer
from bacpypes3.local.device import DeviceObject
from bacpypes3.primitivedata import ObjectIdentifier, Real, Unsigned

from ..core.config import BINARY_TYPES, MULTISTATE_TYPES
from ..monitoring.event_log import _log_event_notification_received
from ..monitoring.metrics import metrics
from ..simulation.behaviors import ManualBehavior
from .trend_logs import _build_log_record, _slice_trend_records

log = logging.getLogger("bacnet-sim")


def _dependencies():
    """
    Resolve src.dependencies lazily.

    IMPORTANT:
    Do not import src.dependencies at module import time.

    src.dependencies imports src.db.database, which itself reaches this
    module at import time via simulation.models.store -> simulation.models
    -> simulation.providers -> simulation.providers.builtin ->
    bacnet.app (this module). Importing src.dependencies eagerly here
    would re-enter that partially initialized chain and raise a
    circular-import ImportError -- caught by directly booting SimEngine,
    not by pyflakes (which checks each file in isolation and can't see
    multi-hop cycles like this one).

    Only `packet_capture` (a src.dependencies global) is still needed
    lazily here; everything else this module used to reach through legacy/
    dependencies (metrics, _log_event_notification_received) now has its
    own direct, cycle-free import above.
    """
    from .. import dependencies
    return dependencies


def _is_broadcast_address(destination) -> bool:
    # The *source* of a UDP packet is always the sender's own unicast return
    # address, whether the packet was sent broadcast or not — it never tells
    # you how the request was addressed. The destination does: bacpypes3's
    # IPv4 BVLL layer sets pduDestination to LocalBroadcast()/GlobalBroadcast()
    # only when the incoming LPDU was an Original-Broadcast-NPDU.
    if destination is None:
        return False
    return bool(getattr(destination, "is_localbroadcast", False) or getattr(destination, "is_globalbroadcast", False))


def _is_device_objid(objid) -> bool:
    if not isinstance(objid, tuple) or len(objid) != 2:
        return False
    t = objid[0]
    return t == "device" or (isinstance(t, int) and t == 8)


def _resolve_base_ip() -> str:
    iface = os.environ.get("BACPYPES_IFACE", "")
    if iface:
        ip = iface.split(":")[0].split("/")[0]
        # "0.0.0.0" here means "bind to all interfaces" — a valid bind
        # address but not a usable destination for self-directed traffic
        # (e.g. a device-type notification recipient resolving to our own
        # address). Fall through to hostname resolution for a real one.
        if ip and ip != "0.0.0.0":
            return ip
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        pass
    return "0.0.0.0"
@bacpypes_debugging
class SimApplication(Application):
    """Multi-device BACnet application — all virtual devices share one UDP socket."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._virtual_devices: dict[int, DeviceObject] = {}
        self._virtual_object_lists: dict[int, list] = {}
        self._sim_engine: Any = None  # set by SimEngine after construction
        self._own_ip: Optional[str] = None  # set by SimEngine.start(), for I-Am loopback filtering
        self._i_am_listeners: list[Callable[[Any], None]] = []

    def add_i_am_listener(self, listener: Callable[[Any], None]) -> None:
        """Register a callback invoked with every inbound I-Am APDU, after
        this class's own duplicate-device-ID handling. Used by
        src/bacnet/client/transport.py's targeted-discovery I-Am collector.
        Purely additive -- never replaces do_IAmRequest itself."""
        self._i_am_listeners.append(listener)

    def remove_i_am_listener(self, listener: Callable[[Any], None]) -> None:
        if listener in self._i_am_listeners:
            self._i_am_listeners.remove(listener)

    def get_object_id(self, objid):
        obj = super().get_object_id(objid)
        if obj is not None:
            return obj
        if _is_device_objid(objid):
            return self._virtual_devices.get(int(objid[1]))
        return None

    async def do_WhoIsRequest(self, apdu) -> None:
        low = apdu.deviceInstanceRangeLowLimit
        high = apdu.deviceInstanceRangeHighLimit
        source = apdu.pduSource
        is_unicast = not _is_broadcast_address(getattr(apdu, "pduDestination", None))

        metrics.requests_total += 1
        metrics.requests_by_service["WhoIs"] += 1
        metrics.discovery_total += 1
        if is_unicast:
            metrics.requests_unicast += 1
        else:
            metrics.requests_broadcast += 1
        now = time.time()
        src_str = str(source)
        metrics.clients_seen[src_str] = now
        metrics.recent_requests.append({
            "ts": now, "service": "WhoIs", "source": src_str,
            "broadcast": not is_unicast, "device": None, "ok": True,
        })

        saved = self.device_object
        try:
            for did, dev_obj in self._virtual_devices.items():
                in_range = (low is None and high is None) or (
                    low is not None and high is not None and low <= did <= high
                )
                if not in_range:
                    continue
                self.device_object = dev_obj
                if is_unicast:
                    self.i_am(address=source)
                else:
                    self.i_am()
        finally:
            self.device_object = saved

    async def do_IAmRequest(self, apdu) -> None:
        # Unconfirmed and previously unhandled — Application has no default
        # do_IAmRequest, so incoming I-Am from other devices on the network
        # was silently dropped before this override existed (indication()
        # only raises UnrecognizedService for confirmed requests with no
        # handler; unconfirmed ones with no handler just return, app.py
        # ~878-881). This hook is purely additive — no existing behavior
        # changes by adding it.
        try:
            instance = int(apdu.iAmDeviceIdentifier[1])
        except Exception:
            return
        source = apdu.pduSource
        src_str = str(source)
        now = time.time()

        metrics.discovery_total += 1
        metrics.clients_seen[src_str] = now
        is_new = instance not in metrics.iam_seen
        metrics.iam_seen[instance] = now
        if is_new:
            metrics.new_devices_timeline.append({"ts": now, "device_instance": instance, "source": src_str})

        # Flag a real collision: someone other than us claiming one of our
        # own virtual devices' instance numbers. Loopback of our own I_am
        # broadcasts (if the OS reflects them back) is filtered by IP.
        own_ip = (self._own_ip or "").split(":")[0]
        source_ip = src_str.split(":")[0]
        if instance in self._virtual_devices and source_ip and source_ip != own_ip:
            metrics.duplicate_id_events.append({
                "ts": now, "device_instance": instance, "source": src_str,
            })

        # Restore BACpypes3's own I-Am processing (WhoIsIAmServices's
        # _who_is_futures matching, used by Application.who_is() callers) --
        # previously unreachable since this override never chained to it.
        try:
            await super().do_IAmRequest(apdu)
        except Exception:
            log.exception("super().do_IAmRequest failed for %r", apdu)

        # Notify any registered temporary listeners (targeted-discovery I-Am
        # collectors, see src/bacnet/client/transport.py) -- purely additive.
        for listener in list(self._i_am_listeners):
            try:
                listener(apdu)
            except Exception:
                log.exception("I-Am listener failed for %r", apdu)

    def _log_received_event_notification(self, apdu) -> None:
        """Parses a real, wire-received APDU and defers to the shared logger.
        Kept only for genuine external (address-type) recipients — see
        _log_event_notification_received's docstring for why device-type
        recipients no longer go through this path at all."""
        try:
            sender_instance = int(apdu.initiatingDeviceIdentifier[1])
        except Exception:
            sender_instance = None
        message_text = str(getattr(apdu, "messageText", "") or "")
        from_state = str(getattr(apdu, "fromState", "?"))
        to_state = str(getattr(apdu, "toState", "?"))
        _log_event_notification_received(sender_instance, None, message_text, from_state, to_state)

    async def do_UnconfirmedEventNotificationRequest(self, apdu) -> None:
        # Unconfirmed and previously unhandled — same situation do_IAmRequest
        # documents above: Application has no default handler, so this was
        # silently dropped before this override existed. Purely additive.
        #
        # Exceptions here would otherwise be swallowed by Application.
        # indication()'s broad except block, which logs via bacpypes3's own
        # debug channel (off by default) rather than the standard logging
        # module — so a bug in this handler would be silent even with normal
        # log levels turned up. Log explicitly instead of trusting that path.
        try:
            self._log_received_event_notification(apdu)
        except Exception:
            log.exception("do_UnconfirmedEventNotificationRequest failed on %r", apdu)

    async def do_ConfirmedEventNotificationRequest(self, apdu) -> None:
        # Same as above, but must ack — Application has no default handler
        # for this service either, so without this the *sender's* confirmed
        # wait (alarms.send_event_notification's asyncio.wait_for) would
        # error out instead of completing.
        try:
            self._log_received_event_notification(apdu)
        except Exception:
            log.exception("do_ConfirmedEventNotificationRequest failed on %r", apdu)
        await self.response(SimpleAckPDU(context=apdu))

    async def do_ReadPropertyRequest(self, apdu) -> None:
        objid = apdu.objectIdentifier

        # Stamp pending context here (cheap: dict write + counter increments,
        # no scans/allocation) — SimApplication.response() pops this to
        # attribute latency + success/error once the outcome is known,
        # whether this method answers directly or delegates to super().
        pending_key = (str(apdu.pduSource), apdu.apduInvokeID)
        metrics.pending[pending_key] = {
            "service": "ReadProperty", "objid": str(objid), "started": time.monotonic(),
        }
        metrics.requests_total += 1
        metrics.requests_by_service["ReadProperty"] += 1
        metrics.reads_total += 1
        metrics.requests_unicast += 1  # ReadProperty is always confirmed/unicast by protocol
        metrics.clients_seen[str(apdu.pduSource)] = time.time()

        if _is_device_objid(objid):
            did = int(objid[1])
            virtual = self._virtual_devices.get(did)
            if virtual:
                prop = apdu.propertyIdentifier
                try:
                    prop_code = int(prop)
                except Exception:
                    prop_code = getattr(prop, "value", prop)
                if prop_code == 76:
                    cls = DeviceObject._elements.get("objectList")
                    raw = self._virtual_object_lists.get(did, [])
                    value = cls([ObjectIdentifier(o) for o in raw])
                elif prop_code == 77:
                    value = virtual.objectName
                elif prop_code == 121:
                    value = virtual.vendorName
                elif prop_code == 70:
                    value = virtual.modelName
                elif prop_code == 28:
                    value = virtual.description
                else:
                    await super().do_ReadPropertyRequest(apdu)
                    return
                resp = ReadPropertyACK(
                    objectIdentifier=objid,
                    propertyIdentifier=prop,
                    propertyArrayIndex=apdu.propertyArrayIndex,
                    propertyValue=value,
                    context=apdu,
                )
                await self.response(resp)
                return
        await super().do_ReadPropertyRequest(apdu)

    async def do_WritePropertyRequest(self, apdu) -> None:
        pending_key = (str(apdu.pduSource), apdu.apduInvokeID)
        metrics.pending[pending_key] = {
            "service": "WriteProperty", "objid": str(apdu.objectIdentifier), "started": time.monotonic(),
        }
        metrics.requests_total += 1
        metrics.requests_by_service["WriteProperty"] += 1
        metrics.writes_total += 1
        metrics.requests_unicast += 1  # WriteProperty is always confirmed/unicast by protocol
        metrics.clients_seen[str(apdu.pduSource)] = time.time()

        if self._sim_engine is None:
            await super().do_WritePropertyRequest(apdu)
            return

        # Only intercept present-value writes (property identifier 85)
        prop = apdu.propertyIdentifier
        try:
            prop_code = int(prop)
        except Exception:
            prop_code = getattr(prop, "value", None)
        if prop_code != 85:
            await super().do_WritePropertyRequest(apdu)
            return

        # Find the bacpypes3 object
        obj = self.get_object_id(apdu.objectIdentifier)
        if obj is None:
            await super().do_WritePropertyRequest(apdu)
            return

        # Resolve to DB id by object identity
        db_id = self._sim_engine.db_id_for_bacnet_object(obj)
        if db_id is None:
            await super().do_WritePropertyRequest(apdu)
            return

        obj_row = await asyncio.to_thread(self._sim_engine.db.get_object, db_id)
        if not obj_row:
            await super().do_WritePropertyRequest(apdu)
            return

        otype = obj_row["object_type"]
        WRITABLE = {
            "analog-output", "analog-value", "binary-output", "binary-value",
            "multi-state-output", "multi-state-value",
        }
        if otype not in WRITABLE:
            await super().do_WritePropertyRequest(apdu)
            return

        # Extract the written value
        try:
            if "analog" in otype:
                value: Any = float(apdu.propertyValue.cast_out(Real))
            elif otype in MULTISTATE_TYPES:
                value = int(apdu.propertyValue.cast_out(Unsigned))
            else:
                bpv = apdu.propertyValue.cast_out(BinaryPV)
                value = (str(bpv) == "active")
        except Exception as e:
            log.warning("WriteProperty decode error on %s: %s", apdu.objectIdentifier, e)
            metrics.errors_by_type["error:property.invalidDataType"] += 1
            await super().do_WritePropertyRequest(apdu)
            return

        # Persist to DB and update in-memory sim
        await self._sim_engine.write_object(db_id, value, source=str(apdu.pduSource))
        await self.response(SimpleAckPDU(context=apdu))

    async def do_ReadRangeRequest(self, apdu) -> None:
        """Serve BACnet ReadRange for Trend Log objects' Log_Buffer property
        (by position, by sequence number, by time, or the whole buffer if
        no range is given) — see _slice_trend_records(). Every other
        object/property falls through to bacpypes3's own handling, which is
        unimplemented (raises NotImplementedError), same as before this
        override existed."""
        pending_key = (str(apdu.pduSource), apdu.apduInvokeID)
        metrics.pending[pending_key] = {
            "service": "ReadRange", "objid": str(apdu.objectIdentifier), "started": time.monotonic(),
        }
        metrics.requests_total += 1
        metrics.requests_by_service["ReadRange"] += 1
        metrics.reads_total += 1
        metrics.requests_unicast += 1
        metrics.clients_seen[str(apdu.pduSource)] = time.time()

        if self._sim_engine is None:
            await super().do_ReadRangeRequest(apdu)
            return

        objid = apdu.objectIdentifier
        tl_id = None
        for tlid, bobj in self._sim_engine._trend_log_objects.items():
            if bobj.objectIdentifier == objid:
                tl_id = tlid
                break
        if tl_id is None:
            await super().do_ReadRangeRequest(apdu)
            return

        prop = apdu.propertyIdentifier
        try:
            prop_code = int(prop)
        except Exception:
            prop_code = getattr(prop, "value", None)
        if prop_code != 131:  # log-buffer
            raise ExecutionError(errorClass="property", errorCode="unknownProperty")

        tl_cfg = await asyncio.to_thread(self._sim_engine.db.get_trend_log, tl_id)
        if not tl_cfg or not tl_cfg["enabled"]:
            raise ExecutionError(errorClass="object", errorCode="unknownObject")

        monitored = await asyncio.to_thread(self._sim_engine.db.get_object, tl_cfg["monitored_object_id"])
        otype = monitored["object_type"] if monitored else "analog-input"

        all_records = await asyncio.to_thread(
            self._sim_engine.db.get_trend_log_records, tl_id, limit=tl_cfg["buffer_size"], order="asc"
        )
        try:
            selected, is_first, is_last = _slice_trend_records(all_records, apdu.range)
        except Exception as e:
            raise ExecutionError(errorClass="property", errorCode="invalidArrayIndex") from e

        log_records = [_build_log_record(r, otype) for r in selected]
        item_data = BACnetAny(SequenceOf(LogRecord)(log_records))

        resp = ReadRangeACK(
            objectIdentifier=objid,
            propertyIdentifier=prop,
            propertyArrayIndex=apdu.propertyArrayIndex,
            resultFlags=[is_first, is_last, not is_last],
            itemCount=len(log_records),
            itemData=item_data,
            firstSequenceNumber=selected[0]["sequence_number"] if selected else 1,
            context=apdu,
        )
        await self.response(resp)

    async def response(self, apdu) -> None:  # type: ignore[override]
        # Every outcome — success, reject, abort, or protocol error — passes
        # through here before being sent on the wire, regardless of whether
        # it originated in our own do_*Request code above or fell through to
        # bacpypes3's own internal object/property validation inside
        # super().do_*Request(). See Application.indication() (bacpypes3
        # app.py): it catches RejectException/AbortException/ExecutionError
        # from the do_*Request call and turns each into exactly the PDU
        # types checked below, always via self.response(...) — so this is
        # the one stable place to observe every request's real outcome
        # without touching indication()'s own dispatch logic.
        pending_key = (str(apdu.pduDestination), apdu.apduInvokeID) if getattr(apdu, "pduDestination", None) else None
        ctx = metrics.pending.pop(pending_key, None) if pending_key else None

        now = time.time()
        latency_ms = (time.monotonic() - ctx["started"]) * 1000 if ctx else None
        if latency_ms is not None:
            metrics.latencies_ms.append(latency_ms)

        objid_key = ctx["objid"] if ctx else None
        service = ctx["service"] if ctx else None
        ok = True
        error_label = None

        if isinstance(apdu, RejectPDU):
            ok = False
            error_label = f"reject:{apdu.apduAbortRejectReason}"
        elif isinstance(apdu, AbortPDU):
            ok = False
            error_label = f"abort:{apdu.apduAbortRejectReason}"
        elif isinstance(apdu, ErrorPDU):
            ok = False
            err_class = getattr(apdu, "errorClass", "unknown")
            err_code = getattr(apdu, "errorCode", "unknown")
            error_label = f"error:{err_class}.{err_code}"

        if error_label:
            metrics.errors_by_type[error_label] += 1
            metrics.recent_errors.append({
                "ts": now, "type": error_label, "service": service, "object": objid_key,
            })
        elif objid_key and service == "ReadProperty":
            metrics.object_reads[objid_key] += 1
        elif objid_key and service == "WriteProperty":
            metrics.object_writes[objid_key] += 1

        if service:
            metrics.recent_requests.append({
                "ts": now, "service": service, "object": objid_key, "ok": ok,
                "latency_ms": latency_ms,
            })

        await super().response(apdu)


def _apply_reliability(bacnet_obj: Any, reliability_str: str) -> None:
    """Force a specific Reliability value (GH #16) on a constructed analog/
    binary/multi-state object, for testing client-side fault handling. Also
    sets the statusFlags.fault bit, matching what real BACnet clients
    actually key off of — Reliability alone is often not surfaced in a
    client's UI, but the fault status bit almost always is."""
    try:
        reliability = Reliability(reliability_str)
    except Exception:
        reliability = Reliability("no-fault-detected")
    bacnet_obj.reliability = reliability
    fault = 0 if str(reliability) == "no-fault-detected" else 1
    bacnet_obj.statusFlags = StatusFlags([0, fault, 0, 0])


def _apply_polarity(bacnet_obj: Any, polarity_str: str) -> None:
    """Set Polarity (GH #19) on a constructed Binary Input/Output object.
    Binary Value has no polarity property in bacpypes3's schema (matching
    real BACnet spec — only physically-wired points have one), so this is
    only ever called for binary-input/binary-output."""
    try:
        bacnet_obj.polarity = Polarity(polarity_str)
    except Exception:
        bacnet_obj.polarity = Polarity("normal")


def coerce_binary_write_value(raw: Any) -> bool:
    """Interprets a raw write value (a manual-override/priority-array write
    request -- e.g. the Functional Test builder's Set block, or the admin
    UI) as a BACnet binary state, for call sites that already know the
    target object is binary. Plain `bool(raw)` on a string is always True
    for any non-empty text -- bool("0") and bool("off") are BOTH True --
    so writing the string "0" or "off" to mean inactive would silently
    write active instead. Recognizes the same ON/OFF-ish vocabulary as
    ManualBehavior._coerce, plus numeric strings ("0"/"1")."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in ManualBehavior._TRUE_WORDS:
            return True
        if normalized in ManualBehavior._FALSE_WORDS:
            return False
        try:
            return bool(float(normalized))
        except ValueError:
            pass
    return bool(raw)


def normalize_present_value(object_type: str, val: Any) -> Any:
    """Canonicalizes a Behavior.compute() result before it's stored/served/
    logged anywhere (the tick loop's SimEngine._objects processing calls
    this immediately after compute(), before _update_value(), before
    _prev_values[obj_id] = val, before the /sim/state snapshot, trend logs,
    or alarm/enrollment evaluation see it).

    Behavior.compute() is typed float|bool and different Behavior
    subclasses disagree on which they return for the same logical binary
    point -- ManualBehavior keeps bool only for literal JSON true/false
    (numeric input is coerced to float), while DailyPatternBehavior and
    FaultBehavior always return float, even when wrapping a boolean
    ConstantBehavior. Present_Value for binary-input/output/value is
    BACnetBinaryPV (an ENUMERATED{inactive,active}, not a float or a
    general integer -- see ASHRAE 135's object type tables), so the
    simulator's canonical internal/API representation is a plain bool
    (True=active). Normalizing once, here, is what lets every downstream
    consumer (API JSON, the /sim/state snapshot, the Vue UI) treat
    "is this point binary" as the single source of truth for how to
    display/interpret a value, instead of branching on whatever Python/JS
    runtime type a particular Behavior happened to produce."""
    if object_type in BINARY_TYPES:
        return bool(val)
    return val


# ─── Sim Engine ───────────────────────────────────────────────────────────────

def _force_close_bacnet_transports(app: Any) -> None:
    """
    Defensive cleanup for what app.close() can't handle: if a link-layer UDP
    endpoint's creation task hadn't finished binding yet when close() ran (it
    can raise AttributeError trying to close a transport that was never set
    -- see bacpypes3's ipv4/__init__.py IPv4DatagramServer.close()), close()
    bails out without ever touching that task. BACpypes3's own endpoint
    setup (retrying_create_datagram_endpoint) then keeps it retrying forever
    in the background, fully detached from this now-abandoned Application --
    if it eventually succeeds, it binds a real socket nothing else will ever
    release, permanently occupying this simulator's own BACnet port even
    though self.app has already been reset to None. Reaches into each link
    layer's server and either cancels the task (still pending) or closes the
    transport it already produced (finished just after close() gave up).
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


_bacpypes_capture_hooks_installed = False


def _bacpypes_address_tuple(
    address: Any,
    *,
    fallback_ip: str,
    fallback_port: int,
) -> tuple[str, int]:
    """
    Convert a BACpypes3 IPv4Address-like object into (IP, port).

    BACpypes3 versions may expose the tuple under slightly different
    attributes, so this deliberately uses defensive fallbacks.
    """
    if address is None:
        return fallback_ip, fallback_port

    for attribute in ("addrTuple", "addr_tuple"):
        value = getattr(address, attribute, None)

        if (
            isinstance(value, tuple)
            and len(value) >= 2
        ):
            return str(value[0]), int(value[1])

    text = str(address)

    # BACpypes3 may stringify an address as "10.0.0.60:47808".
    if ":" in text:
        host, possible_port = text.rsplit(":", 1)

        try:
            return host, int(possible_port)
        except ValueError:
            pass

    return text or fallback_ip, fallback_port


def install_bacpypes_packet_capture_hooks(
    *,
    local_ip: str,
    local_port: int,
    get_clock_state: Callable[[], str],
) -> None:
    """
    Install one process-wide hook around BACpypes3's IPv4 UDP transport.

    indication()   = outbound toward the UDP socket
    confirmation() = inbound from the UDP socket

    While clock_state != "running" (i.e. "paused" or "stopped"), ALL
    outbound traffic is suppressed here — this is the single choke point
    every outbound byte passes through regardless of what generated it
    (Who-Is/I-Am response, ReadProperty/WriteProperty ACK, COV
    notification, ...), so it's the simplest, safest place to make both
    "Pause" and "Stop" mean "the simulator stops responding" without
    tearing down and rebinding the UDP socket itself (which Start would
    then have to safely reconstruct — this way Start/Pause/Stop just
    toggle a check, the transport is never touched). Pause and Stop still
    differ elsewhere (Stop rewinds elapsed_seconds/time_of_day to 0,
    Pause leaves them exactly where they were so Resume picks up without
    losing simulated time) — this suppression is the one thing they now
    share.

    Suppressed packets are neither sent nor recorded by packet capture —
    they never happened, from the network's point of view. Inbound
    traffic (confirmation()) is NOT gated here: a real, paused/stopped
    controller still receives whatever other devices broadcast at it
    (e.g. Who-Is), it just doesn't answer -- see the "Stop"/"Pause"
    behavior decided for this simulator.
    """
    global _bacpypes_capture_hooks_installed

    if _bacpypes_capture_hooks_installed:
        return

    original_indication = IPv4DatagramServer.indication
    original_confirmation = IPv4DatagramServer.confirmation

    async def captured_indication(
        transport_self: IPv4DatagramServer,
        pdu: Any,
    ) -> None:
        if get_clock_state() != "running":
            return

        try:
            payload = bytes(getattr(pdu, "pduData", b""))

            destination = _bacpypes_address_tuple(
                getattr(pdu, "pduDestination", None),
                fallback_ip="255.255.255.255",
                fallback_port=local_port,
            )

            source = _bacpypes_address_tuple(
                getattr(pdu, "pduSource", None),
                fallback_ip=local_ip,
                fallback_port=local_port,
            )

            if payload:
                _dependencies().packet_capture.record_outbound(
                    payload,
                    source=source,
                    destination=destination,
                )
        except Exception:
            # Capture must never interrupt BACnet communication.
            log.exception(
                "Failed to record outbound BACnet/IP packet"
            )

        await original_indication(transport_self, pdu)

    async def captured_confirmation(
        transport_self: IPv4DatagramServer,
        pdu: Any,
    ) -> None:
        try:
            payload = bytes(getattr(pdu, "pduData", b""))

            source = _bacpypes_address_tuple(
                getattr(pdu, "pduSource", None),
                fallback_ip="0.0.0.0",
                fallback_port=local_port,
            )

            destination = _bacpypes_address_tuple(
                getattr(pdu, "pduDestination", None),
                fallback_ip=local_ip,
                fallback_port=local_port,
            )

            if payload:
                _dependencies().packet_capture.record_inbound(
                    payload,
                    source=source,
                    destination=destination,
                )
        except Exception:
            log.exception(
                "Failed to record inbound BACnet/IP packet"
            )

        await original_confirmation(transport_self, pdu)

    IPv4DatagramServer.indication = captured_indication
    IPv4DatagramServer.confirmation = captured_confirmation

    _bacpypes_capture_hooks_installed = True

    log.info(
        "Installed BACpypes3 packet-capture hooks for %s:%s",
        local_ip,
        local_port,
    )
