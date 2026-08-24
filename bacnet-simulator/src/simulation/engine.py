"""BACnet simulation engine.

Physically extracted from src/legacy.py's `SimEngine` -- the tick/
reconciliation core that owns the running `SimApplication` and steps every
simulated point's `Behavior`/FMU provider each tick. Continuing the GH #15
refactor, same "moved verbatim, no behavior changes" standard as the
Database and API-router extractions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Optional

from bacpypes3.basetypes import (
    BinaryPV,
    DeviceObjectPropertyReference,
    EngineeringUnits,
    LoggingType,
    LogRecord,
    PriorityValue,
    Reliability,
    Segmentation,
)
from bacpypes3.local.analog import AnalogInputObject, AnalogOutputObject, AnalogValueObject
from bacpypes3.local.binary import BinaryInputObject, BinaryOutputObject, BinaryValueObject
from bacpypes3.local.cmd import Commandable
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.multistate import MultiStateInputObject, MultiStateOutputObject, MultiStateValueObject
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.primitivedata import Boolean, Real, Unsigned
from bacpypes3.constructeddata import SequenceOf
from fastapi import WebSocket

from ..bacnet import alarms
from ..bacnet import calendar as bacnet_calendar
from ..bacnet import schedule as bacnet_schedule
from ..bacnet.app import (
    SimApplication,
    _apply_polarity,
    _apply_reliability,
    _force_close_bacnet_transports,
    _resolve_base_ip,
    coerce_binary_write_value,
    install_bacpypes_packet_capture_hooks,
    normalize_present_value,
)
from ..bacnet.trend_logs import LocalTrendLogObject, _build_log_record
from ..core.config import BACNET_PORT, MULTISTATE_TYPES
from .. import dependencies
from ..db import Database
from ..fault_detection import FaultDetectionEngine
from ..monitoring.event_log import _log_event, _log_event_notification_received
from ..energy import EnergyEngine
from .behaviors import Behavior, ManualBehavior, make_behavior
from .providers.base import PointConfig, ProviderStatus, SimulationContext, SimulationProvider
from .providers.builtin import BuiltInSimulationProvider
from .state import SimState

log = logging.getLogger("bacnet-sim")


class SimEngine:
    """Manages the running BACnet application and the simulation tick loop."""

    def __init__(self, db: Database):
        self.db = db
        self.state = SimState()
        self.app: Optional[SimApplication] = None
        self.network_port: Optional[NetworkPortObject] = None
        # object DB id → (bacpypes3 object, Behavior)
        self._objects: dict[int, tuple[Any, Behavior]] = {}
        self._used_object_identifiers: set[tuple[str, int]] = set()
        # device instance → slot index (for physical instance offset)
        self._device_slots: dict[int, int] = {}
        # device instance → device row, rebuilt on every start()/reload() from
        # the same DB fetch as _device_slots -- lets packet-capture streaming
        # resolve device identity in O(1) without a per-packet DB query.
        self._devices_by_instance: dict[int, dict] = {}
        self._reload_event = asyncio.Event()
        # Guards reload() against overlapping runs. Every device/object CRUD route
        # fires reload() via asyncio.create_task() (fire-and-forget), and start()
        # does hundreds of awaited DB calls for a large project — plenty of time
        # for a second reload() to start before the first finishes. Without this
        # lock, two reloads race on self.app/_objects/_device_slots and the loser
        # can leave a stale DeviceObject (e.g. an old instance number for a device
        # that was since renumbered) registered in the winner's _virtual_devices,
        # where it keeps answering Who-Is broadcasts indefinitely even though the
        # DB (source of truth) has already moved on.
        self._reload_lock = asyncio.Lock()
        self._current_values: dict = {}  # for API
        # object DB id → last logged value (for change detection)
        self._prev_values: dict[int, Any] = {}  # kept for history only
        # object DB id → live Behavior instance, built once at _create_object()
        # for EVERY object regardless of provider ownership. Non-provider-owned
        # points ignore this dict entirely (BuiltInSimulationProvider keeps its
        # own separate _behaviors, unchanged); provider-owned points read from
        # this one each tick to apply the configured Behavior on top of the
        # provider's raw value -- see _apply_fmu_behavior().
        self._point_behaviors: dict[int, Behavior] = {}
        # object DB id → the provider's raw/unmodified value from the most
        # recent tick, always populated before any Behavior transformation.
        # Never persisted -- purely a live diagnostics surface (see
        # src/api/routers/objects.py's "raw_provider_value" field) so the
        # configured Behavior's effect can be told apart from the FMU/
        # model's own live output.
        self._raw_provider_values: dict[int, Any] = {}
        # object DB id → rolling 1-hour history (720 ticks × 5 s), never persisted
        self._history: dict[int, deque] = {}
        # object DB id → intrinsic-reporting runtime state (not persisted — a
        # restart starts every object back at "normal", an acceptable
        # simulator simplification; see alarms.py)
        self._alarm_runtime: dict[int, alarms.AlarmRuntime] = {}
        # event_enrollment DB id → algorithmic-reporting runtime state (same
        # not-persisted simplification as _alarm_runtime above)
        self._enrollment_runtime: dict[int, alarms.AlarmRuntime] = {}
        # Mirror propagation: obj DB id -> last normalized value injected by mirror_sync_loop
        self._mirror_values: dict[int, Any] = {}
        # trend_log DB id → last value actually recorded, for COV-triggered
        # logging (not persisted — same simplification as the above)
        self._trend_log_last_value: dict[int, Any] = {}
        # trend_log DB id → live bacpypes3 TrendLogObject, once exposed on
        # the BACnet wire (see _create_trend_log_objects())
        self._trend_log_objects: dict[int, Any] = {}
        # schedule DB id → live bacnet_schedule.LocalScheduleObject. Unlike
        # the above, these don't need a runtime cache in tick() — bacpypes3's
        # ScheduleObject self-schedules its own next transition.
        self._schedule_objects: dict[int, Any] = {}
        # calendar DB id → live bacnet_calendar.LocalCalendarObject (GH #18).
        # presentValue has no self-scheduling hook like ScheduleObject does,
        # so tick() refreshes it directly — see _refresh_calendar_present_values.
        self._calendar_objects: dict[int, Any] = {}
        # simulation clock: whether tick() advances time / recomputes values.
        # Independent of self.app (the BACnet stack) — objects stay reachable
        # and hold their last value while paused/stopped.
        # One of "running" / "paused" / "stopped" — "paused" freezes values in
        # place, "stopped" additionally rewinds elapsed time/history to zero.
        # Starts running on process boot (historical default, pre-dates these
        # controls); loading/switching a project explicitly stops it instead
        # (see load_project()) so a freshly loaded project doesn't silently
        # start ticking.
        self.clock_state: str = "running"

        # Provider runtime registry.
        #
        # "builtin" is always present and owns every normal simulated point
        # that has not been explicitly claimed as an output by another
        # provider. Additional providers (FMU/Learned/etc.) register
        # their input/output point bindings through register_simulation_provider().
        #
        # BACnet object lifecycle, alarms, trends, history and snapshots stay
        # in SimEngine; providers only generate point values.
        self._builtin_provider = BuiltInSimulationProvider()
        self._providers: dict[str, Any] = {"builtin": self._builtin_provider}
        self._provider_contexts: dict[str, SimulationContext] = {}
        self._provider_input_points: dict[str, set[int]] = {"builtin": set()}
        self._provider_output_points: dict[str, set[int]] = {"builtin": set()}
        self._point_output_owner: dict[int, str] = {}
        self._provider_diagnostics: dict[str, dict[str, Any]] = {}
        self._model_input_shadow_values: dict[int, tuple[Any, str | None]] = {}
        # Guards register_simulation_provider/unregister_simulation_provider.
        # A plain threading.Lock (not asyncio.Lock) because the simulation
        # recovery sweep runs its body in a worker thread via
        # asyncio.to_thread -- there's no running event loop in that thread
        # to await an asyncio.Lock against, only a real OS thread that can
        # race the main thread's route handlers for the same registry dicts.
        # Must be reentrant: register_simulation_provider calls
        # unregister_simulation_provider internally when replacing an
        # already-registered provider (the recovery sweep's main case), so a
        # plain non-reentrant Lock would deadlock on that path.
        self._simulation_registry_lock = threading.RLock()

    def _model_input_shadow_for_point(self, point_id: int) -> tuple[Any | None, str | None]:
        if point_id in self._model_input_shadow_values:
            return self._model_input_shadow_values[point_id]
        for provider_id, diagnostics in self._provider_diagnostics.items():
            if provider_id == "builtin":
                continue
            state = str(
                diagnostics.get("runtime_state")
                or diagnostics.get("status")
                or ""
            ) or None
            for report in (diagnostics.get("last_step_inputs") or {}).values():
                try:
                    report_point_id = int(report.get("point_id"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if report_point_id == int(point_id):
                    return report.get("value"), state
        return None, None

    def _twin_snapshot_payload(
        self,
        obj_id: int,
        obj_row: dict,
        mirror_val: Any,
        provider_outputs: dict[int, Any],
    ) -> Optional[dict]:
        model_value = None
        model_state = None
        owner = self._point_output_owner.get(obj_id)
        if owner:
            model_state = str(
                (self._provider_diagnostics.get(owner) or {}).get("runtime_state")
                or (self._provider_diagnostics.get(owner) or {}).get("status")
                or ""
            ) or None
            if obj_id in provider_outputs:
                model_value = provider_outputs[obj_id]
        if model_value is None:
            input_model_value, input_model_state = self._model_input_shadow_for_point(obj_id)
            if input_model_value is not None:
                model_value = input_model_value
                model_state = input_model_state
        if mirror_val is None and owner is None and model_value is None:
            return None

        payload = {
            "id": obj_id,
            "name": obj_row["name"],
            "object_type": obj_row["object_type"],
            "object_instance": obj_row["object_instance"],
            "units": obj_row.get("units", ""),
            "behavior": obj_row["behavior"],
        }
        if mirror_val is not None:
            payload["value"] = mirror_val
        if model_state is not None:
            payload["model_state"] = model_state
        if model_value is not None:
            payload["model_value"] = model_value
        return payload

    @staticmethod
    def _point_config_from_row(obj_row: dict) -> PointConfig:
        """Translate an object DB row into protocol-agnostic provider config."""
        return PointConfig(
            point_id=int(obj_row["id"]),
            behavior=str(obj_row.get("behavior") or "constant"),
            behavior_params=str(obj_row.get("behavior_params") or '{"value":0}'),
            manual_value=obj_row.get("manual_value"),
            object_type=str(obj_row.get("object_type") or "analog-value"),
        )

    def register_simulation_provider(
        self,
        provider_id: str,
        provider: Any,
        *,
        context: SimulationContext,
        input_point_ids: Optional[list[int] | set[int] | tuple[int, ...]] = None,
        output_point_ids: Optional[list[int] | set[int] | tuple[int, ...]] = None,
        replace: bool = False,
    ) -> None:
        """Register one non-built-in simulation provider.

        Provider ownership is explicit only for OUTPUT points. Input points may
        still be generated by another provider (for example a VAV System model
        reading SAT from the Built-in provider or from an AHU System model).

        A point can have only one output owner. This prevents Built-in + System,
        two System models, or a future FMU/Learned provider from racing to write
        the same point in one tick.

        Providers execute in registration order after Built-in. This gives a
        deterministic dependency chain. If provider B reads a point produced by
        provider A and A was registered first, B sees A's value from the same
        tick. Cyclic provider graphs naturally fall back to the previous value
        for the unresolved side and should be avoided by model configuration.
        """
        provider_id = str(provider_id).strip()
        if not provider_id or provider_id == "builtin":
            raise ValueError("provider_id must be non-empty and cannot be 'builtin'")

        inputs = {int(pid) for pid in (input_point_ids or ())}
        outputs = {int(pid) for pid in (output_point_ids or ())}

        with self._simulation_registry_lock:
            if provider_id in self._providers and not replace:
                raise ValueError(f"Simulation provider {provider_id!r} is already registered")

            # If replacing an existing provider, release its old output claims first.
            if provider_id in self._providers:
                self.unregister_simulation_provider(provider_id)

            conflicts = {
                point_id: self._point_output_owner[point_id]
                for point_id in outputs
                if point_id in self._point_output_owner
            }
            if conflicts:
                details = ", ".join(
                    f"{point_id} -> {owner}"
                    for point_id, owner in sorted(conflicts.items())
                )
                raise ValueError(
                    f"Simulation output point(s) already owned by another provider: {details}"
                )

            validation = provider.validate()
            if not validation.valid:
                message = "; ".join(validation.errors) or "provider validation failed"
                raise ValueError(
                    f"Simulation provider {provider_id!r} is not valid: {message}"
                )

            context.metadata["provider_id"] = provider_id
            provider.initialize(context)

            self._providers[provider_id] = provider
            self._provider_contexts[provider_id] = context
            self._provider_input_points[provider_id] = inputs
            self._provider_output_points[provider_id] = outputs
            for point_id in outputs:
                self._point_output_owner[point_id] = provider_id

            if self.clock_state == "running":
                provider.start()

            log.info(
                "Registered simulation provider %s (%s): %d inputs, %d outputs",
                provider_id,
                type(provider).__name__,
                len(inputs),
                len(outputs),
            )

    def unregister_simulation_provider(self, provider_id: str) -> bool:
        """Remove a non-built-in provider and release its point ownership."""
        with self._simulation_registry_lock:
            if provider_id == "builtin" or provider_id not in self._providers:
                return False

            provider = self._providers.pop(provider_id)
            try:
                provider.stop()
            except Exception:
                log.exception("Failed to stop simulation provider %s", provider_id)

            for point_id in self._provider_output_points.pop(provider_id, set()):
                if self._point_output_owner.get(point_id) == provider_id:
                    self._point_output_owner.pop(point_id, None)

            for point_id in self._provider_input_points.pop(provider_id, set()):
                self._model_input_shadow_values.pop(int(point_id), None)
            self._provider_contexts.pop(provider_id, None)
            self._provider_diagnostics.pop(provider_id, None)

        log.info("Unregistered simulation provider %s", provider_id)
        return True

    def get_simulation_providers(self) -> dict[str, dict]:
        """Small runtime diagnostic snapshot for API/debug use."""
        result: dict[str, dict] = {}
        for provider_id, provider in self._providers.items():
            try:
                status = provider.get_status()
                status_value = getattr(status, "value", str(status))
            except Exception:
                status_value = "error"
            result[provider_id] = {
                "type": type(provider).__name__,
                "status": status_value,
                "input_point_ids": sorted(
                    self._provider_input_points.get(provider_id, set())
                ),
                "output_point_ids": sorted(
                    self._provider_output_points.get(provider_id, set())
                ),
            }
            if hasattr(provider, "get_diagnostics"):
                try:
                    result[provider_id]["diagnostics"] = provider.get_diagnostics()
                except Exception as exc:
                    result[provider_id]["diagnostics"] = {
                        "error": str(exc),
                    }
        return result

    def _replace_builtin_provider(self, provider: BuiltInSimulationProvider) -> None:
        """Swap the Built-in adapter without disturbing registered providers."""
        self._builtin_provider = provider
        self._providers["builtin"] = provider

    @staticmethod
    def _plain_present_value(value: Any) -> Any:
        """Convert a BACpypes presentValue wrapper to a plain Python value."""
        if isinstance(value, Real):
            return float(value)
        if isinstance(value, Unsigned):
            return int(value)
        if isinstance(value, BinaryPV):
            return str(value) == "active"
        try:
            return float(value)
        except (TypeError, ValueError):
            return value

    def _provider_input_value(
        self,
        point_id: int,
        generated_values: dict[int, Any],
    ) -> Any:
        """Resolve a model input using newest same-tick value when available."""
        if point_id in generated_values:
            return generated_values[point_id]
        if point_id in self._prev_values:
            return self._prev_values[point_id]

        runtime_entry = self._objects.get(point_id)
        if runtime_entry is None:
            return None
        bacnet_obj, _ = runtime_entry
        return self._plain_present_value(getattr(bacnet_obj, "presentValue", None))

    def resolve_provider_input_value(self, point_id: int) -> Any:
        """Public counterpart of _provider_input_value for callers outside
        the tick loop (provider registration/recovery), where no same-tick
        generated_values exist yet. Read-only access to _prev_values/
        _objects -- safe to call from a worker thread (e.g. the recovery
        sweep's asyncio.to_thread body): dict reads are atomic under the
        GIL, and the tick loop's own writes to these dicts happen on the
        main event-loop thread, so at worst this returns a value that is one
        tick stale, the same characteristic _provider_input_value already
        has mid-tick.
        """
        return self._provider_input_value(point_id, {})

    def _run_registered_providers(
        self,
        dt: float,
        generated_values: dict[int, Any],
    ) -> dict[int, Any]:
        """Run every non-built-in provider once in deterministic order."""
        for provider_id, provider in list(self._providers.items()):
            if provider_id == "builtin":
                continue

            inputs = {
                point_id: value
                for point_id in self._provider_input_points.get(provider_id, set())
                if (value := self._provider_input_value(point_id, generated_values))
                is not None
            }

            try:
                provider.set_inputs(inputs)
                if provider.get_status() != ProviderStatus.RUNNING:
                    provider.start()
                provider.step(dt)
                outputs = dict(provider.get_outputs())
                diagnostics = {}
                if hasattr(provider, "get_diagnostics"):
                    try:
                        diagnostics = provider.get_diagnostics()
                    except Exception as exc:
                        diagnostics = {"diagnostics_error": str(exc)}
                self._provider_diagnostics[provider_id] = diagnostics
                runtime_state = str(
                    diagnostics.get("runtime_state")
                    or diagnostics.get("status")
                    or ""
                ) or None
                for point_id, value in inputs.items():
                    self._model_input_shadow_values[int(point_id)] = (value, runtime_state)
            except Exception:
                log.exception(
                    "Simulation provider %s (%s) failed during tick",
                    provider_id,
                    type(provider).__name__,
                )
                continue

            declared_outputs = self._provider_output_points.get(provider_id, set())
            for point_id, value in outputs.items():
                point_id = int(point_id)
                if point_id not in declared_outputs:
                    log.warning(
                        "Provider %s emitted undeclared output point %s; ignoring",
                        provider_id,
                        point_id,
                    )
                    continue
                if self._point_output_owner.get(point_id) != provider_id:
                    log.warning(
                        "Provider %s no longer owns output point %s; ignoring",
                        provider_id,
                        point_id,
                    )
                    continue
                generated_values[point_id] = value

            if provider_id != "builtin":
                log.info(
                    "Simulation provider output batch: provider=%s type=%s "
                    "dt=%s inputs=%s outputs=%s diagnostics=%s",
                    provider_id,
                    type(provider).__name__,
                    dt,
                    inputs,
                    outputs,
                    diagnostics,
                )

        return generated_values

    def pause(self) -> None:
        self.clock_state = "paused"
        for provider_id, provider in list(self._providers.items()):
            try:
                provider.pause()
            except Exception:
                log.exception("Failed to pause simulation provider %s", provider_id)

    def resume(self) -> None:
        self.clock_state = "running"
        for provider_id, provider in list(self._providers.items()):
            try:
                provider.start()
            except Exception:
                log.exception("Failed to resume simulation provider %s", provider_id)

    def reset(self) -> None:
        """Stop the clock and rewind simulated time/history back to the start."""
        self.clock_state = "stopped"
        self.state.elapsed_seconds = 0.0
        self.state.time_of_day = 12.0
        self._history.clear()
        for provider_id, provider in list(self._providers.items()):
            try:
                provider.reset()
            except Exception:
                log.exception("Failed to reset simulation provider %s", provider_id)

    @staticmethod
    def _simulated_enabled_devices(devices: list[dict]) -> list[dict]:
        """The ONLY devices SimEngine ever turns into live virtual BACnet
        DeviceObjects. External BACnet devices (source_type ==
        'external-bacnet') must NEVER appear here -- they belong to a real
        physical device the simulator only reads from, never impersonates.
        Every downstream effect (virtual-device registration, object/
        trend-log/schedule/calendar creation, and the I-Am announcement
        loop) cascades from this one list -- see start(). A device dict
        with no source_type key at all (pre-migration shape) defaults to
        'simulated' for backward compatibility."""
        return [
            d for d in devices
            if d["enabled"] and d.get("source_type", "simulated") == "simulated"
        ]

    async def start(self) -> None:
        devices = await asyncio.to_thread(self.db.get_devices)
        self._devices_by_instance = {d["device_instance"]: d for d in devices}
        enabled = self._simulated_enabled_devices(devices)

        # A start/reload rebuilds the BACnet runtime from DB, so rebuild only
        # the Built-in adapter from the same source of truth. Registered FMU/
        # Learned providers remain registered and keep their output claims.
        self._replace_builtin_provider(BuiltInSimulationProvider())
        provider_participants: list[int] = []
        provider_points: list[PointConfig] = []

        if not enabled:
            log.info("No enabled devices — BACnet stack idle")
            self.app = None
            return

        base_ip = _resolve_base_ip()

        install_bacpypes_packet_capture_hooks(
            local_ip=base_ip,
            local_port=BACNET_PORT,
            get_clock_state=lambda: self.clock_state,
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

        primary = enabled[0]
        bind_addr = f"{base_ip}:{BACNET_PORT}"

        primary_dev_obj = self._make_device_object(primary)
        self.network_port = NetworkPortObject(
            bind_addr,
            objectIdentifier=("network-port", 1),
            objectName="NetworkPort-1",
        )

        self.app = SimApplication.from_object_list([primary_dev_obj, self.network_port])
        self.app._sim_engine = self
        self.app._own_ip = base_ip  # for filtering our own I-Am loopback in duplicate-ID detection
        self._used_object_identifiers.clear()
        await asyncio.sleep(0.3)

        self.app._virtual_devices[primary["device_instance"]] = primary_dev_obj
        self._device_slots = {d["device_instance"]: i for i, d in enumerate(enabled)}

        log.info("BACnet socket bound to %s", bind_addr)

        for idx, dev in enumerate(enabled):
            slot = idx
            provider_participants.append(int(dev["id"]))
            if idx == 0:
                dev_obj = primary_dev_obj
            else:
                dev_obj = self._make_device_object(dev)
                self.app._virtual_devices[dev["device_instance"]] = dev_obj

            objects = await asyncio.to_thread(self.db.get_objects, dev["id"])
            bacnet_ids = [dev_obj.objectIdentifier]
            if idx == 0:
                bacnet_ids.append(self.network_port.objectIdentifier)

            for obj_row in objects:
                if not obj_row["enabled"]:
                    continue
                bacnet_obj, behavior = self._create_object(obj_row, slot, dev["name"])
                try:
                    self.app.add_object(bacnet_obj)
                except RuntimeError:
                    log.exception(
                        "Failed to add object %r as %r on device %r "
                        "(name/identifier collision?) — skipping",
                        obj_row["name"], bacnet_obj.objectIdentifier, dev["name"],
                    )
                    continue
                self._objects[obj_row["id"]] = (bacnet_obj, behavior)
                bacnet_ids.append(bacnet_obj.objectIdentifier)

                # Mirror points remain driven by mirror_sync_loop. Everything
                # else follows the historical built-in behavior path.
                if (
                    dev.get("simulation_mode", "simulation") != "mirror"
                    and int(obj_row["id"]) not in self._point_output_owner
                ):
                    provider_points.append(self._point_config_from_row(obj_row))

            # Calendars (GH #18) must be built before Schedules below, since a
            # Schedule's exceptionSchedule may reference one by name.
            calendars = await asyncio.to_thread(self.db.get_calendars, dev["id"])
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
                    self.app.add_object(cal_bacnet_obj)
                    self._calendar_objects[cal["id"]] = cal_bacnet_obj
                    calendar_phys_by_name[cal["name"]] = phys
                    bacnet_ids.append(cal_bacnet_obj.objectIdentifier)
                except Exception:
                    log.exception("Failed to build calendar %r on device %r — skipping", cal["name"], dev["name"])

            trend_logs = await asyncio.to_thread(self.db.get_trend_logs, dev["id"])
            for tl_idx, tl in enumerate(trend_logs):
                monitored = self._objects.get(tl["monitored_object_id"])
                if monitored is None:
                    continue  # monitored object disabled/missing — skip exposing on the wire
                monitored_objid = monitored[0].objectIdentifier
                records = await asyncio.to_thread(
                    self.db.get_trend_log_records, tl["id"], limit=tl["buffer_size"], order="asc"
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
                    self.app.add_object(tl_bacnet_obj)
                    self._trend_log_objects[tl["id"]] = tl_bacnet_obj
                    bacnet_ids.append(tl_bacnet_obj.objectIdentifier)
                except Exception:
                    log.exception("Failed to build trend log %r on device %r — skipping", tl["name"], dev["name"])

            schedules = await asyncio.to_thread(self.db.get_schedules, dev["id"])
            for sched_idx, sched in enumerate(schedules):
                if not sched["enabled"]:
                    continue  # same convention as disabled regular objects: not built at all
                targets = await asyncio.to_thread(self.db.get_schedule_targets, sched["id"])
                obj_prop_refs = []
                for t in targets:
                    target_entry = self._objects.get(t["object_id"])
                    if target_entry is None:
                        continue  # target object disabled/missing — skip that reference
                    obj_prop_refs.append(DeviceObjectPropertyReference(
                        objectIdentifier=target_entry[0].objectIdentifier,
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
                    self.app.add_object(sched_bacnet_obj)
                    self._schedule_objects[sched["id"]] = sched_bacnet_obj
                    bacnet_ids.append(sched_bacnet_obj.objectIdentifier)
                except Exception:
                    log.exception("Failed to build schedule %r on device %r — skipping", sched["name"], dev["name"])

            dev_obj.objectList = bacnet_ids
            self.app._virtual_object_lists[dev["device_instance"]] = bacnet_ids
            log.info("Device %d (%s): %d objects", dev["device_instance"], dev["name"], len(objects))

        self._builtin_provider.initialize(
            SimulationContext(
                participant_device_ids=provider_participants,
                point_configs=provider_points,
            )
        )
        if self.clock_state == "running":
            self._builtin_provider.start()

        # Keep registered non-built-in models alive across BACnet runtime
        # rebuilds. Device/object CRUD reloads should not rewind FMU/Learned
        # sessions; their output ownership/mappings remain valid because the
        # DB object IDs do not change.
        for provider_id, provider in list(self._providers.items()):
            if provider_id == "builtin":
                continue
            try:
                if self.clock_state == "running":
                    provider.start()
            except Exception:
                log.exception(
                    "Failed to keep simulation provider %s running during start",
                    provider_id,
                )

        # Announce all devices
        saved = self.app.device_object
        try:
            for dev_obj in self.app._virtual_devices.values():
                self.app.device_object = dev_obj
                self.app.i_am()
        finally:
            self.app.device_object = saved

    def _make_device_object(self, dev: dict) -> DeviceObject:
        try:
            segmentation = Segmentation(dev.get("segmentation_supported") or "segmented-both")
        except Exception:
            segmentation = Segmentation("segmented-both")
        return DeviceObject(
            objectIdentifier=f"device,{dev['device_instance']}",
            objectName=dev["name"],
            vendorIdentifier=999,
            description=dev.get("description", ""),
            modelName=dev.get("model_name", "BACnet Simulator"),
            vendorName=dev.get("vendor_name", "Iotistica"),
            applicationSoftwareVersion="3.0",
            location=dev["name"],
            firmwareRevision=dev.get("firmware_revision") or "N/A",
            protocolRevision=Unsigned(dev.get("protocol_revision") or 22),
            maxApduLengthAccepted=Unsigned(dev.get("max_apdu_length_accepted") or 1024),
            segmentationSupported=segmentation,
        )

    def _allocate_object_identifier(
        self,
        object_type: str,
        preferred_instance: int,
    ) -> int:
        """Return a BACnet object instance not already used for this type."""
        instance = max(0, int(preferred_instance)) % 4_194_303
        while (object_type, instance) in self._used_object_identifiers:
            instance += 1
            if instance > 4_194_302:
                instance = 0

        self._used_object_identifiers.add((object_type, instance))
        return instance

    def _create_object(self, obj_row: dict, slot: int, device_name: str = "") -> tuple[Any, Behavior]:


        otype = obj_row["object_type"]
        phys = self._allocate_object_identifier(
            otype,
            slot * 1000 + int(obj_row["object_instance"]),
        )
        behavior = make_behavior(
            obj_row["behavior"],
            obj_row["behavior_params"],
            obj_row.get("manual_value"),
        )
        # Kept for every object, not just provider-owned ones -- provider-owned
        # points read this same instance every tick to apply the configured
        # Behavior on top of the provider's raw value (see
        # _apply_fmu_behavior()); non-provider-owned points ignore it, since
        # BuiltInSimulationProvider keeps its own separate instance.
        self._point_behaviors[int(obj_row["id"])] = behavior
        val = behavior.compute(self.state)
        # BACnet requires globally unique object names within a single application,
        # even across virtual devices — prefix with device name to guarantee uniqueness.
        obj_name = f"{device_name}.{obj_row['name']}" if device_name else obj_row["name"]

        _ANALOG_CLS = {
            "analog-input":  AnalogInputObject,
            "analog-output": AnalogOutputObject,
            "analog-value":  AnalogValueObject,
        }
        _BINARY_CLS = {
            "binary-input":  BinaryInputObject,
            "binary-output": BinaryOutputObject,
            "binary-value":  BinaryValueObject,
        }
        if otype in _ANALOG_CLS:
            units_str = obj_row.get("units") or "no-units"
            try:
                units = EngineeringUnits(units_str)
            except Exception:
                units = EngineeringUnits("no-units")
            bacnet_obj = _ANALOG_CLS[otype](
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                presentValue=Real(float(val)),
                units=units,
            )
            _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
        elif otype == "binary-output":
            # Pass presentValue= in the constructor so Commandable.__init__ can set
            # relinquishDefault from it (line 87 in bacpypes3/local/cmd.py).
            # _Object.__init__ sets it directly via super().__setattr__, bypassing
            # Commandable.__setattr__, so priorityArray is not accessed before it exists.
            # The tick loop later writes via Commandable.__setattr__ → priorityArray[15]
            # → recalculating() which keeps presentValue up-to-date for ReadProperty.
            active = bool(val) if not isinstance(val, bool) else val
            bacnet_obj = BinaryOutputObject(
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                presentValue=BinaryPV("active" if active else "inactive"),
            )
            _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
            _apply_polarity(bacnet_obj, obj_row.get("polarity") or "normal")
        elif otype in MULTISTATE_TYPES:
            _MULTISTATE_CLS = {
                "multi-state-input":  MultiStateInputObject,
                "multi-state-output": MultiStateOutputObject,
                "multi-state-value":  MultiStateValueObject,
            }
            n_states = max(1, int(obj_row.get("number_of_states") or 2))
            state = max(1, min(n_states, round(float(val))))
            # Same reasoning as binary-output above — multi-state-output is
            # Commandable too, so presentValue must be passed at construction.
            bacnet_obj = _MULTISTATE_CLS[otype](
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                presentValue=Unsigned(state),
                numberOfStates=Unsigned(n_states),
            )
            _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
        else:
            active = bool(val) if not isinstance(val, bool) else val
            cls = _BINARY_CLS.get(otype, BinaryInputObject)
            bacnet_obj = cls(
                objectIdentifier=f"{otype},{phys}",
                objectName=obj_name,
                presentValue=BinaryPV("active" if active else "inactive"),
            )
            _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
            if otype == "binary-input":
                _apply_polarity(bacnet_obj, obj_row.get("polarity") or "normal")
        return bacnet_obj, behavior
    
    def get_object_value(self, object_id: int):
        """Live per-object value, refreshed every tick (self._prev_values,
        set at the top of the tick loop -- see the `self._prev_values[obj_id]
        = val` line right before each object's snapshot entry is built).

        NOTE ON HISTORY: this used to read self._current_values instead,
        which looks plausible (the name suggests "current value") but is
        WRONG -- _current_values is only ever assigned wholesale as either
        `{}` or `{"devices": [...], "tick": ...}` (the /sim/state snapshot
        cache for the API/WebSocket, see get_state()), never as a per-object
        {object_id: value} dict. `_current_values.get(object_id)` therefore
        always returned None once the sim had ticked at least once.

        This class used to ALSO define a second get_object_value() later in
        the class body that correctly read _prev_values -- Python's last-
        definition-wins for duplicate method names meant that second
        definition silently shadowed this one, so every caller
        (get_device_point_values(), the Energy Engine, SemanticResolver,
        CommissioningPointResolver) was actually getting the CORRECT
        _prev_values-based behavior by accident of definition order. An
        earlier fix here mistakenly resolved the naming collision by keeping
        THIS (broken, _current_values-based) definition as canonical and
        renaming the working one out of the way -- which fixed the naming
        collision but broke real value resolution in the process. Both
        definitions are now consolidated into this one, correct,
        _prev_values-based implementation."""
        return self._prev_values.get(object_id)

    def get_device_point_values(self, objects: list[dict]) -> dict[str, object]:
        values: dict[str, object] = {}
        for obj in objects:
            point_type = obj.get("point_type")
            if point_type:
                values[str(point_type)] = self.get_object_value(obj["id"])
        return values
    def get_devices_by_instance(self) -> dict[int, dict]:
        return self._devices_by_instance

    def resolve_wire_object(
        self,
        object_type: str,
        physical_instance: int,
    ) -> Optional[dict]:
        """Resolve a wire-visible BACnet object back to its simulator row.

        Regular simulator objects are stored in ``self._objects`` as:
            database object id -> (live BACpypes object, Behavior)

        Matching the actual live ``objectIdentifier`` is safer than trying to
        reverse the slot-offset formula because it also stays correct after
        reloads and for any future changes to instance allocation.
        """
        normalized_type = str(object_type).strip().lower().replace('_', '-')

        for object_db_id, (bacnet_obj, behavior) in self._objects.items():
            try:
                identifier = bacnet_obj.objectIdentifier
                wire_type = str(identifier[0]).strip().lower().replace('_', '-')
                wire_instance = int(identifier[1])
            except Exception:
                continue

            if (
                wire_type != normalized_type
                or wire_instance != int(physical_instance)
            ):
                continue

            obj_row = self.db.get_object(object_db_id)
            if obj_row is None:
                return None

            device = self.db.get_device(int(obj_row['device_id']))
            if device is None:
                return None

            # Same fix as get_object_value() above: _current_values is only
            # ever the whole /sim/state snapshot, never per-object -- use
            # _prev_values, which IS refreshed per-object every tick.
            current_value = self._prev_values.get(object_db_id)
            if isinstance(current_value, dict):
                current_value = current_value.get('value')

            return {
                'device_id': int(device['id']),
                'device_instance': int(device['device_instance']),
                'device_name': str(device['name']),
                'object_id': int(obj_row['id']),
                'object_name': str(obj_row['name']),
                'object_type': str(obj_row['object_type']),
                'object_instance': int(obj_row['object_instance']),
                'wire_object_identifier': (
                    f"{wire_type}:{wire_instance}"
                ),
                'local_object_identifier': (
                    f"{obj_row['object_type']}:{obj_row['object_instance']}"
                ),
                'current_value': current_value,
                'units': obj_row.get('units'),
                'behavior': obj_row.get('behavior'),
                'point_type': obj_row.get('point_type'),
            }

        return None

    def _update_value(self, bacnet_obj: Any, otype: str, val: Any) -> None:
        if otype in ("analog-input", "analog-output", "analog-value"):
            bacnet_obj.presentValue = Real(float(val))
        elif otype == "binary-output":
            active = bool(val) if not isinstance(val, bool) else val
            bacnet_obj.presentValue = BinaryPV("active" if active else "inactive")  # triggers recalculating() via priorityArray
        elif otype in MULTISTATE_TYPES:
            n_states = int(bacnet_obj.numberOfStates)
            state = max(1, min(n_states, round(float(val))))
            bacnet_obj.presentValue = Unsigned(state)
        else:
            active = bool(val) if not isinstance(val, bool) else val
            bacnet_obj.presentValue = BinaryPV("active" if active else "inactive")

    def _apply_fmu_behavior(
        self, behavior: Behavior, behavior_name: str, raw_value: Any, state: SimState,
    ) -> Any:
        """Apply a provider (FMU/learned model)-owned point's configured
        Behavior on top of the provider's raw value for this tick.

        Reuses each Behavior class's own compute() math completely
        unchanged -- for the types whose stored config normally describes an
        absolute baseline (sine/noise's `base`, ramp's `from`, schedule's
        `default`), that baseline attribute is overwritten with the live
        `raw_value` immediately before calling compute(), so the SAME
        formula naturally produces "raw + perturbation" instead of
        "stored-baseline + perturbation" -- no duplicated math, and nothing
        in behaviors.py itself changes. `constant` is the one type with no
        FMU equivalent at all (its only job for a normal point is to BE the
        value, which for an FMU point is the provider's own job) so it's a
        pure passthrough, deliberately not treating a legacy constant value
        as an offset. See each branch below for the specific mapping; must
        stay in sync with the matching UI adaptation in ObjectDrawer.vue.
        """
        if behavior_name in ("constant", "raw"):
            # "raw" is the explicit, discoverable reset-to-FMU-value
            # option (see VALID_BEHAVIORS in src/core/config.py); "constant"
            # does the same thing here for backward compatibility with
            # points whose behavior predates this feature. Both are a pure
            # passthrough -- deliberately not treating a stored value as an
            # offset.
            return raw_value

        if behavior_name == "manual":
            # Already an absolute override -- identical to the non-FMU case.
            return behavior.compute(state)

        if behavior_name in ("sine", "noise"):
            # Both classes read self.base fresh on every compute() call.
            behavior.base = raw_value
            return behavior.compute(state)

        if behavior_name == "ramp":
            # from=0 makes compute()'s from + (to-from)*frac reduce to
            # to*frac -- i.e. an offset drifting from 0 toward "to" ("Offset
            # To" in the FMU-facing UI), added on top of the raw value.
            behavior.from_val = 0.0
            return raw_value + behavior.compute(state)

        if behavior_name == "random_walk":
            # Seed the walk at offset 0 exactly once (not every tick, or the
            # walk could never accumulate) -- from then on compute() mutates
            # and returns self._value in place, so the offset persists
            # naturally across ticks on this same Behavior instance. min/max
            # are the same stored bounds as always; for an FMU-owned point
            # they're documented in the UI as offset bounds instead of
            # absolute engineering limits, but no code distinguishes them --
            # they're just the bounds the offset is clamped to either way.
            if not getattr(behavior, "_fmu_walk_seeded", False):
                behavior._value = 0.0
                behavior._fmu_walk_seeded = True
            return raw_value + behavior.compute(state)

        if behavior_name == "schedule":
            # DailyPatternBehavior.compute() returns self.default when no
            # block matches the current time-of-day, else the matching
            # block's own absolute value. Overwriting self.default with
            # raw_value makes the "no block active" case fall through to
            # the live FMU value while leaving active-block values
            # (genuine absolute overrides, per spec) untouched.
            behavior.default = raw_value
            return behavior.compute(state)

        if behavior_name == "fault":
            # Let FaultBehavior manage its own mtbf/duration timer exactly
            # as it already does. While inactive it would normally return
            # its inner/base behavior's own value -- for an FMU-owned point
            # that inner value is meaningless (the FMU IS the base), so
            # discard it and substitute the raw value instead. compute()
            # must still be called unconditionally first: it's what
            # advances/checks the timer and decides _fault_active for this
            # tick.
            computed = behavior.compute(state)
            return computed if behavior._fault_active else raw_value

        # Unrecognized behavior name -- defensive fallback, never reached
        # through the normal UI/API path.
        return raw_value

    async def tick(self) -> None:
        """Advance providers once, then fan generated values into existing consumers."""
        if self.clock_state != "running":
            return

        # Keep the legacy compatibility clock in lock-step for alarm timing,
        # API snapshots, schedules, and code that still reads SimEngine.state.
        # Behavior generation itself is authoritative in the provider.
        self.state.elapsed_seconds += dependencies.TICK_SECONDS
        self.state.time_of_day = (self.state.time_of_day + dependencies.TICK_SECONDS / 3600) % 24

        snapshot: dict[int, dict] = {}
        devices = await asyncio.to_thread(self.db.get_devices)
        dev_map = {d["id"]: d for d in devices}
        device_capabilities = {d["device_instance"]: dependencies._effective_can_receive_events(d) for d in devices}

        alarm_configs = {c["object_id"]: c for c in await asyncio.to_thread(self.db.get_all_alarm_configs)}
        event_enrollments = await asyncio.to_thread(self.db.get_all_event_enrollments)
        enrollments_by_object: dict[int, list[dict]] = {}
        for ee in event_enrollments:
            enrollments_by_object.setdefault(ee["monitored_object_id"], []).append(ee)
        notification_classes = (
            {nc["id"]: nc for nc in await asyncio.to_thread(self.db.get_notification_classes)}
            if alarm_configs or event_enrollments else {}
        )
        trend_logs = await asyncio.to_thread(self.db.get_all_trend_logs)
        trend_logs_by_object: dict[int, list[dict]] = {}
        for tl in trend_logs:
            trend_logs_by_object.setdefault(tl["monitored_object_id"], []).append(tl)
        now = time.time()

        # Build one DB-backed config snapshot for this tick. This preserves the
        # simulator's existing live-edit behavior without reinitializing the
        # provider (which would incorrectly reset random-walk/fault state).
        runtime_rows: dict[int, dict] = {}
        active_builtin_points: list[PointConfig] = []
        for obj_id in list(self._objects):
            obj_row = await asyncio.to_thread(self.db.get_object, obj_id)
            if not obj_row:
                continue
            runtime_rows[obj_id] = obj_row
            dev = dev_map.get(obj_row["device_id"])
            if (
                dev
                and dev.get("simulation_mode", "simulation") != "mirror"
                and obj_id not in self._point_output_owner
            ):
                active_builtin_points.append(self._point_config_from_row(obj_row))

        # Built-in runs first. It owns every normal simulated point that has
        # not been claimed as an output by another provider.
        # Keep Built-in configs live without coupling SimEngine to one exact
        # BuiltInSimulationProvider sync_point_configs() signature. Older provider
        # implementations accept only the config list; newer ones also accept
        # remove_missing=True.
        try:
            self._builtin_provider.sync_point_configs(
                active_builtin_points,
                remove_missing=True,
            )
        except TypeError as exc:
            if "remove_missing" not in str(exc):
                raise
            self._builtin_provider.sync_point_configs(active_builtin_points)

        if self._builtin_provider.get_status() != ProviderStatus.RUNNING:
            self._builtin_provider.start()
        self._builtin_provider.step(dependencies.TICK_SECONDS)

        # Then run FMU/Learned providers in registration order. Inputs
        # resolve against the newest value already generated this tick, so a
        # System model can consume a Built-in point (or an earlier provider's
        # output) without duplicating the point.
        provider_outputs: dict[int, Any] = {
            int(point_id): value
            for point_id, value in self._builtin_provider.get_outputs().items()
            if int(point_id) not in self._point_output_owner
        }
        # to_thread, not a direct call: FMUSimulationProvider.step() makes a
        # blocking urllib HTTP call to the FMU runtime, and an
        # EnergyPlus/Spawn-backed model can take several real seconds per
        # step (measured ~8.5s) -- called directly here, that stalls the
        # single asyncio event loop this coroutine shares with the whole
        # HTTP API for the same duration, so every request (project save,
        # object list, anything) hangs until the step returns.
        # _run_registered_providers is self-contained (no self.db access,
        # only in-memory provider/dict state) so it's safe to run off-loop.
        provider_outputs = await asyncio.to_thread(
            self._run_registered_providers,
            dependencies.TICK_SECONDS,
            provider_outputs,
        )

        for obj_id, (bacnet_obj, behavior) in self._objects.items():
            obj_row = runtime_rows.get(obj_id)
            if not obj_row:
                continue
            dev = dev_map.get(obj_row["device_id"])
            if not dev:
                continue
            if dev.get("simulation_mode", "simulation") == "mirror":
                # Behaviors stay stored but inactive; mirror_sync_loop drives the value.
                mirror_val = self._mirror_values.get(obj_id)
                payload = self._twin_snapshot_payload(
                    obj_id,
                    obj_row,
                    mirror_val,
                    provider_outputs,
                )
                if payload is not None:
                    did = dev["device_instance"]
                    if did not in snapshot:
                        snapshot[did] = {"device_instance": did, "name": dev["name"], "objects": []}
                    snapshot[did]["objects"].append(payload)
                continue
            # Value generation is provider-owned. Everything after this
            # assignment remains the existing BACnet/alarm/trend/history path.
            if obj_id not in provider_outputs:
                owner = self._point_output_owner.get(obj_id, "builtin")
                log.warning(
                    "Simulation provider %s produced no value for object id %s",
                    owner,
                    obj_id,
                )
                continue
            val = provider_outputs[obj_id]
            owner = self._point_output_owner.get(obj_id, "builtin")
            if owner != "builtin":
                # The provider (FMU/learned model) remains the source of
                # truth; the raw value is preserved for diagnostics BEFORE
                # any Behavior transformation, and the provider keeps
                # computing it every tick regardless of whether a Behavior
                # is configured.
                self._raw_provider_values[obj_id] = val
                behavior = self._point_behaviors.get(obj_id)
                if behavior is not None:
                    try:
                        val = self._apply_fmu_behavior(
                            behavior, obj_row["behavior"], val, self.state,
                        )
                    except Exception:
                        log.warning(
                            "Behavior application failed for object id %s; falling back to raw provider value",
                            obj_id,
                            exc_info=True,
                        )
                        val = provider_outputs[obj_id]
            self._update_value(bacnet_obj, obj_row["object_type"], val)
            if owner != "builtin":
                log.info(
                    "BACnet object write from simulation provider: provider=%s "
                    "tick=%s device_id=%s device_instance=%s device_name=%s "
                    "object_id=%s object_name=%s object_type=%s "
                    "object_instance=%s value=%s units=%s",
                    owner,
                    self.state.elapsed_seconds,
                    obj_row["device_id"],
                    dev["device_instance"],
                    dev["name"],
                    obj_id,
                    obj_row["name"],
                    obj_row["object_type"],
                    obj_row["object_instance"],
                    val,
                    obj_row.get("units", ""),
                )

            cfg = alarm_configs.get(obj_id)
            if cfg is not None:
                await self._evaluate_alarm(obj_id, obj_row, dev, val, cfg, notification_classes, device_capabilities)

            for enrollment in enrollments_by_object.get(obj_id, []):
                await self._evaluate_enrollment(enrollment, obj_row, dev, val, notification_classes, device_capabilities)

            for tl in trend_logs_by_object.get(obj_id, []):
                if tl["logging_type"] == "polled":
                    if now - (tl["last_sampled_at"] or 0) >= tl["log_interval"]:
                        await self._sample_trend_log(tl["id"], val)
                elif tl["logging_type"] == "cov":
                    last = self._trend_log_last_value.get(tl["id"])
                    if last is None or self._trend_log_value_changed(val, last, tl["cov_increment"]):
                        await self._sample_trend_log(tl["id"], val)
                        self._trend_log_last_value[tl["id"]] = val

            self._prev_values[obj_id] = val

            # Append to rolling history (never persisted)
            hist = self._history.setdefault(obj_id, deque(maxlen=dependencies.OBJECT_HISTORY_MAXLEN))
            hist.append((time.time(), 1.0 if val is True else 0.0 if val is False else float(val)))

            did = dev["device_instance"]
            if did not in snapshot:
                snapshot[did] = {"device_instance": did, "name": dev["name"], "objects": []}
            payload = {
                "id": obj_id,
                "name": obj_row["name"],
                "object_type": obj_row["object_type"],
                "object_instance": obj_row["object_instance"],
                "value": val,
                "units": obj_row.get("units", ""),
                "behavior": obj_row["behavior"],
            }
            if owner != "builtin":
                provider_diag = self._provider_diagnostics.get(owner) or {}
                payload["model_state"] = str(provider_diag.get("runtime_state") or "RUNNING")
                payload["model_value"] = val
            snapshot[did]["objects"].append(payload)

        self._current_values = {"devices": list(snapshot.values()), "tick": self.state.elapsed_seconds}

        # Calendar objects (GH #18) have no self-scheduling hook like Schedule
        # does, so refresh presentValue here — cheap, and only cosmetic for a
        # direct ReadProperty since Schedule's own calendarReference
        # resolution reads dateList directly, not presentValue.
        for cal_id, cal_bacnet_obj in self._calendar_objects.items():
            cal_row = await asyncio.to_thread(self.db.get_calendar, cal_id)
            if not cal_row:
                continue
            try:
                entries = json.loads(cal_row["date_list"] or "[]")
                cal_bacnet_obj.presentValue = Boolean(bacnet_calendar.today_in_date_list(entries))
            except Exception:
                log.exception("Failed to refresh calendar %r presentValue", cal_row.get("name"))

    async def inject_mirror_values(
        self,
        device_id: int,
        values: dict[tuple[str, int], Any],
        objects: list[dict],
    ) -> None:
        """Propagate external present-values into a Mirror simulated device.
        Called only by mirror_sync_loop -- never from the HTTP layer."""
        dev = await asyncio.to_thread(self.db.get_device, device_id)
        for obj in objects:
            key = (obj["object_type"], obj["object_instance"])
            if key not in values:
                continue
            val = values[key]
            if val is None:
                continue  # BACnet read failed for this object
            obj_id = obj["id"]
            entry = self._objects.get(obj_id)
            if not entry:
                continue
            bacnet_obj, _ = entry
            normalized = normalize_present_value(obj["object_type"], val)
            self._update_value(bacnet_obj, obj["object_type"], normalized)
            self._mirror_values[obj_id] = normalized
            self._prev_values[obj_id] = normalized
            if dev is not None:
                self._upsert_current_value_snapshot(dev, obj, normalized)

    async def _evaluate_alarm(
        self, obj_id: int, obj_row: dict, dev: dict, val: Any, cfg: dict, notification_classes: dict[int, dict],
        device_capabilities: dict[int, bool],
    ) -> None:
        """Advance one object's intrinsic-reporting state machine and, on a
        confirmed transition, log it and (best-effort) notify the object's
        Notification Class recipients. See alarms.py for the algorithm."""
        runtime = self._alarm_runtime.setdefault(obj_id, alarms.AlarmRuntime())
        try:
            params = json.loads(cfg["params"] or "{}")
        except (TypeError, ValueError):
            params = {}
        transition = alarms.evaluate(
            obj_row["object_type"], val, params, runtime,
            self.state.elapsed_seconds, cfg["time_delay"], cfg["time_delay_normal"],
        )
        if transition is None:
            return
        from_state, to_state = transition

        tname = alarms.transition_name(to_state)
        try:
            event_enable = json.loads(cfg["event_enable"] or "[]")
        except (TypeError, ValueError):
            event_enable = []
        if tname not in event_enable:
            return

        nc = notification_classes.get(cfg["notification_class_id"])
        priority = 100
        ack_required = False
        if nc is not None:
            priority = {
                "to-offnormal": nc["priority_to_offnormal"],
                "to-fault": nc["priority_to_fault"],
                "to-normal": nc["priority_to_normal"],
            }.get(tname, 100)
            try:
                ack_list = json.loads(nc["ack_required_transitions"] or "[]")
            except (TypeError, ValueError):
                ack_list = []
            ack_required = tname in ack_list

        detail = alarms.describe_transition(obj_row["object_type"], val, params, from_state, to_state, obj_row.get("units", ""))
        message = f"{obj_row['name']} transitioned {from_state} → {to_state}: {detail}"
        await asyncio.to_thread(self.db.log_alarm, {
            "object_id": obj_id,
            "device_id": dev["id"],
            "object_name": obj_row["name"],
            "from_state": from_state,
            "to_state": to_state,
            "priority": priority,
            "value": str(val),
            "message": message,
            "ack_required": 1 if ack_required else 0,
        })
        log_level = "info" if to_state == "normal" else "error" if to_state == "fault" else "warn"
        _log_event(dev["id"], log_level, f"Alarm: {message}")

        if nc is not None and self.app is not None:
            asyncio.create_task(alarms.send_event_notification(
                self.app, dev["device_instance"], obj_row, nc,
                from_state, to_state, priority, ack_required,
                device_capabilities=device_capabilities,
                log_fn=lambda level, msg: _log_event(dev["id"], level, msg),
                on_local_delivery=_log_event_notification_received,
            ))

    async def _evaluate_enrollment(
        self, enrollment: dict, obj_row: dict, dev: dict, val: Any, notification_classes: dict[int, dict],
        device_capabilities: dict[int, bool],
    ) -> None:
        """Same shape as _evaluate_alarm(), but for an Event Enrollment
        watching obj_row's present-value independently of obj_row's own
        alarm config — see alarms.evaluate_enrollment()."""
        runtime = self._enrollment_runtime.setdefault(enrollment["id"], alarms.AlarmRuntime())
        try:
            params = json.loads(enrollment["event_parameters"] or "{}")
        except (TypeError, ValueError):
            params = {}
        transition = alarms.evaluate_enrollment(
            enrollment["algorithm"], obj_row["object_type"], val, params, runtime,
            self.state.elapsed_seconds, enrollment["time_delay"], enrollment["time_delay_normal"],
        )
        if transition is None:
            return
        from_state, to_state = transition

        tname = alarms.transition_name(to_state)
        try:
            event_enable = json.loads(enrollment["event_enable"] or "[]")
        except (TypeError, ValueError):
            event_enable = []
        if tname not in event_enable:
            return

        nc = notification_classes.get(enrollment["notification_class_id"])
        priority = 100
        ack_required = False
        if nc is not None:
            priority = {
                "to-offnormal": nc["priority_to_offnormal"],
                "to-fault": nc["priority_to_fault"],
                "to-normal": nc["priority_to_normal"],
            }.get(tname, 100)
            try:
                ack_list = json.loads(nc["ack_required_transitions"] or "[]")
            except (TypeError, ValueError):
                ack_list = []
            ack_required = tname in ack_list

        detail = alarms.describe_transition(obj_row["object_type"], val, params, from_state, to_state, obj_row.get("units", ""))
        message = f"[{enrollment['name']}] {obj_row['name']} transitioned {from_state} → {to_state}: {detail}"

        await asyncio.to_thread(self.db.log_alarm, {
            "object_id": obj_row["id"],
            "device_id": dev["id"],
            "object_name": f"{enrollment['name']} ({obj_row['name']})",
            "from_state": from_state,
            "to_state": to_state,
            "priority": priority,
            "value": str(val),
            "message": message,
            "ack_required": 1 if ack_required else 0,
        })
        log_level = "info" if to_state == "normal" else "error" if to_state == "fault" else "warn"
        _log_event(dev["id"], log_level, f"Alarm: {message}")

        if nc is not None and self.app is not None:
            asyncio.create_task(alarms.send_event_notification(
                self.app, dev["device_instance"], obj_row, nc,
                from_state, to_state, priority, ack_required,
                device_capabilities=device_capabilities,
                log_fn=lambda level, msg: _log_event(dev["id"], level, msg),
                on_local_delivery=_log_event_notification_received,
            ))

    async def reload(self) -> None:
        """Rebuild the BACnet stack from DB (called after config changes)."""
        async with self._reload_lock:
            log.info("Reloading BACnet stack...")
            if self.app:
                for (bacnet_obj, _) in list(self._objects.values()):
                    try:
                        self.app.delete_object(bacnet_obj)
                    except Exception:
                        pass
                for bacnet_obj in list(self._trend_log_objects.values()):
                    try:
                        self.app.delete_object(bacnet_obj)
                    except Exception:
                        pass
                for bacnet_obj in list(self._schedule_objects.values()):
                    if getattr(bacnet_obj, "_interpret_schedule_handle", None):
                        bacnet_obj._interpret_schedule_handle.cancel()
                    try:
                        self.app.delete_object(bacnet_obj)
                    except Exception:
                        pass
                for bacnet_obj in list(self._calendar_objects.values()):
                    try:
                        self.app.delete_object(bacnet_obj)
                    except Exception:
                        pass
                self._objects.clear()
                self._trend_log_objects.clear()
                self._schedule_objects.clear()
                self._calendar_objects.clear()
                self._prev_values.clear()
                self._history.clear()
                self._alarm_runtime.clear()
                self._enrollment_runtime.clear()
                self._trend_log_last_value.clear()
                self._current_values = {}
                # Explicitly close the bacpypes3 socket before dropping the reference.
                # BinaryOutputObject↔PriorityArray form a circular reference that delays
                # GC, keeping the UDP socket bound to port 47808 and preventing re-bind.
                try:
                    await self.app.close()
                except Exception:
                    pass
                finally:
                    # Always sweep for any endpoint task close() didn't get
                    # to, so a failure here can never leak a live socket
                    # bind past this reload (see the helper's docstring).
                    try:
                        _force_close_bacnet_transports(self.app)
                    except Exception:
                        pass
                self.app = None
            await self.start()
            log.info("Reload complete")

    async def stop(self) -> None:
        """Cleanly shut down providers and the BACnet stack."""
        for provider_id, provider in list(self._providers.items()):
            try:
                provider.stop()
            except Exception:
                log.exception("Failed to stop simulation provider %s", provider_id)

        if self.app:
            for (bacnet_obj, _) in list(self._objects.values()):
                try:
                    self.app.delete_object(bacnet_obj)
                except Exception:
                    pass
            for bacnet_obj in list(self._trend_log_objects.values()):
                try:
                    self.app.delete_object(bacnet_obj)
                except Exception:
                    pass
            for bacnet_obj in list(self._schedule_objects.values()):
                if getattr(bacnet_obj, "_interpret_schedule_handle", None):
                    bacnet_obj._interpret_schedule_handle.cancel()
                try:
                    self.app.delete_object(bacnet_obj)
                except Exception:
                    pass
            for bacnet_obj in list(self._calendar_objects.values()):
                try:
                    self.app.delete_object(bacnet_obj)
                except Exception:
                    pass
            self._objects.clear()
            self._trend_log_objects.clear()
            self._schedule_objects.clear()
            self._calendar_objects.clear()
            try:
                await self.app.close()
            except Exception:
                pass
            finally:
                try:
                    _force_close_bacnet_transports(self.app)
                except Exception:
                    pass
            self.app = None
        log.info("BACnet stack stopped")

    async def add_object_hot(self, device_instance: int, obj_row: dict) -> None:
        """Hot-add a single object to the running BACnet app without full reload."""
        if not self.app:
            return
        slot = self._device_slots.get(device_instance, 0)
        dev_obj = self.app._virtual_devices.get(device_instance)
        dev_name = str(dev_obj.objectName) if dev_obj else ""
        bacnet_obj, behavior = self._create_object(obj_row, slot, dev_name)
        self.app.add_object(bacnet_obj)
        self._objects[obj_row["id"]] = (bacnet_obj, behavior)
        dev = self.db.get_device(int(obj_row["device_id"]))
        if (
            dev
            and dev.get("simulation_mode", "simulation") != "mirror"
            and int(obj_row["id"]) not in self._point_output_owner
        ):
            self._builtin_provider.sync_point_config(self._point_config_from_row(obj_row))
        if dev_obj:
            existing = list(self.app._virtual_object_lists.get(device_instance, []))
            existing.append(bacnet_obj.objectIdentifier)
            dev_obj.objectList = existing
            self.app._virtual_object_lists[device_instance] = existing

    def set_manual_value(self, obj_id: int, value: Any) -> bool:
        if obj_id not in self._objects:
            return False
        bacnet_obj, behavior = self._objects[obj_id]
        if isinstance(behavior, ManualBehavior):
            behavior.set(value)
            active_behavior = behavior
        else:
            # Keep the legacy tuple shape during migration; provider is the
            # authoritative behavior executor.
            new_b = ManualBehavior({"value": value})
            self._objects[obj_id] = (bacnet_obj, new_b)
            active_behavior = new_b

        obj_row = self.db.get_object(obj_id)
        if obj_row:
            manual_cfg = PointConfig(
                point_id=obj_id,
                behavior="manual",
                behavior_params=json.dumps({"value": value}),
                manual_value=value,
                object_type=str(obj_row["object_type"]),
            )
            if obj_id not in self._point_output_owner:
                self._builtin_provider.sync_point_config(manual_cfg)
                self._builtin_provider.set_inputs({obj_id: value})
            self._update_value(bacnet_obj, obj_row["object_type"], value)
            # _update_value() only touches the live BACnet presentValue --
            # get_object_value() (what Functional Test Verify/Wait Until
            # steps, the Energy Engine, etc. actually read) reads
            # self._prev_values, which is otherwise only refreshed by the
            # periodic tick loop. Without this, a manual override written
            # and immediately read back (e.g. a Set block followed straight
            # by a Verify, no Wait in between) sees the STALE pre-write
            # value until the next tick happens to run -- this was the
            # actual cause of a Set-then-Verify reporting the point's OLD
            # state instead of what was just written.
            self._prev_values[obj_id] = normalize_present_value(
                obj_row["object_type"], active_behavior.compute(None)
            )
        return True

    async def write_object(self, obj_id: int, value: Any, source: Optional[str] = None) -> bool:
        """Handle a BACnet WriteProperty — switches the object to manual, persists, updates live.

        Unlike the REST "Set" endpoint (set_object_value, which logs its own
        "Manual override" activity-log entry), this is the path a genuine
        external BACnet client's WriteProperty request takes — it was
        previously silent in the per-device Activity Log (still counted in
        the analytics traffic metrics, just not human-audit-visible), so a
        real external write left no record of what was written or by whom.
        `source` is the requesting client's address (apdu.pduSource), when
        the caller has one, for a real audit trail of who wrote what.
        """
        if obj_id not in self._objects:
            return False
        bacnet_obj, _ = self._objects[obj_id]
        obj_row = await asyncio.to_thread(self.db.get_object, obj_id)
        if not obj_row:
            return False
        await asyncio.to_thread(self.db.write_object, obj_id, value)
        new_b = ManualBehavior({"value": value})
        self._objects[obj_id] = (bacnet_obj, new_b)

        # Re-read the persisted row so provider config matches DB exactly.
        updated_row = await asyncio.to_thread(self.db.get_object, obj_id)
        if updated_row and obj_id not in self._point_output_owner:
            self._builtin_provider.sync_point_config(self._point_config_from_row(updated_row))
            self._builtin_provider.set_inputs({obj_id: value})

        self._update_value(bacnet_obj, obj_row["object_type"], value)
        val_str = str(value) + (f" {obj_row['units']}" if obj_row.get("units") and obj_row["units"] != "no-units" else "")
        source_suffix = f" (from {source})" if source else ""
        _log_event(obj_row["device_id"], "info", f"External write: {obj_row['name']} → {val_str}{source_suffix}")
        return True

    @staticmethod
    def _priority_value_out(pv: PriorityValue) -> Any:
        """Decode a PriorityValue slot to a plain Python value, or None if null."""
        if pv._choice == "null":
            return None
        raw = getattr(pv, pv._choice)
        return str(raw) == "active" if pv._choice == "enumerated" else raw

    def get_priority_array(self, obj_id: int) -> Optional[dict]:
        """Read all 16 priority-array slots + relinquish default (GH #17).
        Returns None for object types with no real priority array — i.e.
        everything except the three Commandable *-output types."""
        if obj_id not in self._objects:
            return None
        bacnet_obj, _ = self._objects[obj_id]
        if not isinstance(bacnet_obj, Commandable):
            return None
        cp = bacnet_obj.currentCommandPriority
        rd = bacnet_obj.relinquishDefault
        relinquish_default = str(rd) == "active" if isinstance(rd, BinaryPV) else \
            (float(rd) if isinstance(rd, Real) else int(rd))
        return {
            "priority_array": [self._priority_value_out(pv) for pv in bacnet_obj.priorityArray],
            "relinquish_default": relinquish_default,
            "current_command_priority": int(cp.unsigned) if cp.unsigned is not None else None,
        }

    async def write_priority(self, obj_id: int, priority: int, value: Any) -> bool:
        """Write (or, if value is None, relinquish) a specific priority-array
        slot on a Commandable object (GH #17) — this is a direct priority-array
        write, distinct from write_object()'s "the sim value" (priority 16)."""
        if obj_id not in self._objects:
            return False
        bacnet_obj, _ = self._objects[obj_id]
        if not isinstance(bacnet_obj, Commandable):
            return False
        if not (1 <= priority <= 16):
            return False
        obj_row = await asyncio.to_thread(self.db.get_object, obj_id)
        if not obj_row:
            return False
        otype = obj_row["object_type"]
        if value is None:
            pv = PriorityValue(null=())
        elif otype == "analog-output":
            pv = PriorityValue(real=float(value))
        elif otype == "multi-state-output":
            pv = PriorityValue(unsigned=int(value))
        else:
            active = coerce_binary_write_value(value)
            pv = PriorityValue(enumerated=BinaryPV("active" if active else "inactive"))
        bacnet_obj.priorityArray[priority - 1] = pv
        # Same staleness gap as set_manual_value() above: writing into the
        # priority array updates the live BACnet presentValue immediately
        # (Commandable resolves it across all 16 slots on read), but
        # self._prev_values -- what get_object_value() actually reads -- is
        # otherwise only refreshed by the periodic tick loop, so a Set on a
        # commandable point followed straight by a Verify/Wait Until would
        # see the pre-write value until the next tick. Best-effort: a
        # decode failure here must never fail the write itself.
        try:
            pv_out = bacnet_obj.presentValue
            resolved = (
                str(pv_out) == "active" if isinstance(pv_out, BinaryPV)
                else (float(pv_out) if isinstance(pv_out, Real) else int(pv_out))
            )
            self._prev_values[obj_id] = normalize_present_value(otype, resolved)
        except Exception:
            log.exception("Failed to refresh cached value for object %s after a priority-array write", obj_id)
        return True

    def get_state(self) -> dict:
        return self._current_values

    def _upsert_current_value_snapshot(
        self,
        dev: dict,
        obj_row: dict,
        value: Any,
    ) -> None:
        """Patch one live value into the API/WebSocket snapshot cache.

        Normal simulated values flow through tick(), but Mirror values are
        injected by mirror_sync_loop on a separate cadence. Keeping the cache
        current here lets the UI show mirror-device values even while the
        simulation clock is paused or stopped.
        """
        current = self._current_values if isinstance(self._current_values, dict) else {}
        devices = [dict(d) for d in current.get("devices", [])]
        did = dev["device_instance"]

        device_snapshot = next(
            (d for d in devices if d.get("device_instance") == did),
            None,
        )
        if device_snapshot is None:
            device_snapshot = {
                "device_instance": did,
                "name": dev["name"],
                "objects": [],
            }
            devices.append(device_snapshot)

        objects = [
            dict(o)
            for o in device_snapshot.get("objects", [])
            if o.get("id") != obj_row["id"]
        ]
        objects.append({
            "id": obj_row["id"],
            "name": obj_row["name"],
            "object_type": obj_row["object_type"],
            "object_instance": obj_row["object_instance"],
            "value": value,
            "units": obj_row.get("units", ""),
            "behavior": obj_row["behavior"],
        })
        objects.sort(
            key=lambda o: (
                str(o.get("object_type", "")),
                int(o.get("object_instance", 0)),
            ),
        )
        device_snapshot["objects"] = objects

        self._current_values = {
            "devices": devices,
            "tick": current.get("tick", self.state.elapsed_seconds),
        }

    def db_id_for_bacnet_object(self, bacnet_obj: Any) -> Optional[int]:
        """Reverse lookup: given a live bacpypes3 object, find the DB row id
        that owns it. Used by both incoming WriteProperty requests and
        Schedule objects' present_value_changed() (see bacnet_schedule.py)."""
        for did, (bobj, _) in self._objects.items():
            if bobj is bacnet_obj:
                return did
        return None

    @staticmethod
    def _trend_log_value_changed(value: Any, last: Any, cov_increment: float) -> bool:
        if isinstance(value, bool) or isinstance(last, bool):
            return bool(value) != bool(last)
        try:
            return abs(float(value) - float(last)) >= cov_increment
        except (TypeError, ValueError):
            return value != last

    async def _sample_trend_log(self, tl_id: int, val: Any) -> Optional[int]:
        """Append a record and, if this trend log is exposed on the BACnet
        wire, refresh its recordCount/totalRecordCount so a ReadProperty
        reflects the latest buffer state without waiting for a reload().
        Returns the new sequence number, or None if the buffer was full
        with stop_when_full set."""
        seq = await asyncio.to_thread(self.db.add_trend_record, tl_id, val)
        if seq is None:
            return None
        bacnet_obj = self._trend_log_objects.get(tl_id)
        if bacnet_obj is not None:
            cfg = await asyncio.to_thread(self.db.get_trend_log, tl_id)
            if cfg:
                bacnet_obj.recordCount = Unsigned(cfg["record_count"])
                bacnet_obj.totalRecordCount = Unsigned(cfg["total_record_count"])
        return seq

    def refresh_trend_log_buffer_empty(self, tl_id: int) -> None:
        """Reflect a cleared record buffer on the BACnet-wire object, if any."""
        bacnet_obj = self._trend_log_objects.get(tl_id)
        if bacnet_obj is not None:
            bacnet_obj.logBuffer = SequenceOf(LogRecord)([])
            bacnet_obj.recordCount = Unsigned(0)

