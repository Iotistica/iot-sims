"""Shared pytest fixtures.

database/seeded_database build a real temp-file SQLite Database via the
same Database.setup()/seed_default() the app uses in production -- no
mocking of the persistence layer.

test_app/client deliberately bypass src/legacy.py's full lifespan()
(which binds a UDP BACnet socket and starts background tick loops) by
wiring just the routers under test into a bare FastAPI() with
app.state.db set directly. Add new routers to _ROUTERS as later phases
introduce them (e.g. the semantic router from phase 3).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from src.legacy import Database  # noqa: E402


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.setup()
    return db


@pytest.fixture
def seeded_database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "seeded.db")
    db.setup()
    db.seed_default()
    return db


def _routers():
    # Imported lazily (inside the fixture) so a router module that fails
    # to import doesn't break every other test file's collection.
    from src.api.routers.locations import router as locations_router

    routers = [locations_router]

    try:
        from src.api.routers.semantic import router as semantic_router
        routers.append(semantic_router)
    except ImportError:
        pass  # not added until phase 3

    return routers


@pytest.fixture
def test_app(database: Database):
    from fastapi import FastAPI

    app = FastAPI()
    app.state.db = database
    for router in _routers():
        app.include_router(router)
    return app


@pytest.fixture
def client(test_app):
    from fastapi.testclient import TestClient

    with TestClient(test_app) as c:
        yield c
