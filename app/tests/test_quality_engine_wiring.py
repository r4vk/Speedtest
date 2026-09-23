"""The engine owns a load test runner and a diagnostics runner (spec §10, §11).

These tests are about the wiring only — what each runner does on its own is
covered by `test_load_tests.py` and `test_diagnostics.py`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pytest

from speedtest_app import quality_db
from speedtest_app.config import AppConfig
from speedtest_app.diagnostics import DiagnosticsRunner
from speedtest_app.incidents import IncidentEvent, IncidentState
from speedtest_app.load_tests import LoadTestRunner
from speedtest_app.probe_types import ProbeTarget, Protocol
from speedtest_app.quality_engine import QualityEngine
from speedtest_app.time_utils import parse_dt, to_iso_z

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


# -- expected windows ------------------------------------------------------
#
# A closing incident is matched against the expected-window rules (spec §5).
# The rules are local wall-clock times, so the zone is pinned; Europe/Warsaw in
# September is UTC+2, which puts the local 02:55–03:15 window at 00:55–01:15Z.


@pytest.fixture
def warsaw(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the local zone for one test, the way `test_expected_windows.py` does."""
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def nightly_window(db_path: str) -> int:
    return quality_db.insert_expected_window(
        db_path,
        name="restart routera",
        time_from="02:55",
        time_to="03:15",
        days="[0,1,2,3,4,5,6]",
    )


def persist(engine: QualityEngine, event: IncidentEvent) -> None:
    engine._persist_event(event, engine.incident_settings)


def incident_event(
    event_type: str, state: IncidentState, *, target_id: int, at: str
) -> IncidentEvent:
    return IncidentEvent(
        type=event_type,  # type: ignore[arg-type]
        target_id=target_id,
        protocol="icmp",
        state=state,
        close_reason=state.close_reason,
        window_start=parse_dt(at),
        window_end=parse_dt(at),
    )


def close_incident(
    engine: QualityEngine, *, started_at: str, ended_at: str, target_id: int = 1
) -> int:
    """Open an incident through the engine, then close it; return the row id.

    Both events go through `_persist_event`, so the insert and the closing
    update are the same two statements a real outage produces.
    """
    state = IncidentState(status="open", started_at=started_at, kind="outage")
    persist(engine, incident_event("opened", state, target_id=target_id, at=started_at))
    state.status = "closed"
    state.ended_at = ended_at
    state.closed_at = ended_at
    state.close_reason = "recovered"
    persist(engine, incident_event("closed", state, target_id=target_id, at=ended_at))
    assert state.incident_id is not None
    return state.incident_id


def test_closing_incident_inside_a_window_marks_it_expected(db_path: str, warsaw: None) -> None:
    rule_id = nightly_window(db_path)
    engine = make_engine(db_path)

    incident_id = close_incident(
        engine, started_at="2026-09-21T01:00:00.000Z", ended_at="2026-09-21T01:04:00.000Z"
    )

    row = quality_db.get_incident(db_path, incident_id)
    assert row is not None
    assert row["expected"] == 1
    assert row["expected_source"] == "rule"
    assert row["expected_rule_id"] == rule_id


def test_closing_incident_that_overruns_the_window_stays_unexpected(
    db_path: str, warsaw: None
) -> None:
    nightly_window(db_path)
    engine = make_engine(db_path)

    incident_id = close_incident(
        engine, started_at="2026-09-21T01:00:00.000Z", ended_at="2026-09-21T04:30:00.000Z"
    )

    row = quality_db.get_incident(db_path, incident_id)
    assert row is not None
    assert row["expected"] == 0
    assert row["expected_source"] is None


def test_a_rule_scoped_to_another_target_does_not_mark_the_incident(
    db_path: str, warsaw: None
) -> None:
    quality_db.insert_expected_window(
        db_path,
        name="restart routera",
        time_from="02:55",
        time_to="03:15",
        days="[0,1,2,3,4,5,6]",
        target_id=2,
    )
    engine = make_engine(db_path)

    incident_id = close_incident(
        engine,
        started_at="2026-09-21T01:00:00.000Z",
        ended_at="2026-09-21T01:04:00.000Z",
        target_id=1,
    )

    row = quality_db.get_incident(db_path, incident_id)
    assert row is not None
    assert row["expected"] == 0


def test_an_updated_event_never_writes_the_flag(db_path: str, warsaw: None) -> None:
    """Only a closed outage has an end, so only a close can be judged."""
    nightly_window(db_path)
    engine = make_engine(db_path)
    state = IncidentState(status="open", started_at="2026-09-21T01:00:00.000Z", kind="outage")
    persist(engine, incident_event("opened", state, target_id=1, at="2026-09-21T01:00:00.000Z"))
    state.ended_at = "2026-09-21T01:04:00.000Z"

    persist(engine, incident_event("updated", state, target_id=1, at="2026-09-21T01:04:00.000Z"))

    assert state.incident_id is not None
    row = quality_db.get_incident(db_path, state.incident_id)
    assert row is not None
    assert row["expected"] == 0
    assert row["closed_at"] is None


def test_unreadable_rules_do_not_stop_the_incident_from_closing(
    db_path: str, warsaw: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(quality_db, "list_expected_windows", boom)
    engine = make_engine(db_path)

    incident_id = close_incident(
        engine, started_at="2026-09-21T01:00:00.000Z", ended_at="2026-09-21T01:04:00.000Z"
    )

    row = quality_db.get_incident(db_path, incident_id)
    assert row is not None
    assert row["closed_at"] is not None
    assert row["expected"] == 0
