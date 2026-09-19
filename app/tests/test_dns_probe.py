"""Tests for speedtest_app.dns_probe: DNS resolution probe (spec sec 4.2/4.3).

No real network or real dnspython resolver is touched: every test monkeypatches
``dns_probe.Resolver`` (the module-level name used by ``_make_resolver``) with a
fake resolver class.
"""
from __future__ import annotations

import asyncio

import dns.rdatatype
import dns.resolver
import pytest

from speedtest_app import dns_probe
from speedtest_app.probe_types import Outcome, ProbeTarget, Protocol


def make_target(**overrides) -> ProbeTarget:
    defaults = dict(
        id=1,
        name="dns-system",
        kind="dns",
        protocol=Protocol.DNS,
        host="example.com",
        port=None,
        interval_seconds=30.0,
        timeout_ms=2000,
        enabled=True,
        family_pref="auto",
        extra={"qname": "example.com", "resolver": "system"},
    )
    defaults.update(overrides)
    return ProbeTarget(**defaults)


class FakeAnswer:
    """Mimics enough of dns.resolver.Answer for our code: indexing + str()."""

    def __init__(self, addresses: list[str]) -> None:
        self._addresses = addresses

    def __getitem__(self, index: int) -> str:
        return self._addresses[index]


def make_resolver_class(*, result=None, exc: Exception | None = None, hang: bool = False, calls: list | None = None):
    class FakeResolver:
        def __init__(self, *args, configure: bool = True, **kwargs) -> None:
            self.nameservers: list[str] = []
            self._configure = configure

        async def resolve(self, qname, rdtype, lifetime=None):
            if calls is not None:
                calls.append((qname, rdtype, lifetime))
            if hang:
                await asyncio.sleep(10)
            if exc is not None:
                raise exc
            return result

    return FakeResolver


class TestDnsProbeOk:
    async def test_ok_returns_resolved_ip(self, monkeypatch):
        monkeypatch.setattr(
            dns_probe, "Resolver", make_resolver_class(result=FakeAnswer(["93.184.216.34"]))
        )
        target = make_target()

        result = await dns_probe.probe(target)

        assert result.outcome == Outcome.OK
        assert result.protocol == Protocol.DNS
        assert result.resolved_ip == "93.184.216.34"
        assert result.ip_family == 4
        assert result.error_kind is None
        assert result.rtt_ms is not None and result.rtt_ms >= 0
        assert result.stages == {"query_ms": result.rtt_ms}
        assert result.timeout_ms == 2000
        assert result.duration_ms >= 0

    async def test_family_pref_ipv6_uses_aaaa(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            dns_probe,
            "Resolver",
            make_resolver_class(result=FakeAnswer(["2606:2800:220:1:248:1893:25c8:1946"]), calls=calls),
        )
        target = make_target(family_pref="ipv6")

        result = await dns_probe.probe(target)

        assert result.outcome == Outcome.OK
        assert result.ip_family == 6
        assert calls[0][1] == dns.rdatatype.AAAA

    async def test_family_pref_default_uses_a(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            dns_probe, "Resolver", make_resolver_class(result=FakeAnswer(["1.2.3.4"]), calls=calls)
        )
        target = make_target(family_pref="auto")

        await dns_probe.probe(target)

        assert calls[0][1] == dns.rdatatype.A


class TestDnsProbeErrors:
    async def test_nxdomain(self, monkeypatch):
        monkeypatch.setattr(
            dns_probe, "Resolver", make_resolver_class(exc=dns.resolver.NXDOMAIN("nxdomain"))
        )
        result = await dns_probe.probe(make_target())
        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "dns_nxdomain"

    async def test_no_answer(self, monkeypatch):
        monkeypatch.setattr(
            dns_probe, "Resolver", make_resolver_class(exc=dns.resolver.NoAnswer("no answer"))
        )
        result = await dns_probe.probe(make_target())
        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "dns_no_answer"

    async def test_lifetime_timeout_is_timeout_outcome(self, monkeypatch):
        monkeypatch.setattr(
            dns_probe, "Resolver", make_resolver_class(exc=dns.resolver.LifetimeTimeout("timed out"))
        )
        result = await dns_probe.probe(make_target())
        assert result.outcome == Outcome.TIMEOUT
        assert result.error_kind is None

    async def test_no_nameservers_servfail(self, monkeypatch):
        monkeypatch.setattr(
            dns_probe,
            "Resolver",
            make_resolver_class(
                exc=dns.resolver.NoNameservers("All nameservers failed to answer the query: SERVFAIL")
            ),
        )
        result = await dns_probe.probe(make_target())
        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "dns_servfail"

    async def test_no_nameservers_refused(self, monkeypatch):
        monkeypatch.setattr(
            dns_probe,
            "Resolver",
            make_resolver_class(
                exc=dns.resolver.NoNameservers("All nameservers failed to answer the query: REFUSED")
            ),
        )
        result = await dns_probe.probe(make_target())
        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "dns_refused"

    async def test_no_nameservers_other_is_dns_error(self, monkeypatch):
        monkeypatch.setattr(
            dns_probe,
            "Resolver",
            make_resolver_class(
                exc=dns.resolver.NoNameservers("All nameservers failed to answer the query: mystery")
            ),
        )
        result = await dns_probe.probe(make_target())
        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "dns_error"

    async def test_no_resolver_configuration(self, monkeypatch):
        class RaisingConstructorResolver:
            def __init__(self, *args, configure: bool = True, **kwargs) -> None:
                if configure:
                    raise dns.resolver.NoResolverConfiguration("no resolv.conf")
                self.nameservers = []

            async def resolve(self, *args, **kwargs):  # pragma: no cover - never reached
                raise AssertionError("resolve should not be called")

        monkeypatch.setattr(dns_probe, "Resolver", RaisingConstructorResolver)
        result = await dns_probe.probe(make_target(extra={"qname": "example.com", "resolver": "system"}))
        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "resolver_unavailable"

    async def test_explicit_resolver_does_not_hit_configure_path(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            dns_probe, "Resolver", make_resolver_class(result=FakeAnswer(["1.1.1.1"]), calls=calls)
        )
        target = make_target(extra={"qname": "example.com", "resolver": "9.9.9.9"})
        result = await dns_probe.probe(target)
        assert result.outcome == Outcome.OK

    async def test_hard_guard_exec_timeout_on_hanging_resolve(self, monkeypatch):
        monkeypatch.setattr(dns_probe, "Resolver", make_resolver_class(hang=True))
        target = make_target(timeout_ms=10)

        result = await dns_probe.probe(target)

        assert result.outcome == Outcome.ERROR
        assert result.error_kind == "exec_timeout"

    async def test_timeout_ms_override(self, monkeypatch):
        monkeypatch.setattr(
            dns_probe, "Resolver", make_resolver_class(result=FakeAnswer(["1.2.3.4"]))
        )
        target = make_target(timeout_ms=2000)
        result = await dns_probe.probe(target, timeout_ms=500)
        assert result.timeout_ms == 500

    async def test_default_qname_falls_back_to_host(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            dns_probe, "Resolver", make_resolver_class(result=FakeAnswer(["1.2.3.4"]), calls=calls)
        )
        target = make_target(host="fallback.example", extra={})
        await dns_probe.probe(target)
        assert calls[0][0] == "fallback.example"

    async def test_error_detail_truncated(self, monkeypatch):
        long_msg = "x" * 500
        monkeypatch.setattr(
            dns_probe, "Resolver", make_resolver_class(exc=dns.resolver.NXDOMAIN(long_msg))
        )
        result = await dns_probe.probe(make_target())
        assert result.error_detail is not None
        assert len(result.error_detail) <= 200
