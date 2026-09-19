"""SQLite write robustness: busy timeout, integrity check, WAL checkpoint
(design spec §14).
"""
from __future__ import annotations

import threading
import time

from speedtest_app.db import checkpoint_wal, db_conn, integrity_quick_check, set_setting


def test_integrity_quick_check_reports_ok_on_a_fresh_migrated_db(db_path: str) -> None:
    assert integrity_quick_check(db_path) == "ok"


def test_checkpoint_wal_runs_without_error_after_writes(db_path: str) -> None:
    set_setting(db_path, "some_key", "some_value", source="env")

    checkpoint_wal(db_path)  # must not raise

    with db_conn(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'some_key'").fetchone()
    assert row["value"] == "some_value"


def test_a_second_writer_waits_instead_of_raising_database_locked(db_path: str) -> None:
    """`PRAGMA busy_timeout` lets a blocked writer wait instead of failing."""
    errors: list[BaseException] = []
    started = threading.Event()

    def hold_a_write_lock() -> None:
        try:
            with db_conn(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                started.set()
                time.sleep(0.3)
                conn.execute(
                    "INSERT INTO config_changes(changed_at, key, old_value, new_value, source) "
                    "VALUES ('t', 'a', NULL, '1', 'env')"
                )
                conn.execute("COMMIT")
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    def write_concurrently() -> None:
        started.wait(timeout=2.0)
        try:
            with db_conn(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO config_changes(changed_at, key, old_value, new_value, source) "
                    "VALUES ('t', 'b', NULL, '2', 'env')"
                )
                conn.execute("COMMIT")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=hold_a_write_lock)
    t2 = threading.Thread(target=write_concurrently)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert errors == []
    with db_conn(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM config_changes WHERE key IN ('a', 'b')"
        ).fetchone()["c"]
    assert count == 2
