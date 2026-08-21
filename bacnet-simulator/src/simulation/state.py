"""Simulation clock state.

Physically extracted from src/legacy.py's `SimState` (continuing the GH #15
refactor, same "moved verbatim, no behavior changes" standard as the
Database and API-router extractions).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SimState:
    time_of_day: float = 12.0
    elapsed_seconds: float = 0.0
