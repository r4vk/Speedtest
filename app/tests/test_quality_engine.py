"""Deterministic tests for the quality engine (design spec §5, §7, §8).

Everything is injected: a virtual monotonic/wall clock pair, a virtual ``sleep``
that only moves when the test advances time, and a fake probe registry whose
outcome per target the test flips. No real timers, no real network, no waiting.

The wall clock is anchored on a multiple of the evaluation window, so window
boundaries fall on whole virtual seconds and every assertion below names the
exact window it is about.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import pytest

from speedtest_app import quality_db, quality_engine
from speedtest_app.config import AppConfig
from speedtest_app.db import TimeRange, query_blocked_periods, set_setting
from speedtest_app.probe_types import Outcome, ProbeResult, ProbeTarget, Protocol
from speedtest_app.quality_engine import QualityEngine
from speedtest_app.time_utils import to_iso_z

_EPOCH = 1_750_000_000.0  # divisible by every window width used here
_START = 1_000.0
WINDOW = 2.0


# ---------------------------------------------------------------------------
# virtual time
# ---------------------------------------------------------------------------


async def drain(rounds: int = 80) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeTime:
    """A monotonic/wall clock pair plus a ``sleep`` driven only by ``advance``."""

    def __init__(self, start: float = _START) -> None:
        self.now = start
        self._seq = 0
        self._sleepers: list[list[Any]] = []

    def clock(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return datetime.fromtimestamp(_EPOCH + self.now, tz=timezone.utc)

    def wall_at(self, moment: float) -> datetime:
        return datetime.fromtimestamp(_EPOCH + moment, tz=timezone.utc)

    def iso_at(self, moment: float) -> str:
        return to_iso_z(self.wall_at(moment))

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        self._seq += 1
        entry: list[Any] = [self.now + delay, self._seq, asyncio.get_running_loop().create_future()]
        self._sleepers.append(entry)
        try:
            await entry[2]
        finally:
            if entry in self._sleepers:
                self._sleepers.remove(entry)

    async def advance(self, amount: float) -> None:
        target = self.now + amount
        await drain()
        while True:
            due = sorted((e for e in self._sleepers if e[0] <= target), key=lambda e: (e[0], e[1]))
            if not due:
                break
            entry = due[0]
            self._sleepers.remove(entry)
            self.now = max(self.now, entry[0])
            if not entry[2].done():
                entry[2].set_result(None)
            await drain()
        self.now = target
        await drain()


# ---------------------------------------------------------------------------
# fakes and fixtures
# ---------------------------------------------------------------------------


class FakeProbes:
    """One probe for every protocol; the test owns the outcome per target."""

    def __init__(self, fake: FakeTime) -> None:
        self.fake = fake
        self.outcome: dict[int, Outcome] = {}
        self.calls = 0

    async def __call__(self, target: ProbeTarget) -> ProbeResult:
        self.calls += 1
        outcome = self.outcome.get(target.id, Outcome.OK)
        return ProbeResult(
            target_id=target.id,
            protocol=target.protocol,
            started_at=to_iso_z(self.fake.wall()),
            duration_ms=1.0,
            outcome=outcome,
            timeout_ms=target.timeout_ms,
            rtt_ms=10.0 if outcome is Outcome.OK else None,
            error_kind=None if outcome is not Outcome.ERROR else "exec",
        )

    def registry(self) -> dict[Protocol, Any]:
        return {protocol: self for protocol in Protocol}


DEFAULT_SETTINGS = {
    "incident_window_seconds": str(WINDOW),
    "incident_min_samples": "5",
    "incident_open_windows": "2",
    "incident_stabilization_seconds": "4",
    "incident_no_data_close_seconds": "300",
    "availability_eval_seconds": "1",
    "availability_window_seconds": "2",
}


def prepare_db(db_path: str, *, targets: int = 1, interval: float = 0.2, **settings: str) -> None:
    """Replace the seeded targets with fast fake ones and set the thresholds."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM probe_targets")
        # so the fake targets get the ids 1..n and every assertion can name them
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'probe_targets'")
    for index in range(targets):
        quality_db.insert_target(
            db_path,
            name=f"fake-{index + 1}",
            kind="internet",
            protocol=Protocol.ICMP,
            host=f"10.0.0.{index + 1}",
            interval_seconds=interval,
            timeout_ms=200,
            enabled=True,
        )
    for key, value in {**DEFAULT_SETTINGS, **settings}.items():
        set_setting(db_path, key, value, source="env")


def make_engine(db_path: str, fake: FakeTime, probes: FakeProbes) -> QualityEngine:
    cfg = AppConfig(
        data_dir=os.path.dirname(db_path),
        probe_flush_seconds=1.0,
        probe_flush_max=50,
    )
    return QualityEngine(
        cfg,
        app_version="test",
        probe_registry=probes.registry(),
        clock=fake.clock,
        wall_clock=fake.wall,
        sleep=fake.sleep,
    )


@asynccontextmanager
async def running(engine: QualityEngine) -> AsyncIterator[QualityEngine]:
    await engine.start()
    try:
        yield engine
    finally:
        await engine.stop()


def incidents(db_path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM incidents ORDER BY id").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def probe_rows(db_path: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM probe_results ORDER BY id").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# (a) start / stop
# ---------------------------------------------------------------------------


async def test_start_runs_the_targets_and_stop_flushes_their_rows(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    engine = make_engine(db_path, fake, probes)

    await engine.start()
    assert [t.name for t in engine.scheduler.targets()] == ["fake-1"]
    await fake.advance(3.0)
    assert probes.calls >= 15
    await engine.stop()

    rows = probe_rows(db_path)
    assert len(rows) == probes.calls
    assert {row["outcome"] for row in rows} == {"ok"}
    assert {row["device_id"] for row in rows} == {"nas"}


async def test_stop_is_idempotent_and_leaves_no_task_behind(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    engine = make_engine(db_path, fake, probes)

    await engine.start()
    await fake.advance(1.0)
    await engine.stop()
    await engine.stop()

    alive = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert alive == []


# ---------------------------------------------------------------------------
# (b) an incident opens, updates and closes
# ---------------------------------------------------------------------------


async def test_timeouts_open_an_incident_that_recovers(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    engine = make_engine(db_path, fake, probes)

    async with running(engine):
        await fake.advance(4.0)  # two healthy windows
        assert incidents(db_path) == []

        probes.outcome[1] = Outcome.TIMEOUT
        await fake.advance(6.0)  # [1004,1006) degraded, [1006,1008) outage -> opened

        opened = incidents(db_path)
        assert len(opened) == 1
        row = opened[0]
        assert row["started_at"] == fake.iso_at(1004.0)
        assert row["ended_at"] == fake.iso_at(1008.0)
        assert row["closed_at"] is None
        assert row["kind"] == "outage"
        assert row["window_seconds"] == 2
        assert row["probe_interval_seconds"] == pytest.approx(0.2)
        assert row["peak_loss_pct"] == pytest.approx(100.0)

        probes.outcome[1] = Outcome.OK
        await fake.advance(8.0)  # one last bad window, then two healthy ones

        closed = incidents(db_path)[0]
        assert closed["close_reason"] == "recovered"
        assert closed["ended_at"] == fake.iso_at(1010.0)
        assert closed["closed_at"] == fake.iso_at(1014.0)
        assert closed["ended_at"] < closed["closed_at"]
        assert closed["windows_degraded"] == 3


# ---------------------------------------------------------------------------
# (c) shutdown closes what is open
# ---------------------------------------------------------------------------


async def test_stop_closes_an_open_incident_with_shutdown(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    engine = make_engine(db_path, fake, probes)

    await engine.start()
    probes.outcome[1] = Outcome.TIMEOUT
    await fake.advance(6.0)
    assert len(incidents(db_path)) == 1
    await engine.stop()

    row = incidents(db_path)[0]
    assert row["close_reason"] == "shutdown"
    assert row["closed_at"] == fake.iso_at(fake.now)
    assert row["ended_at"] == fake.iso_at(1004.0)


# ---------------------------------------------------------------------------
# (d) a crash leaves an incident open
# ---------------------------------------------------------------------------


async def test_startup_closes_incidents_left_open_by_a_crash(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    stale_id = quality_db.insert_incident(
        db_path,
        target_id=1,
        protocol="icmp",
        kind="outage",
        started_at=fake.iso_at(_START - 600.0),
        ended_at=fake.iso_at(_START - 500.0),
        window_seconds=2,
        probe_interval_seconds=0.2,
    )
    engine = make_engine(db_path, fake, probes)

    async with running(engine):
        row = quality_db.get_incident(db_path, stale_id)
        assert row is not None
        assert row["close_reason"] == "no_data"
        assert row["closed_at"] == fake.iso_at(_START)
        assert row["ended_at"] == fake.iso_at(_START - 500.0)


async def test_startup_closes_load_tests_left_running_by_a_crash(db_path: str) -> None:
    """A killed process used to leave a `running` row visible for ever."""
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    stale_id = quality_db.insert_load_test(
        db_path,
        started_at=fake.iso_at(_START - 600.0),
        kind="iperf_udp",
        direction="both",
        server="iperf.example",
        params_json="{}",
        status="running",
    )
    finished_id = quality_db.insert_load_test(
        db_path,
        started_at=fake.iso_at(_START - 500.0),
        ended_at=fake.iso_at(_START - 490.0),
        kind="iperf_udp",
        direction="both",
        server="iperf.example",
        params_json="{}",
        status="ok",
    )
    engine = make_engine(db_path, fake, probes)

    async with running(engine):
        stale = quality_db.get_load_test(db_path, stale_id)
        assert (stale["status"], stale["error"]) == ("error", "interrupted")
        assert stale["ended_at"] == fake.iso_at(_START)
        # a finished row is left exactly as it was
        assert quality_db.get_load_test(db_path, finished_id)["status"] == "ok"


# ---------------------------------------------------------------------------
# (e) blocked probes
# ---------------------------------------------------------------------------


async def test_disabled_ping_blocks_the_targets_and_records_the_period(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    set_setting(db_path, "ping_enabled", "false", source="env")
    engine = make_engine(db_path, fake, probes)

    async with running(engine):
        await fake.advance(5.0)
        assert engine.scheduler.targets() == []
        assert engine.blocked_reason == "disabled"
        assert probes.calls == 0

    assert probe_rows(db_path) == []
    blocked = query_blocked_periods(
        db_path,
        TimeRange(start_iso=fake.iso_at(_START - 10.0), end_iso=fake.iso_at(fake.now + 10.0)),
        test_type="ping",
    )
    assert [row["reason"] for row in blocked] == ["disabled"]


async def test_reenabling_ping_ends_the_blocked_period_and_starts_probing(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    set_setting(db_path, "ping_enabled", "false", source="env")
    engine = make_engine(db_path, fake, probes)

    async with running(engine):
        await fake.advance(1.0)
        set_setting(db_path, "ping_enabled", "true", source="env")
        await fake.advance(12.0)  # the scheduler reloads its targets every 10 s
        assert engine.blocked_reason is None
        assert probes.calls > 0

    blocked = query_blocked_periods(
        db_path,
        TimeRange(start_iso=fake.iso_at(_START - 10.0), end_iso=fake.iso_at(fake.now + 10.0)),
        test_type="ping",
    )
    assert len(blocked) == 1
    assert blocked[0]["ended_at"] is not None


# ---------------------------------------------------------------------------
# (f) settings refresh
# ---------------------------------------------------------------------------


async def test_settings_refresh_replaces_the_thresholds(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path, interval=1.0)
    engine = make_engine(db_path, fake, probes)

    async with running(engine):
        assert engine.incident_settings.loss_pct_threshold == pytest.approx(20.0)
        set_setting(db_path, "incident_loss_pct_threshold", "55", source="env")
        set_setting(db_path, "availability_eval_seconds", "3", source="env")
        await fake.advance(31.0)

        assert engine.incident_settings.loss_pct_threshold == pytest.approx(55.0)
        assert engine.incident_engine.settings is engine.incident_settings
        assert engine.availability_settings.eval_seconds == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# (g) subscribers
# ---------------------------------------------------------------------------


async def test_subscribers_receive_the_opened_event_with_its_target(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    engine = make_engine(db_path, fake, probes)
    seen: list[tuple[str, int | None]] = []

    async def async_subscriber(event: Any, target: ProbeTarget | None) -> None:
        seen.append((event.type, target.id if target else None))

    def broken_subscriber(event: Any, target: ProbeTarget | None) -> None:
        raise RuntimeError("subscriber is broken")

    engine.subscribe(broken_subscriber)
    engine.subscribe(async_subscriber)

    async with running(engine):
        probes.outcome[1] = Outcome.TIMEOUT
        await fake.advance(8.0)

    assert seen[0] == ("opened", 1)
    assert [event for event, _ in seen[1:]] == ["updated"] * (len(seen) - 1)
    opened = [event for event in seen if event[0] == "opened"]
    assert len(opened) == 1


async def test_subscribers_see_the_incident_id_of_the_opened_event(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    engine = make_engine(db_path, fake, probes)
    ids: list[int | None] = []

    engine.subscribe(lambda event, _target: ids.append(event.state.incident_id))

    async with running(engine):
        probes.outcome[1] = Outcome.TIMEOUT
        await fake.advance(6.0)

    assert ids[0] == incidents(db_path)[0]["id"]


# ---------------------------------------------------------------------------
# (h) status
# ---------------------------------------------------------------------------


async def test_status_reports_availability_quality_and_scheduler_health(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path, targets=2)
    engine = make_engine(db_path, fake, probes)

    assert engine.status()["availability"] == "no_data"

    async with running(engine):
        await fake.advance(4.0)
        status = engine.status()

        assert status["availability"] == "up"
        assert status["quality"] == "ok"
        assert status["lan_degraded"] is False
        assert isinstance(status["icmp_method"], str)
        assert status["open_incidents"] == []
        assert set(status["scheduler"]) == {
            "buffered_rows",
            "dropped_rows",
            "skipped_ticks",
            "restarts",
            "flush_errors",
            "last_flush_at",
        }
        assert status["scheduler"]["restarts"] == {}
        assert [t["id"] for t in status["targets"]] == [1, 2]
        assert status["targets"][0]["protocol"] == "icmp"
        assert status["targets"][0]["kind"] == "internet"
        assert status["targets"][0]["enabled"] is True
        assert status["targets"][0]["last"]["outcome"] == "ok"

        probes.outcome[1] = Outcome.TIMEOUT
        probes.outcome[2] = Outcome.TIMEOUT
        await fake.advance(6.0)
        degraded = engine.status()
        assert degraded["availability"] == "down"
        assert degraded["quality"] == "degraded"
        assert len(degraded["open_incidents"]) == 2


async def test_status_survives_a_broken_database(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    engine = make_engine(db_path, fake, probes)

    async with running(engine):
        await fake.advance(2.0)
        os.replace(db_path, db_path + ".moved")
        try:
            with open(db_path, "w", encoding="utf-8") as handle:
                handle.write("this is not a database")
            status = engine.status()
            assert status["open_incidents"] == []
            assert status["quality"] in {"ok", "unknown"}
        finally:
            os.replace(db_path + ".moved", db_path)


# ---------------------------------------------------------------------------
# availability wiring
# ---------------------------------------------------------------------------


async def test_availability_periods_follow_the_probe_outcomes(db_path: str) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    engine = make_engine(db_path, fake, probes)

    async with running(engine):
        await fake.advance(3.0)
        assert engine.availability.last_state == "up"
        probes.outcome[1] = Outcome.TIMEOUT
        await fake.advance(4.0)
        assert engine.availability.last_state == "down"
        probes.outcome[1] = Outcome.ERROR
        await fake.advance(4.0)
        assert engine.availability.last_state == "no_data"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM connectivity_periods ORDER BY id")]
    finally:
        conn.close()
    assert [row["is_up"] for row in rows] == [1, 0]
    assert rows[-1]["ended_at"] is not None  # no_data closed the outage period


# ---------------------------------------------------------------------------
# a failed insert must not lose the incident
# ---------------------------------------------------------------------------


async def test_a_failed_opening_insert_is_retried_on_the_next_event(
    db_path: str, monkeypatch: Any
) -> None:
    fake = FakeTime()
    probes = FakeProbes(fake)
    prepare_db(db_path)
    engine = make_engine(db_path, fake, probes)

    real_insert = quality_db.insert_incident
    failures: list[int] = []

    def flaky_insert(path: str, **fields: Any) -> int:
        if not failures:
            failures.append(1)
            raise sqlite3.OperationalError("database is locked")
        return real_insert(path, **fields)

    monkeypatch.setattr(quality_engine.quality_db, "insert_incident", flaky_insert)

    async with running(engine):
        probes.outcome[1] = Outcome.TIMEOUT
        await fake.advance(6.0)  # the `opened` insert fails
        assert incidents(db_path) == []
        assert failures == [1]

        await fake.advance(2.0)  # the next `updated` writes the row after all
        rows = incidents(db_path)
        assert len(rows) == 1
        assert rows[0]["started_at"] == fake.iso_at(_START)
        assert rows[0]["closed_at"] is None

        probes.outcome[1] = Outcome.OK
        await fake.advance(8.0)  # ... and it still closes properly

    closed = incidents(db_path)[0]
    assert closed["close_reason"] == "recovered"
    assert closed["closed_at"] is not None


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


class Wall:
    """A wall clock the test moves by hand, with real calendar dates."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def write_probe_rows(db_path: str, target_id: int, start: datetime, count: int, step: float) -> None:
    quality_db.insert_probe_results(
        db_path,
        [
            ProbeResult(
                target_id=target_id,
                protocol=Protocol.ICMP,
                started_at=to_iso_z(start + timedelta(seconds=index * step)),
                duration_ms=1.0,
                outcome=Outcome.OK,
                timeout_ms=1000,
                rtt_ms=10.0 + index,
            )
            for index in range(count)
        ],
    )


def aggregate_engine(db_path: str, wall: Wall) -> QualityEngine:
    fake = FakeTime()
    cfg = AppConfig(data_dir=os.path.dirname(db_path))
    return QualityEngine(
        cfg,
        app_version="test",
        probe_registry={},
        clock=fake.clock,
        wall_clock=wall,
        sleep=fake.sleep,
    )


async def test_aggregation_completes_yesterday_and_follows_the_day_change(
    db_path: str, monkeypatch: Any
) -> None:
    prepare_db(db_path)
    yesterday = datetime(2026, 3, 1, tzinfo=timezone.utc)
    today = yesterday + timedelta(days=1)
    write_probe_rows(db_path, 1, yesterday + timedelta(hours=22), 120, 60.0)  # 22:00-23:59
    write_probe_rows(db_path, 1, today + timedelta(hours=8, minutes=30), 120, 60.0)

    # more than the rolling window after midnight: that window alone can never
    # complete yesterday's daily bucket
    wall = Wall(today + timedelta(hours=10, minutes=30))
    engine = aggregate_engine(db_path, wall)

    ranges: list[tuple[datetime, datetime]] = []
    real = quality_engine.aggregates.aggregate_all

    def spy(path: str, targets: Any, start: datetime, end: datetime, *, now_iso: str) -> Any:
        ranges.append((start, end))
        return real(path, targets, start, end, now_iso=now_iso)

    monkeypatch.setattr(quality_engine.aggregates, "aggregate_all", spy)

    # first pass after a start: yesterday has no daily row yet, so it is built
    engine._aggregate_once()
    assert len(ranges) == 2
    assert ranges[0] == (wall.moment - timedelta(hours=2), wall.moment)
    assert ranges[1] == (yesterday, today)
    daily = quality_db.query_aggregates(db_path, "1d", to_iso_z(yesterday), to_iso_z(yesterday))
    assert len(daily) == 1
    assert daily[0]["attempts"] == 120
    hourly = quality_db.query_aggregates(
        db_path, "1h", to_iso_z(yesterday), to_iso_z(today + timedelta(hours=23))
    )
    assert [row["bucket_start"] for row in hourly] == [
        to_iso_z(yesterday + timedelta(hours=22)),
        to_iso_z(yesterday + timedelta(hours=23)),
        to_iso_z(today + timedelta(hours=8)),
        to_iso_z(today + timedelta(hours=9)),
    ]
    assert engine._last_aggregated_day == today.date()

    # a second pass on the same day does not redo yesterday
    ranges.clear()
    engine._aggregate_once()
    assert len(ranges) == 1

    # ... and neither does a later start, because the daily row is there now
    ranges.clear()
    engine._last_aggregated_day = None
    engine._aggregate_once()
    assert len(ranges) == 1

    # crossing midnight while running always aggregates the day that just ended
    write_probe_rows(db_path, 1, today + timedelta(hours=23), 60, 60.0)
    wall.moment = today + timedelta(days=1, hours=10, minutes=30)
    ranges.clear()
    engine._aggregate_once()
    assert len(ranges) == 2
    assert ranges[1] == (today, today + timedelta(days=1))
    daily = quality_db.query_aggregates(db_path, "1d", to_iso_z(today), to_iso_z(today))
    assert len(daily) == 1
    assert daily[0]["attempts"] == 180


async def test_aggregation_does_nothing_without_targets(db_path: str, monkeypatch: Any) -> None:
    prepare_db(db_path, targets=0)
    calls: list[Any] = []
    monkeypatch.setattr(
        quality_engine.aggregates, "aggregate_all", lambda *a, **k: calls.append(a)
    )
    engine = aggregate_engine(db_path, Wall(datetime(2026, 3, 2, tzinfo=timezone.utc)))

    engine._aggregate_once()
    assert calls == []
