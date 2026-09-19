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
import json
import logging
import math
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Iterable, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import quality_db
from .coverage import coverage
from .db import TimeRange, query_connectivity_checks
from .iperf_udp import LoadTestResult, summarize_for_report
from .network_tools import _validate_hostname
from .probe_types import ProbeTarget
from .quality_settings import read_quality_settings
from .stats import ProbeStats, bucket_rows, compute_stats, merge_counters
from .time_utils import local_tz, parse_dt, parse_range, to_iso_z, to_local_iso, utc_now

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["quality"])

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

#: Buckets the aggregate fallback and the aggregates export understand.
AGGREGATE_BUCKETS = ("1h", "1d")


# ---------------------------------------------------------------------------
# shared helpers (also used by report.py)
# ---------------------------------------------------------------------------

def db_path_of(request: Request) -> str:
    """The database this app instance serves."""
    path = getattr(request.app.state, "db_path", None)
    if path:
        return str(path)
    from .config import AppConfig  # deferred: tests reload the config module

    return AppConfig().db_path


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


def target_stats_entries(
    db_path: str,
    start: datetime,
    end: datetime,
    targets: Sequence[ProbeTarget] | None = None,
) -> list[dict[str, Any]]:
    """Per-target counters and statistics for the range, enabled targets included.

    Raw rows are the source whenever the range still holds any; when they have
    been pruned (spec §14) the hourly aggregates are pooled instead and
    ``data_source`` says so, so a reader can tell a measurement from a
    reconstruction. A target with neither is ``none`` — never a silent zero.
    """
    start_iso, end_iso = to_iso_z(start), to_iso_z(end)
    entries: list[dict[str, Any]] = []
    for target in targets if targets is not None else quality_db.list_targets(db_path):
        rows = quality_db.query_probe_results(db_path, start_iso, end_iso, target_id=target.id)
        if rows:
            stats, source = compute_stats(rows), "raw"
        else:
            aggregates = quality_db.query_aggregates(db_path, "1h", start_iso, end_iso, target_id=target.id)
            if aggregates:
                stats, source = _stats_from_aggregates(aggregates), "aggregates"
            else:
                stats, source = ProbeStats(), "none"
        entries.append(
            {
                "target": target_payload(target),
                "stats": stats_payload(stats),
                "note": PROTOCOL_NOTES.get(str(target.protocol), ""),
                "error_kinds": error_kinds_histogram(rows),
                "data_source": source,
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


def _loads(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


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


def target_names(db_path: str) -> dict[int, str]:
    return {target.id: target.name for target in quality_db.list_targets(db_path)}


def timeline_bucket_seconds(requested: float, start: datetime, end: datetime) -> float:
    """Bucket width honouring the 10 s floor and the 2000 point cap (spec §12)."""
    span = max(0.0, (end - start).total_seconds())
    needed = math.ceil(span / MAX_TIMELINE_POINTS) if span > 0 else 0
    return float(max(MIN_BUCKET_SECONDS, math.ceil(requested), needed))


def range_payload(start: datetime, end: datetime) -> dict[str, Any]:
    return {"from": local_iso(start), "to": local_iso(end)}


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
    return {
        "now": local_iso(now),
        "tz": tz_name(),
        "measured_from": MEASURED_FROM,
        "session": _latest_session(db_path),
        "coverage_24h_pct": coverage_24h["coverage_pct"],
        "coverage_known": coverage_24h["coverage_known"],
        **status,
    }


@router.get("/quality/stats")
def api_quality_stats(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    db_path = db_path_of(request)
    pr = parse_range(from_, to)
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


@router.get("/quality/timeline")
def api_quality_timeline(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    bucket_seconds: float = Query(default=60.0, gt=0, le=86400),
) -> dict[str, Any]:
    """Bucketed points per target plus everything drawn on the shared axis.

    The trailing, still-filling bucket is not returned (it would look like a
    quiet period), so the response states `bucket_seconds` and the end of the
    last complete bucket: a live view can tell "not measured yet" from a gap.
    """
    db_path = db_path_of(request)
    pr = parse_range(from_, to)
    bucket = timeline_bucket_seconds(bucket_seconds, pr.start, pr.end)
    start_iso, end_iso = to_iso_z(pr.start), to_iso_z(pr.end)

    targets = quality_db.list_targets(db_path)
    series = []
    complete_buckets = 0
    for target in targets:
        rows = quality_db.query_probe_results(db_path, start_iso, end_iso, target_id=target.id)
        points = bucket_rows(rows, bucket, pr.start, pr.end)
        complete_buckets = max(complete_buckets, len(points))
        series.append(
            {
                "target": target_payload(target),
                "points": [{**point, "t": local_iso(point["t"])} for point in points],
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
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    target_id: int | None = Query(default=None),
) -> dict[str, Any]:
    db_path = db_path_of(request)
    pr = parse_range(from_, to)
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
def api_coverage(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    db_path = db_path_of(request)
    pr = parse_range(from_, to)
    return {
        "range": range_payload(pr.start, pr.end),
        "tz": tz_name(),
        **_coverage_payload(db_path, pr.start, pr.end),
    }


# ---------------------------------------------------------------------------
# load tests and diagnostics
# ---------------------------------------------------------------------------

@router.get("/quality/load-tests")
def api_load_tests(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> dict[str, Any]:
    db_path = db_path_of(request)
    pr = parse_range(from_, to)
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
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    incident_id: int | None = Query(default=None),
) -> dict[str, Any]:
    db_path = db_path_of(request)
    pr = parse_range(from_, to)
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
def api_report_html(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> HTMLResponse:
    # Deferred: report.py reads this module's shared helpers, so importing it
    # at module level would close the loop.
    from . import report as report_module

    db_path = db_path_of(request)
    pr = parse_range(from_, to)
    model = report_module.build_report_model(
        db_path,
        pr.start,
        pr.end,
        app_version=str(getattr(request.app, "version", "dev")),
        now=utc_now(),
    )
    return HTMLResponse(report_module.render_report(model))
