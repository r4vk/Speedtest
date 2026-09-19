"""One TCP connect attempt with stage timings (spec §4.2).

Resolution is part of the attempt: ``stages`` carries ``dns_ms`` and
``connect_ms``, ``rtt_ms`` is the connect time alone. The connection is closed as
soon as the SYN-ACK arrived - nothing is ever sent to the peer. A probe never
raises: every failure becomes ``outcome=error`` with an ``error_kind`` from §4.3.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import suppress
from datetime import datetime
from time import perf_counter

from .probe_types import Outcome, ProbeResult, ProbeTarget
from .time_utils import to_iso_z, utc_now

logger = logging.getLogger(__name__)

#: Extra wall time granted on top of ``timeout_ms`` before the attempt is killed.
GUARD_EXTRA_SECONDS = 0.25
#: How long the writer may take to shut down before we stop caring.
CLOSE_TIMEOUT_SECONDS = 0.05
_MAX_ERROR_DETAIL = 200


async def probe(target: ProbeTarget, *, timeout_ms: int | None = None) -> ProbeResult:
    """Open a TCP connection to ``target.host:target.port`` and time it."""
    effective_timeout = int(target.timeout_ms if timeout_ms is None else timeout_ms)
    started_wall = utc_now()
    started = perf_counter()
    guard = effective_timeout / 1000.0 + GUARD_EXTRA_SECONDS
    try:
        return await asyncio.wait_for(
            _attempt(target, effective_timeout, started_wall, started), guard
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("TCP probe of %s exceeded the hard guard", target.host)
        return _result(
            target,
            started_wall,
            started,
            effective_timeout,
            Outcome.ERROR,
            error_kind="exec_timeout",
            error_detail="attempt exceeded the hard guard",
        )
    except Exception as exc:  # pragma: no cover - _attempt handles its own errors
        return _result(
            target,
            started_wall,
            started,
            effective_timeout,
            Outcome.ERROR,
            error_kind="exec",
            error_detail=_detail(exc),
        )


async def _attempt(
    target: ProbeTarget, timeout_ms: int, started_wall: datetime, started: float
) -> ProbeResult:
    if target.port is None:
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.ERROR,
            error_kind="exec",
            error_detail="target has no port",
        )

    budget = timeout_ms / 1000.0
    dns_started = perf_counter()
    try:
        family, ip = await _resolve(target)
    except socket.gaierror as exc:
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.ERROR,
            stages={"dns_ms": _since(dns_started)},
            error_kind="dns",
            error_detail=_detail(exc),
        )
    except Exception as exc:
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.ERROR,
            stages={"dns_ms": _since(dns_started)},
            error_kind="exec",
            error_detail=_detail(exc),
        )

    dns_ms = _since(dns_started)
    ip_family = 6 if family == socket.AF_INET6 else 4
    remaining = max(0.0, budget - dns_ms / 1000.0)
    connect_started = perf_counter()
    writer: asyncio.StreamWriter | None = None
    try:
        _reader, writer = await asyncio.wait_for(
            # The family is already known, so the connect does not look the host
            # up a second time across both families.
            asyncio.open_connection(host=ip, port=target.port, family=family),
            remaining,
        )
    except (asyncio.TimeoutError, TimeoutError):
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.TIMEOUT,
            resolved_ip=ip,
            ip_family=ip_family,
            stages={"dns_ms": dns_ms, "connect_ms": _since(connect_started)},
        )
    except ConnectionRefusedError as exc:
        return _connect_error(
            target, started_wall, started, timeout_ms, ip, ip_family, dns_ms,
            connect_started, "tcp_refused", exc,
        )
    except ConnectionResetError as exc:
        return _connect_error(
            target, started_wall, started, timeout_ms, ip, ip_family, dns_ms,
            connect_started, "tcp_reset", exc,
        )
    except OSError as exc:
        return _connect_error(
            target, started_wall, started, timeout_ms, ip, ip_family, dns_ms,
            connect_started, "tcp_error", exc,
        )
    else:
        connect_ms = _since(connect_started)
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.OK,
            rtt_ms=connect_ms,
            resolved_ip=ip,
            ip_family=ip_family,
            stages={"dns_ms": dns_ms, "connect_ms": connect_ms},
        )
    finally:
        if writer is not None:
            await _close(writer)


async def _resolve(target: ProbeTarget) -> tuple[int, str]:
    family = _family_of(target.family_pref)
    infos = await asyncio.to_thread(
        socket.getaddrinfo, target.host, target.port, family, socket.SOCK_STREAM
    )
    if not infos:
        raise socket.gaierror(socket.EAI_NONAME, "no address returned")
    af, _type, _proto, _canon, sockaddr = infos[0]
    return af, sockaddr[0]


async def _close(writer: asyncio.StreamWriter) -> None:
    """Close immediately; a peer that lingers must not extend the attempt."""
    with suppress(Exception):
        writer.close()
    with suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), CLOSE_TIMEOUT_SECONDS)


def _family_of(family_pref: str) -> int:
    if family_pref == "ipv4":
        return socket.AF_INET
    if family_pref == "ipv6":
        return socket.AF_INET6
    return socket.AF_UNSPEC


def _since(start: float) -> float:
    return max(0.0, (perf_counter() - start) * 1000.0)


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_DETAIL]


def _connect_error(
    target: ProbeTarget,
    started_wall: datetime,
    started: float,
    timeout_ms: int,
    ip: str,
    ip_family: int,
    dns_ms: float,
    connect_started: float,
    error_kind: str,
    exc: BaseException,
) -> ProbeResult:
    return _result(
        target,
        started_wall,
        started,
        timeout_ms,
        Outcome.ERROR,
        resolved_ip=ip,
        ip_family=ip_family,
        stages={"dns_ms": dns_ms, "connect_ms": _since(connect_started)},
        error_kind=error_kind,
        error_detail=_detail(exc),
    )


def _result(
    target: ProbeTarget,
    started_wall: datetime,
    started: float,
    timeout_ms: int,
    outcome: Outcome,
    *,
    rtt_ms: float | None = None,
    resolved_ip: str | None = None,
    ip_family: int | None = None,
    stages: dict[str, float] | None = None,
    error_kind: str | None = None,
    error_detail: str | None = None,
) -> ProbeResult:
    return ProbeResult(
        target_id=target.id,
        protocol=target.protocol,
        started_at=to_iso_z(started_wall),
        duration_ms=max(0.0, (perf_counter() - started) * 1000.0),
        outcome=outcome,
        timeout_ms=timeout_ms,
        rtt_ms=rtt_ms,
        resolved_ip=resolved_ip,
        ip_family=ip_family,
        stages=stages,
        error_kind=error_kind,
        error_detail=error_detail[:_MAX_ERROR_DETAIL] if error_detail else None,
    )
