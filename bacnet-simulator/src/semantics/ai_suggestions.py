"""AI fallback for a single point's Brick classification -- used only when
the deterministic engine (suggestions.py) returns low/none confidence,
triggered explicitly per-point via "Use AI" in the review UI. Never runs
automatically, never writes to the database, never touches BACnet.

Vocabulary enforcement is NOT delegated to the model's response schema (a
dynamically-built Literal of all ~59 POINT_TYPES keys was considered, but
the openai/Azure structured-output stack's behavior with that shape hasn't
been verified against this repo's actual pinned versions, and this is a
fallback feature, not core functionality -- not worth the risk). The
canonical list is given to the model as plain prompt content ("choose only
from this list, or null") as soft guidance; the single AUTHORITATIVE check
is server-side, in the caller (src/api/routers/semantic_suggestions.py),
immediately after parse() returns: suggested_class is None or
suggested_class in POINT_TYPES. See that route for the enforcement point --
this module never claims to guarantee vocabulary membership on its own.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from ..core.config import POINT_TYPES
from ..integrations.llm import StructuredLLMClient
from .suggestions import tokens_for


class AiPointSuggestion(BaseModel):
    suggested_class: Optional[str] = None
    confidence: Literal["high", "medium", "low", "none"]
    reason: str


_SYSTEM_PROMPT = """You are a BACnet/Brick building-automation classification assistant.

Given a single BACnet point (with its parent device and neighboring points
for context), classify it into exactly one Brick point class from the
provided canonical list, or return null if none genuinely fits.

Rules:
- Choose suggested_class ONLY from the "Allowed point classes" list given in
  the user message, using the exact class name shown (e.g.
  "Supply_Air_Temperature_Sensor"), or null if none of them are a good fit.
  Never invent a class name that is not in that list.
- confidence must be one of: high, medium, low, none. Use "none" whenever
  suggested_class is null.
- reason must be a short, concrete, human-readable explanation (one or two
  sentences) referencing the actual evidence you used (point name, units,
  object type, parent equipment, neighboring points) -- not a generic
  statement.
- Do not guess wildly from a bare abbreviation with no other supporting
  evidence -- prefer null/low confidence over a confident-sounding but
  unsupported guess.
"""


def _format_point_types() -> str:
    return "\n".join(f"- {name} ({label})" for name, label in sorted(POINT_TYPES.items()))


# How many already-classified points to surface as few-shot grounding, and
# the minimum similarity score (see _score_similarity) to bother including
# one at all -- an unrelated point is worse than no example.
_MAX_SIMILAR_EXAMPLES = 5


def _score_similarity(target: dict, candidate: dict) -> int:
    """Cheap, explainable similarity -- name-token overlap dominates (reuses
    the same tokens_for()/TOKEN_ALIASES the deterministic engine scores
    with, so "SAT" and "Supply-Air-Temp" still overlap), units/object_type
    matches are secondary tie-breakers. Not the same scoring as suggestions.
    py's own rule engine (that scores a point against a Brick rule; this
    scores a point against another point) -- deliberately simpler, since
    this only ever ranks candidates against each other, never against a
    confidence threshold."""
    name_overlap = len(tokens_for(target.get("name")) & tokens_for(candidate.get("name")))
    units_match = 1 if target.get("units") and target.get("units") == candidate.get("units") else 0
    type_match = 1 if target.get("object_type") == candidate.get("object_type") else 0
    return name_overlap * 2 + units_match + type_match


def _find_similar_examples(target: dict, classified_points: list[dict]) -> list[dict]:
    scored = [(c, _score_similarity(target, c)) for c in classified_points]
    scored = [(c, score) for c, score in scored if score > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [c for c, _score in scored[:_MAX_SIMILAR_EXAMPLES]]


def _build_user_prompt(
    *, device: dict, target: dict, siblings: list[dict], equipment_context: str | None,
    similar_examples: list[dict],
) -> str:
    lines: list[str] = []

    lines.append("Device:")
    lines.append(f"  name: {device.get('name')}")
    lines.append(f"  description: {device.get('description') or '(none)'}")
    lines.append(f"  vendor: {device.get('vendor_name') or '(unknown)'}")
    lines.append(f"  model: {device.get('model_name') or '(unknown)'}")
    lines.append(f"  equipment type (current/inferred): {equipment_context or '(unclassified)'}")

    lines.append("")
    lines.append("Target point to classify:")
    lines.append(f"  name: {target.get('name')}")
    lines.append(f"  object_type: {target.get('object_type')}")
    lines.append(f"  units: {target.get('units') or '(none)'}")
    lines.append(f"  description: {target.get('description') or '(none)'}")

    if siblings:
        lines.append("")
        lines.append("Other points on the same device (context only, do not classify these):")
        for s in siblings:
            lines.append(f"  - {s.get('name')} ({s.get('object_type')}, units={s.get('units') or 'none'})")

    if similar_examples:
        lines.append("")
        lines.append(
            "Similar points already classified elsewhere in this project (real examples, "
            "for reference -- weigh them alongside the target's own name/units/context, "
            "don't copy one blindly if the target clearly differs):"
        )
        for ex in similar_examples:
            lines.append(f"  - \"{ex.get('name')}\" ({ex.get('object_type')}, units={ex.get('units') or 'none'}) -> {ex.get('point_type')}")

    lines.append("")
    lines.append("Allowed point classes (choose exactly one name from this list, or null):")
    lines.append(_format_point_types())

    return "\n".join(lines)


def suggest_point_via_ai(
    client: StructuredLLMClient,
    *,
    device: dict,
    target: dict,
    siblings: list[dict],
    equipment_context: str | None,
    classified_points: list[dict] | None = None,
) -> AiPointSuggestion:
    similar_examples = _find_similar_examples(target, classified_points or [])
    user_prompt = _build_user_prompt(
        device=device, target=target, siblings=siblings, equipment_context=equipment_context,
        similar_examples=similar_examples,
    )
    return client.parse(
        response_model=AiPointSuggestion,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )
