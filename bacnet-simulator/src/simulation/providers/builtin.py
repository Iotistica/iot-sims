from __future__ import annotations

from typing import Any, Mapping

from ...legacy import Behavior, ManualBehavior, SimState, make_behavior, normalize_present_value
from .base import PointConfig, ProviderStatus, SimulationContext, SimulationProvider, ValidationResult

_KNOWN_BEHAVIORS = {
    "constant", "sine", "noise", "random_walk",
    "manual", "schedule", "ramp", "fault",
}


class BuiltInSimulationProvider(SimulationProvider):
    """Adapter around the existing built-in Behavior engine."""

    def __init__(self) -> None:
        self._context: SimulationContext | None = None
        self._state = SimState()
        self._behaviors: dict[int, Behavior] = {}
        self._outputs: dict[int, Any] = {}
        self._status = ProviderStatus.NOT_CONFIGURED

    def _build_behaviors(self) -> None:
        self._behaviors = {}
        if self._context is None:
            return
        for point in self._context.point_configs:
            self._behaviors[point.point_id] = make_behavior(
                point.behavior,
                point.behavior_params,
                point.manual_value,
            )

    def initialize(self, context: SimulationContext) -> None:
        self._context = context
        self._state = SimState()
        self._outputs = {}
        self._build_behaviors()
        self._status = ProviderStatus.READY

    def start(self) -> None:
        if self._status in (ProviderStatus.READY, ProviderStatus.PAUSED):
            self._status = ProviderStatus.RUNNING

    def pause(self) -> None:
        if self._status == ProviderStatus.RUNNING:
            self._status = ProviderStatus.PAUSED

    def stop(self) -> None:
        self._status = (
            ProviderStatus.STOPPED
            if self._context is not None
            else ProviderStatus.NOT_CONFIGURED
        )

    def reset(self) -> None:
        # True reset: clear SimState + outputs + all stateful Behavior instances.
        self._state = SimState()
        self._outputs = {}
        if self._context is None:
            self._behaviors = {}
            self._status = ProviderStatus.NOT_CONFIGURED
            return
        self._build_behaviors()
        self._status = ProviderStatus.READY

    def step(self, dt: float) -> None:
        if self._status != ProviderStatus.RUNNING:
            return
        if self._context is None or dt < 0:
            self._status = ProviderStatus.ERROR
            return

        self._state.elapsed_seconds += dt
        self._state.time_of_day = (self._state.time_of_day + dt / 3600.0) % 24.0

        point_by_id = {p.point_id: p for p in self._context.point_configs}
        for point_id, behavior in self._behaviors.items():
            point = point_by_id.get(point_id)
            if point is None:
                continue
            raw = behavior.compute(self._state)
            self._outputs[point_id] = normalize_present_value(point.object_type, raw)

    def set_inputs(self, values: Mapping[int, Any]) -> None:
        for point_id, value in values.items():
            behavior = self._behaviors.get(point_id)
            if isinstance(behavior, ManualBehavior):
                behavior.set(value)

    def get_outputs(self) -> Mapping[int, Any]:
        return dict(self._outputs)

    def validate(self) -> ValidationResult:
        if self._context is None:
            return ValidationResult(valid=False, errors=["Provider not initialized"])
        warnings = [
            f"Unknown behavior '{p.behavior}' on point {p.point_id}; legacy fallback will be used."
            for p in self._context.point_configs
            if p.behavior not in _KNOWN_BEHAVIORS
        ]
        return ValidationResult(valid=True, warnings=warnings)

    def get_status(self) -> ProviderStatus:
        return self._status
