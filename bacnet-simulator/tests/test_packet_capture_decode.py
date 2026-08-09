"""Who-Is/I-Am parameter decoding added to src/bacnet/packet_capture.py for
simulator device association. Before this, decode_bacnet_summary() only
extracted service_choice/service_name for unconfirmed services -- no
device_instance was ever exposed, so Who-Is/I-Am packets could never be
associated with a simulator device. Pure decode tests: no DB, no engine,
no simulator dependency (matches the module's own constraint)."""
from __future__ import annotations

from src.bacnet.packet_capture import decode_bacnet_summary


# I-Am, device_instance=1003, max-apdu=1476, segmentation=no-segmentation(3),
# vendor-id=15. BVLC(4) + NPDU(2) + APDU(14) = 20 bytes total.
I_AM_DEVICE_1003 = bytes.fromhex(
    "81 0a 00 14 01 00 10 00 c4 02 00 03 eb 22 05 c4 91 03 21 0f"
)

# Same shape, device_instance=4194302 (the schema's max, near the 22-bit
# ceiling) -- object identifier is always a fixed 4 bytes regardless of
# value, so the packet is the same length.
I_AM_DEVICE_MAX_INSTANCE = bytes.fromhex(
    "81 0a 00 14 01 00 10 00 c4 02 3f ff fe 22 05 c4 91 03 21 0f"
)

# Directed Who-Is, range low=high=1003. BVLC(4)+NPDU(2)+APDU(8) = 14 bytes.
WHO_IS_DIRECTED_1003 = bytes.fromhex(
    "81 0a 00 0e 01 00 10 08 22 03 eb 22 03 eb"
)

# Broadcast Who-Is, no range params. BVLC(4)+NPDU(2)+APDU(2) = 8 bytes.
WHO_IS_BROADCAST = bytes.fromhex(
    "81 0b 00 08 01 00 10 08"
)


def test_i_am_decodes_device_instance():
    result = decode_bacnet_summary(I_AM_DEVICE_1003)

    assert result["decode_status"] == "decoded"
    assert result["service_name"] == "I-Am"
    assert result["summary"]["device_instance"] == 1003
    assert result["service"]["max_apdu_length_accepted"] == 1476
    assert result["service"]["vendor_id"] == 15


def test_i_am_near_max_instance_does_not_crash():
    result = decode_bacnet_summary(I_AM_DEVICE_MAX_INSTANCE)

    assert result["decode_status"] == "decoded"
    assert result["summary"]["device_instance"] == 4194302


def test_directed_who_is_decodes_matching_range():
    result = decode_bacnet_summary(WHO_IS_DIRECTED_1003)

    assert result["decode_status"] == "decoded"
    assert result["service_name"] == "Who-Is"
    assert result["summary"]["device_instance_range_low"] == 1003
    assert result["summary"]["device_instance_range_high"] == 1003


def test_broadcast_who_is_decodes_no_range():
    result = decode_bacnet_summary(WHO_IS_BROADCAST)

    assert result["decode_status"] == "decoded"
    assert result["service_name"] == "Who-Is"
    assert "device_instance_range_low" not in result["summary"]
    assert "device_instance_range_high" not in result["summary"]
