"""Pure statistics over probe rows (design spec §6)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from speedtest_app import stats
from speedtest_app.probe_types import Outcome, ProbeResult, Protocol
from speedtest_app.time_utils import to_iso_z

BASE = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)


def _at(offset_seconds: float) -> str:
    return to_iso_z(BASE + timedelta(seconds=offset_seconds))


def _row(offset_seconds: float, outcome: Outcome, rtt_ms: float | None = None) -> ProbeResult:
    return ProbeResult(
        target_id=1,
        protocol=Protocol.ICMP,
        started_at=_at(offset_seconds),
        duration_ms=1.0,
        outcome=outcome,
        timeout_ms=1000,
        rtt_ms=rtt_ms,
    )


def _ok(offset_seconds: float, rtt_ms: float) -> ProbeResult:
    return _row(offset_seconds, Outcome.OK, rtt_ms)


def _timeout(offset_seconds: float) -> ProbeResult:
    return _row(offset_seconds, Outcome.TIMEOUT)


def _error(offset_seconds: float) -> ProbeResult:
    return _row(offset_seconds, Outcome.ERROR)


# ---------------------------------------------------------------------------
# counters and loss
# ---------------------------------------------------------------------------

def test_counters_and_loss_exclude_errors_but_count_them():
    rows = [_ok(i, 10.0 + i) for i in range(6)]
    rows += [_timeout(10), _timeout(11)]
    rows += [_error(20), _error(21)]

    result = stats.compute_stats(rows)

    assert (result.attempts, result.ok, result.timeouts, result.errors) == (10, 6, 2, 2)
    # errors are "no measurement": 2/(6+2), not 2/10 and not 4/10
    assert result.loss_pct == pytest.approx(25.0)
    assert result.first_at == _at(0)
    assert result.last_at == _at(21)


def test_loss_is_none_when_no_ok_and_no_timeout():
    result = stats.compute_stats([_error(0), _error(1), _error(2)])

    assert (result.attempts, result.ok, result.timeouts, result.errors) == (3, 0, 0, 3)
    assert result.loss_pct is None
    assert result.rtt_p95_ms is None
    assert result.longest_fail_streak == 0


def test_empty_window_is_no_data_not_zero_loss():
    result = stats.compute_stats([])

    assert result.attempts == 0
    assert result.loss_pct is None
    assert result.rtt_mean_ms is None
    assert result.rtt_variation_ms is None
    assert result.longest_fail_streak == 0
    assert result.first_at is None and result.last_at is None


def test_full_loss_is_hundred_percent():
    result = stats.compute_stats([_timeout(i) for i in range(5)])

    assert result.loss_pct == pytest.approx(100.0)
    assert result.longest_fail_streak == 5
    assert result.rtt_min_ms is None


def test_mapping_rows_are_accepted_like_dataclasses():
    rows = [_ok(0, 10.0), _timeout(1), _ok(2, 30.0)]
    mappings = [r.to_row() for r in rows]

    assert stats.compute_stats(mappings) == stats.compute_stats(rows)


# ---------------------------------------------------------------------------
# percentiles
# ---------------------------------------------------------------------------

def test_percentile_is_nearest_rank_never_interpolated():
    assert stats.percentile([5.0], 50) == 5.0
    assert stats.percentile([5.0], 99) == 5.0

    two = [10.0, 20.0]
    assert stats.percentile(two, 50) == 10.0
    assert stats.percentile(two, 95) == 20.0
    assert stats.percentile(two, 99) == 20.0

    twenty = [float(i) for i in range(1, 21)]
    assert stats.percentile(twenty, 50) == 10.0
    assert stats.percentile(twenty, 95) == 19.0
    assert stats.percentile(twenty, 99) == 20.0

    hundred = [float(i) for i in range(1, 101)]
    assert stats.percentile(hundred, 0) == 1.0
    assert stats.percentile(hundred, 50) == 50.0  # not the 50.5 of an interpolating method
    assert stats.percentile(hundred, 95) == 95.0
    assert stats.percentile(hundred, 99) == 99.0
    assert stats.percentile(hundred, 100) == 100.0


def test_percentile_rejects_empty_input():
    with pytest.raises(ValueError):
        stats.percentile([], 95)


def test_compute_stats_percentiles_over_ok_rows_only():
    rows = [_ok(i, float(i + 1)) for i in range(100)]
    rows += [_timeout(200), _error(201)]

    result = stats.compute_stats(rows)

    assert result.rtt_min_ms == 1.0
    assert result.rtt_p50_ms == 50.0
    assert result.rtt_p95_ms == 95.0
    assert result.rtt_p99_ms == 99.0
    assert result.rtt_max_ms == 100.0
    assert result.rtt_mean_ms == pytest.approx(50.5)
    assert result.rtt_spread_ms == pytest.approx(45.0)  # p95 - p50


def test_min_samples_leaves_rtt_none_but_keeps_counters():
    rows = [_ok(0, 10.0), _ok(1, 20.0), _ok(2, 30.0), _timeout(3)]

    result = stats.compute_stats(rows, min_samples=5)

    assert (result.attempts, result.ok, result.timeouts) == (4, 3, 1)
    assert result.loss_pct == pytest.approx(25.0)
    assert result.rtt_min_ms is None
    assert result.rtt_p50_ms is None
    assert result.rtt_mean_ms is None
    assert result.rtt_variation_ms is None
    assert result.rtt_spread_ms is None
    assert stats.compute_stats(rows, min_samples=3).rtt_p50_ms == 20.0


# ---------------------------------------------------------------------------
# variation, spread, streaks
# ---------------------------------------------------------------------------

def test_variation_skips_non_ok_rows_between_samples():
    rows = [_ok(0, 10.0), _timeout(1), _error(2), _ok(3, 30.0), _ok(4, 35.0)]

    result = stats.compute_stats(rows)

    # |30-10| = 20, |35-30| = 5 -> mean 12.5
    assert result.rtt_variation_ms == pytest.approx(12.5)


def test_variation_sees_a_spike_a_mean_would_hide():
    calm = [_ok(i, 20.0) for i in range(10)]
    spiky = [_ok(0, 20.0), _ok(1, 220.0), _ok(2, 20.0), _ok(3, 220.0)]

    assert stats.compute_stats(calm).rtt_variation_ms == pytest.approx(0.0)
    assert stats.compute_stats(spiky).rtt_variation_ms == pytest.approx(200.0)


def test_variation_needs_two_ok_samples():
    assert stats.compute_stats([_ok(0, 10.0), _timeout(1)]).rtt_variation_ms is None


def test_variation_follows_time_order_not_input_order():
    ordered = stats.compute_stats([_ok(0, 10.0), _ok(1, 30.0), _ok(2, 35.0)])
    shuffled = stats.compute_stats([_ok(2, 35.0), _ok(0, 10.0), _ok(1, 30.0)])

    assert ordered.rtt_variation_ms == shuffled.rtt_variation_ms == pytest.approx(12.5)


def test_longest_fail_streak_ignores_error_rows():
    rows = [
        _ok(0, 10.0),
        _timeout(1),
        _error(2),
        _timeout(3),
        _timeout(4),
        _ok(5, 10.0),
        _timeout(6),
    ]

    # the error row is skipped, so the run is timeout, timeout, timeout
    assert stats.compute_stats(rows).longest_fail_streak == 3


def test_longest_fail_streak_is_broken_by_ok_rows():
    rows = [_timeout(0), _timeout(1), _ok(2, 10.0), _timeout(3)]

    assert stats.compute_stats(rows).longest_fail_streak == 2


def test_as_dict_exposes_every_field():
    result = stats.compute_stats([_ok(0, 10.0), _ok(1, 20.0)])
    data = result.as_dict()

    assert data["attempts"] == 2
    assert data["rtt_mean_ms"] == pytest.approx(15.0)
    assert set(data) == {
        "attempts", "ok", "timeouts", "errors", "loss_pct",
        "rtt_min_ms", "rtt_p50_ms", "rtt_p95_ms", "rtt_p99_ms", "rtt_max_ms",
        "rtt_mean_ms", "rtt_variation_ms", "rtt_spread_ms",
        "longest_fail_streak", "first_at", "last_at",
    }


# ---------------------------------------------------------------------------
# merge_counters
# ---------------------------------------------------------------------------

def test_merge_counters_pools_loss_instead_of_averaging_percentages():
    heavy = stats.compute_stats([_ok(i, 10.0) for i in range(50)] + [_timeout(100 + i) for i in range(50)])
    light = stats.compute_stats([_ok(200 + i, 10.0) for i in range(10)])

    assert heavy.loss_pct == pytest.approx(50.0)
    assert light.loss_pct == pytest.approx(0.0)

    merged = stats.merge_counters([heavy, light])

    assert (merged.attempts, merged.ok, merged.timeouts) == (110, 60, 50)
    # pooled: 50 / (60 + 50) = 45.4545...%, not (50 + 0) / 2 = 25%
    assert merged.loss_pct == pytest.approx(50 / 110 * 100)
    assert merged.loss_pct == pytest.approx(45.4545454545, abs=1e-6)
    assert merged.loss_pct != pytest.approx(25.0)


def test_merge_counters_drops_rtt_and_keeps_the_longest_streak():
    first = stats.compute_stats([_ok(0, 10.0), _timeout(1), _timeout(2)])
    second = stats.compute_stats([_timeout(10), _timeout(11), _timeout(12), _timeout(13)])

    merged = stats.merge_counters([first, second])

    assert merged.rtt_min_ms is None
    assert merged.rtt_p50_ms is None
    assert merged.rtt_p95_ms is None
    assert merged.rtt_p99_ms is None
    assert merged.rtt_max_ms is None
    assert merged.rtt_mean_ms is None
    assert merged.rtt_variation_ms is None
    assert merged.rtt_spread_ms is None
    # cross-boundary streaks are not joined: max of the parts, not 2 + 4
    assert merged.longest_fail_streak == 4
    assert merged.first_at == _at(0)
    assert merged.last_at == _at(13)


def test_merge_counters_of_nothing_is_empty():
    merged = stats.merge_counters([])

    assert merged.attempts == 0
    assert merged.loss_pct is None
    assert merged.first_at is None


def test_merge_counters_keeps_loss_none_when_nothing_was_measurable():
    only_errors = stats.compute_stats([_error(0), _error(1)])

    assert stats.merge_counters([only_errors, only_errors]).loss_pct is None


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------

def test_window_is_half_open_on_the_right():
    rows = [_ok(0, 10.0), _ok(10, 10.0), _ok(15, 10.0), _timeout(20)]

    result = stats.window(rows, 10.0, BASE + timedelta(seconds=20))

    # [10 s, 20 s): the row at 20 s belongs to the next window, the one at 0 s to the previous
    assert result.attempts == 2
    assert result.first_at == _at(10)
    assert result.last_at == _at(15)


def test_window_without_rows_reports_no_data():
    result = stats.window([_ok(0, 10.0)], 10.0, BASE + timedelta(seconds=60))

    assert result.attempts == 0
    assert result.loss_pct is None


def test_tumbling_windows_keep_empty_windows_and_drop_the_partial_tail():
    rows = [_ok(1, 10.0), _ok(2, 12.0), _timeout(21), _timeout(22)]

    windows = stats.tumbling_windows(rows, 10.0, BASE, BASE + timedelta(seconds=35))

    assert [(w[0], w[1]) for w in windows] == [
        (BASE, BASE + timedelta(seconds=10)),
        (BASE + timedelta(seconds=10), BASE + timedelta(seconds=20)),
        (BASE + timedelta(seconds=20), BASE + timedelta(seconds=30)),
    ]
    assert [w[2].attempts for w in windows] == [2, 0, 2]
    assert windows[1][2].loss_pct is None  # no data, not 0 % loss
    assert windows[2][2].loss_pct == pytest.approx(100.0)


def test_tumbling_windows_are_aligned_to_start_at():
    start = BASE + timedelta(seconds=3)
    windows = stats.tumbling_windows([], 10.0, start, start + timedelta(seconds=20))

    assert [w[0] for w in windows] == [start, start + timedelta(seconds=10)]


def test_tumbling_windows_of_an_empty_range_is_empty():
    assert stats.tumbling_windows([], 10.0, BASE, BASE) == []


# ---------------------------------------------------------------------------
# timeline buckets
# ---------------------------------------------------------------------------

def test_bucket_rows_shape_and_alignment():
    rows = [_ok(0, 10.0), _ok(1, 30.0), _timeout(2), _error(3), _ok(61, 50.0)]

    buckets = stats.bucket_rows(rows, 60.0, BASE, BASE + timedelta(seconds=180))

    assert [b["t"] for b in buckets] == [_at(0), _at(60), _at(120)]
    assert set(buckets[0]) == {
        "t", "attempts", "ok", "timeouts", "errors", "loss_pct", "p50", "p95", "max", "partial",
    }
    first = buckets[0]
    assert (first["attempts"], first["ok"], first["timeouts"], first["errors"]) == (4, 2, 1, 1)
    assert first["loss_pct"] == pytest.approx(1 / 3 * 100)
    assert first["p50"] == 10.0
    assert first["p95"] == 30.0
    assert first["max"] == 30.0
    assert buckets[1]["attempts"] == 1
    assert buckets[2] == {
        "t": _at(120), "attempts": 0, "ok": 0, "timeouts": 0, "errors": 0,
        "loss_pct": None, "p50": None, "p95": None, "max": None, "partial": False,
    }


def test_bucket_rows_partial_tail_closes_the_range():
    """`include_partial` accounts for the tail, so buckets sum to the range."""
    rows = [_ok(0, 10.0), _ok(59, 20.0), _ok(60, 30.0), _ok(150, 40.0), _ok(180, 50.0)]
    start, end = BASE, BASE + timedelta(seconds=180)

    complete = stats.bucket_rows(rows, 60.0, start, end)
    assert [b["t"] for b in complete] == [_at(0), _at(60), _at(120)]
    assert [b["partial"] for b in complete] == [False, False, False]
    # the row exactly at `end` falls outside the half-open windows
    assert sum(b["attempts"] for b in complete) == 4

    with_tail = stats.bucket_rows(rows, 60.0, start, end, include_partial=True)
    assert [b["t"] for b in with_tail] == [_at(0), _at(60), _at(120), _at(180)]
    assert [b["partial"] for b in with_tail] == [False, False, False, True]
    # every attempt of the closed range `[start, end]` is counted exactly once
    assert sum(b["attempts"] for b in with_tail) == len(rows)
    assert with_tail[-1]["attempts"] == 1
    assert with_tail[-1]["p50"] == 50.0
    assert with_tail[:3] == complete


def test_bucket_rows_partial_tail_holds_an_unfinished_bucket():
    rows = [_ok(0, 10.0), _ok(70, 20.0), _ok(95, 30.0)]
    start, end = BASE, BASE + timedelta(seconds=100)

    buckets = stats.bucket_rows(rows, 60.0, start, end, include_partial=True)

    assert [b["t"] for b in buckets] == [_at(0), _at(60)]
    assert [b["partial"] for b in buckets] == [False, True]
    assert buckets[1]["attempts"] == 2  # 70 s and 95 s, both inside [60, 100]
    assert sum(b["attempts"] for b in buckets) == 3


def test_bucket_rows_without_a_range_has_no_partial_tail():
    rows = [_ok(0, 10.0)]
    assert stats.bucket_rows(rows, 60.0, BASE, BASE, include_partial=True) == [
        {
            "t": _at(0), "attempts": 1, "ok": 1, "timeouts": 0, "errors": 0,
            "loss_pct": 0.0, "p50": 10.0, "p95": 10.0, "max": 10.0, "partial": True,
        }
    ]


def test_a_zero_width_tail_is_emitted_only_for_a_row_sitting_on_the_end():
    """The closing point exists to carry `end_at` itself, not to pad the chart.

    `query_probe_results` is inclusive on both ends, so a row stamped exactly
    `end_at` belongs to no complete window and would be lost without the
    partial point — the buckets would stop summing to the statistics. With no
    such row the zero-width point would say "no data" about no time at all,
    so it is left out.
    """
    end = BASE + timedelta(seconds=120)
    inside = [_ok(5, 10.0)]
    on_the_edge = [_ok(5, 10.0), _ok(120, 11.0)]

    without = stats.bucket_rows(inside, 10.0, BASE, end, include_partial=True)
    assert len(without) == 12
    assert [point["partial"] for point in without] == [False] * 12
    assert sum(point["attempts"] for point in without) == 1

    with_edge = stats.bucket_rows(on_the_edge, 10.0, BASE, end, include_partial=True)
    assert len(with_edge) == 13
    assert with_edge[-1]["partial"] is True
    assert with_edge[-1]["attempts"] == 1
    assert sum(point["attempts"] for point in with_edge) == 2

    # a tail with real width is still emitted, empty or not
    ragged = stats.bucket_rows(inside, 10.0, BASE, end + timedelta(seconds=3), include_partial=True)
    assert ragged[-1]["partial"] is True
    assert ragged[-1]["attempts"] == 0


def test_bucket_rows_parses_each_timestamp_once(monkeypatch):
    """One `parse_dt` per row, not three (timeline/stats hot path).

    `bucket_rows` used to call `_timed` over the whole range, then again
    inside every bucket's `compute_stats`, then a third time to pick the
    trailing partial bucket — so a 24 h timeline parsed and re-sorted 86 400
    timestamps per target three times over. The rows already arrive ordered
    from `ORDER BY started_at ASC, id ASC`; re-deriving that order for every
    bucket was the bulk of the endpoint's CPU time.
    """
    calls = 0
    real = stats.parse_dt

    def counting(value):
        nonlocal calls
        calls += 1
        return real(value)

    monkeypatch.setattr(stats, "parse_dt", counting)

    rows = [_row(i, Outcome.OK, rtt_ms=float(i)) for i in range(120)]
    points = stats.bucket_rows(rows, 30.0, BASE, BASE + timedelta(seconds=120), include_partial=True)

    assert sum(point["attempts"] for point in points) == len(rows)
    assert calls == len(rows), f"expected one parse per row, got {calls}"


def test_bucket_rows_still_orders_unsorted_input():
    """Reordering stays the caller's guarantee, not the caller's duty."""
    rows = [_row(90, Outcome.OK, rtt_ms=9.0), _row(5, Outcome.TIMEOUT), _row(35, Outcome.OK, rtt_ms=1.0)]
    points = stats.bucket_rows(rows, 30.0, BASE, BASE + timedelta(seconds=120))

    assert [point["attempts"] for point in points] == [1, 1, 0, 1]
    assert stats.compute_stats(rows).first_at == _at(5)
    assert stats.compute_stats(rows).last_at == _at(90)
