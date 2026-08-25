from __future__ import annotations

from dataclasses import dataclass

from .models import FaultState


@dataclass
class RuleRuntimeState:
    """
    Runtime-only state for one (device, rule) pair.

    ACTIVE faults are intentionally not auto-cleared when a rule becomes
    unevaluable because a sensor/semantic disappears. Losing evidence should
    not be interpreted as proof that the fault recovered.
    """

    state: FaultState = FaultState.NORMAL
    condition_started_at: float | None = None
    clear_started_at: float | None = None
    activated_at: float | None = None
    cleared_at: float | None = None
    last_message: str | None = None
    last_evaluable: bool = True
