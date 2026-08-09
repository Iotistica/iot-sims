from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from .types import (
    BACnetDevice,
    BACnetObject,
    BACnetProperty,
    BACnetReadResult,
    OBJECT_TYPE_NAME_TO_CODE,
    ObjectIdentifier,
    BACnetTransport
)


class ReadOnlyBACnetError(PermissionError):
    pass


class BACnetClient:
    """
    Read-only BACnet client for one configured/discovered device.

    This is deliberately different from the Edge Agent client:
    - reads/retries/concurrency are retained
    - external WriteProperty is not implemented
    - write_property()/write() explicitly fail closed so accidental callers
      cannot silently command a production BACnet network
    """

    def __init__(
        self,
        config: BACnetDevice,
        transport: BACnetTransport,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.logger = logger or logging.getLogger(__name__)
        self.connected = False
        self.last_error: str | None = None

    async def connect(self) -> None:
        # BACnet/IP is connectionless. Verify reachability with a targeted Who-Is.
        replies = await self.transport.who_is(
            address=self.config.ip_address,
            low_limit=self.config.device_instance,
            high_limit=self.config.device_instance,
            timeout_ms=self.config.connection_timeout_ms,
        )
        if not any(r.device_instance == self.config.device_instance for r in replies):
            raise TimeoutError(
                f"BACnet device {self.config.name} "
                f"({self.config.device_instance}) did not answer Who-Is"
            )
        self.connected = True
        self.last_error = None

    async def disconnect(self) -> None:
        # Transport/Application ownership is above this per-device client.
        self.connected = False

    async def read_device_name(self) -> str | None:
        if not self.connected:
            return None
        try:
            value = await self.transport.read_property(
                address=self.config.ip_address,
                object_identifier=ObjectIdentifier(8, self.config.device_instance),
                property_identifier=int(BACnetProperty.OBJECT_NAME),
                timeout_ms=self.config.connection_timeout_ms,
            )
            return str(value).strip() if value is not None and str(value).strip() else None
        except Exception:
            return None

    async def read_object(
        self,
        obj: BACnetObject,
        remaining_retries: int | None = None,
    ) -> BACnetReadResult:
        if not self.connected:
            return BACnetReadResult(value=None, quality="BAD", error="Not connected")

        retries = (
            self.config.retry_attempts
            if remaining_retries is None
            else remaining_retries
        )

        object_type_num = OBJECT_TYPE_NAME_TO_CODE.get(obj.object_type)
        if object_type_num is None:
            return BACnetReadResult(
                value=None,
                quality="BAD",
                error=f"Unknown object type: {obj.object_type}",
            )

        try:
            value = await self.transport.read_property(
                address=self.config.ip_address,
                object_identifier=ObjectIdentifier(
                    object_type_num,
                    obj.object_instance,
                ),
                property_identifier=obj.property_id,
                timeout_ms=self.config.connection_timeout_ms,
            )
            return BACnetReadResult(value=value, quality="GOOD")
        except Exception as exc:
            if retries > 0:
                await asyncio.sleep(self.config.retry_delay_ms / 1000.0)
                return await self.read_object(obj, retries - 1)

            self.last_error = str(exc)
            self.logger.warning(
                "Error reading %s from %s: %s",
                obj.name,
                self.config.name,
                exc,
            )
            return BACnetReadResult(value=None, quality="BAD", error=str(exc))

    async def read_objects(
        self,
        objects: Iterable[BACnetObject] | None = None,
    ) -> dict[str, BACnetReadResult]:
        selected = list(objects if objects is not None else self.config.objects)
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrent_reads))
        results: dict[str, BACnetReadResult] = {}

        async def one(obj: BACnetObject) -> None:
            async with semaphore:
                results[obj.name] = await self.read_object(obj)

        await asyncio.gather(*(one(obj) for obj in selected))
        return results

    async def read(
        self,
        objects: Iterable[BACnetObject] | None = None,
    ) -> dict[str, BACnetReadResult]:
        return await self.read_objects(objects)

    def get_object_config(self, point_name: str) -> BACnetObject | None:
        return next(
            (
                obj
                for obj in self.config.objects
                if obj.name == point_name
                or obj.object_name == point_name
                or (
                    obj.object_name is not None
                    and obj.object_name.split(".")[-1] == point_name
                )
            ),
            None,
        )

    async def write_property(self, *_args, **_kwargs) -> None:
        raise ReadOnlyBACnetError(
            "External BACnet writes are disabled by design. "
            "Create a simulation scenario and modify the simulated copy instead."
        )

    async def write(self, *_args, **_kwargs) -> None:
        raise ReadOnlyBACnetError(
            "External BACnet writes are disabled by design. "
            "Create a simulation scenario and modify the simulated copy instead."
        )

    def is_connected(self) -> bool:
        return self.connected

    def get_last_error(self) -> str | None:
        return self.last_error
