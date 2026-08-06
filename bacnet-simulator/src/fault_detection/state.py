from __future__ import annotations

from dataclasses import dataclass

from .models import FaultState


@dataclass
class RuleRuntimeState:
    state: FaultState = FaultState.NORMAL
    condition_started_at: float | None = None
    clear_started_at: float | None = None
    activated_at: float | None = None
    cleared_at: float | None = None
    last_message: str | None = None
