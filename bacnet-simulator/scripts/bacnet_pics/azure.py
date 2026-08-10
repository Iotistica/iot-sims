"""Re-exports AzureStructuredClient from src/integrations/azure_openai.py --
the implementation now lives there so the running app (which ships src/,
never scripts/) can share it too. Kept here so this package's own imports
(`from .azure import AzureStructuredClient`) and any external callers of
this module path keep working unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

# This script tree runs standalone (python scripts/parse_pics.py, etc.),
# not via the app's own package install -- make sure the repo root is on
# sys.path so `import src...` resolves regardless of the caller's cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.integrations.azure_openai import AzureStructuredClient  # noqa: E402,F401
