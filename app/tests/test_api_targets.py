"""Probe targets CRUD and its validation (design spec §12).

The validation is the interesting part: an interval below one second or a
timeout longer than the interval would quietly change what the measurement
means, so both are refused instead of being clamped.
"""
from __future__ import annotations

from speedtest_app import quality_db
from speedtest_app.db import set_setting
from speedtest_app.probe_types import Outcome, ProbeResult, Protocol
from speedtest_app.time_utils import to_iso_z, utc_now

BODY = {
    "name": "extra-icmp",
    "kind": "internet",
    "protocol": "icmp",
    "host": "9.9.9.9",
    "interval_seconds": 1.0,
    "timeout_ms": 1000,
}


def test_list_targets_carries_the_seeded_ones(client) -> None:
    payload = client.get("/api/targets").json()

    assert payload["tz"]
    names = {item["name"] for item in payload["items"]}
    assert {"cloudflare-dns", "google-dns", "quad9-dns", "gateway"} <= names
    assert all(item["last_result"] is None for item in payload["items"])


def test_list_targets_shows_the_last_result(client) -> None:
    db_path = client.app_db_path
    target = next(t for t in quality_db.list_targets(db_path) if t.name == "cloudflare-dns")
    quality_db.insert_probe_results(
        db_path,
        [
            ProbeResult(
                target_id=target.id,
                protocol=Protocol.ICMP,
                started_at=to_iso_z(utc_now()),
                duration_ms=12.0,
                outcome=Outcome.OK,
                timeout_ms=1000,
                rtt_ms=12.0,
            )
        ],
    )

    item = next(i for i in client.get("/api/targets").json()["items"] if i["id"] == target.id)
    assert item["last_result"]["outcome"] == "ok"
    assert item["last_result"]["rtt_ms"] == 12.0
    # local time, not the stored UTC ISO-Z
    assert not item["last_result"]["started_at"].endswith("Z")


def test_create_update_and_delete_a_target(client) -> None:
    created = client.post("/api/targets", json=BODY)
    assert created.status_code == 200
    target = created.json()["target"]
    assert target["name"] == "extra-icmp"
    assert target["enabled"] is True

    updated = client.put(f"/api/targets/{target['id']}", json={"enabled": False, "timeout_ms": 800})
    assert updated.status_code == 200
    assert updated.json()["target"]["enabled"] is False
    assert updated.json()["target"]["timeout_ms"] == 800
    assert updated.json()["target"]["host"] == "9.9.9.9"

    assert client.delete(f"/api/targets/{target['id']}").json()["deleted"] is True
    assert client.delete(f"/api/targets/{target['id']}").status_code == 404
    assert client.put(f"/api/targets/{target['id']}", json={"enabled": True}).status_code == 404


def test_duplicate_name_is_a_conflict(client) -> None:
    assert client.post("/api/targets", json=BODY).status_code == 200
    duplicate = client.post("/api/targets", json=BODY)
    assert duplicate.status_code == 409
    assert "extra-icmp" in duplicate.json()["detail"]


def test_renaming_onto_another_target_is_a_conflict(client) -> None:
    created = client.post("/api/targets", json=BODY).json()["target"]
    response = client.put(f"/api/targets/{created['id']}", json={"name": "cloudflare-dns"})
    assert response.status_code == 409
    # renaming a target to its own name stays allowed
    assert client.put(f"/api/targets/{created['id']}", json={"name": "extra-icmp"}).status_code == 200


def test_sub_second_intervals_need_diagnostic_mode(client) -> None:
    fast = {**BODY, "interval_seconds": 0.5, "timeout_ms": 400}
    assert client.post("/api/targets", json=fast).status_code == 422

    set_setting(client.app_db_path, "diagnostic_mode", "true")
    assert client.post("/api/targets", json=fast).status_code == 200

    too_fast = {**BODY, "name": "way-too-fast", "interval_seconds": 0.1, "timeout_ms": 100}
    assert client.post("/api/targets", json=too_fast).status_code == 422


def test_timeout_may_not_exceed_the_interval(client) -> None:
    response = client.post("/api/targets", json={**BODY, "timeout_ms": 5000})
    assert response.status_code == 422
    assert "timeout_ms" in response.json()["detail"]

    # the 60 s ceiling holds even for a long interval
    long_interval = {**BODY, "name": "slow", "interval_seconds": 300.0, "timeout_ms": 60001}
    assert client.post("/api/targets", json=long_interval).status_code == 422


def test_host_is_validated_per_protocol(client) -> None:
    assert client.post("/api/targets", json={**BODY, "host": "nie a host"}).status_code == 422

    https_body = {
        "name": "https-extra",
        "kind": "https",
        "protocol": "https",
        "host": "cloudflare.com",
        "interval_seconds": 60.0,
        "timeout_ms": 5000,
    }
    assert client.post("/api/targets", json=https_body).status_code == 422
    ok = client.post(
        "/api/targets", json={**https_body, "host": "https://cloudflare.com/cdn-cgi/trace"}
    )
    assert ok.status_code == 200
    assert ok.json()["target"]["host"] == "https://cloudflare.com/cdn-cgi/trace"


def test_unknown_kind_or_protocol_is_rejected(client) -> None:
    assert client.post("/api/targets", json={**BODY, "kind": "satellite"}).status_code == 422
    assert client.post("/api/targets", json={**BODY, "protocol": "quic"}).status_code == 422
    assert client.post("/api/targets", json={**BODY, "family_pref": "ipv7"}).status_code == 422
