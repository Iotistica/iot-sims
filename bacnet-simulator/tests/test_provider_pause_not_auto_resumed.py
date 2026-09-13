"""Per-device Pause/Resume/Reset (Simulation -> Model's own runtime
controls, distinct from the global POST /sim/pause and from Enable/Disable)
-- the bug this whole feature exists to fix: _step_one_provider used to call
provider.start() unconditionally whenever a provider's status wasn't
RUNNING, and FMUSimulationProvider.start() treats PAUSED the same as a
never-started READY provider -- so an explicitly paused provider was
silently un-paused on the very next tick. See engine.py's own
_step_one_provider/pause_provider/resume() comments.

Uses a small SimulationProvider ABC-conforming fake (not FMUSimulationProvider
directly) so these tests exercise SimEngine's own orchestration logic in
isolation from FMU-session/HTTP-client plumbing -- and deliberately
reproduces the real bug's exact shape (start() treats READY/PAUSED as
equally resumable) so a regression of the engine-side fix would actually
fail these tests, not just a mock that always cooperates.
"""
from __future__ import annotations

from src.simulation.engine import SimEngine
from src.simulation.providers.base import ProviderStatus, SimulationContext, ValidationResult


class _FakeProvider:
    """Mirrors the real bug shape: start() resumes READY *and* PAUSED alike
    (base.py's own ABC contract, satisfied minimally). step() only ever
    advances output_value while genuinely RUNNING -- the observable signal
    these tests check "did this provider actually get stepped". Also
    records every dt it was actually stepped with, for the RTF tests below.
    """

    def __init__(self, output_point_id: int) -> None:
        self.output_point_id = output_point_id
        self._status = ProviderStatus.READY
        self.output_value = 0
        self.step_calls = 0
        self.dts_seen: list[float] = []

    def initialize(self, context: SimulationContext) -> None:
        pass

    def start(self) -> None:
        if self._status in (ProviderStatus.READY, ProviderStatus.PAUSED):
            self._status = ProviderStatus.RUNNING

    def pause(self) -> None:
        if self._status == ProviderStatus.RUNNING:
            self._status = ProviderStatus.PAUSED

    def stop(self) -> None:
        self._status = ProviderStatus.STOPPED

    def reset(self) -> None:
        self.output_value = 0
        self.step_calls = 0
        self._status = ProviderStatus.RUNNING

    def step(self, dt: float) -> None:
        if self._status != ProviderStatus.RUNNING:
            return
        self.step_calls += 1
        self.output_value += 1
        self.dts_seen.append(dt)

    def set_inputs(self, values) -> None:
        pass

    def get_outputs(self):
        return {self.output_point_id: self.output_value}

    def validate(self) -> ValidationResult:
        return ValidationResult(valid=True)

    def get_status(self) -> ProviderStatus:
        return self._status


def _register(engine: SimEngine, runtime_id: str, output_point_id: int) -> _FakeProvider:
    provider = _FakeProvider(output_point_id)
    engine.register_simulation_provider(
        runtime_id, provider,
        context=SimulationContext(),
        output_point_ids={output_point_id},
    )
    return provider


def _tick(engine: SimEngine) -> dict:
    """One provider-stepping pass, bypassing the full async tick()/BACnet-
    object plumbing entirely -- _run_registered_providers is deliberately
    self-contained (no self.db access, see its own docstring), so this is
    exactly the part _step_one_provider's fix lives in."""
    return engine._run_registered_providers(5.0, {})


def test_paused_provider_is_not_auto_resumed_on_next_tick(database):
    engine = SimEngine(database)
    runtime_id = "fmu:test:1"
    provider = _register(engine, runtime_id, output_point_id=101)

    _tick(engine)  # first tick: READY -> RUNNING -> stepped once
    assert provider.get_status() == ProviderStatus.RUNNING
    assert provider.step_calls == 1

    assert engine.pause_provider(runtime_id) is True
    assert provider.get_status() == ProviderStatus.PAUSED

    for _ in range(3):
        _tick(engine)
    # The bug this test guards against: a naive `if status != RUNNING:
    # provider.start()` would have flipped this back to RUNNING and
    # stepped it on the very first of these three ticks.
    assert provider.get_status() == ProviderStatus.PAUSED
    assert provider.step_calls == 1
    assert provider.output_value == 1


def test_resume_provider_brings_it_back_to_running(database):
    engine = SimEngine(database)
    runtime_id = "fmu:test:1"
    provider = _register(engine, runtime_id, output_point_id=101)
    _tick(engine)
    engine.pause_provider(runtime_id)
    _tick(engine)
    assert provider.step_calls == 1  # still paused, unaffected by the tick above

    assert engine.resume_provider(runtime_id) is True
    assert provider.get_status() == ProviderStatus.RUNNING

    _tick(engine)
    assert provider.step_calls == 2


def test_local_pause_survives_global_pause_then_resume(database):
    """The other half of the spec: a locally-paused device must remain
    paused after a global pause followed by global resume, while a
    DIFFERENT, never-locally-paused provider resumes normally."""
    engine = SimEngine(database)
    provider_a = _register(engine, "fmu:test:a", output_point_id=101)
    provider_b = _register(engine, "fmu:test:b", output_point_id=102)
    _tick(engine)
    assert provider_a.step_calls == 1
    assert provider_b.step_calls == 1

    engine.pause_provider("fmu:test:a")
    assert provider_a.get_status() == ProviderStatus.PAUSED

    engine.pause()  # global pause -- cascades provider.pause() to both (no-op for A)
    assert provider_a.get_status() == ProviderStatus.PAUSED
    assert provider_b.get_status() == ProviderStatus.PAUSED

    engine.resume()  # global resume
    assert provider_a.get_status() == ProviderStatus.PAUSED   # stays paused
    assert provider_b.get_status() == ProviderStatus.RUNNING  # resumes normally

    _tick(engine)
    assert provider_a.step_calls == 1   # never advanced further
    assert provider_b.step_calls == 2


def test_reset_provider_clears_local_pause_and_brings_it_back_running(database):
    engine = SimEngine(database)
    runtime_id = "fmu:test:1"
    provider = _register(engine, runtime_id, output_point_id=101)
    _tick(engine)
    engine.pause_provider(runtime_id)

    assert engine.reset_provider(runtime_id) is True
    assert provider.get_status() == ProviderStatus.RUNNING
    assert provider.output_value == 0
    assert runtime_id not in engine._locally_paused_provider_ids

    _tick(engine)
    assert provider.step_calls == 1


def test_global_reset_clears_all_local_pause_tracking(database):
    engine = SimEngine(database)
    provider_a = _register(engine, "fmu:test:a", output_point_id=101)
    engine.pause_provider("fmu:test:a")
    assert "fmu:test:a" in engine._locally_paused_provider_ids

    engine.reset()

    assert engine._locally_paused_provider_ids == set()
    assert provider_a.get_status() == ProviderStatus.RUNNING


def test_pause_resume_reset_provider_return_false_for_unknown_runtime_id(database):
    engine = SimEngine(database)
    assert engine.pause_provider("fmu:does-not-exist:1") is False
    assert engine.resume_provider("fmu:does-not-exist:1") is False
    assert engine.reset_provider("fmu:does-not-exist:1") is False


def test_locally_paused_flag_surfaced_in_get_simulation_providers(database):
    engine = SimEngine(database)
    runtime_id = "fmu:test:1"
    _register(engine, runtime_id, output_point_id=101)

    assert engine.get_simulation_providers()[runtime_id]["locally_paused"] is False
    engine.pause_provider(runtime_id)
    assert engine.get_simulation_providers()[runtime_id]["locally_paused"] is True
    engine.resume_provider(runtime_id)
    assert engine.get_simulation_providers()[runtime_id]["locally_paused"] is False


def test_unregister_clears_stale_local_pause_tracking(database):
    engine = SimEngine(database)
    runtime_id = "fmu:test:1"
    _register(engine, runtime_id, output_point_id=101)
    engine.pause_provider(runtime_id)
    assert runtime_id in engine._locally_paused_provider_ids

    assert engine.unregister_simulation_provider(runtime_id) is True
    assert runtime_id not in engine._locally_paused_provider_ids


# ─── Per-provider real-time-factor ─────────────────────────────────────────
# Scales how much simulated time a provider's own step(dt) call represents
# per shared wall-clock tick -- every provider still steps together, on the
# same cadence (see _run_registered_providers' own provider_dt); only the
# dt each one is individually told changes.

def test_default_real_time_factor_is_one(database):
    engine = SimEngine(database)
    runtime_id = "fmu:test:1"
    provider = _register(engine, runtime_id, output_point_id=101)

    _tick(engine)

    assert provider.dts_seen == [5.0]
    assert engine.get_simulation_providers()[runtime_id]["real_time_factor"] == 1.0


def test_real_time_factor_scales_this_providers_own_dt_only(database):
    engine = SimEngine(database)
    provider_a = _register(engine, "fmu:test:a", output_point_id=101)
    provider_b = _register(engine, "fmu:test:b", output_point_id=102)

    assert engine.set_provider_real_time_factor("fmu:test:a", 2.0) is True
    _tick(engine)

    # A sees 2x the simulated time per tick; B (left at the 1.0 default)
    # sees the plain shared dt, completely unaffected by A's own setting.
    assert provider_a.dts_seen == [10.0]
    assert provider_b.dts_seen == [5.0]
    assert engine.get_simulation_providers()["fmu:test:a"]["real_time_factor"] == 2.0
    assert engine.get_simulation_providers()["fmu:test:b"]["real_time_factor"] == 1.0


def test_set_real_time_factor_returns_false_for_unknown_runtime_id(database):
    engine = SimEngine(database)
    assert engine.set_provider_real_time_factor("fmu:does-not-exist:1", 2.0) is False


def test_unregister_clears_stale_real_time_factor(database):
    engine = SimEngine(database)
    runtime_id = "fmu:test:1"
    _register(engine, runtime_id, output_point_id=101)
    engine.set_provider_real_time_factor(runtime_id, 3.0)
    assert runtime_id in engine._provider_real_time_factor

    assert engine.unregister_simulation_provider(runtime_id) is True
    assert runtime_id not in engine._provider_real_time_factor
