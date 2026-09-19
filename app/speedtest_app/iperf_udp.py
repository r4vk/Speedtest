"""
iperf3 UDP/TCP command building and result parsing.

Pure tool-layer module: builds the `iperf3 -J ...` argv for a load-test run and
parses/validates the resulting JSON output into a `LoadTestResult`. This module
never runs a subprocess and never touches the network — a later stage (load
test scheduling) is responsible for executing the command this module builds
and feeding the captured stdout back into `parse_result`.

Design reference: docs/network-quality-design.md §10 ("Load tests").
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Literal

from .network_tools import _validate_hostname

_BITRATE_RE = re.compile(r"^\d+(\.\d+)?[KMG]?$")


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadTestParams:
    server: str
    port: int = 5201
    kind: Literal["iperf_udp", "iperf_tcp"] = "iperf_udp"
    direction: Literal["upload", "download"] = "upload"
    duration_seconds: int = 10
    udp_bitrate: str = "10M"
    datagram_len: int = 1200
    parallel: int = 1


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError(f"port musi byc w zakresie 1-65535, otrzymano {port!r}")
    return port


def _validate_duration(duration: int) -> int:
    if isinstance(duration, bool) or not isinstance(duration, int) or not (1 <= duration <= 60):
        raise ValueError(f"duration_seconds musi byc w zakresie 1-60, otrzymano {duration!r}")
    return duration


def _validate_datagram_len(length: int) -> int:
    if isinstance(length, bool) or not isinstance(length, int) or not (64 <= length <= 65507):
        raise ValueError(f"datagram_len musi byc w zakresie 64-65507, otrzymano {length!r}")
    return length


def _validate_bitrate(bitrate: str) -> str:
    if not isinstance(bitrate, str) or not _BITRATE_RE.match(bitrate):
        raise ValueError(f"udp_bitrate ma nieprawidlowy format: {bitrate!r}")
    return bitrate


def build_command(p: LoadTestParams) -> list[str]:
    """Build the `iperf3 -J ...` argv for one direction of one load test run.

    Both directions of a load test (upload + download) are two sequential
    invocations built from two `LoadTestParams` instances; this function
    never combines them.
    """
    server = _validate_hostname(p.server)
    port = _validate_port(p.port)
    duration = _validate_duration(p.duration_seconds)
    datagram_len = _validate_datagram_len(p.datagram_len)
    bitrate = _validate_bitrate(p.udp_bitrate)

    cmd = ["iperf3", "-c", server, "-p", str(port), "-J", "-t", str(duration)]
    if p.kind == "iperf_udp":
        cmd += ["-u", "-b", bitrate, "-l", str(datagram_len)]
    if p.direction == "download":
        cmd.append("-R")
    if p.parallel > 1:
        cmd += ["-P", str(p.parallel)]
    return cmd


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntervalStat:
    start_s: float
    end_s: float
    packets: int | None
    lost_packets: int | None
    jitter_ms: float | None
    bits_per_second: float | None


@dataclass(frozen=True)
class LoadTestResult:
    kind: str
    direction: str
    receiver: dict
    sender: dict
    intervals: list[IntervalStat]
    duration_seconds: float
    protocol: str
    version: str | None

    def to_json(self) -> dict:
        return asdict(self)


class ResultValidationError(Exception):
    """Raised when iperf3 JSON output cannot be turned into a trustworthy result."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _to_float_or_none(value: object) -> float | None:
    return float(value) if _is_number(value) else None


def _to_int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _pick_udp_views(end: dict) -> tuple[dict, dict]:
    """Select the (receiver, sender) statistics dicts from iperf3's `end` object.

    iperf3 >= 3.1 emits unambiguous per-role views, `end.sum_sent` and
    `end.sum_received`, alongside the legacy `end.sum` (tagged with
    `"sender": true/false`). Older iperf3 (< 3.1) only emits `end.sum`.
    This holds for both directions: for `-R` (download) the local process is
    the receiver, but iperf3 still reports it under the same `sum_received`/
    `sum_sent`/`sum` keys — direction is not used to pick the view.

    Receiver view, in order of preference:
      1. `end.sum_received` if present.
      2. `end.sum` if `end.sum["sender"]` is `False` (old-style, receiver-only report).
      3. Otherwise -> `ResultValidationError("missing receiver view")`.

    Sender view, in order of preference:
      1. `end.sum_sent` if present.
      2. `end.sum` if `end.sum["sender"]` is `True`.
      3. Otherwise -> `{}` (no sender-side data available; sender is informative only).
    """
    sum_sent = end.get("sum_sent")
    sum_received = end.get("sum_received")
    sum_legacy = end.get("sum")

    if isinstance(sum_received, dict):
        receiver = sum_received
    elif isinstance(sum_legacy, dict) and sum_legacy.get("sender") is False:
        receiver = sum_legacy
    else:
        raise ResultValidationError("missing receiver view")

    if isinstance(sum_sent, dict):
        sender = sum_sent
    elif isinstance(sum_legacy, dict) and sum_legacy.get("sender") is True:
        sender = sum_legacy
    else:
        sender = {}

    return receiver, sender


def _require_int_field(view: dict, name: str) -> int:
    value = view.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResultValidationError(f"missing receiver field {name}")
    return value


def _require_number_field(view: dict, name: str) -> float:
    value = view.get(name)
    if not _is_number(value):
        raise ResultValidationError(f"missing receiver field {name}")
    return float(value)


def _udp_side_view(view: dict) -> dict:
    """Best-effort extraction of the common UDP fields, for the (informative) sender side."""
    return {
        "packets": _to_int_or_none(view.get("packets")),
        "lost_packets": _to_int_or_none(view.get("lost_packets")),
        "lost_percent": _to_float_or_none(view.get("lost_percent")),
        "jitter_ms": _to_float_or_none(view.get("jitter_ms")),
        "bits_per_second": _to_float_or_none(view.get("bits_per_second")),
    }


def _build_udp_receiver_sender(end: dict) -> tuple[dict, dict]:
    receiver_view, sender_view = _pick_udp_views(end)

    packets = _require_int_field(receiver_view, "packets")
    lost_packets = _require_int_field(receiver_view, "lost_packets")
    lost_percent = _require_number_field(receiver_view, "lost_percent")
    jitter_ms = _require_number_field(receiver_view, "jitter_ms")

    if packets == 0:
        raise ResultValidationError("no packets received")

    recomputed = False
    expected_percent = (lost_packets / packets) * 100
    if abs(lost_percent - expected_percent) > 0.5:
        lost_percent = expected_percent
        recomputed = True

    receiver = {
        "packets": packets,
        "lost_packets": lost_packets,
        "lost_percent": lost_percent,
        "jitter_ms": jitter_ms,
        "bits_per_second": _to_float_or_none(receiver_view.get("bits_per_second")),
    }
    if recomputed:
        receiver["lost_percent_recomputed"] = True

    sender = _udp_side_view(sender_view)
    return receiver, sender


def _build_tcp_receiver_sender(end: dict) -> tuple[dict, dict]:
    sum_received = end.get("sum_received")
    sum_sent = end.get("sum_sent")

    if not isinstance(sum_received, dict) or not _is_number(sum_received.get("bits_per_second")):
        raise ResultValidationError("missing receiver field bits_per_second")
    if not isinstance(sum_sent, dict) or not _is_number(sum_sent.get("bits_per_second")):
        raise ResultValidationError("missing sender field bits_per_second")

    receiver = {
        "bits_per_second": float(sum_received["bits_per_second"]),
        "bytes": sum_received.get("bytes"),
        "retransmits": sum_received.get("retransmits"),
    }
    sender = {
        "bits_per_second": float(sum_sent["bits_per_second"]),
        "bytes": sum_sent.get("bytes"),
        "retransmits": sum_sent.get("retransmits"),
    }
    return receiver, sender


def _parse_interval(raw: object) -> IntervalStat:
    sum_ = raw.get("sum") if isinstance(raw, dict) else None
    sum_ = sum_ if isinstance(sum_, dict) else {}
    start = _to_float_or_none(sum_.get("start"))
    end = _to_float_or_none(sum_.get("end"))
    return IntervalStat(
        start_s=start if start is not None else 0.0,
        end_s=end if end is not None else 0.0,
        packets=_to_int_or_none(sum_.get("packets")),
        lost_packets=_to_int_or_none(sum_.get("lost_packets")),
        jitter_ms=_to_float_or_none(sum_.get("jitter_ms")),
        bits_per_second=_to_float_or_none(sum_.get("bits_per_second")),
    )


def _extract_duration_seconds(end: dict) -> float:
    sum_legacy = end.get("sum")
    if isinstance(sum_legacy, dict) and _is_number(sum_legacy.get("seconds")):
        return float(sum_legacy["seconds"])
    sum_received = end.get("sum_received")
    if isinstance(sum_received, dict) and _is_number(sum_received.get("seconds")):
        return float(sum_received["seconds"])
    return 0.0


def parse_result(stdout: str, *, kind: str, direction: str) -> LoadTestResult:
    """Parse and validate iperf3 `-J` stdout into a `LoadTestResult`.

    Raises `ResultValidationError` (never a synthetic 0% loss) when the JSON
    is malformed, iperf3 itself reported an `error`, the reported protocol
    does not match `kind`, or required receiver fields are missing/invalid.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ResultValidationError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ResultValidationError("invalid JSON: top-level value is not an object")

    error = data.get("error")
    if isinstance(error, str):
        raise ResultValidationError(error)

    expected_protocol = "UDP" if kind == "iperf_udp" else "TCP"
    start = data.get("start")
    start = start if isinstance(start, dict) else {}
    test_start = start.get("test_start")
    test_start = test_start if isinstance(test_start, dict) else {}
    actual_protocol = test_start.get("protocol")
    if actual_protocol != expected_protocol:
        raise ResultValidationError(
            f"protocol mismatch: expected {expected_protocol}, got {actual_protocol!r}"
        )

    end = data.get("end")
    if not isinstance(end, dict):
        raise ResultValidationError("missing end section")

    if kind == "iperf_udp":
        receiver, sender = _build_udp_receiver_sender(end)
    else:
        receiver, sender = _build_tcp_receiver_sender(end)

    raw_intervals = data.get("intervals")
    intervals = (
        [_parse_interval(iv) for iv in raw_intervals]
        if isinstance(raw_intervals, list)
        else []
    )

    version = start.get("version")
    if not isinstance(version, str):
        version = None

    return LoadTestResult(
        kind=kind,
        direction=direction,
        receiver=receiver,
        sender=sender,
        intervals=intervals,
        duration_seconds=_extract_duration_seconds(end),
        protocol=actual_protocol,
        version=version,
    )


def summarize_for_report(r: LoadTestResult) -> dict:
    """Reduce a `LoadTestResult` to the numbers shown in reports.

    Loss is always recomputed from the receiver's raw counters
    (`lost_packets`/`packets`), not read back from `receiver["lost_percent"]`,
    so the reported figure is internally consistent with `lost`/`packets`.
    """
    packets = r.receiver.get("packets")
    lost = r.receiver.get("lost_packets")
    jitter_ms = r.receiver.get("jitter_ms")
    bits_per_second = r.receiver.get("bits_per_second")

    loss_pct = round((lost / packets) * 100, 2) if packets else None
    mbps = round(bits_per_second / 1_000_000, 2) if _is_number(bits_per_second) else None

    return {
        "loss_pct": loss_pct,
        "lost": lost,
        "packets": packets,
        "jitter_ms": jitter_ms,
        "mbps": mbps,
        "direction": r.direction,
        "kind": r.kind,
        "recomputed": bool(r.receiver.get("lost_percent_recomputed", False)),
    }
