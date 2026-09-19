"""Hourly and daily aggregation of raw probe rows (design spec §6.4).

A bucket is always computed from the raw ``probe_results`` rows of that bucket:
a daily row is *not* built from the hourly rows, because percentiles cannot be
averaged and loss has to be pooled from counters. The only persistence used
here is ``quality_db``; all arithmetic lives in :mod:`speedtest_app.stats`.

If the raw rows of a bucket were already pruned there is nothing to compute, so
an existing aggregate row is left untouched and no new one is written — missing
means "no data", never "zero".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Mapping, Sequence

from . import quality_db
from .probe_types import ProbeTarget
from .stats import compute_stats
from .time_utils import parse_dt, to_iso_z

Bucket = Literal["1h", "1d"]

#: Width of every supported bucket.
BUCKET_WIDTHS: dict[str, timedelta] = {"1h": timedelta(hours=1), "1d": timedelta(days=1)}

#: ISO-Z strings do not sort like the instants they denote when fractional
#: seconds are mixed in ("...T10:00:00.500Z" < "...T10:00:00Z"), so the SQL
#: range is widened by this much and the exact filtering is done on parsed
#: timestamps.
_QUERY_GUARD = timedelta(seconds=1)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _width(bucket: str) -> timedelta:
    try:
        return BUCKET_WIDTHS[bucket]
    except KeyError:
        raise ValueError(f"unknown bucket: {bucket!r}") from None


def bucket_start(dt: datetime, bucket: Bucket) -> datetime:
    """Start of the UTC-aligned bucket containing ``dt``."""
    _width(bucket)
    moment = _as_utc(dt)
    if bucket == "1h":
        return moment.replace(minute=0, second=0, microsecond=0)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def bucket_end(dt: datetime, bucket: Bucket) -> datetime:
    """End (exclusive) of the UTC-aligned bucket containing ``dt``."""
    return bucket_start(dt, bucket) + _width(bucket)


def compute_bucket(
    rows: Iterable[Any],
    target_id: int,
    protocol: str,
    bucket: Bucket,
    bucket_start: datetime,
    *,
    now_iso: str,
) -> dict[str, Any] | None:
    """A ``probe_aggregates`` row from the raw rows of one bucket.

    ``None`` when there are no raw rows: the caller must then leave whatever is
    stored alone instead of writing an empty bucket.
    """
    _width(bucket)
    rows = list(rows)
    if not rows:
        return None

    stats = compute_stats(rows)
    return {
        "target_id": int(target_id),
        "protocol": str(protocol),
        "bucket": str(bucket),
        "bucket_start": to_iso_z(_as_utc(bucket_start)),
        "attempts": stats.attempts,
        "ok_count": stats.ok,
        "timeout_count": stats.timeouts,
        "error_count": stats.errors,
        "loss_pct": stats.loss_pct,
        "rtt_min_ms": stats.rtt_min_ms,
        "rtt_p50_ms": stats.rtt_p50_ms,
        "rtt_p95_ms": stats.rtt_p95_ms,
        "rtt_p99_ms": stats.rtt_p99_ms,
        "rtt_max_ms": stats.rtt_max_ms,
        "rtt_mean_ms": stats.rtt_mean_ms,
        "rtt_variation_ms": stats.rtt_variation_ms,
        "longest_fail_streak": stats.longest_fail_streak,
        "percentiles_from_raw": 1,
        "computed_at": now_iso,
    }


def _raw_rows(
    db_path: str,
    target_id: int,
    protocol: str,
    start: datetime,
    end: datetime,
) -> list[Mapping[str, Any]]:
    """Raw rows of ``[start, end)`` for one target and protocol."""
    rows = quality_db.query_probe_results(
        db_path,
        to_iso_z(start - _QUERY_GUARD),
        to_iso_z(end + _QUERY_GUARD),
        target_id=target_id,
        protocol=protocol,
    )
    selected: list[Mapping[str, Any]] = []
    for row in rows:
        raw = row.get("started_at")
        if not isinstance(raw, str):
            continue
        try:
            started_at = parse_dt(raw)
        except ValueError:
            continue
        if start <= started_at < end:
            selected.append(row)
    return selected


def aggregate_range(
    db_path: str,
    target_id: int,
    protocol: str,
    bucket: Bucket,
    start: datetime,
    end: datetime,
    *,
    now_iso: str,
    include_partial: bool = False,
) -> int:
    """Upsert every bucket touching ``[start, end)``; returns how many were written.

    Buckets are aligned to the UTC grid, so a range starting mid-bucket still
    aggregates that whole bucket — an aggregate always describes a full hour or
    day. The trailing bucket is only written when ``include_partial`` is set,
    and it is then computed from the rows up to ``end`` only.
    Buckets without raw rows are skipped, leaving any stored row untouched.
    """
    width = _width(bucket)
    range_start = _as_utc(start)
    range_end = _as_utc(end)
    written = 0

    current = bucket_start(range_start, bucket)
    while current < range_end:
        stop = current + width
        if stop > range_end:
            if not include_partial:
                break
            rows = _raw_rows(db_path, target_id, protocol, current, range_end)
        else:
            rows = _raw_rows(db_path, target_id, protocol, current, stop)
        row = compute_bucket(rows, target_id, protocol, bucket, current, now_iso=now_iso)
        if row is not None:
            quality_db.upsert_aggregate(db_path, row)
            written += 1
        current = stop
    return written


def aggregate_all(
    db_path: str,
    targets: Sequence[ProbeTarget],
    start: datetime,
    end: datetime,
    *,
    now_iso: str,
) -> dict[str, int]:
    """Aggregate both buckets for every target; returns ``{bucket: written}``."""
    written = {bucket: 0 for bucket in BUCKET_WIDTHS}
    for target in targets:
        for bucket in BUCKET_WIDTHS:
            written[bucket] += aggregate_range(
                db_path,
                target.id,
                str(target.protocol),
                bucket,  # type: ignore[arg-type]
                start,
                end,
                now_iso=now_iso,
            )
    return written
