"""HTTPS/HTTP probe with manually-timed stages (spec sec 4.2, 4.3).

Unlike a plain ``httpx`` request, this probe measures each stage of the
connection by hand - DNS resolution, TCP connect, TLS handshake and time to
first byte - so a TCP-level success is never reported as an HTTPS success: a
bad certificate is a real ``tls`` failure even though the socket connected
fine. Each stage is charged against the *remaining* slice of ``timeout_ms``,
and the probe never raises: every failure becomes ``outcome=error`` (or
``timeout``) with one of the closed ``error_kind`` values, and the socket is
always closed.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from typing import Awaitable, TypeVar
from urllib.parse import urlsplit

from .probe_types import Outcome, ProbeResult, ProbeTarget, Protocol
from .time_utils import to_iso_z, utc_now

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

#: Extra wall-clock slack given to the hard guard beyond the configured budget.
_HARD_GUARD_SLACK_S = 0.25
_ERROR_DETAIL_MAX = 200
_MAX_HEADER_BYTES = 64 * 1024
_USER_AGENT = "r4vk-speedtest"

_FAMILY_MAP: dict[str, socket.AddressFamily] = {
    "ipv4": socket.AF_INET,
    "ipv6": socket.AF_INET6,
}


class _StageTimeout(Exception):
    """Raised internally when a single stage exceeds its remaining budget."""

    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


def _ssl_context() -> ssl.SSLContext:
    """Default TLS context factory - verifies certificates.

    A module-level seam so tests can swap in a context that trusts a specific
    (e.g. self-signed) certificate.
    """
    return ssl.create_default_context()


def _truncate(detail: str) -> str:
    return detail[:_ERROR_DETAIL_MAX]


async def _run_stage(
    stage: str, deadline: float, awaitable: Awaitable[_T], stages: dict[str, float]
) -> _T:
    """Await ``awaitable`` within what remains of the overall budget.

    Records ``stages[f"{stage}_ms"]`` on success; raises ``_StageTimeout``
    when the remaining budget is already spent or is exceeded while waiting.
    """
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise _StageTimeout(stage)
    stage_start = time.perf_counter()
    try:
        result = await asyncio.wait_for(awaitable, timeout=remaining)
    except asyncio.TimeoutError as exc:
        raise _StageTimeout(stage) from exc
    stages[f"{stage}_ms"] = (time.perf_counter() - stage_start) * 1000
    return result


async def _resolve_host(
    loop: asyncio.AbstractEventLoop, hostname: str, port: int, family: socket.AddressFamily
) -> tuple[str, int]:
    infos = await loop.getaddrinfo(hostname, port, family=family, type=socket.SOCK_STREAM)
    if not infos:
        raise socket.gaierror(f"no address found for {hostname}")
    addr_family, _, _, _, sockaddr = infos[0]
    ip_family = 6 if addr_family == socket.AF_INET6 else 4
    return sockaddr[0], ip_family


def _parse_status_line(data: bytes) -> int:
    line = data.split(b"\r\n", 1)[0]
    parts = line.split(b" ", 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        raise ValueError(f"malformed status line: {line[:80]!r}")
    try:
        return int(parts[1])
    except ValueError as exc:
        raise ValueError(f"malformed status code: {parts[1][:20]!r}") from exc


async def _fetch_status(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, hostname: str, path: str) -> int:
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {hostname}\r\n"
        f"User-Agent: {_USER_AGENT}\r\n"
        f"Accept: */*\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")
    writer.write(request)
    await writer.drain()

    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) <= _MAX_HEADER_BYTES:
        chunk = await reader.read(4096)
        if not chunk:
            break
        buf += chunk

    return _parse_status_line(buf)


def _split_url(url: str) -> tuple[str, str, int | None, str] | None:
    """Return (scheme, hostname, url_port, path_with_query), or None if invalid."""
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return None
    if not hostname:
        return None
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return parsed.scheme, hostname, parsed.port, path


async def probe(target: ProbeTarget, *, timeout_ms: int | None = None) -> ProbeResult:
    """Fetch ``target.host`` (a URL) and report per-stage timings and outcome."""
    effective_timeout_ms = timeout_ms if timeout_ms is not None else target.timeout_ms
    started_at = to_iso_z(utc_now())
    overall_start = time.perf_counter()

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
            protocol=Protocol.HTTPS,
            started_at=started_at,
            duration_ms=(time.perf_counter() - overall_start) * 1000,
            outcome=outcome,
            timeout_ms=effective_timeout_ms,
            rtt_ms=rtt_ms,
            resolved_ip=resolved_ip,
            ip_family=ip_family,
            error_kind=error_kind,
            error_detail=_truncate(error_detail) if error_detail else None,
            stages=stages or None,
        )

    async def _attempt() -> ProbeResult:
        parts = _split_url(target.host)
        if parts is None:
            return _finish(Outcome.ERROR, error_kind="exec", error_detail="invalid url")
        scheme, hostname, url_port, path = parts
        port = target.port or url_port or (443 if scheme == "https" else 80)
        family = _FAMILY_MAP.get(target.family_pref, socket.AF_UNSPEC)

        budget_s = effective_timeout_ms / 1000
        deadline = time.perf_counter() + budget_s
        stages: dict[str, float] = {}
        resolved_ip: str | None = None
        ip_family: int | None = None
        writer: asyncio.StreamWriter | None = None
        loop = asyncio.get_running_loop()

        try:
            resolved_ip, ip_family = await _run_stage(
                "dns", deadline, _resolve_host(loop, hostname, port, family), stages
            )
            reader, writer = await _run_stage(
                "connect", deadline, asyncio.open_connection(resolved_ip, port), stages
            )
            if scheme == "https":
                ctx = _ssl_context()
                await _run_stage(
                    "tls", deadline, writer.start_tls(ctx, server_hostname=hostname), stages
                )
            status = await _run_stage(
                "ttfb", deadline, _fetch_status(reader, writer, hostname, path), stages
            )
        except _StageTimeout as exc:
            return _finish(
                Outcome.TIMEOUT,
                stages=stages,
                resolved_ip=resolved_ip,
                ip_family=ip_family,
                error_detail=f"{exc.stage} exceeded budget",
            )
        except socket.gaierror as exc:
            return _finish(Outcome.ERROR, error_kind="dns", error_detail=str(exc))
        except (ssl.CertificateError, ssl.SSLError) as exc:
            return _finish(
                Outcome.ERROR,
                error_kind="tls",
                stages=stages,
                resolved_ip=resolved_ip,
                ip_family=ip_family,
                error_detail=str(exc),
            )
        except ConnectionRefusedError as exc:
            return _finish(
                Outcome.ERROR,
                error_kind="tcp_refused",
                stages=stages,
                resolved_ip=resolved_ip,
                ip_family=ip_family,
                error_detail=str(exc),
            )
        except ConnectionResetError as exc:
            return _finish(
                Outcome.ERROR,
                error_kind="tcp_reset",
                stages=stages,
                resolved_ip=resolved_ip,
                ip_family=ip_family,
                error_detail=str(exc),
            )
        except ValueError as exc:
            return _finish(
                Outcome.ERROR,
                error_kind="http_protocol",
                stages=stages,
                resolved_ip=resolved_ip,
                ip_family=ip_family,
                error_detail=str(exc),
            )
        except OSError as exc:
            return _finish(
                Outcome.ERROR,
                error_kind="tcp_error",
                stages=stages,
                resolved_ip=resolved_ip,
                ip_family=ip_family,
                error_detail=str(exc),
            )
        except Exception as exc:  # never raise out of a probe
            logger.exception("https probe: unexpected error for target %s", target.name)
            return _finish(
                Outcome.ERROR,
                error_kind="exec",
                stages=stages,
                resolved_ip=resolved_ip,
                ip_family=ip_family,
                error_detail=str(exc),
            )
        else:
            if status >= 400:
                return _finish(
                    Outcome.ERROR,
                    error_kind="http_status",
                    stages=stages,
                    resolved_ip=resolved_ip,
                    ip_family=ip_family,
                    error_detail=str(status),
                )
            return _finish(
                Outcome.OK,
                stages=stages,
                resolved_ip=resolved_ip,
                ip_family=ip_family,
                rtt_ms=(time.perf_counter() - overall_start) * 1000,
            )
        finally:
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    logger.debug("https probe: error closing connection", exc_info=True)

    try:
        return await asyncio.wait_for(
            _attempt(), timeout=effective_timeout_ms / 1000 + _HARD_GUARD_SLACK_S
        )
    except asyncio.TimeoutError:
        logger.debug("https probe: hard guard exceeded for target %s", target.name)
        return _finish(Outcome.ERROR, error_kind="exec_timeout", error_detail="hard guard exceeded")
    except Exception as exc:  # never raise out of a probe
        logger.exception("https probe: crashed for target %s", target.name)
        return _finish(Outcome.ERROR, error_kind="exec", error_detail=str(exc))
