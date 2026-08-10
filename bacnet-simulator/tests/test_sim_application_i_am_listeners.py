"""
Regression/behavioral tests for SimApplication's I-Am handling
(src/legacy.py): existing duplicate-device-ID detection, chaining to
bacpypes3's own WhoIsIAmServices.do_IAmRequest (previously unreachable --
see the plan notes for why), and the new permanent add_i_am_listener()/
remove_i_am_listener() mechanism used by src/bacnet/client/transport.py's
targeted-discovery I-Am collector.

SimApplication() is constructed bare (no bound socket/link layer) -- that's
enough for do_IAmRequest, since it only touches its own fields, metrics, and
(via super()) the WhoIsIAmServices _who_is_futures list. It does NOT call
.request()/.who_is(), which do require a bound stack.
"""
from __future__ import annotations

import pytest
from bacpypes3.apdu import IAmRequest
from bacpypes3.pdu import Address as Bp3Address

from src.legacy import SimApplication, metrics


def _make_i_am(device_instance: int, address: str, vendor_id: int = 999) -> IAmRequest:
    i_am = IAmRequest(
        iAmDeviceIdentifier=("device", device_instance),
        maxAPDULengthAccepted=1024,
        segmentationSupported="noSegmentation",
        vendorID=vendor_id,
    )
    i_am.pduSource = Bp3Address(address)
    return i_am


class _FakeWhoIsFuture:
    """Stands in for bacpypes3's own WhoIsFuture -- only .match() matters
    for confirming do_IAmRequest chains to super()."""

    def __init__(self) -> None:
        self.matched_with: list = []

    def match(self, apdu) -> None:
        self.matched_with.append(apdu)


# ── 1. Existing duplicate-device-ID detection still fires (regression) ─────

@pytest.mark.asyncio
async def test_duplicate_device_id_detection_unchanged():
    app = SimApplication()
    app._own_ip = "10.0.0.5"
    app._virtual_devices = {1001: object()}  # membership-only check, no attrs touched

    before = len(metrics.duplicate_id_events)
    i_am = _make_i_am(1001, "10.0.0.99")  # different source than our own IP

    await app.do_IAmRequest(i_am)

    assert len(metrics.duplicate_id_events) == before + 1
    assert metrics.duplicate_id_events[-1]["device_instance"] == 1001


# ── 2. add_i_am_listener()/remove_i_am_listener() work ──────────────────────

@pytest.mark.asyncio
async def test_registered_listener_is_notified():
    app = SimApplication()
    received: list = []
    app.add_i_am_listener(received.append)

    i_am = _make_i_am(2001, "10.0.0.50")
    await app.do_IAmRequest(i_am)

    assert received == [i_am]


@pytest.mark.asyncio
async def test_removed_listener_is_not_notified():
    app = SimApplication()
    received: list = []
    listener = received.append
    app.add_i_am_listener(listener)
    app.remove_i_am_listener(listener)

    await app.do_IAmRequest(_make_i_am(2002, "10.0.0.51"))

    assert received == []


# ── 3. do_IAmRequest now chains to super(), restoring _who_is_futures matching ──

@pytest.mark.asyncio
async def test_do_i_am_request_chains_to_super_who_is_futures():
    app = SimApplication()
    fake_future = _FakeWhoIsFuture()
    app._who_is_futures = [fake_future]

    i_am = _make_i_am(2003, "10.0.0.52")
    await app.do_IAmRequest(i_am)

    assert fake_future.matched_with == [i_am]


# ── 4. A listener that raises doesn't break duplicate-ID detection or other listeners ──

@pytest.mark.asyncio
async def test_raising_listener_does_not_break_others_or_duplicate_detection():
    app = SimApplication()
    app._own_ip = "10.0.0.5"
    app._virtual_devices = {1001: object()}

    def bad_listener(apdu):
        raise RuntimeError("simulated listener failure")

    received: list = []
    app.add_i_am_listener(bad_listener)
    app.add_i_am_listener(received.append)

    before = len(metrics.duplicate_id_events)
    i_am = _make_i_am(1001, "10.0.0.99")

    await app.do_IAmRequest(i_am)  # must not raise

    assert received == [i_am]
    assert len(metrics.duplicate_id_events) == before + 1


# ── 5. remove_i_am_listener() for an unregistered listener is a safe no-op ──

def test_remove_unregistered_listener_is_noop():
    app = SimApplication()
    app.remove_i_am_listener(lambda apdu: None)  # never added -- must not raise
    assert app._i_am_listeners == []
