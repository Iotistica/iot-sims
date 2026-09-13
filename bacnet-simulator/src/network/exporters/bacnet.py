import io
import struct
import socket
import ipaddress

def export_pcap(packets) -> bytes:
    """
    Return a classic PCAP containing reconstructed Ethernet/IPv4/UDP frames.

    Link type 1 means Ethernet.
    """

    for packet in packets:
        if packet.protocol != "bacnet" or packet.transport != "udp":
            raise ValueError("PCAP export supports BACnet/UDP only")

    output = io.BytesIO()

    # PCAP global header, little-endian.
    output.write(
        struct.pack(
            "<IHHIIII",
            0xA1B2C3D4,  # magic
            2,           # major
            4,           # minor
            0,           # timezone
            0,           # timestamp accuracy
            65_535,      # snapshot length
            1,           # Ethernet
        )
    )

    for packet in packets:
        frame = build_udp_ethernet_frame(
            source_ip=packet.source.address,
            destination_ip=packet.destination.address,
            source_port=packet.source.port,
            destination_port=packet.destination.port,
            payload=packet.payload,
        )

        seconds = int(packet.timestamp)
        microseconds = int(
            (packet.timestamp - seconds) * 1_000_000
        )

        output.write(
            struct.pack(
                "<IIII",
                seconds,
                microseconds,
                len(frame),
                len(frame),
            )
        )
        output.write(frame)

    return output.getvalue()

def build_udp_ethernet_frame(
    *,
    source_ip: str,
    destination_ip: str,
    source_port: int,
    destination_port: int,
    payload: bytes,
) -> bytes:
    if len(payload) > 65_507:
        raise ValueError("Payload exceeds IPv4 UDP frame capacity")
    source_ip_bytes = ipaddress.ip_address(source_ip).packed
    destination_ip_bytes = ipaddress.ip_address(destination_ip).packed

    if len(source_ip_bytes) != 4 or len(destination_ip_bytes) != 4:
        raise ValueError("Initial PCAP implementation supports IPv4 only")

    # Synthetic MAC addresses. Wireshark only needs a valid Ethernet frame.
    destination_mac = b"\x02\x00\x00\x00\x00\x02"
    source_mac = b"\x02\x00\x00\x00\x00\x01"
    ethernet_type_ipv4 = b"\x08\x00"

    ethernet_header = (
        destination_mac
        + source_mac
        + ethernet_type_ipv4
    )

    udp_length = 8 + len(payload)
    total_ip_length = 20 + udp_length

    ip_header_without_checksum = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,                # IPv4 + 20-byte header
        0,                   # DSCP/ECN
        total_ip_length,
        0,                   # identification
        0,                   # flags/fragment offset
        64,                  # TTL
        socket.IPPROTO_UDP,
        0,                   # checksum placeholder
        source_ip_bytes,
        destination_ip_bytes,
    )

    ip_checksum = internet_checksum(ip_header_without_checksum)

    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_ip_length,
        0,
        0,
        64,
        socket.IPPROTO_UDP,
        ip_checksum,
        source_ip_bytes,
        destination_ip_bytes,
    )

    udp_header_without_checksum = struct.pack(
        "!HHHH",
        source_port,
        destination_port,
        udp_length,
        0,
    )

    pseudo_header = struct.pack(
        "!4s4sBBH",
        source_ip_bytes,
        destination_ip_bytes,
        0,
        socket.IPPROTO_UDP,
        udp_length,
    )

    udp_checksum = internet_checksum(
        pseudo_header + udp_header_without_checksum + payload
    )

    if udp_checksum == 0:
        udp_checksum = 0xFFFF

    udp_header = struct.pack(
        "!HHHH",
        source_port,
        destination_port,
        udp_length,
        udp_checksum,
    )

    return ethernet_header + ip_header + udp_header + payload


def internet_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"

    words = struct.unpack(f"!{len(data) // 2}H", data)
    total = sum(words)

    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)

    return (~total) & 0xFFFF


