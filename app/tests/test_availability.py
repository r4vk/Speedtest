"""Availability evaluation, period bookkeeping and quality state (spec §8).

`evaluate` is pure, so it is tested with hand-built probe results; the tracker
gets a real (temporary) database and a fake notifier, so no mail is ever sent
and no clock is guessed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import pytest

from speedtest_app.availability import (
    AvailabilitySettings,
    AvailabilityTracker,
    evaluate,
    quality_state,
)
from speedtest_app.config import AppConfig
from speedtest_app.db import get_current_connectivity_period, record_connectivity
from speedtest_app.probe_types import Outcome, ProbeResult, ProbeTarget, Protocol
from speedtest_app.time_utils import to_iso_z

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_target(
    target_id: int,
    *,
    kind: str = "internet",
    protocol: Protocol = Protocol.ICMP,
    enabled: bool = True,
) -> ProbeTarget:
    return ProbeTarget(
        id=target_id,
        name=f"target-{target_id}",
        kind=kind,
        protocol=protocol,
        host="1.1.1.1",
        port=None,
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=enabled,
        family_pref="auto",
        extra={},
    )


def results(
    target: ProbeTarget,
    outcomes: Sequence[Outcome],
    *,
    now: datetime = NOW,
    step: float = 1.0,
) -> list[ProbeResult]:
    """One result per outcome, the last one ``step`` seconds before ``now``."""
    count = len(outcomes)
    return [
        ProbeResult(
            target_id=target.id,
            protocol=target.protocol,
            started_at=to_iso_z(now - timedelta(seconds=(count - index) * step)),
            duration_ms=1.0,
            outcome=outcome,
            timeout_ms=target.timeout_ms,
            rtt_ms=10.0 if outcome is Outcome.OK else None,
        )
        for index, outcome in enumerate(outcomes)
    ]


def settings(**kwargs: Any) -> AvailabilitySettings:
    return AvailabilitySettings(**{"eval_seconds": 5.0, "window_seconds": 10.0, "min_samples": 5, **kwargs})


def make_config(tmp_path: Any, **kwargs: Any) -> AppConfig:
    return AppConfig(data_dir=str(tmp_path), **kwargs)


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, cfg: AppConfig, started_at: str, ended_at: str, duration_seconds: float) -> bool:
        self.calls.append((started_at, ended_at, duration_seconds))
        return True


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_settings_defaults_and_parsing() -> None:
    assert AvailabilitySettings() == AvailabilitySettings(
        eval_seconds=5.0, window_seconds=10.0, min_samples=5
    )
    parsed = AvailabilitySettings.from_settings(
        {
            "availability_eval_seconds": "2.5",
            "availability_window_seconds": "30",
            "incident_min_samples": "8",
        }
    )
    assert parsed == AvailabilitySettings(eval_seconds=2.5, window_seconds=30.0, min_samples=8)


def test_settings_keep_defaults_for_missing_or_broken_values() -> None:
    parsed = AvailabilitySettings.from_settings(
        {"availability_eval_seconds": "", "availability_window_seconds": "nonsense"}
    )
    assert parsed == AvailabilitySettings()


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


def test_up_when_one_target_answered_among_timeouts() -> None:
    good = make_target(1)
    bad = make_target(2)
    recent = {
        good.id: results(good, [Outcome.TIMEOUT, Outcome.TIMEOUT, Outcome.OK]),
        bad.id: results(bad, [Outcome.TIMEOUT] * 5),
    }
    assert evaluate(recent, [good, bad], now=NOW, settings=settings()) == "up"


def test_down_when_enough_attempts_and_none_answered() -> None:
    a = make_target(1)
    b = make_target(2, kind="tcp", protocol=Protocol.TCP)
    recent = {
        a.id: results(a, [Outcome.TIMEOUT] * 3),
        b.id: results(b, [Outcome.TIMEOUT, Outcome.TIMEOUT, Outcome.ERROR]),
    }
    # 5 measurable attempts (errors do not count), none ok
    assert evaluate(recent, [a, b], now=NOW, settings=settings()) == "down"


def test_no_data_when_too_few_attempts() -> None:
    a = make_target(1)
    recent = {a.id: results(a, [Outcome.TIMEOUT] * 4)}
    assert evaluate(recent, [a], now=NOW, settings=settings()) == "no_data"


def test_no_data_when_every_attempt_errored() -> None:
    a = make_target(1)
    recent = {a.id: results(a, [Outcome.ERROR] * 20)}
    assert evaluate(recent, [a], now=NOW, settings=settings()) == "no_data"


def test_no_data_when_only_the_gateway_answered() -> None:
    gateway = make_target(1, kind="gateway")
    internet = make_target(2)
    recent = {
        gateway.id: results(gateway, [Outcome.OK] * 10),
        internet.id: results(internet, [Outcome.ERROR] * 10),
    }
    assert evaluate(recent, [gateway, internet], now=NOW, settings=settings()) == "no_data"


def test_gateway_and_other_kinds_never_make_the_state_down() -> None:
    gateway = make_target(1, kind="gateway")
    dns = make_target(2, kind="dns", protocol=Protocol.DNS)
    recent = {
        gateway.id: results(gateway, [Outcome.TIMEOUT] * 10),
        dns.id: results(dns, [Outcome.TIMEOUT] * 10),
    }
    assert evaluate(recent, [gateway, dns], now=NOW, settings=settings()) == "no_data"


def test_disabled_targets_are_ignored() -> None:
    disabled = make_target(1, enabled=False)
    recent = {disabled.id: results(disabled, [Outcome.OK] * 10)}
    assert evaluate(recent, [disabled], now=NOW, settings=settings()) == "no_data"


def test_results_older_than_the_window_are_ignored() -> None:
    a = make_target(1)
    old = results(a, [Outcome.OK] * 10, now=NOW - timedelta(seconds=60))
    assert evaluate({a.id: old}, [a], now=NOW, settings=settings()) == "no_data"


def test_results_of_unknown_targets_are_ignored() -> None:
    a = make_target(1)
    ghost = make_target(99)
    recent = {ghost.id: results(ghost, [Outcome.OK] * 10)}
    assert evaluate(recent, [a], now=NOW, settings=settings()) == "no_data"


# ---------------------------------------------------------------------------
# tracker: connectivity_periods
# ---------------------------------------------------------------------------


async def test_tracker_writes_periods_for_up_down_up(db_path: str, tmp_path: Any) -> None:
    tracker = AvailabilityTracker(db_path, make_config(tmp_path), notifier=FakeNotifier())

    assert tracker.last_state is None
    assert tracker.apply("up", NOW) is None
    assert tracker.apply("down", NOW + timedelta(seconds=5)) == "up"
    assert tracker.apply("up", NOW + timedelta(seconds=10)) == "down"
    assert tracker.last_state == "up"
    await tracker.drain()

    periods = _periods(db_path)
    assert [p["is_up"] for p in periods] == [1, 0, 1]
    assert periods[0]["ended_at"] == to_iso_z(NOW + timedelta(seconds=5))
    assert periods[-1]["ended_at"] is None


async def test_tracker_repeated_state_does_not_open_a_new_period(db_path: str, tmp_path: Any) -> None:
    tracker = AvailabilityTracker(db_path, make_config(tmp_path), notifier=FakeNotifier())
    for offset in range(4):
        tracker.apply("up", NOW + timedelta(seconds=offset))
    await tracker.drain()
    assert len(_periods(db_path)) == 1


async def test_no_data_ends_the_open_period_and_up_opens_a_fresh_one(
    db_path: str, tmp_path: Any
) -> None:
    tracker = AvailabilityTracker(db_path, make_config(tmp_path), notifier=FakeNotifier())
    tracker.apply("up", NOW)
    tracker.apply("no_data", NOW + timedelta(seconds=5))

    periods = _periods(db_path)
    assert len(periods) == 1
    assert periods[0]["ended_at"] == to_iso_z(NOW + timedelta(seconds=5))
    assert get_current_connectivity_period(db_path) is None

    tracker.apply("up", NOW + timedelta(seconds=10))
    await tracker.drain()
    periods = _periods(db_path)
    assert len(periods) == 2
    assert periods[1]["started_at"] == to_iso_z(NOW + timedelta(seconds=10))
    assert periods[1]["ended_at"] is None


async def test_no_data_without_an_open_period_writes_nothing(db_path: str, tmp_path: Any) -> None:
    tracker = AvailabilityTracker(db_path, make_config(tmp_path), notifier=FakeNotifier())
    assert tracker.apply("no_data", NOW) is None
    await tracker.drain()
    assert _periods(db_path) == []


# ---------------------------------------------------------------------------
# tracker: recovery email
# ---------------------------------------------------------------------------


def smtp_config(tmp_path: Any, min_outage: int = 60) -> AppConfig:
    return make_config(
        tmp_path,
        smtp_host="smtp.example.org",
        smtp_user="user",
        smtp_password="secret",
        smtp_to="ops@example.org",
        smtp_min_outage_seconds=min_outage,
    )


async def test_recovery_email_sent_once_for_a_long_enough_outage(db_path: str, tmp_path: Any) -> None:
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=notifier)

    tracker.apply("up", NOW)
    tracker.apply("down", NOW + timedelta(seconds=10))
    tracker.apply("down", NOW + timedelta(seconds=40))
    tracker.apply("up", NOW + timedelta(seconds=130))
    tracker.apply("up", NOW + timedelta(seconds=140))
    await tracker.drain()

    assert len(notifier.calls) == 1
    assert notifier.calls[0][2] == pytest.approx(120.0)


async def test_recovery_email_skipped_for_a_short_outage(db_path: str, tmp_path: Any) -> None:
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=notifier)

    tracker.apply("up", NOW)
    tracker.apply("down", NOW + timedelta(seconds=10))
    tracker.apply("up", NOW + timedelta(seconds=40))
    await tracker.drain()

    assert notifier.calls == []


async def test_no_email_without_smtp_configuration(db_path: str, tmp_path: Any) -> None:
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, make_config(tmp_path), notifier=notifier)

    tracker.apply("down", NOW)
    tracker.apply("up", NOW + timedelta(seconds=600))
    await tracker.drain()

    assert notifier.calls == []


async def test_unmeasured_time_cancels_the_pending_recovery_email(db_path: str, tmp_path: Any) -> None:
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=notifier)

    tracker.apply("down", NOW)
    tracker.apply("no_data", NOW + timedelta(seconds=100))
    tracker.apply("up", NOW + timedelta(seconds=200))
    await tracker.drain()

    assert notifier.calls == []


async def test_a_failing_notifier_never_breaks_the_tracker(db_path: str, tmp_path: Any) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("smtp is down")

    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=boom)
    tracker.apply("down", NOW)
    tracker.apply("up", NOW + timedelta(seconds=600))
    await tracker.drain()

    assert tracker.last_state == "up"


# ---------------------------------------------------------------------------
# tracker: restart
# ---------------------------------------------------------------------------


async def test_restart_adopts_the_open_period_without_duplicating_it(
    db_path: str, tmp_path: Any
) -> None:
    record_connectivity(db_path, is_up=True, now_iso=to_iso_z(NOW - timedelta(seconds=300)))
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=FakeNotifier())

    assert tracker.last_state == "up"
    tracker.apply("up", NOW)
    await tracker.drain()
    assert len(_periods(db_path)) == 1


async def test_restart_during_an_outage_keeps_the_outage_start(db_path: str, tmp_path: Any) -> None:
    notifier = FakeNotifier()
    started = NOW - timedelta(seconds=300)
    record_connectivity(db_path, is_up=False, now_iso=to_iso_z(started))
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=notifier)

    assert tracker.last_state == "down"
    tracker.apply("up", NOW)
    await tracker.drain()

    assert len(notifier.calls) == 1
    assert notifier.calls[0][2] == pytest.approx(300.0)
    assert [p["is_up"] for p in _periods(db_path)] == [0, 1]


# ---------------------------------------------------------------------------
# quality_state
# ---------------------------------------------------------------------------


def test_quality_is_ok_without_incidents() -> None:
    targets = [make_target(1), make_target(2, kind="gateway")]
    assert quality_state("up", [], targets) == ("ok", False)


def test_quality_is_unknown_when_availability_is_unknown() -> None:
    targets = [make_target(1)]
    assert quality_state("no_data", [], targets) == ("unknown", False)


def test_quality_is_degraded_for_an_internet_incident() -> None:
    targets = [make_target(1), make_target(2, kind="tcp", protocol=Protocol.TCP)]
    assert quality_state("up", [{"target_id": 1}], targets) == ("degraded", False)
    assert quality_state("up", [{"target_id": 2}], targets) == ("degraded", False)


def test_gateway_incident_is_reported_separately() -> None:
    targets = [make_target(1), make_target(2, kind="gateway")]
    assert quality_state("up", [{"target_id": 2}], targets) == ("ok", True)
    assert quality_state("up", [{"target_id": 1}, {"target_id": 2}], targets) == ("degraded", True)


def test_incident_on_an_unknown_target_does_not_change_quality() -> None:
    targets = [make_target(1)]
    assert quality_state("up", [{"target_id": 77}], targets) == ("ok", False)


def test_open_incident_outranks_missing_availability_data() -> None:
    targets = [make_target(1)]
    assert quality_state("no_data", [{"target_id": 1}], targets) == ("degraded", False)


# ---------------------------------------------------------------------------


def _periods(db_path: str) -> list[dict[str, Any]]:
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, started_at, ended_at, is_up FROM connectivity_periods ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
