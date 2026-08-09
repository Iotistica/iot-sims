from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from .client import BACnetClient, ReadOnlyBACnetError
from .discovery import BACnetDiscovery
from .types import (
    BACnetAdapterConfig,
    BACnetDevice,
    BACnetTransport,
    DeviceDataPoint,
    DiscoveredDevice,
    DiscoveryOptions,
)


DataCallback = Callable[[list[DeviceDataPoint]], Awaitable[None] | None]


class BACnetAdapter:
    """
    Read-only network adapter intended for digital-twin/snapshot use.

    It can:
      - discover BACnet devices
      - validate/enumerate their objects
      - read/poll configured points
      - produce baseline-friendly snapshots

    It cannot command an external BACnet device.
    """

    def __init__(
        self,
        config: BACnetAdapterConfig,
        transport: BACnetTransport,
        *,
        logger: logging.Logger | None = None,
        on_data: DataCallback | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.logger = logger or logging.getLogger(__name__)
        self.on_data = on_data

        self.discovery = BACnetDiscovery(transport, self.logger)
        self.clients: dict[str, BACnetClient] = {}
        self.running = False
        self._poll_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_poll_times: dict[str, float] = {}

    async def discover_devices(
        self,
        options: DiscoveryOptions | None = None,
    ) -> list[DiscoveredDevice]:
        return await self.discovery.discover(options)

    async def inspect_device(
        self,
        device: DiscoveredDevice,
        *,
        timeout_ms: int = 10_000,
        max_objects: int | None = None,
        concurrency: int = 4,
    ) -> dict:
        return await self.discovery.validate(
            device,
            timeout_ms=timeout_ms,
            max_objects=max_objects,
            concurrency=concurrency,
        )

    async def create_network_snapshot(
        self,
        options: DiscoveryOptions | None = None,
    ) -> dict:
        opts = options or DiscoveryOptions()
        devices = await self.discover_devices(opts)

        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrent_devices))

        async def inspect(device: DiscoveredDevice) -> dict:
            async with semaphore:
                validation = await self.inspect_device(
                    device,
                    timeout_ms=max(opts.timeout_ms, 10_000),
                    max_objects=opts.max_objects_per_device,
                    concurrency=opts.validation_concurrency,
                )
                return {
                    "name": device.name,
                    "host": device.host,
                    "port": device.port,
                    "deviceInstance": device.device_instance,
                    "fingerprint": device.fingerprint,
                    "metadata": device.metadata,
                    "validation": validation,
                }

        inspected = await asyncio.gather(
            *(inspect(device) for device in devices),
            return_exceptions=True,
        )

        successful: list[dict] = []
        errors: list[dict] = []
        for device, result in zip(devices, inspected):
            if isinstance(result, Exception):
                errors.append({"device": device.name, "error": str(result)})
            else:
                successful.append(result)

        return {
            "snapshotAt": datetime.now(timezone.utc).isoformat(),
            "protocol": "bacnet",
            "readOnly": True,
            "devices": successful,
            "errors": errors,
        }

    async def start(self) -> None:
        if self.running:
            return

        enabled = [device for device in self.config.devices if device.enabled]
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrent_devices))

        async def connect(device: BACnetDevice) -> None:
            async with semaphore:
                client = BACnetClient(device, self.transport, self.logger)
                await client.connect()
                self.clients[device.name] = client

        results = await asyncio.gather(
            *(connect(device) for device in enabled),
            return_exceptions=True,
        )

        for device, result in zip(enabled, results):
            if isinstance(result, Exception):
                self.logger.warning(
                    "BACnet device %s failed to initialize: %s",
                    device.name,
                    result,
                )

        self.running = True
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="bacnet-readonly-poll-loop",
        )

    async def stop(self) -> None:
        if not self.running:
            return

        self.running = False
        self._stop_event.set()

        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        await asyncio.gather(
            *(client.disconnect() for client in self.clients.values()),
            return_exceptions=True,
        )
        self.clients.clear()

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            now = asyncio.get_running_loop().time()
            due: list[BACnetDevice] = []

            for device in self.config.devices:
                if not device.enabled or device.name not in self.clients:
                    continue
                interval = (
                    device.poll_interval_ms
                    if device.poll_interval_ms
                    else self.config.global_poll_interval_ms
                ) / 1000.0
                last = self._last_poll_times.get(device.name, 0.0)
                if now - last >= interval:
                    due.append(device)

            if due:
                semaphore = asyncio.Semaphore(max(1, self.config.max_concurrent_devices))

                async def poll(device: BACnetDevice) -> None:
                    async with semaphore:
                        await self._poll_device(device)

                await asyncio.gather(
                    *(poll(device) for device in due),
                    return_exceptions=True,
                )

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def _poll_device(self, device: BACnetDevice) -> None:
        client = self.clients.get(device.name)
        if client is None:
            return

        self._last_poll_times[device.name] = asyncio.get_running_loop().time()
        enabled_objects = [obj for obj in device.objects if obj.enabled]
        results = await client.read(enabled_objects)

        points: list[DeviceDataPoint] = []
        resolved_display_name = device.display_name

        for obj in enabled_objects:
            result = results.get(obj.name)
            if result is None:
                continue

            raw_name = obj.object_name
            raw_point_name = (
                self.derive_raw_point_name(raw_name, resolved_display_name)
                if raw_name
                else None
            )

            points.append(
                DeviceDataPoint(
                    device_name=device.name,
                    metric=obj.name,
                    value=result.value,
                    unit=obj.unit,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    quality=result.quality,
                    quality_code=result.error,
                    resolved_display_name=resolved_display_name,
                    raw_object_name=raw_name,
                    raw_point_name=raw_point_name,
                )
            )

        if points and self.on_data is not None:
            maybe = self.on_data(points)
            if asyncio.iscoroutine(maybe):
                await maybe

    @staticmethod
    def derive_raw_point_name(
        raw_object_name: str,
        resolved_display_name: str | None = None,
    ) -> str:
        raw = raw_object_name.strip()
        if not raw:
            return raw

        if resolved_display_name and resolved_display_name.strip():
            prefix = resolved_display_name.strip() + "."
            if raw.lower().startswith(prefix.lower()):
                suffix = raw[len(prefix):].strip()
                return suffix or raw

        if "." in raw:
            prefix, suffix = raw.split(".", 1)
            if prefix and suffix.strip():
                return suffix.strip()

        return raw

    async def write_property(self, *_args, **_kwargs) -> None:
        raise ReadOnlyBACnetError(
            "This adapter is read-only. External BACnet writes are intentionally "
            "disabled; modify a simulated scenario instead."
        )
