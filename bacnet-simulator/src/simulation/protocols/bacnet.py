"""BacnetProtocolAdapter -- Step 2: the real BACnet implementation, not a delegate.

BACnet's own device/point creation is still a batch operation (shared NetworkPortObject,
primary-device selection, device object-list assembly, calendars, trend logs, schedules, and I-Am
announcements, all built together across the whole participating device set at once) -- Step 2
deliberately keeps that shape rather than decomposing it into independent per-device
add_device()/add_point() calls (that restructuring carries real behavioral risk -- shared network
port, primary-device selection, calendar-before-schedule ordering -- for no functional gain, since
every other protocol already gets the decomposed treatment via SimEngine's own generic per-protocol
loop). What Step 2 DOES do: `start()` below is the actual BACnet startup implementation (base IP
resolution, packet-capture hooks, the BACnet-specific asyncio exception handler,
NetworkPortObject/SimApplication construction, per-device object/calendar/trend-log/schedule
construction, I-Am announcements), and `stop()` is the actual teardown implementation -- both
physically live here now, not in SimEngine. `SimEngine.start()`/`stop()`/`reload()` call
`self._protocol_adapters["bacnet"].start(...)`/`.stop()` exactly like they do for every other
protocol; there is no SimEngine method that performs BACnet startup or teardown directly anymore.

Object/device CONSTRUCTION itself (building one bacpypes3 DeviceObject, one bacpypes3 point
object + its initial Behavior) lives one level further out, in src/bacnet/device_factory.py and
src/bacnet/object_factory.py -- small, focused modules `start()` and `add_device()`/`add_point()`
below all share, so there is exactly one place each of those two constructions happens.

add_device()/add_point()/remove_device()/remove_point() are real, individually-correct
implementations -- mirroring the exact patterns SimEngine.add_object_hot() and this adapter's own
stop() use -- but `start()`'s own batch loop does NOT call them internally (it calls the two
factory modules directly, same as the pre-Step-2 code did): that decomposition remains explicitly
out of scope (see above). They exist for interface completeness and direct testability.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, TYPE_CHECKING

from bacpypes3.basetypes import (
    BinaryPV,
    DeviceObjectPropertyReference,
    LoggingType,
    LogRecord,
    Reliability,
)
from bacpypes3.constructeddata import SequenceOf
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.primitivedata import Boolean, Real, Unsigned

from .base import ProtocolAdapter, ProtocolContext
from ..protocol_runtime import DeviceRuntimeConfig, PointQuality, PointRuntimeConfig, PointValue, RuntimePoint
from ...bacnet import calendar as bacnet_calendar
from ...bacnet import device_factory, object_factory, schedule as bacnet_schedule
from ...bacnet.app import SimApplication, _apply_reliability, _force_close_bacnet_transports, _resolve_base_ip, install_bacpypes_packet_capture_hooks
from ...bacnet.trend_logs import LocalTrendLogObject, _build_log_record
from ...core.config import BACNET_PORT, MULTISTATE_TYPES

if TYPE_CHECKING:
    from ..engine import SimEngine

log = logging.getLogger("bacnet-sim")


class BacnetProtocolAdapter(ProtocolAdapter):
    protocol = "bacnet"
    builds_runtime_points_in_start = True

    def __init__(self, engine: "SimEngine") -> None:
        self._engine = engine

    async def start(self, context: ProtocolContext) -> None:
        """The full BACnet startup batch -- base IP resolution, packet-capture hooks, the
        BACnet-specific asyncio exception handler, NetworkPortObject/SimApplication construction,
        per-device live object/calendar/trend-log/schedule construction, and I-Am announcements.

        Feeds engine._boot_provider_participants/_boot_provider_points -- set fresh by
        SimEngine.start() before any adapter runs -- exactly like the generic per-protocol loop in
        SimEngine.start() does for every other protocol, so the one shared
        engine._builtin_provider.initialize(...) call afterward sees every protocol's points
        regardless of which adapter produced them.

        I-Am runs at the END of this method, before returning -- it has no dependency on provider
        state either way, and this is what keeps the entire BACnet-only startup surface inside one
        call SimEngine.start() makes and never has to sequence around."""
        engine = self._engine
        bacnet_devices = [dict(d.protocol_config) for d in context.devices]
        if not bacnet_devices:
            engine.app = None
            return

        base_ip = _resolve_base_ip()

        install_bacpypes_packet_capture_hooks(
            local_ip=base_ip,
            local_port=BACNET_PORT,
            get_clock_state=lambda: engine.clock_state,
        )

        loop = asyncio.get_running_loop()
        orig = loop.get_exception_handler()

        def _exc_handler(loop, ctx):
            exc = ctx.get("exception")
            if isinstance(exc, RuntimeError) and str(exc) == "no broadcast":
                return
            if orig:
                orig(loop, ctx)
            else:
                loop.default_exception_handler(ctx)

        loop.set_exception_handler(_exc_handler)

        primary = bacnet_devices[0]
        bind_addr = f"{base_ip}:{BACNET_PORT}"

        primary_dev_obj = device_factory.make_device_object(engine, primary)
        engine.network_port = NetworkPortObject(
            bind_addr,
            objectIdentifier=("network-port", 1),
            objectName="NetworkPort-1",
        )

        engine.app = SimApplication.from_object_list([primary_dev_obj, engine.network_port])
        engine.app._sim_engine = engine
        engine.app._own_ip = base_ip  # for filtering our own I-Am loopback in duplicate-ID detection
        engine._used_object_identifiers.clear()
        await asyncio.sleep(0.3)

        engine.app._virtual_devices[primary["device_instance"]] = primary_dev_obj
        engine._device_slots = {d["device_instance"]: i for i, d in enumerate(bacnet_devices)}

        log.info("BACnet socket bound to %s", bind_addr)

        for idx, dev in enumerate(bacnet_devices):
            slot = idx
            engine._boot_provider_participants.append(int(dev["id"]))
            if idx == 0:
                dev_obj = primary_dev_obj
            else:
                dev_obj = device_factory.make_device_object(engine, dev)
                engine.app._virtual_devices[dev["device_instance"]] = dev_obj

            objects = await asyncio.to_thread(engine.db.get_objects, dev["id"])
            bacnet_ids = [dev_obj.objectIdentifier]
            if idx == 0:
                bacnet_ids.append(engine.network_port.objectIdentifier)

            for obj_row in objects:
                if not obj_row["enabled"]:
                    continue
                bacnet_obj, behavior = object_factory.create_object(engine, obj_row, slot, dev["name"])
                try:
                    engine.app.add_object(bacnet_obj)
                except RuntimeError:
                    log.exception(
                        "Failed to add object %r as %r on device %r "
                        "(name/identifier collision?) — skipping",
                        obj_row["name"], bacnet_obj.objectIdentifier, dev["name"],
                    )
                    continue
                engine._runtime_points[obj_row["id"]] = RuntimePoint(
                    config=engine._point_runtime_config(obj_row),
                    adapter=self,
                    handle=bacnet_obj,
                    behavior=behavior,
                )
                bacnet_ids.append(bacnet_obj.objectIdentifier)

                # Mirror points remain driven by mirror_sync_loop. Everything
                # else follows the historical built-in behavior path.
                if (
                    dev.get("simulation_mode", "simulation") != "mirror"
                    and int(obj_row["id"]) not in engine._point_output_owner
                ):
                    engine._boot_provider_points.append(engine._point_config_from_row(obj_row))

            # Calendars (GH #18) must be built before Schedules below, since a
            # Schedule's exceptionSchedule may reference one by name.
            calendars = await asyncio.to_thread(engine.db.get_calendars, dev["id"])
            calendar_phys_by_name: dict[str, int] = {}
            for cal_idx, cal in enumerate(calendars):
                if not cal["enabled"]:
                    continue
                try:
                    entries = json.loads(cal["date_list"] or "[]")
                    phys = slot * 1000 + cal_idx + 1
                    cal_bacnet_obj = bacnet_calendar.LocalCalendarObject(
                        objectIdentifier=("calendar", phys),
                        objectName=f"{dev['name']}.{cal['name']}",
                        description=cal.get("description", ""),
                        presentValue=Boolean(bacnet_calendar.today_in_date_list(entries)),
                        dateList=bacnet_calendar.build_date_list(entries),
                    )
                    engine.app.add_object(cal_bacnet_obj)
                    engine._calendar_objects[cal["id"]] = cal_bacnet_obj
                    calendar_phys_by_name[cal["name"]] = phys
                    bacnet_ids.append(cal_bacnet_obj.objectIdentifier)
                except Exception:
                    log.exception("Failed to build calendar %r on device %r — skipping", cal["name"], dev["name"])

            trend_logs = await asyncio.to_thread(engine.db.get_trend_logs, dev["id"])
            for tl_idx, tl in enumerate(trend_logs):
                monitored = engine._runtime_points.get(tl["monitored_object_id"])
                if monitored is None:
                    continue  # monitored object disabled/missing — skip exposing on the wire
                monitored_objid = monitored.handle.objectIdentifier
                records = await asyncio.to_thread(
                    engine.db.get_trend_log_records, tl["id"], limit=tl["buffer_size"], order="asc"
                )
                log_buffer = SequenceOf(LogRecord)(
                    [_build_log_record(r, monitored_objid[0]) for r in records]
                )
                try:
                    tl_bacnet_obj = LocalTrendLogObject(
                        objectIdentifier=("trend-log", slot * 1000 + tl_idx + 1),
                        objectName=f"{dev['name']}.{tl['name']}",
                        description=tl.get("description", ""),
                        enable=Boolean(bool(tl["enabled"])),
                        stopWhenFull=Boolean(bool(tl["stop_when_full"])),
                        bufferSize=Unsigned(tl["buffer_size"]),
                        logBuffer=log_buffer,
                        recordCount=Unsigned(tl["record_count"]),
                        totalRecordCount=Unsigned(tl["total_record_count"]),
                        loggingType=LoggingType(tl["logging_type"]),
                        statusFlags=[0, 0, 0, 0],
                        reliability=Reliability("no-fault-detected"),
                        logDeviceObjectProperty=DeviceObjectPropertyReference(
                            objectIdentifier=monitored_objid,
                            propertyIdentifier="present-value",
                        ),
                        logInterval=Unsigned(tl.get("log_interval") or 0),
                    )
                    engine.app.add_object(tl_bacnet_obj)
                    engine._trend_log_objects[tl["id"]] = tl_bacnet_obj
                    bacnet_ids.append(tl_bacnet_obj.objectIdentifier)
                except Exception:
                    log.exception("Failed to build trend log %r on device %r — skipping", tl["name"], dev["name"])

            schedules = await asyncio.to_thread(engine.db.get_schedules, dev["id"])
            for sched_idx, sched in enumerate(schedules):
                if not sched["enabled"]:
                    continue  # same convention as disabled regular objects: not built at all
                targets = await asyncio.to_thread(engine.db.get_schedule_targets, sched["id"])
                obj_prop_refs = []
                for t in targets:
                    target_entry = engine._runtime_points.get(t["object_id"])
                    if target_entry is None:
                        continue  # target object disabled/missing — skip that reference
                    obj_prop_refs.append(DeviceObjectPropertyReference(
                        objectIdentifier=target_entry.handle.objectIdentifier,
                        propertyIdentifier=t.get("property_identifier", "present-value"),
                    ))
                try:
                    value_type = sched.get("value_type", "real")
                    default_raw = json.loads(sched["schedule_default"] or "0")
                    sched_bacnet_obj = bacnet_schedule.LocalScheduleObject(
                        objectIdentifier=("schedule", slot * 1000 + sched_idx + 1),
                        objectName=f"{dev['name']}.{sched['name']}",
                        description=sched.get("description", ""),
                        presentValue=bacnet_schedule.default_value(value_type, default_raw),
                        effectivePeriod=bacnet_schedule.build_effective_period(
                            sched.get("effective_start"), sched.get("effective_end")
                        ),
                        weeklySchedule=bacnet_schedule.build_weekly_schedule(
                            json.loads(sched["weekly_schedule"] or "{}"), value_type
                        ),
                        exceptionSchedule=bacnet_schedule.build_exception_schedule(
                            json.loads(sched["exception_schedule"] or "[]"), value_type, calendar_phys_by_name
                        ),
                        scheduleDefault=bacnet_schedule.default_value(value_type, default_raw),
                        listOfObjectPropertyReferences=SequenceOf(DeviceObjectPropertyReference)(obj_prop_refs),
                        priorityForWriting=Unsigned(sched["priority_for_writing"]),
                    )
                    sched_bacnet_obj._value_type = value_type
                    engine.app.add_object(sched_bacnet_obj)
                    engine._schedule_objects[sched["id"]] = sched_bacnet_obj
                    bacnet_ids.append(sched_bacnet_obj.objectIdentifier)
                except Exception:
                    log.exception("Failed to build schedule %r on device %r — skipping", sched["name"], dev["name"])

            dev_obj.objectList = bacnet_ids
            engine.app._virtual_object_lists[dev["device_instance"]] = bacnet_ids
            log.info("Device %d (%s): %d objects", dev["device_instance"], dev["name"], len(objects))

        # Announce all devices
        saved = engine.app.device_object
        try:
            for dev_obj in engine.app._virtual_devices.values():
                engine.app.device_object = dev_obj
                engine.app.i_am()
        finally:
            engine.app.device_object = saved

    async def stop(self) -> None:
        """The BACnet teardown batch -- called by SimEngine.stop()/reload() exactly like every
        other protocol's adapter.stop(). A no-op when engine.app is already None (nothing running
        this cycle), the same gating the old inline `if self.app: ...` block provided -- so
        calling this unconditionally every stop()/reload() cycle is always safe, including for a
        null-only project that never started BACnet at all."""
        engine = self._engine
        if engine.app is None:
            return
        for runtime_point in list(engine._runtime_points.values()):
            try:
                engine.app.delete_object(runtime_point.handle)
            except Exception:
                pass
        for bacnet_obj in list(engine._trend_log_objects.values()):
            try:
                engine.app.delete_object(bacnet_obj)
            except Exception:
                pass
        for bacnet_obj in list(engine._schedule_objects.values()):
            if getattr(bacnet_obj, "_interpret_schedule_handle", None):
                bacnet_obj._interpret_schedule_handle.cancel()
            try:
                engine.app.delete_object(bacnet_obj)
            except Exception:
                pass
        for bacnet_obj in list(engine._calendar_objects.values()):
            try:
                engine.app.delete_object(bacnet_obj)
            except Exception:
                pass
        engine._trend_log_objects.clear()
        engine._schedule_objects.clear()
        engine._calendar_objects.clear()
        # Explicitly close the bacpypes3 socket before dropping the reference.
        # BinaryOutputObject↔PriorityArray form a circular reference that delays
        # GC, keeping the UDP socket bound to port 47808 and preventing re-bind.
        try:
            await engine.app.close()
        except Exception:
            pass
        finally:
            # Always sweep for any endpoint task close() didn't get to, so a
            # failure here can never leak a live socket bind past this
            # teardown (see the helper's own docstring).
            try:
                _force_close_bacnet_transports(engine.app)
            except Exception:
                pass
        engine.app = None

    def add_device(self, device: DeviceRuntimeConfig) -> Any:
        """Constructs (but does not register with a running BACnet application) one BACnet
        DeviceObject -- same factory start()'s own batch loop uses."""
        return device_factory.make_device_object(self._engine, dict(device.protocol_config))

    def remove_device(self, handle: Any) -> None:
        if self._engine.app is not None:
            try:
                self._engine.app.delete_object(handle)
            except Exception:
                pass

    def add_point(self, device_handle: Any, point: PointRuntimeConfig) -> Any:
        """Same slot-lookup convention SimEngine.add_object_hot() already uses for a hot-added
        point on an already-running device: the physical-instance-numbering offset comes from
        SimEngine._device_slots, keyed by the device's own BACnet instance number (recoverable
        from device_handle.objectIdentifier -- a live bacpypes3 DeviceObject's identifier is
        ("device", instance))."""
        device_instance = int(device_handle.objectIdentifier[1])
        slot = self._engine._device_slots.get(device_instance, 0)
        device_name = str(device_handle.objectName)
        bacnet_obj, _behavior = object_factory.create_object(self._engine, dict(point.protocol_config), slot, device_name)
        return bacnet_obj

    def remove_point(self, handle: Any) -> None:
        if self._engine.app is not None:
            try:
                self._engine.app.delete_object(handle)
            except Exception:
                pass

    def write_point(self, handle: Any, update: PointValue) -> Any:
        """`handle` is a live bacpypes3 object -- object_type and this object's own persisted
        (non-fault) reliability were stashed onto it at creation time (see object_factory.
        create_object()'s own comment) since this method otherwise has no obj_row to read them
        from."""
        otype = getattr(handle, "_sim_object_type", None)
        if not otype:
            raise ValueError("BACnet point handle is missing _sim_object_type")
        written = self._update_value(handle, otype, update.value)
        configured_reliability = getattr(handle, "_sim_configured_reliability", None) or "no-fault-detected"
        reliability = "communication-failure" if update.quality == PointQuality.OFFLINE else configured_reliability
        _apply_reliability(handle, reliability)
        return written

    @staticmethod
    def _update_value(bacnet_obj: Any, otype: str, val: Any) -> Any:
        """Writes the live BACnet presentValue and returns the exact value
        actually written (post type-coercion/clamping) -- this is the one
        place that knows what really landed on the wire, e.g. a multi-state
        object's [1, numberOfStates] clamp. Callers that separately track a
        "current value" for /sim/state, the Admin UI, Mirror sync, or manual
        overrides should use this return value rather than re-deriving their
        own, so that tracked value can't drift from what Present_Value
        actually reads -- see SimEngine.tick(), inject_mirror_values, and
        set_manual_value, all of which previously stored the pre-clamp raw
        value instead and could disagree with a real BACnet client's read.

        A @staticmethod, not an instance method that reads self._engine --
        writing an already-live bacpypes3 object's presentValue needs no
        engine state at all, only the object, its type, and the value."""
        if otype in ("analog-input", "analog-output", "analog-value"):
            written = float(val)
            bacnet_obj.presentValue = Real(written)
        elif otype == "binary-output":
            written = bool(val) if not isinstance(val, bool) else val
            bacnet_obj.presentValue = BinaryPV("active" if written else "inactive")  # triggers recalculating() via priorityArray
        elif otype in MULTISTATE_TYPES:
            n_states = int(bacnet_obj.numberOfStates)
            written = max(1, min(n_states, round(float(val))))
            bacnet_obj.presentValue = Unsigned(written)
        else:
            written = bool(val) if not isinstance(val, bool) else val
            bacnet_obj.presentValue = BinaryPV("active" if written else "inactive")
        return written
