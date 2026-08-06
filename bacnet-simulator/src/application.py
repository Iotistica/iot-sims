"""FastAPI application composition.

Phase 1 delegates to the original application object to preserve every route,
middleware, websocket, lifespan hook, and public URL exactly.
"""
from fastapi import FastAPI
from .legacy import api


def create_app() -> FastAPI:
    return api


__all__ = ["api", "create_app"]
