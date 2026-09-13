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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from bacpypes3.basetypes import BinaryPV, LogRecord, PriorityValue
from bacpypes3.local.cmd import Commandable
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.primitivedata import Boolean, Real, Unsigned
from bacpypes3.constructeddata import SequenceOf
from fastapi import WebSocket

from ..bacnet import alarms
from ..bacnet import calendar as bacnet_calendar
from ..bacnet import object_factory
from ..bacnet.app import SimApplication, coerce_binary_write_value, normalize_present_value
from .. import dependencies
from ..db import Database
from ..fault_detection import FaultDetectionEngine
from ..monitoring.event_log import _log_event, _log_event_notification_received
from ..energy import EnergyEngine
from .behaviors import Behavior, FaultBehavior, ManualBehavior, make_behavior
from .protocol_runtime import (
    DeviceRuntimeConfig, PointQuality, PointRuntimeConfig, PointValue, RuntimePoint,
)
from .protocols.base import ProtocolAdapter, ProtocolContext
from .providers.base import PointConfig, PointDataType, ProviderStatus, SimulationContext, SimulationProvider
from .providers.builtin import BuiltInSimulationProvider
from .state import SimState

log = logging.getLogger("bacnet-sim")


def _is_offline_fault_active(live_behavior: Any) -> bool:
    """Protocol-neutral: is an "offline" fault CURRENTLY active on this point's live Behavior
    THIS tick? Factored out of _offline_reliability_override() below (BACnet's own
    reliability-string mapping of this same question) so tick()'s generic per-point loop can ask
    the identical question to build a PointQuality for ProtocolAdapter.write_point(), without
    duplicating the isinstance/fault_type/_fault_active check in two places."""
    return (
        isinstance(live_behavior, FaultBehavior)
        and live_behavior.fault_type == "offline"
        and live_behavior._fault_active
    )


def _offline_reliability_override(live_behavior: Any, configured_reliability: Optional[str]) -> str:
    """The Reliability string SimEngine.tick() used to apply directly to a point's BACnet object
    THIS tick, given its live Behavior instance (whatever actually computed this tick's value --
    see tick()'s own comment on why that's NOT always `self._runtime_points[obj_id].behavior`)
    and the object's own persisted `reliability` field (the value it would show if no fault were
    involved at all). Still the reliability-mapping source of truth (test_fault_offline.py calls
    this directly) and still used by BacnetProtocolAdapter.write_point() -- tick() itself no
    longer calls it directly, since Present_Value and Reliability now travel together through one
    ProtocolAdapter.write_point() call, fed a protocol-neutral PointQuality (see
    _is_offline_fault_active() above) rather than a BACnet Reliability string.

    A pure function, deliberately factored out of tick() so this specific decision -- the one
    piece of "offline" that isn't visible in behaviors.py's own unit tests, since FaultBehavior
    itself has no BACnet object reference at all by design -- is unit-testable without running a
    full engine tick. "offline" is the only fault_type that changes Reliability at all (see
    FaultBehavior's own class docstring for why spike/stuck/bias/drift/delay are pure
    Present_Value transformations instead); every other case here restores the object's own
    configured reliability, which is what makes recovery, switching away from offline, and
    Behavior != "fault" all self-correct with no dedicated cleanup path."""
    is_offline_active = _is_offline_fault_active(live_behavior)
    return "communication-failure" if is_offline_active else (configured_reliability or "no-fault-detected")


class SimEngine:
    """Manages the running BACnet application and the simulation tick loop."""

    def __init__(self, db: Database):
        self.db = db
        self.state = SimState()
        self.app: Optional[SimApplication] = None
        self.network_port: Optional[NetworkPortObject] = None
        # object DB id → RuntimePoint (protocol-neutral: a handle owned by whichever
        # ProtocolAdapter created it, plus the point's own live Behavior -- see
        # protocol_runtime.py's own docstring for why `.behavior` is a transitional field, not
        # part of the intended protocol boundary). Was `dict[int, tuple[bacpypes3_object,
        # Behavior]]` before the protocol-adapter seam; every consumer below now goes through
        # `.handle`/`.adapter`/`.behavior` instead of tuple-unpacking.
        self._runtime_points: dict[int, RuntimePoint] = {}
        # protocol name -> the ProtocolAdapter driving it. SimEngine does not construct wire
        # protocol adapters; application composition registers them explicitly.
        self._protocol_adapters: dict[str, ProtocolAdapter] = {}
        # protocol name -> currently started this start()/reload() cycle -- what stop()/reload()
        # iterate to tear down every adapter that actually ran, regardless of whether BACnet
        # itself ever started (see stop()'s own comment on why this can't just be `if self.app:`).
        self._active_adapters: set[str] = set()
        # protocol name -> device handles that adapter's add_device() returned this cycle, for
        # stop()/reload() to hand back to remove_device(). Only populated for non-BACnet
        # protocols -- BACnet's own device/point teardown is one batch call to
        # BacnetProtocolAdapter.stop() instead (see that method's own docstring on why it isn't
        # decomposed into per-device remove_device() calls, unlike every other protocol).
        self._adapter_device_handles: dict[str, list[Any]] = {}
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
        # object DB id → live value snapshotted immediately before reload()
        # tears state down, consulted by _create_object() while rebuilding
        # provider-owned raw/constant points (see reload()'s own comment).
        # One-shot: populated at the start of a reload, drained by the end
        # of start(), empty the rest of the time.
        self._reload_preserved_values: dict[int, Any] = {}
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
        # Replay propagation: obj DB id -> last normalized value injected by
        # replay_playback_loop. Kept separate from _mirror_values so
        # /sim/state diagnostics don't conflate Twin (live external mirror)
        # and Replay (recorded-data playback) devices.
        self._replay_values: dict[int, Any] = {}
        # Per-object link propagation: obj DB id -> last normalized value
        # injected by _link_sync_once (runtime.py) for an object with its
        # own source_object_id set -- independent of _mirror_values/
        # _replay_values above, which are keyed by the OWNING DEVICE's
        # simulation_mode rather than a per-object condition.
        self._linked_values: dict[int, Any] = {}
        # device DB id -> in-memory playback transport state (never
        # persisted -- a restart resets every Replay device to stopped-at-
        # first-sample, an accepted simplification). See _get_replay_state.
        self._replay_playback: dict[int, dict[str, Any]] = {}
        # device DB id -> the sample_index whose values are currently
        # written into this device's objects -- lets advance_replay_playback
        # tell "already showing this frame" apart from "needs re-injecting"
        # (a seek while paused/stopped still needs one injection).
        self._replay_applied_index: dict[int, int] = {}
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

        # runtime_id -> deliberately, individually paused via pause_provider()
        # (Simulation -> Model's own per-device Pause control), as opposed to
        # merely PAUSED because the global clock is paused. This distinction
        # is what lets a locally-paused provider survive a global
        # pause()/resume() cycle still paused (see resume()'s own check
        # below) -- clock_state alone can't tell the two apart, since both
        # produce the exact same provider ProviderStatus.PAUSED.
        self._locally_paused_provider_ids: set[str] = set()

        # runtime_id -> per-model real-time-factor target, default 1.0 (not
        # tracked here for unregistered/never-set ids -- see
        # set_provider_real_time_factor's own get(..., 1.0) fallback
        # everywhere this is read). Scales how much SIMULATED time this
        # provider's own step(dt) call represents per shared wall-clock
        # tick -- every provider still steps together, on the same cadence,
        # via _run_registered_providers; nothing here changes when a
        # provider is stepped, only how big a dt it's told. Confirmed
        # numerically safe for every catalog model except
        # EnergyPlusThermalZone (the one Co-Simulation-kind FMU): iot-models
        # runs ME2 models through pyfmi's fmu.simulate() under an adaptive-
        # step CVode solver keyed to its own rtol/atol
        # (CVODE_RTOL/CVODE_ATOL in iot-models' shared/runtime/models/
        # manager.py), so a larger requested interval changes how far it
        # integrates before returning a value, not the solver's own
        # internal step size/accuracy -- dt*RTF is exactly the same kind of
        # scaling that already happens implicitly whenever TICK_SECONDS
        # itself changes. EnergyPlusThermalZone's own CS do_step/internal-
        # sub-stepping behavior under a scaled communication step is
        # unverified (no in-repo evidence either way) -- accepted,
        # documented limitation, not fixed here.
        self._provider_real_time_factor: dict[str, float] = {}

        # Provider runtime registry.
        #
        # "builtin" is always present and owns every normal simulated point
        # that has not been explicitly claimed as an output by another
        # provider. Additional providers (FMU/AI/etc.) register
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

    def register_protocol_adapter(
        self,
        protocol: str,
        adapter: ProtocolAdapter,
        *,
        replace: bool = False,
    ) -> None:
        protocol = str(protocol).strip()
        if not protocol:
            raise ValueError("protocol must be explicit")
        if protocol in self._protocol_adapters and not replace:
            raise ValueError(f"ProtocolAdapter already registered for protocol {protocol!r}")
        if getattr(adapter, "protocol", protocol) != protocol:
            raise ValueError(
                f"ProtocolAdapter protocol {getattr(adapter, 'protocol', None)!r} "
                f"does not match registration {protocol!r}"
            )
        self._protocol_adapters[protocol] = adapter

    def get_protocol_adapter(self, protocol: str) -> ProtocolAdapter:
        protocol = str(protocol).strip()
        if not protocol:
            raise ValueError("protocol must be explicit")
        try:
            return self._protocol_adapters[protocol]
        except KeyError as exc:
            raise ValueError(f"No ProtocolAdapter registered for protocol {protocol!r}") from exc

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
        object_type = obj_row.get("object_type")
        if object_type is None:
            raise ValueError(f"Object row {obj_row.get('id')!r} is missing explicit object_type")
        return PointConfig(
            point_id=int(obj_row["id"]),
            behavior=str(obj_row.get("behavior") or "constant"),
            behavior_params=str(obj_row.get("behavior_params") or '{"value":0}'),
            data_type=SimEngine._point_data_type_from_bacnet_object_type(str(object_type)),
            manual_value=obj_row.get("manual_value"),
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
        two System models, or a future FMU/AI provider from racing to write
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
            self._locally_paused_provider_ids.discard(provider_id)
            self._provider_real_time_factor.pop(provider_id, None)

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
                # Distinguishes "paused because a user explicitly paused
                # just this model" from merely being PAUSED as a side
                # effect of the global clock being paused -- see
                # pause_provider()/resume()'s own comments.
                "locally_paused": provider_id in self._locally_paused_provider_ids,
                "real_time_factor": self._provider_real_time_factor.get(provider_id, 1.0),
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

        runtime_point = self._runtime_points.get(point_id)
        if runtime_point is None:
            return None
        return self._plain_present_value(getattr(runtime_point.handle, "presentValue", None))

    def resolve_provider_input_value(self, point_id: int) -> Any:
        """Public counterpart of _provider_input_value for callers outside
        the tick loop (provider registration/recovery), where no same-tick
        generated_values exist yet. Read-only access to _prev_values/
        _runtime_points -- safe to call from a worker thread (e.g. the recovery
        sweep's asyncio.to_thread body): dict reads are atomic under the
        GIL, and the tick loop's own writes to these dicts happen on the
        main event-loop thread, so at worst this returns a value that is one
        tick stale, the same characteristic _provider_input_value already
        has mid-tick.
        """
        return self._provider_input_value(point_id, {})

    def _step_one_provider(
        self,
        provider_id: str,
        provider: SimulationProvider,
        inputs: dict[int, Any],
        dt: float,
    ) -> tuple[dict[int, Any], dict[str, Any], str | None] | None:
        """Steps a single provider (set_inputs / start-if-needed / step /
        get_outputs / diagnostics) and returns (outputs, diagnostics,
        runtime_state), or None on failure (already logged here). Called
        directly for a wave with only one provider, or via
        ThreadPoolExecutor.submit for a wave with more than one -- FMU
        providers each own their own FMURuntimeClient/session (see
        FMUSimulationProvider.__init__), so concurrent calls for *different*
        providers never touch shared client/session state. Deliberately
        touches no *engine* shared state either (self._provider_diagnostics/
        self._model_input_shadow_values/generated_values) -- the caller
        merges every wave's results back in sequentially, in registration
        order, once the whole wave has finished, so concurrent calls here
        never race on those dict writes."""
        try:
            provider.set_inputs(inputs)
            status = provider.get_status()
            if status == ProviderStatus.PAUSED:
                # Deliberately paused (see pause_provider()) -- never
                # auto-resume from this per-tick path the way a
                # never-started READY provider would be; only an explicit
                # resume (resume_provider(), or global resume() for a
                # NON-locally-paused one) calls provider.start(). Falls
                # through to the same outputs/diagnostics reporting below,
                # just without start()/step() -- get_outputs() still
                # returns its last-known values unchanged, so this
                # provider's points correctly stay frozen and it still
                # shows up live in diagnostics (matches how a STOPPED
                # provider already behaves today, via its own internal
                # step() guard: FMUSimulationProvider.step() itself
                # no-ops when self._status != RUNNING).
                pass
            else:
                if status != ProviderStatus.RUNNING:
                    provider.start()
                # Timed separately from the rest of this method (set_inputs/
                # get_outputs/diagnostics are in-memory and fast) -- step()
                # is the one call that can be a real blocking HTTP round trip
                # to the FMU runtime, and is the thing tick_loop's own overrun
                # warning is actually explained by when it fires.
                step_started_at = time.monotonic()
                provider.step(dt)
                step_duration = time.monotonic() - step_started_at
                if step_duration > dependencies.TICK_SECONDS:
                    log.warning(
                        "Provider %s (%s) step() took %.2fs, exceeding TICK_SECONDS=%.2fs",
                        provider_id,
                        type(provider).__name__,
                        step_duration,
                        dependencies.TICK_SECONDS,
                    )
                else:
                    log.debug(
                        "Provider %s (%s) step() took %.2fs",
                        provider_id,
                        type(provider).__name__,
                        step_duration,
                    )
            outputs = dict(provider.get_outputs())
            diagnostics = {}
            if hasattr(provider, "get_diagnostics"):
                try:
                    diagnostics = provider.get_diagnostics()
                except Exception as exc:
                    diagnostics = {"diagnostics_error": str(exc)}
            runtime_state = str(
                diagnostics.get("runtime_state")
                or diagnostics.get("status")
                or ""
            ) or None
            return outputs, diagnostics, runtime_state
        except Exception:
            log.exception(
                "Simulation provider %s (%s) failed during tick",
                provider_id,
                type(provider).__name__,
            )
            return None

    def _run_registered_providers(
        self,
        dt: float,
        generated_values: dict[int, Any],
    ) -> dict[int, Any]:
        """Run every non-built-in provider once, staged into dependency
        "waves" and stepped concurrently within each wave.

        Providers are mostly independent FMU-backed devices whose step() is
        a blocking HTTP round trip to the external FMU runtime -- stepping
        them one at a time made a single tick's duration the *sum* of every
        device's round trip (confirmed live: Replay Recording exports showed
        5-6 consecutive identical samples, ~20-30s, across every point on a
        device at once, on projects with several FMU-backed devices).

        Full unordered parallelism would silently break a documented
        contract this project actively relies on (RTU -> VAV chaining,
        confirmed live via FMU INPUT RESOLVE log lines): "providers execute
        in registration order; if provider B reads a point produced by
        provider A and A was registered first, B sees A's value from the
        same tick" (see register_simulation_provider's own docstring).
        Wave staging preserves that exactly -- a provider's wave is
        1 + max(wave of any *earlier-registered* provider whose output
        points intersect this provider's input points), so a dependent
        provider always lands in a strictly later wave than what it depends
        on, and waves run strictly sequentially (only providers *within* a
        wave -- the common case, since most FMU-backed devices here are
        independent systems -- run concurrently). Only earlier-registered
        providers are considered because, under the original sequential
        semantics, a provider can never see a *later*-registered provider's
        same-tick output anyway (it hasn't run yet) -- so there's no real
        dependency edge in that direction, and no cycle detection is needed.
        """
        provider_items = [
            (provider_id, provider)
            for provider_id, provider in self._providers.items()
            if provider_id != "builtin"
        ]

        wave_of: dict[str, int] = {}
        waves: dict[int, list[tuple[str, SimulationProvider]]] = {}
        for idx, (provider_id, provider) in enumerate(provider_items):
            input_points = self._provider_input_points.get(provider_id, set())
            max_dep_wave = -1
            for earlier_id, _earlier_provider in provider_items[:idx]:
                if input_points & self._provider_output_points.get(earlier_id, set()):
                    max_dep_wave = max(max_dep_wave, wave_of[earlier_id])
            wave_of[provider_id] = max_dep_wave + 1
            waves.setdefault(max_dep_wave + 1, []).append((provider_id, provider))

        for wave_num in sorted(waves):
            wave_providers = waves[wave_num]

            # Inputs are resolved from the pre-wave generated_values snapshot
            # -- unchanged for the whole wave, since same-wave providers run
            # concurrently and must not observe each other's outputs (that's
            # exactly what wave staging exists to prevent).
            wave_inputs = {
                provider_id: {
                    point_id: value
                    for point_id in self._provider_input_points.get(provider_id, set())
                    if (value := self._provider_input_value(point_id, generated_values))
                    is not None
                }
                for provider_id, _provider in wave_providers
            }

            # Per-provider dt -- scales how much SIMULATED time this
            # provider's own step() represents, not when/how often it's
            # stepped (every provider is still stepped together, on the
            # exact same wall-clock cadence, right here). See
            # set_provider_real_time_factor's own docstring for why this is
            # numerically safe for every catalog model except
            # EnergyPlusThermalZone.
            provider_dt = {
                provider_id: dt * self._provider_real_time_factor.get(provider_id, 1.0)
                for provider_id, _provider in wave_providers
            }

            if len(wave_providers) == 1:
                # Skip ThreadPoolExecutor overhead for the common
                # single-provider-per-wave case.
                only_id, only_provider = wave_providers[0]
                results = {
                    only_id: self._step_one_provider(
                        only_id, only_provider, wave_inputs[only_id], provider_dt[only_id],
                    )
                }
            else:
                with ThreadPoolExecutor(max_workers=min(8, len(wave_providers))) as pool:
                    futures = {
                        provider_id: pool.submit(
                            self._step_one_provider, provider_id, provider, wave_inputs[provider_id], provider_dt[provider_id],
                        )
                        for provider_id, provider in wave_providers
                    }
                    results = {provider_id: future.result() for provider_id, future in futures.items()}

            # Merge this wave's results back sequentially, in registration
            # order -- same shared-state writes the original loop made,
            # just deferred until the whole wave has finished stepping.
            for provider_id, provider in wave_providers:
                result = results[provider_id]
                if result is None:
                    continue  # failure already logged in _step_one_provider

                inputs = wave_inputs[provider_id]
                outputs, diagnostics, runtime_state = result

                self._provider_diagnostics[provider_id] = diagnostics
                for point_id, value in inputs.items():
                    self._model_input_shadow_values[int(point_id)] = (value, runtime_state)

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
            if provider_id in self._locally_paused_provider_ids:
                # Deliberately paused via pause_provider() (Simulation ->
                # Model's own per-device Pause control) before this global
                # resume happened -- a local pause survives a global
                # pause()/resume() cycle, so this provider is skipped here
                # and stays paused until its own resume_provider() call.
                continue
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
        # A full reset legitimately un-pauses everything -- every provider
        # comes back fresh via its own reset()/re-initialize() below, so
        # nothing should still be considered "locally paused" afterward.
        self._locally_paused_provider_ids.clear()
        for provider_id, provider in list(self._providers.items()):
            try:
                provider.reset()
            except Exception:
                log.exception("Failed to reset simulation provider %s", provider_id)

    def pause_provider(self, runtime_id: str) -> bool:
        """Freeze just this one provider's outputs in place -- everything
        else (other providers, builtin, the tick loop itself) keeps
        running normally. See _step_one_provider's own PAUSED handling for
        why this actually sticks (the bug it fixes: a naive provider.pause()
        used to get silently undone on the very next tick)."""
        provider = self._providers.get(runtime_id)
        if provider is None:
            return False
        self._locally_paused_provider_ids.add(runtime_id)
        provider.pause()
        return True

    def resume_provider(self, runtime_id: str) -> bool:
        """Explicitly resume a provider paused via pause_provider(). Calls
        provider.start() directly, outside the per-tick auto-start path --
        the only thing that can bring a PAUSED provider back to RUNNING."""
        provider = self._providers.get(runtime_id)
        if provider is None:
            return False
        self._locally_paused_provider_ids.discard(runtime_id)
        provider.start()
        return True

    def reset_provider(self, runtime_id: str) -> bool:
        """Reset just this one provider (real FMU-session terminate +
        re-initialize, or the builtin-shaped equivalent) -- also clears any
        local-pause tracking, since a reset provider comes back running."""
        provider = self._providers.get(runtime_id)
        if provider is None:
            return False
        self._locally_paused_provider_ids.discard(runtime_id)
        provider.reset()
        return True

    def set_provider_real_time_factor(self, runtime_id: str, target: float) -> bool:
        """Scales how much simulated time this one provider's own step(dt)
        call represents per shared wall-clock tick (>1 = faster than every
        other provider, <1 = slower) -- see _run_registered_providers'
        provider_dt and this field's own docstring in __init__ for why
        that's numerically safe for every catalog model except
        EnergyPlusThermalZone. Every provider still steps together, on the
        same wall-clock cadence; this never changes when a provider is
        stepped, only how big a dt it's told."""
        if runtime_id not in self._providers:
            return False
        self._provider_real_time_factor[runtime_id] = target
        return True

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

    @staticmethod
    def _protocol_of(device_row: dict) -> str:
        protocol = str(device_row.get("protocol") or "").strip()
        if not protocol:
            raise ValueError("Device row is missing explicit protocol")
        return protocol

    @staticmethod
    def _device_runtime_config(device_row: dict) -> DeviceRuntimeConfig:
        return DeviceRuntimeConfig(
            device_id=int(device_row["id"]),
            name=str(device_row["name"]),
            protocol=SimEngine._protocol_of(device_row),
            protocol_config=device_row,
        )

    @staticmethod
    def _point_runtime_config(obj_row: dict) -> PointRuntimeConfig:
        return PointRuntimeConfig(
            point_id=int(obj_row["id"]),
            device_id=int(obj_row["device_id"]),
            name=str(obj_row["name"]),
            units=obj_row.get("units"),
            protocol_config=obj_row,
        )

    @staticmethod
    def _point_data_type_from_bacnet_object_type(object_type: str) -> PointDataType:
        normalized = str(object_type or "").strip().lower().replace("_", "-")
        if normalized.startswith("analog-"):
            return PointDataType.REAL
        if normalized.startswith("binary-"):
            return PointDataType.BOOLEAN
        if normalized.startswith("multi-state-"):
            return PointDataType.ENUM
        if normalized == "characterstring-value":
            return PointDataType.STRING
        raise ValueError(f"Unsupported BACnet object_type for simulation provider: {object_type!r}")

    async def start(self) -> None:
        devices = await asyncio.to_thread(self.db.get_devices)
        self._devices_by_instance = {d["device_instance"]: d for d in devices}
        enabled = self._simulated_enabled_devices(devices)

        # A start/reload rebuilds every protocol runtime from DB, so rebuild only the Built-in
        # adapter from the same source of truth. Registered FMU/AI providers remain registered
        # and keep their output claims.
        self._replace_builtin_provider(BuiltInSimulationProvider())
        # Fed by whichever ProtocolAdapter.start() calls run below. Batch adapters fill these
        # internally; other adapters use the generic per-device loop further down. They are then
        # consumed once by the single
        # self._builtin_provider.initialize(...) call after the loop, regardless of which
        # protocol(s) actually produced points this cycle.
        self._boot_provider_participants = []
        self._boot_provider_points = []

        if not enabled:
            log.info("No enabled devices — protocol runtimes idle")
            self.app = None
            self._reload_preserved_values = {}
            return

        devices_by_protocol: dict[str, list[dict]] = {}
        for dev in enabled:
            devices_by_protocol.setdefault(self._protocol_of(dev), []).append(dev)

        for protocol, devs in devices_by_protocol.items():
            adapter = self.get_protocol_adapter(protocol)
            device_configs = [self._device_runtime_config(d) for d in devs]
            await adapter.start(ProtocolContext(devices=device_configs))
            self._active_adapters.add(protocol)
            if adapter.builds_runtime_points_in_start:
                continue
            for dev, device_config in zip(devs, device_configs):
                device_handle = adapter.add_device(device_config)
                self._adapter_device_handles.setdefault(protocol, []).append(device_handle)
                objects = await asyncio.to_thread(self.db.get_objects, dev["id"])
                for obj_row in objects:
                    if not obj_row["enabled"]:
                        continue
                    point_config = self._point_runtime_config(obj_row)
                    point_handle = adapter.add_point(device_handle, point_config)
                    behavior = make_behavior(
                        obj_row["behavior"], obj_row["behavior_params"], obj_row.get("manual_value"),
                    )
                    self._point_behaviors[int(obj_row["id"])] = behavior
                    self._runtime_points[obj_row["id"]] = RuntimePoint(
                        config=point_config, adapter=adapter, handle=point_handle, behavior=behavior,
                    )
                    self._boot_provider_participants.append(int(dev["id"]))
                    if (
                        dev.get("simulation_mode", "simulation") != "mirror"
                        and int(obj_row["id"]) not in self._point_output_owner
                    ):
                        self._boot_provider_points.append(self._point_config_from_row(obj_row))

        self._builtin_provider.initialize(
            SimulationContext(
                participant_device_ids=self._boot_provider_participants,
                point_configs=self._boot_provider_points,
            )
        )
        if self.clock_state == "running":
            self._builtin_provider.start()

        # Keep registered non-built-in models alive across a runtime rebuild.
        # Device/object CRUD reloads should not rewind FMU/AI sessions; their
        # output ownership/mappings remain valid because the DB object IDs do
        # not change.
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

        # One-shot: every _create_object() call for this start() has now
        # run, so the pre-reload snapshot has done its job. Drop it rather
        # than let it linger and get reused by some later, unrelated
        # start() call.
            self._reload_preserved_values = {}

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

        Regular simulator objects are stored in ``self._runtime_points`` as:
            database object id -> RuntimePoint(handle=live BACpypes object, ...)

        Matching the actual live ``objectIdentifier`` is safer than trying to
        reverse the slot-offset formula because it also stays correct after
        reloads and for any future changes to instance allocation.
        """
        normalized_type = str(object_type).strip().lower().replace('_', '-')

        for object_db_id, runtime_point in self._runtime_points.items():
            try:
                identifier = runtime_point.handle.objectIdentifier
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

    def _apply_fmu_behavior(
        self, behavior: Behavior, behavior_name: str, raw_value: Any, state: SimState,
    ) -> Any:
        """Apply a provider (FMU/AI model)-owned point's configured
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
        for obj_id in list(self._runtime_points):
            obj_row = await asyncio.to_thread(self.db.get_object, obj_id)
            if not obj_row:
                continue
            runtime_rows[obj_id] = obj_row
            dev = dev_map.get(obj_row["device_id"])
            if (
                dev
                and dev.get("simulation_mode", "simulation") != "mirror"
                and obj_row.get("source_object_id") is None
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

        # Then run FMU/AI providers in registration order. Inputs
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

        for obj_id, runtime_point in self._runtime_points.items():
            obj_row = runtime_rows.get(obj_id)
            if not obj_row:
                continue
            dev = dev_map.get(obj_row["device_id"])
            if not dev:
                continue
            sim_mode = dev.get("simulation_mode", "simulation")
            is_linked = obj_row.get("source_object_id") is not None
            if sim_mode in ("mirror", "replay") or is_linked:
                # Behaviors stay stored but inactive; mirror_sync_loop drives
                # the value out of band -- either the whole-device Mirror
                # path (mirror), replay_playback_loop (replay), or the
                # per-object link path (_link_sync_once, is_linked) added
                # alongside it. is_linked is independent of the owning
                # device's own simulation_mode -- an ordinary 'simulation'
                # device can freely mix normal points and linked points.
                injected_val = (
                    self._linked_values.get(obj_id) if is_linked
                    else self._mirror_values.get(obj_id) if sim_mode == "mirror"
                    else self._replay_values.get(obj_id)
                )
                payload = self._twin_snapshot_payload(
                    obj_id,
                    obj_row,
                    injected_val,
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
                # The provider (FMU/AI model) remains the source of
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
            # written_val is what write_point() actually put on the wire (post
            # type-coercion/clamping -- e.g. a multi-state object's [1, numberOfStates]
            # clamp). Used below only for /sim/state (_prev_values) and the Admin UI's live
            # "value" field, so those can never disagree with a real client's read.
            # Alarm/enrollment evaluation, trend logging, and history deliberately keep using
            # the pre-clamp `val` -- unchanged behavior, since clamping is a no-op for every
            # object type except multi-state.
            #
            # Offline is a protocol-status fault, not a value fault (see behaviors.py's
            # FaultBehavior class docstring) -- write_point() folds quality (derived from
            # whether an "offline" fault is CURRENTLY active on this point, via
            # _is_offline_fault_active()) into the same call that writes Present_Value,
            # letting each adapter translate it into whatever its own protocol calls
            # "unreliable" (BACnet: Reliability + Status_Flags.fault, via the same
            # _apply_reliability helper _create_object() already uses once, at creation, for
            # an object's own persisted `reliability` field -- see
            # BacnetProtocolAdapter.write_point()). Idempotent when nothing changed, and what
            # makes recovery, switching to a different fault_type, and Behavior != "fault" all
            # self-correct for free: none of those is "offline right now", so the adapter
            # always falls through to restoring the point's own configured reliability, with
            # no separate offline-specific cleanup/state needed. `runtime_point.behavior`
            # (the transitional field -- see RuntimePoint's own docstring) is NOT used here
            # for a builtin-owned point -- it's the instance _create_object() built once and
            # is never the live one BuiltInSimulationProvider actually ticks (see that dict's
            # own docstring); get_behavior() reaches the real one.
            live_behavior = (
                self._builtin_provider.get_behavior(obj_id) if owner == "builtin"
                else self._point_behaviors.get(obj_id)
            )
            quality = (
                PointQuality.OFFLINE if _is_offline_fault_active(live_behavior)
                else PointQuality.GOOD
            )
            written_val = runtime_point.adapter.write_point(
                runtime_point.handle, PointValue(value=val, quality=quality)
            )

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

            self._prev_values[obj_id] = written_val

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
                "value": written_val,
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
            runtime_point = self._runtime_points.get(obj_id)
            if not runtime_point:
                continue
            normalized = normalize_present_value(obj["object_type"], val)
            # written_val: see the tick loop's identical comment above --
            # what actually landed on the wire, used for /sim/state so it
            # can't disagree with a real BACnet client's read even if the
            # mirrored source and this object's own config disagree (e.g.
            # a numberOfStates mismatch). _mirror_values / the snapshot
            # deliberately keep the un-clamped `normalized` value -- that's
            # "what the mirrored source reported", a distinct diagnostic
            # concept from "what this object's Present_Value now holds".
            written_val = runtime_point.adapter.write_point(
                runtime_point.handle, PointValue(value=normalized)
            )
            self._mirror_values[obj_id] = normalized
            self._prev_values[obj_id] = written_val
            if dev is not None:
                self._upsert_current_value_snapshot(dev, obj, normalized)

    async def inject_linked_value(self, object_id: int, value: Any) -> None:
        """Per-object sibling of inject_mirror_values -- propagates one
        already-resolved value (from either a simulated source's own
        get_object_value() or an external-bacnet ReadProperty) into a
        single linked object. Called only by _link_sync_once (runtime.py) --
        never from the HTTP layer. `value` is expected already normalized
        for a simulated source (get_object_value returns what a prior tick
        already wrote); an external-bacnet source's raw read is normalized
        here exactly like inject_mirror_values does for its own source."""
        obj = await asyncio.to_thread(self.db.get_object, object_id)
        if obj is None:
            return
        runtime_point = self._runtime_points.get(object_id)
        if not runtime_point:
            return
        normalized = normalize_present_value(obj["object_type"], value)
        written_val = runtime_point.adapter.write_point(
            runtime_point.handle, PointValue(value=normalized)
        )
        self._linked_values[object_id] = normalized
        self._prev_values[object_id] = written_val
        dev = await asyncio.to_thread(self.db.get_device, obj["device_id"])
        if dev is not None:
            self._upsert_current_value_snapshot(dev, obj, normalized)

    async def inject_replay_values(
        self,
        device_id: int,
        values: dict[tuple[str, int], Any],
        objects: list[dict],
    ) -> None:
        """Propagate one recorded snapshot's present-values into a Replay
        simulated device. Called only by advance_replay_playback -- never
        from the HTTP layer. Mirrors inject_mirror_values exactly, writing
        into the separate _replay_values cache instead of _mirror_values
        (see that field's own comment)."""
        dev = await asyncio.to_thread(self.db.get_device, device_id)
        for obj in objects:
            key = (obj["object_type"], obj["object_instance"])
            if key not in values:
                continue
            val = values[key]
            if val is None:
                continue
            obj_id = obj["id"]
            runtime_point = self._runtime_points.get(obj_id)
            if not runtime_point:
                continue
            normalized = normalize_present_value(obj["object_type"], val)
            written_val = runtime_point.adapter.write_point(
                runtime_point.handle, PointValue(value=normalized)
            )
            self._replay_values[obj_id] = normalized
            self._prev_values[obj_id] = written_val
            if dev is not None:
                self._upsert_current_value_snapshot(dev, obj, normalized)

    # ─── Replay playback transport ──────────────────────────────────────────
    # In-memory only (see _replay_playback's own comment). Pause holds the
    # current sample; explicit Stop resets to the recording's first sample;
    # reaching the end naturally (no loop) holds the last sample instead of
    # resetting -- see advance_replay_playback for where that distinction is
    # actually applied.

    def _get_replay_state(self, device_id: int) -> dict[str, Any]:
        state = self._replay_playback.get(device_id)
        if state is None:
            state = {
                "status": "stopped",
                "current_sample_index": 0,
                "speed": 1.0,
                "loop": False,
                "last_advance_wall_time": None,
            }
            self._replay_playback[device_id] = state
        return state

    def get_replay_state(self, device_id: int) -> dict[str, Any]:
        return dict(self._get_replay_state(device_id))

    def replay_play(self, device_id: int) -> dict[str, Any]:
        state = self._get_replay_state(device_id)
        state["status"] = "playing"
        state["last_advance_wall_time"] = time.monotonic()
        return state

    def replay_pause(self, device_id: int) -> dict[str, Any]:
        state = self._get_replay_state(device_id)
        state["status"] = "paused"
        return state

    async def replay_stop(self, device_id: int) -> dict[str, Any]:
        """Unlike Pause (holds position) or a natural end (holds the last
        sample), an explicit Stop resets back to the recording's actual
        first sample_index -- not literally 0, since buffer_mode='overwrite'
        eviction can leave a recording's stored range starting above 0."""
        state = self._get_replay_state(device_id)
        state["status"] = "stopped"
        first_index = 0
        device = await asyncio.to_thread(self.db.get_device, device_id)
        recording_id = device.get("active_recording_id") if device else None
        if recording_id is not None:
            bounds = await asyncio.to_thread(
                self.db.get_recording_sample_index_bounds, recording_id
            )
            if bounds is not None:
                first_index = bounds[0]
        state["current_sample_index"] = first_index
        return state

    def replay_seek(self, device_id: int, sample_index: int) -> dict[str, Any]:
        state = self._get_replay_state(device_id)
        state["current_sample_index"] = max(0, sample_index)
        return state

    def replay_set_loop(self, device_id: int, loop: bool) -> dict[str, Any]:
        state = self._get_replay_state(device_id)
        state["loop"] = loop
        return state

    def replay_set_speed(self, device_id: int, speed: float) -> dict[str, Any]:
        state = self._get_replay_state(device_id)
        state["speed"] = speed
        return state

    async def advance_replay_playback(self, device: dict) -> None:
        """Called once per REPLAY_PLAYBACK_POLL_SECONDS (see
        replay_playback_loop) for every simulation_mode='replay' device.
        Advances playback position when playing (Polled recordings are
        evenly spaced, so sample_interval_seconds/speed IS "the recorded
        timing between samples" -- no per-frame timestamp lookup needed),
        and re-injects values whenever the current position doesn't match
        what's actually applied yet (covers Play advancing AND a Seek that
        happened while paused/stopped)."""
        device_id = device["id"]
        recording_id = device.get("active_recording_id")
        if recording_id is None:
            return
        recording = await asyncio.to_thread(self.db.get_recording, recording_id)
        bounds = await asyncio.to_thread(
            self.db.get_recording_sample_index_bounds, recording_id
        )
        if not recording or bounds is None:
            return
        min_index, max_index = bounds
        state = self._get_replay_state(device_id)
        if state["current_sample_index"] < min_index or state["current_sample_index"] > max_index:
            state["current_sample_index"] = min_index

        if state["status"] == "playing":
            interval = max(0.01, recording["sample_interval_seconds"]) / max(0.01, state["speed"])
            last = state["last_advance_wall_time"] or time.monotonic()
            elapsed = time.monotonic() - last
            steps = int(elapsed // interval)
            if steps > 0:
                state["last_advance_wall_time"] = last + steps * interval
                for _ in range(steps):
                    if state["current_sample_index"] >= max_index:
                        if state["loop"]:
                            state["current_sample_index"] = min_index
                        else:
                            # Natural end: hold the last sample, distinct
                            # from an explicit Stop's reset-to-first.
                            state["status"] = "stopped"
                            break
                    else:
                        state["current_sample_index"] += 1

        if self._replay_applied_index.get(device_id) == state["current_sample_index"]:
            return

        point_lookup = {p["id"]: p for p in recording["points"]}
        samples = await asyncio.to_thread(
            self.db.get_recording_samples, recording_id, state["current_sample_index"]
        )
        values: dict[tuple[str, int], Any] = {}
        for s in samples:
            p = point_lookup.get(s["recording_point_id"])
            if not p:
                continue
            try:
                values[(p["object_type"], p["object_instance"])] = json.loads(s["value"])
            except (TypeError, json.JSONDecodeError):
                continue
        objects = await asyncio.to_thread(self.db.get_objects, device_id)
        await self.inject_replay_values(device_id, values, objects)
        self._replay_applied_index[device_id] = state["current_sample_index"]

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

    async def _stop_active_protocol_adapters(self) -> None:
        for protocol in list(self._active_adapters):
            adapter = self.get_protocol_adapter(protocol)
            for runtime_point in list(self._runtime_points.values()):
                if runtime_point.adapter is adapter:
                    try:
                        adapter.remove_point(runtime_point.handle)
                    except Exception:
                        pass
            for device_handle in self._adapter_device_handles.get(protocol, []):
                try:
                    adapter.remove_device(device_handle)
                except Exception:
                    pass
            try:
                await adapter.stop()
            except Exception:
                pass

        self._runtime_points.clear()
        self._active_adapters.clear()
        self._adapter_device_handles.clear()

    async def reload(self) -> None:
        """Rebuild the BACnet stack from DB (called after config changes).

        Registered FMU/AI providers are NOT re-registered here (see
        start()'s own comment) -- only the BACnet objects/_prev_values get
        torn down and rebuilt. Snapshot _prev_values first so
        _create_object() can restore each provider-owned raw/constant
        point's actual live value instead of re-seeding it from
        behavior_params, which is often empty/stale for a point whose
        value has only ever come from the provider (never a real
        configured default) -- see _create_object()'s own comment for the
        incident this fixes (an unrelated object edit anywhere triggers
        this same reload, and used to zero a provider-owned point's value,
        which could self-latch off forever for a point that's also one of
        its own model's inputs).
        """
        self._reload_preserved_values = dict(self._prev_values)
        async with self._reload_lock:
            log.info("Reloading protocol runtimes...")
            await self._stop_active_protocol_adapters()
            self._prev_values.clear()
            self._history.clear()
            self._alarm_runtime.clear()
            self._enrollment_runtime.clear()
            self._trend_log_last_value.clear()
            self._current_values = {}

            await self.start()
            log.info("Reload complete")

    async def stop(self) -> None:
        """Cleanly shut down providers and every protocol runtime."""
        for provider_id, provider in list(self._providers.items()):
            try:
                provider.stop()
            except Exception:
                log.exception("Failed to stop simulation provider %s", provider_id)

        await self._stop_active_protocol_adapters()
        log.info("Protocol runtimes stopped")

    async def add_object_hot(self, device_instance: int, obj_row: dict) -> None:
        """Hot-add a single object to the running BACnet app without full reload.

        Takes _reload_lock -- the same lock reload() holds for its own
        teardown/rebuild of self.app/self._runtime_points/self._device_slots (see
        _reload_lock's own comment). Without this, a hot-add racing a
        concurrent reload() (routine: creating a device schedules a
        fire-and-forget reload(), and each object POSTed onto that device
        right after schedules its own fire-and-forget hot-add -- exactly
        what cloning a multi-point device does, back-to-back) could land
        while reload() is mid-teardown/rebuild and get silently discarded:
        confirmed live where an 11-object clone ended up with only the
        first 3 objects actually registered in self._runtime_points, the rest
        present in the DB but never reachable on the BACnet wire or in any
        snapshot/injection path. Queuing behind the lock instead of racing
        it makes the hot-add always land after any in-flight reload's
        rebuild has settled."""
        if not self.app:
            return
        async with self._reload_lock:
            adapter = self.get_protocol_adapter("bacnet")
            slot = self._device_slots.get(device_instance, 0)
            dev_obj = self.app._virtual_devices.get(device_instance)
            dev_name = str(dev_obj.objectName) if dev_obj else ""
            bacnet_obj, behavior = object_factory.create_object(self, obj_row, slot, dev_name)
            self.app.add_object(bacnet_obj)
            self._runtime_points[obj_row["id"]] = RuntimePoint(
                config=self._point_runtime_config(obj_row),
                adapter=adapter,
                handle=bacnet_obj,
                behavior=behavior,
            )
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
        runtime_point = self._runtime_points.get(obj_id)
        if runtime_point is None:
            return False
        if isinstance(runtime_point.behavior, ManualBehavior):
            runtime_point.behavior.set(value)
            active_behavior = runtime_point.behavior
        else:
            # Keep the RuntimePoint.behavior swap exactly as today; provider is
            # the authoritative behavior executor.
            new_b = ManualBehavior({"value": value})
            runtime_point.behavior = new_b
            active_behavior = new_b

        obj_row = self.db.get_object(obj_id)
        if obj_row:
            manual_cfg = PointConfig(
                point_id=obj_id,
                behavior="manual",
                behavior_params=json.dumps({"value": value}),
                data_type=self._point_data_type_from_bacnet_object_type(str(obj_row["object_type"])),
                manual_value=value,
            )
            if obj_id not in self._point_output_owner:
                self._builtin_provider.sync_point_config(manual_cfg)
                self._builtin_provider.set_inputs({obj_id: value})
            # _update_value()'s return is the exact value it wrote to the
            # live BACnet presentValue (post type-coercion/clamping, e.g.
            # a multi-state object's [1, numberOfStates] clamp) --
            # get_object_value() (what Functional Test Verify/Wait Until
            # steps, the Energy Engine, etc. actually read) reads
            # self._prev_values, which is otherwise only refreshed by the
            # periodic tick loop. Without updating it here, a manual
            # override written and immediately read back (e.g. a Set block
            # followed straight by a Verify, no Wait in between) sees the
            # STALE pre-write value until the next tick happens to run --
            # this was the actual cause of a Set-then-Verify reporting the
            # point's OLD state instead of what was just written. Using
            # _update_value's own return (rather than separately
            # recomputing normalize_present_value(active_behavior.compute
            # (None))) also fixes a second, narrower bug: normalize_
            # present_value() is a no-op for multi-state types, so the old
            # code could leave _prev_values holding a raw out-of-range
            # value (e.g. 0) that Present_Value's own clamp would never
            # actually allow on the wire.
            self._prev_values[obj_id] = runtime_point.adapter.write_point(
                runtime_point.handle, PointValue(value=value)
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
        runtime_point = self._runtime_points.get(obj_id)
        if runtime_point is None:
            return False
        bacnet_obj = runtime_point.handle
        obj_row = await asyncio.to_thread(self.db.get_object, obj_id)
        if not obj_row:
            return False
        await asyncio.to_thread(self.db.write_object, obj_id, value)
        new_b = ManualBehavior({"value": value})
        runtime_point.behavior = new_b

        # Re-read the persisted row so provider config matches DB exactly.
        updated_row = await asyncio.to_thread(self.db.get_object, obj_id)
        if updated_row and obj_id not in self._point_output_owner:
            self._builtin_provider.sync_point_config(self._point_config_from_row(updated_row))
            self._builtin_provider.set_inputs({obj_id: value})

        self._prev_values[obj_id] = runtime_point.adapter.write_point(
            bacnet_obj,
            PointValue(value=value),
        )
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
        runtime_point = self._runtime_points.get(obj_id)
        if runtime_point is None:
            return None
        bacnet_obj = runtime_point.handle
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
        runtime_point = self._runtime_points.get(obj_id)
        if runtime_point is None:
            return False
        bacnet_obj = runtime_point.handle
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
        for did, runtime_point in self._runtime_points.items():
            if runtime_point.handle is bacnet_obj:
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

