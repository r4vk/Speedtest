"""The printable ISP report (design spec §13).

The report is only trustworthy if its numbers are the API's numbers, so the
first test compares the model with `/api/quality/stats` field by field.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest

from speedtest_app import db, quality_db, report
from speedtest_app.probe_types import Outcome, ProbeResult, Protocol
from speedtest_app.time_utils import parse_dt, to_iso_z, utc_now


def _target(db_path: str, name: str = "report-icmp"):
    return quality_db.insert_target(
        db_path,
        name=name,
        kind="internet",
        protocol="icmp",
        host="1.1.1.1",
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=1,
    )


def _seed(db_path: str, target_id: int, start, count: int = 30) -> None:
    rows = []
    for index in range(count):
        started_at = to_iso_z(start + timedelta(seconds=index * 10))
        if index % 5 == 0:
            rows.append(
                ProbeResult(
                    target_id=target_id,
                    protocol=Protocol.ICMP,
                    started_at=started_at,
                    duration_ms=1000.0,
                    outcome=Outcome.TIMEOUT,
                    timeout_ms=1000,
                )
            )
        else:
            rows.append(
                ProbeResult(
                    target_id=target_id,
                    protocol=Protocol.ICMP,
                    started_at=started_at,
                    duration_ms=float(10 + index),
                    outcome=Outcome.OK,
                    timeout_ms=1000,
                    rtt_ms=float(10 + index),
                )
            )
    quality_db.insert_probe_results(db_path, rows)


def test_report_model_numbers_equal_the_stats_endpoint(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    start = utc_now().replace(microsecond=0) - timedelta(minutes=30)
    end = start + timedelta(minutes=5)
    _seed(db_path, target.id, start)

    api = client.get(
        "/api/quality/stats", params={"from": to_iso_z(start), "to": to_iso_z(end)}
    ).json()
    model = report.build_report_model(
        db_path, parse_dt(to_iso_z(start)), parse_dt(to_iso_z(end)), app_version="1.2.3", now=utc_now()
    )

    api_entry = next(t for t in api["targets"] if t["target"]["id"] == target.id)
    model_entry = next(t for t in model["targets"] if t["target"]["id"] == target.id)

    assert model_entry["stats"] == api_entry["stats"]
    assert model_entry["note"] == api_entry["note"]
    assert model["coverage"]["coverage_pct"] == api["coverage"]["coverage_pct"]
    assert model["tz"] == api["tz"]
    assert model_entry["data_source"] == "raw"


def test_rendered_report_is_self_contained_and_complete(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    start = utc_now().replace(microsecond=0) - timedelta(minutes=30)
    end = start + timedelta(minutes=5)
    _seed(db_path, target.id, start)
    quality_db.insert_annotation(db_path, to_iso_z(start + timedelta(minutes=1)), "zacięcie TV")

    response = client.get(
        "/api/quality/report.html", params={"from": to_iso_z(start), "to": to_iso_z(end)}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text

    for heading in (
        "Raport jakości łącza",
        "Pokrycie danych",
        "Dostępność",
        "Statystyki sond",
        "Incydenty",
        "Testy obciążeniowe",
        "Wykresy",
        "Ograniczenia pomiaru",
    ):
        assert heading in html

    assert "zgłoszenie użytkownika" in html
    assert "Dotychczasowe pomiary TCP (bez procentu strat)" in html
    assert report.LIMITATIONS[0] in html
    assert client.get("/api/version").json()["version"] in html
    assert client.get("/api/quality/status").json()["tz"] in html

    # self-contained: no external stylesheet, script or image anywhere
    assert "<script" not in html
    assert "http://" not in html
    assert 'src="https://' not in html
    assert "<link" not in html
    assert "url(http" not in html


def test_rendered_report_escapes_an_annotation_label(client) -> None:
    """finding 12: a hostile annotation label must never reach the page raw."""
    db_path = client.app_db_path
    target = _target(db_path)
    start = utc_now().replace(microsecond=0) - timedelta(minutes=30)
    end = start + timedelta(minutes=5)
    _seed(db_path, target.id, start)
    quality_db.insert_annotation(
        db_path, to_iso_z(start + timedelta(minutes=1)), "<script>alert(1)</script>"
    )

    html = client.get(
        "/api/quality/report.html", params={"from": to_iso_z(start), "to": to_iso_z(end)}
    ).text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_report_says_brak_danych_for_an_empty_range(client) -> None:
    db_path = client.app_db_path
    _target(db_path)
    start = utc_now() - timedelta(days=3)
    end = start + timedelta(minutes=5)

    html = client.get(
        "/api/quality/report.html", params={"from": to_iso_z(start), "to": to_iso_z(end)}
    ).text
    assert "brak danych" in html


def test_report_notes_pruned_raw_data(client) -> None:
    db_path = client.app_db_path
    client.put("/api/config", json={"retention_raw_days": 1})
    now = utc_now()
    model = report.build_report_model(
        db_path, now - timedelta(days=10), now, app_version="1.0.0", now=now
    )
    assert model["retention_note"] is not None
    assert "retencja" in model["retention_note"]


def test_report_falls_back_to_aggregates_when_raw_rows_are_gone(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path, name="agg-only")
    now = utc_now()
    bucket_start = (now - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
    quality_db.upsert_aggregate(
        db_path,
        {
            "target_id": target.id,
            "protocol": "icmp",
            "bucket": "1h",
            "bucket_start": to_iso_z(bucket_start),
            "attempts": 3600,
            "ok_count": 3500,
            "timeout_count": 100,
            "error_count": 0,
            "loss_pct": 100 * 100 / 3600,
            "rtt_p95_ms": 30.0,
            "percentiles_from_raw": 0,
            "computed_at": to_iso_z(now),
        },
    )

    model = report.build_report_model(
        db_path,
        bucket_start - timedelta(hours=1),
        bucket_start + timedelta(hours=2),
        app_version="1.0.0",
        now=now,
    )
    entry = next(t for t in model["targets"] if t["target"]["id"] == target.id)
    assert entry["data_source"] == "aggregates"
    assert entry["stats"]["attempts"] == 3600
    assert entry["stats"]["ok"] == 3500


def test_report_matches_the_stats_endpoint_for_a_pruned_range(client) -> None:
    """finding 2: the aggregate fallback must agree between the API and the report."""
    db_path = client.app_db_path
    target = _target(db_path, name="agg-parity")
    now = utc_now()
    bucket_start = (now - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
    quality_db.upsert_aggregate(
        db_path,
        {
            "target_id": target.id,
            "protocol": "icmp",
            "bucket": "1h",
            "bucket_start": to_iso_z(bucket_start),
            "attempts": 3600,
            "ok_count": 3500,
            "timeout_count": 100,
            "error_count": 0,
            "loss_pct": 100 * 100 / 3600,
            "rtt_p95_ms": 30.0,
            "percentiles_from_raw": 0,
            "computed_at": to_iso_z(now),
        },
    )
    start = bucket_start - timedelta(hours=1)
    end = bucket_start + timedelta(hours=2)

    api = client.get(
        "/api/quality/stats", params={"from": to_iso_z(start), "to": to_iso_z(end)}
    ).json()
    model = report.build_report_model(
        db_path, start, end, app_version="1.0.0", now=now
    )

    api_entry = next(t for t in api["targets"] if t["target"]["id"] == target.id)
    model_entry = next(t for t in model["targets"] if t["target"]["id"] == target.id)

    assert model_entry["data_source"] == api_entry["data_source"] == "aggregates"
    assert model_entry["stats"] == api_entry["stats"]
    assert model_entry["covered_from"] == api_entry["covered_from"]
    assert model_entry["covered_to"] == api_entry["covered_to"]


@pytest.mark.parametrize("y_max", [None, 100.0])
def test_svg_line_chart_breaks_the_line_on_missing_points(y_max) -> None:
    svg = report.svg_line_chart(
        [
            {
                "label": "cloudflare-dns",
                "points": [("10:00", 1.0), ("10:01", None), ("10:02", 3.0), ("10:03", 4.0)],
            }
        ],
        width=600,
        height=200,
        y_label="strata %",
        y_max=y_max,
    )

    assert "viewBox=\"0 0 600 200\"" in svg
    assert svg.count("<polyline") == 2  # the None splits the series in two
    assert "strata %" in svg
    assert "cloudflare-dns" in svg


def test_svg_line_chart_escapes_labels_and_survives_empty_series() -> None:
    svg = report.svg_line_chart(
        [{"label": "<script>x</script>", "points": []}],
        width=400,
        height=120,
        y_label="p95 [ms]",
    )
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    assert "<polyline" not in svg


def test_report_model_shares_the_incident_configuration(client) -> None:
    db_path = client.app_db_path
    target = _target(db_path)
    now = utc_now()
    quality_db.insert_incident(
        db_path,
        target_id=target.id,
        protocol="icmp",
        kind="outage",
        started_at=to_iso_z(now - timedelta(minutes=10)),
        ended_at=to_iso_z(now - timedelta(minutes=8)),
        closed_at=to_iso_z(now - timedelta(minutes=7)),
        close_reason="recovered",
        window_seconds=10,
        probe_interval_seconds=1.0,
        peak_loss_pct=100.0,
        windows_degraded=12,
        summary_json=json.dumps([[to_iso_z(now - timedelta(minutes=10)), "outage", 100.0, None]]),
    )

    model = report.build_report_model(
        db_path, now - timedelta(hours=1), now, app_version="1.0.0", now=now
    )
    assert model["config"]["incident_thresholds"]["incident_loss_pct_threshold"] == 20.0
    assert model["config"]["targets"]
    assert model["incidents"][0]["kind"] == "outage"
    assert model["incidents"][0]["windows"][0]["verdict"] == "outage"


# ---------------------------------------------------------------------------
# one read of the raw range per report (review finding C1b)
# ---------------------------------------------------------------------------

def test_report_reads_the_raw_range_once_per_target(client, monkeypatch) -> None:
    """The table and the charts share one fetch instead of querying twice."""
    db_path = client.app_db_path
    target = _target(db_path, name="single-fetch")
    now = utc_now().replace(microsecond=0)
    start = now - timedelta(minutes=10)
    _seed(db_path, target.id, start)

    calls: list[dict] = []
    original = quality_db.query_probe_metrics

    def _counting(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    # the report reads raw rows through the narrow projection; the wide
    # accessor is only for `probes.csv`, which reproduces the stored row
    monkeypatch.setattr(quality_db, "query_probe_metrics", _counting)
    model = report.build_report_model(db_path, start, now, app_version="1.0.0", now=now)

    targets = quality_db.list_targets(db_path)
    # one fetch per target for the range, and nothing else (`_latency_under_load`
    # only runs for a load test, and there is none here)
    assert len(calls) == len(targets)
    entry = next(t for t in model["targets"] if t["target"]["id"] == target.id)
    assert entry["data_source"] == "raw"
    assert "polyline" in model["charts"]["loss"]


def test_report_charts_fall_back_to_aggregates_when_raw_rows_are_gone(client) -> None:
    """A populated table under an empty chart would contradict itself."""
    db_path = client.app_db_path
    target = _target(db_path, name="agg-chart")
    now = utc_now()
    bucket_start = (now - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
    quality_db.upsert_aggregate(
        db_path,
        {
            "target_id": target.id,
            "protocol": "icmp",
            "bucket": "1h",
            "bucket_start": to_iso_z(bucket_start),
            "attempts": 3600,
            "ok_count": 3000,
            "timeout_count": 600,
            "error_count": 0,
            "loss_pct": 600 * 100 / 3600,
            "rtt_p95_ms": 30.0,
            "percentiles_from_raw": 0,
            "computed_at": to_iso_z(now),
        },
    )

    model = report.build_report_model(
        db_path,
        bucket_start - timedelta(hours=1),
        bucket_start + timedelta(hours=2),
        app_version="1.0.0",
        now=now,
    )
    entry = next(t for t in model["targets"] if t["target"]["id"] == target.id)
    assert entry["data_source"] == "aggregates"
    # the loss chart carries the pooled bucket; p95 stays unknown (spec §6)
    assert "brak danych" not in model["charts"]["loss"]
    assert "brak danych" in model["charts"]["p95"]


def test_report_of_a_range_wider_than_the_raw_limit_uses_aggregates(client, monkeypatch) -> None:
    """finding C1b: the report is bound by the same raw-range cap as the API."""
    db_path = client.app_db_path
    target = _target(db_path, name="wide-report")
    now = utc_now().replace(microsecond=0)
    _seed(db_path, target.id, now - timedelta(minutes=10))

    calls: list[tuple] = []
    original = quality_db.query_probe_results

    def _counting(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(quality_db, "query_probe_results", _counting)
    model = report.build_report_model(
        db_path, now - timedelta(days=60), now, app_version="1.0.0", now=now
    )

    assert calls == []  # nothing read a raw row for a 60 day range
    entry = next(t for t in model["targets"] if t["target"]["id"] == target.id)
    assert entry["data_source"] in {"aggregates", "none"}


# ---------------------------------------------------------------------------
# lost measurements are stated, never silently covered (review finding I5)
# ---------------------------------------------------------------------------

def test_report_states_dropped_rows_of_the_running_session(client) -> None:
    db_path = client.app_db_path
    now = utc_now()
    model = report.build_report_model(
        db_path,
        now - timedelta(minutes=10),
        now,
        app_version="1.0.0",
        now=now,
        scheduler={"dropped_rows": 12, "flush_errors": 3},
    )
    assert "12" in model["dropped_rows_note"]
    assert "3" in model["dropped_rows_note"]
    assert "12" in report.render_report(model)


def test_report_without_an_engine_says_nothing_about_dropped_rows(client) -> None:
    db_path = client.app_db_path
    now = utc_now()
    healthy = report.build_report_model(
        db_path,
        now - timedelta(minutes=10),
        now,
        app_version="1.0.0",
        now=now,
        scheduler={"dropped_rows": 0, "flush_errors": 0},
    )
    without = report.build_report_model(
        db_path, now - timedelta(minutes=10), now, app_version="1.0.0", now=now
    )
    assert healthy["dropped_rows_note"] is None
    assert without["dropped_rows_note"] is None


def test_report_separates_expected_downtime(client) -> None:
    """The printable report never hides a planned outage — it labels it.

    The report is evidence handed to an ISP, so an expected outage stays in
    both tables and is called out separately instead of being filtered away.
    """
    db_path = client.app_db_path
    target = _target(db_path, name="expected-report")
    session_id = quality_db.start_session(
        db_path, device_id="test", app_version="test", now_iso="2026-09-21T00:00:00.000Z"
    )
    quality_db.end_session(db_path, session_id, "2026-09-21T05:00:00.000Z", "shutdown")
    db.record_connectivity(db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    db.record_connectivity(db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    db.record_connectivity(db_path, is_up=False, now_iso="2026-09-21T03:00:00.000Z")
    db.record_connectivity(db_path, is_up=True, now_iso="2026-09-21T03:04:00.000Z")
    db.mark_connectivity_outage_expected(
        db_path,
        started_at_iso="2026-09-21T03:00:00.000Z",
        ended_at_iso="2026-09-21T03:00:00.000Z",
        expected=True,
        source="rule",
        rule_id=None,
    )
    quality_db.insert_incident(
        db_path,
        target_id=target.id,
        protocol="icmp",
        kind="outage",
        started_at="2026-09-21T03:00:00.000Z",
        ended_at="2026-09-21T03:04:00.000Z",
        closed_at="2026-09-21T03:05:00.000Z",
        close_reason="recovered",
        window_seconds=10,
        probe_interval_seconds=1.0,
        expected=1,
        expected_source="rule",
    )

    start, end = parse_dt("2026-09-21T00:00:00.000Z"), parse_dt("2026-09-21T05:00:00.000Z")
    model = report.build_report_model(db_path, start, end, app_version="1.0.0", now=end)

    assert model["availability"]["downtime_seconds"] == 540
    assert model["availability"]["expected_downtime_seconds"] == 240
    assert model["availability"]["expected_incident_count"] == 1
    assert [incident["expected"] for incident in model["incidents"]] == [1]

    html = client.get(
        "/api/quality/report.html",
        params={"from": "2026-09-21T00:00:00.000Z", "to": "2026-09-21T05:00:00.000Z"},
    ).text
    assert "spodziewane" in html


# ---------------------------------------------------------------------------
# a hand-written verdict is printed as it was written (review finding: the
# report branched on `expected_source` alone, so a row a person had cleared
# printed as "tak (ręcznie)" — the opposite of what the person recorded)
# ---------------------------------------------------------------------------

#: Spelled in UTC: `parse_dt` reads a naked `2026-09-21T00:00` as *local*, so a
#: wall-clock range would slide off the fixtures on any machine but UTC+0.
VERDICT_RANGE = {"from": "2026-09-21T00:00:00.000Z", "to": "2026-09-21T05:00:00.000Z"}


def _expected_column(html: str, heading: str) -> list[str]:
    """The last cell of every data row of the first table under `heading`.

    The flag column is the last one in both tables, and reading it out of the
    rendered page is the point: the model has been right all along — it is the
    template that spoke for it.
    """
    section = html[html.index(heading) :]
    table = section[section.index("<table>") : section.index("</table>")]
    rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)[1:]
    return [re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)[-1].strip() for row in rows]


def test_report_availability_table_prints_the_verdict_a_person_wrote(client) -> None:
    """Four outages, four different verdicts, four different labels (§6).

    "Nobody has looked" and "a person looked and this was real" are different
    facts — that difference is the whole reason `expected_source` is stored
    for an `expected = 0` row, and the report is where it has to show.
    """
    db_path = client.app_db_path
    for hour in (1, 2, 3, 4):
        db.record_connectivity(db_path, is_up=False, now_iso=f"2026-09-21T0{hour}:00:00.000Z")
        db.record_connectivity(db_path, is_up=True, now_iso=f"2026-09-21T0{hour}:05:00.000Z")
    db.mark_connectivity_outage_expected(
        db_path,
        started_at_iso="2026-09-21T01:00:00.000Z",
        ended_at_iso="2026-09-21T01:00:00.000Z",
        expected=True,
        source="rule",
        rule_id=None,
    )
    ids = [item["id"] for item in client.get("/api/outages", params=VERDICT_RANGE).json()["items"]]
    assert client.patch(f"/api/outages/{ids[1]}/expected", json={"expected": True}).status_code == 200
    assert client.patch(f"/api/outages/{ids[2]}/expected", json={"expected": False}).status_code == 200

    html = client.get("/api/quality/report.html", params=VERDICT_RANGE).text

    assert _expected_column(html, "<h2>Dostępność</h2>") == [
        "tak",
        "tak (ręcznie)",
        "nie (ręcznie)",
        "—",
    ]


def test_report_incident_table_prints_the_verdict_a_person_wrote(client) -> None:
    """The same ladder in the incidents table, which had the same bug."""
    db_path = client.app_db_path
    target = _target(db_path, name="verdict-report")
    for hour, fields in (
        (1, {"expected": 1, "expected_source": "rule"}),
        (2, {}),
        (3, {}),
        (4, {}),
    ):
        quality_db.insert_incident(
            db_path,
            target_id=target.id,
            protocol="icmp",
            kind="outage",
            started_at=f"2026-09-21T0{hour}:00:00.000Z",
            ended_at=f"2026-09-21T0{hour}:04:00.000Z",
            closed_at=f"2026-09-21T0{hour}:05:00.000Z",
            close_reason="recovered",
            window_seconds=10,
            probe_interval_seconds=1.0,
            **fields,
        )
    ids = [
        incident["id"]
        for incident in client.get("/api/quality/incidents", params=VERDICT_RANGE).json()["items"]
    ]
    assert (
        client.patch(
            f"/api/quality/incidents/{ids[1]}/expected", json={"expected": True}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/quality/incidents/{ids[2]}/expected", json={"expected": False}
        ).status_code
        == 200
    )

    html = client.get("/api/quality/report.html", params=VERDICT_RANGE).text

    assert _expected_column(html, "<h2>Incydenty</h2>") == [
        "tak",
        "tak (ręcznie)",
        "nie (ręcznie)",
        "—",
    ]
