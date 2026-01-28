from __future__ import annotations

import ftplib
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx


@dataclass(frozen=True)
class SpeedTestResult:
    started_at_monotonic: float
    duration_seconds: float
    bytes_downloaded: int
    mbps: float
    error: str | None = None


def _calc_mbps(bytes_downloaded: int, duration_seconds: float) -> float:
    if duration_seconds <= 0:
        return 0.0
    return (bytes_downloaded * 8.0) / duration_seconds / 1_000_000.0


def _download_http(url: str, duration_seconds: float, timeout_seconds: float) -> SpeedTestResult:
    started = time.monotonic()
    total = 0
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout_seconds) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if (time.monotonic() - started) >= duration_seconds:
                        break
        elapsed = max(0.001, time.monotonic() - started)
        return SpeedTestResult(
            started_at_monotonic=started,
            duration_seconds=elapsed,
            bytes_downloaded=total,
            mbps=_calc_mbps(total, elapsed),
            error=None,
        )
    except Exception as e:
        elapsed = max(0.001, time.monotonic() - started)
        return SpeedTestResult(
            started_at_monotonic=started,
            duration_seconds=elapsed,
            bytes_downloaded=total,
            mbps=_calc_mbps(total, elapsed),
            error=str(e),
        )


def _download_ftp(url: str, duration_seconds: float, timeout_seconds: float) -> SpeedTestResult:
    started = time.monotonic()
    total = 0
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return SpeedTestResult(started, 0.001, 0, 0.0, error="Invalid FTP URL (missing hostname)")

    port = parsed.port or 21
    username = unquote(parsed.username) if parsed.username else "anonymous"
    password = unquote(parsed.password) if parsed.password else "anonymous@"
    path = parsed.path or "/"
    if path.startswith("/"):
        path = path[1:]
    path = unquote(path)

    ftp: ftplib.FTP | None = None
    sock = None
    try:
        ftp = ftplib.FTP()
        ftp.connect(host=host, port=port, timeout=timeout_seconds)
        ftp.login(user=username, passwd=password)
        ftp.voidcmd("TYPE I")

        sock = ftp.transfercmd(f"RETR {path}")
        sock.settimeout(timeout_seconds)
        while (time.monotonic() - started) < duration_seconds:
            chunk = sock.recv(64 * 1024)
            if not chunk:
                break
            total += len(chunk)

        try:
            sock.close()
        except Exception:
            pass

        try:
            ftp.abort()
        except Exception:
            pass

        try:
            ftp.close()
        except Exception:
            pass

        elapsed = max(0.001, time.monotonic() - started)
        return SpeedTestResult(
            started_at_monotonic=started,
            duration_seconds=elapsed,
            bytes_downloaded=total,
            mbps=_calc_mbps(total, elapsed),
            error=None,
        )
    except Exception as e:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass
        try:
            if ftp is not None:
                ftp.close()
        except Exception:
            pass
        elapsed = max(0.001, time.monotonic() - started)
        return SpeedTestResult(
            started_at_monotonic=started,
            duration_seconds=elapsed,
            bytes_downloaded=total,
            mbps=_calc_mbps(total, elapsed),
            error=str(e),
        )


def run_speed_test(url: str, duration_seconds: float, timeout_seconds: float) -> SpeedTestResult:
    parsed = urlparse(url)
    if parsed.scheme.lower() in {"http", "https"}:
        return _download_http(url, duration_seconds, timeout_seconds)
    if parsed.scheme.lower() == "ftp":
        return _download_ftp(url, duration_seconds, timeout_seconds)
    return SpeedTestResult(0.0, 0.001, 0, 0.0, error=f"Unsupported URL scheme: {parsed.scheme}")

