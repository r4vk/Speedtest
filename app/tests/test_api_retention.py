"""`GET /api/quality/retention` (design spec §14)."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_retention_status_shape(client: TestClient) -> None:
    resp = client.get("/api/quality/retention")
    assert resp.status_code == 200
    payload = resp.json()

    assert set(payload) == {
        "rows_24h",
        "db_bytes",
        "bytes_per_row_estimate",
        "estimated_raw_bytes_per_day",
        "estimated_raw_bytes_at_retention",
        "settings",
        "raw_available_from",
    }
    assert set(payload["rows_24h"]) == {"probe_results", "probe_aggregates", "incidents"}
    assert payload["db_bytes"] > 0
    assert isinstance(payload["raw_available_from"], str)

    settings = payload["settings"]
    assert settings == {
        "raw_days": 14,
        "aggregate_days": 365,
        "incident_days": 730,
        "load_test_raw_days": 90,
        "diagnostics_days": 365,
        "batch_size": 5000,
    }
    # a fresh DB has no raw rows yet, so the retention-window estimate is defined (not None)
    assert payload["estimated_raw_bytes_at_retention"] == 0.0


def test_retention_status_reflects_overridden_settings(client: TestClient) -> None:
    from speedtest_app.db import set_setting

    set_setting(client.app_db_path, "retention_raw_days", "7", source="ui")

    payload = client.get("/api/quality/retention").json()

    assert payload["settings"]["raw_days"] == 7
