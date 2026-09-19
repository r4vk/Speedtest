"""The quality settings served by `/api/config` (spec §7, §8, §10, §11, §14).

The settings table is the contract between T6 (engine), T7 (load tests and
diagnostics), T9 (retention) and this API, so the defaults are asserted here
and nowhere else.
"""
from __future__ import annotations

from speedtest_app import quality_db
from speedtest_app.db import get_settings
from speedtest_app.quality_settings import QUALITY_SETTING_SPECS


def _gateway(db_path: str):
    return next(t for t in quality_db.list_targets(db_path) if t.name == "gateway")


def test_defaults_are_seeded_and_served(client) -> None:
    payload = client.get("/api/config").json()

    assert payload["incident_window_seconds"] == 10
    assert payload["incident_min_samples"] == 5
    assert payload["incident_loss_pct_threshold"] == 20.0
    assert payload["incident_outage_loss_pct"] == 100.0
    assert payload["incident_rtt_p95_ms_threshold"] == 150.0
    assert payload["incident_fail_streak_threshold"] == 3
    assert payload["incident_open_windows"] == 2
    assert payload["incident_stabilization_seconds"] == 60
    assert payload["incident_no_data_close_seconds"] == 300
    assert payload["availability_eval_seconds"] == 5
    assert payload["availability_window_seconds"] == 10
    assert payload["load_test_enabled"] is False
    assert payload["load_test_interval_seconds"] == 21600
    assert payload["load_test_port"] == 5201
    assert payload["load_test_udp_bitrate"] == "10M"
    assert payload["load_test_duration_seconds"] == 10
    assert payload["load_test_datagram_len"] == 1200
    assert payload["load_test_directions"] == "both"
    assert payload["diagnostics_min_interval_seconds"] == 300
    assert payload["diagnostics_max_per_incident"] == 3
    assert payload["retention_raw_days"] == 14
    assert payload["retention_aggregate_days"] == 365
    assert payload["retention_incident_days"] == 730
    assert payload["diagnostic_mode"] is False
    assert payload["gateway_host"] == ""
    # the legacy fields are untouched
    assert payload["connect_target"]


def test_every_spec_key_is_stored_in_the_database(client) -> None:
    stored = get_settings(client.app_db_path, list(QUALITY_SETTING_SPECS))
    assert set(stored) == set(QUALITY_SETTING_SPECS)
    # keys the update model does not expose still have to exist for T7/T9
    assert stored["retention_load_test_raw_days"] == "90"
    assert stored["retention_diagnostics_days"] == "365"
    assert stored["diagnostics_max_concurrent"] == "1"
    assert stored["diagnostics_mtr_count"] == "10"
    assert stored["diagnostics_mtr_timeout_seconds"] == "90"
    assert stored["load_test_kind"] == "iperf_udp"


def test_update_persists_the_quality_fields(client) -> None:
    response = client.put(
        "/api/config",
        json={
            "incident_window_seconds": 30,
            "incident_loss_pct_threshold": 5.5,
            "load_test_enabled": True,
            "load_test_server": "iperf.example",
            "load_test_udp_bitrate": "25.5M",
            "load_test_directions": "download",
            "diagnostics_enabled": False,
            "retention_raw_days": 7,
            "diagnostic_mode": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["incident_window_seconds"] == 30
    assert payload["incident_loss_pct_threshold"] == 5.5
    assert payload["load_test_enabled"] is True
    assert payload["load_test_server"] == "iperf.example"
    assert payload["load_test_udp_bitrate"] == "25.5M"
    assert payload["load_test_directions"] == "download"
    assert payload["diagnostics_enabled"] is False
    assert payload["retention_raw_days"] == 7
    assert payload["diagnostic_mode"] is True

    stored = get_settings(client.app_db_path, ["incident_window_seconds", "load_test_enabled"])
    assert stored == {"incident_window_seconds": "30", "load_test_enabled": "true"}
    assert client.get("/api/config").json()["incident_window_seconds"] == 30


def test_out_of_range_values_are_refused(client) -> None:
    cases = [
        {"incident_window_seconds": 1},
        {"incident_window_seconds": 3601},
        {"incident_loss_pct_threshold": 101},
        {"incident_min_samples": 0},
        {"availability_eval_seconds": 0},
        {"availability_window_seconds": 4},
        {"load_test_interval_seconds": 10},
        {"load_test_port": 70000},
        {"load_test_udp_bitrate": "bardzo szybko"},
        {"load_test_duration_seconds": 61},
        {"load_test_datagram_len": 10},
        {"load_test_directions": "wszędzie"},
        {"diagnostics_min_interval_seconds": 5},
        {"diagnostics_max_per_incident": 21},
        {"retention_raw_days": 0},
        {"retention_aggregate_days": 4000},
        {"retention_incident_days": 0},
    ]
    for case in cases:
        assert client.put("/api/config", json=case).status_code == 422, case
    # nothing was written
    assert client.get("/api/config").json()["incident_window_seconds"] == 10


def test_gateway_host_toggles_the_gateway_target(client) -> None:
    db_path = client.app_db_path
    assert _gateway(db_path).enabled is False

    response = client.put("/api/config", json={"gateway_host": "192.168.1.1"})
    assert response.status_code == 200
    assert response.json()["gateway_host"] == "192.168.1.1"
    gateway = _gateway(db_path)
    assert (gateway.host, gateway.enabled) == ("192.168.1.1", True)

    cleared = client.put("/api/config", json={"gateway_host": ""})
    assert cleared.json()["gateway_host"] == ""
    gateway = _gateway(db_path)
    assert (gateway.host, gateway.enabled) == ("", False)


def test_gateway_host_is_validated(client) -> None:
    response = client.put("/api/config", json={"gateway_host": "nie jest hostem"})
    assert response.status_code == 422
    assert _gateway(client.app_db_path).enabled is False
