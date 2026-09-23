"""Pure statistics over probe rows (design spec §6).

Every function here is a pure function of its arguments: no database, no clock,
no network. Rows may be :class:`~speedtest_app.probe_types.ProbeResult`
instances or mappings straight from ``quality_db.query_probe_results``.

The measurement rules of §1 are encoded here and nowhere else:

* loss is computed only from ``ok`` + ``timeout`` attempts — ``error`` attempts
  are "no measurement", counted and reported separately, never as loss and
  never as success;
* an empty window is *no data* (``attempts == 0``, ``loss_pct is None``), never
  "0 % loss";
* percentiles use nearest-rank on raw samples, are never interpolated and are
  never averaged across sub-periods;
* loss over several periods is re-derived from summed counters
  (:func:`merge_counters`), never averaged as percentages.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import fsum
from collections.abc import Mapping
from typing import Any, Iterable, Sequence

from .time_utils import parse_dt, to_iso_z

#: Sort key for rows whose ``started_at`` is missing or unparsable.
_UNKNOWN_TS = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class ProbeStats:
    """Counters and RTT statistics of a set of attempts (spec §6).

    ``None`` always means "not measurable from these rows", never zero:
    ``loss_pct`` is ``None`` without a single ``ok``/``timeout`` attempt and
    the ``rtt_*`` fields are ``None`` below ``min_samples`` replies.
    """

    attempts: int = 0
    ok: int = 0
    timeouts: int = 0
    errors: int = 0
    loss_pct: float | None = None
    rtt_min_ms: float | None = None
    rtt_p50_ms: float | None = None
    rtt_p95_ms: float | None = None
    rtt_p99_ms: float | None = None
    rtt_max_ms: float | None = None
    rtt_mean_ms: float | None = None
    rtt_variation_ms: float | None = None
    rtt_spread_ms: float | None = None
    longest_fail_streak: int = 0
    first_at: str | None = None
    last_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Field name -> value, in the order of the spec's table."""
        return asdict(self)


def _field(row: Any, name: str) -> Any:
    """Read ``name`` from a mapping row or from a dataclass row.

    ``dict`` is tried first: rows come from ``sqlite3`` as plain dicts by the
    hundreds of thousands, and an ``isinstance`` against the abstract
    ``Mapping`` costs a full ABC subclass check every time.
    """
    if type(row) is dict:
        return row.get(name)
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_dt(value)
    except ValueError:
        return None


def _as_utc(dt: datetime) -> datetime:
    """Naive datetimes are read as UTC; aware ones are converted."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


#: A row paired with its parsed ``started_at`` (``None`` when unparsable).
Timed = tuple[datetime | None, Any]


def _timed(rows: Iterable[Any]) -> list[Timed]:
    """Rows paired with their parsed ``started_at``, in chronological order.

    Call this **once** per row set and pass the result down: the internal
    ``_from_timed`` helpers take these pairs so a bucketed view parses and
    sorts each timestamp a single time, however many buckets it splits into.
    """
    pairs = [(_parse_ts(_field(row, "started_at")), row) for row in rows]
    pairs.sort(key=lambda pair: pair[0] or _UNKNOWN_TS)
    return pairs


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile: ``idx = ceil(p / 100 * n) - 1`` clamped to the list.

    The sample is returned as measured — never interpolated between two
    samples and never derived from the percentiles of sub-periods. ``p`` is in
    percent; the product is formed as ``p * n / 100`` so that whole-percent
    ranks stay exact in binary floating point.
    """
    n = len(sorted_values)
    if n == 0:
        raise ValueError("percentile() needs at least one value")
    index = math.ceil(p * n / 100.0) - 1
    return sorted_values[min(max(index, 0), n - 1)]


def compute_stats(rows: Iterable[Any], *, min_samples: int = 1) -> ProbeStats:
    """Counters, loss and RTT statistics of ``rows`` (spec §6).

    ``min_samples`` is the number of replies below which the ``rtt_*`` fields
    stay ``None`` (1 for reporting; incident thresholds pass their own value).
    Counters and ``loss_pct`` are always reported, whatever ``min_samples`` is.
    """
    return _stats_from_timed(_timed(rows), min_samples=min_samples)


def _stats_from_timed(ordered: Sequence[Timed], *, min_samples: int = 1) -> ProbeStats:
    """:func:`compute_stats` over rows already paired with their timestamps.

    ``ordered`` must be chronological — :func:`_timed` output, or a contiguous
    slice of it. Taking pairs instead of raw rows is what lets a bucketed view
    parse each ``started_at`` once for the whole range.
    """
    attempts = len(ordered)
    ok = timeouts = errors = 0
    ok_rtts: list[float] = []
    streak = longest_fail_streak = 0

    for _ts, row in ordered:
        outcome = _field(row, "outcome")
        outcome = str(outcome) if outcome is not None else ""
        if outcome == "ok":
            ok += 1
            rtt = _field(row, "rtt_ms")
            if rtt is not None:
                ok_rtts.append(float(rtt))
            streak = 0
        elif outcome == "timeout":
            timeouts += 1
            streak += 1
            longest_fail_streak = max(longest_fail_streak, streak)
        else:
            # `error` attempts produced no measurement: they neither break nor
            # extend a run of unanswered probes, and they are never loss.
            errors += 1

    measurable = ok + timeouts
    loss_pct = (timeouts / measurable * 100.0) if measurable else None

    rtt_min = rtt_p50 = rtt_p95 = rtt_p99 = rtt_max = rtt_mean = None
    rtt_variation = rtt_spread = None
    if ok_rtts and len(ok_rtts) >= min_samples:
        ordered_rtts = sorted(ok_rtts)
        rtt_min = ordered_rtts[0]
        rtt_max = ordered_rtts[-1]
        rtt_p50 = percentile(ordered_rtts, 50)
        rtt_p95 = percentile(ordered_rtts, 95)
        rtt_p99 = percentile(ordered_rtts, 99)
        rtt_mean = fsum(ordered_rtts) / len(ordered_rtts)
        rtt_spread = rtt_p95 - rtt_p50
        if len(ok_rtts) >= 2:
            # consecutive replies in time order; non-ok attempts in between are
            # skipped, so this is variability of the replies we did measure.
            deltas = [abs(b - a) for a, b in zip(ok_rtts, ok_rtts[1:])]
            rtt_variation = fsum(deltas) / len(deltas)

    return ProbeStats(
        attempts=attempts,
        ok=ok,
        timeouts=timeouts,
        errors=errors,
        loss_pct=loss_pct,
        rtt_min_ms=rtt_min,
        rtt_p50_ms=rtt_p50,
        rtt_p95_ms=rtt_p95,
        rtt_p99_ms=rtt_p99,
        rtt_max_ms=rtt_max,
        rtt_mean_ms=rtt_mean,
        rtt_variation_ms=rtt_variation,
        rtt_spread_ms=rtt_spread,
        longest_fail_streak=longest_fail_streak,
        first_at=_field(ordered[0][1], "started_at") if ordered else None,
        last_at=_field(ordered[-1][1], "started_at") if ordered else None,
    )


def merge_counters(parts: list[ProbeStats]) -> ProbeStats:
    """Pool several periods: counters are summed, loss is re-derived from them.

    Percentiles cannot be pooled without the raw samples, so every ``rtt_*``
    field (including ``rtt_variation_ms`` and ``rtt_spread_ms``) is ``None``
    here — recompute from raw rows when you need them.
    ``longest_fail_streak`` is the longest streak of any part: a streak running
    across a period boundary is **not** joined, so the merged value is a lower
    bound.
    """
    attempts = sum(part.attempts for part in parts)
    ok = sum(part.ok for part in parts)
    timeouts = sum(part.timeouts for part in parts)
    errors = sum(part.errors for part in parts)
    measurable = ok + timeouts

    starts = [part.first_at for part in parts if part.first_at is not None]
    ends = [part.last_at for part in parts if part.last_at is not None]

    return ProbeStats(
        attempts=attempts,
        ok=ok,
        timeouts=timeouts,
        errors=errors,
        loss_pct=(timeouts / measurable * 100.0) if measurable else None,
        longest_fail_streak=max((part.longest_fail_streak for part in parts), default=0),
        first_at=min(starts, key=lambda value: _parse_ts(value) or _UNKNOWN_TS) if starts else None,
        last_at=max(ends, key=lambda value: _parse_ts(value) or _UNKNOWN_TS) if ends else None,
    )


def window(
    rows: Iterable[Any],
    window_seconds: float,
    end_at: datetime,
    *,
    min_samples: int = 1,
) -> ProbeStats:
    """Statistics of the half-open window ``[end_at - window_seconds, end_at)``."""
    end = _as_utc(end_at)
    start = end - timedelta(seconds=window_seconds)
    selected = [pair for pair in _timed(rows) if pair[0] is not None and start <= pair[0] < end]
    return _stats_from_timed(selected, min_samples=min_samples)


def tumbling_windows(
    rows: Iterable[Any],
    window_seconds: float,
    start_at: datetime,
    end_at: datetime,
    *,
    min_samples: int = 1,
) -> list[tuple[datetime, datetime, ProbeStats]]:
    """Consecutive half-open windows aligned to ``start_at``.

    Windows without a single attempt are returned with ``attempts == 0`` (no
    data) instead of being dropped. A trailing window that does not fit in
    ``[start_at, end_at)`` completely is **excluded**: a partial window would
    otherwise look like a quiet one.
    """
    return _tumbling_from_timed(
        _timed(rows), window_seconds, start_at, end_at, min_samples=min_samples
    )


def _tumbling_from_timed(
    ordered: Sequence[Timed],
    window_seconds: float,
    start_at: datetime,
    end_at: datetime,
    *,
    min_samples: int = 1,
) -> list[tuple[datetime, datetime, ProbeStats]]:
    """:func:`tumbling_windows` over rows already paired with their timestamps.

    Each bucket keeps the pairs, so the slice handed to ``_stats_from_timed``
    is already chronological and no bucket re-parses or re-sorts what the one
    ``_timed`` call at the top of the range has done once.
    """
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    start = _as_utc(start_at)
    end = _as_utc(end_at)
    width = timedelta(seconds=window_seconds)
    count = max(int((end - start) // width), 0)

    buckets: list[list[Timed]] = [[] for _ in range(count)]
    for pair in ordered:
        ts = pair[0]
        if ts is None or ts < start:
            continue
        index = int((ts - start) // width)
        if 0 <= index < count:
            buckets[index].append(pair)

    result: list[tuple[datetime, datetime, ProbeStats]] = []
    for index, bucket in enumerate(buckets):
        window_start = start + width * index
        result.append(
            (window_start, window_start + width, _stats_from_timed(bucket, min_samples=min_samples))
        )
    return result


def _bucket_point(window_start: datetime, bucket: ProbeStats, *, partial: bool) -> dict[str, Any]:
    return {
        "t": to_iso_z(window_start),
        "attempts": bucket.attempts,
        "ok": bucket.ok,
        "timeouts": bucket.timeouts,
        "errors": bucket.errors,
        "loss_pct": bucket.loss_pct,
        "p50": bucket.rtt_p50_ms,
        "p95": bucket.rtt_p95_ms,
        "max": bucket.rtt_max_ms,
        "partial": partial,
    }


def bucket_rows(
    rows: Iterable[Any],
    bucket_seconds: float,
    start_at: datetime,
    end_at: datetime,
    *,
    include_partial: bool = False,
) -> list[dict[str, Any]]:
    """Timeline points for the API and the report (spec §12).

    One dict per bucket, empty buckets included, so the caller can draw gaps
    instead of interpolating over them.

    The complete buckets are the half-open :func:`tumbling_windows` of
    ``[start_at, end_at)``, which leaves out the tail that does not fill a
    whole bucket. ``include_partial=True`` appends that tail as one more point
    covering ``[last_start, end_at]`` — closed at ``end_at``, like
    ``quality_db.query_probe_results`` — and marks it ``"partial": True``.
    Views that have to account for every attempt in the range (timeline,
    report charts) use it, so their buckets sum to exactly what the statistics
    and the CSV exports report; incident evaluation keeps the plain tumbling
    windows, where a half-filled window must not be judged.
    """
    ordered = _timed(rows)
    windows = _tumbling_from_timed(ordered, bucket_seconds, start_at, end_at)
    points = [
        _bucket_point(window_start, bucket, partial=False)
        for window_start, _window_end, bucket in windows
    ]
    if not include_partial:
        return points

    start = _as_utc(start_at)
    end = _as_utc(end_at)
    tail_start = start + timedelta(seconds=bucket_seconds) * len(windows)
    if tail_start > end:
        return points
    tail = [pair for pair in ordered if pair[0] is not None and tail_start <= pair[0] <= end]
    if tail_start == end and not tail:
        # A zero-width closing point cannot mean "no data": there is no time in
        # it for a measurement to have happened. It is still emitted when a row
        # sits exactly on `end_at` — `query_probe_results` is inclusive there,
        # so dropping it would stop the buckets summing to the statistics.
        return points
    points.append(_bucket_point(tail_start, _stats_from_timed(tail), partial=True))
    return points
