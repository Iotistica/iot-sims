from __future__ import annotations

from typing import Any

__all__ = ["SimEngine", "SimState"]


def __getattr__(name: str) -> Any:
    """
    Lazy compatibility exports.

    Keep package initialization lightweight: src.db.database reaches this
    package (via simulation.model_store -> simulation.models ->
    simulation.providers) while it's still initializing itself. Eagerly
    importing .engine or .state here -- both of which pull in
    src.dependencies, which imports src.db -- would re-enter that partially
    initialized chain and create a circular import.
    """
    if name == "SimEngine":
        from .engine import SimEngine
        return SimEngine

    if name == "SimState":
        from .state import SimState
        return SimState

    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )
