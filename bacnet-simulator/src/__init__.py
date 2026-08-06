"""BACnet simulator package.

This transitional refactor preserves the existing public API while exposing
smaller responsibility-focused modules. Runtime behavior remains implemented
by ``legacy`` until each facade is migrated independently.
"""
from .application import api, create_app

__all__ = ["api", "create_app"]
