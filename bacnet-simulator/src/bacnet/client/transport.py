from __future__ import annotations

import asyncio
from typing import Any

from bacpypes3.apdu import ErrorRejectAbortNack, WhoIsRequest
from bacpypes3.pdu import Address, IPv4Address
from bacpypes3.primitivedata import ObjectIdentifier as Bp3ObjectIdentifier

from .types import IAmDevice, ObjectIdentifier


def _ensure_i_am_listener_support(app: Any) -> None:
    """
    One-time adapter instrumentation for a bare/third-party Application that
    doesn't already expose add_i_am_listener()/remove_i_am_listener()
    natively (SimApplication, src/legacy.py, defines these natively and is
    never touched by this function -- hasattr(...) short-circuits
    immediately for it). Installed at most once per Application instance,
    idempotent, and never removed/reinstalled again afterward -- individual
    discovery calls only add/remove their own listener from a plain list,
    which is safe for any number of concurrent callers and never creates a
    window where the wrong handler is active.
    """
    if hasattr(app, "add_i_am_listener"):
        return

    app._i_am_listeners = []
    original_do_i_am = app.do_IAmRequest

    async def _dispatching_do_i_am_request(apdu) -> None:
        await original_do_i_am(apdu)
        for listener in list(app._i_am_listeners):
            try:
                listener(apdu)
            except Exception:
                pass  # a listener failing must never affect normal I-Am handling

    app.do_IAmRequest = _dispatching_do_i_am_request
    app.add_i_am_listener = lambda listener: app._i_am_listeners.append(listener)
    app.remove_i_am_listener = lambda listener: (
        app._i_am_listeners.remove(listener) if listener in app._i_am_listeners else None
    )


def _source_matches_target(destination: Address, source: Any) -> bool:
    """
    True if `source` (an inbound apdu.pduSource) can be confidently
    confirmed as coming from `destination` (the host a targeted Who-Is was
    sent to). Only meaningful for same-subnet BACnet/IP unicast, where both
    are IPv4Address instances -- a routed reply's pduSource is a
    RemoteStation (keyed by BACnet network number rather than IP) and can't
    be reliably compared this way at all. Deliberately permissive for
    anything else: "can't tell" returns True (don't filter) rather than
    silently rejecting a reply we can't actually evaluate.
    """
    if not isinstance(destination, IPv4Address) or not isinstance(source, IPv4Address):
        return True
    return destination == source


class Bacpypes3Transport:
    """
    Thin adapter around an existing BACpypes3 Application object.

    This intentionally accepts an already-created Application so the simulator
    can share its existing BACnet stack instead of binding a second UDP socket.

    BACpypes3 exposes high-level async helpers such as who_is() and
    read_property(). Exact return container types vary by BACpypes3 version, so
    this class normalizes the common shapes into this package's small protocol.

    If the simulator pins a specific BACpypes3 version, tighten these
    normalizers to that version.
    """

    def __init__(self, application: Any) -> None:
        self.application = application

    async def who_is(
        self,
        *,
        address: str | None,
        low_limit: int,
        high_limit: int,
        timeout_ms: int,
    ) -> list[IAmDevice]:
        if not address:
            # Broadcast discovery is unchanged: bacpypes3's own who_is()/
            # WhoIsFuture already collects every I-Am within its window when
            # no explicit address is given (only_one is False in that case).
            result = await asyncio.wait_for(
                self.application.who_is(low_limit=low_limit, high_limit=high_limit),
                timeout=max(timeout_ms / 1000.0, 0.1) + 0.5,
            )
            items = result or []
        else:
            # A targeted/unicast Who-Is makes bacpypes3's own WhoIsFuture
            # stop collecting after the first I-Am (WhoIsFuture.only_one is
            # True whenever an explicit address is given -- see
            # bacpypes3/service/device.py), which silently drops every
            # device but the first when multiple virtual devices share one
            # host. Collect via a listener instead, sidestepping only_one
            # entirely rather than racing it.
            items = await self._collect_i_ams(
                destination=Address(address),
                low_limit=low_limit,
                high_limit=high_limit,
                timeout_ms=timeout_ms,
            )

        devices: list[IAmDevice] = []
        for item in items:
            normalized = self._normalize_i_am(item)
            if normalized is not None:
                devices.append(normalized)
        return devices

    async def _collect_i_ams(
        self,
        *,
        destination: Address,
        low_limit: int | None,
        high_limit: int | None,
        timeout_ms: int,
    ) -> list[Any]:
        """
        Send one targeted WhoIsRequest and collect every matching I-Am
        received during a bounded window, via a listener rather than
        bacpypes3's own who_is()/WhoIsFuture (see who_is()'s docstring
        comment for why). Never replaces do_IAmRequest itself -- only adds
        and removes its own listener closure, so overlapping calls (or
        normal BACnet traffic on a shared Application) cannot corrupt each
        other: each closure only touches its own local `collected`/`done`.
        """
        app = self.application
        _ensure_i_am_listener_support(app)

        collected: dict[int, Any] = {}
        single_target = low_limit is not None and low_limit == high_limit
        done = asyncio.Event()

        def on_i_am(apdu) -> None:
            if not _source_matches_target(destination, getattr(apdu, "pduSource", None)):
                return
            try:
                instance = int(apdu.iAmDeviceIdentifier[1])
            except Exception:
                return
            if low_limit is not None and instance < low_limit:
                return
            if high_limit is not None and instance > high_limit:
                return
            collected.setdefault(instance, apdu)  # first-seen wins, matches discover()'s dedup convention
            if single_target and instance == low_limit:
                done.set()  # already found the one device we want -- don't wait out the full window

        app.add_i_am_listener(on_i_am)
        try:
            who_is = WhoIsRequest(destination=destination)
            if low_limit is not None:
                who_is.deviceInstanceRangeLowLimit = low_limit
            if high_limit is not None:
                who_is.deviceInstanceRangeHighLimit = high_limit
            app.request(who_is)
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout_ms / 1000.0)
            except asyncio.TimeoutError:
                pass  # normal for the multi-device case -- we wait out the full window
        finally:
            app.remove_i_am_listener(on_i_am)

        return list(collected.values())

    async def read_property(
        self,
        *,
        address: str,
        object_identifier: ObjectIdentifier,
        property_identifier: int | str,
        array_index: int | None = None,
        timeout_ms: int = 5000,
    ) -> Any:
        # Must be a real bacpypes3 ObjectIdentifier, not the "type,instance"
        # string form: Application.read_property() indexes objid[0] directly
        # when looking up the object class to decode the response (see
        # bacpypes3/service/object.py), with no string-to-ObjectIdentifier
        # coercion first. A plain string silently returns its first
        # character there (e.g. "d" from "device,1000") instead of raising,
        # so every read appears to "succeed" but decodes to the literal
        # fallback string "-no object class-". Verified live against a real
        # simulator instance (see PR discussion) -- passing a real
        # ObjectIdentifier fixes it.
        object_id = Bp3ObjectIdentifier(
            (object_identifier.type_name, object_identifier.instance)
        )

        kwargs: dict[str, Any] = {}
        if array_index is not None:
            kwargs["array_index"] = array_index

        try:
            result = await asyncio.wait_for(
                self.application.read_property(
                    Address(address),
                    object_id,
                    property_identifier,
                    **kwargs,
                ),
                timeout=max(timeout_ms / 1000.0, 0.1) + 0.5,
            )
        except ErrorRejectAbortNack as exc:
            # BACpypes3 models Error/Reject/Abort/Nack as BaseException, not
            # Exception (see bacpypes3.apdu.ErrorRejectAbortNack) -- a plain
            # `except Exception` at any caller (retry logic, per-point
            # validation, etc.) does NOT catch it, so a completely normal
            # real-device response like "this object doesn't support
            # Priority_Array" or "unknown property" would otherwise crash the
            # whole caller instead of being treated as one failed read.
            # Verified live against a real device. Re-raise as a normal
            # Exception so every caller's existing `except Exception`
            # handling works as intended.
            raise RuntimeError(str(exc)) from exc

        # Some paths return the Error/Reject/Abort/Nack as a value instead of
        # raising it (see bacpypes3/service/object.py's own
        # `if isinstance(response, ErrorRejectAbortNack): return response`) --
        # normalize that the same way so callers never see a PDU object where
        # they expect a decoded property value.
        if isinstance(result, ErrorRejectAbortNack):
            raise RuntimeError(str(result))

        return result

    async def close(self) -> None:
        # The Application is owned by the simulator; do not close it here.
        return None

    @staticmethod
    def _normalize_i_am(item: Any) -> IAmDevice | None:
        """
        Normalize common BACpypes3 who_is() response shapes.

        Supported examples:
          (device_instance, address)
          (device_instance, address, vendor_id)
          objects exposing deviceIdentifier / pduSource / vendorID
        """
        if isinstance(item, (tuple, list)):
            if len(item) < 2:
                return None
            device_instance = Bacpypes3Transport._instance_value(item[0])
            address = str(item[1])
            vendor_id = int(item[2]) if len(item) > 2 and item[2] is not None else None
            if device_instance is None:
                return None
            return IAmDevice(
                device_instance=device_instance,
                address=Bacpypes3Transport._host_only(address),
                port=Bacpypes3Transport._port_from_address(address),
                vendor_id=vendor_id,
            )

        device_identifier = (
            getattr(item, "deviceIdentifier", None)
            or getattr(item, "iAmDeviceIdentifier", None)
            or getattr(item, "device_identifier", None)
        )
        device_instance = Bacpypes3Transport._instance_value(device_identifier)
        if device_instance is None:
            return None

        source = (
            getattr(item, "pduSource", None)
            or getattr(item, "address", None)
            or getattr(item, "source", None)
        )
        address = str(source) if source is not None else ""
        vendor = (
            getattr(item, "vendorID", None)
            or getattr(item, "vendorIdentifier", None)
            or getattr(item, "vendor_id", None)
        )

        return IAmDevice(
            device_instance=device_instance,
            address=Bacpypes3Transport._host_only(address),
            port=Bacpypes3Transport._port_from_address(address),
            vendor_id=int(vendor) if vendor is not None else None,
        )

    @staticmethod
    def _instance_value(value: Any) -> int | None:
        if isinstance(value, int):
            return value

        if isinstance(value, (tuple, list)) and len(value) >= 2:
            try:
                return int(value[1])
            except (TypeError, ValueError):
                return None

        for attr in ("instance", "instanceNumber", "instance_number"):
            found = getattr(value, attr, None)
            if found is not None:
                try:
                    return int(found)
                except (TypeError, ValueError):
                    pass

        text = str(value)
        for separator in (",", ":"):
            if separator in text:
                tail = text.rsplit(separator, 1)[-1]
                try:
                    return int(tail)
                except ValueError:
                    pass
        return None

    @staticmethod
    def _host_only(address: str) -> str:
        # BACpypes3 IPv4 addresses may include CIDR and/or :port.
        value = address.strip()
        if "/" in value:
            value = value.split("/", 1)[0]
        if ":" in value and value.count(":") == 1:
            host, maybe_port = value.rsplit(":", 1)
            if maybe_port.isdigit():
                return host
        return value

    @staticmethod
    def _port_from_address(address: str) -> int:
        value = address.strip()
        if ":" in value and value.count(":") == 1:
            maybe_port = value.rsplit(":", 1)[1]
            if maybe_port.isdigit():
                return int(maybe_port)
        return 47808
