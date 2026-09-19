"""The one table of quality settings: key -> (type, default) (spec §7, §8, §10, §11, §14).

Every module that reads a quality setting reads its default from here, so the
incident engine, the load tests, the diagnostics, the retention job, the API
and the report can never disagree about what "unset" means. The values live in
the ``settings`` table as strings; this module owns their serialisation.
"""
from __future__ import annotations

from typing import Any, Mapping

from .db import ensure_default_setting, get_settings

#: Setting key -> (type, default). The type drives both parsing and
#: serialisation; ``bool`` is stored as the strings "true"/"false".
QUALITY_SETTING_SPECS: dict[str, tuple[type, Any]] = {
    # §7 — incident state machine
    "incident_window_seconds": (int, 10),
    "incident_min_samples": (int, 5),
    "incident_loss_pct_threshold": (float, 20.0),
    "incident_outage_loss_pct": (float, 100.0),
    "incident_rtt_p95_ms_threshold": (float, 150.0),
    "incident_fail_streak_threshold": (int, 3),
    "incident_open_windows": (int, 2),
    "incident_stabilization_seconds": (int, 60),
    "incident_no_data_close_seconds": (int, 300),
    # §8 — availability evaluator
    "availability_eval_seconds": (int, 5),
    "availability_window_seconds": (int, 10),
    # §10 — load tests
    "load_test_enabled": (bool, False),
    "load_test_interval_seconds": (int, 21600),
    "load_test_kind": (str, "iperf_udp"),
    "load_test_server": (str, ""),
    "load_test_port": (int, 5201),
    "load_test_udp_bitrate": (str, "10M"),
    "load_test_duration_seconds": (int, 10),
    "load_test_datagram_len": (int, 1200),
    "load_test_directions": (str, "both"),
    # §11 — incident diagnostics
    "diagnostics_enabled": (bool, True),
    "diagnostics_min_interval_seconds": (int, 300),
    "diagnostics_max_per_incident": (int, 3),
    "diagnostics_max_concurrent": (int, 1),
    "diagnostics_mtr_count": (int, 10),
    "diagnostics_mtr_timeout_seconds": (int, 90),
    # §14 — retention
    "retention_raw_days": (int, 14),
    "retention_aggregate_days": (int, 365),
    "retention_incident_days": (int, 730),
    "retention_load_test_raw_days": (int, 90),
    "retention_diagnostics_days": (int, 365),
    # cross-cutting
    #: Allows sub-second probe intervals; off by default so a mistyped
    #: interval cannot flood the link (spec §12).
    "diagnostic_mode": (bool, False),
    #: Mirrors the host of the `gateway` target (spec §3.1).
    "gateway_host": (str, ""),
}

#: The thresholds shown in the report's configuration section (spec §13.1).
INCIDENT_THRESHOLD_KEYS: tuple[str, ...] = tuple(
    key for key in QUALITY_SETTING_SPECS if key.startswith("incident_")
)


def serialize_setting(key: str, value: Any) -> str:
    """Render ``value`` the way the ``settings`` table stores it."""
    kind, _default = QUALITY_SETTING_SPECS[key]
    if kind is bool:
        return "true" if value else "false"
    return str(value)


def _parse(kind: type, raw: str, default: Any) -> Any:
    """Parse one stored value; anything unusable keeps the default."""
    text = raw.strip()
    try:
        if kind is bool:
            return text.lower() in {"true", "1", "yes", "on"}
        if kind is int:
            return int(float(text))
        if kind is float:
            return float(text)
        return text
    except (TypeError, ValueError):
        return default


def parse_settings(values: Mapping[str, str]) -> dict[str, Any]:
    """Typed view of raw ``settings`` rows; missing keys fall back to defaults."""
    parsed: dict[str, Any] = {}
    for key, (kind, default) in QUALITY_SETTING_SPECS.items():
        raw = values.get(key)
        parsed[key] = default if raw is None else _parse(kind, str(raw), default)
    return parsed


def read_quality_settings(db_path: str) -> dict[str, Any]:
    """Every quality setting of this database, typed and defaulted."""
    return parse_settings(get_settings(db_path, list(QUALITY_SETTING_SPECS)))


def ensure_quality_defaults(db_path: str, overrides: Mapping[str, Any] | None = None) -> None:
    """Seed every missing quality setting with its default (or an override).

    Called once at import time by ``main.py``: an operator who never opens the
    settings page still gets the documented behaviour, and ``config_changes``
    records the seeding with source ``env``.
    """
    overrides = overrides or {}
    for key, (_kind, default) in QUALITY_SETTING_SPECS.items():
        value = overrides.get(key, default)
        ensure_default_setting(db_path, key, serialize_setting(key, value))
