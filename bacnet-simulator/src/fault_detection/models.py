from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FaultSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class FaultState(StrEnum):
    NORMAL = "normal"
    PENDING = "pending"
    ACTIVE = "active"
    CLEARED = "cleared"


@dataclass(frozen=True)
class FaultEvidence:
    point: str
    value: Any
    expected: str | None = None


@dataclass(frozen=True)
class FaultDefinition:
    rule_id: str
    name: str
    equipment_type: str
    description: str
    persistence_seconds: float
    clear_seconds: float
    severity: FaultSeverity


@dataclass(frozen=True)
class FaultResult:
    condition_present: bool
    message: str
    severity: FaultSeverity
    evidence: list[FaultEvidence] = field(default_factory=list)
    evaluable: bool = True


@dataclass(frozen=True)
class FaultEvaluation:
    """
    Stateful result returned by the FDD engine.

    evaluable:
        False means the rule could not currently be evaluated because one or
        more required canonical semantics/points were unavailable. This is
        deliberately different from condition_present=False.

    condition_started_at:
        Timestamp when the currently abnormal condition first began. For an
        ACTIVE fault this may precede activated_at by persistence_seconds.

    activated_at:
        Timestamp when the rule satisfied its persistence requirement and
        became ACTIVE.

    cleared_at:
        Timestamp when an ACTIVE fault satisfied its clear-time requirement.
    """

    device_id: int
    rule_id: str
    state: FaultState
    previous_state: FaultState
    message: str
    severity: FaultSeverity
    evidence: list[FaultEvidence]
    timestamp: float

    evaluable: bool = True
    condition_started_at: float | None = None
    activated_at: float | None = None
    cleared_at: float | None = None
