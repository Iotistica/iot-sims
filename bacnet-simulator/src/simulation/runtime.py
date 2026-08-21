"""Background simulation tasks and FastAPI lifespan.

Physically extracted from src/legacy.py's tick_loop/mirror_sync_loop/
simulation_recovery_loop/metrics_loop/lifespan -- continuing the GH #15
refactor, same "moved verbatim, no behavior changes" standard as the
Database, SimEngine, and SimApplication extractions. legacy.py itself is
now gone; `db`/`engine`/the cadence settings/`_apply_settings_live` live in
src/dependencies.py (see that module's own docstring for why -- it stays
import-light specifically so this module, and everything else that reads
this state, can import it directly rather than through app.state).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from .. import dependencies
from ..core.config import DB_PATH, SIM_API_PORT
from ..core.security import (
    create_access_token, get_current_user, hash_password, user_from_token, verify_password,
)
from ..db import Database
from ..energy import EnergyEngine
from ..fault_detection import FaultDetectionEngine, build_default_registry
from ..monitoring.broadcasters import broadcast_state
from ..monitoring.event_log import (
    _device_logs, _device_names, _global_log, _log_event,
    get_device_log_entries, get_global_log_entries,
)
from ..monitoring.metrics import broadcast_metrics, build_metrics_snapshot
from .engine import SimEngine
from .model_runtime import recover_unhealthy_simulation_models

log = logging.getLogger("bacnet-sim")


# ─── Background tasks ─────────────────────────────────────────────────────────

async def tick_loop(fault_detection_engine: FaultDetectionEngine | None,
    energy_engine: EnergyEngine | None,) -> None:
    while True:
        await asyncio.sleep(dependencies.TICK_SECONDS)
        try:
            await dependencies.engine.tick()

            # engine.tick() already no-ops while paused/stopped (present
            # values stay frozen) -- fault/energy evaluation must respect
            # the same gate, otherwise energy totals keep accumulating
            # kWh for elapsed time that, per the frozen clock, never
            # actually passed.
            if dependencies.engine.clock_state == "running":
                if fault_detection_engine is not None:
                    await fault_detection_engine.evaluate_all()

                if energy_engine is not None:
                    await energy_engine.evaluate_all(
                        elapsed_seconds=dependencies.TICK_SECONDS,
                    )

            await broadcast_state()
            state = dependencies.engine.get_state()
            for dev in state.get("devices", []):
                vals = "  ".join(
                    f"{o['name']}={o['value']:.2f}" if isinstance(o["value"], float) else f"{o['name']}={o['value']}"
                    for o in dev["objects"]
                )
                log.info("[%s]  %s", dev["name"], vals)
        except Exception as e:
            log.error("Tick error: %s", e)


async def mirror_sync_loop() -> None:
    """Reads present-values from source external devices and propagates them
    into all linked Mirror simulated devices every MIRROR_POLL_SECONDS.
    Groups reads by source device so each physical device is polled once per
    cycle regardless of how many Mirror copies reference it."""
    # Deferred to avoid circular import: routers import from this module
    # (for `lifespan`, via application.py) at module level; this module
    # must not import from routers at module level.
    from ..api.routers.discovery import _discovery_session  # noqa: PLC0415
    while True:
        await asyncio.sleep(dependencies.MIRROR_POLL_SECONDS)
        try:
            await _mirror_sync_once(dependencies.engine, _discovery_session)
        except Exception as exc:
            log.error("Mirror sync error: %s", exc)


async def _mirror_sync_once(sim_engine: SimEngine, discovery_session_ctx: Any) -> None:
    devices = await asyncio.to_thread(sim_engine.db.get_devices)
    dev_map = {d["id"]: d for d in devices}
    mirror_devices = [
        d for d in devices
        if d.get("simulation_mode") == "mirror"
        and d.get("source_device_id") is not None
        and d.get("source_type", "simulated") == "simulated"
        and d.get("enabled")
    ]
    if not mirror_devices:
        return

    # Group by source device to issue one BACnet read per unique source.
    from collections import defaultdict  # noqa: PLC0415
    source_to_mirrors: dict[int, list[dict]] = defaultdict(list)
    for m in mirror_devices:
        source_to_mirrors[m["source_device_id"]].append(m)

    for src_dev_id, linked_mirrors in source_to_mirrors.items():
        src_dev = dev_map.get(src_dev_id)
        if not src_dev or src_dev.get("source_type") != "external-bacnet":
            continue
        host = src_dev.get("external_host")
        if not host:
            continue

        # Collect the union of all object (type, instance) pairs across
        # all linked mirrors; they should be identical (all are copies of
        # the same source) but reading the union is safe and defensive.
        mirrors_with_objects: list[tuple[dict, list[dict]]] = []
        all_points: set[tuple[str, int]] = set()
        for mirror_dev in linked_mirrors:
            objs = await asyncio.to_thread(sim_engine.db.get_objects, mirror_dev["id"])
            if objs:
                mirrors_with_objects.append((mirror_dev, objs))
                all_points.update((o["object_type"], o["object_instance"]) for o in objs)
        if not all_points:
            continue

        try:
            async with discovery_session_ctx() as discovery:
                values = await discovery.read_present_values(
                    host,
                    src_dev["device_instance"],
                    list(all_points),
                )
        except Exception:
            # Source unavailable -- retain last _mirror_values; no behaviors start.
            continue

        await asyncio.to_thread(sim_engine.db.touch_external_device_last_seen, src_dev_id)
        for mirror_dev, mirror_objects in mirrors_with_objects:
            await sim_engine.inject_mirror_values(mirror_dev["id"], values, mirror_objects)


# Guards simulation_recovery_loop() against overlapping sweeps -- if a cycle
# is still running (e.g. several models each timing out before the sweep's
# own health() probe short-circuits) when the next SIMULATION_RECOVERY_SECONDS
# fires, skip that cycle instead of running two sweeps concurrently. Separate
# from SimEngine._simulation_registry_lock, which route handlers must be
# able to block on -- this one is deliberately non-blocking.
_simulation_recovery_sweep_lock = threading.Lock()


async def simulation_recovery_loop() -> None:
    """Self-heals FMU simulation model sessions without requiring a human to
    click Apply: every SIMULATION_RECOVERY_SECONDS, reload any enabled FMU
    model config whose runtime registration is missing or in ERROR status
    (session lost to an FMU runtime restart, or never established because a
    configured Point input had no live value yet -- see
    FMUInputResolutionError in simulation/providers/fmu.py). Runs its
    blocking body via asyncio.to_thread so a slow/unreachable FMU runtime
    never stalls tick_loop/mirror_sync_loop/websocket broadcasting.
    """
    while True:
        await asyncio.sleep(dependencies.SIMULATION_RECOVERY_SECONDS)
        if not _simulation_recovery_sweep_lock.acquire(blocking=False):
            log.debug(
                "Simulation recovery sweep still in progress; skipping this cycle"
            )
            continue
        try:
            result = await asyncio.to_thread(
                recover_unhealthy_simulation_models, dependencies.db, dependencies.engine
            )
            if result["recovered"] or result["errors"] or result["runtime_unreachable"]:
                log.info(
                    "Simulation recovery sweep: recovered=%s errors=%s "
                    "runtime_unreachable=%s",
                    result["recovered"],
                    result["errors"],
                    result["runtime_unreachable"],
                )
        except Exception as exc:
            log.error("Simulation recovery sweep error: %s", exc)
        finally:
            _simulation_recovery_sweep_lock.release()


async def metrics_loop() -> None:
    # Deliberately independent of TICK_SECONDS/tick_loop — device-value
    # simulation and analytics refresh are different concerns with different
    # natural cadences (5s vs 1s), and coupling them would mean either
    # slowing down analytics or speeding up (and adding load to) the actual
    # device simulation just to serve the dashboard.
    while True:
        await asyncio.sleep(1.0)
        try:
            await broadcast_metrics()
        except Exception as e:
            log.error("Metrics tick error: %s", e)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    dependencies.db = Database(DB_PATH)
    await asyncio.to_thread(dependencies.db.setup)
    await asyncio.to_thread(dependencies.db.seed_default)
    for d in dependencies.db.get_devices():
        _device_names[d["id"]] = d["name"]
    dependencies.engine = SimEngine(dependencies.db)

     # Expose shared runtime objects to extracted routers.
    app.state.db = dependencies.db
    app.state.engine = dependencies.engine
    app.state.packet_capture = dependencies.packet_capture


    app.state.get_current_user = get_current_user
    app.state.log_event = _log_event

    app.state.device_names = _device_names
    app.state.effective_can_receive_events = (
        dependencies._effective_can_receive_events
    )

    app.state.build_metrics_snapshot = (
    build_metrics_snapshot
    )

    app.state.user_from_token = user_from_token
    app.state.hash_password = hash_password
    app.state.verify_password = verify_password
    app.state.create_access_token = create_access_token

    app.state.device_logs = _device_logs
    app.state.global_log = _global_log
    app.state.get_device_logs = (
    get_device_log_entries
    )
    app.state.get_global_logs = (
        get_global_log_entries
    )

    app.state.ws_clients = dependencies.ws_clients
    app.state.metrics_ws_clients = dependencies.metrics_ws_clients
    app.state.packet_stream_ws_clients = dependencies.packet_stream_ws_clients


    fault_detection_engine = FaultDetectionEngine(
        database=dependencies.db,
        simulation_engine=dependencies.engine,
        registry=build_default_registry(),
        event_callback=_log_event,
    )

    energy_engine = EnergyEngine(
        database=dependencies.db,
        simulation_engine=dependencies.engine,
        event_callback=_log_event,
        history_interval_seconds=60.0,
        history_retention_days=7,
        audit_log_interval_seconds=60.0,
    )

    app.state.energy_engine = energy_engine

    app.state.fault_detection_engine = fault_detection_engine




    dependencies._apply_settings_live(await asyncio.to_thread(dependencies.db.get_settings))
    await dependencies.engine.start()

    # Restore persisted FMU/Learned model registrations only after the
    # BACnet runtime exists. Built-in remains the default/fallback provider.
    # Deferred to avoid circular import -- same reasoning as
    # mirror_sync_loop's deferred discovery import above.
    from ..api.routers.simulation import bootstrap_simulation_models  # noqa: PLC0415
    model_bootstrap = await bootstrap_simulation_models(app)

    if model_bootstrap.get("errors"):
        log.warning(
            "Simulation model bootstrap completed with errors: %s",
            model_bootstrap["errors"],
        )
    else:
        log.info(
            "Simulation model bootstrap loaded %d provider(s)",
            len(model_bootstrap.get("loaded", [])),
        )

    tick_task = asyncio.create_task(tick_loop(fault_detection_engine,energy_engine))
    mirror_task = asyncio.create_task(mirror_sync_loop())
    metrics_task = asyncio.create_task(metrics_loop())
    simulation_recovery_task = asyncio.create_task(simulation_recovery_loop())
    log.info("BACnet Simulator API ready on port %d", SIM_API_PORT)
    yield
    log.info("Shutting down")
    tick_task.cancel()
    mirror_task.cancel()
    metrics_task.cancel()
    simulation_recovery_task.cancel()
    try:
        await tick_task
    except asyncio.CancelledError:
        pass
    await dependencies.engine.stop()
