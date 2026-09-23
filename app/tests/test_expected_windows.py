"""Matching of expected maintenance windows (design spec §4).

Every assertion here is about the clock on the wall in Europe/Warsaw, so the
zone is pinned for the module; the UTC instants in the tests are the ones a
probe would actually record. The two DST days are the reason this module
exists at all — a rule is a local time, and local times are not uniform.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from speedtest_app.expected_windows import ExpectedWindow, match, parse_windows


@pytest.fixture(autouse=True)
def warsaw(monkeypatch):
    """Pin the local zone. `test_time_utils.py` pins none, so this is its own."""
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def window(**over):
    base = dict(
        id=1, name="restart routera", time_from="02:55", time_to="03:15",
        days=frozenset({0, 1, 2, 3, 4, 5, 6}), target_id=None, note=None,
    )
    base.update(over)
    return ExpectedWindow(**base)


def utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# Europe/Warsaw in September is UTC+2, so local 03:00 is 01:00Z.

def test_outage_inside_the_window_matches():
    assert match([window()], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4)) is not None


def test_outage_that_overruns_the_window_does_not_match():
    assert match([window()], utc(2026, 9, 21, 0, 58), utc(2026, 9, 21, 4, 30)) is None


def test_outage_starting_before_the_window_does_not_match():
    assert match([window()], utc(2026, 9, 21, 0, 50), utc(2026, 9, 21, 1, 4)) is None


def test_bounds_are_inclusive():
    assert match([window()], utc(2026, 9, 21, 0, 55), utc(2026, 9, 21, 1, 15)) is not None


def test_open_outage_never_matches():
    assert match([window()], utc(2026, 9, 21, 1, 0), None) is None


def test_weekday_scoping():
    monday_only = window(days=frozenset({0}))
    # 2026-09-21 is a Monday, 2026-09-20 a Sunday (local time decides).
    assert match([monday_only], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4)) is not None
    assert match([monday_only], utc(2026, 9, 20, 1, 0), utc(2026, 9, 20, 1, 4)) is None


def test_target_scoping():
    scoped = window(target_id=3)
    assert match([scoped], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4), target_id=3) is not None
    assert match([scoped], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4), target_id=4) is None
    assert match([scoped], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4), target_id=None) is None
    assert match([window()], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4), target_id=7) is not None


def test_overnight_window_spanning_midnight():
    # 23:50 Monday -> 00:10 Tuesday local; the outage is at 00:00 local Tuesday.
    w = window(time_from="23:50", time_to="00:10", days=frozenset({0}))
    assert match([w], utc(2026, 9, 21, 22, 0), utc(2026, 9, 21, 22, 5)) is not None
    # The same clock time on Tuesday belongs to a window that starts Tuesday,
    # which this Monday-only rule does not cover.
    assert match([w], utc(2026, 9, 22, 22, 0), utc(2026, 9, 22, 22, 5)) is None


# -- the two DST days ------------------------------------------------------
#
# Verified against the tz database: Europe/Warsaw switches at 01:00Z on both
# 2026-03-29 and 2026-10-25. In spring local 01:59:59+01:00 is followed by
# 03:00:00+02:00, so 02:00–02:59 never happens; in autumn 02:59:59+02:00 is
# followed by 02:00:00+01:00, so 02:00–02:59 happens twice.

def test_spring_forward_skipped_hour_produces_no_window():
    # The naive reading — "02:30 means 02:30+01:00" — puts this window at
    # 01:30Z–01:45Z, which contains the outage exactly. It must not.
    w = window(time_from="02:30", time_to="02:45")
    assert match([w], utc(2026, 3, 29, 1, 30), utc(2026, 3, 29, 1, 40)) is None


def test_spring_forward_window_before_the_jump_keeps_the_winter_offset():
    """The zone must be rule-aware, not frozen at today's offset.

    Local 01:10 on the morning of the change is still +01:00, i.e. 00:10Z. A
    matcher holding a fixed +02:00 — which is what `local_tz()` hands out in
    September — would place this window on the previous evening and miss.
    """
    w = window(time_from="01:10", time_to="01:20")
    assert match([w], utc(2026, 3, 29, 0, 12), utc(2026, 3, 29, 0, 18)) is not None


def test_autumn_fold_matches_either_instance():
    # Local 02:20–02:40 happens twice: 00:20Z–00:40Z, then 01:20Z–01:40Z.
    w = window(time_from="02:20", time_to="02:40")
    assert match([w], utc(2026, 10, 25, 0, 25), utc(2026, 10, 25, 0, 35)) is not None
    assert match([w], utc(2026, 10, 25, 1, 25), utc(2026, 10, 25, 1, 35)) is not None


def test_outage_spanning_both_fold_instances_does_not_match():
    """An hour of downtime is an hour, however often the clock reads 02:30.

    Each instance of the window is twenty real minutes long; pairing every
    start with every end would stretch it to cover the whole repeated hour.
    """
    w = window(time_from="02:20", time_to="02:40")
    assert match([w], utc(2026, 10, 25, 0, 30), utc(2026, 10, 25, 1, 30)) is None


# -- rubbish in, no exception out ------------------------------------------

@pytest.mark.parametrize(
    "broken",
    [
        {"days": frozenset()},
        {"time_from": "krowa"},
        {"time_to": "25:00"},
        {"time_from": "03:00", "time_to": "03:00"},
    ],
)
def test_broken_rule_is_skipped_not_raised(broken):
    assert match([window(**broken)], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4)) is None


def test_reversed_timestamps_do_not_match():
    assert match([window()], utc(2026, 9, 21, 1, 10), utc(2026, 9, 21, 1, 0)) is None


def test_first_match_wins_by_id():
    first = window(id=1, name="pierwsze")
    second = window(id=2, name="drugie")
    assert match([first, second], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4)).name == "pierwsze"


def test_parse_windows_reads_db_rows():
    rows = [
        {"id": 5, "name": "noc", "time_from": "02:55", "time_to": "03:15",
         "days": "[0,1,2,3,4,5,6]", "target_id": None, "note": "router", "enabled": 1},
        {"id": 6, "name": "wyłączone", "time_from": "01:00", "time_to": "02:00",
         "days": "[0]", "target_id": 2, "note": None, "enabled": 0},
    ]
    parsed = parse_windows(rows)
    assert [w.id for w in parsed] == [5]           # disabled rules are dropped
    assert parsed[0].days == frozenset({0, 1, 2, 3, 4, 5, 6})


def test_parse_windows_survives_broken_days_json():
    rows = [{"id": 1, "name": "z", "time_from": "01:00", "time_to": "02:00",
             "days": "not json", "target_id": None, "note": None, "enabled": 1}]
    assert parse_windows(rows)[0].days == frozenset()
