"""SimEngine._run_registered_providers -- dependency-staged ("wave")
concurrent provider stepping. Each FMU-backed provider's step() is a
blocking HTTP round trip to the external FMU runtime; stepping every
registered provider one at a time made a single tick's duration the *sum*
of every device's round trip (confirmed live: Replay Recording exports
showed many consecutive identical samples across every point on a device
at once, on a project with several FMU-backed devices).

Uses lightweight fake providers satisfying the interface
_run_registered_providers actually calls (set_inputs/get_status/start/
step/get_outputs/get_diagnostics) rather than a real FMUSimulationProvider
-- these tests exercise the wave-scheduling/merge logic itself, not
FMU-specific request/response handling (already covered elsewhere, e.g.
tests/test_fmu_behavior.py). Constructs a bare SimEngine(database) with no
running BACnet app, following this codebase's established unit-test
convention (see tests/test_fmu_behavior.py, tests/test_sim_engine_object_value.py).
"""
from __future__ import annotations

import time

from src.simulation.engine import SimEngine
from src.simulation.providers.base import ProviderStatus


class _FakeProvider:
    """Minimal stand-in for the provider interface. `on_step` receives the
    resolved inputs dict for this call and returns this provider's outputs
    dict."""

    def __init__(self, on_step):
        self._status = ProviderStatus.RUNNING
        self._on_step = on_step
        self._last_inputs: dict[int, float] = {}
        self._outputs: dict[int, float] = {}

    def set_inputs(self, inputs):
        self._last_inputs = dict(inputs)

    def get_status(self):
        return self._status

    def start(self):
        self._status = ProviderStatus.RUNNING

    def step(self, dt):
        self._outputs = self._on_step(dict(self._last_inputs))

    def get_outputs(self):
        return self._outputs

    def get_diagnostics(self):
        return {"runtime_state": "RUNNING"}


class _SleepingProvider(_FakeProvider):
    """Simulates a real FMU HTTP round trip's latency."""

    def __init__(self, delay_s, outputs):
        super().__init__(on_step=lambda _inputs: outputs)
        self._delay_s = delay_s

    def step(self, dt):
        time.sleep(self._delay_s)
        super().step(dt)


class _RaisingProvider(_FakeProvider):
    def step(self, dt):
        raise RuntimeError("boom")


def _register(engine: SimEngine, provider_id, provider, *, inputs=(), outputs=()):
    engine._providers[provider_id] = provider
    engine._provider_input_points[provider_id] = set(inputs)
    engine._provider_output_points[provider_id] = set(outputs)
    for point_id in outputs:
        engine._point_output_owner[point_id] = provider_id


def test_independent_providers_step_concurrently_not_sequentially(database):
    engine = SimEngine(database)
    providers = [_SleepingProvider(0.3, outputs={100 + i: float(i)}) for i in range(4)]
    for i, provider in enumerate(providers):
        _register(engine, f"p{i}", provider, outputs={100 + i})

    start = time.monotonic()
    result = engine._run_registered_providers(5.0, {})
    duration = time.monotonic() - start

    # Sequential would take ~4 * 0.3s = 1.2s; concurrent (one wave, all
    # independent) should take close to a single 0.3s sleep.
    assert duration < 0.7
    assert result == {100: 0.0, 101: 1.0, 102: 2.0, 103: 3.0}


def test_dependent_provider_sees_same_tick_upstream_value(database):
    engine = SimEngine(database)

    upstream = _FakeProvider(on_step=lambda _inputs: {200: 42.0})
    downstream_received: dict = {}

    def _downstream_step(inputs):
        downstream_received.update(inputs)
        return {201: inputs.get(200)}

    downstream = _FakeProvider(on_step=_downstream_step)

    # Registration order matters: upstream first, downstream second -- so
    # downstream's wave lands strictly after upstream's wave, and sees
    # upstream's *this-tick* output rather than a stale value.
    _register(engine, "upstream", upstream, outputs={200})
    _register(engine, "downstream", downstream, inputs={200}, outputs={201})

    result = engine._run_registered_providers(5.0, {})

    assert downstream_received == {200: 42.0}
    assert result[201] == 42.0


def test_one_providers_failure_does_not_lose_the_others(database):
    engine = SimEngine(database)
    good = _FakeProvider(on_step=lambda _inputs: {300: 1.0})
    bad = _RaisingProvider(on_step=lambda _inputs: {})

    _register(engine, "good", good, outputs={300})
    _register(engine, "bad", bad, outputs={301})

    result = engine._run_registered_providers(5.0, {})

    assert result == {300: 1.0}
    assert engine._provider_diagnostics.get("good") == {"runtime_state": "RUNNING"}
    assert "bad" not in engine._provider_diagnostics


def test_diagnostics_recorded_per_provider(database):
    engine = SimEngine(database)
    provider = _FakeProvider(on_step=lambda _inputs: {400: 9.0})
    _register(engine, "p", provider, outputs={400})

    engine._run_registered_providers(5.0, {})

    assert engine._provider_diagnostics["p"] == {"runtime_state": "RUNNING"}
