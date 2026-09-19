"""The CSV exports of `/api/quality/export/*` (design spec §12).

Split out of `api_quality.py` to keep both files readable; the router lives
here and `main.py` includes it next to the quality router. Every export reuses
that module's shared helpers, so a CSV can never disagree with the panel — and
a range reaching behind the raw retention says so in a comment line instead of
returning a short file without explanation (spec §14).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator, Mapping

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import quality_db
from .quality_views import (
    AGGREGATE_BUCKETS,
    RAW_RANGE_MAX_DAYS,
    db_path_of,
    get_range,
    load_test_summaries,
    raw_range_allowed,
    retention_cutoff,
    target_names,
    tz_name,
)
from .csv_utils import csv_response
from .time_utils import ParsedRange, parse_dt, to_iso_z, to_local_display, utc_now

router = APIRouter(prefix="/api", tags=["quality-export"])

#: Comment line put at the top of an export whose range reaches behind the raw
#: retention (spec §14). ASCII on purpose: it travels in a CSV.
RETENTION_CSV_COMMENT = "# surowe dane niedostepne (retencja) przed {when}"

#: First comment line of every quality export (finding 10): every timestamp
#: column in these files is local time, and the report/panel state the same
#: zone, so the CSV has to say which one it is instead of leaving it implicit.
TIMEZONE_CSV_COMMENT = "# strefa czasowa: {tz}"


def _timezone_comment() -> str:
    return TIMEZONE_CSV_COMMENT.format(tz=tz_name())


def _retention_comments(db_path: str, start: datetime, now: datetime) -> list[str]:
    cutoff = retention_cutoff(db_path, now)
    if start >= cutoff:
        return []
    return [RETENTION_CSV_COMMENT.format(when=to_local_display(cutoff))]


#: Header of `probes.csv`; the streaming generator yields it first.
PROBE_CSV_HEADER: tuple[str, ...] = (
    "started_at_local",
    "started_at_utc",
    "device_id",
    "target",
    "protocol",
    "outcome",
    "rtt_ms",
    "timeout_ms",
    "resolved_ip",
    "ip_family",
    "error_kind",
    "error_detail",
    "load_test_id",
)


def _probe_csv_rows(
    db_path: str, pr: ParsedRange, target_id: int | None, names: Mapping[int, str]
) -> Iterator[list[Any]]:
    """The header and one list per raw row, straight off the cursor.

    A generator on purpose (review finding C1a): the export used to fetch the
    whole range and then build a second, equally large list of CSV rows on top
    of it, which is ~640 MB for the default 24 h of the seeded targets. Here
    exactly one row exists at a time.
    """
    yield list(PROBE_CSV_HEADER)
    for row in quality_db.iter_probe_results(
        db_path, to_iso_z(pr.start), to_iso_z(pr.end), target_id=target_id
    ):
        yield [
            to_local_display(parse_dt(row["started_at"])),
            row["started_at"],
            row["device_id"],
            names.get(int(row["target_id"]), str(row["target_id"])),
            row["protocol"],
            row["outcome"],
            row["rtt_ms"] if row["rtt_ms"] is not None else "",
            row["timeout_ms"],
            row["resolved_ip"] or "",
            row["ip_family"] if row["ip_family"] is not None else "",
            row["error_kind"] or "",
            row["error_detail"] or "",
            row["load_test_id"] if row["load_test_id"] is not None else "",
        ]


@router.get("/quality/export/probes.csv")
def export_probes_csv(
    request: Request,
    pr: ParsedRange = Depends(get_range),
    target_id: int | None = Query(default=None),
):
    if not raw_range_allowed(pr.start, pr.end):
        # Stating the limit is better than streaming for ten minutes and
        # dying on the last row (review finding C1b); the aggregates export
        # covers arbitrarily wide ranges.
        raise HTTPException(
            status_code=422,
            detail=f"zakres surowych danych maks. {RAW_RANGE_MAX_DAYS} dni",
        )
    db_path = db_path_of(request)
    names = target_names(db_path)
    comments = [_timezone_comment(), *_retention_comments(db_path, pr.start, utc_now())]
    return csv_response(
        "probes.csv",
        _probe_csv_rows(db_path, pr, target_id, names),
        comment_lines=comments,
    )


@router.get("/quality/export/incidents.csv")
def export_incidents_csv(request: Request, pr: ParsedRange = Depends(get_range)):
    db_path = db_path_of(request)
    names = target_names(db_path)
    rows: list[list[Any]] = [
        [
            "id",
            "started_at_local",
            "started_at_utc",
            "ended_at_local",
            "target",
            "protocol",
            "kind",
            "closed_at_local",
            "close_reason",
            "window_seconds",
            "probe_interval_seconds",
            "peak_loss_pct",
            "peak_p95_rtt_ms",
            "longest_fail_streak",
            "windows_degraded",
        ]
    ]
    for row in quality_db.query_incidents(db_path, to_iso_z(pr.start), to_iso_z(pr.end)):
        rows.append(
            [
                row["id"],
                to_local_display(parse_dt(row["started_at"])),
                row["started_at"],
                to_local_display(parse_dt(row["ended_at"])) if row["ended_at"] else "",
                names.get(int(row["target_id"]), str(row["target_id"])),
                row["protocol"],
                row["kind"],
                to_local_display(parse_dt(row["closed_at"])) if row["closed_at"] else "",
                row["close_reason"] or "",
                row["window_seconds"],
                row["probe_interval_seconds"],
                row["peak_loss_pct"] if row["peak_loss_pct"] is not None else "",
                row["peak_p95_rtt_ms"] if row["peak_p95_rtt_ms"] is not None else "",
                row["longest_fail_streak"] if row["longest_fail_streak"] is not None else "",
                row["windows_degraded"],
            ]
        )
    return csv_response("incidents.csv", rows, comment_lines=[_timezone_comment()])


@router.get("/quality/export/aggregates.csv")
def export_aggregates_csv(
    request: Request,
    pr: ParsedRange = Depends(get_range),
    bucket: str = Query(default="1h"),
):
    if bucket not in AGGREGATE_BUCKETS:
        raise HTTPException(status_code=422, detail="bucket musi być 1h albo 1d")
    db_path = db_path_of(request)
    names = target_names(db_path)
    columns = (
        "attempts",
        "ok_count",
        "timeout_count",
        "error_count",
        "loss_pct",
        "rtt_min_ms",
        "rtt_p50_ms",
        "rtt_p95_ms",
        "rtt_p99_ms",
        "rtt_max_ms",
        "rtt_mean_ms",
        "rtt_variation_ms",
        "longest_fail_streak",
        "percentiles_from_raw",
    )
    rows: list[list[Any]] = [
        ["bucket_start_local", "bucket_start_utc", "target", "protocol", "bucket", *columns]
    ]
    for row in quality_db.query_aggregates(db_path, bucket, to_iso_z(pr.start), to_iso_z(pr.end)):
        rows.append(
            [
                to_local_display(parse_dt(row["bucket_start"])),
                row["bucket_start"],
                names.get(int(row["target_id"]), str(row["target_id"])),
                row["protocol"],
                row["bucket"],
                *[row[column] if row[column] is not None else "" for column in columns],
            ]
        )
    return csv_response("aggregates.csv", rows, comment_lines=[_timezone_comment()])


@router.get("/quality/export/load-tests.csv")
def export_load_tests_csv(request: Request, pr: ParsedRange = Depends(get_range)):
    db_path = db_path_of(request)
    rows: list[list[Any]] = [
        [
            "id",
            "started_at_local",
            "started_at_utc",
            "ended_at_local",
            "kind",
            "direction",
            "server",
            "status",
            "error",
            "loss_pct",
            "lost",
            "packets",
            "jitter_ms",
            "mbps",
        ]
    ]
    for row in quality_db.query_load_tests(db_path, to_iso_z(pr.start), to_iso_z(pr.end)):
        for summary in load_test_summaries(row):
            rows.append(
                [
                    row["id"],
                    to_local_display(parse_dt(row["started_at"])),
                    row["started_at"],
                    to_local_display(parse_dt(row["ended_at"])) if row["ended_at"] else "",
                    summary.get("kind") or row["kind"],
                    summary.get("direction") or row["direction"],
                    row["server"] or "",
                    row["status"],
                    row["error"] or "",
                    summary.get("loss_pct") if summary.get("loss_pct") is not None else "",
                    summary.get("lost") if summary.get("lost") is not None else "",
                    summary.get("packets") if summary.get("packets") is not None else "",
                    summary.get("jitter_ms") if summary.get("jitter_ms") is not None else "",
                    summary.get("mbps") if summary.get("mbps") is not None else "",
                ]
            )
    return csv_response("load-tests.csv", rows, comment_lines=[_timezone_comment()])
