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
import time
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
from .models.runtime import recover_unhealthy_simulation_models

log = logging.getLogger("bacnet-sim")


# ─── Background tasks ─────────────────────────────────────────────────────────

async def tick_loop(fault_detection_engine: FaultDetectionEngine | None,
    energy_engine: EnergyEngine | None,) -> None:
    """Runs one simulation tick per iteration, targeting TICK_SECONDS of
    real-world cadence -- self-correcting (accounts for how long the
    previous tick actually took) rather than a plain sleep-then-tick loop,
    which would drift by the full overrun every time a tick ran long (an
    FMU-backed device's step() is a real blocking HTTP round trip; see
    SimEngine._step_one_provider's own per-provider timing/warning).
    TICK_SECONDS itself always represents exactly that much *simulated*
    time per tick (SimEngine.tick()'s own state.elapsed_seconds +=
    TICK_SECONDS runs unconditionally, unaffected by how long the tick
    actually took to compute) -- this loop only adjusts real-world
    scheduling, never simulated-time accounting, and energy integration
    below still always uses the fixed TICK_SECONDS timestep, not a
    measured wall-clock delta.

    Ticks never overlap: this is a single sequential loop that always
    awaits one tick() fully (and everything it transitively awaits,
    including every provider's step() within _run_registered_providers)
    before starting the next -- there is no code path that could start
    tick N+1 while tick N, or any one provider's step within it, is still
    in flight.
    """
    next_tick_at = time.monotonic() + dependencies.TICK_SECONDS
    while True:
        delay = next_tick_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            log.warning(
                "Simulation running behind schedule by %.2fs; starting next tick immediately",
                -delay,
            )

        tick_started_at = time.monotonic()
        try:
            await dependencies.engine.tick()

            # engine.tick() already no-ops while paused/stopped (present
            # values stay frozen) -- fault/energy evaluation and sampling
            # of simulated-device recordings must respect the same gate,
            # otherwise energy totals keep accumulating kWh (and
            # recordings keep taking samples) for elapsed time that, per
            # the frozen clock, never actually passed.
            if dependencies.engine.clock_state == "running":
                if fault_detection_engine is not None:
                    await fault_detection_engine.evaluate_all()

                if energy_engine is not None:
                    await energy_engine.evaluate_all(
                        elapsed_seconds=dependencies.TICK_SECONDS,
                    )

                await _sample_due_simulated_recordings(dependencies.db, dependencies.engine)

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

        tick_duration = time.monotonic() - tick_started_at
        if tick_duration > dependencies.TICK_SECONDS:
            log.warning(
                "Tick took %.2fs, exceeding TICK_SECONDS=%.2fs",
                tick_duration,
                dependencies.TICK_SECONDS,
            )
        next_tick_at += dependencies.TICK_SECONDS


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


# recording_id -> last-sampled wall-clock time (time.monotonic()). In-memory
# only, like _mirror_values -- transient process state, no DB column, reset
# on restart (a recording just gets sampled again on the next due tick).
# External-BACnet-device recordings only -- see _replay_last_sampled_sim_time
# for simulated/mirror/replay-device recordings, which are sampled on
# simulated time (tick_loop-triggered) instead of wall-clock time.
_replay_last_sampled: dict[int, float] = {}


def _next_replay_recording_sleep_seconds(database: Any) -> float:
    """How long replay_recording_loop should sleep before its next wake-up
    -- NOT a fixed poll cadence. Computed from how soon the earliest active
    recording's own sample_interval_seconds comes due, so a 1s-interval
    recording gets sampled close to every second while a 60s-interval one
    doesn't cause 59 wasted wake-ups in between. REPLAY_RECORDING_IDLE_CEILING_SECONDS
    only matters when nothing is currently due (including "no active
    recordings at all"), so a newly-started recording is still picked up
    promptly.

    Deliberately still considers every "recording"-status row, including
    simulated-device ones this loop no longer actually samples (see
    _replay_recording_sample_once) -- filtering those out here would need
    an extra device lookup per recording in a function that's otherwise a
    single cheap in-memory pass; the cost of the occasional wasted wake-up
    is negligible next to that."""
    recordings = [r for r in database.get_replay_recordings() if r["status"] == "recording"]
    if not recordings:
        return dependencies.REPLAY_RECORDING_IDLE_CEILING_SECONDS
    now = time.monotonic()
    due_in = min(
        max(0.0, r["sample_interval_seconds"] - (now - _replay_last_sampled.get(r["id"], 0.0)))
        for r in recordings
    )
    return max(0.05, min(due_in, dependencies.REPLAY_RECORDING_IDLE_CEILING_SECONDS))


async def replay_recording_loop() -> None:
    """Samples every due Replay Recording's external source device. A
    single shared task (not one per recording), woken based on the next
    recording that's actually due rather than a fixed short poll interval
    -- see _next_replay_recording_sleep_seconds."""
    from ..api.routers.discovery import _discovery_session  # noqa: PLC0415
    while True:
        await asyncio.sleep(_next_replay_recording_sleep_seconds(dependencies.db))
        try:
            await _replay_recording_sample_once(dependencies.db, _discovery_session)
        except Exception as exc:
            log.error("Replay recording sample error: %s", exc)


async def _replay_recording_sample_once(database: Any, discovery_session_ctx: Any) -> None:
    """External-BACnet-device recordings only, sampled on real wall-clock
    time -- those devices exist in real time regardless of this
    simulator's own tick cadence. Simulated/mirror/replay-device recordings
    are sampled from tick_loop instead (see _sample_due_simulated_recordings),
    tied to simulation state actually advancing rather than an independent
    wall-clock poll -- polling on wall-clock time alone could re-read the
    same not-yet-updated cached value multiple times while a slow tick is
    still in flight (confirmed live: Replay Recording exports showed many
    consecutive identical samples once individual FMU ticks started taking
    longer than a recording's own sample_interval_seconds)."""
    now = time.monotonic()
    due = [
        r for r in database.get_replay_recordings()
        if r["status"] == "recording"
        and now - _replay_last_sampled.get(r["id"], 0.0) >= r["sample_interval_seconds"]
    ]
    for recording in due:
        device = await asyncio.to_thread(database.get_device, recording["source_device_id"])
        if not device or device.get("source_type") != "external-bacnet" or not device.get("external_host"):
            # Not this loop's concern (simulated/mirror/replay device, or a
            # misconfigured external one) -- mark sampled so it doesn't
            # stay "due" every cycle; _sample_due_simulated_recordings
            # handles the simulated case from tick_loop instead.
            _replay_last_sampled[recording["id"]] = now
            continue

        detail = await asyncio.to_thread(database.get_replay_recording, recording["id"])
        points = detail["points"] if detail else []
        if not points:
            _replay_last_sampled[recording["id"]] = now
            continue

        try:
            async with discovery_session_ctx() as discovery:
                values = await discovery.read_present_values(
                    device["external_host"],
                    device["device_instance"],
                    [(p["object_type"], p["object_instance"]) for p in points],
                )
        except Exception:
            # Source unavailable this cycle -- try again next time it's due.
            _replay_last_sampled[recording["id"]] = now
            continue

        sample_values = {
            p["id"]: {"value": values.get((p["object_type"], p["object_instance"]))}
            for p in points
        }

        await asyncio.to_thread(database.add_replay_sample, recording["id"], sample_values)
        _replay_last_sampled[recording["id"]] = now


# recording_id -> last-sampled *simulated* time (SimEngine.state.elapsed_seconds)
# at which a simulated/mirror/replay-device recording was last sampled.
# Separate from _replay_last_sampled (wall-clock, external-device only) --
# `None`/absent means "not yet sampled", always due immediately (matches
# "Start Recording starts immediately").
_replay_last_sampled_sim_time: dict[int, float] = {}


async def _sample_due_simulated_recordings(database: Any, engine: Any) -> None:
    """Samples every active recording on a simulated/mirror/replay device
    whose configured sample_interval_seconds is due in *simulated* time.
    Called from tick_loop right after engine.tick() completes (and only
    while the simulation clock is actually running), so every sample
    reflects a fully-computed tick's fresh values rather than a value that
    just hasn't updated yet -- unlike wall-clock polling, this can never
    read the same tick's output twice, since it only ever runs once per
    completed tick and only advances _replay_last_sampled_sim_time using
    the simulated clock (SimEngine.state.elapsed_seconds), which itself
    only moves forward once per completed tick.

    A recording's own sample_interval_seconds finer than TICK_SECONDS
    degrades gracefully to "once per tick" -- a simulated device's value
    cannot change faster than the tick cadence, so there is nothing finer
    to sample even if asked for it.

    External-BACnet-device recordings are untouched by this function --
    see _replay_recording_sample_once for those."""
    elapsed = engine.state.elapsed_seconds
    recordings = await asyncio.to_thread(database.get_replay_recordings)
    for recording in recordings:
        if recording["status"] != "recording":
            continue
        last_sim_time = _replay_last_sampled_sim_time.get(recording["id"])
        if last_sim_time is not None and elapsed - last_sim_time < recording["sample_interval_seconds"]:
            continue

        device = await asyncio.to_thread(database.get_device, recording["source_device_id"])
        if not device or device.get("source_type") == "external-bacnet":
            continue  # not this function's concern -- see _replay_recording_sample_once

        detail = await asyncio.to_thread(database.get_replay_recording, recording["id"])
        points = detail["points"] if detail else []
        sample_values = {
            p["id"]: {"value": engine.get_object_value(p["source_object_id"])}
            for p in points
            if p.get("source_object_id") is not None
        }
        if not sample_values:
            _replay_last_sampled_sim_time[recording["id"]] = elapsed
            continue

        await asyncio.to_thread(database.add_replay_sample, recording["id"], sample_values)
        _replay_last_sampled_sim_time[recording["id"]] = elapsed


async def replay_playback_loop() -> None:
    """Advances every simulation_mode='replay' device's playback position
    (see SimEngine.advance_replay_playback) -- runs at a fixed cadence
    independent of TICK_SECONDS/MIRROR_POLL_SECONDS, since a recording's own
    sample_interval_seconds and playback speed can both be much finer than
    the 5s tick."""
    while True:
        await asyncio.sleep(dependencies.REPLAY_PLAYBACK_POLL_SECONDS)
        try:
            await _advance_replay_playback_once(dependencies.engine)
        except Exception as exc:
            log.error("Replay playback error: %s", exc)


async def _advance_replay_playback_once(sim_engine: SimEngine) -> None:
    devices = await asyncio.to_thread(sim_engine.db.get_devices)
    replay_devices = [
        d for d in devices
        if d.get("simulation_mode") == "replay"
        and d.get("replay_recording_id") is not None
        and d.get("enabled")
    ]
    for dev in replay_devices:
        await sim_engine.advance_replay_playback(dev)


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
    replay_recording_task = asyncio.create_task(replay_recording_loop())
    replay_playback_task = asyncio.create_task(replay_playback_loop())
    log.info("BACnet Simulator API ready on port %d", SIM_API_PORT)
    yield
    log.info("Shutting down")
    tick_task.cancel()
    mirror_task.cancel()
    metrics_task.cancel()
    simulation_recovery_task.cancel()
    replay_recording_task.cancel()
    replay_playback_task.cancel()
    try:
        await tick_task
    except asyncio.CancelledError:
        pass
    await dependencies.engine.stop()
