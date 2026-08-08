from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class CommissioningStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class CommissioningCheck:
    name: str
    status: CommissioningStatus
    message: str = ""
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommissioningResult:
    test_id: str
    device_id: int
    equipment_type: str
    status: CommissioningStatus
    checks: list[CommissioningCheck] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "device_id": self.device_id,
            "equipment_type": self.equipment_type,
            "status": self.status.value,
            "checks": [
                {**check.to_dict(), "status": check.status.value}
                for check in self.checks
            ],
            "measurements": self.measurements,
            "warnings": self.warnings,
        }
