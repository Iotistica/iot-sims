from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .base import ProviderStatus, SimulationContext, SimulationProvider, ValidationResult


class SystemModel(Protocol):
    def reset(self) -> None: ...
    def set_inputs(self, values: Mapping[str, Any]) -> None: ...
    def step(self, dt: float) -> None: ...
    def get_outputs(self) -> Mapping[str, Any]: ...
    def validate(self) -> ValidationResult: ...


@dataclass(frozen=True)
class PointBinding:
    point_id: int
    variable: str
    direction: str  # 'input' or 'output'


class SystemSimulationProvider(SimulationProvider):
    """Runs one reusable Python system model and maps simulator points to it."""

    def __init__(self, model: SystemModel, bindings: list[PointBinding]) -> None:
        self._model = model
        self._bindings = bindings
        self._context: SimulationContext | None = None
        self._point_inputs: dict[int, Any] = {}
        self._outputs: dict[int, Any] = {}
        self._status = ProviderStatus.NOT_CONFIGURED

    def initialize(self, context: SimulationContext) -> None:
        self._context = context
        self._point_inputs = {}
        self._outputs = {}
        self._model.reset()
        self._status = ProviderStatus.READY if self.validate().valid else ProviderStatus.NOT_CONFIGURED

    def start(self) -> None:
        if self._status in (ProviderStatus.READY, ProviderStatus.PAUSED):
            self._status = ProviderStatus.RUNNING

    def pause(self) -> None:
        if self._status == ProviderStatus.RUNNING:
            self._status = ProviderStatus.PAUSED

    def stop(self) -> None:
        self._status = ProviderStatus.STOPPED if self._context is not None else ProviderStatus.NOT_CONFIGURED

    def reset(self) -> None:
        self._point_inputs = {}
        self._outputs = {}
        self._model.reset()
        self._status = ProviderStatus.READY if self._context is not None and self.validate().valid else ProviderStatus.NOT_CONFIGURED

    def step(self, dt: float) -> None:
        if self._status != ProviderStatus.RUNNING:
            return
        if dt <= 0:
            self._status = ProviderStatus.ERROR
            return

        model_inputs: dict[str, Any] = {}
        for binding in self._bindings:
            if binding.direction == "input" and binding.point_id in self._point_inputs:
                model_inputs[binding.variable] = self._point_inputs[binding.point_id]

        self._model.set_inputs(model_inputs)
        self._model.step(dt)
        model_outputs = dict(self._model.get_outputs())

        self._outputs = {
            binding.point_id: model_outputs[binding.variable]
            for binding in self._bindings
            if binding.direction == "output" and binding.variable in model_outputs
        }

    def set_inputs(self, values: Mapping[int, Any]) -> None:
        self._point_inputs.update(values)

    def get_outputs(self) -> Mapping[int, Any]:
        return dict(self._outputs)

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        for binding in self._bindings:
            if binding.direction not in {"input", "output"}:
                errors.append(f"Invalid direction {binding.direction!r} for point {binding.point_id}")

        model_result = self._model.validate()
        errors.extend(model_result.errors)
        return ValidationResult(valid=not errors and model_result.valid, errors=errors, warnings=list(model_result.warnings))

    def get_status(self) -> ProviderStatus:
        return self._status
