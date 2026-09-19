"""TCP probe tests (design spec §4.2).

The happy path uses a real listener on 127.0.0.1 (loopback only, no network);
every failure mode is simulated by monkeypatching ``asyncio.open_connection`` or
the resolver.
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any, AsyncIterator

import pytest

from speedtest_app import tcp_probe
from speedtest_app.probe_types import Outcome, ProbeTarget, Protocol


def make_target(**overrides: Any) -> ProbeTarget:
    fields: dict[str, Any] = dict(
        id=5,
        name="legacy-tcp",
        kind="tcp",
        protocol=Protocol.TCP,
        host="127.0.0.1",
        port=9,
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=True,
        family_pref="ipv4",
        extra={},
    )
    fields.update(overrides)
    return ProbeTarget(**fields)


@pytest.fixture
async def listening_port() -> AsyncIterator[int]:
    """A server on 127.0.0.1 that accepts and immediately forgets the peer."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


def free_port() -> int:
    """A port that nothing listens on (bound and released straight away)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_open_port_is_ok_with_stage_timings(listening_port: int) -> None:
    result = await tcp_probe.probe(make_target(port=listening_port))

    assert result.outcome is Outcome.OK
    assert result.error_kind is None
    assert result.rtt_ms is not None and result.rtt_ms >= 0
    assert result.stages is not None
    assert set(result.stages) == {"dns_ms", "connect_ms"}
    assert result.stages["connect_ms"] == pytest.approx(result.rtt_ms)
    assert result.duration_ms >= result.rtt_ms
    assert (result.resolved_ip, result.ip_family) == ("127.0.0.1", 4)
    assert result.timeout_ms == 1000


async def test_closed_port_is_refused() -> None:
    result = await tcp_probe.probe(make_target(port=free_port()))

    assert result.outcome is Outcome.ERROR
    assert result.error_kind == "tcp_refused"
    assert result.error_detail is not None
    assert result.rtt_ms is None
    assert result.stages is not None and "connect_ms" in result.stages


async def test_hanging_connect_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never_connects(*args: Any, **kwargs: Any) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(tcp_probe.asyncio, "open_connection", never_connects)
    result = await tcp_probe.probe(make_target(timeout_ms=25))

    assert result.outcome is Outcome.TIMEOUT
    assert result.error_kind is None
    assert result.rtt_ms is None
    assert result.duration_ms >= 25
    assert result.stages is not None and set(result.stages) == {"dns_ms", "connect_ms"}


async def test_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> list:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(tcp_probe.socket, "getaddrinfo", boom)
    result = await tcp_probe.probe(make_target(host="nope.invalid"))

    assert result.outcome is Outcome.ERROR
    assert result.error_kind == "dns"
    assert result.resolved_ip is None
    assert result.stages is not None and set(result.stages) == {"dns_ms"}


@pytest.mark.parametrize(
    "exception,expected",
    [
        (ConnectionResetError("reset by peer"), "tcp_reset"),
        (OSError(51, "Network is unreachable"), "tcp_error"),
        (ConnectionRefusedError(61, "Connection refused"), "tcp_refused"),
    ],
)
async def test_connect_errors_are_categorised(
    monkeypatch: pytest.MonkeyPatch, exception: Exception, expected: str
) -> None:
    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise exception

    monkeypatch.setattr(tcp_probe.asyncio, "open_connection", failing)
    result = await tcp_probe.probe(make_target())

    assert result.outcome is Outcome.ERROR
    assert result.error_kind == expected
    assert result.error_detail is not None and type(exception).__name__ in result.error_detail


async def test_target_without_a_port_is_a_visible_error() -> None:
    result = await tcp_probe.probe(make_target(port=None))
    assert (result.outcome, result.error_kind) == (Outcome.ERROR, "exec")
    assert result.error_detail == "target has no port"


async def test_explicit_timeout_argument_wins(listening_port: int) -> None:
    result = await tcp_probe.probe(make_target(port=listening_port), timeout_ms=750)
    assert result.timeout_ms == 750
    assert result.outcome is Outcome.OK
