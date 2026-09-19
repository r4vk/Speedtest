"""SQLite schema, migrations, legacy accessors, settings and the config change log.

Schema v1 is the legacy monitoring database (connectivity checks + speed tests);
schema v2 adds the network quality model (design spec §3). ``ensure_db`` creates
the v1 tables and then migrates forward idempotently.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from . import connectivity
from .config import AppConfig
from .time_utils import to_iso_z

log = logging.getLogger(__name__)


SCHEMA_VERSION = 2

#: Version created by the legacy DDL below; everything above it is a migration.
LEGACY_SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    """Current UTC time in the single storage format of spec §1 (millisecond ISO-Z)."""
    return to_iso_z(datetime.now(timezone.utc))


def _parse_utc_iso(value: str) -> datetime:
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _connect(db_path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path, timeout=30, isolation_level=None, check_same_thread=check_same_thread
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # Belt-and-braces on top of `timeout=` above: a writer waits up to 30s for
    # another writer's transaction instead of failing immediately with
    # "database is locked" (design spec §14 write robustness).
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


@contextmanager
def db_conn(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def streaming_conn(db_path: str) -> Iterator[sqlite3.Connection]:
    """A read connection for a cursor a *generator* is walked with.

    Starlette drives the body iterator of a `StreamingResponse` returned from
    a sync route through `iterate_in_threadpool`, which has no thread
    affinity: the `fetchmany` after a yield can land on a different worker
    than the one that opened the connection. With the default
    ``check_same_thread=True`` that raises `sqlite3.ProgrammingError` in the
    middle of a 200 response — a silently truncated download — and then again
    in the `finally` that should have closed the connection, leaking it (and
    with it the WAL checkpoint).

    The check is safe to drop here because the generator is advanced one
    `next()` at a time: the access is serialised even when the thread behind
    it changes. It is *not* a licence to share the connection between
    concurrent callers.
    """
    conn = _connect(db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def ensure_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with db_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS connectivity_periods (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              ended_at TEXT NULL,
              is_up INTEGER NOT NULL CHECK (is_up IN (0,1))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS connectivity_checks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              checked_at TEXT NOT NULL,
              is_up INTEGER NOT NULL CHECK (is_up IN (0,1)),
              latency_ms REAL NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_connectivity_checks_checked_at ON connectivity_checks(checked_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS speed_tests (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              duration_seconds REAL NOT NULL,
              bytes_downloaded INTEGER NOT NULL,
              mbps REAL NOT NULL,
              error TEXT NULL
            )
            """
        )
        _ensure_speed_tests_columns(conn)
        _ensure_blocked_periods_table(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blocked_periods (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              test_type TEXT NOT NULL CHECK (test_type IN ('ping', 'speed')),
              started_at TEXT NOT NULL,
              ended_at TEXT NULL,
              reason TEXT NOT NULL CHECK (reason IN ('disabled', 'schedule'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocked_periods_started_at ON blocked_periods(started_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocked_periods_test_type ON blocked_periods(test_type)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(LEGACY_SCHEMA_VERSION),),
        )
        _migrate(conn)


def _ensure_speed_tests_columns(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(speed_tests)").fetchall()}
    desired: list[tuple[str, str]] = [
        ("speedtest_mode", "TEXT"),
        ("upload_mbps", "REAL"),
        ("ping_ms", "REAL"),
        ("server_name", "TEXT"),
        ("server_country", "TEXT"),
    ]
    for name, ctype in desired:
        if name in cols:
            continue
        conn.execute(f"ALTER TABLE speed_tests ADD COLUMN {name} {ctype} NULL")


def _ensure_blocked_periods_table(conn: sqlite3.Connection) -> None:
    """Ensure blocked_periods table exists (migration for existing DBs)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blocked_periods (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          test_type TEXT NOT NULL CHECK (test_type IN ('ping', 'speed')),
          started_at TEXT NOT NULL,
          ended_at TEXT NULL,
          reason TEXT NOT NULL CHECK (reason IN ('disabled', 'schedule'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_blocked_periods_started_at ON blocked_periods(started_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_blocked_periods_test_type ON blocked_periods(test_type)"
    )


# ---------------------------------------------------------------------------
# Schema v2 — network quality model (design spec §3)
# ---------------------------------------------------------------------------

SCHEMA_V2_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS probe_targets (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('gateway','internet','dns','https','tcp')),
      protocol TEXT NOT NULL CHECK (protocol IN ('icmp','tcp','dns','https')),
      host TEXT NOT NULL,
      port INTEGER NULL,
      interval_seconds REAL NOT NULL,
      timeout_ms INTEGER NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
      family_pref TEXT NOT NULL DEFAULT 'auto' CHECK (family_pref IN ('auto','ipv4','ipv6')),
      extra_json TEXT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS probe_results (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      device_id TEXT NOT NULL DEFAULT 'nas',
      target_id INTEGER NOT NULL REFERENCES probe_targets(id) ON DELETE CASCADE,
      protocol TEXT NOT NULL,
      started_at TEXT NOT NULL,
      duration_ms REAL NOT NULL,
      outcome TEXT NOT NULL CHECK (outcome IN ('ok','timeout','error')),
      rtt_ms REAL NULL,
      timeout_ms INTEGER NOT NULL,
      resolved_ip TEXT NULL,
      ip_family INTEGER NULL CHECK (ip_family IN (4,6)),
      error_kind TEXT NULL,
      error_detail TEXT NULL,
      stages_json TEXT NULL,
      load_test_id INTEGER NULL,
      external_id TEXT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_probe_results_target_time ON probe_results(target_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_probe_results_time ON probe_results(started_at)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_probe_results_external ON probe_results(device_id, external_id)
      WHERE external_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS monitor_sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      device_id TEXT NOT NULL DEFAULT 'nas',
      started_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      ended_at TEXT NULL,
      end_reason TEXT NULL CHECK (end_reason IN ('shutdown','unclean')),
      app_version TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS config_changes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      changed_at TEXT NOT NULL,
      key TEXT NOT NULL,
      old_value TEXT NULL,
      new_value TEXT NULL,
      source TEXT NOT NULL CHECK (source IN ('ui','env','migration','api'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS incidents (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      target_id INTEGER NOT NULL REFERENCES probe_targets(id) ON DELETE CASCADE,
      protocol TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('outage','degraded')),
      started_at TEXT NOT NULL,
      ended_at TEXT NULL,
      closed_at TEXT NULL,
      close_reason TEXT NULL CHECK (close_reason IN ('recovered','no_data','shutdown')),
      window_seconds INTEGER NOT NULL,
      probe_interval_seconds REAL NOT NULL,
      peak_loss_pct REAL NULL,
      peak_p95_rtt_ms REAL NULL,
      longest_fail_streak INTEGER NULL,
      windows_degraded INTEGER NOT NULL DEFAULT 0,
      summary_json TEXT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_incidents_time ON incidents(started_at)",
    """
    CREATE TABLE IF NOT EXISTS probe_aggregates (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      target_id INTEGER NOT NULL REFERENCES probe_targets(id) ON DELETE CASCADE,
      protocol TEXT NOT NULL,
      bucket TEXT NOT NULL CHECK (bucket IN ('1h','1d')),
      bucket_start TEXT NOT NULL,
      attempts INTEGER NOT NULL,
      ok_count INTEGER NOT NULL,
      timeout_count INTEGER NOT NULL,
      error_count INTEGER NOT NULL,
      loss_pct REAL NULL,
      rtt_min_ms REAL NULL, rtt_p50_ms REAL NULL, rtt_p95_ms REAL NULL, rtt_p99_ms REAL NULL,
      rtt_max_ms REAL NULL, rtt_mean_ms REAL NULL, rtt_variation_ms REAL NULL,
      longest_fail_streak INTEGER NULL,
      percentiles_from_raw INTEGER NOT NULL DEFAULT 1,
      computed_at TEXT NOT NULL,
      UNIQUE(target_id, bucket, bucket_start)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS annotations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT NOT NULL,
      at TEXT NOT NULL,
      label TEXT NOT NULL,
      note TEXT NULL,
      incident_id INTEGER NULL REFERENCES incidents(id) ON DELETE SET NULL,
      source TEXT NOT NULL DEFAULT 'user'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS load_tests (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at TEXT NOT NULL,
      ended_at TEXT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('iperf_udp','iperf_tcp','speedtest')),
      direction TEXT NOT NULL CHECK (direction IN ('upload','download','both')),
      server TEXT NULL,
      params_json TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('running','ok','error','skipped')),
      error TEXT NULL,
      result_json TEXT NULL,
      raw_json TEXT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS diagnostics (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      incident_id INTEGER NULL REFERENCES incidents(id) ON DELETE SET NULL,
      target_id INTEGER NULL REFERENCES probe_targets(id) ON DELETE SET NULL,
      tool TEXT NOT NULL,
      started_at TEXT NOT NULL,
      duration_ms REAL NULL,
      status TEXT NOT NULL CHECK (status IN ('ok','error','timeout')),
      error TEXT NULL,
      result_json TEXT NULL,
      raw_output TEXT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS devices (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('nas','macos','other')),
      token_hash TEXT NULL,
      created_at TEXT NOT NULL,
      last_seen_at TEXT NULL,
      last_skew_ms REAL NULL
    )
    """,
)


def _read_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return LEGACY_SCHEMA_VERSION
    try:
        return int(str(row["value"]).strip())
    except ValueError:
        return LEGACY_SCHEMA_VERSION


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations; each one runs in a single transaction."""
    version = _read_schema_version(conn)
    if version >= SCHEMA_VERSION:
        return
    if version < 2:
        conn.execute("BEGIN")
        try:
            _migrate_1_to_2(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    now_iso = _utc_now_iso()
    for statement in SCHEMA_V2_DDL:
        conn.execute(statement)
    conn.execute(
        "INSERT OR IGNORE INTO devices(id, name, kind, created_at) VALUES ('nas','NAS (kabel)','nas',?)",
        (now_iso,),
    )
    _seed_probe_targets(conn, now_iso)
    conn.execute(
        """
        INSERT INTO config_changes(changed_at, key, old_value, new_value, source)
        VALUES (?,?,?,?,?)
        """,
        (now_iso, "schema_version", "1", "2", "migration"),
    )
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('schema_version','2')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """
    )


def _float_or(raw: str | None, fallback: float) -> float:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _int_or(raw: str | None, fallback: int) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _seed_gateway_host() -> str:
    """`GATEWAY_HOST` for the seeded `gateway` target, validated (finding I7).

    The seeded host used to be taken verbatim from the environment, which is
    the one path into `probe_targets.host` that skips the API's validation —
    and that host is later spawned as an mtr argument. An unusable value seeds
    an empty, disabled target with a warning instead of a silently broken one.
    """
    raw = (os.getenv("GATEWAY_HOST") or "").strip()
    if not raw:
        return ""
    # Deferred: `network_tools` pulls in httpx/dnspython, and `db` is imported
    # by everything, including the tests that never touch the network.
    from .network_tools import _validate_hostname

    try:
        return _validate_hostname(raw)
    except ValueError as exc:
        log.warning("GATEWAY_HOST=%r is unusable (%s); seeding the gateway target disabled", raw, exc)
        return ""


def _seed_probe_targets(conn: sqlite3.Connection, now_iso: str) -> None:
    """Seed the default targets of spec §3.1 — only when the table is empty."""
    if conn.execute("SELECT 1 FROM probe_targets LIMIT 1").fetchone() is not None:
        return

    cfg = AppConfig()
    settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings").fetchall()}
    gateway_host = _seed_gateway_host()
    legacy_host, legacy_port = connectivity.resolve_target(
        settings.get("connect_target", cfg.connect_target), cfg.connect_default_port
    )
    legacy_interval = _float_or(settings.get("connect_interval_seconds"), cfg.connect_interval_seconds)
    legacy_timeout = _int_or(settings.get("ping_timeout_ms"), cfg.ping_timeout_ms)

    rows: list[tuple[Any, ...]] = [
        ("gateway", "gateway", "icmp", gateway_host, None, 1.0, 1000, 1 if gateway_host else 0, None),
        ("cloudflare-dns", "internet", "icmp", "1.1.1.1", None, 1.0, 1000, 1, None),
        ("google-dns", "internet", "icmp", "8.8.8.8", None, 1.0, 1000, 1, None),
        ("quad9-dns", "internet", "icmp", "9.9.9.9", None, 1.0, 1000, 1, None),
        ("legacy-tcp", "tcp", "tcp", legacy_host, legacy_port, legacy_interval, legacy_timeout, 1, None),
        (
            "dns-system",
            "dns",
            "dns",
            "example.com",
            None,
            30.0,
            2000,
            1,
            json.dumps({"qname": "example.com", "resolver": "system"}),
        ),
        (
            "https-cloudflare",
            "https",
            "https",
            "https://cloudflare.com/cdn-cgi/trace",
            443,
            60.0,
            5000,
            1,
            None,
        ),
        (
            "https-google",
            "https",
            "https",
            "https://www.google.com/generate_204",
            443,
            60.0,
            5000,
            1,
            None,
        ),
    ]
    conn.executemany(
        """
        INSERT INTO probe_targets(
          name, kind, protocol, host, port, interval_seconds, timeout_ms, enabled,
          extra_json, family_pref, created_at, updated_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,'auto',?,?)
        """,
        [row + (now_iso, now_iso) for row in rows],
    )


def get_settings(db_path: str, keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    placeholders = ",".join(["?"] * len(keys))
    with db_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
            tuple(keys),
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}


def setting_was_set_by_user(db_path: str, key: str) -> bool:
    """Whether `key` was ever written through the UI or the API.

    `config_changes` is the audit trail of every setting write, including its
    source, so it answers "did a person ever decide this?" — which is what
    lets an environment variable fill a value it has never been given without
    ever overriding a decision somebody made in the panel (spec §3.1).
    """
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM config_changes WHERE key = ? AND source IN ('ui','api') LIMIT 1",
            (key,),
        ).fetchone()
    return row is not None


def set_setting(
    db_path: str,
    key: str,
    value: str,
    now_iso: str | None = None,
    source: str = "ui",
) -> None:
    """Store a setting and append a `config_changes` row when the value changes."""
    now_iso = now_iso or _utc_now_iso()
    with db_conn(db_path) as conn:
        conn.execute("BEGIN")
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            old_value = row["value"] if row is not None else None
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at)
                VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now_iso),
            )
            if old_value != value:
                conn.execute(
                    """
                    INSERT INTO config_changes(changed_at, key, old_value, new_value, source)
                    VALUES (?,?,?,?,?)
                    """,
                    (now_iso, key, old_value, value, source),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def ensure_default_setting(db_path: str, key: str, value: str) -> None:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT 1 FROM settings WHERE key = ? LIMIT 1", (key,)).fetchone()
        if row:
            return
    set_setting(db_path, key, value, source="env")


def get_current_connectivity_period(db_path: str):
    with db_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, started_at, ended_at, is_up
            FROM connectivity_periods
            WHERE ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None


def end_current_connectivity_period(db_path: str, now_iso: str | None = None) -> bool:
    """Close the open availability period, if there is one. Returns True if closed.

    Used when availability becomes unknown (spec §8): time nobody measured must
    not be attributed to the previous state, so the period ends instead of
    silently growing.
    """
    now_iso = now_iso or _utc_now_iso()
    with db_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE connectivity_periods SET ended_at = ? WHERE ended_at IS NULL",
            (now_iso,),
        )
        return cur.rowcount > 0


def record_connectivity(db_path: str, is_up: bool, now_iso: str | None = None) -> None:
    now_iso = now_iso or _utc_now_iso()
    with db_conn(db_path) as conn:
        current = conn.execute(
            """
            SELECT id, is_up
            FROM connectivity_periods
            WHERE ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if current is None:
            conn.execute(
                "INSERT INTO connectivity_periods(started_at, ended_at, is_up) VALUES (?,?,?)",
                (now_iso, None, 1 if is_up else 0),
            )
            return

        current_is_up = bool(current["is_up"])
        if current_is_up == is_up:
            return

        conn.execute(
            "UPDATE connectivity_periods SET ended_at = ? WHERE id = ?",
            (now_iso, current["id"]),
        )
        conn.execute(
            "INSERT INTO connectivity_periods(started_at, ended_at, is_up) VALUES (?,?,?)",
            (now_iso, None, 1 if is_up else 0),
        )


def record_connectivity_check(
    db_path: str,
    is_up: bool,
    checked_at_iso: str | None = None,
    latency_ms: float | None = None,
) -> None:
    checked_at_iso = checked_at_iso or _utc_now_iso()
    with db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO connectivity_checks(checked_at, is_up, latency_ms) VALUES (?,?,?)",
            (checked_at_iso, 1 if is_up else 0, latency_ms),
        )


def record_connectivity_checks_batch(
    db_path: str,
    rows: list[tuple[str, bool, float | None]],
) -> None:
    if not rows:
        return
    values = [(checked_at_iso, 1 if is_up else 0, latency_ms) for checked_at_iso, is_up, latency_ms in rows]
    with db_conn(db_path) as conn:
        try:
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO connectivity_checks(checked_at, is_up, latency_ms) VALUES (?,?,?)",
                values,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def record_speed_test(
    db_path: str,
    started_at_iso: str,
    duration_seconds: float,
    bytes_downloaded: int,
    mbps: float,
    error: str | None,
    speedtest_mode: str | None = None,
    upload_mbps: float | None = None,
    ping_ms: float | None = None,
    server_name: str | None = None,
    server_country: str | None = None,
) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO speed_tests(
              started_at, duration_seconds, bytes_downloaded, mbps, error,
              speedtest_mode, upload_mbps, ping_ms, server_name, server_country
            )
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                started_at_iso,
                duration_seconds,
                bytes_downloaded,
                mbps,
                error,
                speedtest_mode,
                upload_mbps,
                ping_ms,
                server_name,
                server_country,
            ),
        )


def get_last_speed_test(db_path: str):
    with db_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, started_at, duration_seconds, bytes_downloaded, mbps, error,
                   speedtest_mode, upload_mbps, ping_ms, server_name, server_country
            FROM speed_tests
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None


def get_last_success_speed_test(db_path: str):
    with db_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, started_at, duration_seconds, bytes_downloaded, mbps, error,
                   speedtest_mode, upload_mbps, ping_ms, server_name, server_country
            FROM speed_tests
            WHERE error IS NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None


@dataclass(frozen=True)
class TimeRange:
    start_iso: str
    end_iso: str


def query_speed_tests(db_path: str, tr: TimeRange):
    with db_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT started_at, duration_seconds, bytes_downloaded, mbps, error,
                   speedtest_mode, upload_mbps, ping_ms, server_name, server_country
            FROM speed_tests
            WHERE started_at >= ? AND started_at <= ?
            ORDER BY started_at ASC
            """,
            (tr.start_iso, tr.end_iso),
        ).fetchall()
        return [dict(r) for r in rows]


def query_connectivity_periods(db_path: str, tr: TimeRange, is_up: bool | None = None):
    where_is_up = ""
    params = [tr.end_iso, tr.start_iso]
    if is_up is not None:
        where_is_up = " AND is_up = ?"
        params.append(1 if is_up else 0)
    with db_conn(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT started_at, ended_at, is_up
            FROM connectivity_periods
            WHERE started_at < ?
              AND (ended_at IS NULL OR ended_at > ?)
              {where_is_up}
            ORDER BY started_at ASC
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def query_connectivity_checks(db_path: str, tr: TimeRange):
    with db_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT checked_at, is_up, latency_ms
            FROM connectivity_checks
            WHERE checked_at >= ? AND checked_at <= ?
            ORDER BY checked_at ASC
            """,
            (tr.start_iso, tr.end_iso),
        ).fetchall()
        return [dict(r) for r in rows]


def get_current_blocked_period(db_path: str, test_type: str):
    """Get the current open blocked period for a test type."""
    with db_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, test_type, started_at, ended_at, reason
            FROM blocked_periods
            WHERE test_type = ? AND ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (test_type,),
        ).fetchone()
        return dict(row) if row else None


def start_blocked_period(db_path: str, test_type: str, reason: str, now_iso: str | None = None) -> None:
    """Start a new blocked period if not already in one."""
    now_iso = now_iso or _utc_now_iso()
    current = get_current_blocked_period(db_path, test_type)
    if current is not None:
        # Already in a blocked period
        return
    with db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO blocked_periods(test_type, started_at, ended_at, reason) VALUES (?,?,?,?)",
            (test_type, now_iso, None, reason),
        )


def end_blocked_period(db_path: str, test_type: str, now_iso: str | None = None) -> None:
    """End the current blocked period if there is one."""
    now_iso = now_iso or _utc_now_iso()
    current = get_current_blocked_period(db_path, test_type)
    if current is None:
        return
    with db_conn(db_path) as conn:
        conn.execute(
            "UPDATE blocked_periods SET ended_at = ? WHERE id = ?",
            (now_iso, current["id"]),
        )


def integrity_quick_check(db_path: str) -> str:
    """``PRAGMA quick_check``: ``'ok'`` when healthy, otherwise a short summary.

    Cheaper than ``PRAGMA integrity_check`` (it skips verifying every index),
    which is why it is safe to run once on every startup. A problem here is
    logged as a warning by the caller and never stops the app from starting —
    a corrupt row store is something to investigate, not a reason to refuse
    to serve the UI or keep probing.
    """
    with db_conn(db_path) as conn:
        rows = conn.execute("PRAGMA quick_check").fetchall()
    values = [str(r[0]) for r in rows]
    if values == ["ok"]:
        return "ok"
    return "; ".join(values) if values else "unknown"


def checkpoint_wal(db_path: str) -> None:
    """``PRAGMA wal_checkpoint(TRUNCATE)``: fold the WAL back into ``app.db``.

    Run at the end of a retention pass so that deleting a large number of rows
    does not leave a bloated ``-wal`` file sitting next to a much smaller main
    file until SQLite gets around to checkpointing it on its own.
    """
    with db_conn(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def query_blocked_periods(db_path: str, tr: TimeRange, test_type: str | None = None):
    """Query blocked periods within a time range."""
    where_type = ""
    params: list = [tr.end_iso, tr.start_iso]
    if test_type is not None:
        where_type = " AND test_type = ?"
        params.append(test_type)
    with db_conn(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT test_type, started_at, ended_at, reason
            FROM blocked_periods
            WHERE started_at < ?
              AND (ended_at IS NULL OR ended_at > ?)
              {where_type}
            ORDER BY started_at ASC
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
