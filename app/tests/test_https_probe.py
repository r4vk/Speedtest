"""Tests for speedtest_app.https_probe: HTTP(S) probe with manual stage timing
(spec sec 4.2/4.3).

Every test drives a local ``asyncio.start_server`` on 127.0.0.1 - no real
network or real internet host is touched. The TLS tests use the self-signed
fixture pair committed under ``tests/fixtures/tls/``.
"""
from __future__ import annotations

import asyncio
import gc
import ssl
import time
import warnings
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


class TestRunStageDoesNotLeakCoroutines:
    """Regression tests for the eager-coroutine-construction bug (review round 1).

    ``_run_stage`` used to take an already-built awaitable, so an expired
    deadline raised ``_StageTimeout`` without ever awaiting (or closing) it,
    producing a "coroutine was never awaited" RuntimeWarning on GC. It now
    takes a zero-arg factory invoked only after the budget check passes.
    """

    async def test_expired_deadline_never_calls_the_factory(self):
        stages: dict[str, float] = {}
        calls: list[object] = []

        def factory():
            async def _inner():
                return "unused"  # pragma: no cover - never reached

            coro = _inner()
            calls.append(coro)
            return coro

        expired_deadline = time.perf_counter() - 1.0

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(https_probe._StageTimeout) as exc_info:
                await https_probe._run_stage("ttfb", expired_deadline, factory, stages)
            del exc_info  # keep the traceback frame from pinning anything
            gc.collect()

        assert calls == []  # factory must not run once the budget is already spent
        assert stages == {}
        runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert not runtime_warnings, [str(w.message) for w in runtime_warnings]

    async def test_expired_deadline_raises_stage_timeout_naming_the_stage(self):
        stages: dict[str, float] = {}
        expired_deadline = time.perf_counter() - 1.0

        with pytest.raises(https_probe._StageTimeout) as exc_info:
            await https_probe._run_stage(
                "connect", expired_deadline, lambda: asyncio.sleep(0), stages
            )

        assert exc_info.value.stage == "connect"

    async def test_probe_with_zero_budget_times_out_without_runtime_warning(self):
        """End-to-end repro: timeout_ms=0 expires the deadline before the very
        first stage (dns) runs, which used to leak an un-awaited coroutine.
        """
        server, port = await start_plain_server(respond_204())
        try:
            target = make_target(f"http://127.0.0.1:{port}/", timeout_ms=0)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = await https_probe.probe(target)
                gc.collect()
        finally:
            server.close()
            await server.wait_closed()

        assert result.outcome == Outcome.TIMEOUT
        assert result.error_detail == "dns exceeded budget"
        runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert not runtime_warnings, [str(w.message) for w in runtime_warnings]


class TestBuildRequest:
    def test_plain_hostname_host_header(self):
        req = https_probe._build_request("example.com", "/status")
        lines = req.split(b"\r\n")
        assert lines[0] == b"GET /status HTTP/1.1"
        assert b"Host: example.com" in lines

    def test_ipv6_literal_hostname_is_bracketed(self):
        req = https_probe._build_request("::1", "/")
        assert b"Host: [::1]" in req.split(b"\r\n")


class _FakeReader:
    """Feeds fixed-size chunks with no CRLFCRLF, so the header loop can only
    stop via the size bound - used to pin the exact 64 KiB cutoff."""

    def __init__(self, chunk: bytes) -> None:
        self._chunk = chunk
        self.calls = 0

    async def read(self, n: int) -> bytes:
        self.calls += 1
        return self._chunk[:n]


class _FakeWriter:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        return None


class TestHeaderReadBound:
    async def test_stops_at_exact_64kib_without_one_extra_chunk(self):
        reader = _FakeReader(b"x" * 4096)
        writer = _FakeWriter()

        with pytest.raises(ValueError):
            await https_probe._fetch_status(reader, writer, "host", "/")

        # 64 KiB of 4 KiB chunks is exactly 16 reads; `<=` instead of `<`
        # would let a 17th read push the buffer past the 64 KiB cap.
        assert reader.calls == https_probe._MAX_HEADER_BYTES // 4096
