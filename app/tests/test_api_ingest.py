"""Device registration and result upload (spec §15, stage 9)."""
from __future__ import annotations

from speedtest_app import ingest, quality_db
from speedtest_app.time_utils import to_iso_z, utc_now


def _register(client, name="MacBook Rafała"):
    resp = client.post("/api/devices", json={"name": name})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["device"]["id"], {"Authorization": f"Bearer {body['token']}"}


def _result(ext, target, host, outcome="ok", rtt=4.2, offset_s=0):
    started = to_iso_z(utc_now()) if offset_s == 0 else None
    return {
        "external_id": ext,
        "target": target,
        "host": host,
        "protocol": "icmp",
        "started_at": started or "2026-11-06T10:00:00.000Z",
        "duration_ms": 5.0,
        "outcome": outcome,
        "rtt_ms": rtt if outcome == "ok" else None,
        "timeout_ms": 1000,
        "link": "up",
    }


def _batch(device_id, results):
    return {"device_id": device_id, "sent_at": to_iso_z(utc_now()), "results": results}


def test_register_returns_token_once_and_lists_without_hash(client):
    device_id, _ = _register(client)
    assert device_id == "macbook-rafala"
    listed = client.get("/api/devices").json()["items"]
    mac = next(d for d in listed if d["id"] == device_id)
    assert mac["can_ingest"] is True and "token" not in mac and "token_hash" not in mac
    assert next(d for d in listed if d["id"] == "nas")["can_ingest"] is False
    second, _ = _register(client)
    assert second == "macbook-rafala-2"


def test_register_rejects_nas_id(client):
    assert client.post("/api/devices", json={"name": "x", "id": "nas"}).status_code == 400


def test_ingest_matches_targets_and_is_idempotent(client):
    device_id, auth = _register(client)
    results = [
        _result("1:internet", "internet", "1.1.1.1"),
        _result("1:quad", "quad9-dns", None, outcome="timeout"),
        _result("1:nas", "nas", "192.168.10.2"),
        {"external_id": "bad", "target": "internet", "host": "1.1.1.1", "outcome": "ok"},
    ]
    resp = client.post("/api/ingest/results", json=_batch(device_id, results), headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["accepted"], body["duplicates"]) == (2, 0)
    assert sorted((r["external_id"], r["reason"]) for r in body["rejected"]) == [
        ("1:nas", "unknown_target"), ("bad", "invalid"),
    ]
    assert body["skew_ms"] is not None

    again = client.post("/api/ingest/results", json=_batch(device_id, results[:2]), headers=auth)
    assert (again.json()["accepted"], again.json()["duplicates"]) == (0, 2)

    db_path = client.app_db_path
    rows = quality_db.query_probe_results(
        db_path, "2000-01-01T00:00:00.000Z", "2100-01-01T00:00:00.000Z", device_id=device_id
    )
    names = {t.id: t.name for t in quality_db.list_targets(db_path)}
    assert sorted(names[r["target_id"]] for r in rows) == ["cloudflare-dns", "quad9-dns"]
    assert quality_db.get_device(db_path, device_id)["last_seen_at"] is not None


def test_uploaded_rows_do_not_change_nas_stats(client):
    device_id, auth = _register(client)
    before = client.get("/api/quality/stats").json()
    results = [_result(f"{i}:internet", "internet", "1.1.1.1", outcome="timeout") for i in range(5)]
    client.post("/api/ingest/results", json=_batch(device_id, results), headers=auth)
    after = client.get("/api/quality/stats").json()
    assert [t["stats"] for t in after["targets"]] == [t["stats"] for t in before["targets"]]


def test_ingest_rejects_bad_token_and_foreign_device_id(client):
    device_id, auth = _register(client)
    batch = _batch(device_id, [_result("1:internet", "internet", "1.1.1.1")])
    assert client.post("/api/ingest/results", json=batch).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/api/ingest/results", json=batch, headers=bad).status_code == 401
    foreign = {**batch, "device_id": "nas"}
    assert client.post("/api/ingest/results", json=foreign, headers=auth).status_code == 403


def test_deleting_device_revokes_token(client):
    device_id, auth = _register(client)
    assert client.delete(f"/api/devices/{device_id}").status_code == 200
    batch = _batch(device_id, [_result("1:internet", "internet", "1.1.1.1")])
    assert client.post("/api/ingest/results", json=batch, headers=auth).status_code == 401
    assert client.delete("/api/devices/nas").status_code == 400


def test_batch_limit(client, monkeypatch):
    device_id, auth = _register(client)
    monkeypatch.setattr(ingest, "MAX_BATCH", 1)
    batch = _batch(device_id, [_result(f"{i}:internet", "internet", "1.1.1.1") for i in range(2)])
    assert client.post("/api/ingest/results", json=batch, headers=auth).status_code == 413


def test_compare_puts_device_next_to_nas_on_same_buckets(client):
    from datetime import timedelta

    from speedtest_app.probe_types import Outcome, ProbeResult, Protocol

    device_id, auth = _register(client)
    db_path = client.app_db_path
    cf = next(t for t in quality_db.list_targets(db_path) if t.name == "cloudflare-dns")
    t0 = utc_now().replace(second=0, microsecond=0) - timedelta(minutes=5)
    quality_db.insert_probe_results(db_path, [
        ProbeResult(target_id=cf.id, protocol=Protocol.ICMP, started_at=to_iso_z(t0 + timedelta(seconds=i)),
                    duration_ms=1.0, outcome=Outcome.OK, rtt_ms=7.0, timeout_ms=1000)
        for i in range(10)
    ])
    mac = []
    for i in range(10):
        item = _result(f"{i}:internet", "internet", "1.1.1.1", outcome="timeout" if i < 4 else "ok")
        item["started_at"] = to_iso_z(t0 + timedelta(seconds=i))
        mac.append(item)
    client.post("/api/ingest/results", json=_batch(device_id, mac), headers=auth)

    resp = client.get("/api/quality/compare", params={
        "device": device_id, "from": to_iso_z(t0 - timedelta(minutes=1)), "to": to_iso_z(t0 + timedelta(minutes=2)),
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [s["target"]["name"] for s in body["targets"]] == ["cloudflare-dns"]
    side = body["targets"][0]
    assert side["nas"]["stats"]["loss_pct"] == 0
    assert side["device"]["stats"]["loss_pct"] == 40
    assert [p["t"] for p in side["nas"]["points"]] == [p["t"] for p in side["device"]["points"]]

    assert client.get("/api/quality/compare", params={"device": "nas"}).status_code == 404
