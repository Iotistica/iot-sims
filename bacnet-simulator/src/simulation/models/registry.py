from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from ...semantics.contract import SemanticContract


@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    label: str
    type: str = "number"
    default: Any = None
    unit: str | None = None
    required: bool = False
    advanced: bool = False
    minimum: float | None = None
    maximum: float | None = None
    # A fixed, enumerated set of legal values (e.g. a genset's fuelType: 1=diesel,
    # 2=natural gas) -- each entry {"value": ..., "label": ...}. None means "free
    # entry", the existing behavior for every parameter without one. Presence of
    # this (not `type`) is what tells SimulationModelDrawer.vue to render a select
    # instead of a free-text/number box -- see _parameter_definition's own comment.
    allowed_values: tuple[dict[str, Any], ...] | None = None


@dataclass(frozen=True)
class VariableDefinition:
    name: str
    label: str
    direction: str
    unit: str | None = None
    default: Any = None
    required: bool = True
    # The ONE authoritative domain-meaning contract, parsed from model.json's `semantic`
    # block. None when the model.json entry has no `semantic.id` (not yet described).
    # (There is no `mapping_hints` -- it was removed entirely in the clean break.)
    semantic: SemanticContract | None = None


@dataclass(frozen=True)
class ModelDefinition:
    model_type: str
    label: str
    provider_type: str
    description: str
    parameters: tuple[ParameterDefinition, ...]
    variables: tuple[VariableDefinition, ...]
    factory: Callable[[dict[str, Any]], Any]
    runtime_model: str | None = None
    # Only set for provider_type == "classifier" -- the fixed, ordered set
    # of class names this model's single output predicts (index i of a
    # /step() prediction_index maps to class_labels[i]), and which
    # recording field ("condition"/"fault_type") it was trained against.
    # Model-level, not per-variable, since a classifier has exactly one
    # synthetic output declaring these values -- see
    # engine.py's classifier attach/output-mapping code, which sizes and
    # labels the auto-created BACnet MultiStateValue from these.
    class_labels: tuple[str, ...] = ()
    classifier_target: str | None = None
    # The promoted ML model's persisted input feature contract (one entry per base model
    # input: {variable, semantic, expected_unit, identity_kind, ...}) -- see iot-models'
    # shared/runtime/models/contract.py. Empty for an FMU model or a pre-contract
    # surrogate. Used by FMUSimulationProvider to normalise each live BACnet value into
    # the model's expected engineering unit before /step.
    ml_input_contract: tuple[dict[str, Any], ...] = ()

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "model_type": self.model_type,
            "label": self.label,
            "provider_type": self.provider_type,
            "description": self.description,
            "parameters": [asdict(p) for p in self.parameters],
            "inputs": [asdict(v) for v in self.variables if v.direction == "input"],
            "outputs": [asdict(v) for v in self.variables if v.direction == "output"],
            "class_labels": list(self.class_labels) or None,
            "classifier_target": self.classifier_target,
        }


