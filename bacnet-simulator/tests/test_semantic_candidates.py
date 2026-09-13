"""src/semantics/candidates.py -- the compat boundary that turns today's flat point_type
mirror into a neutral ResolutionCandidate. Consumers use these; they never see point_type."""
from __future__ import annotations

import ast
from pathlib import Path

from src.semantics.candidates import (
    build_resolution_candidate_from_object,
    build_resolution_candidate_from_recording_point,
)

_SRC = Path(__file__).resolve().parents[1] / "src"


def test_object_builder_is_neutral():
    cand = build_resolution_candidate_from_object(
        {"id": 5, "name": "AI_07", "units": "degrees-celsius", "point_type": "Supply_Air_Temperature_Sensor"}
    )
    assert cand.id == 5
    assert cand.source_object_id == 5
    assert cand.display_name == "AI_07"
    assert cand.representation_snapshot == {"brick.point_class": "Supply_Air_Temperature_Sensor"}


def test_recording_point_builder_handles_deleted_source():
    cand = build_resolution_candidate_from_recording_point(
        {"id": 12, "object_name": "old", "units": None, "point_type": "Fan_Status", "source_object_id": None}
    )
    assert cand.id == 12
    assert cand.source_object_id is None
    assert cand.representation_snapshot == {"brick.point_class": "Fan_Status"}


def test_no_point_type_yields_empty_snapshot():
    cand = build_resolution_candidate_from_object({"id": 1, "name": "x", "point_type": None})
    assert cand.representation_snapshot == {}


def _module_names_referenced(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            names.add(mod)
            for alias in node.names:
                names.add(alias.name)
    return names


def test_consumers_do_not_reference_a_semantic_representation():
    """calibration.py and suggestions.py must speak only the generic vocabulary --
    no Brick, no feature_contract, no point_class, no ResolvedBrickPoint."""
    forbidden = ("brick", "feature_contract", "ResolvedBrickPoint", "point_class", "haystack")
    for rel in ("api/routers/calibration.py", "simulation/mapping/suggestions.py"):
        text = (_SRC / rel).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{rel} references {token!r}"
