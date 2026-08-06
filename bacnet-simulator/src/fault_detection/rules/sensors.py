from __future__ import annotations

from collections import deque

from ..context import FaultContext
from ..models import FaultDefinition, FaultEvidence, FaultResult, FaultSeverity
from .base import FaultRule


class FrozenSensorRule(FaultRule):
    definition = FaultDefinition(
        rule_id="sensor.frozen_value",
        name="Frozen sensor value",
        equipment_type="*",
        description="A configured sensor value has not changed for the configured duration.",
        persistence_seconds=0.0,
        clear_seconds=30.0,
        severity=FaultSeverity.WARNING,
    )

    def __init__(self) -> None:
        self._samples: dict[tuple[int, str], deque[tuple[float, float]]] = {}

    def evaluate(self, context: FaultContext) -> FaultResult:
        point_type = str(context.parameters.get("frozen_sensor_point_type", ""))
        if not point_type:
            return FaultResult(False, "frozen_sensor_point_type is not configured.", self.definition.severity, evaluable=False)
        point = context.point(point_type)
        if point is None or not isinstance(point.value, (int, float)):
            return FaultResult(False, f"Point {point_type!r} unavailable or non-numeric.", self.definition.severity, evaluable=False)
        window = float(context.parameters.get("frozen_window_seconds", 300.0))
        epsilon = float(context.parameters.get("frozen_epsilon", 0.01))
        key = (context.device_id, point_type)
        samples = self._samples.setdefault(key, deque(maxlen=500))
        samples.append((context.timestamp, float(point.value)))
        while samples and context.timestamp - samples[0][0] > window:
            samples.popleft()
        if len(samples) < 2:
            return FaultResult(False, "Not enough history to evaluate frozen sensor.", self.definition.severity, evaluable=False)
        duration = samples[-1][0] - samples[0][0]
        values = [value for _, value in samples]
        spread = max(values) - min(values)
        active = duration >= window and spread <= epsilon
        return FaultResult(
            condition_present=active,
            message=f"{point_type} changed by only {spread:.4f} over {duration:.1f} seconds.",
            severity=self.definition.severity,
            evidence=[
                FaultEvidence(point_type, point.value),
                FaultEvidence("sample-spread", spread, f"> {epsilon}"),
                FaultEvidence("sample-window-seconds", duration, f">= {window}"),
            ],
        )
