from __future__ import annotations

from typing import Any

__all__ = ["SimEngine", "SimState"]


def __getattr__(name: str) -> Any:
    """
    Lazy compatibility exports.

    Keep package initialization lightweight because src.legacy imports
    src.simulation.providers while src.legacy itself is still initializing.
    Eagerly importing .engine or .state here would re-enter src.legacy and
    create a circular import.
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
