"""Hourly and daily aggregation from raw probe rows (design spec §6.4)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from speedtest_app import aggregates, quality_db, stats
from speedtest_app.probe_types import Outcome, ProbeResult, ProbeTarget, Protocol
from speedtest_app.time_utils import to_iso_z

DAY1 = datetime(2026, 9, 19, tzinfo=timezone.utc)
DAY2 = datetime(2026, 9, 20, tzinfo=timezone.utc)
DAY3 = datetime(2026, 9, 21, tzinfo=timezone.utc)
NOW_ISO = "2026-09-21T00:05:00Z"

#: (offset from DAY1, outcome, rtt) — hour 12 is deliberately empty.
RAW: tuple[tuple[timedelta, Outcome, float | None], ...] = (
    # hour 10: ten replies 10..100 ms and two lost ones in a row
    (timedelta(hours=10, seconds=0.5), Outcome.OK, 10.0),
    (timedelta(hours=10, minutes=5), Outcome.OK, 20.0),
    (timedelta(hours=10, minutes=10), Outcome.OK, 30.0),
    (timedelta(hours=10, minutes=15), Outcome.OK, 40.0),
    (timedelta(hours=10, minutes=20), Outcome.OK, 50.0),
    (timedelta(hours=10, minutes=25), Outcome.OK, 60.0),
    (timedelta(hours=10, minutes=30), Outcome.OK, 70.0),
    (timedelta(hours=10, minutes=35), Outcome.OK, 80.0),
    (timedelta(hours=10, minutes=40), Outcome.OK, 90.0),
    (timedelta(hours=10, minutes=45), Outcome.OK, 100.0),
    (timedelta(hours=10, minutes=50), Outcome.TIMEOUT, None),
    (timedelta(hours=10, minutes=55), Outcome.TIMEOUT, None),
    # hour 11: a quiet hour
    (timedelta(hours=11, minutes=0), Outcome.OK, 1.0),
    (timedelta(hours=11, minutes=10), Outcome.OK, 2.0),
    (timedelta(hours=11, minutes=20), Outcome.OK, 3.0),
    (timedelta(hours=11, minutes=30), Outcome.OK, 4.0),
    (timedelta(hours=11, minutes=40), Outcome.OK, 5.0),
    # hour 13: slow replies plus one error (never counted as loss)
    (timedelta(hours=13, minutes=5), Outcome.OK, 200.0),
    (timedelta(hours=13, minutes=10), Outcome.OK, 300.0),
    (timedelta(hours=13, minutes=15), Outcome.ERROR, None),
    (timedelta(hours=13, minutes=45), Outcome.OK, 400.0),
    # the next day
    (timedelta(days=1, hours=0, minutes=0), Outcome.OK, 7.0),
    (timedelta(days=1, hours=0, minutes=30), Outcome.OK, 8.0),
    (timedelta(days=1, hours=0, minutes=45), Outcome.OK, 9.0),
)

DAY1_OK_RTTS = sorted(rtt for delta, outcome, rtt in RAW if outcome is Outcome.OK and delta < timedelta(days=1))


def _target(db_path: str, name: str = "agg-target", protocol: Protocol = Protocol.ICMP) -> ProbeTarget:
    return quality_db.insert_target(
        db_path,
        name=name,
        kind="internet",
        protocol=protocol,
        host="1.1.1.1",
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=True,
    )


def _seed(db_path: str, target: ProbeTarget) -> None:
    rows = [
        ProbeResult(
            target_id=target.id,
            protocol=target.protocol,
            started_at=to_iso_z(DAY1 + delta),
            duration_ms=1.0,
            outcome=outcome,
            timeout_ms=1000,
            rtt_ms=rtt,
        )
        for delta, outcome, rtt in RAW
    ]
    assert quality_db.insert_probe_results(db_path, rows) == len(RAW)


def _by_start(rows: list[dict]) -> dict[str, dict]:
    return {row["bucket_start"]: row for row in rows}


# ---------------------------------------------------------------------------
# bucket boundaries
# ---------------------------------------------------------------------------

def test_bucket_start_and_end_are_utc_aligned():
    moment = datetime(2026, 9, 19, 13, 45, 12, 500000, tzinfo=timezone.utc)

    assert aggregates.bucket_start(moment, "1h") == datetime(2026, 9, 19, 13, tzinfo=timezone.utc)
    assert aggregates.bucket_end(moment, "1h") == datetime(2026, 9, 19, 14, tzinfo=timezone.utc)
    assert aggregates.bucket_start(moment, "1d") == DAY1
    assert aggregates.bucket_end(moment, "1d") == DAY2


def test_bucket_start_converts_other_zones_and_naive_times_to_utc():
    other_zone = datetime(2026, 9, 19, 1, 30, tzinfo=timezone(timedelta(hours=2)))  # 23:30 UTC, previous day

    assert aggregates.bucket_start(other_zone, "1h") == datetime(2026, 9, 18, 23, tzinfo=timezone.utc)
    assert aggregates.bucket_start(datetime(2026, 9, 19, 13, 45), "1h") == datetime(2026, 9, 19, 13, tzinfo=timezone.utc)


def test_unknown_bucket_is_rejected():
    with pytest.raises(ValueError):
        aggregates.bucket_start(DAY1, "1w")


# ---------------------------------------------------------------------------
# compute_bucket
# ---------------------------------------------------------------------------

def test_compute_bucket_without_rows_is_no_data():
    assert aggregates.compute_bucket([], 1, "icmp", "1h", DAY1, now_iso=NOW_ISO) is None


def test_compute_bucket_maps_stats_onto_aggregate_columns(db_path):
    target = _target(db_path)
    _seed(db_path, target)
    rows = quality_db.query_probe_results(db_path, to_iso_z(DAY1), to_iso_z(DAY3))

    row = aggregates.compute_bucket(rows, target.id, "icmp", "1h", DAY1 + timedelta(hours=10), now_iso=NOW_ISO)

    assert set(row) <= quality_db.AGGREGATE_COLUMNS
    assert row["target_id"] == target.id
    assert row["protocol"] == "icmp"
    assert row["bucket"] == "1h"
    assert row["bucket_start"] == to_iso_z(DAY1 + timedelta(hours=10))
    assert row["percentiles_from_raw"] == 1
    assert row["computed_at"] == NOW_ISO
    # the rows handed in are used as given (the caller selects the bucket)
    assert row["attempts"] == len(RAW)


# ---------------------------------------------------------------------------
# aggregate_range: hourly
# ---------------------------------------------------------------------------

def test_hourly_buckets_are_computed_from_raw_rows(db_path):
    target = _target(db_path)
    _seed(db_path, target)

    written = aggregates.aggregate_range(
        db_path, target.id, "icmp", "1h", DAY1 + timedelta(hours=10), DAY1 + timedelta(hours=14),
        now_iso=NOW_ISO,
    )

    assert written == 3  # hour 12 has no rows at all
    rows = _by_start(quality_db.query_aggregates(db_path, "1h", to_iso_z(DAY1), to_iso_z(DAY3)))
    assert sorted(rows) == [
        to_iso_z(DAY1 + timedelta(hours=10)),
        to_iso_z(DAY1 + timedelta(hours=11)),
        to_iso_z(DAY1 + timedelta(hours=13)),
    ]

    hour10 = rows[to_iso_z(DAY1 + timedelta(hours=10))]
    assert (hour10["attempts"], hour10["ok_count"], hour10["timeout_count"], hour10["error_count"]) == (12, 10, 2, 0)
    assert hour10["loss_pct"] == pytest.approx(2 / 12 * 100)
    assert hour10["rtt_min_ms"] == 10.0
    assert hour10["rtt_p50_ms"] == 50.0
    assert hour10["rtt_p95_ms"] == 100.0
    assert hour10["rtt_p99_ms"] == 100.0
    assert hour10["rtt_max_ms"] == 100.0
    assert hour10["rtt_mean_ms"] == pytest.approx(55.0)
    assert hour10["rtt_variation_ms"] == pytest.approx(10.0)
    assert hour10["longest_fail_streak"] == 2
    assert hour10["percentiles_from_raw"] == 1

    hour13 = rows[to_iso_z(DAY1 + timedelta(hours=13))]
    assert (hour13["attempts"], hour13["ok_count"], hour13["error_count"]) == (4, 3, 1)
    assert hour13["loss_pct"] == pytest.approx(0.0)  # an error is not a lost response
    assert hour13["rtt_p95_ms"] == 400.0


def test_empty_buckets_are_skipped_and_leave_no_row(db_path):
    target = _target(db_path)
    _seed(db_path, target)

    written = aggregates.aggregate_range(
        db_path, target.id, "icmp", "1h", DAY1 + timedelta(hours=12), DAY1 + timedelta(hours=13),
        now_iso=NOW_ISO,
    )

    assert written == 0
    assert quality_db.query_aggregates(db_path, "1h", to_iso_z(DAY1), to_iso_z(DAY3)) == []


def test_partial_bucket_is_excluded_unless_asked_for(db_path):
    target = _target(db_path)
    _seed(db_path, target)
    start = DAY1 + timedelta(hours=13)
    end = DAY1 + timedelta(hours=13, minutes=30)

    assert aggregates.aggregate_range(db_path, target.id, "icmp", "1h", start, end, now_iso=NOW_ISO) == 0
    assert quality_db.query_aggregates(db_path, "1h", to_iso_z(DAY1), to_iso_z(DAY3)) == []

    written = aggregates.aggregate_range(
        db_path, target.id, "icmp", "1h", start, end, now_iso=NOW_ISO, include_partial=True,
    )

    assert written == 1
    row = quality_db.query_aggregates(db_path, "1h", to_iso_z(DAY1), to_iso_z(DAY3))[0]
    # only the rows up to 13:30 — the 13:45 reply is not in the requested range
    assert (row["attempts"], row["ok_count"], row["error_count"]) == (3, 2, 1)
    assert row["rtt_max_ms"] == 300.0


def test_only_the_requested_target_and_protocol_are_aggregated(db_path):
    target = _target(db_path)
    other = _target(db_path, name="other-target", protocol=Protocol.TCP)
    _seed(db_path, target)
    _seed(db_path, other)

    aggregates.aggregate_range(
        db_path, target.id, "icmp", "1d", DAY1, DAY2, now_iso=NOW_ISO,
    )

    rows = quality_db.query_aggregates(db_path, "1d", to_iso_z(DAY1), to_iso_z(DAY3))
    assert len(rows) == 1
    assert rows[0]["target_id"] == target.id
    assert rows[0]["attempts"] == 21


# ---------------------------------------------------------------------------
# aggregate_range: daily
# ---------------------------------------------------------------------------

def test_daily_bucket_is_built_from_raw_rows_not_from_hourly_percentiles(db_path):
    target = _target(db_path)
    _seed(db_path, target)
    aggregates.aggregate_range(
        db_path, target.id, "icmp", "1h", DAY1, DAY2, now_iso=NOW_ISO,
    )
    hourly_p95 = [r["rtt_p95_ms"] for r in quality_db.query_aggregates(db_path, "1h", to_iso_z(DAY1), to_iso_z(DAY2))]

    written = aggregates.aggregate_range(db_path, target.id, "icmp", "1d", DAY1, DAY3, now_iso=NOW_ISO)

    assert written == 2
    rows = _by_start(quality_db.query_aggregates(db_path, "1d", to_iso_z(DAY1), to_iso_z(DAY3)))
    day1 = rows[to_iso_z(DAY1)]
    assert (day1["attempts"], day1["ok_count"], day1["timeout_count"], day1["error_count"]) == (21, 18, 2, 1)
    assert day1["loss_pct"] == pytest.approx(2 / 20 * 100)  # pooled from counters
    assert day1["rtt_p50_ms"] == stats.percentile(DAY1_OK_RTTS, 50) == 40.0
    assert day1["rtt_p95_ms"] == stats.percentile(DAY1_OK_RTTS, 95) == 400.0
    assert day1["rtt_p99_ms"] == stats.percentile(DAY1_OK_RTTS, 99) == 400.0
    assert day1["rtt_min_ms"] == 1.0 and day1["rtt_max_ms"] == 400.0
    # averaging the hourly percentiles would have produced something else entirely
    assert day1["rtt_p95_ms"] != pytest.approx(sum(hourly_p95) / len(hourly_p95))

    day2 = rows[to_iso_z(DAY2)]
    assert (day2["attempts"], day2["ok_count"]) == (3, 3)
    assert day2["rtt_max_ms"] == 9.0


def test_rerunning_the_aggregation_upserts_instead_of_duplicating(db_path):
    target = _target(db_path)
    _seed(db_path, target)

    first = aggregates.aggregate_range(db_path, target.id, "icmp", "1d", DAY1, DAY3, now_iso=NOW_ISO)
    again = aggregates.aggregate_range(
        db_path, target.id, "icmp", "1d", DAY1, DAY3, now_iso="2026-09-21T06:00:00Z",
    )

    assert first == again == 2
    rows = quality_db.query_aggregates(db_path, "1d", to_iso_z(DAY1), to_iso_z(DAY3))
    assert len(rows) == 2
    assert {r["computed_at"] for r in rows} == {"2026-09-21T06:00:00Z"}
    assert rows[0]["attempts"] == 21


def test_a_range_starting_mid_bucket_still_aggregates_the_whole_bucket(db_path):
    target = _target(db_path)
    _seed(db_path, target)

    written = aggregates.aggregate_range(
        db_path, target.id, "icmp", "1h", DAY1 + timedelta(hours=10, minutes=30), DAY1 + timedelta(hours=11),
        now_iso=NOW_ISO,
    )

    assert written == 1
    row = quality_db.query_aggregates(db_path, "1h", to_iso_z(DAY1), to_iso_z(DAY3))[0]
    assert row["bucket_start"] == to_iso_z(DAY1 + timedelta(hours=10))
    assert row["attempts"] == 12  # the whole hour, not just its second half


# ---------------------------------------------------------------------------
# aggregate_all
# ---------------------------------------------------------------------------

def test_aggregate_all_covers_both_buckets_for_every_target(db_path):
    first = _target(db_path)
    second = _target(db_path, name="tcp-target", protocol=Protocol.TCP)
    _seed(db_path, first)
    _seed(db_path, second)

    written = aggregates.aggregate_all(db_path, [first, second], DAY1, DAY3, now_iso=NOW_ISO)

    assert written == {"1h": 8, "1d": 4}  # 3 hours + 1 hour next day, 2 days, per target
    hourly = quality_db.query_aggregates(db_path, "1h", to_iso_z(DAY1), to_iso_z(DAY3))
    assert {r["protocol"] for r in hourly} == {"icmp", "tcp"}
    assert {r["target_id"] for r in hourly} == {first.id, second.id}
