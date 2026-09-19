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


def test_set_setting_survives_a_writer_committing_between_its_read_and_write(db_path: str) -> None:
    """A concurrent commit must not turn a settings write into "database is locked".

    `set_setting` reads the old value before it writes the new one. In WAL mode
    a *deferred* transaction that has already read cannot upgrade to a writer
    once someone else has committed: SQLite fails the write immediately and
    `busy_timeout` never gets a chance to wait. That is how saving the settings
    form — one `set_setting` per field, racing the probe scheduler's flush —
    used to answer HTTP 500.
    """
    stop = threading.Event()
    writer_errors: list[BaseException] = []

    def commit_continuously() -> None:
        i = 0
        while not stop.is_set():
            i += 1
            try:
                with db_conn(db_path) as conn:
                    conn.execute(
                        "INSERT INTO config_changes(changed_at, key, old_value, new_value, source)"
                        " VALUES (?,?,?,?,?)",
                        ("2026-01-01T00:00:00.000Z", "noise", None, str(i), "env"),
                    )
            except BaseException as exc:  # noqa: BLE001
                writer_errors.append(exc)
                return

    thread = threading.Thread(target=commit_continuously)
    thread.start()
    try:
        for i in range(300):
            set_setting(db_path, "connect_target", f"host-{i}")
    finally:
        stop.set()
        thread.join(timeout=5.0)

    assert writer_errors == []
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'connect_target'").fetchone()
    assert row["value"] == "host-299"
