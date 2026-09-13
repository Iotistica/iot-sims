"""src/semantics/providers/__init__.py -- the swappable seam. Representation is chosen per
resolution (context hint > project setting > "brick"); no consumer knows or cares which."""
from __future__ import annotations

import pytest

from src.semantics.contract import SemanticContract
from src.semantics.providers import (
    _PROVIDERS,
    available_providers,
    get_semantic_provider,
    register_provider,
)
from src.semantics.resolution import ResolutionContext, SemanticResolution


class _StubProvider:
    name = "stub"

    def resolve(self, database, contract, candidates, *, context=None):
        return SemanticResolution(999, "exact", "stub", {"summary": "stub"})


class _FakeDB:
    def __init__(self, provider: str | None = None):
        self._provider = provider

    def get_settings(self):
        return {"semantic_provider": self._provider} if self._provider else {}


@pytest.fixture(autouse=True)
def _restore_registry():
    original = dict(_PROVIDERS)
    yield
    _PROVIDERS.clear()
    _PROVIDERS.update(original)


def test_default_is_brick():
    provider = get_semantic_provider(_FakeDB())
    assert provider.name == "brick"


def test_project_setting_selects_provider():
    register_provider("stub", _StubProvider)
    provider = get_semantic_provider(_FakeDB(provider="stub"))
    assert provider.name == "stub"
    assert "stub" in available_providers()


def test_context_hint_wins_over_setting():
    register_provider("stub", _StubProvider)
    provider = get_semantic_provider(
        _FakeDB(provider="brick"),
        ResolutionContext(semantic_provider_hint="stub"),
    )
    assert provider.name == "stub"


def test_unknown_provider_name_falls_back_to_brick():
    provider = get_semantic_provider(_FakeDB(provider="does-not-exist"))
    assert provider.name == "brick"


def test_consumers_consume_a_stub_providers_resolution_unchanged():
    register_provider("stub", _StubProvider)
    from src.semantics.resolution import ResolutionCandidate, resolve

    res = resolve(
        _FakeDB(provider="stub"),
        get_semantic_provider(_FakeDB(provider="stub")),
        SemanticContract(id="supply_air_temperature", role="sensor"),
        [ResolutionCandidate(id=1, display_name="x")],
    )
    assert (res.candidate_id, res.quality, res.provider) == (999, "exact", "stub")
