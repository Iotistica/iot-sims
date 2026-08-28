"""tick_loop -- self-correcting cadence. A plain sleep(TICK_SECONDS)-then-
tick() loop drifts by the full overrun every time a tick takes longer than
TICK_SECONDS (confirmed live: FMU-backed devices' step() is a real blocking
HTTP round trip that can take longer than the nominal 5s tick). tick_loop
now tracks a fixed target wall-clock schedule (next_tick_at) and skips the
sleep entirely -- running the next tick immediately -- whenever it's
already behind, logging that it's doing so, rather than compounding the
delay forever."""
from __future__ import annotations

import asyncio
import logging

from src import dependencies
from src.simulation import runtime


class _FakeState:
    def __init__(self) -> None:
        self.elapsed_seconds = 0.0


class _FakeEngine:
    """Enough of SimEngine for tick_loop's own body: clock_state gate,
    an async tick() that counts calls (and can simulate a slow step), and
    get_state()/state for the post-tick logging/recording hooks."""

    def __init__(self, *, tick_delay_s: float = 0.0) -> None:
        self.clock_state = "running"
        self.state = _FakeState()
        self.tick_calls = 0
        self._tick_delay_s = tick_delay_s

    async def tick(self) -> None:
        if self._tick_delay_s:
            await asyncio.sleep(self._tick_delay_s)
        self.tick_calls += 1

    def get_state(self) -> dict:
        return {"devices": []}

    def get_object_value(self, object_id: int):
        return None


async def _run_tick_loop_briefly(monkeypatch, database, engine, *, tick_seconds: float, run_for_s: float) -> None:
    monkeypatch.setattr(dependencies, "TICK_SECONDS", tick_seconds)
    monkeypatch.setattr(dependencies, "engine", engine)
    monkeypatch.setattr(dependencies, "db", database)
    monkeypatch.setattr(dependencies, "ws_clients", [])  # broadcast_state() no-ops when empty

    task = asyncio.create_task(runtime.tick_loop(None, None))
    try:
        await asyncio.sleep(run_for_s)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_tick_cadence_matches_tick_seconds_when_ticks_are_fast(monkeypatch, database):
    engine = _FakeEngine(tick_delay_s=0.0)
    tick_seconds = 0.05

    await _run_tick_loop_briefly(monkeypatch, database, engine, tick_seconds=tick_seconds, run_for_s=0.5)

    # ~0.5s / 0.05s per tick = ~10 ticks; allow generous slack for CI timing
    # jitter without the test being able to pass on a fundamentally wrong
    # cadence (e.g. one tick, or hundreds).
    assert 5 <= engine.tick_calls <= 15


async def test_slow_tick_logs_running_behind_schedule_and_continues_immediately(monkeypatch, database, caplog):
    # Each tick takes 3x TICK_SECONDS -- every tick after the first should
    # find itself already behind schedule and skip its sleep entirely.
    tick_seconds = 0.03
    engine = _FakeEngine(tick_delay_s=tick_seconds * 3)

    caplog.set_level(logging.WARNING, logger="bacnet-sim")
    await _run_tick_loop_briefly(monkeypatch, database, engine, tick_seconds=tick_seconds, run_for_s=0.3)

    assert engine.tick_calls >= 2
    messages = [r.getMessage() for r in caplog.records]
    assert any("running behind schedule" in m for m in messages)
    assert any("exceeding TICK_SECONDS" in m for m in messages)


async def test_ticks_never_overlap(monkeypatch, database):
    """A slow tick must fully finish (tick_calls incremented) before the
    next one starts -- proven by tick_calls never exceeding what a single
    sequential loop could have produced in the run window, even though
    each tick sleeps far longer than TICK_SECONDS."""
    tick_seconds = 0.02
    tick_delay_s = 0.1
    run_for_s = 0.35
    engine = _FakeEngine(tick_delay_s=tick_delay_s)

    await _run_tick_loop_briefly(monkeypatch, database, engine, tick_seconds=tick_seconds, run_for_s=run_for_s)

    # If ticks overlapped, many more than run_for_s/tick_delay_s could have
    # been *started* concurrently; sequential execution caps it at roughly
    # run_for_s/tick_delay_s (+1 for the one in flight when cancelled).
    max_possible_sequential = int(run_for_s / tick_delay_s) + 2
    assert engine.tick_calls <= max_possible_sequential
