"""The CSV exports of `/api/quality/export/*` (design spec §12).

Split out of `api_quality.py` to keep both files readable; the router lives
here and `main.py` includes it next to the quality router. Every export reuses
that module's shared helpers, so a CSV can never disagree with the panel — and
a range reaching behind the raw retention says so in a comment line instead of
returning a short file without explanation (spec §14).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from . import quality_db
from .api_quality import (
    AGGREGATE_BUCKETS,
    db_path_of,
    load_test_summaries,
    retention_cutoff,
    target_names,
)
from .csv_utils import csv_response
from .time_utils import parse_dt, parse_range, to_iso_z, to_local_display, utc_now

router = APIRouter(prefix="/api", tags=["quality-export"])

#: Comment line put at the top of an export whose range reaches behind the raw
#: retention (spec §14). ASCII on purpose: it travels in a CSV.
RETENTION_CSV_COMMENT = "# surowe dane niedostepne (retencja) przed {when}"


def _retention_comments(db_path: str, start: datetime, now: datetime) -> list[str]:
    cutoff = retention_cutoff(db_path, now)
    if start >= cutoff:
        return []
    return [RETENTION_CSV_COMMENT.format(when=to_local_display(cutoff))]


@router.get("/quality/export/probes.csv")
def export_probes_csv(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    target_id: int | None = Query(default=None),
):
    db_path = db_path_of(request)
    pr = parse_range(from_, to)
    names = target_names(db_path)
    rows: list[list[Any]] = [
        [
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
        ]
    ]
    for row in quality_db.query_probe_results(
        db_path, to_iso_z(pr.start), to_iso_z(pr.end), target_id=target_id
    ):
        rows.append(
            [
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
        )
    return csv_response(
        "probes.csv", rows, comment_lines=_retention_comments(db_path, pr.start, utc_now())
    )


@router.get("/quality/export/incidents.csv")
def export_incidents_csv(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    db_path = db_path_of(request)
    pr = parse_range(from_, to)
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
    return csv_response("incidents.csv", rows)


@router.get("/quality/export/aggregates.csv")
def export_aggregates_csv(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    bucket: str = Query(default="1h"),
):
    if bucket not in AGGREGATE_BUCKETS:
        raise HTTPException(status_code=422, detail="bucket musi być 1h albo 1d")
    db_path = db_path_of(request)
    pr = parse_range(from_, to)
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
    return csv_response("aggregates.csv", rows)


@router.get("/quality/export/load-tests.csv")
def export_load_tests_csv(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    db_path = db_path_of(request)
    pr = parse_range(from_, to)
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
    return csv_response("load-tests.csv", rows)
