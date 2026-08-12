from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ProviderStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PointConfig:
    point_id: int
    behavior: str
    behavior_params: str
    manual_value: Any = None
    object_type: str = "analog-value"


@dataclass
class SimulationContext:
    """Protocol/Brick-neutral context for one provider instance."""
    participant_device_ids: list[int] = field(default_factory=list)
    point_configs: list[PointConfig] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class SimulationProvider(ABC):
    """Protocol-agnostic contract for simulation value generation."""

    @abstractmethod
    def initialize(self, context: SimulationContext) -> None: ...
    @abstractmethod
    def start(self) -> None: ...
    @abstractmethod
    def pause(self) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...
    @abstractmethod
    def reset(self) -> None: ...
    @abstractmethod
    def step(self, dt: float) -> None: ...
    @abstractmethod
    def set_inputs(self, values: Mapping[int, Any]) -> None: ...
    @abstractmethod
    def get_outputs(self) -> Mapping[int, Any]: ...
    @abstractmethod
    def validate(self) -> ValidationResult: ...
    @abstractmethod
    def get_status(self) -> ProviderStatus: ...
