"""Accessors for the schema v2 tables."""
from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone
from typing import Any

import pytest

from speedtest_app import db, quality_db, retention
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


def test_iter_probe_results_streams_the_same_rows_as_the_list_accessor(db_path, utc_iso):
    """finding C1a: the streaming accessor is the list accessor, one page at a time."""
    target = _make_target(db_path)
    other = _make_target(db_path, name="other-streamed")
    common = dict(duration_ms=1.0, outcome=Outcome.TIMEOUT, timeout_ms=1000)
    rows = [
        ProbeResult(target_id=target.id, protocol=Protocol.ICMP, started_at=utc_iso(i), **common)
        for i in range(12)
    ]
    rows.append(
        ProbeResult(target_id=other.id, protocol=Protocol.TCP, started_at=utc_iso(3), **common)
    )
    quality_db.insert_probe_results(db_path, rows)

    streamed = quality_db.iter_probe_results(db_path, utc_iso(0), utc_iso(60), batch_size=5)
    assert inspect.isgenerator(streamed)
    assert list(streamed) == quality_db.query_probe_results(db_path, utc_iso(0), utc_iso(60))

    for filters in ({"target_id": target.id}, {"protocol": "tcp"}, {"device_id": "nas"}):
        assert list(
            quality_db.iter_probe_results(db_path, utc_iso(0), utc_iso(60), batch_size=3, **filters)
        ) == quality_db.query_probe_results(db_path, utc_iso(0), utc_iso(60), **filters)


def test_iter_probe_results_does_not_materialise_the_whole_range(db_path, utc_iso):
    """The generator yields before the cursor has been drained (finding C1a)."""
    target = _make_target(db_path)
    quality_db.insert_probe_results(
        db_path,
        [
            ProbeResult(
                target_id=target.id,
                protocol=Protocol.ICMP,
                started_at=utc_iso(i),
                duration_ms=1.0,
                outcome=Outcome.OK,
                timeout_ms=1000,
            )
            for i in range(50)
        ],
    )

    stream = quality_db.iter_probe_results(db_path, utc_iso(0), utc_iso(100), batch_size=5)
    first = next(stream)
    assert first["started_at"] == utc_iso(0)
    # closing the abandoned generator must release the connection, not raise
    stream.close()


def test_iter_probe_results_survives_being_advanced_from_other_threads(
    db_path, utc_iso, thread_hopper, db_reader
):
    """A streamed body has no thread affinity (review round 2, critical).

    Starlette drives a sync `StreamingResponse` iterator through
    `iterate_in_threadpool`, so the `fetchmany` after a yield can run on a
    different worker than the one that opened the connection. With the
    default `check_same_thread=True` that used to raise `ProgrammingError`
    inside an already-200 response: a silently truncated CSV.
    """
    target = _make_target(db_path)
    quality_db.insert_probe_results(
        db_path,
        [
            ProbeResult(
                target_id=target.id,
                protocol=Protocol.ICMP,
                started_at=utc_iso(i),
                duration_ms=1.0,
                outcome=Outcome.OK,
                timeout_ms=1000,
            )
            for i in range(25)
        ],
    )
    # a second reader hammering the same database throughout the stream
    db_reader(lambda: quality_db.count_probe_results(db_path, utc_iso(0), utc_iso(100)))

    stream = quality_db.iter_probe_results(db_path, utc_iso(0), utc_iso(100), batch_size=4)
    collected = thread_hopper.drain(stream)

    assert len(collected) == 25  # every page, across ~7 `fetchmany` boundaries
    assert [row["started_at"] for row in collected] == [utc_iso(i) for i in range(25)]


def test_iter_probe_results_closes_its_connection(db_path, utc_iso, monkeypatch):
    """Exhausted or abandoned, the read connection must not be left open."""
    target = _make_target(db_path)
    quality_db.insert_probe_results(
        db_path,
        [
            ProbeResult(
                target_id=target.id,
                protocol=Protocol.ICMP,
                started_at=utc_iso(i),
                duration_ms=1.0,
                outcome=Outcome.OK,
                timeout_ms=1000,
            )
            for i in range(6)
        ],
    )

    opened: list[sqlite3.Connection] = []
    original = db._connect

    def spy(path: str, **kwargs: Any) -> sqlite3.Connection:
        conn = original(path, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, "_connect", spy)

    abandoned = quality_db.iter_probe_results(db_path, utc_iso(0), utc_iso(100), batch_size=2)
    next(abandoned)
    assert len(opened) == 1
    abandoned.close()  # the client hung up after the first page
    with pytest.raises(sqlite3.ProgrammingError):
        opened[-1].execute("PRAGMA user_version")

    drained = list(quality_db.iter_probe_results(db_path, utc_iso(0), utc_iso(100), batch_size=2))
    assert len(drained) == 6
    with pytest.raises(sqlite3.ProgrammingError):
        opened[-1].execute("PRAGMA user_version")


def test_last_result_per_target_uses_the_index_instead_of_scanning(db_path, utc_iso):
    """`/api/targets` must not pay for the whole table to learn eight last rows.

    The window-function form (`ROW_NUMBER() OVER (PARTITION BY target_id …)`)
    reads and sorts every `probe_results` row ever written, so the panel's
    target table got slower every day it ran — ten seconds on a fortnight of
    one-second probes, whatever range the operator asked for. The plan must
    stay a per-target seek on `idx_probe_results_target_time`.
    """
    target = _make_target(db_path)
    other = _make_target(db_path, name="second-target")
    common = dict(duration_ms=1.0, outcome=Outcome.TIMEOUT, timeout_ms=1000)
    quality_db.insert_probe_results(
        db_path,
        [
            ProbeResult(target_id=target.id, protocol=Protocol.ICMP, started_at=utc_iso(10), **common),
            ProbeResult(target_id=target.id, protocol=Protocol.ICMP, started_at=utc_iso(30), **common),
            ProbeResult(target_id=other.id, protocol=Protocol.ICMP, started_at=utc_iso(20), **common),
        ],
    )

    last = quality_db.last_result_per_target(db_path)
    assert last[target.id]["started_at"] == utc_iso(30)
    assert last[other.id]["started_at"] == utc_iso(20)
    assert "rn" not in last[target.id]

    with db.db_conn(db_path) as conn:
        plans = [
            " ".join(str(part) for part in row)
            for statement, params in quality_db.last_result_plans(db_path)
            for row in conn.execute(f"EXPLAIN QUERY PLAN {statement}", params)
        ]
    assert plans, "no statement to explain"
    for plan in plans:
        assert "SCAN probe_results" not in plan, plan
        assert "TEMP B-TREE" not in plan, plan
        assert "idx_probe_results_target_time" in plan, plan


def test_query_probe_metrics_is_the_narrow_twin_of_query_probe_results(db_path, utc_iso):
    """Same rows, same order, only the four columns the statistics read.

    `compute_stats`, `bucket_rows` and `error_kinds_histogram` touch
    `started_at`, `outcome`, `rtt_ms` and `error_kind` and nothing else, but
    the panel used to pull all sixteen columns — `stages_json` and
    `error_detail` included — for every row of the range, twice per refresh
    (once for `/api/quality/stats`, once for `/api/quality/timeline`). Only
    `probes.csv` needs the whole row, and it streams.
    """
    target = _make_target(db_path)
    other = _make_target(db_path, name="metrics-other")
    quality_db.insert_probe_results(
        db_path,
        [
            ProbeResult(
                target_id=target.id, protocol=Protocol.ICMP, started_at=utc_iso(20),
                duration_ms=1.0, outcome=Outcome.OK, timeout_ms=1000, rtt_ms=7.5,
                stages={"dns_ms": 1.0},
            ),
            ProbeResult(
                target_id=target.id, protocol=Protocol.ICMP, started_at=utc_iso(10),
                duration_ms=1.0, outcome=Outcome.ERROR, timeout_ms=1000,
                error_kind="dns", error_detail="NXDOMAIN, at length",
            ),
            ProbeResult(
                target_id=other.id, protocol=Protocol.ICMP, started_at=utc_iso(15),
                duration_ms=1.0, outcome=Outcome.TIMEOUT, timeout_ms=1000,
            ),
        ],
    )

    wide = quality_db.query_probe_results(db_path, utc_iso(0), utc_iso(60))
    narrow = quality_db.query_probe_metrics(db_path, utc_iso(0), utc_iso(60))

    assert set(narrow[0]) == {"started_at", "outcome", "rtt_ms", "error_kind"}
    assert [row["started_at"] for row in narrow] == [row["started_at"] for row in wide]
    assert [row["outcome"] for row in narrow] == [row["outcome"] for row in wide]
    assert [row["rtt_ms"] for row in narrow] == [row["rtt_ms"] for row in wide]
    assert [row["error_kind"] for row in narrow] == [row["error_kind"] for row in wide]

    only_target = quality_db.query_probe_metrics(db_path, utc_iso(0), utc_iso(60), target_id=target.id)
    assert [row["started_at"] for row in only_target] == [utc_iso(10), utc_iso(20)]


# ---------------------------------------------------------------------------
# expected_windows (design spec §7/§11) and the incident `expected` filter
# ---------------------------------------------------------------------------


def test_expected_window_crud(db_path):
    window_id = quality_db.insert_expected_window(
        db_path, name="restart routera", time_from="02:55", time_to="03:15",
        days="[0,1,2,3,4,5,6]", note="codzienny",
    )
    rows = quality_db.list_expected_windows(db_path)
    assert [r["name"] for r in rows] == ["restart routera"]
    assert rows[0]["enabled"] == 1 and rows[0]["created_at"]

    assert quality_db.update_expected_window(db_path, window_id, enabled=0) == 1
    assert quality_db.list_expected_windows(db_path, enabled_only=True) == []

    assert quality_db.delete_expected_window(db_path, window_id) is True
    assert quality_db.delete_expected_window(db_path, window_id) is False


def test_expected_window_rejects_unknown_column(db_path):
    with pytest.raises(ValueError):
        quality_db.insert_expected_window(db_path, name="x", nonsense=1)


def test_incident_expected_columns_round_trip(db_path):
    target = quality_db.list_targets(db_path)[0]
    incident_id = quality_db.insert_incident(
        db_path, target_id=target.id, protocol="icmp", kind="outage",
        started_at="2026-09-21T01:00:00.000Z", window_seconds=10, probe_interval_seconds=1.0,
    )
    quality_db.update_incident(
        db_path, incident_id, expected=1, expected_source="rule", expected_rule_id=None
    )
    row = quality_db.get_incident(db_path, incident_id)
    assert (row["expected"], row["expected_source"]) == (1, "rule")


def test_query_incidents_expected_filter(db_path):
    target = quality_db.list_targets(db_path)[0]
    common = dict(target_id=target.id, protocol="icmp", kind="outage",
                  window_seconds=10, probe_interval_seconds=1.0)
    plain = quality_db.insert_incident(db_path, started_at="2026-09-21T01:00:00.000Z", **common)
    planned = quality_db.insert_incident(db_path, started_at="2026-09-21T02:00:00.000Z", **common)
    quality_db.update_incident(db_path, planned, expected=1, expected_source="rule")

    start, end = "2026-09-21T00:00:00.000Z", "2026-09-21T23:00:00.000Z"

    def ids(mode):
        return [r["id"] for r in quality_db.query_incidents(db_path, start, end, expected=mode)]

    assert ids("all") == [plain, planned]
    assert ids("exclude") == [plain]
    assert ids("only") == [planned]
    assert ids("nonsense") == [plain, planned]      # unknown value falls back to "all"


def test_retention_never_deletes_expected_windows(db_path):
    """Rules are configuration, not measurements (spec §11).

    `retention.py`'s real entry point takes explicit settings/targets rather
    than reading the `settings` table itself (see `test_retention.py`), so
    this drives it directly instead of the plan's placeholder `set_setting` +
    bare `run_retention(db_path)` call.
    """
    quality_db.insert_expected_window(
        db_path, name="restart routera", time_from="02:55", time_to="03:15", days="[0]"
    )
    settings = retention.RetentionSettings(
        raw_days=1, aggregate_days=1, incident_days=1, load_test_raw_days=1, diagnostics_days=1,
    )
    retention.run_retention(db_path, settings, now=datetime(2026, 9, 21, tzinfo=timezone.utc), targets=[])
    assert len(quality_db.list_expected_windows(db_path)) == 1
