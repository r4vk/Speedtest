from __future__ import annotations

import socket
from urllib.parse import urlparse


def resolve_target(target: str, default_port: int) -> tuple[str, int]:
    t = (target or "").strip()
    if not t:
        return ("google.com", default_port)

    if "://" in t:
        parsed = urlparse(t)
        host = parsed.hostname or ""
        if not host:
            return (t, default_port)
        if parsed.port:
            return (host, int(parsed.port))
        scheme = (parsed.scheme or "").lower()
        if scheme == "https":
            return (host, 443)
        if scheme == "http":
            return (host, 80)
        return (host, default_port)

    # hostname / ip
    return (t, default_port)


def tcp_connectivity_check(host: str, port: int, timeout_seconds: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def check_target(target: str, default_port: int, timeout_seconds: float) -> bool:
    host, port = resolve_target(target, default_port=default_port)
    return tcp_connectivity_check(host, port, timeout_seconds)
