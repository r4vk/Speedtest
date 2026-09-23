"""CSV exports and the consistency of every view on one seeded range (spec §12, plan Etap 7).

The consistency test is the point of this file: the panel, the CSV and the
report have to agree on the same numbers, including the rows sitting exactly on
the range boundaries (``query_probe_results`` is inclusive on both ends).
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from anyio import to_thread

from speedtest_app import db, quality_db
from speedtest_app.api_quality_exports import _probe_csv_rows, export_probes_csv
from speedtest_app.quality_views import RAW_RANGE_MAX_DAYS, target_names
from speedtest_app.probe_types import Outcome, ProbeResult, Protocol
from speedtest_app.time_utils import ParsedRange, parse_dt, to_iso_z, utc_now

MINUTE = 60.0


def _target(db_path: str, name: str = "cf", protocol: str = "icmp", host: str = "1.1.1.1"):
    return quality_db.insert_target(
        db_path,
        name=name,
        kind="internet",
        protocol=protocol,
        host=host,
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=1,
    )


def _result(target_id: int, started_at: str, outcome: str, rtt_ms: float | None) -> ProbeResult:
    return ProbeResult(
        target_id=target_id,
        protocol=Protocol.ICMP,
        started_at=started_at,
        duration_ms=rtt_ms or 1000.0,
        outcome=Outcome(outcome),
        timeout_ms=1000,
        rtt_ms=rtt_ms,
        resolved_ip="1.1.1.1",
        ip_family=4,
        error_kind=None if outcome != "error" else "permission",
    )


def _seed_range(db_path: str) -> tuple[int, str, str]:
    """Seed one target with rows on both boundaries and inside; return (id, from, to).

    The seeded window is [start, end] with a row exactly at ``start``, one
    exactly at ``end`` and rows outside both ends that must never be counted.
    """
    target = _target(db_path)
    anchor = utc_now().replace(microsecond=0) - timedelta(hours=1)
    start = anchor
    end = anchor + timedelta(minutes=10)

    rows = [
        _result(target.id, to_iso_z(start - timedelta(seconds=1)), "ok", 11.0),  # before
        _result(target.id, to_iso_z(start), "ok", 12.0),  # exactly at start
        _result(target.id, to_iso_z(start + timedelta(minutes=1)), "timeout", None),
        _result(target.id, to_iso_z(start + timedelta(minutes=2)), "ok", 20.0),
        _result(target.id, to_iso_z(start + timedelta(minutes=3)), "error", None),
        _result(target.id, to_iso_z(start + timedelta(minutes=5)), "ok", 30.0),
        _result(target.id, to_iso_z(end), "ok", 40.0),  # exactly at end
        _result(target.id, to_iso_z(end + timedelta(seconds=1)), "ok", 99.0),  # after
    ]
    quality_db.insert_probe_results(db_path, rows)
    return target.id, to_iso_z(start), to_iso_z(end)


def _csv_rows(text: str) -> list[list[str]]:
    """Data rows only: every export now opens with comment lines (tz, retention)."""
    return [row for row in csv.reader(io.StringIO(text)) if row and not row[0].startswith("#")]


def test_probes_csv_has_local_and_utc_columns(client) -> None:
    db_path = client.app_db_path
    target_id, start, end = _seed_range(db_path)

    response = client.get(
        "/api/quality/export/probes.csv", params={"from": start, "to": end, "target_id": target_id}
    )
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "probes.csv" in response.headers["content-disposition"]

    rows = _csv_rows(response.text)
    assert rows[0] == [
        "started_at_local",
        "started_at_utc",
        "device_id",
        "target",
        "protocol",
        "outcome",
        "rtt_ms",
        "timeout_ms",
        "resolved_ip",
        "ip_family",
        "error_kind",
        "error_detail",
        "load_test_id",
    ]
    # both boundary rows are included, the two rows outside are not
    assert len(rows) - 1 == 6
    assert rows[1][1] == start
    assert rows[-1][1] == end
    assert rows[1][3] == "cf"
    assert rows[1][0] != rows[1][1]  # local column is not the UTC one


def test_probes_csv_notes_pruned_raw_data(client) -> None:
    db_path = client.app_db_path
    _seed_range(db_path)
    client.put("/api/config", json={"retention_raw_days": 1})

    old = utc_now() - timedelta(days=5)
    response = client.get(
        "/api/quality/export/probes.csv",
        params={"from": to_iso_z(old), "to": to_iso_z(utc_now())},
    )
    assert response.status_code == 200
    lines = response.text.splitlines()
    comment_lines = [line for line in lines if line.startswith("#")]
    assert comment_lines[0].startswith("# strefa czasowa: ")
    assert any(
        line.startswith("# surowe dane niedostepne (retencja) przed ") for line in comment_lines
    )


def test_incidents_csv_lists_the_range(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path, name="inc-target")
    now = utc_now()
    quality_db.insert_incident(
        db_path,
        target_id=target.id,
        protocol="icmp",
        kind="degraded",
        started_at=to_iso_z(now - timedelta(minutes=30)),
        ended_at=to_iso_z(now - timedelta(minutes=20)),
        closed_at=to_iso_z(now - timedelta(minutes=19)),
        close_reason="recovered",
        window_seconds=10,
        probe_interval_seconds=1.0,
        peak_loss_pct=50.0,
        peak_p95_rtt_ms=180.0,
        longest_fail_streak=4,
        windows_degraded=3,
        summary_json=json.dumps([[to_iso_z(now - timedelta(minutes=30)), "degraded", 50.0, 180.0]]),
    )

    response = client.get(
        "/api/quality/export/incidents.csv",
        params={"from": to_iso_z(now - timedelta(hours=1)), "to": to_iso_z(now)},
    )
    assert response.status_code == 200
    rows = _csv_rows(response.text)
    assert rows[0][:5] == ["id", "started_at_local", "started_at_utc", "ended_at_local", "target"]
    assert len(rows) == 2
    assert rows[1][4] == "inc-target"
    assert rows[1][6] == "degraded"


def test_aggregates_csv_needs_a_known_bucket(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path, name="agg-target")
    now = utc_now()
    bucket_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    quality_db.upsert_aggregate(
        db_path,
        {
            "target_id": target.id,
            "protocol": "icmp",
            "bucket": "1h",
            "bucket_start": to_iso_z(bucket_start),
            "attempts": 100,
            "ok_count": 90,
            "timeout_count": 10,
            "error_count": 0,
            "loss_pct": 10.0,
            "rtt_p95_ms": 42.0,
            "percentiles_from_raw": 1,
            "computed_at": to_iso_z(now),
        },
    )

    params = {"from": to_iso_z(now - timedelta(hours=3)), "to": to_iso_z(now)}
    response = client.get("/api/quality/export/aggregates.csv", params={**params, "bucket": "1h"})
    assert response.status_code == 200
    rows = _csv_rows(response.text)
    assert rows[0][:4] == ["bucket_start_local", "bucket_start_utc", "target", "protocol"]
    assert len(rows) == 2
    assert rows[1][2] == "agg-target"

    bad = client.get("/api/quality/export/aggregates.csv", params={**params, "bucket": "7d"})
    assert bad.status_code == 422


def test_load_tests_csv_has_one_row_per_direction(client) -> None:
    db_path = client.app_db_path
    now = utc_now()
    result = {
        "upload": {
            "kind": "iperf_udp",
            "direction": "upload",
            "receiver": {"packets": 1000, "lost_packets": 25, "jitter_ms": 1.5, "bits_per_second": 9_000_000},
            "sender": {},
        },
        "download": {
            "kind": "iperf_udp",
            "direction": "download",
            "receiver": {"packets": 1000, "lost_packets": 0, "jitter_ms": 0.5, "bits_per_second": 9_500_000},
            "sender": {},
        },
    }
    quality_db.insert_load_test(
        db_path,
        started_at=to_iso_z(now - timedelta(minutes=10)),
        ended_at=to_iso_z(now - timedelta(minutes=9)),
        kind="iperf_udp",
        direction="both",
        server="iperf.example",
        params_json=json.dumps({"port": 5201}),
        status="ok",
        result_json=json.dumps(result),
    )

    response = client.get(
        "/api/quality/export/load-tests.csv",
        params={"from": to_iso_z(now - timedelta(hours=1)), "to": to_iso_z(now)},
    )
    assert response.status_code == 200
    rows = _csv_rows(response.text)
    header = rows[0]
    assert header[:2] == ["id", "started_at_local"]
    assert "loss_pct" in header and "jitter_ms" in header
    assert len(rows) == 3  # one per direction
    directions = {row[header.index("direction")] for row in rows[1:]}
    assert directions == {"upload", "download"}
    loss = {row[header.index("direction")]: row[header.index("loss_pct")] for row in rows[1:]}
    assert loss["upload"] == "2.5"


def test_stats_timeline_csv_and_report_agree_on_the_same_range(client) -> None:
    """Every view of one range counts the same attempts, including the boundaries."""
    db_path = client.app_db_path
    target_id, start, end = _seed_range(db_path)
    params = {"from": start, "to": end}

    stats = client.get("/api/quality/stats", params=params).json()
    entry = next(t for t in stats["targets"] if t["target"]["id"] == target_id)
    assert (entry["stats"]["attempts"], entry["stats"]["ok"], entry["stats"]["timeouts"]) == (6, 4, 1)
    assert entry["stats"]["errors"] == 1

    timeline = client.get(
        "/api/quality/timeline", params={**params, "bucket_seconds": 60}
    ).json()
    points = next(t for t in timeline["targets"] if t["target"]["id"] == target_id)["points"]
    summed = {
        key: sum(point[key] for point in points)
        for key in ("attempts", "ok", "timeouts", "errors")
    }

    csv_text = client.get(
        "/api/quality/export/probes.csv", params={**params, "target_id": target_id}
    ).text
    csv_count = len(_csv_rows(csv_text)) - 1

    from speedtest_app import report as report_module

    model = report_module.build_report_model(
        db_path,
        parse_dt(start),
        parse_dt(end),
        app_version="test",
        now=utc_now(),
    )
    model_entry = next(t for t in model["targets"] if t["target"]["id"] == target_id)

    assert csv_count == entry["stats"]["attempts"]
    assert model_entry["stats"]["attempts"] == entry["stats"]["attempts"]
    assert model_entry["stats"]["ok"] == entry["stats"]["ok"]
    # Finding 1: the timeline covers the whole `[from, to]` via a closing
    # partial bucket, so it sums to exactly the same counts as stats/CSV/the
    # report — including the row sitting exactly on the `to` boundary.
    assert summed == {
        "attempts": entry["stats"]["attempts"],
        "ok": entry["stats"]["ok"],
        "timeouts": entry["stats"]["timeouts"],
        "errors": entry["stats"]["errors"],
    }
    assert timeline["bucket_seconds"] >= 10
    assert timeline["last_complete_bucket"] is not None


# ---------------------------------------------------------------------------
# the raw-range cap and the streaming export (review findings C1a, C1b)
# ---------------------------------------------------------------------------

def test_probes_csv_refuses_a_range_wider_than_the_raw_limit(client) -> None:
    now = utc_now()
    too_wide = client.get(
        "/api/quality/export/probes.csv",
        params={
            "from": to_iso_z(now - timedelta(days=RAW_RANGE_MAX_DAYS, hours=1)),
            "to": to_iso_z(now),
        },
    )
    assert too_wide.status_code == 422
    assert too_wide.json()["detail"] == "zakres surowych danych maks. 31 dni"

    at_the_limit = client.get(
        "/api/quality/export/probes.csv",
        params={"from": to_iso_z(now - timedelta(days=RAW_RANGE_MAX_DAYS)), "to": to_iso_z(now)},
    )
    assert at_the_limit.status_code == 200


def test_probes_csv_streams_from_the_cursor(client, monkeypatch) -> None:
    """finding C1a: the export never builds the whole result set as a list."""
    db_path = client.app_db_path
    target_id, start, end = _seed_range(db_path)

    def _refuse(*args, **kwargs):  # pragma: no cover - only runs on a regression
        raise AssertionError("probes.csv must stream, not materialise the range")

    monkeypatch.setattr(quality_db, "query_probe_results", _refuse)
    rows = _csv_rows(
        client.get("/api/quality/export/probes.csv", params={"from": start, "to": end}).text
    )

    assert rows[0][0] == "started_at_local"
    assert len(rows) == 7  # header + the six rows inside [from, to]
    assert all(row[3] for row in rows[1:])  # the target name is resolved per row


def _seed_many(db_path: str, count: int) -> tuple[int, str, str]:
    """One target with ``count`` rows a second apart; returns (id, from, to)."""
    target = _target(db_path, name="streamed")
    anchor = utc_now().replace(microsecond=0) - timedelta(hours=1)
    quality_db.insert_probe_results(
        db_path,
        [
            _result(target.id, to_iso_z(anchor + timedelta(seconds=i)), "ok", 10.0 + i)
            for i in range(count)
        ],
    )
    return target.id, to_iso_z(anchor), to_iso_z(anchor + timedelta(seconds=count))


async def test_probes_csv_body_survives_the_threadpool(client) -> None:
    """finding C1a round 2: the real body iterator, driven off the event loop.

    `StreamingResponse` wraps a sync iterator in `iterate_in_threadpool`, so
    the export is walked from anyio workers while the rest of the app keeps
    reading the same database. The whole file has to arrive — a stream that
    dies mid-way still looks like HTTP 200 to the client.
    """
    db_path = client.app_db_path
    _target_id, start, end = _seed_many(db_path, count=40)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_path=db_path)))

    response = export_probes_csv(
        request, ParsedRange(start=parse_dt(start), end=parse_dt(end)), None
    )

    # keep other workers busy so the body cannot rely on one sticky thread
    async def noisy_reader() -> None:
        for _ in range(40):
            await to_thread.run_sync(quality_db.count_probe_results, db_path, start, end)

    async with anyio.create_task_group() as group:
        group.start_soon(noisy_reader)
        group.start_soon(noisy_reader)
        chunks = [chunk async for chunk in response.body_iterator]

    text = "".join(
        chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk for chunk in chunks
    )
    rows = _csv_rows(text)
    assert rows[0][0] == "started_at_local"
    assert len(rows) == 41  # header + every seeded row, none dropped


async def test_probes_csv_leaves_no_read_connection_behind(client, monkeypatch) -> None:
    """A client that hangs up mid-download must not pin the WAL."""
    db_path = client.app_db_path
    _target_id, start, end = _seed_many(db_path, count=20)
    opened: list[Any] = []
    original = db._connect

    def spy(path: str, **kwargs: Any):
        conn = original(path, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, "_connect", spy)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_path=db_path)))
    response = export_probes_csv(
        request, ParsedRange(start=parse_dt(start), end=parse_dt(end)), None
    )

    iterator = response.body_iterator
    await iterator.__anext__()  # first chunk only, then hang up
    await iterator.aclose()

    streamed = [conn for conn in opened if _is_closed(conn)]
    assert streamed, "the export opened no connection at all"
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("PRAGMA user_version")


def _is_closed(conn) -> bool:
    try:
        conn.execute("PRAGMA user_version")
    except sqlite3.ProgrammingError:
        return True
    return False


def test_probe_csv_rows_survive_hopping_between_worker_threads(
    client, thread_hopper, db_reader
) -> None:
    """The export's own generator, advanced from a different thread every time.

    Four `fetchmany` pages and one `next()` per worker is what a thread pool
    with no affinity does in the worst case; before the fix the second page
    raised `ProgrammingError` inside an already-200 response, and the client
    got a truncated CSV under HTTP 200.
    """
    db_path = client.app_db_path
    _target_id, start, end = _seed_many(db_path, count=25)
    db_reader(lambda: quality_db.count_probe_results(db_path, start, end))

    generator = _probe_csv_rows(
        db_path,
        ParsedRange(start=parse_dt(start), end=parse_dt(end)),
        None,
        target_names(db_path),
        batch_size=7,
    )
    rows = thread_hopper.drain(generator)

    assert rows[0][0] == "started_at_local"
    assert len(rows) == 26  # header + all 25 rows, across four pages


def test_incident_csv_carries_the_flag(client) -> None:
    """The CSV is the evidence copy: it has to say which outages were planned."""
    db_path = client.app_db_path
    target = _target(db_path, name="expected-target")
    incident_id = quality_db.insert_incident(
        db_path,
        target_id=target.id,
        protocol="icmp",
        kind="outage",
        started_at="2026-09-21T01:00:00.000Z",
        ended_at="2026-09-21T01:04:00.000Z",
        closed_at="2026-09-21T01:05:00.000Z",
        close_reason="recovered",
        window_seconds=10,
        probe_interval_seconds=1.0,
        expected=1,
        expected_source="rule",
    )
    params = {"from": "2026-09-21T00:00:00.000Z", "to": "2026-09-21T23:00:00.000Z"}

    rows = _csv_rows(client.get("/api/quality/export/incidents.csv", params=params).text)
    assert "expected" in rows[0] and "expected_source" in rows[0]
    flagged = next(row for row in rows[1:] if row[0] == str(incident_id))
    assert flagged[rows[0].index("expected")] == "1"
    assert flagged[rows[0].index("expected_source")] == "rule"

    excluded = _csv_rows(
        client.get(
            "/api/quality/export/incidents.csv", params={**params, "expected": "exclude"}
        ).text
    )
    assert [row[0] for row in excluded[1:]] == []
