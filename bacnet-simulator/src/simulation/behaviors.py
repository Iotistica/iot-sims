"""Simulation behavior strategies.

Physically extracted from src/legacy.py's `Behavior` hierarchy + `make_behavior()`
(continuing the GH #15 refactor, same "moved verbatim, no behavior changes"
standard as the Database and API-router extractions).
"""
from __future__ import annotations

import json
import math
import random
from abc import ABC, abstractmethod
from typing import Any, Optional, Union

from .state import SimState


def _dependencies():
    """
    Resolve src.dependencies lazily.

    IMPORTANT:
    Do not import src.dependencies at module import time.

    src.dependencies imports src.db.database, which itself reaches this
    module at import time via simulation.models.store -> simulation.models
    -> simulation.providers -> simulation.providers.builtin ->
    bacnet.app -> simulation.behaviors (this module). Importing
    src.dependencies eagerly here would re-enter that partially initialized
    chain and raise a circular-import ImportError -- caught by directly
    booting SimEngine, not by pyflakes (which checks each file in isolation
    and can't see multi-hop cycles like this one).

    FaultBehavior.compute() needs the live value of TICK_SECONDS, which is
    a mutable module global on src.dependencies (updated at runtime by
    _apply_settings_live()) -- resolving it lazily, and reading the
    attribute fresh on each call, is what keeps that live.
    """
    from .. import dependencies
    return dependencies


class Behavior(ABC):
    @abstractmethod
    def compute(self, state: SimState) -> Union[float, bool]:
        ...


class ConstantBehavior(Behavior):
    def __init__(self, params: dict):
        self.value = params.get("value", 0)

    def compute(self, state: SimState) -> Any:
        if isinstance(self.value, bool):
            return self.value
        return float(self.value)


class SineBehavior(Behavior):
    def __init__(self, params: dict):
        self.base = float(params.get("base", 20.0))
        self.amplitude = float(params.get("amplitude", 5.0))
        self.period_hours = float(params.get("period_hours", 24.0))
        self.phase_hours = float(params.get("phase_hours", 0.0))

    def compute(self, state: SimState) -> float:
        t = state.time_of_day + self.phase_hours
        return self.base + self.amplitude * math.sin(2 * math.pi * t / self.period_hours)


class NoiseBehavior(Behavior):
    def __init__(self, params: dict):
        self.base = float(params.get("base", 0.0))
        self.noise = float(params.get("noise", 1.0))

    def compute(self, state: SimState) -> float:
        return self.base + random.uniform(-self.noise, self.noise)


class RandomWalkBehavior(Behavior):
    def __init__(self, params: dict):
        self._value = float(params.get("value", 50.0))
        self.step = float(params.get("step", 1.0))
        self.min = float(params.get("min", 0.0))
        self.max = float(params.get("max", 100.0))

    def compute(self, state: SimState) -> float:
        self._value = max(self.min, min(self.max, self._value + random.uniform(-self.step, self.step)))
        return self._value


class ManualBehavior(Behavior):
    # Recognized boolean-ish string forms -- covers not just literal JSON
    # true/false but also the ON/OFF vocabulary this app itself displays
    # binary points as (see admin/src/format.ts's formatPresentValue) and
    # BACnet's own active/inactive enumeration names. Anything NOT in this
    # set falls through to float() -- correct for analog manual overrides
    # (e.g. "72.5"), where treating "false"-shaped text as special would be
    # wrong. Case/whitespace-insensitive since this can come from a free-
    # text UI field, not just a typed API caller.
    _TRUE_WORDS = ("true", "on", "active", "yes")
    _FALSE_WORDS = ("false", "off", "inactive", "no")

    @classmethod
    def _coerce(cls, raw: Any) -> Any:
        if raw is None:
            return 0
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in cls._TRUE_WORDS:
                return True
            if normalized in cls._FALSE_WORDS:
                return False
        return float(raw)

    def __init__(self, params: dict, stored_value: Any = None):
        self._value = self._coerce(params.get("value", stored_value))

    def set(self, v: Any) -> None:
        self._value = self._coerce(v)

    def compute(self, state: SimState) -> Any:
        return self._value


class DailyPatternBehavior(Behavior):
    """Returns different values based on time-of-day blocks (occupied/unoccupied
    scheduling). Not to be confused with a real BACnet Schedule object (see
    bacnet_schedule.py) — this is purely a value-simulation behavior, stored
    under the historical behavior name "schedule" for backward compatibility
    with existing projects/seed data, but renamed at the Python-class level
    now that real BACnet Schedule objects exist too."""

    @staticmethod
    def _parse_time(t: str) -> float:
        try:
            h, m = t.split(":")
            return int(h) + int(m) / 60.0
        except Exception:
            return 0.0

    def __init__(self, params: dict):
        self.default = float(params.get("default", 0))
        raw_blocks = params.get("blocks", [])
        self.blocks = sorted(
            [{"start": self._parse_time(b.get("start", "00:00")), "value": float(b.get("value", 0))}
             for b in raw_blocks if isinstance(b, dict)],
            key=lambda b: b["start"],
        )

    def compute(self, state: SimState) -> float:
        current = state.time_of_day % 24
        value = self.default
        for block in self.blocks:
            if current >= block["start"]:
                value = block["value"]
            else:
                break
        return value


class RampBehavior(Behavior):
    """Linearly ramps from one value to another over a fixed duration, optionally repeating."""

    def __init__(self, params: dict):
        self.from_val = float(params.get("from", 0))
        self.to_val = float(params.get("to", 100))
        self.duration_seconds = float(params.get("duration_minutes", 60)) * 60
        self.repeat = bool(params.get("repeat", True))

    def compute(self, state: SimState) -> float:
        if self.duration_seconds <= 0:
            return self.to_val
        if self.repeat:
            t = state.elapsed_seconds % self.duration_seconds
        else:
            t = min(state.elapsed_seconds, self.duration_seconds)
        frac = t / self.duration_seconds
        return self.from_val + (self.to_val - self.from_val) * frac


class FaultBehavior(Behavior):
    """Wraps a base behavior and randomly injects fault conditions (spike, stuck, offline)."""

    def __init__(self, params: dict):
        self._base_behavior_name = params.get("base_behavior", "constant")
        self._base_params = params.get("base_params", {"value": 0})
        self._inner: Optional[Behavior] = None
        self.fault_type = params.get("fault_type", "spike")
        self.fault_value = float(params.get("fault_value", 999))
        self.mtbf_minutes = float(params.get("mtbf_minutes", 60))
        self.fault_duration_seconds = float(params.get("fault_duration_seconds", 30))
        self._fault_active = False
        self._fault_end_elapsed: float = -1.0

    def compute(self, state: SimState) -> float:
        if self._inner is None:
            self._inner = make_behavior(self._base_behavior_name, json.dumps(self._base_params))

        if self._fault_active and state.elapsed_seconds > self._fault_end_elapsed:
            self._fault_active = False

        if not self._fault_active:
            # Ticks occur every TICK_SECONDS, not every second, so scale the
            # per-tick probability accordingly to make mtbf_minutes accurate.
            prob_per_tick = _dependencies().TICK_SECONDS / max(1.0, self.mtbf_minutes * 60.0)
            if random.random() < prob_per_tick:
                self._fault_active = True
                if self.fault_type == "spike":
                    self._fault_end_elapsed = state.elapsed_seconds
                else:
                    self._fault_end_elapsed = state.elapsed_seconds + self.fault_duration_seconds

        if self._fault_active:
            return 0.0 if self.fault_type == "offline" else self.fault_value

        return float(self._inner.compute(state))


def make_behavior(behavior: str, params_json: str, manual_value: Any = None) -> Behavior:
    try:
        params = json.loads(params_json) if params_json else {}
    except Exception:
        params = {}
    if behavior == "constant":
        return ConstantBehavior(params)
    if behavior == "sine":
        return SineBehavior(params)
    if behavior == "noise":
        return NoiseBehavior(params)
    if behavior == "random_walk":
        return RandomWalkBehavior(params)
    if behavior == "manual":
        return ManualBehavior(params, manual_value)
    if behavior == "schedule":
        return DailyPatternBehavior(params)
    if behavior == "ramp":
        return RampBehavior(params)
    if behavior == "fault":
        return FaultBehavior(params)
    if behavior == "raw":
        # "raw" only has meaning for a provider-owned point once its FMU
        # has actually produced a value (see SimEngine._apply_fmu_behavior,
        # which substitutes the live raw value directly and never calls
        # this instance's compute() at all for "raw"). Before that first
        # tick -- e.g. the one-time initial-value seed in
        # SimEngine._create_object() -- there is no raw value yet, so this
        # instance's own compute() falls back to whatever's stored in
        # behavior_params, exactly like "constant" does. Critically, this
        # must NOT hit the generic "unrecognized -> ConstantBehavior({
        # "value": 0})" fallback below, which discards params entirely --
        # that silently zeroed every "raw" point's seed value (and any
        # weighted-average aggregate depending on it) when this branch was
        # missing.
        return ConstantBehavior(params)
    return ConstantBehavior({"value": 0})
