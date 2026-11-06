"""Routes for registering devices and uploading their results (spec §15, stage 9).

- ``POST /api/devices`` registers a device and returns its token once.
- ``GET /api/devices`` lists devices without their token hashes.
- ``DELETE /api/devices/{id}`` removes a device (never the NAS) and so revokes its token.
- ``POST /api/ingest/results`` takes ``Authorization: Bearer <token>`` and a batch.
- ``GET /api/quality/compare?device=<id>`` puts a device's results next to the NAS's,
  per shared target, on the same buckets (raw rows only).
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal, Mapping

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import ingest, quality_db
from .probe_scheduler import BufferFullError, ProbeScheduler
from .quality_views import (
    db_path_of,
    get_range,
    local_iso,
    range_payload,
    raw_range_allowed,
    stats_payload,
    target_payload,
    timeline_bucket_seconds,
    tz_name,
)
from .stats import stats_and_buckets
from .time_utils import ParsedRange, to_iso_z

router = APIRouter(prefix="/api")


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["macos", "other"] = "macos"
    id: str | None = Field(default=None, max_length=40)


def device_payload(row: Mapping[str, Any], db_path: str | None = None) -> dict[str, Any]:
    """A device as the panel shows it; an upload not yet flushed counts as seen."""
    last_seen, skew = row.get("last_seen_at"), row.get("last_skew_ms")
    pending = ingest.pending_seen(db_path, str(row["id"])) if db_path else None
    if pending is not None:
        last_seen, skew = pending
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "created_at": local_iso(row.get("created_at")),
        "last_seen_at": local_iso(last_seen),
        "last_skew_ms": skew,
        "can_ingest": row["kind"] != "nas" and bool(row.get("token_hash")),
    }


@router.get("/devices")
def api_list_devices(request: Request) -> dict[str, Any]:
    db_path = db_path_of(request)
    return {"items": [device_payload(row, db_path) for row in quality_db.list_devices(db_path)]}


@router.post("/devices", status_code=201)
def api_create_device(request: Request, body: DeviceCreate) -> dict[str, Any]:
    try:
        device, token = ingest.register_device(db_path_of(request), body.name, body.kind, body.id)
    except ingest.IngestError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"device": device_payload(device), "token": token}


@router.delete("/devices/{device_id}")
def api_delete_device(request: Request, device_id: str) -> dict[str, Any]:
    if device_id == quality_db.NAS_DEVICE_ID:
        raise HTTPException(status_code=400, detail="nie można usunąć NAS-a")
    if not quality_db.delete_device(db_path_of(request), device_id):
        raise HTTPException(status_code=404, detail="nie ma takiego urządzenia")
    return {"deleted": device_id}


@router.post("/ingest/results")
async def api_ingest_results(
    request: Request,
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Validate in a worker thread, then hand the new rows to the probe buffer.

    Buffering touches the scheduler's deque, which belongs to the event loop,
    so this handler is async and only the database reads run in a thread.
    """
    db_path = db_path_of(request)
    device = await asyncio.to_thread(ingest.authenticate, db_path, authorization)
    if device is None:
        raise HTTPException(
            status_code=401, detail="brak lub zły token", headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        upload = await asyncio.to_thread(ingest.prepare_upload, db_path, device, body)
    except ingest.IngestError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    scheduler = _running_scheduler(request)
    if scheduler is None:
        outcome = await asyncio.to_thread(ingest.store_upload_now, db_path, upload)
        return {"device_id": device["id"], **outcome.payload()}

    pending = scheduler.pending_external_ids(str(device["id"]))
    rows = [row for row in upload.rows if row.external_id not in pending]
    try:
        scheduler.enqueue_external(rows)
    except BufferFullError as exc:
        raise HTTPException(
            status_code=503,
            detail="bufor zapisu jest pełny, spróbuj później",
            headers={"Retry-After": "60"},
        ) from exc
    upload.outcome.accepted = len(rows)
    upload.outcome.duplicates += len(upload.rows) - len(rows)
    ingest.remember_seen(db_path, upload)
    return {"device_id": device["id"], **upload.outcome.payload()}


def _running_scheduler(request: Request) -> ProbeScheduler | None:
    engine = getattr(request.app.state, "quality_engine", None)
    scheduler = getattr(engine, "scheduler", None)
    return scheduler if isinstance(scheduler, ProbeScheduler) and scheduler.running else None


def _side(rows: list[dict[str, Any]], bucket: float, pr: ParsedRange) -> dict[str, Any]:
    stats, points = stats_and_buckets(rows, bucket, pr.start, pr.end)
    return {
        "stats": stats_payload(stats),
        "points": [{**point, "t": local_iso(point["t"])} for point in points],
    }


@router.get("/quality/compare")
def api_quality_compare(
    request: Request,
    device: str = Query(min_length=1, max_length=40),
    pr: ParsedRange = Depends(get_range),
    bucket_seconds: float = Query(default=60.0, gt=0, le=86400),
) -> dict[str, Any]:
    """The NAS (cable) and ``device`` (e.g. a Mac on Wi-Fi) side by side.

    Only targets the device has results for in the range are listed. Both
    sides use the same buckets, so a loss burst on the device with a clean NAS
    side points at the device's own link; a burst on both points past the
    router. Clocks are not corrected: ``device.last_skew_ms`` says how far the
    device's clock was off at its last upload.
    """
    db_path = db_path_of(request)
    row = quality_db.get_device(db_path, device)
    if row is None or row["kind"] == "nas":
        raise HTTPException(status_code=404, detail="nie ma takiego urządzenia")
    if not raw_range_allowed(pr.start, pr.end):
        raise HTTPException(status_code=400, detail="za szeroki zakres dla surowych danych")
    bucket = timeline_bucket_seconds(bucket_seconds, pr.start, pr.end)
    start_iso, end_iso = to_iso_z(pr.start), to_iso_z(pr.end)
    series = []
    for target in quality_db.list_targets(db_path):
        theirs = quality_db.query_probe_metrics(
            db_path, start_iso, end_iso, target_id=target.id, device_id=device
        )
        if not theirs:
            continue
        ours = quality_db.query_probe_metrics(db_path, start_iso, end_iso, target_id=target.id)
        series.append(
            {
                "target": target_payload(target),
                "nas": _side(ours, bucket, pr),
                "device": _side(theirs, bucket, pr),
            }
        )
    return {
        "range": range_payload(pr.start, pr.end),
        "tz": tz_name(),
        "bucket_seconds": bucket,
        "device": device_payload(row, db_path),
        "targets": series,
    }
