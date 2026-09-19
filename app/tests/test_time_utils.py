"""Stored timestamps have one fixed format so that string order == time order (spec §1)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from speedtest_app.db import _utc_now_iso
from speedtest_app.time_utils import parse_dt, to_iso_z

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_whole_second_keeps_the_millisecond_field():
    assert to_iso_z(datetime(2026, 1, 10, 10, 0, 0, tzinfo=timezone.utc)) == "2026-01-10T10:00:00.000Z"
    assert ISO_Z.match(_utc_now_iso())


def test_string_order_matches_chronological_order_within_a_second():
    base = datetime(2026, 1, 10, 10, 0, 0, tzinfo=timezone.utc)
    stamps = [
        to_iso_z(base),
        to_iso_z(base + timedelta(milliseconds=5)),
        to_iso_z(base + timedelta(milliseconds=500)),
        to_iso_z(base + timedelta(seconds=1)),
    ]

    assert stamps == sorted(stamps)
    assert all(ISO_Z.match(s) for s in stamps)


def test_microseconds_are_truncated_not_rounded():
    base = datetime(2026, 1, 10, 10, 0, 0, 999_999, tzinfo=timezone.utc)

    assert to_iso_z(base) == "2026-01-10T10:00:00.999Z"


def test_naive_and_offset_datetimes_are_normalised_to_utc():
    assert to_iso_z(datetime(2026, 1, 10, 10, 0, 0)) == "2026-01-10T10:00:00.000Z"
    other_zone = timezone(timedelta(hours=2))
    assert to_iso_z(datetime(2026, 1, 10, 12, 0, 0, tzinfo=other_zone)) == "2026-01-10T10:00:00.000Z"


def test_parse_dt_round_trips_every_fraction_length():
    expected = datetime(2026, 1, 10, 10, 0, 0, 123_000, tzinfo=timezone.utc)

    assert parse_dt("2026-01-10T10:00:00Z") == datetime(2026, 1, 10, 10, 0, 0, tzinfo=timezone.utc)
    assert parse_dt("2026-01-10T10:00:00.123Z") == expected
    assert parse_dt("2026-01-10T10:00:00.123456Z") == expected.replace(microsecond=123_456)
    assert to_iso_z(parse_dt("2026-01-10T10:00:00.123456Z")) == "2026-01-10T10:00:00.123Z"
    assert to_iso_z(parse_dt("2026-01-10T10:00:00Z")) == "2026-01-10T10:00:00.000Z"
