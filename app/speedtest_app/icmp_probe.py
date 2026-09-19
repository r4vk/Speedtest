"""One ICMP echo attempt, unprivileged first (spec §4.2).

The method ladder is detected once and logged: an unprivileged ICMP datagram
socket, a raw socket, the ``ping`` binary, or nothing at all. On a datagram
socket the kernel rewrites the identifier, so replies are matched on the
sequence number plus a random payload token, never on the identifier.

The whole attempt runs on the event loop (``loop.sock_recv``); no thread waits
for a reply and the socket is always closed. A probe never raises: every failure
becomes ``outcome=error`` with an ``error_kind`` from spec §4.3.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import shutil
import socket
import struct
import sys
from contextlib import suppress
from datetime import datetime
from math import ceil
from time import perf_counter
from typing import Any

from .probe_types import Outcome, ProbeResult, ProbeTarget
from .time_utils import to_iso_z, utc_now

logger = logging.getLogger(__name__)

ICMP_ECHO_REPLY = 0
ICMP_DEST_UNREACH = 3
ICMP_ECHO_REQUEST = 8
ICMP_TIME_EXCEEDED = 11
ICMPV6_DEST_UNREACH = 1
ICMPV6_TIME_EXCEEDED = 3
ICMPV6_ECHO_REQUEST = 128
ICMPV6_ECHO_REPLY = 129

#: Extra wall time granted on top of ``timeout_ms`` before the attempt is killed.
GUARD_EXTRA_SECONDS = 0.25
#: Size of the random token echoed back by the peer.
PAYLOAD_SIZE = 16
RECV_BUFSIZE = 2048
_MAX_ERROR_DETAIL = 200
_MIN_IPV4_DATAGRAM = 28  # 20 B IPv4 header + 8 B ICMP header
_IPV6_HEADER_SIZE = 40
_PING_TIME_RE = re.compile(r"time[=<]\s*([0-9]+(?:\.[0-9]+)?)\s*ms", re.IGNORECASE)
#: Pending socket errors that mean "the peer answered with an ICMP error".
UNREACHABLE_ERRNOS = frozenset(
    {
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.EHOSTDOWN,
        errno.ENETDOWN,
        errno.ECONNREFUSED,
    }
)

_icmp_method: str | None = None
_sequences: dict[int, int] = {}


# ---------------------------------------------------------------------------
# method detection
# ---------------------------------------------------------------------------


def _try_socket(family: int, sock_type: int, proto: int) -> bool:
    """True when such a socket can be opened by this process right now."""
    try:
        sock = socket.socket(family, sock_type, proto)
    except Exception:
        return False
    sock.close()
    return True


def _detect() -> str:
    try:
        if _try_socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP):
            return "dgram"
        if _try_socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP):
            return "raw"
        if shutil.which("ping"):
            return "ping"
    except Exception:  # pragma: no cover - detection must never raise
        logger.exception("ICMP method detection failed")
    return "unavailable"


def detect_icmp_method() -> str:
    """``dgram`` | ``raw`` | ``ping`` | ``unavailable``, detected once and cached."""
    global _icmp_method
    if _icmp_method is None:
        _icmp_method = _detect()
        logger.info("ICMP method: %s", _icmp_method)
    return _icmp_method


def reset_icmp_method_cache() -> None:
    """Forget the detected method (tests, and a config reload after a restart)."""
    global _icmp_method
    _icmp_method = None


# ---------------------------------------------------------------------------
# packets (pure)
# ---------------------------------------------------------------------------


def icmp_checksum(data: bytes) -> int:
    """RFC 1071 one's complement sum over 16-bit big-endian words."""
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for offset in range(0, len(data), 2):
        total += (data[offset] << 8) | data[offset + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def build_echo_request(
    ident: int, seq: int, payload: bytes, family: int = socket.AF_INET
) -> bytes:
    """An ICMP(v6) echo request; the ICMPv6 checksum is left to the kernel."""
    is_v6 = family == socket.AF_INET6
    echo_type = ICMPV6_ECHO_REQUEST if is_v6 else ICMP_ECHO_REQUEST
    header = struct.pack("!BBHHH", echo_type, 0, 0, ident & 0xFFFF, seq & 0xFFFF)
    if is_v6:
        return header + payload
    checksum = icmp_checksum(header + payload)
    return struct.pack("!BBHHH", echo_type, 0, checksum, ident & 0xFFFF, seq & 0xFFFF) + payload


def _strip_ipv4_header(data: bytes) -> bytes:
    """Drop the IPv4 header when the socket delivered one.

    Raw sockets always deliver it; datagram ICMP sockets strip it on Linux but
    not on macOS, so both shapes have to be accepted.
    """
    if len(data) >= _MIN_IPV4_DATAGRAM and (data[0] >> 4) == 4:
        ihl = (data[0] & 0x0F) * 4
        if 20 <= ihl <= len(data) - 8:
            return data[ihl:]
    return data


def parse_echo_reply(data: bytes, family: int) -> tuple[int, int, bytes] | None:
    """``(ident, seq, payload)`` of an echo reply, or None when it is not one."""
    is_v6 = family == socket.AF_INET6
    message = data if is_v6 else _strip_ipv4_header(data)
    if len(message) < 8:
        return None
    if message[0] != (ICMPV6_ECHO_REPLY if is_v6 else ICMP_ECHO_REPLY):
        return None
    ident, seq = struct.unpack("!HH", message[4:8])
    return ident, seq, message[8:]


def _parse_quoted_echo(data: bytes, family: int) -> tuple[int, int] | None:
    """``(icmp_type, seq)`` of our request quoted inside an ICMP error message."""
    is_v6 = family == socket.AF_INET6
    message = data if is_v6 else _strip_ipv4_header(data)
    if len(message) < 8:
        return None
    message_type = message[0]
    error_types = (
        (ICMPV6_DEST_UNREACH, ICMPV6_TIME_EXCEEDED)
        if is_v6
        else (ICMP_DEST_UNREACH, ICMP_TIME_EXCEEDED)
    )
    if message_type not in error_types:
        return None
    quoted = message[8:]  # the original datagram, possibly truncated
    if is_v6:
        if len(quoted) < _IPV6_HEADER_SIZE + 8:
            return None
        inner = quoted[_IPV6_HEADER_SIZE:]
    else:
        if len(quoted) < _MIN_IPV4_DATAGRAM or (quoted[0] >> 4) != 4:
            return None
        ihl = (quoted[0] & 0x0F) * 4
        if not 20 <= ihl <= len(quoted) - 8:
            return None
        inner = quoted[ihl:]
    if len(inner) < 8 or inner[0] != (ICMPV6_ECHO_REQUEST if is_v6 else ICMP_ECHO_REQUEST):
        return None
    _ident, seq = struct.unpack("!HH", inner[4:8])
    return message_type, seq


# ---------------------------------------------------------------------------
# I/O seams (monkeypatched by the tests)
# ---------------------------------------------------------------------------


def _open_socket(family: int, sock_type: int, proto: int) -> socket.socket:
    return socket.socket(family, sock_type, proto)


async def _sock_sendto(sock: socket.socket, data: bytes, address: Any) -> None:
    await asyncio.get_running_loop().sock_sendto(sock, data, address)


async def _sock_recv(sock: socket.socket, bufsize: int) -> bytes:
    return await asyncio.get_running_loop().sock_recv(sock, bufsize)


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------


async def probe(
    target: ProbeTarget, *, timeout_ms: int | None = None, method: str | None = None
) -> ProbeResult:
    """Send one echo request and turn the outcome into a `ProbeResult`."""
    effective_timeout = int(target.timeout_ms if timeout_ms is None else timeout_ms)
    started_wall = utc_now()
    started = perf_counter()
    guard = effective_timeout / 1000.0 + GUARD_EXTRA_SECONDS
    try:
        return await asyncio.wait_for(
            _attempt(target, effective_timeout, started_wall, started, method), guard
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("ICMP probe of %s exceeded the hard guard", target.host)
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
    target: ProbeTarget,
    timeout_ms: int,
    started_wall: datetime,
    started: float,
    method: str | None,
) -> ProbeResult:
    try:
        family, ip, sockaddr = await _resolve(target)
    except socket.gaierror as exc:
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.ERROR,
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
            error_kind="exec",
            error_detail=_detail(exc),
        )

    ip_family = 6 if family == socket.AF_INET6 else 4
    chosen = method or detect_icmp_method()
    if chosen == "unavailable":
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.ERROR,
            resolved_ip=ip,
            ip_family=ip_family,
            error_kind="permission",
            error_detail="no ICMP method available",
        )
    if chosen == "ping":
        return await _attempt_ping(
            target, timeout_ms, started_wall, started, family, ip, ip_family
        )
    return await _attempt_socket(
        target, timeout_ms, started_wall, started, chosen, family, ip, ip_family, sockaddr
    )


async def _resolve(target: ProbeTarget) -> tuple[int, str, Any]:
    family = _family_of(target.family_pref)
    infos = await asyncio.to_thread(
        socket.getaddrinfo, target.host, None, family, socket.SOCK_DGRAM
    )
    if not infos:
        raise socket.gaierror(socket.EAI_NONAME, "no address returned")
    af, _type, _proto, _canon, sockaddr = infos[0]
    return af, sockaddr[0], sockaddr


async def _attempt_socket(
    target: ProbeTarget,
    timeout_ms: int,
    started_wall: datetime,
    started: float,
    method: str,
    family: int,
    ip: str,
    ip_family: int,
    sockaddr: Any,
) -> ProbeResult:
    proto = socket.IPPROTO_ICMPV6 if family == socket.AF_INET6 else socket.IPPROTO_ICMP
    sock_type = socket.SOCK_RAW if method == "raw" else socket.SOCK_DGRAM
    ident = os.getpid() & 0xFFFF
    seq = _next_seq(target.id)
    token = os.urandom(PAYLOAD_SIZE)
    packet = build_echo_request(ident, seq, token, family)

    try:
        sock = _open_socket(family, sock_type, proto)
    except OSError as exc:
        return _socket_error(target, started_wall, started, timeout_ms, ip, ip_family, exc)

    try:
        sock.setblocking(False)
        sent = perf_counter()
        await _sock_sendto(sock, packet, _destination(sockaddr, family, ip))
        # The budget covers the whole attempt, resolution included, so a slow
        # resolver can never push us past the hard guard (same rule as TCP).
        deadline = started + timeout_ms / 1000.0
        while True:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(_sock_recv(sock, RECV_BUFSIZE), remaining)
            except (asyncio.TimeoutError, TimeoutError):
                break
            received = perf_counter()
            reply = parse_echo_reply(data, family)
            if reply is not None:
                _reply_ident, reply_seq, payload = reply
                if reply_seq == seq and payload[:PAYLOAD_SIZE] == token:
                    return _result(
                        target,
                        started_wall,
                        started,
                        timeout_ms,
                        Outcome.OK,
                        rtt_ms=(received - sent) * 1000.0,
                        resolved_ip=ip,
                        ip_family=ip_family,
                    )
                continue  # someone else's reply (the kernel rewrites our ident)
            quoted = _parse_quoted_echo(data, family)
            if quoted is not None and quoted[1] == seq:
                return _result(
                    target,
                    started_wall,
                    started,
                    timeout_ms,
                    Outcome.ERROR,
                    resolved_ip=ip,
                    ip_family=ip_family,
                    error_kind="icmp_unreachable",
                    error_detail=f"icmp type {quoted[0]} for {ip}",
                )
    except OSError as exc:
        return _socket_error(target, started_wall, started, timeout_ms, ip, ip_family, exc)
    finally:
        sock.close()

    return _result(
        target,
        started_wall,
        started,
        timeout_ms,
        Outcome.TIMEOUT,
        resolved_ip=ip,
        ip_family=ip_family,
    )


async def _attempt_ping(
    target: ProbeTarget,
    timeout_ms: int,
    started_wall: datetime,
    started: float,
    family: int,
    ip: str,
    ip_family: int,
) -> ProbeResult:
    # Our own deadline is the attempt budget, never the child's idea of it: it
    # has to expire before the hard guard so that loss stays `timeout`.
    deadline = started + timeout_ms / 1000.0
    try:
        proc = await asyncio.create_subprocess_exec(
            *ping_argv(timeout_ms, family, ip),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.ERROR,
            resolved_ip=ip,
            ip_family=ip_family,
            error_kind="exec",
            error_detail=_detail(exc),
        )

    remaining = deadline - perf_counter()
    if remaining <= 0:
        await _kill(proc)
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.TIMEOUT,
            resolved_ip=ip,
            ip_family=ip_family,
        )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), remaining)
    except (asyncio.TimeoutError, TimeoutError):
        await _kill(proc)
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.TIMEOUT,
            resolved_ip=ip,
            ip_family=ip_family,
        )
    except asyncio.CancelledError:
        await _kill(proc)
        raise

    text = stdout.decode("utf-8", "replace") + stderr.decode("utf-8", "replace")
    match = _PING_TIME_RE.search(text)
    if proc.returncode == 0 and match:
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.OK,
            rtt_ms=float(match.group(1)),
            resolved_ip=ip,
            ip_family=ip_family,
        )
    if "unreachable" in text.lower():
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.ERROR,
            resolved_ip=ip,
            ip_family=ip_family,
            error_kind="icmp_unreachable",
            error_detail=_first_line(text) or f"unreachable: {ip}",
        )
    if proc.returncode in (1, 2):
        # iputils exits 1 when nothing came back, BSD/macOS exits 2.
        return _result(
            target,
            started_wall,
            started,
            timeout_ms,
            Outcome.TIMEOUT,
            resolved_ip=ip,
            ip_family=ip_family,
        )
    return _result(
        target,
        started_wall,
        started,
        timeout_ms,
        Outcome.ERROR,
        resolved_ip=ip,
        ip_family=ip_family,
        error_kind="exec",
        error_detail=f"ping exit {proc.returncode}: {_first_line(text)}",
    )


async def _kill(proc: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        proc.kill()
    with suppress(Exception):
        await proc.wait()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def ping_argv(timeout_ms: int, family: int, ip: str) -> list[str]:
    """Argument vector for the `ping` fallback, per platform."""
    seconds = max(1, timeout_ms) / 1000.0
    if sys.platform == "darwin":
        # macOS/BSD: -W is milliseconds, -t is a whole-second deadline.
        binary = "ping6" if family == socket.AF_INET6 else "ping"
        return [
            binary,
            "-c",
            "1",
            "-W",
            str(max(1, int(timeout_ms))),
            "-t",
            str(max(1, ceil(seconds))),
            "-n",
            ip,
        ]
    # iputils: -W takes seconds and accepts a fractional value.
    return ["ping", "-c", "1", "-W", f"{seconds:g}", "-n", ip]


def _family_of(family_pref: str) -> int:
    if family_pref == "ipv4":
        return socket.AF_INET
    if family_pref == "ipv6":
        return socket.AF_INET6
    return socket.AF_UNSPEC


def _destination(sockaddr: Any, family: int, ip: str) -> Any:
    if family == socket.AF_INET6:
        flowinfo = sockaddr[2] if isinstance(sockaddr, tuple) and len(sockaddr) > 2 else 0
        scope_id = sockaddr[3] if isinstance(sockaddr, tuple) and len(sockaddr) > 3 else 0
        return (ip, 0, flowinfo, scope_id)
    return (ip, 0)


def _next_seq(target_id: int) -> int:
    seq = (_sequences.get(target_id, 0) + 1) & 0xFFFF
    _sequences[target_id] = seq
    return seq


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_DETAIL]


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:_MAX_ERROR_DETAIL]
    return ""


def _socket_error(
    target: ProbeTarget,
    started_wall: datetime,
    started: float,
    timeout_ms: int,
    ip: str,
    ip_family: int,
    exc: OSError,
) -> ProbeResult:
    denied = isinstance(exc, PermissionError) or exc.errno in (errno.EPERM, errno.EACCES)
    if not denied and exc.errno in UNREACHABLE_ERRNOS:
        # Linux datagram ICMP does not hand the error message to recv(): the
        # kernel keeps the pending error and fails the next call instead. It is
        # still a reply, not a loss (§4.2).
        error_kind = "icmp_unreachable"
    else:
        error_kind = "permission" if denied else "exec"
    return _result(
        target,
        started_wall,
        started,
        timeout_ms,
        Outcome.ERROR,
        resolved_ip=ip,
        ip_family=ip_family,
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
        error_kind=error_kind,
        error_detail=error_detail[:_MAX_ERROR_DETAIL] if error_detail else None,
    )
