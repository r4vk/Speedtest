"""ICMP probe tests against a fake socket layer and a fake clock (spec §4.2).

Nothing here opens a real socket or waits on real time: the module's socket
factory, its send/recv seams and ``perf_counter`` are all monkeypatched. The one
test that touches the loopback interface is marked ``network``.
"""
from __future__ import annotations

import socket
import struct
from typing import Any, Iterator

import pytest

from speedtest_app import icmp_probe
from speedtest_app.probe_types import Outcome, ProbeTarget, Protocol

from test_icmp_packet import echo_reply, ipv4_header

RESOLVED = "1.1.1.1"


@pytest.fixture(autouse=True)
def clean_icmp_method_cache() -> Iterator[None]:
    icmp_probe.reset_icmp_method_cache()
    yield
    icmp_probe.reset_icmp_method_cache()


@pytest.fixture(autouse=True)
def fake_resolver(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolution never leaves the process (except in the `network` test)."""
    if request.node.get_closest_marker("network") is not None:
        return

    def getaddrinfo(host: str, port: Any, family: int = 0, *args: Any, **kwargs: Any) -> list:
        if family == socket.AF_INET6:
            return [(socket.AF_INET6, socket.SOCK_DGRAM, 0, "", ("2606:4700::1111", 0, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", (RESOLVED, 0))]

    monkeypatch.setattr(icmp_probe.socket, "getaddrinfo", getaddrinfo)


class FakeSocket:
    def __init__(self) -> None:
        self.closed = False
        self.blocking = True

    def setblocking(self, flag: bool) -> None:
        self.blocking = flag

    def close(self) -> None:
        self.closed = True


class FakeClock:
    """``perf_counter`` that advances a fixed step on every call."""

    def __init__(self, step: float = 0.001) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def make_target(**overrides: Any) -> ProbeTarget:
    fields: dict[str, Any] = dict(
        id=1,
        name="cloudflare-dns",
        kind="internet",
        protocol=Protocol.ICMP,
        host="1.1.1.1",
        port=None,
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=True,
        family_pref="ipv4",
        extra={},
    )
    fields.update(overrides)
    return ProbeTarget(**fields)


def install_socket_layer(
    monkeypatch: pytest.MonkeyPatch, replies: Any, *, step: float = 0.001
) -> tuple[FakeSocket, list[bytes]]:
    """Patch the module's socket seams; ``replies`` yields the bytes recv returns."""
    sock = FakeSocket()
    sent: list[bytes] = []

    def open_socket(family: int, sock_type: int, proto: int) -> FakeSocket:
        return sock

    async def sendto(target_sock: Any, data: bytes, address: Any) -> None:
        sent.append(data)

    async def recv(target_sock: Any, bufsize: int) -> bytes:
        return replies(sent) if callable(replies) else replies.pop(0)

    monkeypatch.setattr(icmp_probe, "_open_socket", open_socket)
    monkeypatch.setattr(icmp_probe, "_sock_sendto", sendto)
    monkeypatch.setattr(icmp_probe, "_sock_recv", recv)
    monkeypatch.setattr(icmp_probe, "perf_counter", FakeClock(step))
    return sock, sent


def sent_ident_seq(packet: bytes) -> tuple[int, int]:
    _type, _code, _checksum, ident, seq = struct.unpack("!BBHHH", packet[:8])
    return ident, seq


# ---------------------------------------------------------------------------
# method detection
# ---------------------------------------------------------------------------


def test_detect_icmp_method_returns_a_known_value_and_caches_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    method = icmp_probe.detect_icmp_method()
    assert method in {"dgram", "raw", "ping", "unavailable"}

    monkeypatch.setattr(icmp_probe, "_try_socket", lambda *args: False)
    monkeypatch.setattr(icmp_probe.shutil, "which", lambda name: None)
    assert icmp_probe.detect_icmp_method() == method  # cached, not re-detected

    icmp_probe.reset_icmp_method_cache()
    assert icmp_probe.detect_icmp_method() == "unavailable"


@pytest.mark.parametrize(
    "dgram_ok,raw_ok,ping_path,expected",
    [
        (True, True, "/sbin/ping", "dgram"),
        (False, True, "/sbin/ping", "raw"),
        (False, False, "/sbin/ping", "ping"),
        (False, False, None, "unavailable"),
    ],
)
def test_detection_ladder_order(
    monkeypatch: pytest.MonkeyPatch,
    dgram_ok: bool,
    raw_ok: bool,
    ping_path: str | None,
    expected: str,
) -> None:
    def try_socket(family: int, sock_type: int, proto: int) -> bool:
        return dgram_ok if sock_type == socket.SOCK_DGRAM else raw_ok

    monkeypatch.setattr(icmp_probe, "_try_socket", try_socket)
    monkeypatch.setattr(icmp_probe.shutil, "which", lambda name: ping_path)
    assert icmp_probe.detect_icmp_method() == expected


def test_detection_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any) -> bool:
        raise OSError("no sockets today")

    monkeypatch.setattr(icmp_probe, "_try_socket", boom)
    monkeypatch.setattr(icmp_probe.shutil, "which", lambda name: None)
    assert icmp_probe.detect_icmp_method() == "unavailable"


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


async def test_reply_yields_ok_with_a_plausible_rtt(monkeypatch: pytest.MonkeyPatch) -> None:
    def replies(sent: list[bytes]) -> bytes:
        ident, seq = sent_ident_seq(sent[-1])
        return echo_reply(ident, seq, sent[-1][8:])

    sock, sent = install_socket_layer(monkeypatch, replies)
    result = await icmp_probe.probe(make_target(), method="dgram")

    assert result.outcome is Outcome.OK
    assert result.error_kind is None
    assert result.rtt_ms == pytest.approx(2.0)
    assert result.duration_ms >= result.rtt_ms
    assert (result.resolved_ip, result.ip_family) == (RESOLVED, 4)
    assert result.timeout_ms == 1000
    assert len(sent) == 1
    assert len(sent[0]) == 8 + icmp_probe.PAYLOAD_SIZE
    assert sock.closed


async def test_reply_is_accepted_when_the_kernel_rewrote_the_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def replies(sent: list[bytes]) -> bytes:
        _ident, seq = sent_ident_seq(sent[-1])
        return echo_reply(0xFFFF, seq, sent[-1][8:])  # a different ident

    install_socket_layer(monkeypatch, replies)
    result = await icmp_probe.probe(make_target(), method="dgram")
    assert result.outcome is Outcome.OK


async def test_reply_with_the_macos_ip_header_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def replies(sent: list[bytes]) -> bytes:
        ident, seq = sent_ident_seq(sent[-1])
        reply = echo_reply(ident, seq, sent[-1][8:])
        return ipv4_header(len(reply)) + reply

    install_socket_layer(monkeypatch, replies)
    result = await icmp_probe.probe(make_target(), method="dgram")
    assert result.outcome is Outcome.OK


async def test_no_reply_yields_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def replies(sent: list[bytes]) -> bytes:
        ident, seq = sent_ident_seq(sent[-1])
        return echo_reply(ident, (seq + 1) & 0xFFFF, sent[-1][8:])  # never ours

    sock, _sent = install_socket_layer(monkeypatch, replies, step=0.3)
    result = await icmp_probe.probe(make_target(timeout_ms=1000), method="dgram")

    assert result.outcome is Outcome.TIMEOUT
    assert result.error_kind is None
    assert result.rtt_ms is None
    assert result.duration_ms >= 1000
    assert result.resolved_ip == RESOLVED
    assert sock.closed


@pytest.mark.parametrize("with_ip_header", [False, True])
async def test_destination_unreachable_is_an_error_not_a_loss(
    monkeypatch: pytest.MonkeyPatch, with_ip_header: bool
) -> None:
    def replies(sent: list[bytes]) -> bytes:
        quoted = ipv4_header(len(sent[-1])) + sent[-1]
        message = struct.pack("!BBHI", icmp_probe.ICMP_DEST_UNREACH, 1, 0, 0) + quoted
        return ipv4_header(len(message)) + message if with_ip_header else message

    install_socket_layer(monkeypatch, replies)
    result = await icmp_probe.probe(make_target(), method="dgram")

    assert result.outcome is Outcome.ERROR
    assert result.error_kind == "icmp_unreachable"
    assert result.error_detail is not None and RESOLVED in result.error_detail
    assert result.rtt_ms is None


async def test_unreachable_quoting_another_request_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def replies(sent: list[bytes]) -> bytes:
        foreign = icmp_probe.build_echo_request(1, 0xABCD, bytes(16))
        quoted = ipv4_header(len(foreign)) + foreign
        return struct.pack("!BBHI", icmp_probe.ICMP_DEST_UNREACH, 1, 0, 0) + quoted

    install_socket_layer(monkeypatch, replies, step=0.3)
    result = await icmp_probe.probe(make_target(timeout_ms=1000), method="dgram")
    assert result.outcome is Outcome.TIMEOUT


async def test_permission_error_on_socket_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    def open_socket(family: int, sock_type: int, proto: int) -> Any:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(icmp_probe, "_open_socket", open_socket)
    result = await icmp_probe.probe(make_target(), method="raw")

    assert result.outcome is Outcome.ERROR
    assert result.error_kind == "permission"
    assert result.error_detail is not None and "PermissionError" in result.error_detail
    assert result.resolved_ip == RESOLVED


async def test_other_socket_errors_are_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    def open_socket(family: int, sock_type: int, proto: int) -> Any:
        raise OSError(51, "Network is unreachable")

    monkeypatch.setattr(icmp_probe, "_open_socket", open_socket)
    result = await icmp_probe.probe(make_target(), method="dgram")
    assert (result.outcome, result.error_kind) == (Outcome.ERROR, "exec")


async def test_unavailable_method_is_reported_as_permission() -> None:
    result = await icmp_probe.probe(make_target(), method="unavailable")
    assert (result.outcome, result.error_kind) == (Outcome.ERROR, "permission")
    assert result.error_detail == "no ICMP method available"


async def test_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> list:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(icmp_probe.socket, "getaddrinfo", boom)
    result = await icmp_probe.probe(make_target(host="nope.invalid"), method="dgram")

    assert (result.outcome, result.error_kind) == (Outcome.ERROR, "dns")
    assert result.resolved_ip is None


async def test_sequence_numbers_increase_per_target(monkeypatch: pytest.MonkeyPatch) -> None:
    def replies(sent: list[bytes]) -> bytes:
        ident, seq = sent_ident_seq(sent[-1])
        return echo_reply(ident, seq, sent[-1][8:])

    _sock, sent = install_socket_layer(monkeypatch, replies)
    target = make_target(id=4242)
    await icmp_probe.probe(target, method="dgram")
    await icmp_probe.probe(target, method="dgram")

    first, second = (sent_ident_seq(packet)[1] for packet in sent)
    assert second == (first + 1) & 0xFFFF


@pytest.mark.network
async def test_loopback_echo() -> None:
    """Real ICMP against 127.0.0.1; needs `-m network` and a usable method."""
    target = make_target(host="127.0.0.1", timeout_ms=2000)
    result = await icmp_probe.probe(target)
    assert result.resolved_ip == "127.0.0.1"
    if result.outcome is Outcome.OK:
        assert result.rtt_ms is not None and 0 <= result.rtt_ms < 2000
    else:
        assert result.error_kind in {"permission", "exec", "icmp_unreachable"}
