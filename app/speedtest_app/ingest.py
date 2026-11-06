"""Authenticated, idempotent import of probe results from other devices (spec §15, stage 9).

A device (for example the NetLog agent on a Mac) is registered once and gets a
token, shown only at creation and stored as its sha256. Uploaded results are
matched to the NAS's targets by name, or by protocol and host when the device
names its targets differently (NetLog's ``internet`` is the NAS's
``cloudflare-dns``). Results for targets the NAS does not know are rejected,
never turned into new targets: a new target would make the NAS probe it too.

Re-sending a batch is safe: ``(device_id, external_id)`` is unique, so rows
already stored or still waiting in memory count as duplicates.

Uploads are not written on arrival. They join the probe scheduler's
in-memory buffer and reach SQLite with the NAS's own rows, every
``PROBE_FLUSH_SECONDS``, so a device uploading every few minutes does not wake
the disk more often than the NAS itself. The device's "last seen" is kept in
memory too and written after each flush. A buffer that cannot take an upload
answers 503 and the device retries later; rows already acknowledged are lost
only on an abrupt stop, exactly like the NAS's own buffered rows.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from . import quality_db
from .probe_types import Outcome, ProbeResult, ProbeTarget, Protocol
from .time_utils import parse_dt, to_iso_z, utc_now

#: Upper bound of one upload; NetLog sends a few hundred rows at a time.
MAX_BATCH = 5000
_MAX_ERROR_DETAIL = 200
_MAX_TEXT = 200
_DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
DEVICE_KINDS = ("macos", "other")


class IngestError(ValueError):
    """The request as a whole cannot be accepted; ``status`` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def slugify_device_id(name: str) -> str:
    """``MacBook Rafała`` -> ``macbook-rafala``."""
    folded = unicodedata.normalize("NFKD", name.strip().lower().replace("ł", "l"))
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")[:40].strip("-")
    return slug or "device"


def register_device(
    db_path: str, name: str, kind: str = "macos", device_id: str | None = None
) -> tuple[dict[str, Any], str]:
    """Create a device and return it with its token (the only time the token is visible)."""
    name = name.strip()
    if not name or len(name) > 80:
        raise IngestError("nazwa urządzenia musi mieć od 1 do 80 znaków")
    if kind not in DEVICE_KINDS:
        raise IngestError(f"nieznany rodzaj urządzenia: {kind}")
    wanted = (device_id or slugify_device_id(name)).strip().lower()
    if not _DEVICE_ID_RE.match(wanted) or wanted == quality_db.NAS_DEVICE_ID:
        raise IngestError("identyfikator urządzenia: małe litery, cyfry i '-', nie 'nas'")
    final_id, n = wanted, 2
    while quality_db.get_device(db_path, final_id) is not None:
        if device_id:
            raise IngestError(f"urządzenie {final_id} już istnieje", status=409)
        final_id = f"{wanted[:36]}-{n}"
        n += 1
    token = new_token()
    device = quality_db.upsert_device(db_path, final_id, name, kind, token_hash=hash_token(token))
    return device, token


def authenticate(db_path: str, authorization: str | None) -> dict[str, Any] | None:
    """The device for a ``Bearer <token>`` header, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return quality_db.get_device_by_token_hash(db_path, hash_token(token.strip()))


@dataclass
class IngestOutcome:
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    skew_ms: float | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "skew_ms": self.skew_ms,
        }


class _TargetResolver:
    """Name first, then (protocol, host); one lookup table per request."""

    def __init__(self, targets: Sequence[ProbeTarget]) -> None:
        self._by_name = {(t.name, str(t.protocol)): t for t in targets}
        self._by_host: dict[tuple[str, str], ProbeTarget] = {}
        for t in targets:
            self._by_host.setdefault((str(t.protocol), t.host.strip().lower()), t)

    def resolve(self, name: str, protocol: str, host: str | None) -> ProbeTarget | None:
        target = self._by_name.get((name, protocol))
        if target is None and host:
            target = self._by_host.get((protocol, host.strip().lower()))
        return target


def _text(value: Any, limit: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _parse_result(
    device_id: str, item: Any, resolver: _TargetResolver
) -> tuple[ProbeResult | None, str | None]:
    """A validated ProbeResult, or the reason it was rejected."""
    if not isinstance(item, Mapping):
        return None, "invalid"
    external_id = _text(item.get("external_id"))
    if not external_id:
        return None, "missing_external_id"
    try:
        protocol = Protocol(str(item.get("protocol") or "icmp"))
        outcome = Outcome(str(item.get("outcome")))
        started_at = to_iso_z(parse_dt(str(item["started_at"])))
        duration_ms = max(0.0, float(item["duration_ms"]))
        timeout_ms = int(item["timeout_ms"])
        rtt_raw = item.get("rtt_ms")
        rtt_ms = float(rtt_raw) if rtt_raw is not None else None
        family_raw = item.get("ip_family")
        ip_family = int(family_raw) if family_raw is not None else None
    except (KeyError, TypeError, ValueError):
        return None, "invalid"
    if ip_family not in (None, 4, 6) or timeout_ms <= 0:
        return None, "invalid"
    if outcome is Outcome.OK and rtt_ms is None:
        return None, "invalid"
    if outcome is not Outcome.OK:
        rtt_ms = None
    target = resolver.resolve(str(item.get("target") or ""), str(protocol), _text(item.get("host")))
    if target is None:
        return None, "unknown_target"
    return (
        ProbeResult(
            target_id=target.id,
            protocol=protocol,
            started_at=started_at,
            duration_ms=duration_ms,
            outcome=outcome,
            timeout_ms=timeout_ms,
            rtt_ms=rtt_ms,
            resolved_ip=_text(item.get("resolved_ip"), 64),
            ip_family=ip_family,
            error_kind=_text(item.get("error_kind"), 64),
            error_detail=_text(item.get("error_detail"), _MAX_ERROR_DETAIL),
            device_id=device_id,
            external_id=external_id,
        ),
        None,
    )


@dataclass
class PreparedUpload:
    """A validated upload: new rows to store, plus counters for the response."""

    device: Mapping[str, Any]
    rows: list[ProbeResult]
    outcome: IngestOutcome
    seen_at: str


def prepare_upload(
    db_path: str,
    device: Mapping[str, Any],
    body: Mapping[str, Any],
    now: datetime | None = None,
) -> PreparedUpload:
    """Validate ``body`` and drop rows already stored; reads the database, never writes it."""
    device_id = str(device["id"])
    if str(body.get("device_id") or device_id) != device_id:
        raise IngestError("device_id nie zgadza się z tokenem", status=403)
    items = body.get("results")
    if not isinstance(items, list):
        raise IngestError("pole results musi być listą")
    if len(items) > MAX_BATCH:
        raise IngestError(f"najwyżej {MAX_BATCH} wyników w jednej paczce", status=413)

    now = now or utc_now()
    outcome = IngestOutcome()
    try:
        sent_at = parse_dt(str(body["sent_at"]))
        outcome.skew_ms = round((now - sent_at).total_seconds() * 1000.0, 1)
    except (KeyError, TypeError, ValueError):
        outcome.skew_ms = None

    resolver = _TargetResolver(quality_db.list_targets(db_path))
    valid: list[ProbeResult] = []
    seen: set[str] = set()
    for item in items:
        result, reason = _parse_result(device_id, item, resolver)
        if result is None:
            ext = item.get("external_id") if isinstance(item, Mapping) else None
            outcome.rejected.append({"external_id": ext, "reason": reason})
            continue
        if result.external_id in seen:
            outcome.duplicates += 1
            continue
        seen.add(result.external_id)  # type: ignore[arg-type]
        valid.append(result)

    stored = quality_db.existing_external_ids(db_path, device_id, [r.external_id for r in valid])
    fresh = [r for r in valid if r.external_id not in stored]
    outcome.duplicates += len(valid) - len(fresh)
    return PreparedUpload(device=device, rows=fresh, outcome=outcome, seen_at=to_iso_z(now))


# -- "last seen" of devices, written together with the buffered rows ---------

_seen_lock = threading.Lock()
_pending_seen: dict[tuple[str, str], tuple[Mapping[str, Any], str, float | None]] = {}


def remember_seen(db_path: str, upload: PreparedUpload) -> None:
    """Keep the device's last upload in memory until the next buffer flush."""
    device = upload.device
    with _seen_lock:
        _pending_seen[(db_path, str(device["id"]))] = (device, upload.seen_at, upload.outcome.skew_ms)


def pending_seen(db_path: str, device_id: str) -> tuple[str, float | None] | None:
    with _seen_lock:
        hit = _pending_seen.get((db_path, device_id))
    return (hit[1], hit[2]) if hit is not None else None


def write_pending_seen(db_path: str) -> None:
    """Persist remembered uploads; registered as the scheduler's after-flush callback."""
    with _seen_lock:
        mine = {k: v for k, v in _pending_seen.items() if k[0] == db_path}
        for key in mine:
            del _pending_seen[key]
    for (_, device_id), (device, seen_at, skew_ms) in mine.items():
        quality_db.upsert_device(
            db_path, device_id, str(device["name"]), str(device["kind"]),
            last_seen_at=seen_at, last_skew_ms=skew_ms,
        )


def store_upload_now(db_path: str, upload: PreparedUpload) -> IngestOutcome:
    """Write directly, for when no probe scheduler is running to buffer the rows."""
    written = quality_db.insert_probe_results(db_path, upload.rows) if upload.rows else 0
    upload.outcome.accepted = written
    upload.outcome.duplicates += len(upload.rows) - written
    remember_seen(db_path, upload)
    write_pending_seen(db_path)
    return upload.outcome
