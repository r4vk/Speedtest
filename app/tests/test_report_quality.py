"""`/api/report/quality` reports observed time, not wall-clock guesses (spec §9)."""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sqlite3

import pytest

from speedtest_app import quality_db
from speedtest_app.db import db_conn

HOUR = 3600.0


def _add_down_period(db_path: str, started_at: str, ended_at: str | None) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO connectivity_periods(started_at, ended_at, is_up) VALUES (?,?,0)",
            (started_at, ended_at),
        )


def _add_session(db_path: str, started_at: str, ended_at: str) -> None:
    session_id = quality_db.start_session(db_path, "nas", "test", started_at)
    quality_db.end_session(db_path, session_id, ended_at, "shutdown")


def _add_blocked_period(db_path: str, started_at: str, ended_at: str, reason: str = "disabled") -> None:
    # Inserted directly: the running app keeps its own open 'ping' blocked period
    # (the legacy loop is disabled in tests), which start_blocked_period would skip.
    with db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO blocked_periods(test_type, started_at, ended_at, reason) VALUES ('ping',?,?,?)",
            (started_at, ended_at, reason),
        )


def _drop_sessions(db_path: str) -> None:
    with db_conn(db_path) as conn:
        conn.execute("DELETE FROM monitor_sessions")


def _report(client, utc_iso, from_offset: float, to_offset: float) -> dict:
    response = client.get(
        "/api/report/quality",
        params={"from": utc_iso(from_offset), "to": utc_iso(to_offset)},
    )
    assert response.status_code == 200
    return response.json()


def test_startup_opens_a_monitor_session(client):
    sessions = quality_db.query_sessions(
        client.app_db_path, "1970-01-01T00:00:00.000Z", "2999-01-01T00:00:00.000Z"
    )
    assert len(sessions) == 1
    assert sessions[0]["ended_at"] is None
    assert sessions[0]["device_id"] == "nas"


def test_empty_database_reports_no_downtime(client, utc_iso):
    data = _report(client, utc_iso, -3 * HOUR, -1 * HOUR)

    assert data["incident_count"] == 0
    assert data["downtime_seconds"] == 0.0
    assert data["downtime_percent"] == 0.0
    assert data["downtime_percent_of_range"] == 0.0
    assert data["total_seconds"] == pytest.approx(2 * HOUR)
    assert data["coverage_known"] is True


def test_legacy_history_without_sessions_keeps_old_numbers(client, utc_iso):
    db_path = client.app_db_path
    _drop_sessions(db_path)
    _add_down_period(db_path, utc_iso(-2.5 * HOUR), utc_iso(-2 * HOUR))

    data = _report(client, utc_iso, -3 * HOUR, -1 * HOUR)

    assert data["coverage_known"] is False
    assert data["coverage_pct"] is None
    assert data["observed_seconds"] is None
    assert data["gaps"] == []
    assert data["incident_count"] == 1
    assert data["downtime_seconds"] == pytest.approx(1800.0)
    assert data["downtime_percent"] == pytest.approx(25.0)
    assert data["downtime_percent_of_range"] == pytest.approx(25.0)


def test_downtime_is_clipped_to_observed_time_across_a_restart(client, utc_iso):
    db_path = client.app_db_path
    _add_session(db_path, utc_iso(-3 * HOUR), utc_iso(-2.5 * HOUR))
    _add_session(db_path, utc_iso(-2 * HOUR), utc_iso(-1 * HOUR))
    # the outage spans the restart gap; unobserved time must not be counted
    _add_down_period(db_path, utc_iso(-2.75 * HOUR), utc_iso(-1.5 * HOUR))

    data = _report(client, utc_iso, -3 * HOUR, -1 * HOUR)

    assert data["coverage_known"] is True
    assert data["observed_seconds"] == pytest.approx(1.5 * HOUR)
    assert data["coverage_pct"] == pytest.approx(75.0)
    assert data["downtime_seconds"] == pytest.approx(0.25 * HOUR + 0.5 * HOUR)
    assert data["downtime_percent"] == pytest.approx(2700.0 / 5400.0 * 100.0)
    assert data["downtime_percent_of_range"] == pytest.approx(4500.0 / 7200.0 * 100.0)
    assert [g["reason"] for g in data["gaps"]] == ["not_running"]


def test_disabled_monitor_inside_an_outage_is_excluded(client, utc_iso):
    db_path = client.app_db_path
    _add_session(db_path, utc_iso(-3 * HOUR), utc_iso(-1 * HOUR))
    _add_down_period(db_path, utc_iso(-2.5 * HOUR), utc_iso(-1.5 * HOUR))
    _add_blocked_period(db_path, utc_iso(-2.25 * HOUR), utc_iso(-2 * HOUR))

    data = _report(client, utc_iso, -3 * HOUR, -1 * HOUR)

    assert data["observed_seconds"] == pytest.approx(2 * HOUR - 900.0)
    assert data["downtime_seconds"] == pytest.approx(HOUR - 900.0)
    assert data["downtime_percent"] == pytest.approx(2700.0 / 6300.0 * 100.0)
    assert data["downtime_percent_of_range"] == pytest.approx(3600.0 / 7200.0 * 100.0)
    assert [g["reason"] for g in data["gaps"]] == ["disabled"]


async def test_heartbeat_loop_survives_a_failing_heartbeat(client, caplog):
    main_module = importlib.import_module("speedtest_app.main")
    state = main_module.RunningState(stop=asyncio.Event())
    attempts: list[str] = []

    class FlakyTracker:
        def heartbeat(self, now_iso: str) -> None:
            attempts.append(now_iso)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            state.stop.set()

    with caplog.at_level(logging.WARNING):
        await main_module._session_heartbeat_loop(FlakyTracker(), state, interval_seconds=0.001)

    # the first heartbeat failed loudly, the loop kept going and beat again
    assert len(attempts) == 2
    assert "heartbeat failed" in caplog.text.lower()


def test_range_partially_without_data_has_coverage_below_100(client, utc_iso):
    db_path = client.app_db_path
    _add_session(db_path, utc_iso(-2 * HOUR), utc_iso(-1 * HOUR))

    data = _report(client, utc_iso, -3 * HOUR, -1 * HOUR)

    assert data["coverage_known"] is True
    assert data["coverage_pct"] == pytest.approx(50.0)
    assert data["observed_seconds"] == pytest.approx(HOUR)
    assert [g["reason"] for g in data["gaps"]] == ["not_running"]


def test_client_fixture_restores_the_reloaded_modules():
    # Runs after the `client` tests of this module: their teardown must have put
    # the config snapshot (and db's reference to it) back.
    config_module = importlib.import_module("speedtest_app.config")
    db_module = importlib.import_module("speedtest_app.db")

    assert config_module.AppConfig().data_dir == os.getenv("DATA_DIR", "/data")
    assert db_module.AppConfig is config_module.AppConfig
