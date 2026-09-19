"""The `/api/quality/*` surface (design spec §12).

Every response has to state the zone it is talking about, and no endpoint may
turn "not measured" into a zero: a range without rows is empty counters and
`None`, never a clean 0 %.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from speedtest_app import api_quality, quality_db
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

    assert payload["measured_from"] == "NAS (kabel)"
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
    points = next(t for t in fine["targets"] if t["target"]["id"] == target.id)["points"]
    assert len(points) == 12
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
    response = client.post("/api/quality/load-tests/run")
    assert response.status_code == 503
    assert response.json()["detail"] == "load tests unavailable"

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
