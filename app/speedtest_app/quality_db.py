"""Typed accessors for the schema v2 tables (design spec §3).

Thin persistence only: every function maps arguments to one SQL statement and
returns rows as ``dict`` (or ``ProbeTarget``/``ProbeResult``). Statistics,
incident rules and coverage logic live in their own modules.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .db import db_conn, streaming_conn
from .probe_types import ProbeResult, ProbeTarget
from .time_utils import to_iso_z, utc_now


def _now_iso() -> str:
    return to_iso_z(utc_now())


# Columns writable through the generic **fields accessors. Unknown names are
# rejected instead of being interpolated into SQL.
TARGET_COLUMNS = frozenset(
    {
        "name",
        "kind",
        "protocol",
        "host",
        "port",
        "interval_seconds",
        "timeout_ms",
        "enabled",
        "family_pref",
        "extra_json",
    }
)
INCIDENT_COLUMNS = frozenset(
    {
        "target_id",
        "protocol",
        "kind",
        "started_at",
        "ended_at",
        "closed_at",
        "close_reason",
        "window_seconds",
        "probe_interval_seconds",
        "peak_loss_pct",
        "peak_p95_rtt_ms",
        "longest_fail_streak",
        "windows_degraded",
        "summary_json",
        "expected",
        "expected_source",
        "expected_rule_id",
    }
)
#: Columns writable on an `expected_windows` rule row (design spec §7).
EXPECTED_WINDOW_COLUMNS = frozenset(
    {"name", "time_from", "time_to", "days", "target_id", "enabled", "note", "created_at"}
)
AGGREGATE_COLUMNS = frozenset(
    {
        "target_id",
        "protocol",
        "bucket",
        "bucket_start",
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
        "computed_at",
    }
)
LOAD_TEST_COLUMNS = frozenset(
    {
        "started_at",
        "ended_at",
        "kind",
        "direction",
        "server",
        "params_json",
        "status",
        "error",
        "result_json",
        "raw_json",
    }
)
DIAGNOSTIC_COLUMNS = frozenset(
    {
        "incident_id",
        "target_id",
        "tool",
        "started_at",
        "duration_ms",
        "status",
        "error",
        "result_json",
        "raw_output",
    }
)

PROBE_RESULT_COLUMNS: tuple[str, ...] = (
    "device_id",
    "target_id",
    "protocol",
    "started_at",
    "duration_ms",
    "outcome",
    "rtt_ms",
    "timeout_ms",
    "resolved_ip",
    "ip_family",
    "error_kind",
    "error_detail",
    "stages_json",
    "load_test_id",
    "external_id",
)


def _check_columns(fields: Mapping[str, Any], allowed: frozenset[str], table: str) -> None:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown {table} column(s): {', '.join(sorted(unknown))}")


def _insert(conn: sqlite3.Connection, table: str, fields: Mapping[str, Any], allowed: frozenset[str]) -> int:
    _check_columns(fields, allowed, table)
    if not fields:
        raise ValueError(f"no columns given for insert into {table}")
    columns = list(fields)
    placeholders = ",".join(["?"] * len(columns))
    cur = conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
        tuple(fields[c] for c in columns),
    )
    return int(cur.lastrowid)


def _update(
    conn: sqlite3.Connection,
    table: str,
    row_id: int,
    fields: Mapping[str, Any],
    allowed: frozenset[str],
) -> int:
    _check_columns(fields, allowed, table)
    if not fields:
        return 0
    assignments = ",".join(f"{c} = ?" for c in fields)
    cur = conn.execute(
        f"UPDATE {table} SET {assignments} WHERE id = ?",
        tuple(fields.values()) + (row_id,),
    )
    return cur.rowcount


def _dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# probe_targets
# ---------------------------------------------------------------------------

def _normalize_target_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out = dict(fields)
    if "extra" in out:
        extra = out.pop("extra")
        out["extra_json"] = json.dumps(extra) if extra else None
    if "protocol" in out and out["protocol"] is not None:
        out["protocol"] = str(out["protocol"])
    if "enabled" in out and out["enabled"] is not None:
        out["enabled"] = 1 if out["enabled"] else 0
    return out


def list_targets(db_path: str, enabled_only: bool = False) -> list[ProbeTarget]:
    where = " WHERE enabled = 1" if enabled_only else ""
    with db_conn(db_path) as conn:
        rows = conn.execute(f"SELECT * FROM probe_targets{where} ORDER BY id").fetchall()
    return [ProbeTarget.from_row(r) for r in rows]


def get_target(db_path: str, target_id: int) -> ProbeTarget | None:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM probe_targets WHERE id = ?", (target_id,)).fetchone()
    return ProbeTarget.from_row(row) if row is not None else None


def insert_target(db_path: str, now_iso: str | None = None, **fields: Any) -> ProbeTarget:
    now_iso = now_iso or _now_iso()
    values = _normalize_target_fields(fields)
    values.setdefault("family_pref", "auto")
    with db_conn(db_path) as conn:
        _check_columns(values, TARGET_COLUMNS, "probe_targets")
        values["created_at"] = now_iso
        values["updated_at"] = now_iso
        columns = list(values)
        placeholders = ",".join(["?"] * len(columns))
        cur = conn.execute(
            f"INSERT INTO probe_targets({','.join(columns)}) VALUES ({placeholders})",
            tuple(values[c] for c in columns),
        )
        row = conn.execute("SELECT * FROM probe_targets WHERE id = ?", (cur.lastrowid,)).fetchone()
    return ProbeTarget.from_row(row)


def update_target(db_path: str, target_id: int, now_iso: str | None = None, **fields: Any) -> ProbeTarget:
    values = _normalize_target_fields(fields)
    _check_columns(values, TARGET_COLUMNS, "probe_targets")
    values["updated_at"] = now_iso or _now_iso()
    with db_conn(db_path) as conn:
        assignments = ",".join(f"{c} = ?" for c in values)
        conn.execute(
            f"UPDATE probe_targets SET {assignments} WHERE id = ?",
            tuple(values.values()) + (target_id,),
        )
        row = conn.execute("SELECT * FROM probe_targets WHERE id = ?", (target_id,)).fetchone()
    if row is None:
        raise ValueError(f"no probe_target with id={target_id}")
    return ProbeTarget.from_row(row)


def delete_target(db_path: str, target_id: int) -> bool:
    with db_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM probe_targets WHERE id = ?", (target_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# probe_results
# ---------------------------------------------------------------------------

def insert_probe_results(db_path: str, rows: Sequence[ProbeResult]) -> int:
    """Insert a batch in one transaction; duplicate `external_id`s are skipped."""
    if not rows:
        return 0
    values = [tuple(r.to_row()[c] for c in PROBE_RESULT_COLUMNS) for r in rows]
    placeholders = ",".join(["?"] * len(PROBE_RESULT_COLUMNS))
    with db_conn(db_path) as conn:
        before = conn.total_changes
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                f"INSERT OR IGNORE INTO probe_results({','.join(PROBE_RESULT_COLUMNS)}) "
                f"VALUES ({placeholders})",
                values,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return conn.total_changes - before


#: The only columns `stats.compute_stats`, `stats.bucket_rows` and
#: `quality_views.error_kinds_histogram` read. Everything that turns raw rows
#: into numbers goes through those three, so the analytics endpoints select
#: this projection instead of the whole row: a probe row carries
#: `stages_json`, `error_detail` and eleven more columns that no statistic
#: looks at, and the panel materialises a full day of them per target, twice
#: per refresh. Only `probes.csv` needs the complete row, and it streams.
METRIC_COLUMNS = ("started_at", "outcome", "rtt_ms", "error_kind")


def _probe_results_query(
    start_iso: str,
    end_iso: str,
    target_id: int | None,
    protocol: str | None,
    device_id: str | None,
    columns: Sequence[str] = ("*",),
) -> tuple[str, tuple[Any, ...]]:
    """The one SELECT behind the list, the narrow and the streaming accessors.

    `query_probe_results`, `query_probe_metrics` and `iter_probe_results` must
    return exactly the same rows in exactly the same order, or a CSV export
    and the statistics built from the same range would disagree. Only the
    projection differs.
    """
    where = ["started_at >= ?", "started_at <= ?"]
    params: list[Any] = [start_iso, end_iso]
    if target_id is not None:
        where.append("target_id = ?")
        params.append(target_id)
    if protocol is not None:
        where.append("protocol = ?")
        params.append(str(protocol))
    if device_id is not None:
        where.append("device_id = ?")
        params.append(str(device_id))
    sql = (
        f"SELECT {', '.join(columns)} FROM probe_results WHERE {' AND '.join(where)} "
        "ORDER BY started_at ASC, id ASC"
    )
    return sql, tuple(params)


def query_probe_results(
    db_path: str,
    start_iso: str,
    end_iso: str,
    target_id: int | None = None,
    protocol: str | None = None,
    device_id: str | None = None,
) -> list[dict[str, Any]]:
    """Every matching raw row, materialised.

    Callers that only walk the rows once should prefer `iter_probe_results`:
    a wide range holds ~1 kB per row, so a whole day of the seeded targets is
    a few hundred megabytes. The view layer caps how wide a range may be
    served from raw rows at all (`quality_views.RAW_RANGE_MAX_DAYS`).
    """
    sql, params = _probe_results_query(start_iso, end_iso, target_id, protocol, device_id)
    with db_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return _dicts(rows)


def query_probe_metrics(
    db_path: str,
    start_iso: str,
    end_iso: str,
    target_id: int | None = None,
    protocol: str | None = None,
    device_id: str | None = None,
) -> list[dict[str, Any]]:
    """`query_probe_results` narrowed to :data:`METRIC_COLUMNS`.

    The same rows in the same order — only the columns no statistic reads are
    left in the database. Callers that need a whole row (the CSV export, which
    reproduces what was stored) must keep using the wide accessors.
    """
    sql, params = _probe_results_query(
        start_iso, end_iso, target_id, protocol, device_id, columns=METRIC_COLUMNS
    )
    with db_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return _dicts(rows)


def iter_probe_results(
    db_path: str,
    start_iso: str,
    end_iso: str,
    target_id: int | None = None,
    protocol: str | None = None,
    device_id: str | None = None,
    batch_size: int = 5000,
) -> Iterator[dict[str, Any]]:
    """The same rows as `query_probe_results`, streamed off the cursor.

    The generator holds one connection and one `fetchmany(batch_size)` page in
    memory, never the whole range, which is what lets `probes.csv` export a
    month of probes without the result set ever existing as a Python list
    (review finding C1a).

    It runs on a `streaming_conn`, because the server advances a streamed body
    from a thread pool with no thread affinity — the page after a yield may be
    fetched on a different worker than the one that opened the connection. The
    connection is closed when the rows run out, when the caller closes or
    abandons the generator (`GeneratorExit` unwinds the `with` below) and when
    the consumer raises, so no read connection is ever left holding the WAL.
    """
    sql, params = _probe_results_query(start_iso, end_iso, target_id, protocol, device_id)
    page = max(1, int(batch_size))
    with streaming_conn(db_path) as conn:
        cursor = conn.execute(sql, params)
        try:
            while True:
                rows = cursor.fetchmany(page)
                if not rows:
                    return
                for row in rows:
                    yield dict(row)
        finally:
            cursor.close()


#: One seek per target on `idx_probe_results_target_time`, which is ordered
#: `(target_id, started_at)` — exactly the shape this asks for. The previous
#: `ROW_NUMBER() OVER (PARTITION BY target_id ...)` over the bare table had no
#: `WHERE` at all, so `/api/targets` read and sorted every probe ever written:
#: ~10 s on a fortnight of one-second probes, and growing with the archive
#: rather than with the number of targets.
_LAST_RESULT_SQL = (
    "SELECT * FROM probe_results WHERE target_id = ? "
    "ORDER BY started_at DESC, id DESC LIMIT 1"
)


def last_result_plans(db_path: str) -> list[tuple[str, tuple[Any, ...]]]:
    """The statements `last_result_per_target` runs, for `EXPLAIN QUERY PLAN`.

    Exposed so the test suite can assert the plan stays a per-target index
    seek; nothing in the application calls it.
    """
    return [(_LAST_RESULT_SQL, (target.id,)) for target in list_targets(db_path)]


def last_result_per_target(db_path: str) -> dict[int, dict[str, Any]]:
    """The newest row of each target, keyed by target id.

    Targets that never produced a row are absent, as before.
    """
    out: dict[int, dict[str, Any]] = {}
    with db_conn(db_path) as conn:
        target_ids = [
            int(row["id"]) for row in conn.execute("SELECT id FROM probe_targets ORDER BY id")
        ]
        for target_id in target_ids:
            row = conn.execute(_LAST_RESULT_SQL, (target_id,)).fetchone()
            if row is not None:
                out[target_id] = dict(row)
    return out


def count_probe_results(db_path: str, start_iso: str, end_iso: str) -> int:
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM probe_results WHERE started_at >= ? AND started_at <= ?",
            (start_iso, end_iso),
        ).fetchone()
    return int(row["c"])


# ---------------------------------------------------------------------------
# monitor_sessions
# ---------------------------------------------------------------------------

def start_session(db_path: str, device_id: str, app_version: str, now_iso: str) -> int:
    with db_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO monitor_sessions(device_id, started_at, last_seen_at, app_version)
            VALUES (?,?,?,?)
            """,
            (device_id, now_iso, now_iso, app_version),
        )
        return int(cur.lastrowid)


def heartbeat_session(db_path: str, session_id: int, now_iso: str) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            "UPDATE monitor_sessions SET last_seen_at = ? WHERE id = ? AND ended_at IS NULL",
            (now_iso, session_id),
        )


def end_session(db_path: str, session_id: int, now_iso: str, reason: str) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE monitor_sessions
            SET ended_at = ?, end_reason = ?
            WHERE id = ? AND ended_at IS NULL
            """,
            (now_iso, reason, session_id),
        )


def close_stale_sessions(db_path: str) -> int:
    """Close sessions left open by an unclean stop: `ended_at = last_seen_at`."""
    with db_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE monitor_sessions
            SET ended_at = last_seen_at, end_reason = 'unclean'
            WHERE ended_at IS NULL
            """
        )
        return cur.rowcount


def query_sessions(db_path: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Sessions overlapping [start, end); open sessions are always included."""
    with db_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM monitor_sessions
            WHERE started_at < ?
              AND (ended_at IS NULL OR ended_at > ?)
            ORDER BY started_at ASC, id ASC
            """,
            (end_iso, start_iso),
        ).fetchall()
    return _dicts(rows)


def count_sessions(db_path: str) -> int:
    """Total number of monitor sessions ever recorded (coverage known / unknown)."""
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM monitor_sessions").fetchone()
    return int(row["c"])


# ---------------------------------------------------------------------------
# config_changes
# ---------------------------------------------------------------------------

def query_config_changes(db_path: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    with db_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM config_changes
            WHERE changed_at >= ? AND changed_at <= ?
            ORDER BY changed_at ASC, id ASC
            """,
            (start_iso, end_iso),
        ).fetchall()
    return _dicts(rows)


# ---------------------------------------------------------------------------
# incidents
# ---------------------------------------------------------------------------

def insert_incident(db_path: str, **fields: Any) -> int:
    with db_conn(db_path) as conn:
        return _insert(conn, "incidents", fields, INCIDENT_COLUMNS)


def update_incident(db_path: str, incident_id: int, **fields: Any) -> int:
    with db_conn(db_path) as conn:
        return _update(conn, "incidents", incident_id, fields, INCIDENT_COLUMNS)


def get_incident(db_path: str, incident_id: int) -> dict[str, Any] | None:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    return dict(row) if row is not None else None


#: `expected` query modes shared by the incident and connectivity queries.
EXPECTED_FILTERS = {"all", "exclude", "only"}


def expected_clause(expected: str) -> str | None:
    """SQL fragment for an `expected` filter, or `None` for "no filter"."""
    if expected == "exclude":
        return "expected = 0"
    if expected == "only":
        return "expected = 1"
    return None


def expected_mode(value: str | None) -> str:
    """`all` unless the caller clearly asked for something else.

    A typo must never hide an outage, so anything unrecognised reads as `all`.
    It lives here rather than in either API module because `api_quality.py`,
    `api_quality_exports.py`, `main.py` and the report all parse the same query
    parameter — one parser is the only way the three modes cannot drift apart
    between endpoints.
    """
    mode = (value or "all").strip().lower()
    return mode if mode in EXPECTED_FILTERS else "all"


def query_incidents(
    db_path: str,
    start_iso: str,
    end_iso: str,
    target_id: int | None = None,
    open_only: bool = False,
    expected: str = "all",
) -> list[dict[str, Any]]:
    """Incidents overlapping the range (an incident is open until `closed_at`).

    `expected` is `all` (default), `exclude` or `only`; an unknown value is
    read as `all`, so a typo in a query string can never hide an outage.
    """
    where = ["started_at < ?", "(ended_at IS NULL OR ended_at > ?)"]
    params: list[Any] = [end_iso, start_iso]
    if target_id is not None:
        where.append("target_id = ?")
        params.append(target_id)
    if open_only:
        where.append("closed_at IS NULL")
    clause = expected_clause(expected)
    if clause is not None:
        where.append(clause)
    with db_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM incidents WHERE {' AND '.join(where)} ORDER BY started_at ASC, id ASC",
            tuple(params),
        ).fetchall()
    return _dicts(rows)


def list_open_incidents(db_path: str) -> list[dict[str, Any]]:
    with db_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM incidents WHERE closed_at IS NULL ORDER BY started_at ASC, id ASC"
        ).fetchall()
    return _dicts(rows)


# ---------------------------------------------------------------------------
# probe_aggregates
# ---------------------------------------------------------------------------

def upsert_aggregate(db_path: str, row: Mapping[str, Any]) -> None:
    _check_columns(row, AGGREGATE_COLUMNS, "probe_aggregates")
    columns = list(row)
    placeholders = ",".join(["?"] * len(columns))
    updatable = [c for c in columns if c not in {"target_id", "bucket", "bucket_start"}]
    on_conflict = (
        f"DO UPDATE SET {','.join(f'{c} = excluded.{c}' for c in updatable)}"
        if updatable
        else "DO NOTHING"
    )
    with db_conn(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO probe_aggregates({','.join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(target_id, bucket, bucket_start) {on_conflict}
            """,
            tuple(row[c] for c in columns),
        )


def query_aggregates(
    db_path: str,
    bucket: str,
    start_iso: str,
    end_iso: str,
    target_id: int | None = None,
) -> list[dict[str, Any]]:
    where = ["bucket = ?", "bucket_start >= ?", "bucket_start <= ?"]
    params: list[Any] = [bucket, start_iso, end_iso]
    if target_id is not None:
        where.append("target_id = ?")
        params.append(target_id)
    with db_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM probe_aggregates WHERE {' AND '.join(where)} "
            "ORDER BY bucket_start ASC, target_id ASC",
            tuple(params),
        ).fetchall()
    return _dicts(rows)


def mark_aggregates_not_from_raw(
    db_path: str,
    target_id: int,
    bucket: str,
    start_iso: str,
    end_iso: str,
) -> int:
    with db_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE probe_aggregates SET percentiles_from_raw = 0
            WHERE target_id = ? AND bucket = ? AND bucket_start >= ? AND bucket_start <= ?
            """,
            (target_id, bucket, start_iso, end_iso),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# annotations
# ---------------------------------------------------------------------------

def insert_annotation(
    db_path: str,
    at_iso: str,
    label: str,
    note: str | None = None,
    incident_id: int | None = None,
    created_at: str | None = None,
) -> int:
    with db_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO annotations(created_at, at, label, note, incident_id, source)
            VALUES (?,?,?,?,?,'user')
            """,
            (created_at or _now_iso(), at_iso, label, note, incident_id),
        )
        return int(cur.lastrowid)


def delete_annotation(db_path: str, annotation_id: int) -> bool:
    with db_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
        return cur.rowcount > 0


def query_annotations(db_path: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    with db_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM annotations WHERE at >= ? AND at <= ? ORDER BY at ASC, id ASC",
            (start_iso, end_iso),
        ).fetchall()
    return _dicts(rows)


# ---------------------------------------------------------------------------
# load_tests
# ---------------------------------------------------------------------------

def insert_load_test(db_path: str, **fields: Any) -> int:
    with db_conn(db_path) as conn:
        return _insert(conn, "load_tests", fields, LOAD_TEST_COLUMNS)


def update_load_test(db_path: str, load_test_id: int, **fields: Any) -> int:
    with db_conn(db_path) as conn:
        return _update(conn, "load_tests", load_test_id, fields, LOAD_TEST_COLUMNS)


def get_load_test(db_path: str, load_test_id: int) -> dict[str, Any] | None:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM load_tests WHERE id = ?", (load_test_id,)).fetchone()
    return dict(row) if row is not None else None


def close_running_load_tests(db_path: str, now_iso: str, error: str) -> int:
    """Close every `running` row: the recovery after an unclean stop."""
    with db_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE load_tests
            SET status = 'error', error = ?, ended_at = COALESCE(ended_at, ?)
            WHERE status = 'running'
            """,
            (error, now_iso),
        )
        return cur.rowcount


def query_load_tests(db_path: str, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Load tests overlapping the range (a running test has `ended_at IS NULL`)."""
    with db_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM load_tests
            WHERE started_at < ?
              AND (ended_at IS NULL OR ended_at > ?)
            ORDER BY started_at ASC, id ASC
            """,
            (end_iso, start_iso),
        ).fetchall()
    return _dicts(rows)


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

def insert_diagnostic(db_path: str, **fields: Any) -> int:
    with db_conn(db_path) as conn:
        return _insert(conn, "diagnostics", fields, DIAGNOSTIC_COLUMNS)


def query_diagnostics(
    db_path: str,
    start_iso: str | None = None,
    end_iso: str | None = None,
    incident_id: int | None = None,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if start_iso is not None:
        where.append("started_at >= ?")
        params.append(start_iso)
    if end_iso is not None:
        where.append("started_at <= ?")
        params.append(end_iso)
    if incident_id is not None:
        where.append("incident_id = ?")
        params.append(incident_id)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with db_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM diagnostics{clause} ORDER BY started_at ASC, id ASC",
            tuple(params),
        ).fetchall()
    return _dicts(rows)


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------

def get_device(db_path: str, device_id: str) -> dict[str, Any] | None:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return dict(row) if row is not None else None


def list_devices(db_path: str) -> list[dict[str, Any]]:
    with db_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
    return _dicts(rows)


def upsert_device(
    db_path: str,
    device_id: str,
    name: str,
    kind: str,
    token_hash: str | None = None,
    last_seen_at: str | None = None,
    last_skew_ms: float | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    now_iso = now_iso or _now_iso()
    with db_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO devices(id, name, kind, token_hash, created_at, last_seen_at, last_skew_ms)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name = excluded.name,
              kind = excluded.kind,
              token_hash = COALESCE(excluded.token_hash, devices.token_hash),
              last_seen_at = COALESCE(excluded.last_seen_at, devices.last_seen_at),
              last_skew_ms = COALESCE(excluded.last_skew_ms, devices.last_skew_ms)
            """,
            (device_id, name, kind, token_hash, now_iso, last_seen_at, last_skew_ms),
        )
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# expected_windows (design spec §7): recurring maintenance rules.
#
# A rule is configuration, not a measurement: `retention.py` never touches
# this table (spec §11), and nothing here recomputes past `incidents` or
# `connectivity_periods` rows — the verdict a rule produced at close time is
# permanent (spec's "no retroactive rewrite").
# ---------------------------------------------------------------------------

def list_expected_windows(db_path: str, enabled_only: bool = False) -> list[dict[str, Any]]:
    where = " WHERE enabled = 1" if enabled_only else ""
    with db_conn(db_path) as conn:
        rows = conn.execute(f"SELECT * FROM expected_windows{where} ORDER BY id").fetchall()
    return _dicts(rows)


def get_expected_window(db_path: str, window_id: int) -> dict[str, Any] | None:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM expected_windows WHERE id = ?", (window_id,)).fetchone()
    return dict(row) if row is not None else None


def insert_expected_window(db_path: str, now_iso: str | None = None, **fields: Any) -> int:
    fields.setdefault("created_at", now_iso or _now_iso())
    fields.setdefault("enabled", 1)
    with db_conn(db_path) as conn:
        return _insert(conn, "expected_windows", fields, EXPECTED_WINDOW_COLUMNS)


def update_expected_window(db_path: str, window_id: int, **fields: Any) -> int:
    with db_conn(db_path) as conn:
        return _update(conn, "expected_windows", window_id, fields, EXPECTED_WINDOW_COLUMNS)


def delete_expected_window(db_path: str, window_id: int) -> bool:
    with db_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM expected_windows WHERE id = ?", (window_id,))
    return cur.rowcount > 0
