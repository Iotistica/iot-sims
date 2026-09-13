"""The Brick semantic provider (src/semantics/providers/brick.py) -- the first concrete
representation. ResolvedBrickPoint stays internal to the Brick side; these tests exercise
it only through the neutral SemanticResolution / _expected_identity."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.config import POINT_TYPES
from src.semantics.contract import SemanticContract
from src.semantics.providers.brick import (
    CANONICAL_TO_BRICK,
    NAME_TIER_ONLY_IDS,
    BrickSemanticProvider,
)
from src.semantics.resolution import ResolutionCandidate

_MODELS_ROOT = Path(__file__).resolve().parents[2].parent / "iot-models" / "models"


def _cand(cid: int, brick_class: str | None) -> ResolutionCandidate:
    snap = {"brick.point_class": brick_class} if brick_class else {}
    return ResolutionCandidate(id=cid, display_name=f"pt{cid}", representation_snapshot=snap)


def test_every_template_point_class_is_a_real_brick_class():
    for canonical_id, template in CANONICAL_TO_BRICK.items():
        assert template.point_class in POINT_TYPES, (canonical_id, template.point_class)


def test_expected_identity_templated_id():
    provider = BrickSemanticProvider()
    got = provider._expected_identity(SemanticContract(id="supply_fan_command", role="command"))
    assert got is not None
    assert (got.point_class, got.equipment_path) == ("Fan_Speed_Command", ("Supply_Fan",))


def test_expected_identity_unknown_id_is_none():
    # no legacy point_class fallback exists -- an id without a template is unresolvable
    provider = BrickSemanticProvider()
    assert provider._expected_identity(SemanticContract(id="chiller_1_power", role="derived")) is None


def test_derived_contract_reaches_provider_and_resolves_when_templated(monkeypatch):
    # give the Brick provider a template for a derived id, prove it resolves normally
    provider = BrickSemanticProvider()
    monkeypatch.setitem(CANONICAL_TO_BRICK, "compressor_cop_x", type(next(iter(CANONICAL_TO_BRICK.values())))("Power_Sensor"))
    contract = SemanticContract(id="compressor_cop_x", role="derived")
    res = provider.resolve(None, contract, [_cand(1, "Power_Sensor")])
    assert res.quality == "class_only" and res.candidate_id == 1


def test_class_only_single_candidate_binds_low_when_no_path_required():
    provider = BrickSemanticProvider()
    contract = SemanticContract(id="supply_air_temperature", role="sensor")
    res = provider.resolve(None, contract, [
        _cand(1, "Supply_Air_Temperature_Sensor"),
        _cand(2, "Return_Air_Temperature_Sensor"),
    ])
    assert res.quality == "class_only" and res.candidate_id == 1


def test_class_only_multiple_candidates_is_ambiguous():
    provider = BrickSemanticProvider()
    contract = SemanticContract(id="supply_air_temperature", role="sensor")
    res = provider.resolve(None, contract, [
        _cand(1, "Supply_Air_Temperature_Sensor"),
        _cand(2, "Supply_Air_Temperature_Sensor"),
    ])
    assert res.quality == "ambiguous"
    assert set(res.alternatives) == {1, 2}
    assert res.candidate_id is None


def test_path_requiring_target_never_binds_a_class_only_candidate():
    # supply_fan_command needs equipment_path ("Supply_Fan",); a snapshot-only candidate
    # can only be class_only -> the provider must NOT guess, it stays unresolved.
    provider = BrickSemanticProvider()
    contract = SemanticContract(id="supply_fan_command", role="command")
    res = provider.resolve(None, contract, [_cand(1, "Fan_Speed_Command")])
    assert res.quality == "unresolved"
    assert res.candidate_id is None


def test_no_representation_is_unresolved():
    provider = BrickSemanticProvider()
    res = provider.resolve(None, SemanticContract(id="chiller_1_power", role="derived"), [_cand(1, "Power_Sensor")])
    assert res.quality == "unresolved"


@pytest.mark.skipif(not _MODELS_ROOT.exists(), reason="iot-models checkout not adjacent")
def test_every_shipped_semantic_id_is_deliberately_classified():
    """Each id a shipped model uses must be templated, name-tier, derived, or aggregated --
    never silently unclassified (which would mean an accidental name-tier fallback)."""
    derived_or_aggregated: set[str] = set()
    for model_json in _MODELS_ROOT.glob("*/model.json"):
        data = json.loads(model_json.read_text(encoding="utf-8"))
        for direction in ("inputs", "outputs"):
            for entry in data.get(direction, []):
                semantic = entry.get("semantic") or {}
                sid = semantic.get("id")
                if not sid:
                    continue
                role = semantic.get("role")
                aggregated = semantic.get("aggregation") not in (None, "none")
                classified = (
                    sid in CANONICAL_TO_BRICK
                    or sid in NAME_TIER_ONLY_IDS
                    or role == "derived"
                    or aggregated
                )
                assert classified, f"{model_json.parent.name}: {sid!r} is not deliberately classified"
                if role == "derived" or aggregated:
                    derived_or_aggregated.add(sid)
    # sanity: an id is never BOTH templated and (derived/aggregated)
    assert not (derived_or_aggregated & set(CANONICAL_TO_BRICK))
