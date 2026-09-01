from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Literal


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


@dataclass(frozen=True)
class MappingHints:
    """Optional Auto Map guidance for a single model variable: where the
    signal is expected to come from/go to (relative to the model's source
    device) and what BACnet relationship to follow to find it. Purely
    advisory -- consumed only by src/simulation/mapping/suggestions.py;
    never a hard filter, and never required (a variable with no hints
    behaves exactly like Revision 1: device-first, then fleet-wide).
    """

    equipment_scope: Literal["self", "upstream", "downstream", "any"] = "self"
    preferred_equipment_types: tuple[str, ...] = ()
    relationship: Literal["feeds", "isPartOf"] | None = None
    signal_role: str | None = None


@dataclass(frozen=True)
class VariableDefinition:
    name: str
    label: str
    direction: str
    unit: str | None = None
    default: Any = None
    required: bool = True
    suggested_point_types: tuple[str, ...] = ()
    mapping_hints: MappingHints | None = None


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

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "model_type": self.model_type,
            "label": self.label,
            "provider_type": self.provider_type,
            "description": self.description,
            "parameters": [asdict(p) for p in self.parameters],
            "inputs": [asdict(v) for v in self.variables if v.direction == "input"],
            "outputs": [asdict(v) for v in self.variables if v.direction == "output"],
        }


