"""The app starts and stops the quality engine (design spec §2, §8, §12).

The `client` fixture disables ping through the settings, so the engine starts
with no target at all and nothing here touches the network.
"""
from __future__ import annotations

import importlib
import sqlite3

from fastapi.testclient import TestClient

from speedtest_app import quality_db
from speedtest_app.db import mark_connectivity_period_expected, record_connectivity


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


# ---------------------------------------------------------------------------
# marking one closed outage by hand (Task 8 — design spec §6, §9)
# ---------------------------------------------------------------------------

#: The range is spelled in UTC rather than local time: `parse_dt` reads a naked
#: `2026-09-21T00:00` as *local*, so a plain wall-clock range would slide off
#: the fixtures below on any machine that is not at UTC+0.
OUTAGE_RANGE = "from=2026-09-21T00:00:00.000Z&to=2026-09-21T02:00:00.000Z"


def test_outages_carry_the_period_id_and_the_flag(client: TestClient) -> None:
    record_connectivity(client.app_db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(client.app_db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")

    item = client.get(f"/api/outages?{OUTAGE_RANGE}").json()["items"][0]
    assert set(item) >= {"id", "started_at", "ended_at", "expected", "expected_source",
                         "expected_rule_id"}
    assert item["expected"] == 0


def test_mark_outage_expected_by_hand(client: TestClient) -> None:
    record_connectivity(client.app_db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(client.app_db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    url = f"/api/outages?{OUTAGE_RANGE}"
    period_id = client.get(url).json()["items"][0]["id"]

    marked = client.patch(
        f"/api/outages/{period_id}/expected", json={"expected": True, "note": "restart routera"}
    )
    assert marked.status_code == 200

    items = client.get(url).json()["items"]
    assert items[0]["expected"] == 1 and items[0]["expected_source"] == "manual"


def test_manual_unmark_of_an_outage_records_that_a_person_looked(client: TestClient) -> None:
    record_connectivity(client.app_db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(client.app_db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    url = f"/api/outages?{OUTAGE_RANGE}"
    period_id = client.get(url).json()["items"][0]["id"]
    mark_connectivity_period_expected(
        client.app_db_path,
        started_at_iso="2026-09-21T01:00:00.000Z",
        expected=True,
        source="rule",
        rule_id=None,
    )

    client.patch(f"/api/outages/{period_id}/expected", json={"expected": False})
    item = client.get(url).json()["items"][0]
    assert item["expected"] == 0 and item["expected_source"] == "manual"


def test_open_outage_cannot_be_marked(client: TestClient) -> None:
    record_connectivity(client.app_db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    period_id = client.get(f"/api/outages?{OUTAGE_RANGE}").json()["items"][0]["id"]
    assert (
        client.patch(f"/api/outages/{period_id}/expected", json={"expected": True}).status_code
        == 409
    )


def test_a_running_outage_says_it_is_still_running(client: TestClient) -> None:
    """`ended_at` is filled in with "now" for an outage that has not ended, so
    it is `open` that tells a caller not to offer the mark control — otherwise
    the dashboard offers a button whose only possible answer is the 409 above.
    """
    record_connectivity(client.app_db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    running = client.get(f"/api/outages?{OUTAGE_RANGE}").json()["items"][0]
    assert running["open"] is True
    assert running["ended_at"]  # a drawable end, not null

    record_connectivity(client.app_db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    finished = client.get(f"/api/outages?{OUTAGE_RANGE}").json()["items"][0]
    assert finished["open"] is False


def test_a_marked_outage_keeps_its_shape_between_the_list_and_the_patch(
    client: TestClient,
) -> None:
    """The PATCH answers with the row the list would have shown, key for key.

    Both are built by one helper; this is what stops the two drifting apart
    again, which is how the dashboard lost track of what was still running.
    """
    record_connectivity(client.app_db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(client.app_db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    listed = client.get(f"/api/outages?{OUTAGE_RANGE}").json()["items"][0]

    patched = client.patch(
        f"/api/outages/{listed['id']}/expected", json={"expected": True}
    ).json()["outage"]

    assert set(patched) == set(listed)
    assert patched["started_at"] == listed["started_at"]
    assert patched["expected"] == 1


def test_marking_an_unknown_outage_is_404(client: TestClient) -> None:
    assert (
        client.patch("/api/outages/424242/expected", json={"expected": True}).status_code == 404
    )


# ---------------------------------------------------------------------------
# the expected filter across the reads (Task 9 — design spec §10)
# ---------------------------------------------------------------------------

WIDE_RANGE = "from=2026-09-21T00:00:00.000Z&to=2026-09-21T05:00:00.000Z"


def _two_outages(db_path: str) -> None:
    """A 5-minute real outage and, two hours later, a 4-minute expected one."""
    record_connectivity(db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    record_connectivity(db_path, is_up=False, now_iso="2026-09-21T03:00:00.000Z")
    record_connectivity(db_path, is_up=True, now_iso="2026-09-21T03:04:00.000Z")
    mark_connectivity_period_expected(
        db_path,
        started_at_iso="2026-09-21T03:00:00.000Z",
        expected=True,
        source="rule",
        rule_id=None,
    )


def _observed_session(db_path: str) -> None:
    """A closed monitor session covering the fixtures' range.

    `/api/report/quality` clips downtime to the time somebody was watching, so
    a range nobody observed reports zero however many periods it holds.
    """
    session_id = quality_db.start_session(
        db_path, device_id="test", app_version="test", now_iso="2026-09-21T00:00:00.000Z"
    )
    quality_db.end_session(db_path, session_id, "2026-09-21T05:00:00.000Z", "shutdown")


def test_outages_endpoint_filters_expected(client: TestClient) -> None:
    _two_outages(client.app_db_path)
    url = f"/api/outages?{WIDE_RANGE}"

    body = client.get(url).json()
    assert len(body["items"]) == 2
    assert body["expected_filter"] == "all" and body["expected_hidden"] == 0

    filtered = client.get(url + "&expected=exclude").json()
    assert [i["started_at"] for i in filtered["items"]] == [body["items"][0]["started_at"]]
    assert filtered["expected_filter"] == "exclude" and filtered["expected_hidden"] == 1

    only = client.get(url + "&expected=only").json()
    assert [i["expected"] for i in only["items"]] == [1]

    typo = client.get(url + "&expected=krowa").json()
    assert len(typo["items"]) == 2 and typo["expected_filter"] == "all"


def test_report_quality_downtime_follows_the_filter(client: TestClient) -> None:
    """`q-downtime` and `q-percent` read this endpoint, not `/api/outages`."""
    _observed_session(client.app_db_path)
    _two_outages(client.app_db_path)
    url = f"/api/report/quality?{WIDE_RANGE}"

    body = client.get(url).json()
    assert body["downtime_seconds"] == 540
    assert body["incident_count"] == 2
    assert body["expected_filter"] == "all" and body["expected_hidden"] == 0

    filtered = client.get(url + "&expected=exclude").json()
    assert filtered["downtime_seconds"] == 300
    assert filtered["incident_count"] == 1
    assert filtered["expected_hidden"] == 1
    assert filtered["downtime_percent"] < body["downtime_percent"]


def test_outage_csv_carries_the_flag(client: TestClient) -> None:
    _two_outages(client.app_db_path)
    lines = client.get(f"/api/export/outages.csv?{WIDE_RANGE}").text.splitlines()

    header = lines[0].split(",")
    assert "expected" in header and "expected_source" in header
    assert [line.split(",")[header.index("expected")] for line in lines[1:]] == ["0", "1"]

    excluded = client.get(f"/api/export/outages.csv?{WIDE_RANGE}&expected=exclude").text.splitlines()
    assert len(excluded) == 2
