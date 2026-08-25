"""Deterministic Auto Map suggestions for Simulation Model variable-to-
BACnet-point mapping. Mirrors src/semantics/suggestions.py's architecture
(rule/score -> confidence, explainable reasons, alternatives) but scores
*existing BACnet points* against a model's declared input/output
VariableDefinition instead of classifying a point's own Brick class.

Relationship/topology awareness (equipment_scope='upstream'/'downstream' on
a VariableDefinition.mapping_hints) is a bonus signal only: it prioritizes
points on related equipment (via the existing `feeds`/`isPartOf` Brick
relationships), but every code path gracefully falls back to plain device-
first/fleet-wide scoring when no relationship data exists -- most installs
have zero `feeds` edges today. Suggestion only -- nothing here writes to
the database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..semantics.mirror import find_direct_equipment_entity
from ..semantics.suggestions import confidence_for, tokens_for
from .model_store import get_output_owners_by_point
from .models.registry import ModelDefinition, VariableDefinition

EquipmentScope = Literal["self", "upstream", "downstream", "any", "fallback"]

# Coarse unit-family equivalence between a VariableDefinition's short unit
# label (e.g. "°C") and the BACnet engineering-unit strings stored on
# objects.units (e.g. "degrees-celsius"). Deliberately small -- only covers
# units actually used by the model catalog today; an unmapped variable.unit
# is simply skipped (neutral), never penalized.
_UNIT_ALIASES: dict[str, frozenset[str]] = {
    "°C": frozenset({"degrees-celsius", "degrees celsius", "degrees-fahrenheit", "degrees fahrenheit"}),
    "%": frozenset({"percent"}),
    "cfm": frozenset({"cubic-feet-per-minute", "cubic feet per minute"}),
    "kW": frozenset({"kilowatts"}),
    "ppm": frozenset({"parts-per-million", "parts per million"}),
    "kg/s": frozenset({"kilograms-per-second", "kilograms per second"}),
    "W": frozenset({"watts"}),
    # The FMU runtime outputs SI airflow, but the BACnet VAV point set often
    # exposes airflow in CFM; the provider converts at write time, so the
    # mapper should consider both unit families compatible.
    "m3/s": frozenset({
        "cubic-meters-per-second", "cubic meters per second",
        "cubic-feet-per-minute", "cubic feet per minute",
    }),
}

# Object types suitable for a model *writing* an output value into a point.
_WRITABLE_OBJECT_TYPES = frozenset({
    "analog-output", "analog-value",
    "binary-output", "binary-value",
    "multi-state-output", "multi-state-value",
})

_TOKEN_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "temp": ("temperature",),
    "airflow": ("air", "flow"),
    "flow": ("airflow",),
    "cmd": ("command",),
    "pos": ("position",),
    "sp": ("setpoint",),
    "co2": ("co2", "carbon", "dioxide"),
}


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PointCandidate:
    id: int
    name: str
    description: str | None
    units: str | None
    object_type: str | None
    device_id: int
    device_name: str
    point_type: str | None
    equipment_class: str | None
    location_match: bool = False


@dataclass(slots=True)
class MappingAlternative:
    point_id: int
    point_name: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MappingSuggestion:
    variable: str
    direction: str
    suggested_point_id: int | None
    suggested_point_name: str | None
    confidence: str
    score: float
    reasons: list[str] = field(default_factory=list)
    alternatives: list[MappingAlternative] = field(default_factory=list)
    source: str = "rule"
    equipment_scope_used: str = "self"
    related_equipment_name: str | None = None


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------

def _device_candidates(
    database: Any, device_id: int, *, location_match: bool,
) -> list[PointCandidate]:
    device = database.get_device(device_id)
    if device is None:
        return []
    objects = database.get_objects(device_id)
    device_name = device.get("name") or f"Device {device_id}"
    equipment_class = device.get("equipment_type")
    return [
        PointCandidate(
            id=int(obj["id"]),
            name=obj.get("name") or "",
            description=obj.get("description"),
            units=obj.get("units"),
            object_type=obj.get("object_type"),
            device_id=device_id,
            device_name=device_name,
            point_type=obj.get("point_type"),
            equipment_class=equipment_class,
            location_match=location_match,
        )
        for obj in objects
    ]


def _fleet_candidates(
    database: Any, *, exclude_device_id: int | None = None,
) -> list[PointCandidate]:
    candidates: list[PointCandidate] = []
    for device in database.get_devices():
        if device.get("source_type", "simulated") != "simulated":
            continue
        device_id = int(device["id"])
        if exclude_device_id is not None and device_id == exclude_device_id:
            continue
        candidates.extend(_device_candidates(database, device_id, location_match=False))
    return candidates


def _equipment_entity_for_device(database: Any, device_id: int) -> dict | None:
    with database._conn() as conn:
        return find_direct_equipment_entity(conn, device_id)


def _related_equipment_device_ids(
    database: Any, equipment_entity_id: int, relationship: str, direction: str,
) -> list[tuple[int, str]]:
    related = database.get_related_entities(equipment_entity_id, relationship, direction=direction)
    result: list[tuple[int, str]] = []
    for entity in related:
        device_id = entity.get("device_id")
        if device_id is not None:
            result.append((int(device_id), entity.get("name") or f"Equipment {entity.get('id')}"))
    return result


def discover_candidates(
    variable: VariableDefinition,
    created_from_device_id: int | None,
    database: Any,
) -> tuple[list[PointCandidate], EquipmentScope, str | None]:
    """Returns (candidates, equipment_scope_used, related_equipment_name)."""
    hints = variable.mapping_hints
    scope: str = hints.equipment_scope if hints else "self"

    if created_from_device_id is None:
        return _fleet_candidates(database), "any", None

    if scope == "any":
        return _fleet_candidates(database, exclude_device_id=None), "any", None

    if scope == "self":
        candidates = _device_candidates(database, created_from_device_id, location_match=True)
        if not candidates:
            return (
                _fleet_candidates(database, exclude_device_id=created_from_device_id),
                "fallback",
                None,
            )
        return candidates, "self", None

    # upstream / downstream
    relationship = hints.relationship if hints and hints.relationship else "feeds"
    direction = "in" if scope == "upstream" else "out"

    equipment_entity = _equipment_entity_for_device(database, created_from_device_id)
    related_device_ids: list[tuple[int, str]] = []
    if equipment_entity is not None:
        related_device_ids = _related_equipment_device_ids(
            database, int(equipment_entity["id"]), relationship, direction,
        )

    candidates: list[PointCandidate] = []
    related_name: str | None = None
    for device_id, equipment_name in related_device_ids:
        found = _device_candidates(database, device_id, location_match=True)
        if found and related_name is None:
            related_name = equipment_name
        candidates.extend(found)

    if not candidates:
        # Graceful fallback -- no relationship data (or no points on
        # related equipment): behave like plain device-first/fleet-wide.
        self_candidates = _device_candidates(database, created_from_device_id, location_match=False)
        fleet_candidates = _fleet_candidates(database, exclude_device_id=created_from_device_id)
        return self_candidates + fleet_candidates, "fallback", None

    return candidates, scope, related_name


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _expanded_tokens(value: str | None) -> set[str]:
    tokens = tokens_for(value)
    expanded = set(tokens)
    for token in tokens:
        expanded.update(_TOKEN_EXPANSIONS.get(token, ()))
    return expanded

def _score_candidate(
    variable: VariableDefinition,
    candidate: PointCandidate,
    *,
    scope_used: str,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0

    # 1. Brick point semantic compatibility.
    if variable.suggested_point_types and candidate.point_type in variable.suggested_point_types:
        score += 0.30
        reasons.append(f"Brick: {candidate.point_type}")

    # 2. Topology/relationship compatibility.
    if candidate.location_match:
        if scope_used == "upstream":
            reasons.append(f"Upstream {candidate.equipment_class or 'equipment'} ({candidate.device_name})")
        elif scope_used == "downstream":
            reasons.append(f"Downstream {candidate.equipment_class or 'equipment'} ({candidate.device_name})")
        else:
            reasons.append(f"Same device ({candidate.device_name})")
        score += 0.25

    # 3. Preferred equipment type -- a scoring preference only, never a
    # hard candidate filter (an unexpected-but-plausible equipment type
    # still surfaces as a low/medium suggestion rather than disappearing).
    hints = variable.mapping_hints
    if hints and hints.preferred_equipment_types and candidate.equipment_class in hints.preferred_equipment_types:
        score += 0.15
        reasons.append(f"Equipment: {candidate.equipment_class}")

    # 4. Unit compatibility.
    if variable.unit:
        aliases = _UNIT_ALIASES.get(variable.unit)
        if aliases and candidate.units:
            if candidate.units in aliases:
                score += 0.12
                reasons.append(f"Unit: {variable.unit}")
            else:
                score -= 0.12

    # 5. Model input/output compatibility (output ownership is filtered
    # out of the candidate pool before scoring -- see suggest_mapping_for_variable).
    if variable.direction == "output" and candidate.object_type in _WRITABLE_OBJECT_TYPES:
        score += 0.10

    # 6. Point/object metadata + normalized name/alias similarity. When the
    # variable declares no suggested_point_types at all (e.g. generic FMU
    # signals like heating_setpoint_c that have no Brick-class expectation),
    # signal #1's weight is structurally unusable for it -- so name
    # similarity gets a larger allowance in that case specifically, instead
    # of leaving an obviously-correct "Heating-SP" point stuck at "none"
    # confidence just because it can't earn the Brick-match bonus.
    label_tokens = _expanded_tokens(variable.label) | _expanded_tokens(variable.name.replace("_", " "))
    if hints and hints.signal_role:
        label_tokens |= _expanded_tokens(hints.signal_role.replace("_", " "))
    point_tokens = _expanded_tokens(candidate.name) | _expanded_tokens(candidate.description)
    overlap = label_tokens & point_tokens
    if overlap:
        if variable.suggested_point_types:
            score += min(0.14, 0.035 * len(overlap))
        else:
            score += min(0.20, 0.06 * len(overlap))
        reasons.append(f"Name matched {', '.join(sorted(overlap))}")

    score = max(0.0, min(score, 1.0))
    return score, reasons


def _fallback_reason(variable: VariableDefinition, scope_used: str) -> str | None:
    hints = variable.mapping_hints
    if scope_used == "fallback" and hints and hints.equipment_scope in ("upstream", "downstream"):
        return f"No {hints.equipment_scope} relationship found — searched device/fleet-wide"
    return None


def _candidate_label(candidate: PointCandidate) -> str:
    return f"{candidate.device_name} / {candidate.name}"


def suggest_mapping_for_variable(
    variable: VariableDefinition,
    created_from_device_id: int | None,
    database: Any,
    *,
    excluding_model_id: int | None = None,
) -> MappingSuggestion:
    candidates, scope_used, related_name = discover_candidates(
        variable, created_from_device_id, database,
    )

    if variable.direction == "output" and candidates:
        owners = get_output_owners_by_point(
            database,
            [c.id for c in candidates],
            excluding_model_id=excluding_model_id,
        )
        candidates = [c for c in candidates if c.id not in owners]

    scored = sorted(
        (
            (candidate, *_score_candidate(variable, candidate, scope_used=scope_used))
            for candidate in candidates
        ),
        key=lambda item: item[1],
        reverse=True,
    )

    fallback_reason = _fallback_reason(variable, scope_used)

    if not scored:
        return MappingSuggestion(
            variable=variable.name,
            direction=variable.direction,
            suggested_point_id=None,
            suggested_point_name=None,
            confidence="none",
            score=0.0,
            reasons=[fallback_reason] if fallback_reason else [],
            alternatives=[],
            equipment_scope_used=scope_used,
            related_equipment_name=related_name,
        )

    best_candidate, best_score, best_reasons = scored[0]

    # Mirror suggestions.py's own guard: don't hand back an over-specific
    # suggested point when the top score is still below the "low"
    # threshold -- surface it only as an alternative for manual review.
    if best_score < 0.30:
        return MappingSuggestion(
            variable=variable.name,
            direction=variable.direction,
            suggested_point_id=None,
            suggested_point_name=None,
            confidence="none",
            score=best_score,
            reasons=[fallback_reason] if fallback_reason else [],
            alternatives=[
                MappingAlternative(point_id=c.id, point_name=_candidate_label(c), score=s, reasons=r)
                for c, s, r in scored[:5]
            ],
            equipment_scope_used=scope_used,
            related_equipment_name=related_name,
        )

    reasons = list(best_reasons)
    if fallback_reason:
        reasons.append(fallback_reason)

    return MappingSuggestion(
        variable=variable.name,
        direction=variable.direction,
        suggested_point_id=best_candidate.id,
        suggested_point_name=_candidate_label(best_candidate),
        confidence=confidence_for(best_score),
        score=best_score,
        reasons=reasons,
        alternatives=[
            MappingAlternative(point_id=c.id, point_name=_candidate_label(c), score=s, reasons=r)
            for c, s, r in scored[1:6]
        ],
        equipment_scope_used=scope_used,
        related_equipment_name=related_name,
    )


def suggest_mappings_for_model(
    model_definition: ModelDefinition,
    created_from_device_id: int | None,
    database: Any,
    *,
    excluding_model_id: int | None = None,
) -> list[MappingSuggestion]:
    return [
        suggest_mapping_for_variable(
            variable,
            created_from_device_id,
            database,
            excluding_model_id=excluding_model_id,
        )
        for variable in model_definition.variables
    ]


def build_shortlist(
    variable: VariableDefinition,
    created_from_device_id: int | None,
    database: Any,
    *,
    limit: int = 8,
    excluding_model_id: int | None = None,
) -> tuple[list[PointCandidate], str, str | None]:
    """Deterministic top-K candidates for the AI fallback -- same discovery
    + scoring path as suggest_mapping_for_variable, but returned regardless
    of confidence threshold so the AI prompt always has an existing-point-
    only shortlist to choose from."""
    candidates, scope_used, related_name = discover_candidates(
        variable, created_from_device_id, database,
    )

    if variable.direction == "output" and candidates:
        owners = get_output_owners_by_point(
            database,
            [c.id for c in candidates],
            excluding_model_id=excluding_model_id,
        )
        candidates = [c for c in candidates if c.id not in owners]

    scored = sorted(
        candidates,
        key=lambda c: _score_candidate(variable, c, scope_used=scope_used)[0],
        reverse=True,
    )
    return scored[:limit], scope_used, related_name


def relationship_context_string(
    variable: VariableDefinition, scope_used: str, related_name: str | None,
) -> str | None:
    hints = variable.mapping_hints
    if scope_used in ("upstream", "downstream") and related_name:
        if scope_used == "upstream":
            return (
                f"This variable expects a signal from an upstream equipment "
                f"({related_name}), related via "
                f"{(hints.relationship if hints and hints.relationship else 'feeds')}."
            )
        return (
            f"This variable expects a signal serving downstream equipment "
            f"({related_name}), related via "
            f"{(hints.relationship if hints and hints.relationship else 'feeds')}."
        )
    if scope_used == "fallback" and hints and hints.equipment_scope in ("upstream", "downstream"):
        return (
            f"No {hints.equipment_scope} relationship found for this device; "
            "searched device/fleet-wide instead."
        )
    if scope_used == "self":
        return "This variable expects a signal from the source device itself."
    return None
