"""Every `/api/targets*` and `/api/quality/*` route, plus the CSV exports (spec §12).

Handlers stay thin: they parse the range, call the shared functions below and
format the result. The same shared functions serve the printable report
(`report.py`), so the panel, the API, the CSV and the report can only ever
report the same numbers — including on the range boundaries, which
`quality_db.query_probe_results` treats as inclusive on both ends.

Two rules are visible all over this module:

* nothing is invented for time that was not measured — an empty range is
  "no data" (`None`), never a zero;
* ICMP loss, TCP failures and legacy TCP history are separate metrics with
  separate labels, and the legacy history never gets a loss figure (spec §1).
"""
from __future__ import annotations

import inspect
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import quality_db, report
from .coverage import coverage
from .network_tools import _validate_hostname
from .quality_settings import read_quality_settings
from .quality_views import (
    MEASURED_FROM,
    RAW_RANGE_MAX_DAYS,
    _loads,
    aggregate_bucket_for,
    aggregate_points,
    db_path_of,
    get_range,
    incident_payload,
    incident_span,
    incident_windows,
    load_test_payload,
    local_iso,
    localized,
    legacy_tcp_counters,
    range_payload,
    raw_range_allowed,
    stats_payload,
    target_names,
    target_payload,
    target_stats_entries,
    timeline_bucket_seconds,
    tz_name,
)
from .stats import bucket_rows
from .time_utils import ParsedRange, parse_dt, to_iso_z, utc_now

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["quality"])


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------

Kind = Literal["gateway", "internet", "dns", "https", "tcp"]
Proto = Literal["icmp", "tcp", "dns", "https"]
Family = Literal["auto", "ipv4", "ipv6"]


class TargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Kind
    protocol: Proto
    host: str = Field(min_length=1, max_length=2048)
    port: int | None = Field(default=None, ge=1, le=65535)
    interval_seconds: float = Field(default=1.0, gt=0, le=86400)
    timeout_ms: int = Field(default=1000, ge=1, le=60000)
    enabled: bool = True
    family_pref: Family = "auto"
    extra: dict[str, Any] | None = None


class TargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: Kind | None = None
    protocol: Proto | None = None
    host: str | None = Field(default=None, min_length=1, max_length=2048)
    port: int | None = Field(default=None, ge=1, le=65535)
    interval_seconds: float | None = Field(default=None, gt=0, le=86400)
    timeout_ms: int | None = Field(default=None, ge=1, le=60000)
    enabled: bool | None = None
    family_pref: Family | None = None
    extra: dict[str, Any] | None = None


def validate_host(protocol: str, host: str) -> str:
    """Hostname for every probe but HTTPS, which takes an `https://` URL."""
    value = host.strip()
    if protocol == "https":
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise HTTPException(status_code=422, detail="host musi być adresem https://…")
        try:
            _validate_hostname(parts.hostname)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return value
    try:
        return _validate_hostname(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_target(db_path: str, fields: Mapping[str, Any], target_id: int | None = None) -> dict[str, Any]:
    """Cross-field validation of a complete target (spec §12)."""
    values = dict(fields)
    settings = read_quality_settings(db_path)
    min_interval = 0.2 if settings["diagnostic_mode"] else 1.0

    values["host"] = validate_host(str(values["protocol"]), str(values["host"]))

    interval = float(values["interval_seconds"])
    if interval < min_interval:
        raise HTTPException(
            status_code=422,
            detail=f"interval_seconds musi być ≥ {min_interval} s",
        )

    max_timeout = min(interval * 1000.0, 60000.0)
    if float(values["timeout_ms"]) > max_timeout:
        raise HTTPException(
            status_code=422,
            detail=f"timeout_ms musi być ≤ {int(max_timeout)} ms (interwał sondy)",
        )

    name = str(values["name"]).strip()
    for existing in quality_db.list_targets(db_path):
        if existing.name == name and existing.id != target_id:
            raise HTTPException(status_code=409, detail=f"cel o nazwie {name!r} już istnieje")
    values["name"] = name
    return values


@router.get("/targets")
def api_list_targets(request: Request) -> dict[str, Any]:
    db_path = db_path_of(request)
    last_results = quality_db.last_result_per_target(db_path)
    items = []
    for target in quality_db.list_targets(db_path):
        last = last_results.get(target.id)
        items.append(
            {
                **target_payload(target),
                "last_result": localized(last, ("started_at",)) if last else None,
            }
        )
    return {"tz": tz_name(), "items": items}


@router.post("/targets")
def api_create_target(request: Request, body: TargetCreate) -> dict[str, Any]:
    db_path = db_path_of(request)
    values = _validate_target(db_path, body.model_dump())
    try:
        target = quality_db.insert_target(
            db_path,
            name=values["name"],
            kind=values["kind"],
            protocol=values["protocol"],
            host=values["host"],
            port=values["port"],
            interval_seconds=values["interval_seconds"],
            timeout_ms=values["timeout_ms"],
            enabled=values["enabled"],
            family_pref=values["family_pref"],
            extra=values["extra"],
        )
    except sqlite3.IntegrityError as exc:  # UNIQUE(name) lost a race
        raise HTTPException(status_code=409, detail="cel o tej nazwie już istnieje") from exc
    return {"tz": tz_name(), "target": target_payload(target)}


@router.put("/targets/{target_id}")
def api_update_target(request: Request, target_id: int, body: TargetUpdate) -> dict[str, Any]:
    db_path = db_path_of(request)
    current = quality_db.get_target(db_path, target_id)
    if current is None:
        raise HTTPException(status_code=404, detail="nie ma takiego celu")

    changes = body.model_dump(exclude_unset=True)
    merged = {**target_payload(current), **changes}
    values = _validate_target(db_path, merged, target_id=target_id)
    writable = {key: values[key] for key in changes}
    if "host" in changes or "protocol" in changes:
        writable["host"] = values["host"]
    if "name" in changes:
        writable["name"] = values["name"]

    target = quality_db.update_target(db_path, target_id, **writable) if writable else current
    return {"tz": tz_name(), "target": target_payload(target)}


@router.delete("/targets/{target_id}")
def api_delete_target(request: Request, target_id: int) -> dict[str, Any]:
    if not quality_db.delete_target(db_path_of(request), target_id):
        raise HTTPException(status_code=404, detail="nie ma takiego celu")
    return {"tz": tz_name(), "deleted": True}


# ---------------------------------------------------------------------------
# status, statistics, timeline
# ---------------------------------------------------------------------------

def _latest_session(db_path: str) -> dict[str, Any] | None:
    sessions = quality_db.query_sessions(db_path, "1970-01-01T00:00:00.000Z", "2999-01-01T00:00:00.000Z")
    if not sessions:
        return None
    latest = max(sessions, key=lambda row: (str(row["started_at"]), int(row["id"])))
    return localized(latest, ("started_at", "last_seen_at", "ended_at"))


def _engine_status(request: Request, db_path: str) -> dict[str, Any]:
    """`engine.status()`, or the honest "nothing is running" answer."""
    engine = getattr(request.app.state, "quality_engine", None)
    if engine is not None:
        try:
            return dict(engine.status())
        except Exception:  # pragma: no cover - the engine guards itself
            # Never hide it: the status below would claim "no data" while the
            # monitor is in fact running.
            log.warning("Reading the quality engine status failed", exc_info=True)
    return {
        "availability": "no_data",
        "quality": "unknown",
        "lan_degraded": False,
        "icmp_method": "unknown",
        "open_incidents": quality_db.list_open_incidents(db_path),
        "blocked_reason": None,
        "scheduler": {"buffered_rows": 0, "dropped_rows": 0, "skipped_ticks": {}},
        "targets": [],
    }


@router.get("/quality/status")
def api_quality_status(request: Request) -> dict[str, Any]:
    db_path = db_path_of(request)
    now = utc_now()
    status = _engine_status(request, db_path)
    names = target_names(db_path)
    status["open_incidents"] = [
        incident_payload(row, names) for row in status.get("open_incidents", [])
    ]
    coverage_24h = coverage(db_path, now - timedelta(hours=24), now)
    # `**status` first: nothing the engine reports can override the fields
    # this endpoint itself computes (finding 9) — an engine that happened to
    # return a "now"/"tz"/"session"/"coverage_24h_pct" key must never win.
    return {
        **status,
        "now": local_iso(now),
        "tz": tz_name(),
        "measured_from": MEASURED_FROM,
        "session": _latest_session(db_path),
        "coverage_24h_pct": coverage_24h["coverage_pct"],
        "coverage_known": coverage_24h["coverage_known"],
        # How wide a range the raw views and `probes.csv` will serve
        # (review finding C1b) — the panel states the limit instead of
        # letting the operator discover it as a 422.
        "raw_range_max_days": RAW_RANGE_MAX_DAYS,
    }


@router.get("/quality/stats")
def api_quality_stats(request: Request, pr: ParsedRange = Depends(get_range)) -> dict[str, Any]:
    db_path = db_path_of(request)
    return {
        "range": range_payload(pr.start, pr.end),
        "tz": tz_name(),
        "coverage": _coverage_payload(db_path, pr.start, pr.end),
        "targets": target_stats_entries(db_path, pr.start, pr.end),
        "legacy_tcp": legacy_tcp_counters(db_path, pr.start, pr.end),
    }


def _coverage_payload(db_path: str, start: datetime, end: datetime) -> dict[str, Any]:
    result = coverage(db_path, start, end)
    result["gaps"] = [localized(gap, ("from", "to")) for gap in result["gaps"]]
    return result


def _aggregate_timeline_points(
    db_path: str, target_id: int, start: datetime, end: datetime, bucket: str
) -> list[dict[str, Any]]:
    """Aggregates of ``bucket`` mapped to the timeline shape (spec §14, finding 2c).

    Only buckets that fit completely inside ``[start, end]`` are used — the
    same set `target_stats_entries` pools — so a target's timeline always
    sums to its own stats total, aggregate fallback or not. `bucket` comes
    from `aggregate_bucket_for`, which is what keeps this path under the same
    2000-point cap as the raw one (review finding I6).
    """
    return [
        {**point, "t": local_iso(point["t"])}
        for point in aggregate_points(db_path, target_id, start, end, bucket)
    ]


@router.get("/quality/timeline")
def api_quality_timeline(
    request: Request,
    pr: ParsedRange = Depends(get_range),
    bucket_seconds: float = Query(default=60.0, gt=0, le=86400),
) -> dict[str, Any]:
    """Bucketed points per target plus everything drawn on the shared axis.

    Every point of a `raw`-backed series covers `[from, to]` exactly (the
    trailing bucket is included, closed at `to`, per finding 1), so the sum of
    a target's points equals its own `/api/quality/stats` counters. Once raw
    rows for the range are gone (spec §14), the series falls back to the same
    hourly aggregates `target_stats_entries` pools, so the chart is not empty
    under a populated counter (finding 2c); `data_source` says which one a
    series is drawing on, and `last_complete_bucket` only ever reflects `raw`
    coverage — the live panel's "not measured yet" signal has no equivalent
    once the data is a historical rollup.

    A range wider than `RAW_RANGE_MAX_DAYS` takes the aggregate path for every
    target, whether or not raw rows survive for it (review finding C1b): a
    month of raw rows is the widest window this box can materialise. On that
    path each series also states the `bucket` its points are made of.
    """
    db_path = db_path_of(request)
    bucket = timeline_bucket_seconds(bucket_seconds, pr.start, pr.end)
    start_iso, end_iso = to_iso_z(pr.start), to_iso_z(pr.end)
    allow_raw = raw_range_allowed(pr.start, pr.end)
    aggregate_bucket = aggregate_bucket_for(pr.start, pr.end)

    targets = quality_db.list_targets(db_path)
    series = []
    complete_buckets = 0
    for target in targets:
        rows = (
            quality_db.query_probe_results(db_path, start_iso, end_iso, target_id=target.id)
            if allow_raw
            else []
        )
        if rows:
            raw_points = bucket_rows(rows, bucket, pr.start, pr.end, include_partial=True)
            points = [{**point, "t": local_iso(point["t"])} for point in raw_points]
            complete_buckets = max(complete_buckets, sum(1 for p in raw_points if not p["partial"]))
            source = "raw"
        else:
            points = _aggregate_timeline_points(
                db_path, target.id, pr.start, pr.end, aggregate_bucket
            )
            source = "aggregates" if points else "none"
        series.append(
            {
                "target": target_payload(target),
                "data_source": source,
                "bucket": aggregate_bucket if source == "aggregates" else None,
                "points": points,
            }
        )

    names = {target.id: target.name for target in targets}
    last_complete = pr.start + timedelta(seconds=bucket * complete_buckets) if complete_buckets else None
    return {
        "range": range_payload(pr.start, pr.end),
        "tz": tz_name(),
        "bucket_seconds": bucket,
        "last_complete_bucket": local_iso(last_complete),
        "targets": series,
        "incidents": [
            incident_payload(row, names)
            for row in quality_db.query_incidents(db_path, start_iso, end_iso)
        ],
        "gaps": _coverage_payload(db_path, pr.start, pr.end)["gaps"],
        "load_tests": [
            load_test_payload(row) for row in quality_db.query_load_tests(db_path, start_iso, end_iso)
        ],
        "annotations": [
            localized(row, ("at", "created_at"))
            for row in quality_db.query_annotations(db_path, start_iso, end_iso)
        ],
    }


# ---------------------------------------------------------------------------
# incidents, annotations, coverage
# ---------------------------------------------------------------------------

@router.get("/quality/incidents")
def api_incidents(
    request: Request,
    pr: ParsedRange = Depends(get_range),
    target_id: int | None = Query(default=None),
) -> dict[str, Any]:
    db_path = db_path_of(request)
    names = target_names(db_path)
    rows = quality_db.query_incidents(
        db_path, to_iso_z(pr.start), to_iso_z(pr.end), target_id=target_id
    )
    return {
        "range": range_payload(pr.start, pr.end),
        "tz": tz_name(),
        "items": [incident_payload(row, names) for row in rows],
    }


@router.get("/quality/incidents/{incident_id}")
def api_incident_detail(request: Request, incident_id: int) -> dict[str, Any]:
    db_path = db_path_of(request)
    row = quality_db.get_incident(db_path, incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="nie ma takiego incydentu")

    now = utc_now()
    span_start, span_end = incident_span(row, now)
    names = target_names(db_path)
    others = [t for t in quality_db.list_targets(db_path) if t.id != int(row["target_id"])]
    start_iso, end_iso = to_iso_z(span_start), to_iso_z(span_end)

    return {
        "tz": tz_name(),
        "incident": incident_payload(row, names),
        "span": range_payload(span_start, span_end),
        "windows": incident_windows(row),
        "diagnostics": [
            localized(diag, ("started_at",))
            for diag in quality_db.query_diagnostics(db_path, incident_id=incident_id)
        ],
        "related_targets": target_stats_entries(db_path, span_start, span_end, others),
        "load_tests": [
            load_test_payload(lt) for lt in quality_db.query_load_tests(db_path, start_iso, end_iso)
        ],
        "annotations": [
            localized(annotation, ("at", "created_at"))
            for annotation in quality_db.query_annotations(db_path, start_iso, end_iso)
        ],
    }


class AnnotationCreate(BaseModel):
    at: str | None = Field(default=None, max_length=64)
    label: str = Field(max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    incident_id: int | None = None


@router.post("/quality/annotations")
def api_create_annotation(request: Request, body: AnnotationCreate) -> dict[str, Any]:
    """A user's report of a symptom — marked as such, never a measurement (§3)."""
    db_path = db_path_of(request)
    label = body.label.strip()
    if not label:
        raise HTTPException(status_code=422, detail="label nie może być puste")
    try:
        at = parse_dt(body.at) if body.at else utc_now()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"nieprawidłowy czas: {body.at!r}") from exc

    annotation_id = quality_db.insert_annotation(
        db_path,
        to_iso_z(at),
        label,
        note=(body.note or None),
        incident_id=body.incident_id,
    )
    return {
        "tz": tz_name(),
        "annotation": {
            "id": annotation_id,
            "at": local_iso(at),
            "label": label,
            "note": body.note or None,
            "incident_id": body.incident_id,
            "source": "user",
        },
    }


@router.delete("/quality/annotations/{annotation_id}")
def api_delete_annotation(request: Request, annotation_id: int) -> dict[str, Any]:
    if not quality_db.delete_annotation(db_path_of(request), annotation_id):
        raise HTTPException(status_code=404, detail="nie ma takiego oznaczenia")
    return {"tz": tz_name(), "deleted": True}


@router.get("/quality/coverage")
def api_coverage(request: Request, pr: ParsedRange = Depends(get_range)) -> dict[str, Any]:
    db_path = db_path_of(request)
    return {
        "range": range_payload(pr.start, pr.end),
        "tz": tz_name(),
        **_coverage_payload(db_path, pr.start, pr.end),
    }


# ---------------------------------------------------------------------------
# load tests and diagnostics
# ---------------------------------------------------------------------------

@router.get("/quality/load-tests")
def api_load_tests(request: Request, pr: ParsedRange = Depends(get_range)) -> dict[str, Any]:
    db_path = db_path_of(request)
    rows = quality_db.query_load_tests(db_path, to_iso_z(pr.start), to_iso_z(pr.end))
    return {
        "range": range_payload(pr.start, pr.end),
        "tz": tz_name(),
        "items": [load_test_payload(row) for row in rows],
    }


@router.post("/quality/load-tests/run")
async def api_run_load_test(request: Request) -> dict[str, Any]:
    """Run one load test now, if this build has the load test engine (spec §10)."""
    engine = getattr(request.app.state, "quality_engine", None)
    runner = getattr(engine, "run_load_test", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="load tests unavailable")
    outcome = runner("manual")
    if inspect.isawaitable(outcome):
        outcome = await outcome
    if isinstance(outcome, Mapping):
        load_test_id = outcome.get("id")
    else:
        load_test_id = outcome if isinstance(outcome, int) else getattr(outcome, "id", None)
    return {"tz": tz_name(), "id": load_test_id}


@router.get("/quality/load-tests/{load_test_id}")
def api_load_test_detail(request: Request, load_test_id: int) -> dict[str, Any]:
    row = quality_db.get_load_test(db_path_of(request), load_test_id)
    if row is None:
        raise HTTPException(status_code=404, detail="nie ma takiego testu obciążeniowego")
    return {"tz": tz_name(), "load_test": load_test_payload(row, with_raw=True)}


@router.get("/quality/diagnostics")
def api_diagnostics(
    request: Request,
    pr: ParsedRange = Depends(get_range),
    incident_id: int | None = Query(default=None),
) -> dict[str, Any]:
    db_path = db_path_of(request)
    rows = quality_db.query_diagnostics(
        db_path, to_iso_z(pr.start), to_iso_z(pr.end), incident_id=incident_id
    )
    items = []
    for row in rows:
        item = localized(row, ("started_at",))
        item["result"] = _loads(row.get("result_json"))
        item.pop("result_json", None)
        items.append(item)
    return {
        "range": range_payload(pr.start, pr.end),
        "tz": tz_name(),
        "items": items,
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

@router.get("/quality/report.html", response_class=HTMLResponse)
def api_report_html(request: Request, pr: ParsedRange = Depends(get_range)) -> HTMLResponse:
    db_path = db_path_of(request)
    model = report.build_report_model(
        db_path,
        pr.start,
        pr.end,
        app_version=str(getattr(request.app, "version", "dev")),
        now=utc_now(),
    )
    return HTMLResponse(report.render_report(model))
