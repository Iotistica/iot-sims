from __future__ import annotations

import asyncio
from typing import Any

from bacpypes3.pdu import Address

from .types import IAmDevice, ObjectIdentifier


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
        kwargs: dict[str, Any] = {}
        if address:
            # bacpypes3's Application.who_is() reads .is_localstation/
            # .is_remotestation off `address` internally -- a plain str
            # raises AttributeError there, so it must be a real Address.
            kwargs["address"] = Address(address)
        kwargs["low_limit"] = low_limit
        kwargs["high_limit"] = high_limit

        result = await asyncio.wait_for(
            self.application.who_is(**kwargs),
            timeout=max(timeout_ms / 1000.0, 0.1) + 0.5,
        )

        devices: list[IAmDevice] = []
        for item in result or []:
            normalized = self._normalize_i_am(item)
            if normalized is not None:
                devices.append(normalized)
        return devices

    async def read_property(
        self,
        *,
        address: str,
        object_identifier: ObjectIdentifier,
        property_identifier: int | str,
        array_index: int | None = None,
        timeout_ms: int = 5000,
    ) -> Any:
        # BACpypes3 accepts object/property identifiers in its native forms.
        # String object IDs use the documented "object-type,instance" form.
        object_id = f"{object_identifier.type_name},{object_identifier.instance}"

        kwargs: dict[str, Any] = {}
        if array_index is not None:
            kwargs["array_index"] = array_index

        return await asyncio.wait_for(
            self.application.read_property(
                Address(address),
                object_id,
                property_identifier,
                **kwargs,
            ),
            timeout=max(timeout_ms / 1000.0, 0.1) + 0.5,
        )

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
