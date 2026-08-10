from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
from typing import Any, Iterable

from bacpypes3.basetypes import EngineeringUnits

from .types import (
    BACnetObject,
    BACnetProperty,
    DiscoveredDevice,
    DiscoveryOptions,
    IAmDevice,
    OBJECT_TYPE_CODE_TO_NAME,
    OBJECT_TYPE_NAME_TO_CODE,
    ObjectIdentifier,
    BACnetTransport
)


READABLE_POINT_TYPES = {0, 1, 2, 3, 4, 5, 13, 14, 19}
ANALOG_TYPES = {0, 1, 2}
INPUT_TYPES = {0, 3, 13}
POSSIBLY_COMMANDABLE_TYPES = {1, 2, 4, 5, 14, 19}

# Derived from bacpypes3's own EngineeringUnits enum rather than hand-copied,
# same pattern already used by src/bacnet/ede.py's UNIT_CODE table.
NO_UNITS_CODE = int(EngineeringUnits("no-units"))

# Small curated symbol table for common HVAC/energy units. Anything not
# listed here falls back to a humanized version of BACpypes3's own standard
# unit name (see _unit()) instead of a hand-maintained catalog -- mirrors the
# existing agent normalization's curated-symbols-plus-humanized-fallback
# pattern (iot-agent's src/plugins/bacnet/discovery.ts::asUnit()) without
# porting its full cross-protocol canonical-unit/alias system, which is out
# of scope here.
_CURATED_UNIT_SYMBOLS: dict[str, str] = {
    "degrees-celsius": "°C",
    "degrees-fahrenheit": "°F",
    "percent": "%",
    "watts": "W",
    "kilowatts": "kW",
    "watt-hours": "Wh",
    "kilowatt-hours": "kWh",
    "pascals": "Pa",
    "kilopascals": "kPa",
    "square-meters": "m²",
}
UNIT_SYMBOLS: dict[int, str] = {
    int(EngineeringUnits(name)): symbol
    for name, symbol in _CURATED_UNIT_SYMBOLS.items()
}


class BACnetDiscovery:
    """
    Read-only two-phase discovery:
      1) Who-Is/I-Am -> device inventory
      2) Device validation -> Object_List + point metadata/current values
    """

    def __init__(
        self,
        transport: BACnetTransport,
        logger: logging.Logger | None = None,
    ) -> None:
        self.transport = transport
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def generate_fingerprint(ip_address: str, device_instance: int) -> str:
        return hashlib.sha256(
            f"bacnet:{ip_address}:{device_instance}".encode()
        ).hexdigest()[:32]

    @staticmethod
    def _sanitize_device_name(name: str, device_instance: int) -> str:
        normalized = re.sub(r"[^a-z0-9_]", "_", name.lower())
        normalized = re.sub(r"^iotistica_+", "", normalized)
        normalized = normalized.lstrip("_") or "unknown"
        suffix = f"_{device_instance}"
        return normalized if normalized.endswith(suffix) else normalized + suffix

    @staticmethod
    def _sanitize_point_name(name: str) -> str:
        return re.sub(r"[^a-z0-9_]", "_", name.lower())

    @staticmethod
    def _unit(value: Any) -> str | None:
        if value is None:
            return None

        if isinstance(value, str):
            return value.strip() or None

        # A live Units property read comes back as a bacpypes3 EngineeringUnits
        # instance, which is itself int-like (int(value) works directly) --
        # covers both that and a plain int.
        try:
            code = int(value)
        except (TypeError, ValueError):
            return str(value)

        if code == NO_UNITS_CODE:
            return None

        if code in UNIT_SYMBOLS:
            return UNIT_SYMBOLS[code]

        # Humanized fallback via bacpypes3's own standard name for the code
        # (e.g. "cubic-meters-per-hour" -> "cubic meters per hour"), not a
        # hand-maintained table. EngineeringUnits(code) never raises -- an
        # unrecognized code just stringifies back to its own digits, which we
        # turn into an explicit placeholder instead.
        name = str(EngineeringUnits(code))
        if name.isdigit():
            return f"bacnet-unit-{code}"
        return name.replace("-", " ")

    @staticmethod
    def expand_discovery_target(target: str) -> list[str]:
        target = target.strip()

        # Strip an optional :port for IPv4/hostname inputs.
        if target.count(":") == 1:
            host, maybe_port = target.rsplit(":", 1)
            if maybe_port.isdigit():
                target = host

        if "/" in target:
            network = ipaddress.ip_network(target, strict=False)
            # Keep discovery bounded to usable hosts.
            return [str(ip) for ip in network.hosts()]

        range_match = re.fullmatch(
            r"(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3}(?:\.\d{1,3}){3})",
            target,
        )
        if range_match:
            start = int(ipaddress.ip_address(range_match.group(1)))
            end = int(ipaddress.ip_address(range_match.group(2)))
            if end < start:
                raise ValueError(f"Invalid descending IP range: {target}")
            return [str(ipaddress.ip_address(value)) for value in range(start, end + 1)]

        try:
            ipaddress.ip_address(target)
            return [target]
        except ValueError:
            # Hostname: resolve once so targeted Who-Is has an address.
            return [socket.gethostbyname(target)]

    async def discover(
        self,
        options: DiscoveryOptions | None = None,
    ) -> list[DiscoveredDevice]:
        opts = options or DiscoveryOptions()
        low, high = opts.device_id_range

        replies: list[IAmDevice] = []

        if opts.discovery_targets:
            expanded: list[str] = []
            for target in opts.discovery_targets:
                expanded.extend(self.expand_discovery_target(target))

            # Avoid flooding large CIDRs all at once.
            semaphore = asyncio.Semaphore(32)

            async def target_who_is(host: str) -> None:
                async with semaphore:
                    try:
                        found = await self.transport.who_is(
                            address=host,
                            low_limit=low,
                            high_limit=high,
                            timeout_ms=opts.timeout_ms,
                        )
                        replies.extend(found)
                    except Exception as exc:
                        self.logger.debug("Who-Is failed for %s: %s", host, exc)

            await asyncio.gather(*(target_who_is(host) for host in expanded))
            discovery_method = "who_is_unicast"
        else:
            found = await self.transport.who_is(
                address=opts.broadcast_address,
                low_limit=low,
                high_limit=high,
                timeout_ms=opts.timeout_ms,
            )
            replies.extend(found)
            discovery_method = "who_is_broadcast"

        by_instance: dict[int, IAmDevice] = {}
        for reply in replies:
            by_instance.setdefault(reply.device_instance, reply)
            if len(by_instance) >= opts.max_devices:
                break

        discovered: list[DiscoveredDevice] = []

        for device_instance, reply in by_instance.items():
            metadata: dict[str, Any] = {
                "deviceInstance": device_instance,
                "vendorId": reply.vendor_id,
                "discoveryMethod": discovery_method,
            }

            for key, prop in (
                ("objectName", BACnetProperty.OBJECT_NAME),
                ("vendorName", BACnetProperty.VENDOR_NAME),
                ("modelName", BACnetProperty.MODEL_NAME),
                ("description", BACnetProperty.DESCRIPTION),
            ):
                try:
                    value = await self.transport.read_property(
                        address=reply.address,
                        object_identifier=ObjectIdentifier(8, device_instance),
                        property_identifier=int(prop),
                        timeout_ms=opts.timeout_ms,
                    )
                    if value is not None:
                        metadata[key] = str(value)
                except Exception as exc:
                    self.logger.debug(
                        "Failed reading %s for device %s: %s",
                        key,
                        device_instance,
                        exc,
                    )

            display = metadata.get("objectName") or f"bacnet_device_{device_instance}"
            discovered.append(
                DiscoveredDevice(
                    name=self._sanitize_device_name(display, device_instance),
                    fingerprint=self.generate_fingerprint(reply.address, device_instance),
                    host=reply.address,
                    port=reply.port or opts.port,
                    device_instance=device_instance,
                    confidence="high" if metadata.get("objectName") else "medium",
                    metadata=metadata,
                )
            )

        return discovered

    async def validate(
        self,
        device: DiscoveredDevice,
        *,
        timeout_ms: int = 10_000,
        max_objects: int | None = None,
        concurrency: int = 4,
    ) -> dict[str, Any]:
        object_list_value = await self.transport.read_property(
            address=device.host,
            object_identifier=ObjectIdentifier(8, device.device_instance),
            property_identifier=int(BACnetProperty.OBJECT_LIST),
            timeout_ms=timeout_ms,
        )

        refs = self._normalize_object_list(object_list_value)
        if max_objects is not None:
            refs = refs[:max_objects]

        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def inspect(ref: ObjectIdentifier) -> tuple[BACnetObject, Any] | None:
            # Skip Device object as a "point", but retaining it in raw inventory
            # can be added later if desired.
            if ref.type == 8:
                return None

            async with semaphore:
                try:
                    object_name_value = await self.transport.read_property(
                        address=device.host,
                        object_identifier=ref,
                        property_identifier=int(BACnetProperty.OBJECT_NAME),
                        timeout_ms=timeout_ms,
                    )
                    object_name = (
                        str(object_name_value)
                        if object_name_value is not None
                        else f"{ref.type_name}_{ref.instance}"
                    )

                    # Present-Value, unlike Object_Name, is treated as a
                    # property that may individually fail or be absent
                    # (real BACnet devices vary considerably) -- guarded the
                    # same way Units/Priority_Array already are below, so
                    # one bad read degrades to None instead of dropping the
                    # whole object from the scan.
                    present_value: Any = None
                    if ref.type in READABLE_POINT_TYPES:
                        try:
                            present_value = await self.transport.read_property(
                                address=device.host,
                                object_identifier=ref,
                                property_identifier=int(BACnetProperty.PRESENT_VALUE),
                                timeout_ms=timeout_ms,
                            )
                        except Exception:
                            present_value = None

                    description: str | None = None
                    try:
                        raw_description = await self.transport.read_property(
                            address=device.host,
                            object_identifier=ref,
                            property_identifier=int(BACnetProperty.DESCRIPTION),
                            timeout_ms=timeout_ms,
                        )
                        description = str(raw_description) if raw_description is not None else None
                    except Exception:
                        description = None

                    unit: str | None = None
                    if ref.type in ANALOG_TYPES:
                        try:
                            raw_unit = await self.transport.read_property(
                                address=device.host,
                                object_identifier=ref,
                                property_identifier=int(BACnetProperty.UNITS),
                                timeout_ms=timeout_ms,
                            )
                            unit = self._unit(raw_unit)
                        except Exception:
                            unit = None

                    source_commandable = False
                    if ref.type in POSSIBLY_COMMANDABLE_TYPES:
                        try:
                            await self.transport.read_property(
                                address=device.host,
                                object_identifier=ref,
                                property_identifier=int(BACnetProperty.PRIORITY_ARRAY),
                                timeout_ms=timeout_ms,
                            )
                            source_commandable = True
                        except Exception:
                            source_commandable = False

                    obj = BACnetObject(
                        name=self._sanitize_point_name(object_name),
                        object_name=object_name,
                        object_type=ref.type_name,
                        object_instance=ref.instance,
                        unit=unit,
                        source_commandable=source_commandable,
                        description=description,
                    )
                    return obj, present_value
                except Exception as exc:
                    self.logger.debug(
                        "Failed to inspect %s,%s on %s: %s",
                        ref.type_name,
                        ref.instance,
                        device.name,
                        exc,
                    )
                    return None

        inspected = await asyncio.gather(*(inspect(ref) for ref in refs))
        results = [item for item in inspected if item is not None]
        objects = [obj for obj, _present_value in results]

        # Validated point data, reusing the Present-Value each inspect() call
        # above already read -- avoids a second network round trip per point.
        # ("Snapshot" is a separate, out-of-scope future feature; this is just
        # the current values captured during this validation pass.)
        validated_points: list[dict[str, Any]] = [
            {
                "name": obj.name,
                "objectName": obj.object_name,
                "objectType": obj.object_type,
                "objectInstance": obj.object_instance,
                "presentValue": present_value,
                "unit": obj.unit,
                "description": obj.description,
                "sourceCommandable": obj.source_commandable,
                # Effective product policy:
                "writable": False,
            }
            for obj, present_value in results
        ]

        device.data_points = objects
        device.validated = True

        return {
            "manufacturer": device.metadata.get("vendorName"),
            "modelNumber": device.metadata.get("modelName"),
            "capabilities": sorted({obj.object_type for obj in objects}),
            "deviceInfo": {
                "totalObjects": len(objects),
                "analogInputs": sum(o.object_type == "analog-input" for o in objects),
                "analogOutputs": sum(o.object_type == "analog-output" for o in objects),
                "binaryInputs": sum(o.object_type == "binary-input" for o in objects),
                "binaryOutputs": sum(o.object_type == "binary-output" for o in objects),
            },
            "dataPoints": validated_points,
            "readOnly": True,
        }

    async def read_present_values(
        self,
        host: str,
        device_instance: int,
        points: list[tuple[str, int]],
        *,
        timeout_ms: int = 10_000,
        concurrency: int = 4,
    ) -> dict[tuple[str, int], Any]:
        """
        Re-reads just Present-Value for an already-known set of objects
        (object_type name, object_instance), skipping the Object_List read
        validate() always does first. Used for a lightweight "Refresh"
        action distinct from "Rediscover Objects" -- object inventory
        discovery answers "what objects exist", this answers "what are
        their current values" for objects already known. device_instance is
        accepted for symmetry with validate()'s call shape but isn't
        currently needed by read_property() itself (address is enough to
        reach the device over BACnet/IP).
        """
        semaphore = asyncio.Semaphore(max(1, concurrency))
        results: dict[tuple[str, int], Any] = {}

        async def read_one(object_type: str, object_instance: int) -> None:
            code = OBJECT_TYPE_NAME_TO_CODE.get(object_type)
            if code is None:
                results[(object_type, object_instance)] = None
                return
            async with semaphore:
                try:
                    value = await self.transport.read_property(
                        address=host,
                        object_identifier=ObjectIdentifier(code, object_instance),
                        property_identifier=int(BACnetProperty.PRESENT_VALUE),
                        timeout_ms=timeout_ms,
                    )
                except Exception:
                    value = None
                results[(object_type, object_instance)] = value

        await asyncio.gather(*(read_one(t, i) for t, i in points))
        return results

    @staticmethod
    def _normalize_object_list(value: Any) -> list[ObjectIdentifier]:
        refs: list[ObjectIdentifier] = []
        for item in value or []:
            if isinstance(item, ObjectIdentifier):
                refs.append(item)
                continue

            if isinstance(item, (tuple, list)) and len(item) >= 2:
                try:
                    refs.append(ObjectIdentifier(int(item[0]), int(item[1])))
                    continue
                except (TypeError, ValueError):
                    pass

            type_value = getattr(item, "type", None)
            instance_value = getattr(item, "instance", None)
            if type_value is not None and instance_value is not None:
                try:
                    refs.append(ObjectIdentifier(int(type_value), int(instance_value)))
                    continue
                except (TypeError, ValueError):
                    pass

            # BACpypes3 ObjectIdentifier-like values are often stringifiable.
            text = str(item)
            for separator in (",", ":"):
                if separator in text:
                    left, right = text.rsplit(separator, 1)
                    try:
                        instance = int(right)
                    except ValueError:
                        continue

                    type_code = None
                    if left.isdigit():
                        type_code = int(left)
                    else:
                        for code, name in OBJECT_TYPE_CODE_TO_NAME.items():
                            if str(name) == left:
                                type_code = code
                                break
                    if type_code is not None:
                        refs.append(ObjectIdentifier(type_code, instance))
                        break

        return refs
