"""Incident state machine (design spec §7)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from speedtest_app import incidents, quality_db, stats
from speedtest_app.incidents import IncidentEngine, IncidentSettings
from speedtest_app.stats import ProbeStats
from speedtest_app.time_utils import to_iso_z

BASE = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)


def _at(offset_seconds: float) -> str:
    return to_iso_z(BASE + timedelta(seconds=offset_seconds))


def _stats(*, ok: int = 0, timeouts: int = 0, errors: int = 0,
           p95: float | None = None, streak: int = 0) -> ProbeStats:
    """A synthetic window: only the fields the state machine reads."""
    measurable = ok + timeouts
    return ProbeStats(
        attempts=ok + timeouts + errors,
        ok=ok,
        timeouts=timeouts,
        errors=errors,
        loss_pct=(timeouts / measurable * 100) if measurable else None,
        rtt_p95_ms=p95,
        longest_fail_streak=streak,
    )


#: 10 attempts per 10 s window at 1 s sampling.
HEALTHY = _stats(ok=10)
DEGRADED = _stats(ok=7, timeouts=3, streak=3)
OUTAGE = _stats(timeouts=10, streak=10)
SLOW = _stats(ok=10, p95=200.0)
UNKNOWN = _stats(ok=2)
ALL_ERRORS = _stats(errors=10)


class Runner:
    """Feeds consecutive tumbling windows of one key to an engine."""

    def __init__(self, engine: IncidentEngine, *, target_id: int = 1, protocol: str = "icmp",
                 start: datetime = BASE, window_seconds: float = 10.0) -> None:
        self.engine = engine
        self.target_id = target_id
        self.protocol = protocol
        self.now = start
        self.window = timedelta(seconds=window_seconds)

    def feed(self, window_stats: ProbeStats, times: int = 1) -> list[incidents.IncidentEvent]:
        events: list[incidents.IncidentEvent] = []
        for _ in range(times):
            end = self.now + self.window
            events.extend(self.engine.feed(self.target_id, self.protocol, self.now, end, window_stats))
            self.now = end
        return events

    @property
    def state(self) -> incidents.IncidentState:
        return self.engine.state_for(self.target_id, self.protocol)


def _engine(**overrides) -> IncidentEngine:
    return IncidentEngine(IncidentSettings(**overrides), probe_interval_seconds=lambda target_id: 1.0)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def test_settings_defaults_match_the_spec_table():
    s = IncidentSettings()

    assert s.window_seconds == 10
    assert s.min_samples == 5
    assert s.loss_pct_threshold == 20.0
    assert s.outage_loss_pct == 100.0
    assert s.rtt_p95_ms_threshold == 150.0
    assert s.fail_streak_threshold == 3
    assert s.open_windows == 2
    assert s.stabilization_seconds == 60
    assert s.no_data_close_seconds == 300


def test_settings_from_settings_parses_and_falls_back():
    s = IncidentSettings.from_settings(
        {
            "incident_window_seconds": "20",
            "incident_min_samples": "3",
            "incident_loss_pct_threshold": "5.5",
            "incident_rtt_p95_ms_threshold": "80",
            "incident_fail_streak_threshold": "7",
            "incident_stabilization_seconds": "120",
            "incident_outage_loss_pct": "not-a-number",
            "incident_open_windows": "",
            "ping_timeout_ms": "1000",
        }
    )

    assert s.window_seconds == 20
    assert s.min_samples == 3
    assert s.loss_pct_threshold == 5.5
    assert s.rtt_p95_ms_threshold == 80.0
    assert s.fail_streak_threshold == 7
    assert s.stabilization_seconds == 120
    # invalid and empty values fall back to the defaults
    assert s.outage_loss_pct == 100.0
    assert s.open_windows == 2
    assert s.no_data_close_seconds == 300


def test_settings_from_empty_settings_is_the_default():
    assert IncidentSettings.from_settings({}) == IncidentSettings()


# ---------------------------------------------------------------------------
# window classification
# ---------------------------------------------------------------------------

def test_classify_window_needs_min_samples():
    s = IncidentSettings()

    assert incidents.classify_window(_stats(ok=4), s) == "unknown"
    assert incidents.classify_window(_stats(ok=2, timeouts=2), s) == "unknown"
    assert incidents.classify_window(_stats(ok=5), s) == "healthy"


def test_classify_window_unknown_when_nothing_was_measurable():
    s = IncidentSettings()

    assert incidents.classify_window(ALL_ERRORS, s) == "unknown"
    assert incidents.classify_window(stats.compute_stats([]), s) == "unknown"


def test_classify_window_outage_needs_the_outage_threshold():
    s = IncidentSettings()

    assert incidents.classify_window(OUTAGE, s) == "outage"
    # 99 % loss is bad, but the spec reserves `outage` for loss >= outage_loss_pct
    assert incidents.classify_window(_stats(ok=1, timeouts=99), s) == "degraded"


def test_classify_window_degraded_by_each_threshold():
    s = IncidentSettings()

    assert incidents.classify_window(_stats(ok=8, timeouts=2), s) == "degraded"  # 20 % loss
    assert incidents.classify_window(SLOW, s) == "degraded"  # p95 >= 150 ms
    assert incidents.classify_window(_stats(ok=17, timeouts=3, streak=3), s) == "degraded"  # streak
    assert incidents.classify_window(_stats(ok=17, timeouts=3, streak=2), s) == "healthy"  # 15 % loss
    assert incidents.classify_window(HEALTHY, s) == "healthy"


def test_short_burst_of_three_timeouts_in_one_window_is_degraded():
    # 10 s window at 1 s sampling: 3 lost responses out of 10
    burst = _stats(ok=7, timeouts=3, streak=3)

    assert burst.loss_pct == pytest.approx(30.0)
    assert incidents.classify_window(burst, IncidentSettings()) == "degraded"


# ---------------------------------------------------------------------------
# opening
# ---------------------------------------------------------------------------

def test_opening_needs_open_windows_consecutive_degraded_windows():
    run = Runner(_engine())

    assert run.feed(DEGRADED) == []
    assert run.state.status == "pending"
    assert run.state.pending_count == 1

    events = run.feed(DEGRADED)

    assert [e.type for e in events] == ["opened"]
    opened = events[0]
    assert opened.target_id == 1 and opened.protocol == "icmp"
    assert opened.state.status == "open"
    assert opened.state.started_at == _at(0)  # start of the FIRST degraded window
    assert opened.state.ended_at == _at(20)
    assert opened.state.kind == "degraded"
    assert opened.state.windows_degraded == 2
    assert opened.close_reason is None


def test_healthy_window_in_pending_resets_to_closed():
    run = Runner(_engine())

    run.feed(DEGRADED)
    assert run.feed(HEALTHY) == []
    assert run.state.status == "closed"
    assert run.state.pending_count == 0
    assert run.state.windows == []

    assert run.feed(DEGRADED) == []  # counting starts from scratch
    events = run.feed(DEGRADED)

    assert [e.type for e in events] == ["opened"]
    # windows: 0-10 degraded, 10-20 healthy (reset), 20-30 and 30-40 degraded
    assert events[0].state.started_at == _at(20)


def test_unknown_window_in_pending_is_ignored():
    run = Runner(_engine())

    run.feed(DEGRADED)
    assert run.feed(UNKNOWN) == []
    assert run.state.status == "pending"
    assert run.state.pending_count == 1

    events = run.feed(DEGRADED)

    assert [e.type for e in events] == ["opened"]
    assert events[0].state.started_at == _at(0)


def test_event_snapshot_is_a_copy_of_the_state():
    run = Runner(_engine())
    run.feed(DEGRADED)
    opened = run.feed(DEGRADED)[0]
    windows_at_open = list(opened.state.windows)

    run.feed(DEGRADED)

    assert opened.state.windows == windows_at_open
    assert opened.state.ended_at == _at(20)
    assert run.state.ended_at == _at(30)


# ---------------------------------------------------------------------------
# open state
# ---------------------------------------------------------------------------

def test_open_incident_updates_peaks_and_extends_ended_at():
    run = Runner(_engine())
    run.feed(_stats(ok=8, timeouts=2, streak=2), times=2)  # 20 % loss -> opens

    events = run.feed(_stats(ok=5, timeouts=5, p95=400.0, streak=4))

    assert [e.type for e in events] == ["updated"]
    state = run.state
    assert state.peak_loss_pct == pytest.approx(50.0)
    assert state.peak_p95_rtt_ms == pytest.approx(400.0)
    assert state.longest_fail_streak == 4
    assert state.windows_degraded == 3
    assert state.ended_at == _at(30)

    run.feed(_stats(ok=9, timeouts=1, p95=200.0, streak=1))

    # peaks never shrink
    assert run.state.peak_loss_pct == pytest.approx(50.0)
    assert run.state.peak_p95_rtt_ms == pytest.approx(400.0)
    assert run.state.longest_fail_streak == 4


def test_kind_escalates_to_outage_and_never_back():
    run = Runner(_engine())
    run.feed(DEGRADED, times=2)
    assert run.state.kind == "degraded"

    run.feed(OUTAGE)
    assert run.state.kind == "outage"

    run.feed(DEGRADED)
    assert run.state.kind == "outage"


def test_outage_from_the_first_window_opens_as_outage():
    run = Runner(_engine())
    run.feed(OUTAGE, times=2)

    assert run.state.kind == "outage"
    assert run.state.peak_loss_pct == pytest.approx(100.0)


def test_every_open_window_emits_updated_so_the_summary_can_be_refreshed():
    run = Runner(_engine())
    run.feed(DEGRADED, times=2)

    assert [e.type for e in run.feed(DEGRADED)] == ["updated"]
    assert [e.type for e in run.feed(HEALTHY)] == ["updated"]
    assert [e.type for e in run.feed(UNKNOWN)] == ["updated"]
    assert [w[1] for w in run.state.windows] == [
        "degraded", "degraded", "degraded", "healthy", "unknown",
    ]


def test_window_verdicts_record_start_verdict_loss_and_p95():
    run = Runner(_engine())
    run.feed(_stats(ok=5, timeouts=5, p95=300.0, streak=5), times=2)

    assert run.state.windows[0] == [_at(0), "degraded", pytest.approx(50.0), 300.0]
    assert run.state.windows[1][0] == _at(10)


def test_window_verdicts_are_capped_at_720_dropping_the_oldest():
    run = Runner(_engine())
    run.feed(DEGRADED, times=802)

    assert len(run.state.windows) == 720
    assert run.state.windows[0][0] == _at(820)  # 802 windows recorded, the first 82 dropped
    assert run.state.windows[-1][0] == _at(8010)


# ---------------------------------------------------------------------------
# closing
# ---------------------------------------------------------------------------

def test_close_after_stabilization_with_recovered():
    run = Runner(_engine())
    run.feed(DEGRADED, times=2)  # open, ended_at = 20 s

    updates = run.feed(HEALTHY, times=5)  # five healthy windows = 50 s

    assert [e.type for e in updates] == ["updated"] * 5
    assert run.state.status == "open"
    assert run.state.healthy_seconds == pytest.approx(50.0)

    events = run.feed(HEALTHY)

    assert [e.type for e in events] == ["closed"]
    closed = events[0]
    assert closed.close_reason == "recovered"
    assert closed.state.ended_at == _at(20)  # last degraded window end, not the close time
    assert closed.state.closed_at == _at(80)
    assert closed.state.close_reason == "recovered"
    # the state is ready for the next incident
    assert run.state.status == "closed"
    assert run.state.incident_id is None
    assert run.state.windows == []


def test_a_degraded_window_resets_the_healthy_accumulation():
    run = Runner(_engine())
    run.feed(DEGRADED, times=2)
    run.feed(HEALTHY, times=5)

    run.feed(DEGRADED)  # 70 s -> 80 s

    assert run.state.healthy_seconds == pytest.approx(0.0)
    assert run.state.ended_at == _at(80)
    assert [e.type for e in run.feed(HEALTHY, times=5)] == ["updated"] * 5
    assert run.state.status == "open"

    events = run.feed(HEALTHY)

    assert [e.type for e in events] == ["closed"]
    assert events[0].state.ended_at == _at(80)


def test_unknown_windows_close_the_incident_with_no_data():
    run = Runner(_engine())
    run.feed(DEGRADED, times=2)

    assert [e.type for e in run.feed(UNKNOWN, times=29)] == ["updated"] * 29
    assert run.state.status == "open"
    assert run.state.unknown_seconds == pytest.approx(290.0)

    events = run.feed(UNKNOWN)

    assert [e.type for e in events] == ["closed"]
    assert events[0].close_reason == "no_data"
    assert events[0].state.ended_at == _at(20)  # last degraded window end
    assert events[0].state.closed_at == _at(320)


def test_a_measured_window_breaks_the_unknown_run():
    run = Runner(_engine())
    run.feed(DEGRADED, times=2)
    run.feed(UNKNOWN, times=20)

    run.feed(HEALTHY)

    assert run.state.unknown_seconds == pytest.approx(0.0)


def test_close_all_closes_open_incidents_with_shutdown():
    engine = _engine()
    run = Runner(engine)
    pending = Runner(engine, target_id=2)
    run.feed(DEGRADED, times=2)
    pending.feed(DEGRADED)

    events = engine.close_all()

    assert [(e.type, e.close_reason, e.target_id) for e in events] == [("closed", "shutdown", 1)]
    assert events[0].state.ended_at == _at(20)
    assert events[0].state.closed_at == _at(20)
    assert run.state.status == "closed"
    assert pending.state.status == "closed"  # nothing was persisted for a pending key
    assert engine.close_all() == []


def test_a_new_burst_after_a_close_opens_a_new_incident():
    run = Runner(_engine())
    run.feed(DEGRADED, times=2)
    run.engine.attach_incident_id(1, "icmp", 42)
    run.feed(HEALTHY, times=6)  # closed

    run.feed(DEGRADED)
    events = run.feed(DEGRADED)

    assert [e.type for e in events] == ["opened"]
    assert events[0].state.incident_id is None
    assert events[0].state.started_at == _at(80)
    assert events[0].state.windows_degraded == 2


def test_keys_are_independent():
    engine = _engine()
    first = Runner(engine, target_id=1)
    second = Runner(engine, target_id=1, protocol="tcp")
    third = Runner(engine, target_id=2)

    first.feed(DEGRADED, times=2)
    second.feed(DEGRADED)
    third.feed(HEALTHY, times=2)

    assert first.state.status == "open"
    assert second.state.status == "pending"
    assert third.state.status == "closed"


def test_attach_incident_id_is_kept_in_the_state_and_in_later_events():
    run = Runner(_engine())
    run.feed(DEGRADED, times=2)
    run.engine.attach_incident_id(1, "icmp", 7)

    events = run.feed(DEGRADED)

    assert run.state.incident_id == 7
    assert events[0].state.incident_id == 7


# ---------------------------------------------------------------------------
# persistence contract
# ---------------------------------------------------------------------------

def test_incident_row_from_state_only_uses_real_incident_columns():
    settings = IncidentSettings()
    run = Runner(IncidentEngine(settings, probe_interval_seconds=lambda target_id: 1.0))
    run.feed(_stats(ok=5, timeouts=5, p95=300.0, streak=5), times=2)
    run.feed(HEALTHY, times=6)
    state = run.feed(DEGRADED, times=2)[0].state

    row = incidents.incident_row_from_state(state, 1, "icmp", settings, 1.0)

    # A subset, not an equality: the engine describes what it measured, and the
    # table also carries columns nobody measures — `expected*` is decided when
    # the incident closes, by rules this module knows nothing about. What must
    # hold is that the engine invents no column of its own.
    assert set(row) <= quality_db.INCIDENT_COLUMNS
    assert set(row) >= quality_db.INCIDENT_COLUMNS - {
        "expected",
        "expected_source",
        "expected_rule_id",
    }
    assert row["target_id"] == 1
    assert row["protocol"] == "icmp"
    assert row["kind"] == "degraded"
    assert row["started_at"] == _at(80)
    assert row["ended_at"] == _at(100)
    assert row["closed_at"] is None
    assert row["close_reason"] is None
    assert row["window_seconds"] == 10
    assert row["probe_interval_seconds"] == 1.0
    assert row["windows_degraded"] == 2
    assert json.loads(row["summary_json"]) == [
        [_at(80), "degraded", pytest.approx(30.0), None],
        [_at(90), "degraded", pytest.approx(30.0), None],
    ]


def test_incident_row_of_a_closed_incident_carries_the_close():
    settings = IncidentSettings()
    run = Runner(IncidentEngine(settings, probe_interval_seconds=lambda target_id: 1.0))
    run.feed(OUTAGE, times=2)
    closed = run.feed(HEALTHY, times=6)[-1]

    row = incidents.incident_row_from_state(closed.state, 1, "icmp", settings, 1.0)

    assert row["kind"] == "outage"
    assert row["closed_at"] == _at(80)
    assert row["close_reason"] == "recovered"
    assert row["ended_at"] == _at(20)
    assert row["peak_loss_pct"] == pytest.approx(100.0)


def test_probe_interval_is_taken_from_the_injected_callable_when_opening():
    engine = IncidentEngine(IncidentSettings(), probe_interval_seconds=lambda target_id: 0.5 * target_id)
    Runner(engine, target_id=4).feed(DEGRADED, times=2)

    assert engine.probe_interval_for(4, "icmp") == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# the point of the whole exercise
# ---------------------------------------------------------------------------

def test_daily_average_hides_the_burst_but_the_engine_catches_it():
    settings = IncidentSettings()
    run = Runner(IncidentEngine(settings, probe_interval_seconds=lambda target_id: 1.0))
    burst = _stats(ok=7, timeouts=3, streak=3)

    run.feed(burst)
    events = run.feed(burst)

    rest_of_the_day = ProbeStats(attempts=86380, ok=86380, loss_pct=0.0)
    daily = stats.merge_counters([burst, burst, rest_of_the_day])

    assert daily.attempts == 86400
    assert daily.loss_pct == pytest.approx(6 / 86400 * 100)
    assert daily.loss_pct < settings.loss_pct_threshold  # a daily average would say "fine"
    assert [e.type for e in events] == ["opened"]  # the window-level engine does not
    assert events[0].state.peak_loss_pct == pytest.approx(30.0)
