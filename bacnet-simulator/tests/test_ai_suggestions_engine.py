"""Pure tests for the AI-fallback prompt/context assembly
(src/semantics/ai_suggestions.py) -- no network, no Azure client needed.
Confirms the built prompt actually carries the context the feature
requires (device, target point, siblings, equipment context) and that the
canonical POINT_TYPES vocabulary is what gets shown to the model."""
from __future__ import annotations

from src.core.config import POINT_TYPES
from src.semantics.ai_suggestions import (
    _MAX_SIMILAR_EXAMPLES,
    _build_user_prompt,
    _find_similar_examples,
    _format_point_types,
    _score_similarity,
)


def test_prompt_includes_device_target_and_sibling_context():
    device = {"name": "AHU-1", "description": "Air handler", "vendor_name": "Acme", "model_name": "X100"}
    target = {"name": "SF-Speed", "object_type": "analog-input", "units": "percent", "description": None}
    siblings = [
        {"name": "SAT", "object_type": "analog-input", "units": "degrees-celsius"},
        {"name": "RF-Run", "object_type": "binary-input", "units": "no-units"},
    ]
    prompt = _build_user_prompt(device=device, target=target, siblings=siblings, equipment_context="Air_Handling_Unit", similar_examples=[])

    assert "AHU-1" in prompt
    assert "Acme" in prompt
    assert "X100" in prompt
    assert "Air_Handling_Unit" in prompt
    assert "SF-Speed" in prompt
    assert "percent" in prompt
    assert "SAT" in prompt
    assert "RF-Run" in prompt


def test_prompt_handles_missing_equipment_context():
    device = {"name": "Misc", "description": None, "vendor_name": None, "model_name": None}
    target = {"name": "TEMP1", "object_type": "analog-input", "units": None, "description": None}
    prompt = _build_user_prompt(device=device, target=target, siblings=[], equipment_context=None, similar_examples=[])
    assert "unclassified" in prompt.lower()


def test_prompt_enumerates_canonical_vocabulary_exactly():
    listed = _format_point_types()
    for brick_class in POINT_TYPES:
        assert brick_class in listed
    # No entries beyond the canonical set should ever be suggested as
    # options -- count check catches an accidental extra/duplicate line.
    assert listed.count("\n") == len(POINT_TYPES) - 1


# ─── Few-shot grounding from already-classified points ───────────────────────

def test_score_similarity_weighs_name_tokens_above_units_and_type():
    target = {"name": "Supply-Air-Temp", "units": "degrees-celsius", "object_type": "analog-input"}
    name_match_only = {"name": "SAT", "units": "no-units", "object_type": "binary-input"}
    units_and_type_only = {"name": "Totally-Unrelated", "units": "degrees-celsius", "object_type": "analog-input"}
    assert _score_similarity(target, name_match_only) > _score_similarity(target, units_and_type_only)


def test_score_similarity_zero_for_unrelated_point():
    target = {"name": "Supply-Air-Temp", "units": "degrees-celsius", "object_type": "analog-input"}
    unrelated = {"name": "Zzz-Nothing-Alike", "units": "percent", "object_type": "binary-output"}
    assert _score_similarity(target, unrelated) == 0


def test_find_similar_examples_excludes_zero_score_and_caps_at_max():
    target = {"name": "Supply-Air-Temp", "units": "degrees-celsius", "object_type": "analog-input"}
    candidates = [
        {"name": f"Supply-Air-Temp-{i}", "units": "degrees-celsius", "object_type": "analog-input", "point_type": "Supply_Air_Temperature_Sensor"}
        for i in range(_MAX_SIMILAR_EXAMPLES + 3)
    ]
    candidates.append({"name": "Totally-Unrelated", "units": "percent", "object_type": "binary-output", "point_type": "Fan_Status"})

    result = _find_similar_examples(target, candidates)

    assert len(result) == _MAX_SIMILAR_EXAMPLES
    assert all(c["point_type"] == "Supply_Air_Temperature_Sensor" for c in result)


def test_find_similar_examples_sorts_best_match_first():
    target = {"name": "Supply-Air-Temp", "units": "degrees-celsius", "object_type": "analog-input"}
    weak = {"name": "Air-Something", "units": "no-units", "object_type": "binary-input", "point_type": "Weak_Match"}
    strong = {"name": "Supply-Air-Temp-2", "units": "degrees-celsius", "object_type": "analog-input", "point_type": "Strong_Match"}

    result = _find_similar_examples(target, [weak, strong])

    assert [c["point_type"] for c in result] == ["Strong_Match", "Weak_Match"]


def test_prompt_includes_similar_examples_section_when_present():
    device = {"name": "AHU-1", "description": None, "vendor_name": None, "model_name": None}
    target = {"name": "SAT", "object_type": "analog-input", "units": "degrees-celsius", "description": None}
    example = {"name": "SAT-2", "object_type": "analog-input", "units": "degrees-celsius", "point_type": "Supply_Air_Temperature_Sensor"}

    prompt = _build_user_prompt(device=device, target=target, siblings=[], equipment_context=None, similar_examples=[example])

    assert "Similar points already classified" in prompt
    assert "SAT-2" in prompt
    assert "Supply_Air_Temperature_Sensor" in prompt


def test_prompt_omits_similar_examples_section_when_empty():
    device = {"name": "AHU-1", "description": None, "vendor_name": None, "model_name": None}
    target = {"name": "SAT", "object_type": "analog-input", "units": "degrees-celsius", "description": None}

    prompt = _build_user_prompt(device=device, target=target, siblings=[], equipment_context=None, similar_examples=[])

    assert "Similar points already classified" not in prompt
