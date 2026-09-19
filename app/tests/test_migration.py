"""Migration 1 -> 2: new tables, seeded targets, legacy data untouched, atomicity."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from speedtest_app import db as db_module
from speedtest_app.db import SCHEMA_VERSION, db_conn, ensure_db

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

V2_TABLES = [
    "probe_targets",
    "probe_results",
    "monitor_sessions",
    "config_changes",
    "incidents",
    "probe_aggregates",
    "annotations",
    "load_tests",
    "diagnostics",
    "devices",
]

SEEDED_TARGET_NAMES = [
    "gateway",
    "cloudflare-dns",
    "google-dns",
    "quad9-dns",
    "legacy-tcp",
    "dns-system",
    "https-cloudflare",
    "https-google",
]


def _tables(path: str) -> set[str]:
    with db_conn(path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r["name"] for r in rows}


def _schema_version(path: str) -> str | None:
    with db_conn(path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return row["value"] if row else None


def _targets(path: str) -> dict[str, dict]:
    with db_conn(path) as conn:
        rows = conn.execute("SELECT * FROM probe_targets").fetchall()
    return {r["name"]: dict(r) for r in rows}


def _counts(path: str, tables: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    with db_conn(path) as conn:
        for table in tables:
            out[table] = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    return out


def _build_v1_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    script = (FIXTURES_DIR / "app_v1.sql").read_text()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(script)
        conn.commit()
    finally:
        conn.close()


def test_empty_db_migrates_to_v2(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    path = str(tmp_path / "data" / "app.db")

    ensure_db(path)

    assert SCHEMA_VERSION == 2
    assert _schema_version(path) == "2"
    assert set(V2_TABLES).issubset(_tables(path))

    targets = _targets(path)
    assert sorted(targets) == sorted(SEEDED_TARGET_NAMES)
    assert targets["cloudflare-dns"]["host"] == "1.1.1.1"
    assert targets["cloudflare-dns"]["protocol"] == "icmp"
    assert targets["cloudflare-dns"]["interval_seconds"] == 1.0
    assert targets["cloudflare-dns"]["timeout_ms"] == 1000
    assert targets["cloudflare-dns"]["enabled"] == 1
    assert targets["google-dns"]["host"] == "8.8.8.8"
    assert targets["quad9-dns"]["host"] == "9.9.9.9"
    assert targets["dns-system"]["host"] == "example.com"
    assert targets["dns-system"]["interval_seconds"] == 30
    assert targets["dns-system"]["timeout_ms"] == 2000
    assert '"resolver": "system"' in targets["dns-system"]["extra_json"]
    assert targets["https-cloudflare"]["host"] == "https://cloudflare.com/cdn-cgi/trace"
    assert targets["https-cloudflare"]["port"] == 443
    assert targets["https-cloudflare"]["interval_seconds"] == 60
    assert targets["https-cloudflare"]["timeout_ms"] == 5000
    assert targets["https-google"]["host"] == "https://www.google.com/generate_204"

    with db_conn(path) as conn:
        device = conn.execute("SELECT * FROM devices WHERE id = 'nas'").fetchone()
        change = conn.execute(
            "SELECT * FROM config_changes WHERE key = 'schema_version'"
        ).fetchone()
    assert device["name"] == "NAS (kabel)"
    assert device["kind"] == "nas"
    assert (change["old_value"], change["new_value"], change["source"]) == ("1", "2", "migration")


def test_gateway_seed_disabled_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    path = str(tmp_path / "data" / "app.db")

    ensure_db(path)

    gateway = _targets(path)["gateway"]
    assert gateway["host"] == ""
    assert gateway["enabled"] == 0
    assert gateway["kind"] == "gateway"
    assert gateway["protocol"] == "icmp"


def test_gateway_seed_enabled_with_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_HOST", "192.168.1.1")
    path = str(tmp_path / "data" / "app.db")

    ensure_db(path)

    gateway = _targets(path)["gateway"]
    assert gateway["host"] == "192.168.1.1"
    assert gateway["enabled"] == 1


def test_gateway_seed_refuses_an_unusable_env_host(tmp_path, monkeypatch):
    """finding I7: the seed is the one path into `host` that skips the API."""
    monkeypatch.setenv("GATEWAY_HOST", "-oN /tmp/pwn")
    path = str(tmp_path / "data" / "app.db")

    ensure_db(path)

    gateway = _targets(path)["gateway"]
    assert gateway["host"] == ""
    assert gateway["enabled"] == 0


def test_legacy_tcp_seed_derived_from_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    path = str(tmp_path / "data" / "app.db")
    _build_v1_db(path)

    ensure_db(path)

    legacy = _targets(path)["legacy-tcp"]
    assert legacy["kind"] == "tcp"
    assert legacy["protocol"] == "tcp"
    assert legacy["host"] == "example.org"
    assert legacy["port"] == 443
    assert legacy["interval_seconds"] == 7.0
    assert legacy["timeout_ms"] == 1500
    assert legacy["enabled"] == 1


def test_legacy_tcp_seed_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    path = str(tmp_path / "data" / "app.db")

    ensure_db(path)

    from speedtest_app.config import AppConfig

    cfg = AppConfig()
    legacy = _targets(path)["legacy-tcp"]
    assert legacy["host"] == cfg.connect_target
    assert legacy["port"] == cfg.connect_default_port
    assert legacy["interval_seconds"] == cfg.connect_interval_seconds
    assert legacy["timeout_ms"] == cfg.ping_timeout_ms


def test_v1_history_survives_migration(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    path = str(tmp_path / "data" / "app.db")
    _build_v1_db(path)

    legacy_tables = [
        "connectivity_periods",
        "connectivity_checks",
        "speed_tests",
        "blocked_periods",
        "settings",
    ]
    before = _counts(path, legacy_tables)
    with db_conn(path) as conn:
        periods_before = [dict(r) for r in conn.execute("SELECT * FROM connectivity_periods ORDER BY id")]
        checks_before = [dict(r) for r in conn.execute("SELECT * FROM connectivity_checks ORDER BY id")]
        speed_before = [dict(r) for r in conn.execute("SELECT * FROM speed_tests ORDER BY id")]

    ensure_db(path)

    assert _schema_version(path) == "2"
    assert _counts(path, legacy_tables) == before
    with db_conn(path) as conn:
        periods_after = [dict(r) for r in conn.execute("SELECT * FROM connectivity_periods ORDER BY id")]
        checks_after = [dict(r) for r in conn.execute("SELECT * FROM connectivity_checks ORDER BY id")]
        speed_after = [dict(r) for r in conn.execute("SELECT * FROM speed_tests ORDER BY id")]
    assert periods_after == periods_before
    assert checks_after == checks_before
    # speed_tests gains the v1.x columns (NULL) but keeps every existing value
    for old, new in zip(speed_before, speed_after):
        assert {k: new[k] for k in old} == old
        assert new["upload_mbps"] is None


def test_ensure_db_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    path = str(tmp_path / "data" / "app.db")
    _build_v1_db(path)

    ensure_db(path)
    tables = ["probe_targets", "devices", "config_changes", "connectivity_checks", "settings"]
    after_first = _counts(path, tables)
    targets_first = _targets(path)

    ensure_db(path)

    assert _schema_version(path) == "2"
    assert _counts(path, tables) == after_first
    assert _targets(path) == targets_first


def test_failed_migration_leaves_v1_intact(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    path = str(tmp_path / "data" / "app.db")
    _build_v1_db(path)
    before = _counts(path, ["connectivity_checks", "settings"])

    broken = tuple(db_module.SCHEMA_V2_DDL) + ("CREATE TABLE definitely not valid sql (",)
    monkeypatch.setattr(db_module, "SCHEMA_V2_DDL", broken)

    with pytest.raises(sqlite3.Error):
        ensure_db(path)

    assert _schema_version(path) == "1"
    assert _tables(path).isdisjoint(set(V2_TABLES))
    assert _counts(path, ["connectivity_checks", "settings"]) == before
