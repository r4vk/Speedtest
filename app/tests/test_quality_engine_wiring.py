"""The engine owns a load test runner and a diagnostics runner (spec §10, §11).

These tests are about the wiring only — what each runner does on its own is
covered by `test_load_tests.py` and `test_diagnostics.py`.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from speedtest_app import quality_db
from speedtest_app.config import AppConfig
from speedtest_app.diagnostics import DiagnosticsRunner
from speedtest_app.incidents import IncidentEvent, IncidentState
from speedtest_app.load_tests import LoadTestRunner
from speedtest_app.probe_types import ProbeTarget, Protocol
from speedtest_app.quality_engine import QualityEngine
from speedtest_app.time_utils import to_iso_z

NOW = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)
MTR_REPORT = json.dumps(
    {
        "report": {
            "mtr": {"dst": "203.0.113.5"},
            "hubs": [
                {"count": 1, "host": "192.168.1.1", "Loss%": 0.0, "Snt": 10, "Avg": 1.2},
                {"count": 2, "host": "203.0.113.5", "Loss%": 30.0, "Snt": 10, "Avg": 20.0},
            ],
        }
    }
)


def make_engine(db_path: str) -> QualityEngine:
    cfg = AppConfig(data_dir=os.path.dirname(db_path))
    return QualityEngine(cfg, app_version="test", wall_clock=lambda: NOW)


def target(target_id: int, kind: str, host: str, *, enabled: bool = True) -> ProbeTarget:
    return ProbeTarget(
        id=target_id,
        name=kind,
        kind=kind,
        protocol=Protocol.ICMP,
        host=host,
        port=None,
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=enabled,
        family_pref="auto",
        extra={},
    )


async def drain(rounds: int = 60) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def test_the_engine_owns_both_runners(db_path: str) -> None:
    engine = make_engine(db_path)

    assert isinstance(engine.load_tests, LoadTestRunner)
    assert isinstance(engine.diagnostics, DiagnosticsRunner)
    # the diagnostics runner is subscribed to the incident events
    assert engine.diagnostics.on_incident_event in engine._subscribers


def test_the_status_counts_load_tests_and_diagnostics(db_path: str) -> None:
    engine = make_engine(db_path)

    status = engine.status()

    assert status["load_test_running"] is False
    assert status["diagnostics_running"] == 0


def test_the_gateway_host_is_the_enabled_gateway_target(db_path: str) -> None:
    engine = make_engine(db_path)
    targets = [target(1, "internet", "203.0.113.5"), target(2, "gateway", "192.168.1.1")]
    engine.scheduler.targets = lambda: targets  # type: ignore[method-assign]

    assert engine.gateway_host() == "192.168.1.1"

    engine.scheduler.targets = lambda: [  # type: ignore[method-assign]
        target(2, "gateway", "192.168.1.1", enabled=False)
    ]
    assert engine.gateway_host() is None


async def test_run_load_test_returns_a_row_id_when_nothing_is_configured(db_path: str) -> None:
    engine = make_engine(db_path)

    load_test_id = await engine.run_load_test()

    stored = quality_db.get_load_test(db_path, load_test_id)
    assert stored is not None
    assert stored["status"] == "skipped"
    assert stored["error"] == "not_configured"
    assert json.loads(stored["params_json"])["trigger"] == "manual"


async def test_an_incident_event_reaches_the_diagnostics_runner(db_path: str) -> None:
    engine = make_engine(db_path)
    probe_target = quality_db.insert_target(
        db_path,
        name="internet",
        kind="internet",
        protocol=Protocol.ICMP,
        host="203.0.113.5",
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=True,
    )
    incident_id = quality_db.insert_incident(
        db_path,
        target_id=probe_target.id,
        protocol="icmp",
        kind="degraded",
        started_at=to_iso_z(NOW),
        window_seconds=10,
        probe_interval_seconds=1.0,
    )

    calls: list[float] = []

    async def fake_mtr(argv: list[str], timeout: float) -> tuple[int, str, str]:
        calls.append(timeout)
        return (0, MTR_REPORT, "")

    engine.diagnostics._subprocess_runner = fake_mtr  # type: ignore[assignment]
    event = IncidentEvent(
        type="opened",
        target_id=probe_target.id,
        protocol="icmp",
        state=IncidentState(
            status="open", incident_id=incident_id, started_at=to_iso_z(NOW), kind="degraded"
        ),
        close_reason=None,
        window_start=NOW - timedelta(seconds=10),
        window_end=NOW,
    )

    await engine._notify(event, probe_target)
    await drain()

    rows: list[dict[str, Any]] = quality_db.query_diagnostics(db_path, incident_id=incident_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert calls == [90.0]
    await engine.diagnostics.close()
