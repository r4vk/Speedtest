"""Results of other devices never leak into the NAS's own analytics (stage 9).

Every probe reader goes through `quality_db._probe_results_query`; without an
explicit device it must return NAS rows only, so a Mac on Wi-Fi uploading its
results cannot change the wired statistics, aggregates or the ISP report.
"""
from __future__ import annotations

from speedtest_app import quality_db
from speedtest_app.probe_types import Outcome, ProbeResult, Protocol


def _gateway(db_path):
    return next(t for t in quality_db.list_targets(db_path) if t.name == "gateway")


def _rows(target_id, utc_iso):
    common = dict(target_id=target_id, protocol=Protocol.ICMP, duration_ms=1.0, timeout_ms=1000)
    return [
        ProbeResult(started_at=utc_iso(1), outcome=Outcome.OK, rtt_ms=1.0, **common),
        ProbeResult(
            started_at=utc_iso(2), outcome=Outcome.TIMEOUT, device_id="mac-1",
            external_id="2:gateway", **common,
        ),
    ]


def test_queries_default_to_nas_rows(db_path, utc_iso):
    target = _gateway(db_path)
    quality_db.insert_probe_results(db_path, _rows(target.id, utc_iso))

    default = quality_db.query_probe_metrics(db_path, utc_iso(0), utc_iso(10), target_id=target.id)
    assert [r["outcome"] for r in default] == ["ok"]
    mac = quality_db.query_probe_results(db_path, utc_iso(0), utc_iso(10), device_id="mac-1")
    assert [r["outcome"] for r in mac] == ["timeout"]
    every = quality_db.query_probe_results(
        db_path, utc_iso(0), utc_iso(10), device_id=quality_db.ALL_DEVICES
    )
    assert len(every) == 2
    streamed = list(quality_db.iter_probe_results(db_path, utc_iso(0), utc_iso(10)))
    assert [r["device_id"] for r in streamed] == ["nas"]


def test_last_result_ignores_other_devices(db_path, utc_iso):
    target = _gateway(db_path)
    quality_db.insert_probe_results(db_path, _rows(target.id, utc_iso))

    assert quality_db.last_result_per_target(db_path)[target.id]["device_id"] == "nas"
