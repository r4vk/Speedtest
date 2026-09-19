"""Shared pytest fixtures (design spec §16).

Tests never touch the real network: the ``client`` fixture points the app at a
throw-away ``DATA_DIR`` and disables the legacy ping/speedtest loops and the
telemetry heartbeat before the FastAPI startup event runs.
"""
from __future__ import annotations

import importlib
import queue
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from speedtest_app.db import ensure_db, set_setting
from speedtest_app.time_utils import to_iso_z, utc_now


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A migrated (schema v2) SQLite file in a temporary directory."""
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    path = str(tmp_path / "data" / "app.db")
    ensure_db(path)
    return path


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A ``TestClient`` running the real app against a temporary database.

    The database file is available as ``client.app_db_path``.
    """
    data_dir = tmp_path / "app-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    monkeypatch.setenv("TELEMETRY_DEFAULT_ENABLED", "false")

    app_db_path = str(data_dir / "app.db")
    ensure_db(app_db_path)
    # Keep the background loops idle so that no test ever hits the network.
    set_setting(app_db_path, "ping_enabled", "false", source="env")
    set_setting(app_db_path, "speed_enabled", "false", source="env")

    # AppConfig reads the environment at class definition time, so the config
    # module has to be reloaded before main.py picks the class up.
    config_module = importlib.import_module("speedtest_app.config")
    db_module = importlib.import_module("speedtest_app.db")
    importlib.reload(config_module)
    main_module = importlib.import_module("speedtest_app.main")
    importlib.reload(main_module)

    try:
        with TestClient(main_module.app) as test_client:
            test_client.app_db_path = app_db_path
            yield test_client
    finally:
        # Restore the environment snapshot the reloaded modules captured, so
        # tests running after this one see the original configuration.
        monkeypatch.undo()
        importlib.reload(config_module)
        importlib.reload(db_module)


@pytest.fixture
def utc_iso() -> Callable[[float], str]:
    """Return UTC ISO-Z timestamps relative to a single anchor taken now."""
    anchor = utc_now()

    def _utc_iso(offset_seconds: float = 0.0) -> str:
        return to_iso_z(anchor + timedelta(seconds=offset_seconds))

    return _utc_iso


class ThreadHopper:
    """Advance an iterator from a different — and still live — thread each step.

    This is what a server does to the body of a `StreamingResponse` returned
    from a sync route: `iterate_in_threadpool` has no thread affinity, so the
    `fetchmany` after a yield can run on another worker than the one that
    opened the database connection.

    Every worker here stays alive until `close()`, which matters: a thread
    that has already finished can have its identity reused by the next one,
    and `sqlite3`'s same-thread check compares exactly that identity — a test
    spawning one short-lived thread per step passes by luck a good part of the
    time.
    """

    def __init__(self, workers: int = 4) -> None:
        self._jobs: list[queue.Queue] = [queue.Queue() for _ in range(workers)]
        self._results: list[queue.Queue] = [queue.Queue() for _ in range(workers)]
        self._threads = [
            threading.Thread(target=self._loop, args=(index,), daemon=True)
            for index in range(workers)
        ]
        for thread in self._threads:
            thread.start()
        self._next = 0

    def _loop(self, index: int) -> None:
        while True:
            job = self._jobs[index].get()
            if job is None:
                return
            try:
                self._results[index].put(("row", next(job)))
            except StopIteration:
                self._results[index].put(("stop", None))
            except BaseException as exc:  # noqa: BLE001 - which one is the point
                self._results[index].put(("error", exc))

    def step(self, generator: Any) -> tuple[str, Any]:
        """One `next()`, on the next worker in the rotation."""
        index = self._next
        self._next = (self._next + 1) % len(self._jobs)
        self._jobs[index].put(generator)
        return self._results[index].get(timeout=10)

    def drain(self, generator: Any) -> list[Any]:
        """Every item of ``generator``, each fetched from a different thread."""
        items: list[Any] = []
        while True:
            kind, value = self.step(generator)
            if kind == "error":
                raise AssertionError(f"streaming from another thread failed: {value!r}")
            if kind == "stop":
                return items
            items.append(value)

    def close(self) -> None:
        for jobs in self._jobs:
            jobs.put(None)
        for thread in self._threads:
            thread.join(timeout=5)


@pytest.fixture
def thread_hopper() -> Iterator[ThreadHopper]:
    hopper = ThreadHopper()
    try:
        yield hopper
    finally:
        hopper.close()


@pytest.fixture
def db_reader() -> Iterator[Callable[[Callable[[], Any]], None]]:
    """Run a read in a loop on another thread for the duration of a test."""
    stop = threading.Event()
    threads: list[threading.Thread] = []
    errors: list[BaseException] = []

    def start(read: Callable[[], Any]) -> None:
        def loop() -> None:
            while not stop.is_set():
                try:
                    read()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    return

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        threads.append(thread)

    try:
        yield start
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
        assert errors == [], f"the concurrent reader failed: {errors!r}"
