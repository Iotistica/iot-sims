from __future__ import annotations

import os
from pathlib import Path

# Single source of truth for where the runtime writes datasets/calibration
# jobs -- shared between shared/runtime/datasets/ and shared/runtime/calibration/
# so both agree on the same root without either importing the other. Same
# env-var-with-repo-relative-default pattern as FMU_MODELS_ROOT in catalog.py.
_default_root = Path(__file__).resolve().parents[2] / "data"
RUNTIME_DATA_DIR = Path(os.getenv("RUNTIME_DATA_DIR", str(_default_root)))
RUNTIME_DATA_DIR.mkdir(parents=True, exist_ok=True)
