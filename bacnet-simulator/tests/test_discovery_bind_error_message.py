"""raise_discovery_bind_error() must diagnose a port-47808 bind failure
using engine.app (the real "is SimEngine's own BACnet stack holding the
port" signal), not sim_state/the Start-Pause-Stop clock -- those buttons
never release the port (see SimEngine.start()'s early-return and the
docstring on _engine_bacnet_running). A user who clicks Stop and retries
discovery previously got the exact same "stop the simulation" advice every
time, which doesn't fix anything."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.routers.discovery import DiscoveryBindError, raise_discovery_bind_error


def _fake_request(engine):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))


def test_bind_error_when_engine_bacnet_stack_is_running():
    request = _fake_request(SimpleNamespace(app=object()))  # engine.app is not None
    with pytest.raises(HTTPException) as exc_info:
        raise_discovery_bind_error(request, DiscoveryBindError("raw"))
    assert exc_info.value.status_code == 503
    assert "own BACnet stack" in exc_info.value.detail
    assert "does NOT release this port" in exc_info.value.detail


def test_bind_error_when_engine_bacnet_stack_is_idle():
    request = _fake_request(SimpleNamespace(app=None))  # engine.app is None -- not this simulator
    with pytest.raises(HTTPException) as exc_info:
        raise_discovery_bind_error(request, DiscoveryBindError("raw"))
    assert exc_info.value.status_code == 503
    assert "not by this simulator" in exc_info.value.detail


def test_bind_error_falls_back_to_generic_message_when_engine_unavailable():
    request = _fake_request(None)  # app.state.engine itself missing/None
    with pytest.raises(HTTPException) as exc_info:
        raise_discovery_bind_error(request, DiscoveryBindError("raw detail"))
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "raw detail"
