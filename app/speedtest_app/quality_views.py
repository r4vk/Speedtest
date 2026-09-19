"""View helpers shared by `api_quality.py`, `api_quality_exports.py` and `report.py`.

Every one of these functions is used by at least two of the three modules
above; keeping them in one place is what makes the panel, the CSV exports and
the printable report agree on the same numbers, including on the range
boundaries (`quality_db.query_probe_results` is inclusive on both ends) and on
what a "measurement" means once raw rows have been pruned (spec §14).

Two rules are visible all over this module:

* nothing is invented for time that was not measured — an empty range is
  "no data" (`None`), never a zero;
* ICMP loss, TCP failures and legacy TCP history are separate metrics with
  separate labels, and the legacy history never gets a loss figure (spec §1).
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from fastapi import HTTPException, Query, Request

from . import quality_db
from .db import TimeRange, query_connectivity_checks
from .iperf_udp import LoadTestResult, summarize_for_report
from .probe_types import ProbeTarget
from .quality_settings import read_quality_settings
from .stats import ProbeStats, compute_stats, merge_counters
from .time_utils import ParsedRange, local_tz, parse_dt, parse_range, to_iso_z, to_local_iso

#: Where these measurements were taken (spec §12); shown on every view.
MEASURED_FROM = "NAS (kabel)"

#: What a loss figure means for each protocol — the label is part of the
#: measurement, because ICMP loss and TCP failures are not the same thing.
PROTOCOL_NOTES: dict[str, str] = {
    "icmp": "Utrata odpowiedzi ICMP echo",
    "tcp": "Nieudane zestawienia TCP",
    "dns": "Błędy/timeouty zapytań DNS",
    "https": "Błędy/timeouty HTTPS (DNS, TCP, TLS, HTTP)",
}

#: Timeline resolution limits (spec §12): never finer than 10 s, never more
#: than 2000 points per target.
MIN_BUCKET_SECONDS = 10
MAX_TIMELINE_POINTS = 2000

#: Buckets the aggregate fallback and the aggregates export understand, and
#: the width each one covers (used to tell a *complete* bucket from a partial
#: one at the edge of a requested range).
AGGREGATE_BUCKETS = ("1h", "1d")
AGGREGATE_BUCKET_SECONDS: dict[str, int] = {"1h": 3600, "1d": 86400}

#: Widest range that may be served from raw rows (review finding C1b).
#:
#: A raw row costs ~1 kB resident once it is a `dict`, and the seeded targets
#: produce ~370 000 rows a day, so an unbounded range is an out-of-memory kill
#: on the NAS this runs on — and an OOM kill loses the probe buffer, which is
#: exactly the coverage the feature exists to guarantee. Beyond this span the
#: views fall back to the hourly/daily aggregates (`data_source:
#: "aggregates"`) even when raw rows are still there, and `probes.csv` refuses
#: with 422 rather than dying halfway through the download.
RAW_RANGE_MAX_DAYS = 31


def raw_range_allowed(start: datetime, end: datetime) -> bool:
    """Whether `[start, end]` is narrow enough to read raw rows for."""
    return (end - start) <= timedelta(days=RAW_RANGE_MAX_DAYS)


def aggregate_bucket_for(start: datetime, end: datetime, max_points: int = MAX_TIMELINE_POINTS) -> str:
    """`"1h"` while hourly buckets fit the point budget, `"1d"` beyond it.

    The raw timeline is capped at `MAX_TIMELINE_POINTS` per target by
    `timeline_bucket_seconds`; the aggregate fallback — the path *every* range
    older than `retention_raw_days` takes — needs the same bound, or a
    one-year view answers with 8760 points per target (review finding I6).
    """
    span = max(0.0, (end - start).total_seconds())
    hourly = math.ceil(span / AGGREGATE_BUCKET_SECONDS["1h"]) if span > 0 else 0
    return "1h" if hourly <= max(1, max_points) else "1d"


# ---------------------------------------------------------------------------
# request plumbing
# ---------------------------------------------------------------------------

def db_path_of(request: Request) -> str:
    """The database this app instance serves."""
    path = getattr(request.app.state, "db_path", None)
    if path:
        return str(path)
    from .config import AppConfig  # deferred: tests reload the config module

    return AppConfig().db_path


def get_range(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> ParsedRange:
    """`parse_range`, turned into a FastAPI dependency.

    `parse_dt` raises `ValueError` on an unparsable timestamp; left uncaught
    that becomes an unhelpful 500. Every quality endpoint takes its range
    through this dependency so a bad `from`/`to` is always a 422 instead.
    """
    try:
        return parse_range(from_, to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"nieprawidłowy zakres czasu: {exc}") from exc


def tz_name() -> str:
    """Name of the local zone, e.g. ``CEST`` — every response states it."""
    return local_tz().tzname(None) or time.tzname[0]


def local_iso(value: str | datetime | None) -> str | None:
    """Local ISO time of a stored UTC timestamp; ``None`` stays ``None``."""
    if value is None or value == "":
        return None
    moment = value if isinstance(value, datetime) else parse_dt(str(value))
    return to_local_iso(moment)


def localized(row: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """A copy of ``row`` with the given timestamp columns in local time."""
    out = dict(row)
    for key in keys:
        if key in out:
            out[key] = local_iso(out[key])
    return out


def range_payload(start: datetime, end: datetime) -> dict[str, Any]:
    return {"from": local_iso(start), "to": local_iso(end)}


def target_payload(target: ProbeTarget) -> dict[str, Any]:
    """A probe target as the API and the report show it."""
    return {
        "id": target.id,
        "name": target.name,
        "kind": target.kind,
        "protocol": str(target.protocol),
        "host": target.host,
        "port": target.port,
        "interval_seconds": target.interval_seconds,
        "timeout_ms": target.timeout_ms,
        "enabled": target.enabled,
        "family_pref": target.family_pref,
        "extra": target.extra,
    }


def target_names(db_path: str) -> dict[int, str]:
    return {target.id: target.name for target in quality_db.list_targets(db_path)}


def stats_payload(stats: ProbeStats) -> dict[str, Any]:
    """``ProbeStats`` with its two timestamps in local time."""
    payload = stats.as_dict()
    payload["first_at"] = local_iso(payload["first_at"])
    payload["last_at"] = local_iso(payload["last_at"])
    return payload


def error_kinds_histogram(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """How often each `error_kind` was seen — errors are never loss (spec §1)."""
    histogram: dict[str, int] = {}
    for row in rows:
        kind = row.get("error_kind")
        if kind:
            histogram[str(kind)] = histogram.get(str(kind), 0) + 1
    return dict(sorted(histogram.items()))


# ---------------------------------------------------------------------------
# stats, with the aggregate fallback (spec §6, §14)
# ---------------------------------------------------------------------------

def _stats_from_aggregates(rows: Sequence[Mapping[str, Any]]) -> ProbeStats:
    """Pool aggregate buckets: counters only, percentiles stay unknown (spec §6)."""
    parts = [
        ProbeStats(
            attempts=int(row["attempts"]),
            ok=int(row["ok_count"]),
            timeouts=int(row["timeout_count"]),
            errors=int(row["error_count"]),
            loss_pct=row.get("loss_pct"),
            longest_fail_streak=int(row.get("longest_fail_streak") or 0),
            first_at=row["bucket_start"],
            last_at=row["bucket_start"],
        )
        for row in rows
    ]
    return merge_counters(parts)


def fully_contained_aggregates(
    db_path: str, bucket: str, start: datetime, end: datetime, target_id: int | None = None
) -> list[dict[str, Any]]:
    """Aggregate rows of `bucket` whose whole window sits inside `[start, end]`.

    `quality_db.query_aggregates` only filters `bucket_start` against the
    range, so a bucket that merely *starts* inside `[from, to]` can still
    reach past `to` (or, if it starts before `from`, be excluded even though
    most of it falls inside the range). Neither describes the requested range
    honestly: the first over-reports, the second silently drops overlapping
    data. Keeping only buckets that fit completely is what lets
    `covered_from`/`covered_to` state exactly what the pooled numbers
    describe (spec §14), and what keeps the timeline's aggregate fallback
    summing to the same total as the stats fallback.
    """
    width = timedelta(seconds=AGGREGATE_BUCKET_SECONDS[bucket])
    rows = quality_db.query_aggregates(db_path, bucket, to_iso_z(start), to_iso_z(end), target_id=target_id)
    return [row for row in rows if parse_dt(str(row["bucket_start"])) + width <= end]


def aggregate_points(
    db_path: str, target_id: int, start: datetime, end: datetime, bucket: str
) -> list[dict[str, Any]]:
    """Fully-contained aggregate rows in the timeline's point shape (spec §14).

    Only buckets that fit completely inside ``[start, end]`` are used — the
    same set `target_stats_entries` pools — so a target's timeline always sums
    to its own stats total, aggregate fallback or not. ``t`` is the stored
    UTC timestamp; the callers localise it the way they localise every other
    timestamp they emit.
    """
    points: list[dict[str, Any]] = []
    for row in fully_contained_aggregates(db_path, bucket, start, end, target_id):
        stats = _stats_from_aggregates([row])
        points.append(
            {
                "t": str(row["bucket_start"]),
                "attempts": stats.attempts,
                "ok": stats.ok,
                "timeouts": stats.timeouts,
                "errors": stats.errors,
                "loss_pct": stats.loss_pct,
                # Percentiles are not poolable across buckets (spec §6): the
                # aggregate path reports counters and says nothing it cannot
                # know.
                "p50": None,
                "p95": None,
                "max": None,
                "partial": False,
            }
        )
    return points


def raw_rows_by_target(
    db_path: str,
    start: datetime,
    end: datetime,
    targets: Sequence[ProbeTarget],
) -> dict[int, list[dict[str, Any]]]:
    """Each target's raw rows for the range, fetched exactly once.

    The report needs the same rows twice (the per-target table and the
    charts); fetching them once and passing them around is what keeps one
    report request from materialising the range twice (review finding C1b).
    Beyond `RAW_RANGE_MAX_DAYS` nothing is read at all and every target gets
    an empty list, which sends the callers down the aggregate path.
    """
    if not raw_range_allowed(start, end):
        return {target.id: [] for target in targets}
    start_iso, end_iso = to_iso_z(start), to_iso_z(end)
    return {
        target.id: quality_db.query_probe_results(db_path, start_iso, end_iso, target_id=target.id)
        for target in targets
    }


def target_stats_entries(
    db_path: str,
    start: datetime,
    end: datetime,
    targets: Sequence[ProbeTarget] | None = None,
    raw_rows: Mapping[int, Sequence[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Per-target counters and statistics for the range, enabled targets included.

    Raw rows are the source whenever the range still holds any; when they have
    been pruned (spec §14) the hourly aggregates that fit completely inside
    `[start, end]` are pooled instead, and `data_source` says so, so a reader
    can tell a measurement from a reconstruction. A target with neither is
    ``none`` — never a silent zero. `covered_from`/`covered_to` state the span
    the numbers actually describe: the full requested range for `raw`, the
    span of the pooled buckets for `aggregates`, and `None` for `none` — a
    short sub-hour range can legitimately have no fully-contained aggregate at
    all, and that must never be confused with "the target was silent".

    A range wider than `RAW_RANGE_MAX_DAYS` never reads raw rows, even when
    they are still there (review finding C1b): it takes the aggregate path and
    labels itself `aggregates`, which is the honest description of numbers
    pooled from rollups. ``raw_rows`` lets a caller that already fetched the
    rows for this very range (the report) hand them over instead of paying for
    a second query.
    """
    start_iso, end_iso = to_iso_z(start), to_iso_z(end)
    allow_raw = raw_range_allowed(start, end)
    bucket = aggregate_bucket_for(start, end)
    entries: list[dict[str, Any]] = []
    for target in targets if targets is not None else quality_db.list_targets(db_path):
        if raw_rows is not None:
            rows: Sequence[Mapping[str, Any]] = raw_rows.get(target.id) or ()
        elif allow_raw:
            rows = quality_db.query_probe_results(db_path, start_iso, end_iso, target_id=target.id)
        else:
            rows = ()
        covered_from: datetime | None
        covered_to: datetime | None
        if rows:
            stats, source = compute_stats(rows), "raw"
            covered_from, covered_to = start, end
        else:
            aggregates = fully_contained_aggregates(db_path, bucket, start, end, target.id)
            if aggregates:
                stats, source = _stats_from_aggregates(aggregates), "aggregates"
                width = timedelta(seconds=AGGREGATE_BUCKET_SECONDS[bucket])
                starts = [parse_dt(str(row["bucket_start"])) for row in aggregates]
                covered_from, covered_to = min(starts), max(starts) + width
            else:
                stats, source = ProbeStats(), "none"
                covered_from = covered_to = None
        entries.append(
            {
                "target": target_payload(target),
                "stats": stats_payload(stats),
                "note": PROTOCOL_NOTES.get(str(target.protocol), ""),
                "error_kinds": error_kinds_histogram(rows),
                "data_source": source,
                "covered_from": local_iso(covered_from),
                "covered_to": local_iso(covered_to),
            }
        )
    return entries


def legacy_tcp_counters(db_path: str, start: datetime, end: datetime) -> dict[str, int] | None:
    """Legacy TCP history: attempts and failures only — never a loss percentage.

    ``None`` when the range holds no legacy row at all, so the panel can hide
    the table instead of showing zeros (spec §1).
    """
    rows = query_connectivity_checks(
        db_path, TimeRange(start_iso=to_iso_z(start), end_iso=to_iso_z(end))
    )
    if not rows:
        return None
    failures = sum(1 for row in rows if not row["is_up"])
    return {"attempts": len(rows), "failures": failures}


def retention_cutoff(db_path: str, now: datetime, settings: Mapping[str, Any] | None = None) -> datetime:
    """Instant before which raw probe rows may already have been pruned."""
    values = settings if settings is not None else read_quality_settings(db_path)
    return now - timedelta(days=int(values["retention_raw_days"]))


# ---------------------------------------------------------------------------
# load tests
# ---------------------------------------------------------------------------

def _load_test_runs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every direction of one load test row, whatever shape `result_json` has.

    A row can hold one run, a mapping of direction -> run or a list of runs;
    a row without a usable result still yields one entry per requested
    direction so that a failed or skipped test stays visible.
    """
    raw = row.get("result_json")
    parsed: Any = None
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None

    runs: list[dict[str, Any]] = []
    if isinstance(parsed, Mapping):
        if "receiver" in parsed:
            runs = [dict(parsed)]
        else:
            runs = [
                {**value, "direction": value.get("direction", key)}
                for key, value in parsed.items()
                if isinstance(value, Mapping)
            ]
    elif isinstance(parsed, list):
        runs = [dict(item) for item in parsed if isinstance(item, Mapping)]

    if runs:
        return runs
    directions = ["upload", "download"] if row.get("direction") == "both" else [row.get("direction")]
    return [{"direction": direction} for direction in directions]


def load_test_summaries(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One summary per direction, with loss recomputed from the receiver counters."""
    summaries: list[dict[str, Any]] = []
    for run in _load_test_runs(row):
        receiver = run.get("receiver") if isinstance(run.get("receiver"), Mapping) else {}
        result = LoadTestResult(
            kind=str(run.get("kind") or row.get("kind") or ""),
            direction=str(run.get("direction") or row.get("direction") or ""),
            receiver=dict(receiver),
            sender=dict(run.get("sender") or {}),
            intervals=[],
            duration_seconds=float(run.get("duration_seconds") or 0.0),
            protocol=str(run.get("protocol") or ""),
            version=run.get("version"),
        )
        summaries.append(summarize_for_report(result))
    return summaries


def _loads(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def load_test_payload(row: Mapping[str, Any], *, with_raw: bool = False) -> dict[str, Any]:
    """A load test row for the API: parsed params/result plus per-direction summaries."""
    payload = localized(row, ("started_at", "ended_at"))
    payload["params"] = _loads(row.get("params_json"))
    payload["result"] = _loads(row.get("result_json"))
    payload["summaries"] = load_test_summaries(row)
    payload.pop("params_json", None)
    payload.pop("result_json", None)
    if with_raw:
        payload["raw_json"] = row.get("raw_json")
    else:
        payload.pop("raw_json", None)
    return payload


# ---------------------------------------------------------------------------
# incidents
# ---------------------------------------------------------------------------

def incident_windows(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The window verdict sequence stored in `summary_json` (spec §7)."""
    parsed = _loads(row.get("summary_json"))
    if not isinstance(parsed, list):
        return []
    windows: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, (list, tuple)) or not item:
            continue
        values = list(item) + [None] * (4 - len(item))
        windows.append(
            {
                "window_start": local_iso(values[0]),
                "verdict": values[1],
                "loss_pct": values[2],
                "p95": values[3],
            }
        )
    return windows


def incident_payload(row: Mapping[str, Any], names: Mapping[int, str]) -> dict[str, Any]:
    """An incident row for the API: local times, target name, no raw JSON blob."""
    payload = localized(row, ("started_at", "ended_at", "closed_at"))
    payload["target_name"] = names.get(int(row["target_id"]))
    payload["windows_count"] = len(incident_windows(row))
    payload.pop("summary_json", None)
    return payload


def incident_span(row: Mapping[str, Any], now: datetime) -> tuple[datetime, datetime]:
    """`[started_at, ended_at or closed_at or now]` — the incident's own range."""
    start = parse_dt(str(row["started_at"]))
    end_raw = row.get("ended_at") or row.get("closed_at")
    return start, parse_dt(str(end_raw)) if end_raw else now


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------

def timeline_bucket_seconds(requested: float, start: datetime, end: datetime) -> float:
    """Bucket width honouring the 10 s floor and the 2000 point cap (spec §12).

    `bucket_rows(..., include_partial=True)` always appends one closing point
    on top of the complete tumbling windows (spec finding 1), so the point
    budget below has to leave room for it — otherwise a range whose complete
    windows alone hit 2000 would return 2001 points.
    """
    span = max(0.0, (end - start).total_seconds())
    capacity = max(1, MAX_TIMELINE_POINTS - 1)
    needed = math.ceil(span / capacity) if span > 0 else 0
    return float(max(MIN_BUCKET_SECONDS, math.ceil(requested), needed))
