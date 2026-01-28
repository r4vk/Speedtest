from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator


SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_iso(value: str) -> datetime:
    v = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def db_conn(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
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
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


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


def set_setting(db_path: str, key: str, value: str, now_iso: str | None = None) -> None:
    now_iso = now_iso or _utc_now_iso()
    with db_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now_iso),
        )


def ensure_default_setting(db_path: str, key: str, value: str) -> None:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT 1 FROM settings WHERE key = ? LIMIT 1", (key,)).fetchone()
        if row:
            return
    set_setting(db_path, key, value)


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
