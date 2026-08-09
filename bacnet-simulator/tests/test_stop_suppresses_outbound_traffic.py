"""When the simulation is Paused or Stopped (clock_state != "running"),
the simulator must stop RESPONDING on the network -- no outbound BACnet
traffic at all -- while still being allowed to receive (a real paused/
stopped controller can't stop other devices broadcasting Who-Is at it,
it just doesn't answer). See install_bacpypes_packet_capture_hooks() in
src/legacy.py, which is the single choke point every outbound byte
passes through regardless of what generated it. Pause and Stop still
differ elsewhere (Stop rewinds elapsed_seconds/time_of_day to 0, Pause
leaves them in place so Resume picks up without losing simulated time)
-- outbound suppression is the one thing they now share.

NOTE: install_bacpypes_packet_capture_hooks() monkey-patches the real
bacpypes3 IPv4DatagramServer CLASS, guarded by a module-level
"already installed" flag -- it's a process-wide, one-time installation
by design (mirroring how it's only ever installed once per real process
via SimEngine.start()). This must be the ONLY test in the suite that
calls it, so it controls exactly which get_clock_state callback ends up
installed."""
from __future__ import annotations

import pytest

from src import legacy


class _FakePDU:
    def __init__(self, data: bytes = b"\x01\x02\x03"):
        self.pduData = data
        self.pduDestination = None
        self.pduSource = None


@pytest.mark.asyncio
async def test_outbound_suppressed_while_paused_or_stopped():
    assert not legacy._bacpypes_capture_hooks_installed, (
        "hook already installed by another test -- this test must be the "
        "sole installer to control get_clock_state; see module docstring"
    )

    state = {"clock_state": "running"}
    sent: list[bytes] = []

    original_indication = legacy.IPv4DatagramServer.indication

    async def fake_original_indication(transport_self, pdu):
        sent.append(bytes(pdu.pduData))

    legacy.IPv4DatagramServer.indication = fake_original_indication
    try:
        legacy.install_bacpypes_packet_capture_hooks(
            local_ip="127.0.0.1", local_port=47808,
            get_clock_state=lambda: state["clock_state"],
        )
        patched_indication = legacy.IPv4DatagramServer.indication

        # Running -- outbound goes through normally.
        state["clock_state"] = "running"
        await patched_indication(None, _FakePDU(b"running-packet"))
        assert sent == [b"running-packet"]

        # Paused -- outbound is suppressed entirely, never reaches the
        # real transport.
        state["clock_state"] = "paused"
        await patched_indication(None, _FakePDU(b"paused-packet"))
        assert sent == [b"running-packet"]  # unchanged

        # Stopped -- also suppressed entirely.
        state["clock_state"] = "stopped"
        await patched_indication(None, _FakePDU(b"stopped-packet"))
        assert sent == [b"running-packet"]  # unchanged

        # Resuming makes outbound flow again -- no socket rebuild needed.
        state["clock_state"] = "running"
        await patched_indication(None, _FakePDU(b"resumed-packet"))
        assert sent == [b"running-packet", b"resumed-packet"]
    finally:
        # Restore so this monkey-patch doesn't leak into whatever bacpypes3
        # machinery a later test/process might rely on.
        legacy.IPv4DatagramServer.indication = original_indication
