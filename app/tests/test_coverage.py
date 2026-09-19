"""Monitor sessions and observed-time coverage (design spec §9)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from speedtest_app import quality_db
from speedtest_app.coverage import (
    SessionTracker,
    clip_to_observed,
    coverage,
    observed_intervals,
)
from speedtest_app.db import db_conn, start_blocked_period, end_blocked_period
from speedtest_app.time_utils import to_iso_z

BASE = datetime(2026, 1, 10, 10, 0, 0, tzinfo=timezone.utc)


def t(offset_seconds: float) -> datetime:
    return BASE + timedelta(seconds=offset_seconds)


def iso(offset_seconds: float) -> str:
    return to_iso_z(t(offset_seconds))


def _sessions(db_path: str) -> list[dict]:
    with db_conn(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM monitor_sessions ORDER BY id")]


def test_session_lifecycle_clean(db_path):
    tracker = SessionTracker()

    session_id = tracker.start(db_path, "nas", "1.2.3", iso(0))
    tracker.heartbeat(iso(30))
    tracker.stop(iso(60))

    rows = _sessions(db_path)
    assert len(rows) == 1
    assert rows[0]["id"] == session_id == tracker.session_id
    assert rows[0]["device_id"] == "nas"
    assert rows[0]["app_version"] == "1.2.3"
    assert rows[0]["started_at"] == iso(0)
    assert rows[0]["last_seen_at"] == iso(30)
    assert rows[0]["ended_at"] == iso(60)
    assert rows[0]["end_reason"] == "shutdown"


def test_start_closes_stale_session_as_unclean(db_path):
    stale_id = quality_db.start_session(db_path, "nas", "1.0.0", iso(0))
    quality_db.heartbeat_session(db_path, stale_id, iso(120))

    new_id = SessionTracker().start(db_path, "nas", "1.2.3", iso(600))

    rows = {r["id"]: r for r in _sessions(db_path)}
    assert rows[stale_id]["ended_at"] == iso(120)
    assert rows[stale_id]["end_reason"] == "unclean"
    assert rows[new_id]["ended_at"] is None
    assert rows[new_id]["started_at"] == iso(600)


def test_observed_intervals_with_restart_gap(db_path):
    first = quality_db.start_session(db_path, "nas", "1.0.0", iso(0))
    quality_db.end_session(db_path, first, iso(300), "unclean")
    second = quality_db.start_session(db_path, "nas", "1.0.0", iso(500))
    quality_db.end_session(db_path, second, iso(900), "shutdown")

    intervals = observed_intervals(db_path, t(0), t(900))

    assert intervals == [(t(0), t(300)), (t(500), t(900))]


def test_observed_intervals_subtract_blocked_period(db_path):
    session_id = quality_db.start_session(db_path, "nas", "1.0.0", iso(0))
    quality_db.end_session(db_path, session_id, iso(600), "shutdown")
    start_blocked_period(db_path, "ping", "disabled", now_iso=iso(200))
    end_blocked_period(db_path, "ping", now_iso=iso(300))

    intervals = observed_intervals(db_path, t(0), t(600))

    assert intervals == [(t(0), t(200)), (t(300), t(600))]


def test_observed_intervals_open_session_gets_grace_and_clipping(db_path):
    session_id = quality_db.start_session(db_path, "nas", "1.0.0", iso(0))
    quality_db.heartbeat_session(db_path, session_id, iso(100))

    # grace of 30 s on last_seen_at, but never beyond the requested range
    assert observed_intervals(db_path, t(-60), t(600)) == [(t(0), t(130))]
    assert observed_intervals(db_path, t(50), t(120)) == [(t(50), t(120))]


def test_coverage_reports_seconds_and_gaps(db_path):
    first = quality_db.start_session(db_path, "nas", "1.0.0", iso(0))
    quality_db.end_session(db_path, first, iso(300), "unclean")
    second = quality_db.start_session(db_path, "nas", "1.0.0", iso(400))
    quality_db.end_session(db_path, second, iso(1000), "shutdown")
    start_blocked_period(db_path, "ping", "disabled", now_iso=iso(500))
    end_blocked_period(db_path, "ping", now_iso=iso(600))

    result = coverage(db_path, t(0), t(1000))

    assert result["coverage_known"] is True
    assert result["total_seconds"] == 1000.0
    assert result["observed_seconds"] == 800.0
    assert result["coverage_pct"] == 80.0
    assert result["gaps"] == [
        {"from": iso(300), "to": iso(400), "reason": "not_running"},
        {"from": iso(500), "to": iso(600), "reason": "disabled"},
    ]


def test_coverage_unknown_without_any_session(db_path):
    result = coverage(db_path, t(0), t(1000))

    assert result["coverage_known"] is False
    assert result["coverage_pct"] is None
    assert result["observed_seconds"] is None
    assert result["total_seconds"] == 1000.0
    assert result["gaps"] == []


def test_clip_to_observed_intersects_intervals():
    observed = [(t(0), t(100)), (t(200), t(300))]
    intervals = [(t(50), t(250))]

    assert clip_to_observed(intervals, observed) == [(t(50), t(100)), (t(200), t(250))]
    assert clip_to_observed([(t(400), t(500))], observed) == []
