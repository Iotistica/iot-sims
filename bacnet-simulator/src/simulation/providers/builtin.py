from __future__ import annotations

from typing import Any, Mapping

from ...bacnet.app import normalize_present_value
from ..behaviors import FaultBehavior, ManualBehavior, RandomWalkBehavior, make_behavior
from ..state import SimState
from .base import (
    PointConfig,
    ProviderStatus,
    SimulationContext,
    SimulationProvider,
    ValidationResult,
)


_KNOWN_BEHAVIORS = {
    "constant",
    "sine",
    "noise",
    "random_walk",
    "manual",
    "schedule",
    "ramp",
    "fault",
}


class BuiltInSimulationProvider(SimulationProvider):
    """
    Adapter around the existing built-in Behavior engine (src/simulation/
    behaviors.py) -- new provider architecture, existing Behavior
    implementations reused directly.
    """

    def __init__(self) -> None:
        self._context: SimulationContext | None = None
        self._state: Any = None
        self._behaviors: dict[int, Any] = {}
        self._outputs: dict[int, Any] = {}
        self._status = ProviderStatus.NOT_CONFIGURED

    def _new_state(self) -> Any:
        return SimState()

    def _make_behavior(self, point: PointConfig) -> Any:
        return make_behavior(
            point.behavior,
            point.behavior_params,
            point.manual_value,
        )

    def _build_behaviors(self) -> None:
        """Build fresh Behavior instances from the current PointConfig values."""
        self._behaviors = {}

        if self._context is None:
            return

        for point in self._context.point_configs:
            self._behaviors[point.point_id] = self._make_behavior(point)

    def initialize(self, context: SimulationContext) -> None:
        self._context = context
        self._state = self._new_state()
        self._outputs = {}
        self._build_behaviors()
        self._status = ProviderStatus.READY

    def start(self) -> None:
        if self._status in (
            ProviderStatus.READY,
            ProviderStatus.PAUSED,
        ):
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
        """
        Perform a true simulation reset.

        Recreate state and Behavior instances so stateful behavior internals
        such as random-walk position, fault state, and manual runtime state do
        not unintentionally survive a reset.
        """
        self._outputs = {}

        if self._context is None:
            self._state = None
            self._behaviors = {}
            self._status = ProviderStatus.NOT_CONFIGURED
            return

        self._state = self._new_state()
        self._build_behaviors()
        self._status = ProviderStatus.READY

    def step(self, dt: float) -> None:
        if self._status != ProviderStatus.RUNNING:
            return

        if self._context is None or self._state is None:
            self._status = ProviderStatus.ERROR
            return

        if dt < 0:
            self._status = ProviderStatus.ERROR
            return

        self._state.elapsed_seconds += dt
        self._state.time_of_day = (
            self._state.time_of_day + dt / 3600.0
        ) % 24.0

        point_by_id = {
            point.point_id: point
            for point in self._context.point_configs
        }

        for point_id, behavior in self._behaviors.items():
            point = point_by_id.get(point_id)
            if point is None:
                continue

            raw_value = behavior.compute(self._state)

            self._outputs[point_id] = normalize_present_value(
                point.object_type,
                raw_value,
            )

    def set_inputs(self, values: Mapping[int, Any]) -> None:
        for point_id, value in values.items():
            behavior = self._behaviors.get(point_id)

            if isinstance(behavior, ManualBehavior):
                behavior.set(value)

    def get_outputs(self) -> Mapping[int, Any]:
        return dict(self._outputs)

    def validate(self) -> ValidationResult:
        if self._context is None:
            return ValidationResult(
                valid=False,
                errors=["Provider not initialized"],
            )

        warnings = [
            (
                f"Unknown behavior '{point.behavior}' "
                f"on point {point.point_id}; legacy fallback will be used."
            )
            for point in self._context.point_configs
            if point.behavior not in _KNOWN_BEHAVIORS
        ]

        return ValidationResult(
            valid=True,
            warnings=warnings,
        )

    def get_status(self) -> ProviderStatus:
        return self._status

    def sync_point_configs(
        self,
        point_configs: list[PointConfig],
        *,
        remove_missing: bool = True,
    ) -> None:
        """
        Reconcile live point configuration with provider behaviors.

        - unchanged config -> keep existing Behavior instance/state
        - changed config -> rebuild behavior, preserving compatible runtime state
        - new config -> create behavior
        - missing config -> remove only when remove_missing=True
        """
        if self._context is None:
            return

        old_configs = {
            point.point_id: point
            for point in self._context.point_configs
        }

        old_behaviors = dict(self._behaviors)

        incoming = {
            point.point_id: point
            for point in point_configs
        }

        if remove_missing:
            target_ids = set(incoming)
        else:
            target_ids = set(old_configs) | set(incoming)

        new_behaviors: dict[int, Any] = {}

        for point_id in target_ids:
            point = incoming.get(point_id)

            # Keep old config/behavior if this point wasn't included
            # and remove_missing=False.
            if point is None:
                old_behavior = old_behaviors.get(point_id)

                if old_behavior is not None:
                    new_behaviors[point_id] = old_behavior

                continue

            old_point = old_configs.get(point_id)
            old_behavior = old_behaviors.get(point_id)

            # Exact same configuration: preserve existing runtime instance.
            if (
                old_point == point
                and old_behavior is not None
            ):
                new_behaviors[point_id] = old_behavior
                continue

            # New point or changed configuration.
            new_behavior = self._make_behavior(point)

            if old_behavior is not None:
                if (
                    isinstance(new_behavior, ManualBehavior)
                    and isinstance(old_behavior, ManualBehavior)
                ):
                    new_behavior.set(old_behavior._value)

                elif (
                    isinstance(new_behavior, FaultBehavior)
                    and isinstance(old_behavior, FaultBehavior)
                ):
                    new_behavior._fault_active = old_behavior._fault_active
                    new_behavior._fault_end_elapsed = (
                        old_behavior._fault_end_elapsed
                    )
                    new_behavior._inner = old_behavior._inner

                elif (
                    isinstance(new_behavior, RandomWalkBehavior)
                    and isinstance(old_behavior, RandomWalkBehavior)
                ):
                    new_behavior._value = old_behavior._value

            new_behaviors[point_id] = new_behavior

        self._behaviors = new_behaviors

        if remove_missing:
            merged_configs = list(point_configs)
        else:
            merged = dict(old_configs)
            merged.update(incoming)
            merged_configs = list(merged.values())

        self._context = SimulationContext(
            participant_device_ids=list(
                self._context.participant_device_ids
            ),
            point_configs=merged_configs,
            metadata=dict(self._context.metadata),
        )

    def sync_point_config(
    self,
    point_config: PointConfig,
    ) -> None:
        self.sync_point_configs(
            [point_config],
            remove_missing=False,
        )