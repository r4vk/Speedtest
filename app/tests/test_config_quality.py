"""The quality settings served by `/api/config` (spec §7, §8, §10, §11, §14).

The settings table is the contract between T6 (engine), T7 (load tests and
diagnostics), T9 (retention) and this API, so the defaults are asserted here
and nowhere else.
"""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from speedtest_app import quality_db
from speedtest_app.db import ensure_db, get_settings, set_setting
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
    assert payload["diagnostics_enabled"] is True
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
    assert stored["retention_load_test_raw_days"] == "90"
    assert stored["retention_diagnostics_days"] == "365"
    assert stored["diagnostics_max_concurrent"] == "1"
    assert stored["diagnostics_mtr_count"] == "10"
    assert stored["diagnostics_mtr_timeout_seconds"] == "90"
    assert stored["load_test_kind"] == "iperf_udp"


def test_every_spec_key_is_reachable_through_the_config_api(client) -> None:
    """finding I1: a key the engine reads must be readable and writable here.

    Six keys were seeded, read and clamped but exposed by neither model, so
    the operator could not reach the knobs (`retention_diagnostics_days`,
    the mtr limits) they need when the database grows. These two assertions
    turn "we remembered to add the field" into a permanent guarantee.
    """
    main = importlib.import_module("speedtest_app.main")
    assert set(QUALITY_SETTING_SPECS) <= set(main.ConfigResponse.model_fields)
    assert set(QUALITY_SETTING_SPECS) <= set(main.ConfigUpdate.model_fields)


def test_the_six_late_keys_are_served_and_writable(client) -> None:
    payload = client.get("/api/config").json()
    assert payload["load_test_kind"] == "iperf_udp"
    assert payload["diagnostics_max_concurrent"] == 1
    assert payload["diagnostics_mtr_count"] == 10
    assert payload["diagnostics_mtr_timeout_seconds"] == 90
    assert payload["retention_load_test_raw_days"] == 90
    assert payload["retention_diagnostics_days"] == 365

    written = client.put(
        "/api/config",
        json={
            "load_test_kind": "iperf_tcp",
            "diagnostics_max_concurrent": 2,
            "diagnostics_mtr_count": 20,
            "diagnostics_mtr_timeout_seconds": 120,
            "retention_load_test_raw_days": 30,
            "retention_diagnostics_days": 60,
        },
    )
    assert written.status_code == 200
    assert written.json()["load_test_kind"] == "iperf_tcp"

    stored = get_settings(
        client.app_db_path,
        [
            "load_test_kind",
            "diagnostics_max_concurrent",
            "diagnostics_mtr_count",
            "diagnostics_mtr_timeout_seconds",
            "retention_load_test_raw_days",
            "retention_diagnostics_days",
        ],
    )
    assert stored == {
        "load_test_kind": "iperf_tcp",
        "diagnostics_max_concurrent": "2",
        "diagnostics_mtr_count": "20",
        "diagnostics_mtr_timeout_seconds": "120",
        "retention_load_test_raw_days": "30",
        "retention_diagnostics_days": "60",
    }


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
        {"incident_window_seconds": 301},  # bound is `le=300`, not far beyond it
        {"incident_loss_pct_threshold": 101},
        {"incident_outage_loss_pct": 101},
        {"incident_min_samples": 0},
        {"incident_rtt_p95_ms_threshold": 10001},
        {"incident_rtt_p95_ms_threshold": 0},
        {"incident_fail_streak_threshold": 1001},
        {"incident_fail_streak_threshold": 0},
        {"incident_open_windows": 101},
        {"incident_open_windows": 0},
        {"incident_stabilization_seconds": 86401},
        {"incident_stabilization_seconds": -1},
        {"incident_no_data_close_seconds": 86401},
        {"incident_no_data_close_seconds": -1},
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
        {"load_test_kind": "iperf_smoke"},
        {"diagnostics_max_concurrent": 5},
        {"diagnostics_max_concurrent": 0},
        {"diagnostics_mtr_count": 101},
        {"diagnostics_mtr_timeout_seconds": 9},
        {"diagnostics_mtr_timeout_seconds": 601},
        {"retention_load_test_raw_days": 0},
        {"retention_diagnostics_days": 4000},
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


def test_invalid_gateway_host_leaves_other_fields_of_the_same_request_untouched(client) -> None:
    """finding 7: `gateway_host` is validated before anything is written, so a

    422 on it must not leave a partial update behind (it used to be validated
    last, being the last key of QUALITY_SETTING_SPECS).
    """
    response = client.put(
        "/api/config",
        json={"incident_window_seconds": 30, "gateway_host": "nie jest hostem"},
    )
    assert response.status_code == 422

    payload = client.get("/api/config").json()
    assert payload["incident_window_seconds"] == 10
    assert payload["gateway_host"] == ""
    assert _gateway(client.app_db_path).enabled is False


# ---------------------------------------------------------------------------
# GATEWAY_HOST reaches the gateway target on every start (review finding I3)
# ---------------------------------------------------------------------------

def _start_app(tmp_path, monkeypatch, gateway_host: str | None):
    """A client over a database seeded *without* the env, then started with it.

    That is the upgrade path the finding describes: the targets already
    exist, so `_seed_probe_targets` returns early and only the startup
    reconciliation can still apply the variable.
    """
    data_dir = tmp_path / "gw-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "app.db")

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("GATEWAY_HOST", raising=False)
    monkeypatch.setenv("TELEMETRY_DEFAULT_ENABLED", "false")
    ensure_db(db_path)  # seeds the gateway target with an empty host
    set_setting(db_path, "ping_enabled", "false", source="env")
    set_setting(db_path, "speed_enabled", "false", source="env")
    assert _gateway(db_path).host == ""

    if gateway_host is not None:
        monkeypatch.setenv("GATEWAY_HOST", gateway_host)
    config_module = importlib.import_module("speedtest_app.config")
    db_module = importlib.import_module("speedtest_app.db")
    importlib.reload(config_module)
    main_module = importlib.import_module("speedtest_app.main")
    importlib.reload(main_module)
    return db_path, main_module, config_module, db_module


def test_env_gateway_host_is_applied_on_a_later_start(tmp_path, monkeypatch) -> None:
    db_path, main_module, config_module, db_module = _start_app(
        tmp_path, monkeypatch, "192.168.7.1"
    )
    try:
        with TestClient(main_module.app):
            gateway = _gateway(db_path)
            assert (gateway.host, gateway.enabled) == ("192.168.7.1", True)
            assert get_settings(db_path, ["gateway_host"])["gateway_host"] == "192.168.7.1"
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)
        importlib.reload(db_module)


def test_env_never_overwrites_a_host_configured_in_the_ui(tmp_path, monkeypatch) -> None:
    db_path, main_module, config_module, db_module = _start_app(
        tmp_path, monkeypatch, "10.0.0.1"
    )
    try:
        with TestClient(main_module.app) as client:
            assert client.put("/api/config", json={"gateway_host": "192.168.50.1"}).status_code == 200
        # a restart with the env still set must keep the operator's host
        with TestClient(main_module.app):
            gateway = _gateway(db_path)
            assert (gateway.host, gateway.enabled) == ("192.168.50.1", True)
            assert get_settings(db_path, ["gateway_host"])["gateway_host"] == "192.168.50.1"
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)
        importlib.reload(db_module)


def test_an_unusable_env_gateway_host_leaves_the_target_disabled(tmp_path, monkeypatch) -> None:
    db_path, main_module, config_module, db_module = _start_app(
        tmp_path, monkeypatch, "-nie jest hostem"
    )
    try:
        with TestClient(main_module.app):
            gateway = _gateway(db_path)
            assert (gateway.host, gateway.enabled) == ("", False)
            assert get_settings(db_path, ["gateway_host"])["gateway_host"] == ""
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)
        importlib.reload(db_module)
