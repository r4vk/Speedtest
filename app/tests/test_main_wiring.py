"""The app starts and stops the quality engine (design spec §2, §8, §12).

The `client` fixture disables ping through the settings, so the engine starts
with no target at all and nothing here touches the network.
"""
from __future__ import annotations

import importlib
import sqlite3

from fastapi.testclient import TestClient


def test_startup_creates_the_quality_engine(client: TestClient) -> None:
    engine = getattr(client.app.state, "quality_engine", None)
    assert engine is not None
    assert engine.scheduler.targets() == []  # ping is disabled in the fixture
    assert engine.blocked_reason == "disabled"


def test_status_carries_the_quality_block(client: TestClient) -> None:
    payload = client.get("/api/status").json()

    assert set(payload["quality"]) == {"availability", "quality", "icmp_method"}
    assert payload["quality"]["availability"] == "no_data"
    assert payload["quality"]["quality"] == "unknown"
    assert isinstance(payload["quality"]["icmp_method"], str)
    # the legacy shape is unchanged
    assert {"now", "connectivity", "last_speed_test", "speedtest_running", "config"} <= set(payload)


def test_status_without_an_engine_reports_no_data(client: TestClient) -> None:
    main_module = importlib.import_module("speedtest_app.main")
    del client.app.state.quality_engine
    assert main_module._quality_status() == {
        "availability": "no_data",
        "quality": "unknown",
        "icmp_method": "unknown",
    }


def test_shutdown_ends_the_monitor_session(client: TestClient) -> None:
    db_path = client.app_db_path
    client.get("/healthz")
    client.__exit__(None, None, None)  # run the shutdown half of the lifespan

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        session = conn.execute(
            "SELECT * FROM monitor_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        blocked = conn.execute(
            "SELECT * FROM blocked_periods WHERE test_type = 'ping' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert session is not None
    assert session["ended_at"] is not None
    assert session["end_reason"] == "shutdown"
    assert [row["reason"] for row in blocked] == ["disabled"]
    client.__enter__()  # let the fixture's own exit be a no-op on a live client
