"""Database compatibility facade.

The existing Database implementation is re-exported intact in phase 1. Its
methods can later be moved domain-by-domain without changing import sites.
"""
from ..legacy import Database
__all__ = ["Database"]
