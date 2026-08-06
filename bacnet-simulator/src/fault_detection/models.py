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
    device_id: int
    rule_id: str
    state: FaultState
    previous_state: FaultState
    message: str
    severity: FaultSeverity
    evidence: list[FaultEvidence]
    timestamp: float
    activated_at: float | None = None
    cleared_at: float | None = None
