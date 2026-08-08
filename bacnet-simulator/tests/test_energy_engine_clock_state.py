"""Regression test for a production 500: POST /sim/start (and /sim/pause,
/sim/stop) call energy_engine.resume()/pause()/reset() alongside the
matching SimEngine method, but EnergyEngine never had pause()/resume()
methods (only reset() existed) -- AttributeError on every start/pause
request. See src/api/routers/simulation.py's start_simulation/
pause_simulation/stop_simulation handlers."""
from __future__ import annotations

from src.energy.engine import EnergyEngine


class _StubSimulationEngine:
    def get_object_value(self, object_id: int):
        return None

    def get_device_point_values(self, objects):
        return {}


def test_pause_resume_reset_exist_and_track_clock_state(database):
    engine = EnergyEngine(database=database, simulation_engine=_StubSimulationEngine())
    assert engine.clock_state == "running"

    engine.pause()
    assert engine.clock_state == "paused"

    engine.resume()
    assert engine.clock_state == "running"

    engine.reset()
    assert engine.clock_state == "stopped"
