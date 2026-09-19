"""Retention: pruning old data and estimating DB growth (design spec §14)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from speedtest_app import quality_db, retention
from speedtest_app.probe_types import Outcome, ProbeResult, ProbeTarget, Protocol
from speedtest_app.time_utils import to_iso_z

DAY0 = datetime(2026, 9, 18, tzinfo=timezone.utc)  # fully pruned
DAY1 = datetime(2026, 9, 19, tzinfo=timezone.utc)  # fully pruned
DAY2 = datetime(2026, 9, 20, tzinfo=timezone.utc)  # kept (>= raw cutoff)
NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
NOW_ISO = to_iso_z(NOW)


def _target(db_path: str, name: str) -> ProbeTarget:
    return quality_db.insert_target(
        db_path,
        name=name,
        kind="internet",
        protocol=Protocol.ICMP,
        host="1.1.1.1",
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=True,
    )


def _seed_raw(db_path: str, target: ProbeTarget) -> None:
    """Two rows a day for three days: DAY0/DAY1 are pruned, DAY2 survives."""
    rows = [
        ProbeResult(
            target_id=target.id,
            protocol=target.protocol,
            started_at=to_iso_z(day + offset),
            duration_ms=1.0,
            outcome=Outcome.OK,
            timeout_ms=1000,
            rtt_ms=10.0,
        )
        for day in (DAY0, DAY1, DAY2)
        for offset in (timedelta(hours=3), timedelta(hours=15))
    ]
    assert quality_db.insert_probe_results(db_path, rows) == len(rows)


def _settings(**overrides) -> retention.RetentionSettings:
    base = dict(raw_days=1, aggregate_days=30, incident_days=2, load_test_raw_days=2, diagnostics_days=2, batch_size=2)
    base.update(overrides)
    return retention.RetentionSettings(**base)


# ---------------------------------------------------------------------------
# RetentionSettings
# ---------------------------------------------------------------------------


def test_from_settings_reads_every_key():
    settings = retention.RetentionSettings.from_settings(
        {
            "retention_raw_days": "7",
            "retention_aggregate_days": "100",
            "retention_incident_days": "200",
            "retention_load_test_raw_days": "30",
            "retention_diagnostics_days": "40",
        }
    )
    assert settings == retention.RetentionSettings(
        raw_days=7, aggregate_days=100, incident_days=200, load_test_raw_days=30, diagnostics_days=40
    )


def test_from_settings_falls_back_to_defaults_when_missing_or_invalid():
    settings = retention.RetentionSettings.from_settings(
        {"retention_raw_days": "not-a-number", "retention_incident_days": "-5"}
    )
    assert settings.raw_days == 14
    assert settings.incident_days == 730
    assert settings.aggregate_days == 365
    assert settings.load_test_raw_days == 90
    assert settings.diagnostics_days == 365


# ---------------------------------------------------------------------------
# run_retention: raw rows + aggregates
# ---------------------------------------------------------------------------


def test_pruned_buckets_are_aggregated_before_their_raw_rows_are_deleted(db_path):
    target = _target(db_path, "t1")
    _seed_raw(db_path, target)

    retention.run_retention(db_path, _settings(), now=NOW, targets=[target])

    daily = {
        r["bucket_start"]: r
        for r in quality_db.query_aggregates(db_path, "1d", to_iso_z(DAY0), to_iso_z(DAY2))
    }
    assert set(daily) == {to_iso_z(DAY0), to_iso_z(DAY1)}
    assert daily[to_iso_z(DAY0)]["attempts"] == 2
    assert daily[to_iso_z(DAY0)]["percentiles_from_raw"] == 0
    assert daily[to_iso_z(DAY1)]["percentiles_from_raw"] == 0

    hourly = {
        r["bucket_start"]: r
        for r in quality_db.query_aggregates(db_path, "1h", to_iso_z(DAY0), to_iso_z(DAY2))
    }
    assert hourly[to_iso_z(DAY0 + timedelta(hours=3))]["percentiles_from_raw"] == 0
    assert hourly[to_iso_z(DAY0 + timedelta(hours=15))]["percentiles_from_raw"] == 0
    assert hourly[to_iso_z(DAY1 + timedelta(hours=3))]["percentiles_from_raw"] == 0


def test_a_newer_preexisting_aggregate_keeps_percentiles_from_raw(db_path):
    target = _target(db_path, "t1")
    _seed_raw(db_path, target)
    quality_db.upsert_aggregate(
        db_path,
        {
            "target_id": target.id,
            "protocol": "icmp",
            "bucket": "1d",
            "bucket_start": to_iso_z(DAY2),
            "attempts": 2,
            "ok_count": 2,
            "timeout_count": 0,
            "error_count": 0,
            "percentiles_from_raw": 1,
            "computed_at": to_iso_z(DAY2),
        },
    )

    retention.run_retention(db_path, _settings(), now=NOW, targets=[target])

    row = quality_db.query_aggregates(db_path, "1d", to_iso_z(DAY2), to_iso_z(DAY2))[0]
    assert row["percentiles_from_raw"] == 1


def test_raw_rows_older_than_cutoff_are_deleted_newer_ones_untouched(db_path):
    target = _target(db_path, "t1")
    _seed_raw(db_path, target)

    counts = retention.run_retention(db_path, _settings(), now=NOW, targets=[target])

    assert counts["raw_deleted"] == 4  # DAY0 + DAY1, two rows each
    remaining = quality_db.query_probe_results(db_path, to_iso_z(DAY0), to_iso_z(NOW))
    assert len(remaining) == 2
    assert all(r["started_at"].startswith("2026-09-20") for r in remaining)


def test_batching_deletes_everything_eventually(db_path):
    targets = [_target(db_path, f"t{i}") for i in range(3)]
    for target in targets:
        _seed_raw(db_path, target)

    counts = retention.run_retention(db_path, _settings(batch_size=2), now=NOW, targets=targets)

    assert counts["raw_deleted"] == 12  # 3 targets x 4 pruned rows
    for target in targets:
        remaining = quality_db.query_probe_results(
            db_path, to_iso_z(DAY0), to_iso_z(NOW), target_id=target.id
        )
        assert len(remaining) == 2


# ---------------------------------------------------------------------------
# run_retention: everything else
# ---------------------------------------------------------------------------


def test_incidents_pruned_by_their_own_cutoff(db_path):
    target = _target(db_path, "t1")
    old_id = quality_db.insert_incident(
        db_path, target_id=target.id, protocol="icmp", kind="outage",
        started_at=to_iso_z(DAY0 - timedelta(days=2)), window_seconds=10, probe_interval_seconds=1.0,
    )
    new_id = quality_db.insert_incident(
        db_path, target_id=target.id, protocol="icmp", kind="outage",
        started_at=to_iso_z(DAY2), window_seconds=10, probe_interval_seconds=1.0,
    )

    counts = retention.run_retention(db_path, _settings(), now=NOW, targets=[target])

    assert counts["incidents_deleted"] == 1
    assert quality_db.get_incident(db_path, old_id) is None
    assert quality_db.get_incident(db_path, new_id) is not None


def test_diagnostics_pruned_by_their_own_cutoff(db_path):
    target = _target(db_path, "t1")
    quality_db.insert_diagnostic(
        db_path, tool="mtr", started_at=to_iso_z(DAY0 - timedelta(days=2)), status="ok",
    )
    quality_db.insert_diagnostic(db_path, tool="mtr", started_at=to_iso_z(DAY2), status="ok")

    counts = retention.run_retention(db_path, _settings(), now=NOW, targets=[target])

    assert counts["diagnostics_deleted"] == 1
    remaining = quality_db.query_diagnostics(db_path)
    assert len(remaining) == 1
    assert remaining[0]["started_at"] == to_iso_z(DAY2)


def test_load_test_raw_json_is_nulled_but_the_row_survives(db_path):
    target = _target(db_path, "t1")
    old_id = quality_db.insert_load_test(
        db_path, started_at=to_iso_z(DAY0 - timedelta(days=2)), kind="iperf_udp", direction="download",
        params_json="{}", status="ok", raw_json='{"raw": true}',
    )
    new_id = quality_db.insert_load_test(
        db_path, started_at=to_iso_z(DAY2), kind="iperf_udp", direction="download",
        params_json="{}", status="ok", raw_json='{"raw": true}',
    )

    counts = retention.run_retention(db_path, _settings(), now=NOW, targets=[target])

    assert counts["load_tests_raw_cleared"] == 1
    old_row = quality_db.get_load_test(db_path, old_id)
    new_row = quality_db.get_load_test(db_path, new_id)
    assert old_row is not None and old_row["raw_json"] is None
    assert new_row is not None and new_row["raw_json"] == '{"raw": true}'


def test_config_changes_pruned_by_incident_cutoff(db_path):
    target = _target(db_path, "t1")
    from speedtest_app.db import db_conn

    with db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO config_changes(changed_at, key, old_value, new_value, source) VALUES (?,?,?,?,?)",
            (to_iso_z(DAY0 - timedelta(days=2)), "old", None, "1", "env"),
        )
        conn.execute(
            "INSERT INTO config_changes(changed_at, key, old_value, new_value, source) VALUES (?,?,?,?,?)",
            (to_iso_z(DAY2), "new", None, "2", "env"),
        )

    counts = retention.run_retention(db_path, _settings(), now=NOW, targets=[target])

    assert counts["config_changes_deleted"] == 1
    # `ensure_db`'s own migration logs a `schema_version` row at real "now" (today),
    # which is newer than the cutoff and is correctly left alone alongside "new".
    remaining_keys = {
        r["key"]
        for r in quality_db.query_config_changes(db_path, to_iso_z(DAY0 - timedelta(days=5)), to_iso_z(NOW))
    }
    assert "old" not in remaining_keys
    assert "new" in remaining_keys


def test_closed_sessions_pruned_but_an_open_session_never_is(db_path):
    target = _target(db_path, "t1")
    old_closed = quality_db.start_session(db_path, "nas", "1.0", to_iso_z(DAY0 - timedelta(days=2)))
    quality_db.end_session(db_path, old_closed, to_iso_z(DAY0 - timedelta(days=2)), "shutdown")
    old_open = quality_db.start_session(db_path, "nas", "1.0", to_iso_z(DAY0 - timedelta(days=2)))
    new_closed = quality_db.start_session(db_path, "nas", "1.0", to_iso_z(DAY2))
    quality_db.end_session(db_path, new_closed, to_iso_z(DAY2), "shutdown")

    counts = retention.run_retention(db_path, _settings(), now=NOW, targets=[target])

    assert counts["sessions_deleted"] == 1
    sessions = quality_db.query_sessions(db_path, to_iso_z(DAY0 - timedelta(days=5)), to_iso_z(NOW))
    ids = {row["id"] for row in sessions}
    assert ids == {old_open, new_closed}


def test_probe_aggregates_older_than_aggregate_days_are_deleted(db_path):
    target = _target(db_path, "t1")
    ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)
    quality_db.upsert_aggregate(
        db_path,
        {
            "target_id": target.id,
            "protocol": "icmp",
            "bucket": "1d",
            "bucket_start": to_iso_z(ancient),
            "attempts": 1,
            "ok_count": 1,
            "timeout_count": 0,
            "error_count": 0,
            "percentiles_from_raw": 1,
            "computed_at": to_iso_z(ancient),
        },
    )

    counts = retention.run_retention(db_path, _settings(), now=NOW, targets=[target])

    assert counts["aggregates_deleted"] == 1
    assert quality_db.query_aggregates(db_path, "1d", to_iso_z(ancient), to_iso_z(ancient)) == []


def test_second_run_is_a_no_op(db_path):
    target = _target(db_path, "t1")
    _seed_raw(db_path, target)
    quality_db.insert_incident(
        db_path, target_id=target.id, protocol="icmp", kind="outage",
        started_at=to_iso_z(DAY0 - timedelta(days=2)), window_seconds=10, probe_interval_seconds=1.0,
    )
    settings = _settings()

    first = retention.run_retention(db_path, settings, now=NOW, targets=[target])
    assert first["raw_deleted"] > 0

    second = retention.run_retention(db_path, settings, now=NOW, targets=[target])

    assert second["raw_deleted"] == 0
    assert second["incidents_deleted"] == 0
    assert second["diagnostics_deleted"] == 0
    assert second["load_tests_raw_cleared"] == 0
    assert second["sessions_deleted"] == 0
    assert second["config_changes_deleted"] == 0
    assert second["aggregates_deleted"] == 0
    # the surviving rows and aggregates are unchanged
    remaining = quality_db.query_probe_results(db_path, to_iso_z(DAY0), to_iso_z(NOW))
    assert len(remaining) == 2


# ---------------------------------------------------------------------------
# estimate_growth
# ---------------------------------------------------------------------------


def test_estimate_growth_is_positive_and_scales_with_rows(db_path):
    target = _target(db_path, "t1")
    few = retention.estimate_growth(db_path, now=NOW)
    assert few["db_bytes"] > 0
    assert few["rows_24h"] == {"probe_results": 0, "probe_aggregates": 0, "incidents": 0}
    assert few["bytes_per_row_estimate"] == 0.0
    assert few["estimated_raw_bytes_per_day"] == 0.0
    assert few["estimated_raw_bytes_at_retention"] is None

    rows = [
        ProbeResult(
            target_id=target.id,
            protocol=target.protocol,
            started_at=to_iso_z(NOW - timedelta(hours=h)),
            duration_ms=1.0,
            outcome=Outcome.OK,
            timeout_ms=1000,
            rtt_ms=10.0,
        )
        for h in range(1, 200)
    ]
    quality_db.insert_probe_results(db_path, rows)

    grown = retention.estimate_growth(db_path, now=NOW, raw_days=14)

    assert grown["rows_24h"]["probe_results"] == 24  # h=1..24 fall within [now-24h, now]
    assert grown["bytes_per_row_estimate"] > 0
    assert grown["estimated_raw_bytes_per_day"] > 0
    assert grown["estimated_raw_bytes_at_retention"] == pytest.approx(
        grown["estimated_raw_bytes_per_day"] * 14
    )
