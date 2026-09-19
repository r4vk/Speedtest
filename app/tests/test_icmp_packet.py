"""Unit tests for the pure ICMP packet helpers (design spec §4.2)."""
from __future__ import annotations

import socket
import struct

import pytest

from speedtest_app.icmp_probe import (
    ICMP_ECHO_REPLY,
    ICMP_ECHO_REQUEST,
    ICMPV6_ECHO_REPLY,
    ICMPV6_ECHO_REQUEST,
    build_echo_request,
    icmp_checksum,
    parse_echo_reply,
)

TOKEN = bytes(range(16))


def ipv4_header(payload_len: int, proto: int = 1) -> bytes:
    """A minimal, well formed IPv4 header (20 bytes, no options)."""
    header = bytes([0x45, 0x00]) + struct.pack("!HHHBBH", 20 + payload_len, 0, 0, 64, proto, 0)
    return header + socket.inet_aton("1.1.1.1") + socket.inet_aton("192.168.1.10")


def echo_reply(ident: int, seq: int, payload: bytes) -> bytes:
    """Turn our own request into the reply the peer would send back."""
    body = struct.pack("!BBHHH", ICMP_ECHO_REPLY, 0, 0, ident, seq) + payload
    checksum = icmp_checksum(body)
    return struct.pack("!BBHHH", ICMP_ECHO_REPLY, 0, checksum, ident, seq) + payload


# ---------------------------------------------------------------------------
# checksum
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"", 0xFFFF),
        (b"\x00\x00", 0xFFFF),
        (b"\x08\x00\x00\x00\x12\x34\x00\x01", 0xE5CA),
        (b"\x01\x02\x03", 0xFBFD),  # odd length is right-padded with a zero byte
    ],
)
def test_icmp_checksum_known_vectors(data: bytes, expected: int) -> None:
    assert icmp_checksum(data) == expected


def test_checksum_of_a_complete_packet_verifies_to_zero() -> None:
    packet = build_echo_request(0x1234, 7, TOKEN)
    assert icmp_checksum(packet) == 0


def test_checksum_detects_a_corrupted_packet() -> None:
    packet = bytearray(build_echo_request(0x1234, 7, TOKEN))
    packet[-1] ^= 0xFF
    assert icmp_checksum(bytes(packet)) != 0


# ---------------------------------------------------------------------------
# build / parse
# ---------------------------------------------------------------------------


def test_build_echo_request_layout() -> None:
    packet = build_echo_request(0xBEEF, 42, TOKEN)
    icmp_type, code, checksum, ident, seq = struct.unpack("!BBHHH", packet[:8])
    assert (icmp_type, code) == (ICMP_ECHO_REQUEST, 0)
    assert (ident, seq) == (0xBEEF, 42)
    assert checksum != 0
    assert packet[8:] == TOKEN


def test_round_trip_ipv4_without_ip_header() -> None:
    packet = build_echo_request(0xBEEF, 42, TOKEN)
    ident, seq, payload = parse_echo_reply(echo_reply(0xBEEF, 42, TOKEN), socket.AF_INET)
    assert (ident, seq, payload) == (0xBEEF, 42, TOKEN)
    assert packet[8:] == payload


def test_round_trip_ipv4_with_ip_header() -> None:
    """macOS delivers the IPv4 header on datagram ICMP sockets; Linux does not."""
    reply = echo_reply(0xBEEF, 42, TOKEN)
    with_header = ipv4_header(len(reply)) + reply
    assert parse_echo_reply(with_header, socket.AF_INET) == (0xBEEF, 42, TOKEN)


def test_round_trip_ipv4_with_ip_header_carrying_options() -> None:
    reply = echo_reply(1, 2, TOKEN)
    header = bytearray(ipv4_header(len(reply)))
    header[0] = 0x46  # IHL = 6 words = 24 bytes
    with_header = bytes(header) + b"\x00\x00\x00\x00" + reply
    assert parse_echo_reply(with_header, socket.AF_INET) == (1, 2, TOKEN)


def test_ipv6_request_leaves_the_checksum_to_the_kernel() -> None:
    packet = build_echo_request(0xAAAA, 3, TOKEN, socket.AF_INET6)
    icmp_type, code, checksum, ident, seq = struct.unpack("!BBHHH", packet[:8])
    assert (icmp_type, code, checksum) == (ICMPV6_ECHO_REQUEST, 0, 0)
    assert (ident, seq) == (0xAAAA, 3)


def test_ipv6_reply_parses_without_an_ip_header() -> None:
    reply = struct.pack("!BBHHH", ICMPV6_ECHO_REPLY, 0, 0x1234, 0xAAAA, 3) + TOKEN
    assert parse_echo_reply(reply, socket.AF_INET6) == (0xAAAA, 3, TOKEN)


@pytest.mark.parametrize(
    "data,family",
    [
        (b"", socket.AF_INET),
        (b"\x00\x00\x00", socket.AF_INET),
        (struct.pack("!BBHHH", 3, 1, 0, 0, 0) + TOKEN, socket.AF_INET),  # unreachable
        (struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, 1, 1) + TOKEN, socket.AF_INET),
        (struct.pack("!BBHHH", ICMP_ECHO_REPLY, 0, 0, 1, 1) + TOKEN, socket.AF_INET6),
        (struct.pack("!BBHHH", ICMPV6_ECHO_REPLY, 0, 0, 1, 1) + TOKEN, socket.AF_INET),
    ],
)
def test_parse_echo_reply_rejects_what_is_not_an_echo_reply(data: bytes, family: int) -> None:
    assert parse_echo_reply(data, family) is None


def test_a_reply_for_another_request_is_distinguishable() -> None:
    """The caller matches on seq + token, never on the identifier (§4.2)."""
    ours = parse_echo_reply(echo_reply(0xBEEF, 42, TOKEN), socket.AF_INET)
    other_seq = parse_echo_reply(echo_reply(0xBEEF, 43, TOKEN), socket.AF_INET)
    other_token = parse_echo_reply(echo_reply(0xBEEF, 42, bytes(16)), socket.AF_INET)
    rewritten_ident = parse_echo_reply(echo_reply(0x0001, 42, TOKEN), socket.AF_INET)
    assert ours is not None and other_seq is not None and other_token is not None
    assert rewritten_ident is not None
    assert other_seq[1] != ours[1]
    assert other_token[2] != ours[2]
    assert (rewritten_ident[1], rewritten_ident[2]) == (ours[1], ours[2])
