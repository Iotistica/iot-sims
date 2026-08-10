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
from ..integrations.azure_openai import AzureStructuredClient


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


def _build_user_prompt(
    *, device: dict, target: dict, siblings: list[dict], equipment_context: str | None,
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

    lines.append("")
    lines.append("Allowed point classes (choose exactly one name from this list, or null):")
    lines.append(_format_point_types())

    return "\n".join(lines)


def suggest_point_via_ai(
    client: AzureStructuredClient,
    *,
    device: dict,
    target: dict,
    siblings: list[dict],
    equipment_context: str | None,
) -> AiPointSuggestion:
    user_prompt = _build_user_prompt(
        device=device, target=target, siblings=siblings, equipment_context=equipment_context,
    )
    return client.parse(
        response_model=AiPointSuggestion,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )
