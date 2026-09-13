"""Semantic provider registry -- the seam that keeps representation swappable.

``get_semantic_provider`` is context-aware, not inherently global: a dataset/device MAY
name the representation it was authored against (``context.semantic_provider_hint``),
otherwise the project/global ``semantic_provider`` setting decides, otherwise ``"brick"``.

Multi-provider orchestration is NOT implemented -- one provider resolves one contract.
Adding Haystack / an OPC UA information model later = one new module + one dict entry,
with zero change to any consumer.
"""
from __future__ import annotations

from typing import Any, Callable

from ..resolution import ResolutionContext, SemanticProvider
from .brick import BrickSemanticProvider

_PROVIDERS: dict[str, Callable[[], SemanticProvider]] = {
    "brick": BrickSemanticProvider,
}

_DEFAULT_PROVIDER = "brick"


def available_providers() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


def register_provider(name: str, factory: Callable[[], SemanticProvider]) -> None:
    """Test/extension hook -- register a provider under ``name``."""
    _PROVIDERS[name] = factory


def _settings_provider(database: Any) -> str | None:
    try:
        settings = database.get_settings() or {}
    except Exception:
        return None
    value = settings.get("semantic_provider")
    return str(value) if value else None


def get_semantic_provider(
    database: Any,
    context: "ResolutionContext | None" = None,
) -> SemanticProvider:
    name = (
        (context.semantic_provider_hint if context is not None else None)
        or _settings_provider(database)
        or _DEFAULT_PROVIDER
    )
    factory = _PROVIDERS.get(name) or _PROVIDERS[_DEFAULT_PROVIDER]
    return factory()
