"""The generic, provider-agnostic resolution layer (src/semantics/resolution.py).

These tests must not import anything Brick -- the whole point is that the generic layer
never knows which representation answered.
"""
from __future__ import annotations

from src.semantics.contract import SemanticContract
from src.semantics.resolution import (
    ResolutionCandidate,
    SemanticResolution,
    resolve,
)


class _RecordingProvider:
    """A fake provider that records whether it was consulted."""

    name = "fake"

    def __init__(self, result: SemanticResolution | None = None):
        self.called_with: list = []
        self._result = result or SemanticResolution(7, "exact", "fake", {})

    def resolve(self, database, contract, candidates, *, context=None):
        self.called_with.append(contract)
        return self._result


_CANDS = [ResolutionCandidate(id=1, display_name="a"), ResolutionCandidate(id=2, display_name="b")]


def test_empty_contract_is_unresolved_without_calling_the_provider():
    provider = _RecordingProvider()
    res = resolve(None, provider, None, _CANDS)
    assert res.quality == "unresolved"
    assert provider.called_with == []

    res2 = resolve(None, provider, SemanticContract(), _CANDS)
    assert res2.quality == "unresolved"
    assert provider.called_with == []


def test_aggregated_contract_is_unresolved_without_calling_the_provider():
    provider = _RecordingProvider()
    contract = SemanticContract(id="most_open_vav_damper_position", role="sensor", aggregation="most_open")
    res = resolve(None, provider, contract, _CANDS)
    assert res.quality == "unresolved"
    assert "aggregation" in res.provenance.get("reason", "")
    assert provider.called_with == []


def test_derived_contract_is_passed_through_to_the_provider():
    provider = _RecordingProvider(SemanticResolution(None, "unresolved", "fake", {}))
    contract = SemanticContract(id="compressor_cop", role="derived")
    res = resolve(None, provider, contract, _CANDS)
    assert provider.called_with == [contract]  # provider WAS consulted
    assert res.quality == "unresolved"  # ...and it happened to have no representation


def test_ordinary_contract_delegates_and_returns_provider_result():
    provider = _RecordingProvider(SemanticResolution(2, "exact", "fake", {"summary": "ok"}))
    contract = SemanticContract(id="supply_air_temperature", role="sensor")
    res = resolve(None, provider, contract, _CANDS)
    assert (res.candidate_id, res.quality, res.provider) == (2, "exact", "fake")
    assert provider.called_with == [contract]


def test_provider_name_used_in_generic_reasons():
    provider = _RecordingProvider()
    res = resolve(None, provider, SemanticContract(id="x_y", role="sensor", aggregation="total"), _CANDS)
    assert res.provider == "fake"
