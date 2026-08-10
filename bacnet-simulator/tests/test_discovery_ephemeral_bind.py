"""_run_discovery() must request an ephemeral local bind (needs_broadcast=
False) for every targeted call, and only fall back to the real port 47808
(needs_broadcast=True) for a genuine subnet-wide scan (no Discovery
Target) -- see _discovery_session()'s docstring. This is what lets a
mixed simulated + external-BACnet project run targeted discovery/object
reads/live-value refresh while the simulator's own BACnet stack keeps
holding 47808 for its own devices."""
from __future__ import annotations

import contextlib

import pytest

from src.api.routers import discovery as discovery_module
from src.bacnet.schemas import DiscoveryTriggerRequest


@pytest.fixture
def capture_needs_broadcast(monkeypatch):
    calls: list[bool] = []

    @contextlib.asynccontextmanager
    async def fake_session(*, needs_broadcast: bool = False):
        calls.append(needs_broadcast)

        class _FakeDiscovery:
            async def discover(self, options):
                return []

        yield _FakeDiscovery()

    monkeypatch.setattr(discovery_module, "_discovery_session", fake_session)
    return calls


@pytest.mark.asyncio
async def test_targeted_discovery_uses_ephemeral_bind(capture_needs_broadcast):
    body = DiscoveryTriggerRequest(discovery_target="172.22.0.21", timeout_ms=1000)
    await discovery_module._run_discovery(body)
    assert capture_needs_broadcast == [False]


@pytest.mark.asyncio
async def test_broadcast_discovery_uses_real_port(capture_needs_broadcast):
    body = DiscoveryTriggerRequest(discovery_target=None, timeout_ms=1000)
    await discovery_module._run_discovery(body)
    assert capture_needs_broadcast == [True]
