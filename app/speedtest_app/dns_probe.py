"""DNS probe (spec sec 4.2, 4.3).

Resolves a configured query name through dnspython's async resolver and turns
the result into a :class:`~speedtest_app.probe_types.ProbeResult`, using the
closed ``error_kind`` vocabulary so a service-level DNS problem (NXDOMAIN,
SERVFAIL, ...) is never confused with a network-level failure.

The probe never raises: any exception it does not recognize becomes
``outcome=error, error_kind='exec'``, and a hard wall-clock guard bounds the
whole attempt to ``timeout_ms + 250 ms`` regardless of what the resolver does.
"""
from __future__ import annotations

import asyncio
import logging
import time

import dns.exception
import dns.rdatatype
import dns.resolver
from dns.asyncresolver import Resolver

from .probe_types import Outcome, ProbeResult, ProbeTarget, Protocol
from .time_utils import to_iso_z, utc_now

logger = logging.getLogger(__name__)

#: Extra wall-clock slack given to the hard guard beyond the configured budget.
_HARD_GUARD_SLACK_S = 0.25
_ERROR_DETAIL_MAX = 200
_DEFAULT_QNAME = "example.com"


def _make_resolver(resolver: str) -> Resolver:
    """Build an async resolver for ``resolver`` ("system" or a nameserver IP).

    A thin factory so tests can monkeypatch either this function or the
    module-level ``Resolver`` name instead of dnspython internals.
    """
    if resolver == "system":
        # Reads /etc/resolv.conf; raises dns.resolver.NoResolverConfiguration
        # when it cannot find a usable configuration.
        return Resolver()
    r = Resolver(configure=False)
    r.nameservers = [resolver]
    return r


def _truncate(detail: str) -> str:
    return detail[:_ERROR_DETAIL_MAX]


def _classify_no_nameservers(exc: dns.resolver.NoNameservers) -> str:
    text = str(exc).upper()
    if "SERVFAIL" in text:
        return "dns_servfail"
    if "REFUSED" in text:
        return "dns_refused"
    return "dns_error"


async def probe(target: ProbeTarget, *, timeout_ms: int | None = None) -> ProbeResult:
    """Resolve ``target``'s configured query name and report the outcome."""
    effective_timeout_ms = timeout_ms if timeout_ms is not None else target.timeout_ms
    started_at = to_iso_z(utc_now())
    start = time.perf_counter()

    def _finish(
        outcome: Outcome,
        *,
        rtt_ms: float | None = None,
        resolved_ip: str | None = None,
        ip_family: int | None = None,
        error_kind: str | None = None,
        error_detail: str | None = None,
        stages: dict[str, float] | None = None,
    ) -> ProbeResult:
        return ProbeResult(
            target_id=target.id,
            protocol=Protocol.DNS,
            started_at=started_at,
            duration_ms=(time.perf_counter() - start) * 1000,
            outcome=outcome,
            timeout_ms=effective_timeout_ms,
            rtt_ms=rtt_ms,
            resolved_ip=resolved_ip,
            ip_family=ip_family,
            error_kind=error_kind,
            error_detail=_truncate(error_detail) if error_detail else None,
            stages=stages,
        )

    async def _attempt() -> ProbeResult:
        qname = target.extra.get("qname") or target.host or _DEFAULT_QNAME
        resolver_spec = target.extra.get("resolver", "system")
        rdtype = dns.rdatatype.AAAA if target.family_pref == "ipv6" else dns.rdatatype.A
        ip_family = 6 if rdtype == dns.rdatatype.AAAA else 4

        query_start = time.perf_counter()
        try:
            resolver = _make_resolver(resolver_spec)
            answer = await resolver.resolve(qname, rdtype, lifetime=effective_timeout_ms / 1000)
        except dns.resolver.NXDOMAIN as exc:
            return _finish(Outcome.ERROR, error_kind="dns_nxdomain", error_detail=str(exc))
        except dns.resolver.NoAnswer as exc:
            return _finish(Outcome.ERROR, error_kind="dns_no_answer", error_detail=str(exc))
        except dns.exception.Timeout as exc:
            # dns.resolver.LifetimeTimeout (aliased as dns.resolver.Timeout).
            return _finish(Outcome.TIMEOUT, error_detail=str(exc))
        except dns.resolver.NoNameservers as exc:
            return _finish(Outcome.ERROR, error_kind=_classify_no_nameservers(exc), error_detail=str(exc))
        except dns.resolver.NoResolverConfiguration as exc:
            return _finish(Outcome.ERROR, error_kind="resolver_unavailable", error_detail=str(exc))
        except dns.exception.DNSException as exc:
            return _finish(Outcome.ERROR, error_kind="dns_error", error_detail=str(exc))
        except Exception as exc:  # never raise out of a probe
            logger.exception("dns probe: unexpected error for target %s", target.name)
            return _finish(Outcome.ERROR, error_kind="exec", error_detail=str(exc))

        rtt_ms = (time.perf_counter() - query_start) * 1000
        resolved_ip = str(answer[0])
        return _finish(
            Outcome.OK,
            rtt_ms=rtt_ms,
            resolved_ip=resolved_ip,
            ip_family=ip_family,
            stages={"query_ms": rtt_ms},
        )

    try:
        return await asyncio.wait_for(
            _attempt(), timeout=effective_timeout_ms / 1000 + _HARD_GUARD_SLACK_S
        )
    except asyncio.TimeoutError:
        logger.debug("dns probe: hard guard exceeded for target %s", target.name)
        return _finish(Outcome.ERROR, error_kind="exec_timeout", error_detail="hard guard exceeded")
    except Exception as exc:  # never raise out of a probe
        logger.exception("dns probe: crashed for target %s", target.name)
        return _finish(Outcome.ERROR, error_kind="exec", error_detail=str(exc))
