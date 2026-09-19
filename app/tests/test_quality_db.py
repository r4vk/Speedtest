"""Accessors for the schema v2 tables."""
from __future__ import annotations

import sqlite3

import pytest

from speedtest_app import quality_db
from speedtest_app.probe_types import Outcome, ProbeResult, ProbeTarget, Protocol


def _make_target(db_path: str, name: str = "unit-target") -> ProbeTarget:
    return quality_db.insert_target(
        db_path,
        name=name,
        kind="internet",
        protocol=Protocol.ICMP,
        host="1.1.1.1",
        port=None,
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=True,
        extra={"note": "unit"},
    )


def test_target_crud_round_trip(db_path):
    target = _make_target(db_path)

    assert target.id > 0
    assert target.extra == {"note": "unit"}
    assert target.enabled is True
    assert quality_db.get_target(db_path, target.id) == target

    updated = quality_db.update_target(db_path, target.id, enabled=False, timeout_ms=2500)
    assert updated.enabled is False
    assert updated.timeout_ms == 2500

    names = {t.name for t in quality_db.list_targets(db_path, enabled_only=True)}
    assert "unit-target" not in names

    assert quality_db.delete_target(db_path, target.id) is True
    assert quality_db.get_target(db_path, target.id) is None
    assert quality_db.delete_target(db_path, target.id) is False


def test_unknown_target_column_is_rejected(db_path):
    with pytest.raises(ValueError):
        quality_db.insert_target(db_path, name="x", kind="internet", protocol="icmp", host="h", bogus=1)


def test_probe_result_round_trip(db_path, utc_iso):
    target = _make_target(db_path)
    result = ProbeResult(
        target_id=target.id,
        protocol=Protocol.TCP,
        started_at=utc_iso(0),
        duration_ms=12.5,
        outcome=Outcome.OK,
        timeout_ms=1000,
        rtt_ms=11.25,
        resolved_ip="93.184.216.34",
        ip_family=4,
        stages={"dns_ms": 3.5, "connect_ms": 7.75},
    )

    empty_stages = ProbeResult(
        target_id=target.id,
        protocol=Protocol.ICMP,
        started_at=utc_iso(1),
        duration_ms=2.0,
        outcome=Outcome.TIMEOUT,
        timeout_ms=1000,
        stages={},
    )

    assert quality_db.insert_probe_results(db_path, [result, empty_stages]) == 2

    rows = quality_db.query_probe_results(db_path, utc_iso(-60), utc_iso(60))
    assert len(rows) == 2
    assert ProbeResult.from_row(rows[0]) == result
    # an empty stage map must survive as {}, not collapse into None
    assert ProbeResult.from_row(rows[1]) == empty_stages
    assert ProbeResult.from_row(rows[1]).stages == {}
    assert quality_db.count_probe_results(db_path, utc_iso(-60), utc_iso(60)) == 2
    last = quality_db.last_result_per_target(db_path)[target.id]
    assert last["started_at"] == utc_iso(1)
    assert last["rtt_ms"] is None


def test_probe_results_are_ordered_and_filtered(db_path, utc_iso):
    target = _make_target(db_path)
    other = _make_target(db_path, name="other-target")
    common = dict(duration_ms=1.0, outcome=Outcome.TIMEOUT, timeout_ms=1000)
    rows = [
        ProbeResult(target_id=target.id, protocol=Protocol.ICMP, started_at=utc_iso(20), **common),
        ProbeResult(target_id=target.id, protocol=Protocol.ICMP, started_at=utc_iso(10), **common),
        ProbeResult(target_id=other.id, protocol=Protocol.ICMP, started_at=utc_iso(15), **common),
    ]

    assert quality_db.insert_probe_results(db_path, rows) == 3

    all_rows = quality_db.query_probe_results(db_path, utc_iso(0), utc_iso(60))
    assert [r["started_at"] for r in all_rows] == [utc_iso(10), utc_iso(15), utc_iso(20)]
    mine = quality_db.query_probe_results(db_path, utc_iso(0), utc_iso(60), target_id=target.id)
    assert [r["target_id"] for r in mine] == [target.id, target.id]
    assert quality_db.query_probe_results(db_path, utc_iso(0), utc_iso(60), protocol="tcp") == []


def test_duplicate_external_id_is_ignored(db_path, utc_iso):
    target = _make_target(db_path)
    first = ProbeResult(
        target_id=target.id,
        protocol=Protocol.ICMP,
        started_at=utc_iso(0),
        duration_ms=5.0,
        outcome=Outcome.OK,
        timeout_ms=1000,
        rtt_ms=5.0,
        device_id="macbook",
        external_id="abc-1",
    )
    duplicate = ProbeResult(
        target_id=target.id,
        protocol=Protocol.ICMP,
        started_at=utc_iso(1),
        duration_ms=6.0,
        outcome=Outcome.OK,
        timeout_ms=1000,
        rtt_ms=6.0,
        device_id="macbook",
        external_id="abc-1",
    )

    assert quality_db.insert_probe_results(db_path, [first, duplicate]) == 1
    assert quality_db.count_probe_results(db_path, utc_iso(-60), utc_iso(60)) == 1


def test_incident_overlap_query(db_path, utc_iso):
    target = _make_target(db_path)
    common = dict(
        target_id=target.id,
        protocol="icmp",
        kind="degraded",
        window_seconds=10,
        probe_interval_seconds=1.0,
    )
    before = quality_db.insert_incident(db_path, started_at=utc_iso(0), ended_at=utc_iso(100), closed_at=utc_iso(120), **common)
    inside = quality_db.insert_incident(db_path, started_at=utc_iso(300), ended_at=utc_iso(400), closed_at=utc_iso(420), **common)
    still_open = quality_db.insert_incident(db_path, started_at=utc_iso(500), **common)

    found = quality_db.query_incidents(db_path, utc_iso(200), utc_iso(600))
    assert [r["id"] for r in found] == [inside, still_open]
    assert before not in [r["id"] for r in found]

    open_ids = [r["id"] for r in quality_db.query_incidents(db_path, utc_iso(200), utc_iso(600), open_only=True)]
    assert open_ids == [still_open]
    assert [r["id"] for r in quality_db.list_open_incidents(db_path)] == [still_open]

    quality_db.update_incident(db_path, still_open, ended_at=utc_iso(550), closed_at=utc_iso(560), close_reason="recovered")
    assert quality_db.get_incident(db_path, still_open)["close_reason"] == "recovered"
    assert quality_db.list_open_incidents(db_path) == []


def test_aggregate_upsert_replaces_conflicting_bucket(db_path, utc_iso):
    target = _make_target(db_path)
    row = {
        "target_id": target.id,
        "protocol": "icmp",
        "bucket": "1h",
        "bucket_start": "2026-01-10T10:00:00.000Z",
        "attempts": 100,
        "ok_count": 90,
        "timeout_count": 10,
        "error_count": 0,
        "loss_pct": 10.0,
        "rtt_p95_ms": 30.0,
        "percentiles_from_raw": 1,
        "computed_at": utc_iso(0),
    }

    quality_db.upsert_aggregate(db_path, row)
    quality_db.upsert_aggregate(db_path, {**row, "attempts": 200, "ok_count": 190, "loss_pct": 5.0, "computed_at": utc_iso(60)})

    stored = quality_db.query_aggregates(db_path, "1h", "2026-01-10T00:00:00.000Z", "2026-01-11T00:00:00.000Z")
    assert len(stored) == 1
    assert stored[0]["attempts"] == 200
    assert stored[0]["loss_pct"] == 5.0
    assert stored[0]["computed_at"] == utc_iso(60)

    # A row with nothing but the key columns must not build an empty "DO UPDATE SET".
    # It is still rejected, but by the table's NOT NULL constraints — not by broken SQL.
    with pytest.raises(sqlite3.IntegrityError):
        quality_db.upsert_aggregate(
            db_path,
            {"target_id": target.id, "bucket": "1h", "bucket_start": "2026-01-10T10:00:00.000Z"},
        )
    stored = quality_db.query_aggregates(db_path, "1h", "2026-01-10T00:00:00.000Z", "2026-01-11T00:00:00.000Z")
    assert len(stored) == 1
    assert stored[0]["attempts"] == 200

    assert quality_db.mark_aggregates_not_from_raw(
        db_path, target.id, "1h", "2026-01-10T00:00:00.000Z", "2026-01-11T00:00:00.000Z"
    ) == 1
    stored = quality_db.query_aggregates(db_path, "1h", "2026-01-10T00:00:00.000Z", "2026-01-11T00:00:00.000Z")
    assert stored[0]["percentiles_from_raw"] == 0


def test_annotations_load_tests_diagnostics_and_devices(db_path, utc_iso):
    target = _make_target(db_path)

    annotation_id = quality_db.insert_annotation(db_path, utc_iso(0), "zacięcie TV", note="mecz")
    assert [a["label"] for a in quality_db.query_annotations(db_path, utc_iso(-10), utc_iso(10))] == ["zacięcie TV"]
    assert quality_db.delete_annotation(db_path, annotation_id) is True
    assert quality_db.query_annotations(db_path, utc_iso(-10), utc_iso(10)) == []

    load_test_id = quality_db.insert_load_test(
        db_path,
        started_at=utc_iso(0),
        kind="iperf_udp",
        direction="upload",
        params_json="{}",
        status="running",
    )
    quality_db.update_load_test(db_path, load_test_id, ended_at=utc_iso(30), status="ok", result_json="{}")
    assert quality_db.get_load_test(db_path, load_test_id)["status"] == "ok"
    assert [t["id"] for t in quality_db.query_load_tests(db_path, utc_iso(10), utc_iso(20))] == [load_test_id]
    assert quality_db.query_load_tests(db_path, utc_iso(60), utc_iso(90)) == []

    incident_id = quality_db.insert_incident(
        db_path,
        target_id=target.id,
        protocol="icmp",
        kind="outage",
        started_at=utc_iso(0),
        window_seconds=10,
        probe_interval_seconds=1.0,
    )
    quality_db.insert_diagnostic(
        db_path,
        incident_id=incident_id,
        target_id=target.id,
        tool="mtr",
        started_at=utc_iso(5),
        status="ok",
        result_json="{}",
    )
    assert len(quality_db.query_diagnostics(db_path, incident_id=incident_id)) == 1
    assert quality_db.query_diagnostics(db_path, start_iso=utc_iso(60), end_iso=utc_iso(90)) == []

    assert quality_db.get_device(db_path, "nas")["name"] == "NAS (kabel)"
    quality_db.upsert_device(db_path, "macbook", "MacBook", "macos", token_hash="deadbeef")
    quality_db.upsert_device(db_path, "macbook", "MacBook Pro", "macos", last_seen_at=utc_iso(0))
    device = quality_db.get_device(db_path, "macbook")
    assert device["name"] == "MacBook Pro"
    assert device["token_hash"] == "deadbeef"
    assert {d["id"] for d in quality_db.list_devices(db_path)} == {"nas", "macbook"}
