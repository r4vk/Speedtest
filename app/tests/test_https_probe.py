"""Tests for speedtest_app.https_probe: HTTP(S) probe with manual stage timing
(spec sec 4.2/4.3).

Every test drives a local ``asyncio.start_server`` on 127.0.0.1 - no real
network or real internet host is touched. The TLS tests use the self-signed
fixture pair committed under ``tests/fixtures/tls/``.
"""
from __future__ import annotations

import asyncio
import ssl
from pathlib import Path

import pytest

from speedtest_app import https_probe
from speedtest_app.probe_types import Outcome, ProbeTarget, Protocol

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tls"
CERT_PATH = FIXTURES_DIR / "cert.pem"
KEY_PATH = FIXTURES_DIR / "key.pem"


def make_target(url: str, **overrides) -> ProbeTarget:
    defaults = dict(
        id=1,
        name="https-test",
        kind="https",
        protocol=Protocol.HTTPS,
        host=url,
        port=None,
        interval_seconds=60.0,
        timeout_ms=2000,
        enabled=True,
        family_pref="ipv4",
        extra={},
    )
    defaults.update(overrides)
    return ProbeTarget(**defaults)


class Server:
    """A tiny asyncio TCP/TLS server that hands raw bytes to a handler."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port


async def start_plain_server(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def start_tls_server(handler):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_PATH), str(KEY_PATH))
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _drain_request(reader: asyncio.StreamReader) -> None:
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        pass


def respond_204(_reader=None):
    async def handler(reader, writer):
        await _drain_request(reader)
        writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
        await writer.drain()
        writer.close()

    return handler


def respond_503():
    async def handler(reader, writer):
        await _drain_request(reader)
        writer.write(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
        await writer.drain()
        writer.close()

    return handler


def respond_garbage():
    async def handler(reader, writer):
        await _drain_request(reader)
        writer.write(b"not an http response at all, just garbage bytes")
        await writer.drain()
        writer.close()

    return handler


def respond_never():
    async def handler(reader, writer):
        await _drain_request(reader)
        # Long enough to outlast the probe's timeout + hard-guard slack, short
        # enough to keep the test fast; the connection is dropped afterwards
        # so server.wait_closed() below does not hang.
        await asyncio.sleep(1.0)
        writer.close()

    return handler


class TestHttpsProbePlainHttp:
    async def test_ok_204_has_stage_timings_and_no_tls(self):
        server, port = await start_plain_server(respond_204())
        try:
            target = make_target(f"http://127.0.0.1:{port}/status")
            result = await https_probe.probe(target)
        finally:
            server.close()
            await server.wait_closed()

        assert result.outcome == Outcome.OK
        assert result.protocol == Protocol.HTTPS
        assert result.error_kind is None
        assert result.resolved_ip == "127.0.0.1"
        assert result.ip_family == 4
        assert result.stages is not None
        assert "dns_ms" in result.stages
        assert "connect_ms" in result.stages
        assert "ttfb_ms" in result.stages
        assert "tls_ms" not in result.stages
        assert result.rtt_ms is not None and result.rtt_ms >= 0

    async def test_status_503_is_http_status_error(self):
        server, port = await start_plain_server(respond_503())
        try:
            target = make_target(f"http://127.0.0.1:{port}/")
            result = await https_probe.probe(target)
        finally:
            server.close()
            await server.wait_closed()

        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "http_status"
        assert result.error_detail == "503"

    async def test_garbage_response_is_http_protocol_error(self):
        server, port = await start_plain_server(respond_garbage())
        try:
            target = make_target(f"http://127.0.0.1:{port}/")
            result = await https_probe.probe(target)
        finally:
            server.close()
            await server.wait_closed()

        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "http_protocol"

    async def test_server_never_responds_times_out_on_ttfb(self):
        server, port = await start_plain_server(respond_never())
        try:
            target = make_target(f"http://127.0.0.1:{port}/", timeout_ms=300)
            result = await https_probe.probe(target)
        finally:
            server.close()
            await server.wait_closed()

        assert result.outcome == Outcome.TIMEOUT
        assert result.error_detail is not None
        assert "ttfb" in result.error_detail
        assert result.stages is not None
        assert "connect_ms" in result.stages
        assert "ttfb_ms" not in result.stages

    async def test_closed_port_is_tcp_refused(self):
        # Bind then release a port so nothing listens on it.
        server, port = await start_plain_server(respond_204())
        server.close()
        await server.wait_closed()

        target = make_target(f"http://127.0.0.1:{port}/")
        result = await https_probe.probe(target)

        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "tcp_refused"

    async def test_invalid_url_is_exec_error(self):
        target = make_target("not-a-url")
        result = await https_probe.probe(target)
        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "exec"


class TestHttpsProbeTls:
    async def test_self_signed_cert_rejected_by_default_context(self):
        server, port = await start_tls_server(respond_204())
        try:
            target = make_target(f"https://localhost:{port}/")
            result = await https_probe.probe(target)
        finally:
            server.close()
            await server.wait_closed()

        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "tls"
        assert result.stages is not None
        assert "connect_ms" in result.stages
        assert "tls_ms" not in result.stages

    async def test_ok_when_context_trusts_the_cert(self, monkeypatch):
        server, port = await start_tls_server(respond_204())

        def _trusting_context() -> ssl.SSLContext:
            return ssl.create_default_context(cafile=str(CERT_PATH))

        monkeypatch.setattr(https_probe, "_ssl_context", _trusting_context)
        try:
            target = make_target(f"https://localhost:{port}/")
            result = await https_probe.probe(target)
        finally:
            server.close()
            await server.wait_closed()

        assert result.outcome == Outcome.OK
        assert result.error_kind is None
        assert result.stages is not None
        assert "tls_ms" in result.stages
        assert "ttfb_ms" in result.stages
