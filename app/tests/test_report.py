"""The printable ISP report (design spec §13).

The report is only trustworthy if its numbers are the API's numbers, so the
first test compares the model with `/api/quality/stats` field by field.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from speedtest_app import quality_db, report
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
