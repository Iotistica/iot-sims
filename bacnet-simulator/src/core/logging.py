"""Application logging.

Physically extracted from src/legacy.py -- continuing the GH #15 refactor,
same "moved verbatim, no behavior changes" standard as the Database and
simulation-engine extractions.
"""
from __future__ import annotations

import logging

from bacpypes3.debugging import ModuleLogger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bacnet-sim")

_debug = 0
_log = ModuleLogger(globals())

__all__ = ["log", "_log"]
