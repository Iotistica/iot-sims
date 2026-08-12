from __future__ import annotations

from typing import Any, Mapping

from .base import ProviderStatus, SimulationContext, SimulationProvider, ValidationResult


class FMUSimulationProvider(SimulationProvider):
    """Scaffold for future FMI/FMU-based coupled simulation."""

    def __init__(self) -> None:
        self._context: SimulationContext | None = None
        self._inputs: dict[int, Any] = {}
        self._status = ProviderStatus.NOT_CONFIGURED

    def initialize(self, context: SimulationContext) -> None:
        self._context = context
        self._inputs = {}
        self._status = ProviderStatus.NOT_CONFIGURED

    def start(self) -> None:
        self._status = ProviderStatus.NOT_CONFIGURED

    def pause(self) -> None:
        self._status = ProviderStatus.NOT_CONFIGURED

    def stop(self) -> None:
        self._status = ProviderStatus.NOT_CONFIGURED

    def reset(self) -> None:
        self._inputs = {}
        self._status = ProviderStatus.NOT_CONFIGURED

    def step(self, dt: float) -> None:
        self._status = ProviderStatus.NOT_CONFIGURED

    def set_inputs(self, values: Mapping[int, Any]) -> None:
        self._inputs.update(values)

    def get_outputs(self) -> Mapping[int, Any]:
        return {}

    def validate(self) -> ValidationResult:
        return ValidationResult(valid=False, errors=["No FMU configured"])

    def get_status(self) -> ProviderStatus:
        return self._status
