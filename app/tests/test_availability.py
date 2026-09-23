"""Availability evaluation, period bookkeeping and quality state (spec §8).

`evaluate` is pure, so it is tested with hand-built probe results; the tracker
gets a real (temporary) database and a fake notifier, so no mail is ever sent
and no clock is guessed.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

import pytest

from speedtest_app import quality_db
from speedtest_app.availability import (
    AvailabilitySettings,
    AvailabilityTracker,
    evaluate,
    quality_state,
)
from speedtest_app.config import AppConfig
from speedtest_app.db import (
    TimeRange,
    get_current_connectivity_period,
    mark_connectivity_period_expected,
    query_connectivity_periods,
    record_connectivity,
)
from speedtest_app.probe_types import Outcome, ProbeResult, ProbeTarget, Protocol
from speedtest_app.time_utils import parse_dt, to_iso_z

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


async def test_a_short_gap_inside_an_outage_keeps_its_start(db_path: str, tmp_path: Any) -> None:
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(
        db_path, smtp_config(tmp_path), notifier=notifier, no_data_close_seconds=300.0
    )

    tracker.apply("down", NOW)
    tracker.apply("no_data", NOW + timedelta(seconds=100))
    tracker.apply("no_data", NOW + timedelta(seconds=105))
    tracker.apply("down", NOW + timedelta(seconds=110))
    tracker.apply("up", NOW + timedelta(seconds=200))
    await tracker.drain()

    assert len(notifier.calls) == 1
    # the mail reports the outage from its real start, gap included
    assert notifier.calls[0][2] == pytest.approx(200.0)


async def test_a_gap_longer_than_the_no_data_budget_drops_the_outage(
    db_path: str, tmp_path: Any
) -> None:
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(
        db_path, smtp_config(tmp_path), notifier=notifier, no_data_close_seconds=300.0
    )

    tracker.apply("down", NOW)
    tracker.apply("no_data", NOW + timedelta(seconds=100))
    tracker.apply("no_data", NOW + timedelta(seconds=399))
    assert tracker.apply("no_data", NOW + timedelta(seconds=400)) == "no_data"
    tracker.apply("up", NOW + timedelta(seconds=500))
    await tracker.drain()

    assert notifier.calls == []


async def test_a_gap_is_measured_from_its_own_start(db_path: str, tmp_path: Any) -> None:
    """Two short gaps far apart are two gaps, not one long one."""
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(
        db_path, smtp_config(tmp_path), notifier=notifier, no_data_close_seconds=300.0
    )

    tracker.apply("down", NOW)
    tracker.apply("no_data", NOW + timedelta(seconds=100))
    tracker.apply("down", NOW + timedelta(seconds=200))
    tracker.apply("no_data", NOW + timedelta(seconds=600))
    tracker.apply("up", NOW + timedelta(seconds=700))
    await tracker.drain()

    assert len(notifier.calls) == 1
    assert notifier.calls[0][2] == pytest.approx(700.0)


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


async def test_restart_then_a_gap_still_reports_the_adopted_outage(
    db_path: str, tmp_path: Any
) -> None:
    notifier = FakeNotifier()
    started = NOW - timedelta(seconds=300)
    record_connectivity(db_path, is_up=False, now_iso=to_iso_z(started))
    tracker = AvailabilityTracker(
        db_path, smtp_config(tmp_path), notifier=notifier, no_data_close_seconds=300.0
    )

    assert tracker.last_state == "down"
    tracker.apply("no_data", NOW)  # the first verdict after the restart
    tracker.apply("down", NOW + timedelta(seconds=10))
    tracker.apply("up", NOW + timedelta(seconds=60))
    await tracker.drain()

    assert len(notifier.calls) == 1
    assert notifier.calls[0][2] == pytest.approx(360.0)


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


# ---------------------------------------------------------------------------
# query_connectivity_periods / mark_connectivity_period_expected (Task 4)
# ---------------------------------------------------------------------------


def test_query_connectivity_periods_exposes_id_and_flag(db_path: str) -> None:
    record_connectivity(db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    tr = TimeRange(start_iso="2026-09-21T00:00:00.000Z", end_iso="2026-09-21T02:00:00.000Z")
    down = query_connectivity_periods(db_path, tr=tr, is_up=False)
    assert set(down[0]) >= {"id", "started_at", "ended_at", "is_up",
                            "expected", "expected_source", "expected_rule_id"}
    assert down[0]["expected"] == 0


def test_mark_connectivity_period_expected_targets_the_down_period(db_path: str) -> None:
    record_connectivity(db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    updated = mark_connectivity_period_expected(
        db_path, started_at_iso="2026-09-21T01:00:00.000Z",
        expected=True, source="rule", rule_id=None,
    )
    assert updated == 1
    tr = TimeRange(start_iso="2026-09-21T00:00:00.000Z", end_iso="2026-09-21T02:00:00.000Z")
    assert query_connectivity_periods(db_path, tr=tr, is_up=False)[0]["expected"] == 1
    # The "up" period that starts at the same instant must not be touched.
    assert query_connectivity_periods(db_path, tr=tr, is_up=True)[0]["expected"] == 0


def test_query_connectivity_periods_expected_filter(db_path: str) -> None:
    record_connectivity(db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    record_connectivity(db_path, is_up=False, now_iso="2026-09-21T03:00:00.000Z")
    record_connectivity(db_path, is_up=True, now_iso="2026-09-21T03:05:00.000Z")
    mark_connectivity_period_expected(
        db_path, started_at_iso="2026-09-21T03:00:00.000Z",
        expected=True, source="rule", rule_id=None,
    )
    tr = TimeRange(start_iso="2026-09-21T00:00:00.000Z", end_iso="2026-09-21T04:00:00.000Z")

    def starts(mode: str) -> list[str]:
        return [r["started_at"] for r in
                query_connectivity_periods(db_path, tr=tr, is_up=False, expected=mode)]

    assert starts("exclude") == ["2026-09-21T01:00:00.000Z"]
    assert starts("only") == ["2026-09-21T03:00:00.000Z"]
    assert len(starts("all")) == 2


# ---------------------------------------------------------------------------
# tracker: expected windows (Task 6)
# ---------------------------------------------------------------------------


@pytest.fixture
def warsaw(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the local zone: a rule is a wall clock, so it needs a known one.

    Europe/Warsaw in September is UTC+2, which puts the 02:55–03:15 rule below
    between 00:55Z and 01:15Z.
    """
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def nightly_window(db_path: str) -> int:
    """The daily 02:55–03:15 router reboot the spec uses as its example."""
    return quality_db.insert_expected_window(
        db_path,
        name="restart routera",
        time_from="02:55",
        time_to="03:15",
        days="[0,1,2,3,4,5,6]",
    )


def down_period(db_path: str, end_iso: str) -> dict[str, Any]:
    tr = TimeRange(start_iso="2026-09-21T00:00:00.000Z", end_iso=end_iso)
    return query_connectivity_periods(db_path, tr=tr, is_up=False)[0]


async def test_outage_inside_an_expected_window_sends_no_mail(
    db_path: str, tmp_path: Any, warsaw: None
) -> None:
    nightly_window(db_path)
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=notifier)

    tracker.apply("down", parse_dt("2026-09-21T01:00:00.000Z"))
    tracker.apply("up", parse_dt("2026-09-21T01:04:00.000Z"))
    await tracker.drain()

    assert notifier.calls == []
    down = down_period(db_path, "2026-09-21T02:00:00.000Z")
    assert (down["expected"], down["expected_source"]) == (1, "rule")
    assert down["expected_rule_id"] is not None


async def test_outage_that_overruns_the_window_still_mails(
    db_path: str, tmp_path: Any, warsaw: None
) -> None:
    nightly_window(db_path)
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=notifier)

    tracker.apply("down", parse_dt("2026-09-21T01:00:00.000Z"))
    tracker.apply("up", parse_dt("2026-09-21T04:30:00.000Z"))
    await tracker.drain()

    assert len(notifier.calls) == 1
    down = down_period(db_path, "2026-09-21T05:00:00.000Z")
    assert (down["expected"], down["expected_source"]) == (0, None)


async def test_an_expected_outage_is_flagged_without_smtp(
    db_path: str, tmp_path: Any, warsaw: None
) -> None:
    """The flag is a fact about the outage, not a property of the mailer."""
    nightly_window(db_path)
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, make_config(tmp_path), notifier=notifier)

    tracker.apply("down", parse_dt("2026-09-21T01:00:00.000Z"))
    tracker.apply("up", parse_dt("2026-09-21T01:04:00.000Z"))
    await tracker.drain()

    assert notifier.calls == []
    assert down_period(db_path, "2026-09-21T02:00:00.000Z")["expected"] == 1


async def test_unreadable_rules_still_send_the_mail(
    db_path: str, tmp_path: Any, warsaw: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review focus #1: a failed rule read must never swallow the outage mail.

    The outage would fit the rule, so the only reason the mail survives is that
    the read failure is read as "no rules". ``consulted`` keeps this honest: it
    fails as long as nobody asks the rules at all, which is the state before
    the feature exists.
    """
    nightly_window(db_path)
    consulted: list[str] = []

    def boom(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        consulted.append("read")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(quality_db, "list_expected_windows", boom)
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=notifier)

    tracker.apply("down", parse_dt("2026-09-21T01:00:00.000Z"))
    tracker.apply("up", parse_dt("2026-09-21T01:04:00.000Z"))
    await tracker.drain()

    assert consulted, "the tracker never consulted the expected windows"
    assert len(notifier.calls) == 1
    assert down_period(db_path, "2026-09-21T02:00:00.000Z")["expected"] == 0


async def test_short_outage_keeps_its_existing_min_seconds_behaviour(
    db_path: str, tmp_path: Any, warsaw: None
) -> None:
    """The expected check must not disturb the smtp_min_outage_seconds gate."""
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=notifier)

    tracker.apply("down", parse_dt("2026-09-21T01:00:00.000Z"))
    tracker.apply("up", parse_dt("2026-09-21T01:00:10.000Z"))
    await tracker.drain()

    assert notifier.calls == []


async def test_an_outage_split_by_a_gap_is_flagged_on_every_segment(
    db_path: str, tmp_path: Any, warsaw: None
) -> None:
    """Review finding: unmeasured time splits the outage into two periods.

    ``no_data`` closes the open period while the tracker keeps the outage's
    start mark, so the next ``down`` opens a second one. The verdict belongs to
    the whole outage, so both segments must carry it — flagging only the row
    that starts at the mark leaves the segment that actually ran to recovery
    looking like an ordinary, unexplained outage on the dashboard, in the CSV
    and in the downtime the quality report charges.
    """
    nightly_window(db_path)
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=notifier)

    tracker.apply("down", parse_dt("2026-09-21T01:00:00.000Z"))
    tracker.apply("no_data", parse_dt("2026-09-21T01:00:30.000Z"))
    tracker.apply("down", parse_dt("2026-09-21T01:01:00.000Z"))
    tracker.apply("up", parse_dt("2026-09-21T01:04:00.000Z"))
    await tracker.drain()

    assert notifier.calls == []
    tr = TimeRange(start_iso="2026-09-21T00:00:00.000Z", end_iso="2026-09-21T02:00:00.000Z")
    downs = query_connectivity_periods(db_path, tr=tr, is_up=False)
    assert [p["started_at"] for p in downs] == [
        "2026-09-21T01:00:00.000Z",
        "2026-09-21T01:01:00.000Z",
    ]
    assert [p["expected"] for p in downs] == [1, 1]
    assert [p["expected_source"] for p in downs] == ["rule", "rule"]
    assert all(p["expected_rule_id"] is not None for p in downs)


async def test_an_expected_outage_with_no_period_left_is_logged(
    db_path: str, tmp_path: Any, warsaw: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A suppressed mail with nothing written anywhere must not be silent.

    Retention (or a stale start mark) can leave the tracker with nothing to
    flag. The mail stays suppressed either way — the outage really was inside
    the window — but a warning is the only trace left that it happened.
    """
    nightly_window(db_path)
    notifier = FakeNotifier()
    tracker = AvailabilityTracker(db_path, smtp_config(tmp_path), notifier=notifier)
    tracker.apply("down", parse_dt("2026-09-21T01:00:00.000Z"))

    conn = sqlite3.connect(db_path)
    try:  # retention pruned the period out from under the tracker
        conn.execute("DELETE FROM connectivity_periods")
        conn.commit()
    finally:
        conn.close()

    with caplog.at_level(logging.WARNING, logger="speedtest_app.availability"):
        tracker.apply("up", parse_dt("2026-09-21T01:04:00.000Z"))
    await tracker.drain()

    assert notifier.calls == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("expected" in message.lower() for message in warnings), warnings
