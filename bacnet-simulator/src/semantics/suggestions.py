"""Deterministic, rule-based Brick classification suggestions for a device
and its objects -- works identically regardless of source_type (simulated
or external-bacnet); nothing here reads or cares about that field. Callers
build a DeviceSnapshot/PointSnapshot from whatever persisted Device/
SimObject rows they already have (see src/api/routers/semantic_suggestions.py
for the adapter) and get back ranked, explainable candidates. Suggestion
only -- nothing in this module writes to the database; every brick_class it
can ever return is asserted against the app's real EQUIPMENT_TYPES/
POINT_TYPES vocabulary at import time (see the bottom of this file), so a
rule referencing a retired/renamed/invented class fails loudly at startup
instead of silently producing an unusable suggestion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from ..core.config import EQUIPMENT_TYPES, POINT_TYPES


# ---------------------------------------------------------------------------
# Input / output models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PointSnapshot:
    object_type: str
    object_instance: int
    object_name: str
    units: str | None = None
    description: str | None = None


@dataclass(slots=True)
class DeviceSnapshot:
    device_instance: int
    name: str
    vendor_name: str | None = None
    model_name: str | None = None
    description: str | None = None
    points: list[PointSnapshot] = field(default_factory=list)


@dataclass(slots=True)
class SemanticCandidate:
    brick_class: str
    score: float
    reasons: list[str]


@dataclass(slots=True)
class SemanticSuggestion:
    source_kind: str                   # "device" | "point"
    source_key: str
    source_name: str

    suggested_class: str | None
    confidence: str                    # "high" | "medium" | "low" | "none"
    score: float

    reasons: list[str]
    alternatives: list[SemanticCandidate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "sat": ("supply", "air", "temperature"),
    "sa_temp": ("supply", "air", "temperature"),
    "supply_temp": ("supply", "air", "temperature"),

    "rat": ("return", "air", "temperature"),
    "ra_temp": ("return", "air", "temperature"),

    "oat": ("outside", "air", "temperature"),
    "oa_temp": ("outside", "air", "temperature"),

    "mat": ("mixed", "air", "temperature"),

    "zat": ("zone", "air", "temperature"),
    "zt": ("zone", "temperature"),
    "zone_temp": ("zone", "temperature"),

    "chwst": ("chilled", "water", "supply", "temperature"),
    "chws": ("chilled", "water", "supply"),
    "chwrt": ("chilled", "water", "return", "temperature"),
    "chwr": ("chilled", "water", "return"),

    "hwst": ("hot", "water", "supply", "temperature"),
    "hwrt": ("hot", "water", "return", "temperature"),

    "sf": ("supply", "fan"),
    "rf": ("return", "fan"),

    "sp": ("setpoint",),
    "cmd": ("command",),
    "pos": ("position",),
    "fb": ("feedback",),

    "dp": ("differential", "pressure"),

    # Electrical / meter shorthand. These reinforce obvious meter-point names
    # when descriptions or engineering units are incomplete.
    "kwh": ("energy",),
}


def normalize_text(value: str | None) -> str:
    if not value:
        return ""

    value = value.lower()
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def tokens_for(value: str | None) -> set[str]:
    normalized = normalize_text(value)

    if not normalized:
        return set()

    raw_tokens = normalized.split()
    result = set(raw_tokens)

    # Also examine collapsed forms such as "sat", "chwst".
    collapsed = "_".join(raw_tokens)

    for alias, expansion in TOKEN_ALIASES.items():
        alias_normalized = alias.replace("_", " ")

        if (
            alias in raw_tokens
            or alias_normalized in normalized
            or alias == collapsed
        ):
            result.update(expansion)

    return result


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SemanticRule:
    brick_class: str

    required_any: frozenset[str] = frozenset()
    preferred: frozenset[str] = frozenset()

    units: frozenset[str] = frozenset()
    object_types: frozenset[str] = frozenset()

    equipment_classes: frozenset[str] = frozenset()

    base_score: float = 0.0


POINT_RULES: tuple[SemanticRule, ...] = (
    SemanticRule(
        brick_class="Supply_Air_Temperature_Sensor",
        required_any=frozenset({"temperature", "temp"}),
        preferred=frozenset({"supply", "air"}),
        units=frozenset({"degrees-celsius", "degrees-fahrenheit"}),
        object_types=frozenset({"analog-input"}),
        equipment_classes=frozenset({"Air_Handling_Unit"}),
        base_score=0.25,
    ),

    SemanticRule(
        brick_class="Return_Air_Temperature_Sensor",
        required_any=frozenset({"temperature", "temp"}),
        preferred=frozenset({"return", "air"}),
        units=frozenset({"degrees-celsius", "degrees-fahrenheit"}),
        object_types=frozenset({"analog-input"}),
        equipment_classes=frozenset({"Air_Handling_Unit"}),
        base_score=0.25,
    ),

    SemanticRule(
        brick_class="Outside_Air_Temperature_Sensor",
        required_any=frozenset({"temperature", "temp"}),
        preferred=frozenset({"outside", "air"}),
        units=frozenset({"degrees-celsius", "degrees-fahrenheit"}),
        object_types=frozenset({"analog-input"}),
        base_score=0.25,
    ),

    SemanticRule(
        brick_class="Zone_Air_Temperature_Sensor",
        required_any=frozenset({"temperature", "temp"}),
        preferred=frozenset({"zone"}),
        units=frozenset({"degrees-celsius", "degrees-fahrenheit"}),
        object_types=frozenset({"analog-input"}),
        equipment_classes=frozenset({"Variable_Air_Volume_Box"}),
        base_score=0.25,
    ),

    SemanticRule(
        brick_class="Air_Flow_Sensor",
        required_any=frozenset({"flow", "airflow"}),
        preferred=frozenset({"air", "supply", "zone"}),
        object_types=frozenset({"analog-input"}),
        base_score=0.20,
    ),

    SemanticRule(
        brick_class="Static_Pressure_Sensor",
        required_any=frozenset({"pressure"}),
        preferred=frozenset({"static"}),
        object_types=frozenset({"analog-input"}),
        equipment_classes=frozenset({"Air_Handling_Unit"}),
        base_score=0.20,
    ),

    SemanticRule(
        brick_class="Differential_Pressure_Sensor",
        required_any=frozenset({"pressure"}),
        preferred=frozenset({"differential", "water"}),
        object_types=frozenset({"analog-input"}),
        base_score=0.20,
    ),

    SemanticRule(
        brick_class="Fan_Status",
        required_any=frozenset({"fan", "run", "status"}),
        preferred=frozenset({"fan", "run"}),
        object_types=frozenset({"binary-input", "binary-value"}),
        base_score=0.20,
    ),

    SemanticRule(
        brick_class="Fan_Speed_Command",
        required_any=frozenset({"fan", "speed"}),
        preferred=frozenset({"speed", "command"}),
        units=frozenset({"percent"}),
        object_types=frozenset({"analog-output", "analog-value"}),
        base_score=0.20,
    ),

    SemanticRule(
        brick_class="Damper_Position_Command",
        required_any=frozenset({"damper"}),
        preferred=frozenset({"position", "command"}),
        units=frozenset({"percent"}),
        object_types=frozenset({"analog-output", "analog-value"}),
        base_score=0.25,
    ),

    SemanticRule(
        brick_class="Valve_Position_Command",
        required_any=frozenset({"valve"}),
        preferred=frozenset({"position", "command"}),
        units=frozenset({"percent"}),
        object_types=frozenset({"analog-output", "analog-value"}),
        base_score=0.25,
    ),

    # Electrical / utility meter points. Units are especially strong evidence
    # here: kilowatts are power, while kilowatt-hours are accumulated energy.
    SemanticRule(
        brick_class="Energy_Sensor",
        required_any=frozenset({"energy", "kwh"}),
        preferred=frozenset({"total", "energy", "kwh"}),
        units=frozenset({"kilowatt-hours"}),
        object_types=frozenset({"analog-input", "analog-value"}),
        equipment_classes=frozenset({"Meter"}),
        base_score=0.30,
    ),

    SemanticRule(
        brick_class="Power_Sensor",
        required_any=frozenset({"power"}),
        preferred=frozenset({"total", "power"}),
        units=frozenset({"kilowatts"}),
        object_types=frozenset({"analog-input", "analog-value"}),
        equipment_classes=frozenset({"Meter"}),
        base_score=0.30,
    ),

    SemanticRule(
        brick_class="Demand_Sensor",
        required_any=frozenset({"demand"}),
        preferred=frozenset({"peak", "demand", "kw"}),
        units=frozenset({"kilowatts"}),
        object_types=frozenset({"analog-input", "analog-value"}),
        equipment_classes=frozenset({"Meter"}),
        base_score=0.35,
    ),

    SemanticRule(
        brick_class="Cooling_Temperature_Setpoint",
        required_any=frozenset({"setpoint"}),
        preferred=frozenset({"cooling", "cool"}),
        units=frozenset({"degrees-celsius", "degrees-fahrenheit"}),
        object_types=frozenset({"analog-value", "analog-output"}),
        base_score=0.25,
    ),

    SemanticRule(
        brick_class="Heating_Temperature_Setpoint",
        required_any=frozenset({"setpoint"}),
        preferred=frozenset({"heating", "heat"}),
        units=frozenset({"degrees-celsius", "degrees-fahrenheit"}),
        object_types=frozenset({"analog-value", "analog-output"}),
        base_score=0.25,
    ),
)


EQUIPMENT_RULES: tuple[SemanticRule, ...] = (
    SemanticRule(
        brick_class="Air_Handling_Unit",
        required_any=frozenset({"ahu", "air", "handling"}),
        preferred=frozenset({"ahu"}),
        base_score=0.35,
    ),

    SemanticRule(
        brick_class="Variable_Air_Volume_Box",
        required_any=frozenset({"vav", "variable", "volume"}),
        preferred=frozenset({"vav"}),
        base_score=0.40,
    ),

    SemanticRule(
        brick_class="Chiller",
        required_any=frozenset({"chiller", "ch"}),
        preferred=frozenset({"chiller"}),
        base_score=0.35,
    ),

    SemanticRule(
        brick_class="Boiler",
        required_any=frozenset({"boiler", "blr"}),
        preferred=frozenset({"boiler"}),
        base_score=0.35,
    ),

    SemanticRule(
        brick_class="Meter",
        required_any=frozenset({"meter"}),
        preferred=frozenset({"meter", "energy", "power", "demand"}),
        base_score=0.40,
    ),
)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_POWER_UNITS = frozenset({"kilowatts"})
_ENERGY_UNITS = frozenset({"kilowatt-hours"})


def _has_dimensional_unit_conflict(rule: SemanticRule, units: str | None) -> bool:
    """Reject obvious electrical dimension mismatches instead of merely
    lowering their score. A kWh point is energy, not power/demand; a kW point
    is power/demand, not accumulated energy.
    """
    if not units:
        return False

    if rule.brick_class == "Energy_Sensor" and units in _POWER_UNITS:
        return True

    if rule.brick_class in {"Power_Sensor", "Demand_Sensor"} and units in _ENERGY_UNITS:
        return True

    return False


def _score_rule(
    rule: SemanticRule,
    *,
    tokens: set[str],
    units: str | None = None,
    object_type: str | None = None,
    equipment_class: str | None = None,
) -> SemanticCandidate | None:
    if _has_dimensional_unit_conflict(rule, units):
        return None

    reasons: list[str] = []
    score = rule.base_score

    if rule.required_any:
        matches = tokens & rule.required_any
        if not matches:
            return None

        score += 0.15
        reasons.append(
            f"name matched {', '.join(sorted(matches))}"
        )

    preferred_matches = tokens & rule.preferred
    if preferred_matches:
        # More matching contextual tokens => stronger result.
        score += min(0.30, 0.10 * len(preferred_matches))
        reasons.append(
            f"context matched {', '.join(sorted(preferred_matches))}"
        )

    if units and rule.units:
        if units in rule.units:
            score += 0.15
            reasons.append(f"units are {units}")
        else:
            # Conflicting units should hurt confidence.
            score -= 0.15

    if object_type and rule.object_types:
        if object_type in rule.object_types:
            score += 0.10
            reasons.append(f"BACnet type is {object_type}")
        else:
            score -= 0.10

    if equipment_class and rule.equipment_classes:
        if equipment_class in rule.equipment_classes:
            score += 0.15
            reasons.append(f"parent equipment is {equipment_class}")

    score = max(0.0, min(score, 1.0))

    return SemanticCandidate(
        brick_class=rule.brick_class,
        score=score,
        reasons=reasons,
    )


def confidence_for(score: float) -> str:
    if score >= 0.80:
        return "high"
    if score >= 0.55:
        return "medium"
    if score >= 0.30:
        return "low"
    return "none"


# ---------------------------------------------------------------------------
# Equipment suggestions
# ---------------------------------------------------------------------------

def suggest_equipment(device: DeviceSnapshot) -> SemanticSuggestion:
    combined_text = " ".join(
        filter(
            None,
            [
                device.name,
                device.description,
                device.model_name,
            ],
        )
    )

    tokens = tokens_for(combined_text)

    # Point names are useful equipment-level context too.
    for point in device.points:
        tokens.update(tokens_for(point.object_name))

    candidates = [
        candidate
        for rule in EQUIPMENT_RULES
        if (
            candidate := _score_rule(
                rule,
                tokens=tokens,
            )
        )
        is not None
    ]

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)

    best = candidates[0] if candidates else None

    return SemanticSuggestion(
        source_kind="device",
        source_key=f"device:{device.device_instance}",
        source_name=device.name,
        suggested_class=best.brick_class if best else None,
        confidence=confidence_for(best.score) if best else "none",
        score=best.score if best else 0.0,
        reasons=best.reasons if best else [],
        alternatives=candidates[1:4],
    )


# ---------------------------------------------------------------------------
# Point suggestions
# ---------------------------------------------------------------------------

def suggest_point(
    device: DeviceSnapshot,
    point: PointSnapshot,
    *,
    equipment_class: str | None = None,
) -> SemanticSuggestion:
    combined_text = " ".join(
        filter(
            None,
            [
                point.object_name,
                point.description,
            ],
        )
    )

    tokens = tokens_for(combined_text)

    candidates = [
        candidate
        for rule in POINT_RULES
        if (
            candidate := _score_rule(
                rule,
                tokens=tokens,
                units=point.units,
                object_type=point.object_type,
                equipment_class=equipment_class,
            )
        )
        is not None
    ]

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)

    best = candidates[0] if candidates else None

    # Avoid over-specific mapping when evidence is weak.
    if best and best.score < 0.30:
        best = None

    return SemanticSuggestion(
        source_kind="point",
        source_key=(
            f"device:{device.device_instance}/"
            f"{point.object_type}:{point.object_instance}"
        ),
        source_name=point.object_name,
        suggested_class=best.brick_class if best else None,
        confidence=confidence_for(best.score) if best else "none",
        score=best.score if best else 0.0,
        reasons=best.reasons if best else [],
        alternatives=candidates[1:4] if best else candidates[:3],
    )


# ---------------------------------------------------------------------------
# Whole-device inference
# ---------------------------------------------------------------------------

def suggest_device_semantics(
    device: DeviceSnapshot,
) -> list[SemanticSuggestion]:
    suggestions: list[SemanticSuggestion] = []

    equipment = suggest_equipment(device)
    suggestions.append(equipment)

    equipment_class = equipment.suggested_class

    for point in device.points:
        suggestions.append(
            suggest_point(
                device,
                point,
                equipment_class=equipment_class,
            )
        )

    return suggestions


# ---------------------------------------------------------------------------
# Vocabulary safety net -- every rule's brick_class must be a real,
# canonical class the app already recognizes (EQUIPMENT_TYPES/POINT_TYPES,
# src/core/config.py), or every downstream consumer (the Brick class
# picker, validate_semantic_entity(), the TTL exporter) would reject a
# suggestion built from it. Runs once at import time so a future rule typo
# or a retired class fails the moment the app starts, not silently at
# suggestion time.
# ---------------------------------------------------------------------------

def _assert_rules_use_canonical_vocabulary() -> None:
    for rule in EQUIPMENT_RULES:
        assert rule.brick_class in EQUIPMENT_TYPES, (
            f"suggestions.py EQUIPMENT_RULES references {rule.brick_class!r}, "
            f"which is not in EQUIPMENT_TYPES (src/core/config.py)"
        )
    for rule in POINT_RULES:
        assert rule.brick_class in POINT_TYPES, (
            f"suggestions.py POINT_RULES references {rule.brick_class!r}, "
            f"which is not in POINT_TYPES (src/core/config.py)"
        )
        for eq_class in rule.equipment_classes:
            assert eq_class in EQUIPMENT_TYPES, (
                f"suggestions.py POINT_RULES entry {rule.brick_class!r} references "
                f"equipment_classes={eq_class!r}, which is not in EQUIPMENT_TYPES"
            )


_assert_rules_use_canonical_vocabulary()