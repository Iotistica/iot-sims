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
    from src.api.routers.devices import router as devices_router
    from src.api.routers.equipment import router as equipment_router

    routers = [locations_router, devices_router, equipment_router]

    try:
        from src.api.routers.semantic import router as semantic_router
        routers.append(semantic_router)
    except ImportError:
        pass  # not added until phase 3

    try:
        from src.api.routers.objects import router as objects_router
        from src.api.routers.external_objects import router as external_objects_router
        routers.append(objects_router)
        routers.append(external_objects_router)
    except ImportError:
        pass

    try:
        from src.api.routers.semantic_suggestions import router as semantic_suggestions_router
        routers.append(semantic_suggestions_router)
    except ImportError:
        pass

    try:
        from src.api.routers.functional_tests import router as functional_tests_router
        from src.api.routers.functional_test_runs import router as functional_test_runs_router
        routers.append(functional_tests_router)
        routers.append(functional_test_runs_router)
    except ImportError:
        pass

    return routers


class _FakeEngine:
    """Stands in for app.state.engine -- devices.py's schedule_engine_reload()
    fires-and-forgets engine.reload(); tests don't need the real BACnet
    engine, just something with that coroutine so the call doesn't 503."""
    async def reload(self) -> None:
        pass

    async def add_object_hot(self, device_instance: int, obj: dict) -> None:
        # objects.py's create_object() fires-and-forgets this for the
        # hot-add path (an enabled object on an already-enabled device) --
        # same reasoning as reload() above.
        pass


@pytest.fixture
def test_app(database: Database):
    from fastapi import FastAPI

    app = FastAPI()
    app.state.db = database
    app.state.engine = _FakeEngine()
    app.state.device_names = {}

    # devices.py's log_event() reads app.state.log_event as an optional
    # callback (None = silently no-op) -- capture calls here so tests can
    # assert on exactly what got logged, e.g. tests/test_device_delete_logs.py.
    app.state.logged_events = []

    def _log_event(device_id, level, message):
        app.state.logged_events.append(
            {"device_id": device_id, "level": level, "message": message}
        )

    app.state.log_event = _log_event

    for router in _routers():
        app.include_router(router)
    return app


@pytest.fixture
def client(test_app):
    from fastapi.testclient import TestClient

    with TestClient(test_app) as c:
        yield c
