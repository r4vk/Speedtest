"""Retention: pruning old data and estimating DB growth (design spec §14).

Order of operations in :func:`run_retention` is the whole safety story: raw
``probe_results`` rows are only ever deleted *after* every hourly/daily bucket
they back has an aggregate row computed from the still-complete raw data.
Nothing here assumes the continuous aggregation `quality_engine.py` already
does (spec §6.4) ran correctly — this module is self-sufficient and
re-aggregates whatever is about to lose its raw rows before touching them.

A subtlety worth spelling out: the raw cutoff used to decide *what to
aggregate* and *what to delete* is not the literal ``now - raw_days`` instant,
but that instant floored down to the start of its UTC day
(:func:`aggregates.bucket_start`). A day boundary is also always an hour
boundary, so flooring guarantees that a bucket (1h or 1d) is *never* partially
stripped of its raw rows while it still straddles the cutoff: without this,
a bucket that is only partially past the cutoff this run would lose the older
half of its raw rows now, and next run (once it finally looks "complete") its
aggregate would be silently recomputed from the surviving half only — wrong,
and indistinguishable from a fully-sampled bucket since it is still marked
``percentiles_from_raw = 1``. Flooring to a day means raw rows can survive up
to ~24h longer than ``retention_raw_days`` configures, never less — a
generous, not a leaky, retention window.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from . import aggregates, quality_db
from .db import checkpoint_wal, db_conn
from .probe_types import ProbeTarget
from .time_utils import parse_dt, to_iso_z, utc_now

log = logging.getLogger(__name__)

#: Settings keys read by :meth:`RetentionSettings.from_settings` (spec §14).
RETENTION_SETTINGS_KEYS: tuple[str, ...] = (
    "retention_raw_days",
    "retention_aggregate_days",
    "retention_incident_days",
    "retention_load_test_raw_days",
    "retention_diagnostics_days",
)

#: Buckets a raw row backs; both are re-aggregated before their raw data goes.
_BUCKETS: tuple[str, ...] = ("1h", "1d")

#: Sentinel lower bound for an open-ended "before this instant" range query.
#: Only used for string comparison against ISO-Z timestamps, never parsed.
_EPOCH_ISO = "0001-01-01T00:00:00.000Z"

#: How often `retention_loop` runs `run_retention`.
DEFAULT_INTERVAL_SECONDS = 3600.0
#: Delay before the very first run, so the engine's own aggregation settles.
DEFAULT_INITIAL_DELAY_SECONDS = 60.0


@dataclass(frozen=True)
class RetentionSettings:
    raw_days: int = 14
    aggregate_days: int = 365
    incident_days: int = 730
    load_test_raw_days: int = 90
    diagnostics_days: int = 365
    #: Rows deleted from `probe_results` per transaction; not a user setting.
    batch_size: int = 5000

    @staticmethod
    def from_settings(values: Mapping[str, str]) -> "RetentionSettings":
        """Build from the `settings` table; missing/invalid values fall back."""
        return RetentionSettings(
            raw_days=_positive_int(values.get("retention_raw_days"), 14),
            aggregate_days=_positive_int(values.get("retention_aggregate_days"), 365),
            incident_days=_positive_int(values.get("retention_incident_days"), 730),
            load_test_raw_days=_positive_int(values.get("retention_load_test_raw_days"), 90),
            diagnostics_days=_positive_int(values.get("retention_diagnostics_days"), 365),
        )


def _positive_int(raw: str | None, fallback: int) -> int:
    if raw is None:
        return fallback
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# run_retention
# ---------------------------------------------------------------------------


def run_retention(
    db_path: str,
    settings: RetentionSettings,
    *,
    now: datetime,
    targets: Sequence[ProbeTarget],
) -> dict[str, int]:
    """Prune everything past its retention window; returns counts per step.

    Safe to call repeatedly (idempotent): a second call with the same ``now``
    finds nothing left to delete and is a no-op on stored data.
    """
    now = _as_utc(now)
    now_iso = to_iso_z(now)

    raw_cutoff = now - timedelta(days=settings.raw_days)
    delete_cutoff = aggregates.bucket_start(raw_cutoff, "1d")
    delete_cutoff_iso = to_iso_z(delete_cutoff)
    # Buckets strictly before `delete_cutoff` are the ones about to lose every
    # one of their raw rows; the bucket starting exactly at `delete_cutoff`
    # keeps its raw rows this run and must not be marked.
    marked_before_iso = to_iso_z(delete_cutoff - timedelta(milliseconds=1))

    counts = {
        "raw_deleted": 0,
        "aggregates_marked_not_from_raw": 0,
        "aggregates_deleted": 0,
        "incidents_deleted": 0,
        "diagnostics_deleted": 0,
        "load_tests_raw_cleared": 0,
        "sessions_deleted": 0,
        "config_changes_deleted": 0,
    }

    # 1. Ensure aggregates exist for every bucket about to lose all its raw
    #    rows — computed from the still-complete raw data, before step 2.
    for target in targets:
        min_started_iso = _min_started_at_before(db_path, target.id, delete_cutoff_iso)
        if min_started_iso is None:
            continue
        start = parse_dt(min_started_iso)
        for bucket in _BUCKETS:
            aggregates.aggregate_range(
                db_path,
                target.id,
                str(target.protocol),
                bucket,
                start,
                delete_cutoff,
                now_iso=now_iso,
            )

    # 2. Delete the raw rows themselves, in short, batched transactions.
    counts["raw_deleted"] = _delete_probe_results_before(db_path, delete_cutoff_iso, settings.batch_size)

    # 3. Mark the buckets whose raw rows are now gone as such.
    for target in targets:
        for bucket in _BUCKETS:
            counts["aggregates_marked_not_from_raw"] += quality_db.mark_aggregates_not_from_raw(
                db_path, target.id, bucket, _EPOCH_ISO, marked_before_iso
            )

    # 4. Everything else, each by its own cutoff.
    aggregate_cutoff_iso = to_iso_z(now - timedelta(days=settings.aggregate_days))
    incident_cutoff_iso = to_iso_z(now - timedelta(days=settings.incident_days))
    diagnostics_cutoff_iso = to_iso_z(now - timedelta(days=settings.diagnostics_days))
    load_test_cutoff_iso = to_iso_z(now - timedelta(days=settings.load_test_raw_days))

    counts["aggregates_deleted"] = _delete_older_than(
        db_path, "probe_aggregates", "bucket_start", aggregate_cutoff_iso
    )
    counts["incidents_deleted"] = _delete_older_than(db_path, "incidents", "started_at", incident_cutoff_iso)
    counts["diagnostics_deleted"] = _delete_older_than(
        db_path, "diagnostics", "started_at", diagnostics_cutoff_iso
    )
    counts["load_tests_raw_cleared"] = _null_old_load_test_raw(db_path, load_test_cutoff_iso)
    # Sessions still open (`ended_at IS NULL`) are never deleted, however old
    # `started_at` is — an active session's own record must survive.
    counts["sessions_deleted"] = _delete_closed_sessions_before(db_path, incident_cutoff_iso)
    counts["config_changes_deleted"] = _delete_older_than(
        db_path, "config_changes", "changed_at", incident_cutoff_iso
    )

    # 5. Fold the WAL back into the main file after a (possibly large) prune.
    checkpoint_wal(db_path)

    return counts


def _min_started_at_before(db_path: str, target_id: int, cutoff_iso: str) -> str | None:
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT MIN(started_at) AS m FROM probe_results WHERE target_id = ? AND started_at < ?",
            (target_id, cutoff_iso),
        ).fetchone()
    value = row["m"] if row is not None else None
    return str(value) if value is not None else None


def _delete_probe_results_before(db_path: str, cutoff_iso: str, batch_size: int) -> int:
    """Delete `probe_results` older than `cutoff_iso`, a batch per transaction.

    Short transactions keep the write lock brief, so a multi-million-row
    prune never starves the probe scheduler's own periodic flush (spec §5).
    """
    deleted = 0
    while True:
        with db_conn(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """
                    DELETE FROM probe_results
                    WHERE id IN (SELECT id FROM probe_results WHERE started_at < ? LIMIT ?)
                    """,
                    (cutoff_iso, batch_size),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        deleted += cur.rowcount
        if cur.rowcount < batch_size:
            break
    return deleted


def _delete_older_than(db_path: str, table: str, column: str, cutoff_iso: str) -> int:
    """One-shot delete for tables that are never large enough to need batching."""
    with db_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff_iso,))  # noqa: S608
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return cur.rowcount


def _delete_closed_sessions_before(db_path: str, cutoff_iso: str) -> int:
    with db_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "DELETE FROM monitor_sessions WHERE ended_at IS NOT NULL AND started_at < ?",
                (cutoff_iso,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return cur.rowcount


def _null_old_load_test_raw(db_path: str, cutoff_iso: str) -> int:
    with db_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE load_tests SET raw_json = NULL WHERE raw_json IS NOT NULL AND started_at < ?",
                (cutoff_iso,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return cur.rowcount


# ---------------------------------------------------------------------------
# estimate_growth
# ---------------------------------------------------------------------------


def estimate_growth(
    db_path: str,
    *,
    now: datetime,
    raw_days: int | None = None,
) -> dict[str, Any]:
    """Rows/day and bytes/day, extrapolated from the last 24h (design spec §14).

    ``bytes_per_row_estimate`` divides the whole database file's on-disk size
    by the total number of `probe_results` rows ever stored — `probe_results`
    dominates the file (aggregates, incidents etc. are comparatively tiny), so
    this whole-DB average is a reasonable stand-in for "the size of one raw
    row" without a separate `dbstat` query. ``estimated_raw_bytes_at_retention``
    is only computed when `raw_days` is supplied (the `/api/quality/retention`
    endpoint passes the live setting); otherwise it is `None`.
    """
    now = _as_utc(now)
    day_ago_iso = to_iso_z(now - timedelta(hours=24))

    with db_conn(db_path) as conn:
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        probe_results_24h = int(
            conn.execute(
                "SELECT COUNT(*) FROM probe_results WHERE started_at >= ?", (day_ago_iso,)
            ).fetchone()[0]
        )
        # `computed_at` (when the aggregate row was written), not `bucket_start`
        # (which for a "1d" bucket names a day, not the moment it was stored):
        # this is a count of new rows, mirroring the other two tables' meaning.
        probe_aggregates_24h = int(
            conn.execute(
                "SELECT COUNT(*) FROM probe_aggregates WHERE computed_at >= ?", (day_ago_iso,)
            ).fetchone()[0]
        )
        incidents_24h = int(
            conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE started_at >= ?", (day_ago_iso,)
            ).fetchone()[0]
        )
        total_probe_results = int(conn.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0])

    db_bytes = page_count * page_size
    bytes_per_row_estimate = (db_bytes / total_probe_results) if total_probe_results > 0 else 0.0
    estimated_raw_bytes_per_day = bytes_per_row_estimate * probe_results_24h
    estimated_raw_bytes_at_retention = (
        estimated_raw_bytes_per_day * raw_days if raw_days is not None else None
    )

    return {
        "rows_24h": {
            "probe_results": probe_results_24h,
            "probe_aggregates": probe_aggregates_24h,
            "incidents": incidents_24h,
        },
        "db_bytes": db_bytes,
        "bytes_per_row_estimate": bytes_per_row_estimate,
        "estimated_raw_bytes_per_day": estimated_raw_bytes_per_day,
        "estimated_raw_bytes_at_retention": estimated_raw_bytes_at_retention,
    }


# ---------------------------------------------------------------------------
# retention_loop
# ---------------------------------------------------------------------------


def _run_once(
    db_path: str,
    settings_getter: Callable[[], RetentionSettings],
    targets_getter: Callable[[], Sequence[ProbeTarget]],
    now: datetime,
) -> dict[str, int]:
    settings = settings_getter()
    targets = targets_getter()
    counts = run_retention(db_path, settings, now=now, targets=targets)
    log.info("retention run complete: %s", counts)
    return counts


async def retention_loop(
    db_path: str,
    settings_getter: Callable[[], RetentionSettings],
    targets_getter: Callable[[], Sequence[ProbeTarget]],
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wall_clock: Callable[[], datetime] = utc_now,
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
) -> None:
    """Run `run_retention` hourly, first after `initial_delay_seconds` (spec §14).

    Started next to the telemetry loops in `main.py`'s lifespan. `settings_getter`
    and `targets_getter` are blocking (DB-bound), so each run happens in a worker
    thread; a failing run (a locked DB, a bad setting) is logged and never stops
    the loop — the monitor keeps collecting data even when housekeeping breaks.
    """
    await sleep(initial_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(_run_once, db_path, settings_getter, targets_getter, wall_clock())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Retention run failed")
        await sleep(interval_seconds)
