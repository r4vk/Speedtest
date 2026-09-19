"""Enums and dataclasses shared by every probe, statistic and accessor (spec §4.1).

This module owns the vocabulary of a probe attempt: its outcome, its protocol and
the row shape stored in ``probe_results`` / ``probe_targets``. It contains no I/O.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class Outcome(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"


class Protocol(StrEnum):
    ICMP = "icmp"
    TCP = "tcp"
    DNS = "dns"
    HTTPS = "https"


#: Closed set of `error_kind` values (spec §4.3). Anything else is a bug.
ERROR_KINDS: frozenset[str] = frozenset(
    {
        "dns",
        "dns_nxdomain",
        "dns_servfail",
        "dns_refused",
        "dns_no_answer",
        "dns_error",
        "tcp_refused",
        "tcp_reset",
        "tcp_error",
        "tls",
        "http_status",
        "http_protocol",
        "icmp_unreachable",
        "permission",
        "exec",
        "exec_timeout",
        "resolver_unavailable",
    }
)


def _loads_object(raw: str | None) -> dict[str, Any] | None:
    """Parse a JSON object column; anything that is not an object becomes None."""
    if raw is None or raw == "":
        return None
    value = json.loads(raw)
    return value if isinstance(value, dict) else None


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    id: int
    name: str
    kind: str
    protocol: Protocol
    host: str
    port: int | None
    interval_seconds: float
    timeout_ms: int
    enabled: bool
    family_pref: str
    extra: dict[str, Any]

    @staticmethod
    def from_row(row: Mapping[str, Any]) -> "ProbeTarget":
        data = dict(row)
        port = data.get("port")
        return ProbeTarget(
            id=int(data["id"]),
            name=data["name"],
            kind=data["kind"],
            protocol=Protocol(data["protocol"]),
            host=data["host"],
            port=int(port) if port is not None else None,
            interval_seconds=float(data["interval_seconds"]),
            timeout_ms=int(data["timeout_ms"]),
            enabled=bool(data["enabled"]),
            family_pref=data.get("family_pref") or "auto",
            extra=_loads_object(data.get("extra_json")) or {},
        )


@dataclass(frozen=True, slots=True)
class ProbeResult:
    target_id: int
    protocol: Protocol
    started_at: str
    duration_ms: float
    outcome: Outcome
    timeout_ms: int
    rtt_ms: float | None = None
    resolved_ip: str | None = None
    ip_family: int | None = None
    error_kind: str | None = None
    error_detail: str | None = None
    stages: dict[str, float] | None = None
    device_id: str = "nas"
    load_test_id: int | None = None
    external_id: str | None = None

    def to_row(self) -> dict[str, Any]:
        """Column name -> value mapping for the ``probe_results`` table."""
        return {
            "device_id": self.device_id,
            "target_id": self.target_id,
            "protocol": str(self.protocol),
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "outcome": str(self.outcome),
            "rtt_ms": self.rtt_ms,
            "timeout_ms": self.timeout_ms,
            "resolved_ip": self.resolved_ip,
            "ip_family": self.ip_family,
            "error_kind": self.error_kind,
            "error_detail": self.error_detail,
            "stages_json": json.dumps(self.stages) if self.stages is not None else None,
            "load_test_id": self.load_test_id,
            "external_id": self.external_id,
        }

    @staticmethod
    def from_row(row: Mapping[str, Any]) -> "ProbeResult":
        data = dict(row)
        stages = _loads_object(data.get("stages_json"))
        rtt_ms = data.get("rtt_ms")
        ip_family = data.get("ip_family")
        load_test_id = data.get("load_test_id")
        return ProbeResult(
            target_id=int(data["target_id"]),
            protocol=Protocol(data["protocol"]),
            started_at=data["started_at"],
            duration_ms=float(data["duration_ms"]),
            outcome=Outcome(data["outcome"]),
            timeout_ms=int(data["timeout_ms"]),
            rtt_ms=float(rtt_ms) if rtt_ms is not None else None,
            resolved_ip=data.get("resolved_ip"),
            ip_family=int(ip_family) if ip_family is not None else None,
            error_kind=data.get("error_kind"),
            error_detail=data.get("error_detail"),
            stages={k: float(v) for k, v in stages.items()} if stages is not None else None,
            device_id=data.get("device_id") or "nas",
            load_test_id=int(load_test_id) if load_test_id is not None else None,
            external_id=data.get("external_id"),
        )
