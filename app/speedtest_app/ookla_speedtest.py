from __future__ import annotations

import time
from dataclasses import dataclass

import speedtest  # type: ignore


@dataclass(frozen=True)
class OoklaResult:
    duration_seconds: float
    download_mbps: float
    upload_mbps: float | None
    ping_ms: float | None
    server_name: str | None
    server_country: str | None
    error: str | None = None


def _filter_servers_pl(s: speedtest.Speedtest) -> None:
    s.get_servers()
    filtered: dict[float, list[dict]] = {}
    for dist, servers in getattr(s, "servers", {}).items():
        pl = [srv for srv in servers if (srv.get("cc") == "PL" or srv.get("country") == "Poland")]
        if pl:
            filtered[dist] = pl
    s.servers = filtered


def run_ookla(mode: str, timeout_seconds: float) -> OoklaResult:
    started = time.monotonic()
    try:
        s = speedtest.Speedtest(secure=True, timeout=timeout_seconds)
        if mode == "speedtest.pl":
            _filter_servers_pl(s)
        best = s.get_best_server()
        ping_ms = best.get("latency")

        download_bps = s.download()
        upload_bps = None
        try:
            upload_bps = s.upload()
        except Exception:
            upload_bps = None

        elapsed = max(0.001, time.monotonic() - started)
        return OoklaResult(
            duration_seconds=elapsed,
            download_mbps=float(download_bps) / 1_000_000.0,
            upload_mbps=(float(upload_bps) / 1_000_000.0) if upload_bps is not None else None,
            ping_ms=float(ping_ms) if ping_ms is not None else None,
            server_name=best.get("sponsor") or best.get("name"),
            server_country=best.get("country") or best.get("cc"),
            error=None,
        )
    except Exception as e:
        elapsed = max(0.001, time.monotonic() - started)
        return OoklaResult(
            duration_seconds=elapsed,
            download_mbps=0.0,
            upload_mbps=None,
            ping_ms=None,
            server_name=None,
            server_country=None,
            error=str(e),
        )

