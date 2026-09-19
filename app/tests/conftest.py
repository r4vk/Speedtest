"""Shared pytest fixtures (design spec §16).

Tests never touch the real network: the ``client`` fixture points the app at a
throw-away ``DATA_DIR`` and disables the legacy ping/speedtest loops and the
telemetry heartbeat before the FastAPI startup event runs.
"""
from __future__ import annotations

import importlib
from datetime import timedelta
from pathlib import Path
from typing import Callable, Iterator

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
