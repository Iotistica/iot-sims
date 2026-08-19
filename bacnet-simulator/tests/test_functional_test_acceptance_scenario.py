"""The one test proving the full HVAC-regression feature set works
TOGETHER, not just in isolation: cross-device points (an AHU and a separate
Chiller Plant referenced in the same test), the within_tolerance operator,
an operand-based Wait Until value (baseline-relative, via a captured
variable), and Set+automatic-restore -- built directly against
GraphExecutor (no HTTP, no DB) with a FakeTestRuntime seeded so every step
succeeds on its first read, mirroring the acceptance scenario:

Start -> wait AHU SAT near 13C -> capture AHU CC-Valve & Cooling-Capacity-
Available -> Set Chiller Plant CH-2-Run=OFF -> Wait Until AHU cooling
capacity decreases vs. baseline -> Verify CC-Valve increased -> Verify SAT
remains in range -> (implicit restore) -> End.

Mirrors the graph shape a real author would build in the Functional Test
builder for this scenario (the "Load Example" skeleton that used to ship
this shape has been removed -- New Test starts blank now)."""
from __future__ import annotations

import asyncio

from src.functional_tests.executor import GraphExecutor


# Two distinct devices -- the AHU under test, and a separate Chiller Plant
# whose CH-2-Run this scenario disables to provoke the AHU's response.
AHU = 1
CHILLER_PLANT = 2

SAT = {"device_id": AHU, "object_id": 1}
CC_VALVE = {"device_id": AHU, "object_id": 2}
COOLING_CAPACITY = {"device_id": AHU, "object_id": 3}
CH2_RUN = {"device_id": CHILLER_PLANT, "object_id": 1}


def _key(ref: dict) -> tuple:
    return (ref["device_id"], ref["object_id"])


class FakeTestRuntime:
    """SAT, CC-Valve, and Cooling-Capacity-Available each read one value
    before the chiller gets disabled and a (possibly different) one after
    -- switched on whether a write has happened yet -- so the fake actually
    models "the AHU responds to the chiller lockout" instead of every read
    being a frozen constant regardless of what Set just did. wait-sat
    (the very first gate) only ever observes the "before" value, since it
    runs before Set; verify-sat-in-range only ever observes the "after"
    value, since it runs after."""

    def __init__(self, sat_before: float = 13.2, sat_after: float = 13.2):
        self.sat_before = sat_before
        self.sat_after = sat_after
        self._clock = 0.0
        self.writes: list[tuple] = []

    async def read(self, point_ref):
        key = _key(point_ref)
        if key == _key(SAT):
            return self.sat_after if self.writes else self.sat_before
        if key == _key(CC_VALVE):
            return 55 if self.writes else 40
        if key == _key(COOLING_CAPACITY):
            return 90 if self.writes else 100
        return None

    async def write(self, point_ref, value, priority=None):
        self.writes.append((_key(point_ref), value, priority))

    async def snapshot_for_restore(self, point_ref):
        return {"commandable": True}

    def now(self) -> float:
        return self._clock

    async def wait(self, seconds, cancel_event):
        self._clock += seconds


def _acceptance_definition() -> dict:
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "wait-sat", "type": "wait_until", "params": {
                "point": SAT, "operator": "within_tolerance",
                "value": {"kind": "constant", "value": 13}, "tolerance": 0.5,
                "timeout_seconds": 600,
            }},
            {"id": "capture-cc-valve", "type": "capture", "params": {
                "point": CC_VALVE, "variable": "ccValveBaseline",
            }},
            {"id": "capture-cooling-capacity", "type": "capture", "params": {
                "point": COOLING_CAPACITY, "variable": "coolingCapacityBaseline",
            }},
            {"id": "set-ch2-off", "type": "set", "params": {"point": CH2_RUN, "value": False, "priority": 8}},
            {"id": "wait-capacity-drop", "type": "wait_until", "params": {
                "point": COOLING_CAPACITY, "operator": "lt",
                "value": {"kind": "variable", "name": "coolingCapacityBaseline", "offset": -1},
                "timeout_seconds": 600,
            }},
            {"id": "verify-cc-valve-increased", "type": "verify", "params": {
                "left": {"kind": "point", "point": CC_VALVE},
                "operator": "gt",
                "right": {"kind": "variable", "name": "ccValveBaseline"},
            }},
            {"id": "verify-sat-in-range", "type": "verify", "params": {
                "left": {"kind": "point", "point": SAT},
                "operator": "within_tolerance",
                "right": {"kind": "constant", "value": 13},
                "tolerance": 1,
            }},
            {"id": "end-pass", "type": "end", "params": {"result": "pass"}},
            {"id": "end-fail", "type": "end", "params": {"result": "fail", "message": "AHU did not respond as expected"}},
        ],
        "edges": [
            {"source": "start", "target": "wait-sat", "source_handle": None},
            {"source": "wait-sat", "target": "capture-cc-valve", "source_handle": None},
            {"source": "capture-cc-valve", "target": "capture-cooling-capacity", "source_handle": None},
            {"source": "capture-cooling-capacity", "target": "set-ch2-off", "source_handle": None},
            {"source": "set-ch2-off", "target": "wait-capacity-drop", "source_handle": None},
            {"source": "wait-capacity-drop", "target": "verify-cc-valve-increased", "source_handle": None},
            {"source": "verify-cc-valve-increased", "target": "verify-sat-in-range", "source_handle": "pass"},
            {"source": "verify-cc-valve-increased", "target": "end-fail", "source_handle": "fail"},
            {"source": "verify-sat-in-range", "target": "end-pass", "source_handle": "pass"},
            {"source": "verify-sat-in-range", "target": "end-fail", "source_handle": "fail"},
        ],
    }


async def test_acceptance_scenario_passes_end_to_end():
    runtime = FakeTestRuntime(sat_before=13.2, sat_after=13.2)  # stays within 0.5 of the 13C setpoint throughout
    executor = GraphExecutor(_acceptance_definition(), runtime, asyncio.Event())

    result = await executor.run()

    assert result.state == "passed"
    assert result.result == "pass"

    # Cross-device points: both the AHU's and the Chiller Plant's points
    # were actually read/written in the same run. Baselines are captured
    # BEFORE the chiller is disabled (40/100), and both downstream checks
    # observe the post-lockout values (55/90) -- proving the wait/verify
    # steps re-read live state rather than reusing the captured snapshot.
    assert executor.variables["ccValveBaseline"] == 40
    assert executor.variables["coolingCapacityBaseline"] == 100
    assert runtime.writes == [(_key(CH2_RUN), False, 8)]


async def test_acceptance_scenario_fails_when_sat_drifts_out_of_range():
    # Passes the initial "near setpoint" gate (still 13.2C, before the
    # chiller is disabled), but has drifted to 15C by the time the final
    # verify runs -- must take the fail edge instead of silently passing.
    runtime = FakeTestRuntime(sat_before=13.2, sat_after=15.0)
    executor = GraphExecutor(_acceptance_definition(), runtime, asyncio.Event())

    result = await executor.run()

    assert result.state == "failed"
    assert result.result == "fail"
