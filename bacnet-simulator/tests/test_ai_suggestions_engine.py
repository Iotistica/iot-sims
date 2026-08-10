"""Pure tests for the AI-fallback prompt/context assembly
(src/semantics/ai_suggestions.py) -- no network, no Azure client needed.
Confirms the built prompt actually carries the context the feature
requires (device, target point, siblings, equipment context) and that the
canonical POINT_TYPES vocabulary is what gets shown to the model."""
from __future__ import annotations

from src.core.config import POINT_TYPES
from src.semantics.ai_suggestions import _build_user_prompt, _format_point_types


def test_prompt_includes_device_target_and_sibling_context():
    device = {"name": "AHU-1", "description": "Air handler", "vendor_name": "Acme", "model_name": "X100"}
    target = {"name": "SF-Speed", "object_type": "analog-input", "units": "percent", "description": None}
    siblings = [
        {"name": "SAT", "object_type": "analog-input", "units": "degrees-celsius"},
        {"name": "RF-Run", "object_type": "binary-input", "units": "no-units"},
    ]
    prompt = _build_user_prompt(device=device, target=target, siblings=siblings, equipment_context="Air_Handling_Unit")

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
    prompt = _build_user_prompt(device=device, target=target, siblings=[], equipment_context=None)
    assert "unclassified" in prompt.lower()


def test_prompt_enumerates_canonical_vocabulary_exactly():
    listed = _format_point_types()
    for brick_class in POINT_TYPES:
        assert brick_class in listed
    # No entries beyond the canonical set should ever be suggested as
    # options -- count check catches an accidental extra/duplicate line.
    assert listed.count("\n") == len(POINT_TYPES) - 1
