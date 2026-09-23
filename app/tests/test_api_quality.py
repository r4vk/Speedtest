"""The `/api/quality/*` surface (design spec §12).

Every response has to state the zone it is talking about, and no endpoint may
turn "not measured" into a zero: a range without rows is empty counters and
`None`, never a clean 0 %.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from speedtest_app import api_quality, quality_db, quality_views
from speedtest_app.db import db_conn
from speedtest_app.probe_types import Outcome, ProbeResult, Protocol
from speedtest_app.stats import compute_stats
from speedtest_app.time_utils import to_iso_z, utc_now


def _target(db_path: str, name: str = "probe-a", protocol: str = "icmp"):
    return quality_db.insert_target(
        db_path,
        name=name,
        kind="internet",
        protocol=protocol,
        host="1.1.1.1",
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=1,
    )


def _seed_results(db_path: str, target_id: int, start, count: int = 12) -> None:
    rows = []
    for index in range(count):
        outcome = Outcome.TIMEOUT if index % 4 == 3 else Outcome.OK
        rows.append(
            ProbeResult(
                target_id=target_id,
                protocol=Protocol.ICMP,
                started_at=to_iso_z(start + timedelta(seconds=index * 10)),
                duration_ms=20.0,
                outcome=outcome,
                timeout_ms=1000,
                rtt_ms=None if outcome is Outcome.TIMEOUT else float(20 + index),
            )
        )
    quality_db.insert_probe_results(db_path, rows)


def _legacy_check(db_path: str, checked_at: str, is_up: int) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO connectivity_checks(checked_at, is_up, latency_ms) VALUES (?,?,?)",
            (checked_at, is_up, 15.0 if is_up else None),
        )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_reports_the_running_monitor(client) -> None:
    payload = client.get("/api/quality/status").json()

    assert payload["measured_from"] == "urządzenie, na którym działa kontener"
    assert payload["tz"]
    assert payload["availability"] == "no_data"
    assert payload["quality"] == "unknown"
    assert payload["session"]["started_at"]
    assert payload["session"]["ended_at"] is None
    assert payload["blocked_reason"] == "disabled"  # ping is off in the fixture
    assert "coverage_24h_pct" in payload
    assert set(payload["scheduler"]) >= {"buffered_rows", "dropped_rows", "skipped_ticks"}


def test_status_without_an_engine_says_no_data(client) -> None:
    del client.app.state.quality_engine
    payload = client.get("/api/quality/status").json()

    assert payload["availability"] == "no_data"
    assert payload["quality"] == "unknown"
    assert payload["icmp_method"] == "unknown"
    assert payload["open_incidents"] == []
    assert payload["targets"] == []


def test_status_lists_open_incidents_in_local_time(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    del client.app.state.quality_engine
    quality_db.insert_incident(
        db_path,
        target_id=target.id,
        protocol="icmp",
        kind="degraded",
        started_at=to_iso_z(utc_now() - timedelta(minutes=5)),
        window_seconds=10,
        probe_interval_seconds=1.0,
        windows_degraded=2,
    )

    incident = client.get("/api/quality/status").json()["open_incidents"][0]
    assert incident["target_name"] == "probe-a"
    assert not incident["started_at"].endswith("Z")


def test_status_fields_win_over_whatever_the_engine_reports(client) -> None:
    """finding 9: `**status` is spread first, so it can never override `now`/

    `tz`/`session`/`coverage_24h_pct` — an engine that happened to return a
    key of the same name must not be able to forge them.
    """
    client.app.state.quality_engine = SimpleNamespace(
        status=lambda: {
            "availability": "up",
            "quality": "ok",
            "lan_degraded": False,
            "icmp_method": "raw",
            "open_incidents": [],
            "blocked_reason": None,
            "scheduler": {"buffered_rows": 0, "dropped_rows": 0, "skipped_ticks": {}},
            "targets": [],
            "now": "not-a-real-time",
            "tz": "NOWHERE",
            "session": "forged",
            "coverage_24h_pct": -1,
        }
    )
    payload = client.get("/api/quality/status").json()
    assert payload["now"] != "not-a-real-time"
    assert payload["tz"] != "NOWHERE"
    assert payload["session"] != "forged"
    assert payload["coverage_24h_pct"] != -1
    # the engine's own fields still come through
    assert payload["availability"] == "up"
    assert payload["quality"] == "ok"


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_stats_match_compute_stats_on_the_same_rows(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    start = utc_now().replace(microsecond=0) - timedelta(minutes=10)
    end = start + timedelta(minutes=2)
    _seed_results(db_path, target.id, start)

    payload = client.get(
        "/api/quality/stats", params={"from": to_iso_z(start), "to": to_iso_z(end)}
    ).json()
    entry = next(t for t in payload["targets"] if t["target"]["id"] == target.id)

    rows = quality_db.query_probe_results(db_path, to_iso_z(start), to_iso_z(end), target_id=target.id)
    assert entry["stats"] == api_quality.stats_payload(compute_stats(rows))
    assert entry["note"] == "Utrata odpowiedzi ICMP echo"
    assert entry["data_source"] == "raw"
    assert payload["tz"]
    assert payload["coverage"]["coverage_known"] is True


def test_stats_note_depends_on_the_protocol(client) -> None:
    db_path = client.app_db_path
    _target(db_path, name="dns-extra", protocol="dns")
    payload = client.get("/api/quality/stats").json()
    notes = {t["target"]["name"]: t["note"] for t in payload["targets"]}

    assert notes["dns-extra"] == "Błędy/timeouty zapytań DNS"
    assert notes["https-cloudflare"] == "Błędy/timeouty HTTPS (DNS, TCP, TLS, HTTP)"
    assert notes["legacy-tcp"] == "Nieudane zestawienia TCP"


def test_stats_count_error_kinds_separately_from_loss(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    start = utc_now() - timedelta(minutes=5)
    quality_db.insert_probe_results(
        db_path,
        [
            ProbeResult(
                target_id=target.id,
                protocol=Protocol.ICMP,
                started_at=to_iso_z(start),
                duration_ms=1.0,
                outcome=Outcome.ERROR,
                timeout_ms=1000,
                error_kind="permission",
            ),
            ProbeResult(
                target_id=target.id,
                protocol=Protocol.ICMP,
                started_at=to_iso_z(start + timedelta(seconds=1)),
                duration_ms=1.0,
                outcome=Outcome.ERROR,
                timeout_ms=1000,
                error_kind="permission",
            ),
        ],
    )

    entry = next(
        t for t in client.get("/api/quality/stats").json()["targets"] if t["target"]["id"] == target.id
    )
    assert entry["error_kinds"] == {"permission": 2}
    assert entry["stats"]["errors"] == 2
    # two errors are no measurement at all: never 0 % loss, never 100 %
    assert entry["stats"]["loss_pct"] is None


def test_legacy_tcp_counters_have_no_loss(client) -> None:
    db_path = client.app_db_path
    now = utc_now()
    _legacy_check(db_path, to_iso_z(now - timedelta(minutes=3)), 1)
    _legacy_check(db_path, to_iso_z(now - timedelta(minutes=2)), 0)

    legacy = client.get("/api/quality/stats").json()["legacy_tcp"]
    assert legacy == {"attempts": 2, "failures": 1}
    assert "loss_pct" not in legacy


def test_legacy_tcp_is_null_without_history(client) -> None:
    assert client.get("/api/quality/stats").json()["legacy_tcp"] is None


# ---------------------------------------------------------------------------
# aggregate fallback once raw rows are gone (finding 2)
# ---------------------------------------------------------------------------

def _hour_aggregate(db_path: str, target_id: int, bucket_start, attempts: int) -> None:
    quality_db.upsert_aggregate(
        db_path,
        {
            "target_id": target_id,
            "protocol": "icmp",
            "bucket": "1h",
            "bucket_start": to_iso_z(bucket_start),
            "attempts": attempts,
            "ok_count": attempts,
            "timeout_count": 0,
            "error_count": 0,
            "loss_pct": 0.0,
            "rtt_p95_ms": 20.0,
            "percentiles_from_raw": 0,
            "computed_at": to_iso_z(utc_now()),
        },
    )


def test_stats_aggregate_fallback_never_reports_a_sub_hour_range_as_the_whole_hour(client) -> None:
    """`?from=10:00&to=10:30` used to pool the whole hourly bucket (finding 2)."""
    db_path = client.app_db_path
    target = _target(db_path, name="agg-sub-hour")
    hour_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    _hour_aggregate(db_path, target.id, hour_start, attempts=100)

    payload = client.get(
        "/api/quality/stats",
        params={"from": to_iso_z(hour_start), "to": to_iso_z(hour_start + timedelta(minutes=30))},
    ).json()
    entry = next(t for t in payload["targets"] if t["target"]["id"] == target.id)

    assert entry["data_source"] == "none"
    assert entry["stats"]["attempts"] == 0
    assert entry["covered_from"] is None
    assert entry["covered_to"] is None


def test_stats_aggregate_fallback_never_pools_a_bucket_the_range_only_touches(client) -> None:
    """`?from=10:20&to=11:00` used to be able to grab the next, unrelated hour

    because the old filter only checked ``bucket_start <= to`` (finding 2):
    a bucket_start exactly at `to` would qualify even though the range never
    overlaps that hour by more than an instant.
    """
    db_path = client.app_db_path
    target = _target(db_path, name="agg-boundary")
    hour_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    _hour_aggregate(db_path, target.id, hour_start, attempts=100)
    _hour_aggregate(db_path, target.id, hour_start + timedelta(hours=1), attempts=999)

    payload = client.get(
        "/api/quality/stats",
        params={
            "from": to_iso_z(hour_start + timedelta(minutes=20)),
            "to": to_iso_z(hour_start + timedelta(hours=1)),
        },
    ).json()
    entry = next(t for t in payload["targets"] if t["target"]["id"] == target.id)

    assert entry["data_source"] == "none"
    assert entry["stats"]["attempts"] == 0


def test_stats_aggregate_fallback_pools_only_fully_contained_buckets(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path, name="agg-wide")
    hour_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    _hour_aggregate(db_path, target.id, hour_start, attempts=100)
    _hour_aggregate(db_path, target.id, hour_start + timedelta(hours=1), attempts=50)

    payload = client.get(
        "/api/quality/stats",
        params={
            "from": to_iso_z(hour_start - timedelta(minutes=10)),
            "to": to_iso_z(hour_start + timedelta(hours=2)),
        },
    ).json()
    entry = next(t for t in payload["targets"] if t["target"]["id"] == target.id)

    assert entry["data_source"] == "aggregates"
    assert entry["stats"]["attempts"] == 150
    assert entry["covered_from"] == api_quality.local_iso(hour_start)
    assert entry["covered_to"] == api_quality.local_iso(hour_start + timedelta(hours=2))


def test_timeline_falls_back_to_aggregates_when_raw_rows_are_gone(client) -> None:
    """finding 2c: the chart is not empty under a populated counter."""
    db_path = client.app_db_path
    target = _target(db_path, name="agg-timeline")
    hour_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    _hour_aggregate(db_path, target.id, hour_start, attempts=100)
    _hour_aggregate(db_path, target.id, hour_start + timedelta(hours=1), attempts=50)

    payload = client.get(
        "/api/quality/timeline",
        params={
            "from": to_iso_z(hour_start),
            "to": to_iso_z(hour_start + timedelta(hours=2)),
            "bucket_seconds": 60,
        },
    ).json()
    series = next(t for t in payload["targets"] if t["target"]["id"] == target.id)

    assert series["data_source"] == "aggregates"
    assert [point["attempts"] for point in series["points"]] == [100, 50]
    assert sum(point["attempts"] for point in series["points"]) == 150


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------

def test_timeline_clamps_the_bucket_and_caps_the_points(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    start = utc_now().replace(microsecond=0) - timedelta(minutes=10)
    end = start + timedelta(minutes=2)
    _seed_results(db_path, target.id, start)

    fine = client.get(
        "/api/quality/timeline",
        params={"from": to_iso_z(start), "to": to_iso_z(end), "bucket_seconds": 1},
    ).json()
    assert fine["bucket_seconds"] == 10  # the 10 s floor of the spec
    fine_series = next(t for t in fine["targets"] if t["target"]["id"] == target.id)
    points = fine_series["points"]
    assert fine_series["data_source"] == "raw"
    # 12 complete 10 s windows: the range divides evenly and no row sits
    # exactly on `to`, so the zero-width closing point is left out — it could
    # only ever have said "no data" about no time at all.
    assert len(points) == 12
    assert [point["partial"] for point in points] == [False] * 12
    assert sum(point["attempts"] for point in points) == 12
    assert fine["last_complete_bucket"] is not None

    wide = client.get(
        "/api/quality/timeline",
        params={
            "from": to_iso_z(utc_now() - timedelta(days=30)),
            "to": to_iso_z(utc_now()),
            "bucket_seconds": 10,
        },
    ).json()
    assert wide["bucket_seconds"] > 10
    for series in wide["targets"]:
        assert len(series["points"]) <= 2000


def test_timeline_carries_the_shared_axis(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    now = utc_now()
    start = now - timedelta(minutes=30)
    quality_db.insert_incident(
        db_path,
        target_id=target.id,
        protocol="icmp",
        kind="degraded",
        started_at=to_iso_z(now - timedelta(minutes=20)),
        ended_at=to_iso_z(now - timedelta(minutes=15)),
        window_seconds=10,
        probe_interval_seconds=1.0,
        windows_degraded=2,
    )
    quality_db.insert_annotation(db_path, to_iso_z(now - timedelta(minutes=18)), "zacięcie TV")
    quality_db.insert_load_test(
        db_path,
        started_at=to_iso_z(now - timedelta(minutes=10)),
        ended_at=to_iso_z(now - timedelta(minutes=9)),
        kind="iperf_udp",
        direction="upload",
        server="iperf.example",
        params_json="{}",
        status="ok",
    )

    payload = client.get(
        "/api/quality/timeline", params={"from": to_iso_z(start), "to": to_iso_z(now)}
    ).json()
    assert len(payload["incidents"]) == 1
    assert len(payload["annotations"]) == 1
    assert payload["annotations"][0]["source"] == "user"
    assert len(payload["load_tests"]) == 1
    assert isinstance(payload["gaps"], list)
    assert payload["tz"]


# ---------------------------------------------------------------------------
# incidents and annotations
# ---------------------------------------------------------------------------

def _incident(db_path: str, target_id: int, now) -> int:
    return quality_db.insert_incident(
        db_path,
        target_id=target_id,
        protocol="icmp",
        kind="degraded",
        started_at=to_iso_z(now - timedelta(minutes=20)),
        ended_at=to_iso_z(now - timedelta(minutes=10)),
        closed_at=to_iso_z(now - timedelta(minutes=9)),
        close_reason="recovered",
        window_seconds=10,
        probe_interval_seconds=1.0,
        peak_loss_pct=40.0,
        peak_p95_rtt_ms=200.0,
        longest_fail_streak=5,
        windows_degraded=4,
        summary_json=json.dumps(
            [
                [to_iso_z(now - timedelta(minutes=20)), "degraded", 40.0, 200.0],
                [to_iso_z(now - timedelta(minutes=19)), "healthy", 0.0, 20.0],
            ]
        ),
    )


def test_incident_list_and_detail(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    other = _target(db_path, name="probe-b")
    now = utc_now()
    incident_id = _incident(db_path, target.id, now)
    _seed_results(db_path, other.id, now - timedelta(minutes=20))
    quality_db.insert_annotation(db_path, to_iso_z(now - timedelta(minutes=15)), "zacięcie TV")
    quality_db.insert_diagnostic(
        db_path,
        incident_id=incident_id,
        target_id=target.id,
        tool="mtr",
        started_at=to_iso_z(now - timedelta(minutes=19)),
        status="ok",
        result_json=json.dumps({"hops": []}),
    )

    listed = client.get(
        "/api/quality/incidents", params={"from": to_iso_z(now - timedelta(hours=1)), "to": to_iso_z(now)}
    ).json()
    assert [item["id"] for item in listed["items"]] == [incident_id]
    assert listed["items"][0]["target_name"] == "probe-a"
    assert listed["items"][0]["windows_count"] == 2
    assert listed["tz"]

    detail = client.get(f"/api/quality/incidents/{incident_id}").json()
    assert [window["verdict"] for window in detail["windows"]] == ["degraded", "healthy"]
    assert detail["windows"][0]["loss_pct"] == 40.0
    assert [diag["tool"] for diag in detail["diagnostics"]] == ["mtr"]
    assert {entry["target"]["name"] for entry in detail["related_targets"]} == {
        t.name for t in quality_db.list_targets(db_path) if t.id != target.id
    }
    related = next(e for e in detail["related_targets"] if e["target"]["id"] == other.id)
    assert related["stats"]["attempts"] > 0
    assert detail["annotations"][0]["label"] == "zacięcie TV"
    assert detail["annotations"][0]["source"] == "user"

    assert client.get("/api/quality/incidents/9999").status_code == 404


def test_annotations_are_created_and_deleted(client) -> None:
    now = utc_now()
    created = client.post(
        "/api/quality/annotations",
        json={"at": to_iso_z(now - timedelta(minutes=1)), "label": "zacięcie TV", "note": "film"},
    )
    assert created.status_code == 200
    annotation = created.json()["annotation"]
    assert annotation["source"] == "user"
    assert not annotation["at"].endswith("Z")

    rows = quality_db.query_annotations(
        client.app_db_path, to_iso_z(now - timedelta(hours=1)), to_iso_z(now)
    )
    assert [row["label"] for row in rows] == ["zacięcie TV"]

    assert client.delete(f"/api/quality/annotations/{annotation['id']}").json()["deleted"] is True
    assert client.delete(f"/api/quality/annotations/{annotation['id']}").status_code == 404


def test_annotation_needs_a_label_and_a_parsable_time(client) -> None:
    assert client.post("/api/quality/annotations", json={"label": "   "}).status_code == 422
    assert client.post("/api/quality/annotations", json={"label": "x", "at": "wczoraj"}).status_code == 422
    # without `at` the annotation lands on now
    assert client.post("/api/quality/annotations", json={"label": "x"}).status_code == 200


# ---------------------------------------------------------------------------
# coverage, load tests, diagnostics
# ---------------------------------------------------------------------------

def test_coverage_endpoint_reports_gaps(client) -> None:
    now = utc_now()
    payload = client.get(
        "/api/quality/coverage", params={"from": to_iso_z(now - timedelta(hours=2)), "to": to_iso_z(now)}
    ).json()

    assert payload["coverage_known"] is True
    assert payload["total_seconds"] > 0
    assert payload["gaps"]  # the monitor was not running two hours ago
    assert payload["gaps"][0]["reason"] in {"not_running", "disabled"}
    assert payload["tz"]


def test_load_tests_are_listed_and_fetched(client) -> None:
    db_path = client.app_db_path
    now = utc_now()
    load_test_id = quality_db.insert_load_test(
        db_path,
        started_at=to_iso_z(now - timedelta(minutes=5)),
        ended_at=to_iso_z(now - timedelta(minutes=4)),
        kind="iperf_udp",
        direction="upload",
        server="iperf.example",
        params_json=json.dumps({"port": 5201}),
        status="ok",
        result_json=json.dumps(
            {
                "kind": "iperf_udp",
                "direction": "upload",
                "receiver": {"packets": 200, "lost_packets": 4, "jitter_ms": 0.8},
                "sender": {},
            }
        ),
        raw_json="{}",
    )

    listed = client.get("/api/quality/load-tests").json()
    assert [item["id"] for item in listed["items"]] == [load_test_id]
    assert listed["items"][0]["summaries"][0]["loss_pct"] == 2.0
    assert "raw_json" not in listed["items"][0]

    detail = client.get(f"/api/quality/load-tests/{load_test_id}").json()
    assert detail["load_test"]["raw_json"] == "{}"
    assert detail["load_test"]["params"] == {"port": 5201}
    assert client.get("/api/quality/load-tests/404").status_code == 404


def test_running_a_load_test_needs_the_engine(client) -> None:
    real_engine = client.app.state.quality_engine

    # A build without the load-test runner answers 503 instead of pretending.
    client.app.state.quality_engine = SimpleNamespace()
    response = client.post("/api/quality/load-tests/run")
    assert response.status_code == 503
    assert response.json()["detail"] == "load tests unavailable"

    # The real engine runs; with no iperf3 server configured the row says so
    # instead of reporting a fake result.
    client.app.state.quality_engine = real_engine
    response = client.post("/api/quality/load-tests/run")
    assert response.status_code == 200
    load_test_id = response.json()["id"]
    assert isinstance(load_test_id, int)
    row = client.get(f"/api/quality/load-tests/{load_test_id}").json()["load_test"]
    assert row["status"] == "skipped"
    assert row["error"] == "not_configured"

    client.app.state.quality_engine = SimpleNamespace(run_load_test=lambda trigger: 7)
    assert client.post("/api/quality/load-tests/run").json()["id"] == 7

    async def _run(trigger: str) -> dict:
        return {"id": 11, "trigger": trigger}

    client.app.state.quality_engine = SimpleNamespace(run_load_test=_run)
    assert client.post("/api/quality/load-tests/run").json()["id"] == 11


def test_diagnostics_are_listed_and_filtered(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    now = utc_now()
    incident_id = _incident(db_path, target.id, now)
    quality_db.insert_diagnostic(
        db_path,
        incident_id=incident_id,
        target_id=target.id,
        tool="mtr",
        started_at=to_iso_z(now - timedelta(minutes=15)),
        status="ok",
        result_json=json.dumps({"hops": [{"host": "10.0.0.1"}]}),
    )
    quality_db.insert_diagnostic(
        db_path,
        target_id=target.id,
        tool="mtr",
        started_at=to_iso_z(now - timedelta(minutes=14)),
        status="error",
        error="rate_limited",
    )

    payload = client.get("/api/quality/diagnostics").json()
    assert len(payload["items"]) == 2
    assert payload["items"][0]["result"] == {"hops": [{"host": "10.0.0.1"}]}
    assert payload["items"][1]["error"] == "rate_limited"
    assert payload["tz"]

    filtered = client.get("/api/quality/diagnostics", params={"incident_id": incident_id}).json()
    assert len(filtered["items"]) == 1


def test_every_quality_response_states_the_zone(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    now = utc_now()
    incident_id = _incident(db_path, target.id, now)
    load_test_id = quality_db.insert_load_test(
        db_path,
        started_at=to_iso_z(now - timedelta(minutes=5)),
        kind="iperf_udp",
        direction="upload",
        params_json="{}",
        status="running",
    )

    paths = [
        "/api/targets",
        "/api/quality/status",
        "/api/quality/stats",
        "/api/quality/timeline",
        "/api/quality/incidents",
        f"/api/quality/incidents/{incident_id}",
        "/api/quality/coverage",
        "/api/quality/load-tests",
        f"/api/quality/load-tests/{load_test_id}",
        "/api/quality/diagnostics",
    ]
    for path in paths:
        payload = client.get(path).json()
        assert payload.get("tz"), path


# ---------------------------------------------------------------------------
# range parsing (finding 8)
# ---------------------------------------------------------------------------

def test_an_unparsable_range_is_a_422_not_a_500(client) -> None:
    """`parse_dt` raises `ValueError` on garbage input; the shared `get_range`

    dependency turns that into a 422 for every quality endpoint instead of
    letting it become an unhandled 500.
    """
    for path in ("/api/quality/stats", "/api/quality/timeline", "/api/quality/incidents"):
        response = client.get(path, params={"from": "not-a-date"})
        assert response.status_code == 422, path


# ---------------------------------------------------------------------------
# the raw-range cap and the aggregate point cap (review findings C1b, I6)
# ---------------------------------------------------------------------------

def _day_aggregate(db_path: str, target_id: int, bucket_start, attempts: int) -> None:
    quality_db.upsert_aggregate(
        db_path,
        {
            "target_id": target_id,
            "protocol": "icmp",
            "bucket": "1d",
            "bucket_start": to_iso_z(bucket_start),
            "attempts": attempts,
            "ok_count": attempts,
            "timeout_count": 0,
            "error_count": 0,
            "loss_pct": 0.0,
            "rtt_p95_ms": 20.0,
            "percentiles_from_raw": 0,
            "computed_at": to_iso_z(utc_now()),
        },
    )


def test_status_states_the_raw_range_limit(client) -> None:
    payload = client.get("/api/quality/status").json()
    assert payload["raw_range_max_days"] == quality_views.RAW_RANGE_MAX_DAYS == 31


def test_stats_of_a_range_wider_than_the_raw_limit_use_aggregates(client) -> None:
    """finding C1b: a two-month range never materialises raw rows, even if they exist."""
    db_path = client.app_db_path
    target = _target(db_path, name="wide-range")
    now = utc_now().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=60)
    # raw rows inside the range *and* an aggregate for an older hour of it
    _seed_results(db_path, target.id, now - timedelta(days=10))
    _hour_aggregate(db_path, target.id, start + timedelta(days=1), attempts=3600)

    payload = client.get(
        "/api/quality/stats", params={"from": to_iso_z(start), "to": to_iso_z(now)}
    ).json()
    entry = next(t for t in payload["targets"] if t["target"]["id"] == target.id)
    assert entry["data_source"] == "aggregates"
    assert entry["stats"]["attempts"] == 3600

    # just inside the limit the same target is served from raw rows
    narrow = client.get(
        "/api/quality/stats",
        params={
            "from": to_iso_z(now - timedelta(days=quality_views.RAW_RANGE_MAX_DAYS)),
            "to": to_iso_z(now),
        },
    ).json()
    narrow_entry = next(t for t in narrow["targets"] if t["target"]["id"] == target.id)
    assert narrow_entry["data_source"] == "raw"


def test_timeline_of_a_three_month_range_uses_daily_aggregates(client) -> None:
    """finding I6: the aggregate fallback is capped the way the raw path is."""
    db_path = client.app_db_path
    target = _target(db_path, name="quarter")
    now = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = now - timedelta(days=92)
    _day_aggregate(db_path, target.id, start + timedelta(days=1), attempts=86400)
    # an hourly aggregate inside the same range must not be the one picked
    _hour_aggregate(db_path, target.id, start + timedelta(days=1), attempts=3600)

    payload = client.get(
        "/api/quality/timeline", params={"from": to_iso_z(start), "to": to_iso_z(now)}
    ).json()
    series = next(t for t in payload["targets"] if t["target"]["id"] == target.id)

    assert series["data_source"] == "aggregates"
    assert series["bucket"] == "1d"
    assert [point["attempts"] for point in series["points"]] == [86400]
    assert len(series["points"]) <= 2000

    # the same range in the stats endpoint pools the same buckets
    stats = client.get(
        "/api/quality/stats", params={"from": to_iso_z(start), "to": to_iso_z(now)}
    ).json()
    entry = next(t for t in stats["targets"] if t["target"]["id"] == target.id)
    assert entry["stats"]["attempts"] == 86400


def test_timeline_keeps_hourly_buckets_while_they_fit_the_budget(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path, name="hourly-budget")
    hour_start = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    _hour_aggregate(db_path, target.id, hour_start, attempts=100)

    payload = client.get(
        "/api/quality/timeline",
        params={"from": to_iso_z(hour_start), "to": to_iso_z(hour_start + timedelta(hours=2))},
    ).json()
    series = next(t for t in payload["targets"] if t["target"]["id"] == target.id)
    assert series["bucket"] == "1h"
    assert series["data_source"] == "aggregates"


def test_aggregate_bucket_for_switches_at_the_point_budget() -> None:
    now = utc_now()
    assert quality_views.aggregate_bucket_for(now - timedelta(days=30), now) == "1h"
    # 2000 h is the cap; a hair more has to drop to daily rows
    assert quality_views.aggregate_bucket_for(now - timedelta(hours=2000), now) == "1h"
    assert quality_views.aggregate_bucket_for(now - timedelta(hours=2001), now) == "1d"


# ---------------------------------------------------------------------------
# expected-window rules (Task 7 — design spec §7)
# ---------------------------------------------------------------------------

def test_expected_window_crud_over_http(client) -> None:
    created = client.post("/api/quality/expected-windows", json={
        "name": "restart routera", "time_from": "02:55", "time_to": "03:15",
        "days": [0, 1, 2, 3, 4, 5, 6], "note": "codzienny",
    })
    assert created.status_code == 201
    window = created.json()["window"]
    assert window["days"] == [0, 1, 2, 3, 4, 5, 6] and window["enabled"] is True

    listed = client.get("/api/quality/expected-windows").json()["windows"]
    assert [w["name"] for w in listed] == ["restart routera"]

    updated = client.put(f"/api/quality/expected-windows/{window['id']}", json={
        "name": "restart routera", "time_from": "02:50", "time_to": "03:20",
        "days": [0], "enabled": False,
    })
    assert updated.json()["window"]["time_from"] == "02:50"
    assert updated.json()["window"]["enabled"] is False

    assert client.delete(f"/api/quality/expected-windows/{window['id']}").status_code == 200
    assert client.delete(f"/api/quality/expected-windows/{window['id']}").status_code == 404


@pytest.mark.parametrize("body", [
    {"name": "", "time_from": "02:55", "time_to": "03:15", "days": [0]},
    {"name": "x", "time_from": "krowa", "time_to": "03:15", "days": [0]},
    {"name": "x", "time_from": "02:55", "time_to": "25:00", "days": [0]},
    {"name": "x", "time_from": "03:00", "time_to": "03:00", "days": [0]},
    {"name": "x", "time_from": "02:55", "time_to": "03:15", "days": []},
    {"name": "x", "time_from": "02:55", "time_to": "03:15", "days": [9]},
    {"name": "x", "time_from": "02:55", "time_to": "03:15", "days": [0], "target_id": 9999},
])
def test_expected_window_validation(client, body) -> None:
    assert client.post("/api/quality/expected-windows", json=body).status_code == 422


def test_expected_window_mutation_is_audited(client) -> None:
    client.post("/api/quality/expected-windows", json={
        "name": "restart routera", "time_from": "02:55", "time_to": "03:15", "days": [0],
    })
    changes = quality_db.query_config_changes(
        client.app_db_path, "2000-01-01T00:00:00.000Z", "2100-01-01T00:00:00.000Z"
    )
    assert any(c["key"].startswith("expected_window") for c in changes)


# ---------------------------------------------------------------------------
# marking one closed outage by hand (Task 8 — design spec §6)
# ---------------------------------------------------------------------------

def _closed_incident(client, started_at: str = "2026-09-21T01:00:00.000Z", **fields) -> int:
    """A closed incident on the first seeded target, ready to be marked."""
    db_path = client.app_db_path
    target = quality_db.list_targets(db_path)[0]
    fields.setdefault("ended_at", "2026-09-21T01:04:00.000Z")
    fields.setdefault("closed_at", "2026-09-21T01:05:00.000Z")
    fields.setdefault("close_reason", "recovered")
    return quality_db.insert_incident(
        db_path,
        target_id=target.id,
        protocol="icmp",
        kind="outage",
        started_at=started_at,
        window_seconds=10,
        probe_interval_seconds=1.0,
        **fields,
    )


def _open_incident(client, started_at: str = "2026-09-21T01:00:00.000Z") -> int:
    db_path = client.app_db_path
    target = quality_db.list_targets(db_path)[0]
    return quality_db.insert_incident(
        db_path,
        target_id=target.id,
        protocol="icmp",
        kind="outage",
        started_at=started_at,
        window_seconds=10,
        probe_interval_seconds=1.0,
    )


def test_mark_incident_expected_by_hand(client) -> None:
    incident_id = _closed_incident(client)
    response = client.patch(
        f"/api/quality/incidents/{incident_id}/expected",
        json={"expected": True, "note": "restart routera"},
    )
    assert response.status_code == 200
    assert response.json()["incident"]["expected"] == 1
    assert response.json()["incident"]["expected_source"] == "manual"

    detail = client.get(f"/api/quality/incidents/{incident_id}").json()
    assert any(a["label"] == "expected" for a in detail["annotations"])


def test_manual_unmark_records_that_a_person_looked(client) -> None:
    """Review Focus #5: a manual `false` is not the same as "nobody looked"."""
    incident_id = _closed_incident(client, expected=1, expected_source="rule")
    response = client.patch(
        f"/api/quality/incidents/{incident_id}/expected", json={"expected": False}
    )
    assert response.json()["incident"]["expected"] == 0
    assert response.json()["incident"]["expected_source"] == "manual"


def test_manual_unmark_keeps_the_rule_that_had_matched(client) -> None:
    """Spec §6: a hand verdict leaves `expected_rule_id` untouched.

    The rule id is the very thing a "no, this one was real" verdict argues
    against — which window had claimed the outage — so nulling it destroys the
    evidence the verdict is about, and leaves `expected_source = 'manual'`
    standing alone with nothing to say what it overruled.
    """
    window_id = quality_db.insert_expected_window(
        client.app_db_path,
        name="restart routera",
        time_from="02:55",
        time_to="03:15",
        days="[0,1,2,3,4,5,6]",
    )
    incident_id = _closed_incident(
        client, expected=1, expected_source="rule", expected_rule_id=window_id
    )

    marked = client.patch(
        f"/api/quality/incidents/{incident_id}/expected", json={"expected": False}
    ).json()["incident"]

    assert (marked["expected"], marked["expected_source"]) == (0, "manual")
    assert marked["expected_rule_id"] == window_id


def test_open_incident_cannot_be_marked(client) -> None:
    incident_id = _open_incident(client)
    assert (
        client.patch(
            f"/api/quality/incidents/{incident_id}/expected", json={"expected": True}
        ).status_code
        == 409
    )


def test_marking_an_unknown_incident_is_404(client) -> None:
    assert (
        client.patch("/api/quality/incidents/424242/expected", json={"expected": True}).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# the expected filter across the reads (Task 9 — design spec §10)
# ---------------------------------------------------------------------------

#: Spelled in UTC: `parse_dt` reads a naked `2026-09-21T00:00` as *local*, so a
#: wall-clock range would slide off the fixtures on any machine but UTC+0.
INCIDENT_RANGE = {"from": "2026-09-21T00:00:00.000Z", "to": "2026-09-21T23:00:00.000Z"}


def test_incidents_endpoint_filters_expected(client) -> None:
    plain = _closed_incident(client, started_at="2026-09-21T01:00:00.000Z")
    planned = _closed_incident(
        client,
        started_at="2026-09-21T02:00:00.000Z",
        ended_at="2026-09-21T02:04:00.000Z",
        closed_at="2026-09-21T02:05:00.000Z",
        expected=1,
        expected_source="rule",
    )

    body = client.get("/api/quality/incidents", params=INCIDENT_RANGE).json()
    assert [i["id"] for i in body["items"]] == [plain, planned]
    assert body["expected_filter"] == "all" and body["expected_hidden"] == 0

    body = client.get(
        "/api/quality/incidents", params={**INCIDENT_RANGE, "expected": "exclude"}
    ).json()
    assert [i["id"] for i in body["items"]] == [plain]
    assert body["expected_filter"] == "exclude" and body["expected_hidden"] == 1

    only = client.get("/api/quality/incidents", params={**INCIDENT_RANGE, "expected": "only"}).json()
    assert [i["id"] for i in only["items"]] == [planned]

    # a typo must never hide an outage
    typo = client.get("/api/quality/incidents", params={**INCIDENT_RANGE, "expected": "krowa"}).json()
    assert [i["id"] for i in typo["items"]] == [plain, planned]
    assert typo["expected_filter"] == "all"
