from __future__ import annotations

from .models.catalog import ModelCatalog
from .models.manager import FMUSessionManager

catalog = ModelCatalog.from_environment()
manager = FMUSessionManager(catalog)
