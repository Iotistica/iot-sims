"""AI fallback for a single Simulation Model variable's BACnet point
mapping -- used only when the deterministic engine (mapping/suggestions.py)
returns low/none confidence for a variable, triggered explicitly per-row
via "Use AI" in the review UI. Never runs automatically, never writes to
the database.

Mirrors src/semantics/ai_suggestions.py's vocabulary-enforcement pattern:
the model is given a deterministic, existing-points-only shortlist and
told to choose a point_id from it (or null), but that is soft guidance
only -- the single AUTHORITATIVE check is server-side, in the caller
(src/api/routers/simulation.py), immediately after parse() returns:
point_id is None or point_id is in the shortlist's id set. AI must never
be able to invent a point that isn't already in the system.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from ...integrations.llm import StructuredLLMClient
from .suggestions import PointCandidate
from ..models.registry import ModelDefinition, VariableDefinition


class AiMappingSuggestion(BaseModel):
    point_id: Optional[int] = None
    confidence: Literal["high", "medium", "low", "none"]
    reason: str


_SYSTEM_PROMPT = """You are a BACnet/Brick building-automation point-mapping assistant.

Given a simulation model variable (an input or output signal) and a
shortlist of candidate BACnet points already present in the system, select
the single best candidate point_id for this variable, or return null if
none of the candidates genuinely fit.

Rules:
- Choose point_id ONLY from the "Candidate points" list given in the user
  message, using its exact point_id integer, or null if none of them are a
  good fit. Never invent a point_id that is not in that list.
- confidence must be one of: high, medium, low, none. Use "none" whenever
  point_id is null.
- reason must be a short, concrete, human-readable explanation (one or two
  sentences) referencing the actual evidence you used (Brick point class,
  relationship/topology context, units, point name) -- not a generic
  statement.
- Do not guess wildly -- prefer null/low confidence over a confident-
  sounding but unsupported guess.
"""


def _format_candidates(candidates: list[PointCandidate]) -> str:
    lines: list[str] = []
    for c in candidates:
        lines.append(
            f"- point_id={c.id}: \"{c.device_name} / {c.name}\" "
            f"(object_type={c.object_type or 'unknown'}, units={c.units or 'none'}, "
            f"brick_class={c.point_type or 'unclassified'}, "
            f"equipment={c.equipment_class or 'unclassified'})"
        )
    return "\n".join(lines) if lines else "(no candidates found)"


def _build_user_prompt(
    *,
    variable: VariableDefinition,
    model_definition: ModelDefinition,
    equipment_context: str | None,
    relationship_context: str | None,
    candidates: list[PointCandidate],
) -> str:
    hints = variable.mapping_hints
    lines: list[str] = []
    lines.append(f"Simulation model: {model_definition.label} ({model_definition.model_type})")
    lines.append(f"Variable: {variable.label} ({variable.direction}:{variable.name})")
    lines.append(f"Expected unit: {variable.unit or '(none)'}")
    if variable.suggested_point_types:
        lines.append(f"Expected Brick point class(es): {', '.join(variable.suggested_point_types)}")
    if hints:
        lines.append(f"Expected equipment scope: {hints.equipment_scope}")
        if hints.preferred_equipment_types:
            lines.append(f"Preferred equipment type(s): {', '.join(hints.preferred_equipment_types)}")
        if hints.signal_role:
            lines.append(f"Signal role: {hints.signal_role}")
    lines.append(f"Source equipment context: {equipment_context or '(unclassified)'}")
    if relationship_context:
        lines.append(f"Relationship/topology context: {relationship_context}")

    lines.append("")
    lines.append("Candidate points (choose exactly one point_id from this list, or null):")
    lines.append(_format_candidates(candidates))

    return "\n".join(lines)


def suggest_point_for_variable_via_ai(
    client: StructuredLLMClient,
    *,
    variable: VariableDefinition,
    model_definition: ModelDefinition,
    equipment_context: str | None,
    relationship_context: str | None,
    candidates: list[PointCandidate],
) -> AiMappingSuggestion:
    user_prompt = _build_user_prompt(
        variable=variable,
        model_definition=model_definition,
        equipment_context=equipment_context,
        relationship_context=relationship_context,
        candidates=candidates,
    )
    return client.parse(
        response_model=AiMappingSuggestion,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )
